//! Bounded one-shot IPC for the closed bundled backend services.

use super::backend::{Backend, Service};
use super::lifecycle::Ticket;
use serde_json::Value;
use std::io::Write;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::sync::Mutex;
use tokio::task::JoinHandle;
use tokio::time::Instant;

const MIB: usize = 1024 * 1024;

#[derive(Clone, Copy)]
struct Limits {
    input: usize,
    output: usize,
    stderr: usize,
    duration: Duration,
}

#[derive(Clone, Copy)]
struct Plan {
    label: &'static str,
    limits: Limits,
    mutation: bool,
    doctor: bool,
    /// The update service's refusals reach the caller as {"ok": false, "code"}:
    /// the panel needs the typed code. Only the code, never the backend's text.
    typed_refusals: bool,
}

impl Plan {
    fn new(service: Service, operation: &str) -> Result<Self, String> {
        let (label, allowed, mutation, seconds, input, output) = match service {
            Service::Doctor => ("Local setup", operation.is_empty(), false, 30, 0, MIB),
            Service::Library => {
                let allowed = matches!(
                    operation,
                    "snapshot"
                        | "storage_status"
                        | "set_storage_location"
                        | "mark_seen"
                        | "open"
                        | "open_folder"
                        | "pick_folder"
                        | "prepare"
                        | "commit"
                        | "cancel"
                        | "rename"
                        | "copy"
                        | "move"
                        | "delete"
                        | "delete_forever"
                        | "register_file"
                        | "register_link"
                );
                let mutation = !matches!(operation, "snapshot" | "storage_status" | "pick_folder");
                let seconds = match operation {
                    "commit" | "prepare" => 1800,
                    "pick_folder" => 300,
                    _ => 120,
                };
                (
                    "Library",
                    allowed,
                    mutation,
                    seconds,
                    8 * MIB,
                    if mutation { 64 * MIB } else { 32 * MIB },
                )
            }
            Service::Memory => {
                let allowed = matches!(
                    operation,
                    "snapshot"
                        | "status"
                        | "list"
                        | "set_enabled"
                        | "delete"
                        | "approve"
                        | "retry_pending"
                        | "reindex"
                        | "create"
                        | "supersede"
                        | "retrieve"
                );
                let mutation = !matches!(operation, "snapshot" | "status" | "list" | "retrieve");
                let seconds = if matches!(operation, "retry_pending" | "reindex") {
                    1800
                } else {
                    360
                };
                ("Memory", allowed, mutation, seconds, 8 * MIB, 32 * MIB)
            }
            Service::Settings => (
                "Settings",
                matches!(
                    operation,
                    "snapshot"
                        | "get"
                        | "set"
                        | "update"
                        | "reset"
                        | "reset_all"
                        | "audio_devices"
                        | "audio_output_devices"
                ),
                !matches!(
                    operation,
                    "snapshot" | "get" | "audio_devices" | "audio_output_devices"
                ),
                30,
                MIB,
                4 * MIB,
            ),
            Service::Permissions => (
                "Permissions",
                matches!(
                    operation,
                    "snapshot"
                        | "set_policy"
                        | "set_custom_rule"
                        | "reset_permission"
                        | "approval_secret_status"
                        | "approval_secret_set"
                ),
                !matches!(operation, "snapshot" | "approval_secret_status"),
                30,
                MIB,
                4 * MIB,
            ),
            // Reads, approval steps and the native pickers only. "launch" is NEVER a one-shot
            // request: this path waits for its child and kills it at its deadline, which must not
            // happen to an update guard or the installer it hands over to. It runs only through
            // the update lifecycle (lifecycle.rs), which never kills it.
            Service::Update => (
                "Update",
                matches!(
                    operation,
                    "status"
                        | "discover"
                        | "verify"
                        | "request"
                        | "approve"
                        | "discard"
                        | "startup_check"
                        | "pick_package"
                        | "pick_folder"
                ),
                !matches!(
                    operation,
                    "status" | "discover" | "verify" | "startup_check" | "pick_package" | "pick_folder"
                ),
                match operation {
                    "verify" | "request" | "approve" => 900,
                    "pick_package" | "pick_folder" => 300,
                    _ => 60,
                },
                64 * 1024,
                MIB,
            ),
            Service::Bridge | Service::Supervisor | Service::Audio => {
                return Err("This backend service does not support one-shot IPC.".into())
            }
        };
        if !allowed {
            return Err(format!("{label} operation is unsupported."));
        }
        Ok(Self {
            label,
            limits: Limits {
                input,
                output,
                stderr: 64 * 1024,
                duration: Duration::from_secs(seconds),
            },
            mutation,
            doctor: matches!(service, Service::Doctor),
            typed_refusals: matches!(service, Service::Update),
        })
    }

    fn error(self, failure: Failure, started: bool) -> String {
        let reason = match failure {
            Failure::Input => "request exceeds the input limit",
            Failure::Start => "backend could not start",
            Failure::Timeout => "backend exceeded its deadline",
            Failure::Cancelled => "request was cancelled",
            Failure::Stdout => "response exceeds the output limit",
            Failure::Stderr => "diagnostics exceed the output limit",
            Failure::Io => "backend pipe or process became unavailable",
            Failure::Exit => "backend did not complete successfully",
            Failure::Reply => "backend returned an invalid or unsupported response",
            Failure::Refused => "operation was refused",
        };
        let uncertainty = if self.mutation && started {
            " The operation outcome is unknown; inspect the current state before taking further action. Do not automatically retry."
        } else {
            ""
        };
        format!("{} {reason}.{uncertainty}", self.label)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Failure {
    Input,
    Start,
    Timeout,
    Cancelled,
    Stdout,
    Stderr,
    Io,
    Exit,
    Reply,
    Refused,
}

/// `ticket` is the service-gate admission (lifecycle.rs). It moves into the
/// owned worker with the settings guard and is released only after the child is
/// reaped, so an update launch's drain waits for this whole request. Only the
/// update lifecycle itself, which owns the closed gate, passes None.
pub(super) async fn request(
    backend: &Backend,
    service: Service,
    operation: &str,
    payload: Option<Value>,
    settings_gate: Option<Arc<Mutex<()>>>,
    ticket: Option<Ticket>,
) -> Result<Value, String> {
    let operation = operation.trim().to_ascii_lowercase();
    let plan = Plan::new(service, &operation)?;
    let deadline = Instant::now() + plan.limits.duration;
    let guard = match settings_gate {
        Some(gate) => Some(
            tokio::time::timeout_at(deadline, gate.lock_owned())
                .await
                .map_err(|_| plan.error(Failure::Timeout, false))?,
        ),
        None => None,
    };
    let backend = backend.clone();
    dispatch(
        move || {
            let mut command = backend.command(service)?;
            if plan.doctor {
                command.arg("--models");
            } else {
                command.arg(operation);
            }
            Ok(command)
        },
        plan,
        payload,
        (guard, ticket),
        deadline,
    )
    .await
}

struct CancelOnDrop(Arc<AtomicBool>);
impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}

async fn dispatch<F, G>(
    build: F,
    plan: Plan,
    payload: Option<Value>,
    guard: G,
    deadline: Instant,
) -> Result<Value, String>
where
    F: FnOnce() -> Result<Command, String> + Send + 'static,
    G: Send + 'static,
{
    let cancellation = Arc::new(AtomicBool::new(false));
    let _cancel_on_drop = CancelOnDrop(cancellation.clone());
    // A dropped caller only signals cancellation. This owned supervisor stays
    // alive to close its pipes, kill/reap its exact child, and release the gate.
    tokio::spawn(async move {
        // The owned settings guard outlives cancellation of the awaiting UI
        // future and is released only after this worker reaps its child.
        let _guard = guard;
        let preparation = tokio::task::spawn_blocking(build);
        // A blocking OS path lookup may itself be uninterruptible. Bound the
        // async wait and discard any late Command; only this supervisor can
        // launch it. No service is spawned after timeout or caller cancellation.
        while !preparation.is_finished() {
            if cancellation.load(Ordering::Acquire) {
                preparation.abort();
                return Err(plan.error(Failure::Cancelled, false));
            }
            if Instant::now() >= deadline {
                preparation.abort();
                return Err(plan.error(Failure::Timeout, false));
            }
            tokio::time::sleep_until(std::cmp::min(
                deadline,
                Instant::now() + Duration::from_millis(10),
            ))
            .await;
        }
        let command = preparation
            .await
            .map_err(|_| plan.error(Failure::Start, false))?
            .map_err(|_| plan.error(Failure::Start, false))?;
        execute(command, plan, payload, &cancellation, deadline).await
    })
    .await
    .map_err(|_| plan.error(Failure::Io, plan.mutation))?
}

struct BoundedInput {
    bytes: Vec<u8>,
    limit: usize,
}
impl Write for BoundedInput {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.bytes.len()) {
            return Err(std::io::Error::other("Bounded IPC input exceeded."));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

struct PipeTask<T>(Option<JoinHandle<T>>);
impl<T> PipeTask<T> {
    fn finished(&self) -> bool {
        self.0.as_ref().is_some_and(JoinHandle::is_finished)
    }
    async fn abort_and_join(&mut self) {
        if let Some(task) = self.0.take() {
            task.abort();
            let _ = task.await;
        }
    }
}
impl<T> Drop for PipeTask<T> {
    fn drop(&mut self) {
        if let Some(task) = &self.0 {
            task.abort();
        }
    }
}

async fn read_bounded(
    mut pipe: impl AsyncRead + Unpin,
    limit: usize,
    overflow: Failure,
    keep: bool,
    mut exited: tokio::sync::watch::Receiver<bool>,
    deadline: Instant,
) -> Result<Vec<u8>, Failure> {
    let mut bytes = Vec::new();
    let mut total = 0usize;
    let mut buffer = [0u8; 8192];
    let mut drain_deadline = None;
    loop {
        // Exact-child exit seals the response. Drain already-buffered output
        // for a bounded grace period; a document may own the opposite handle
        // indefinitely. This grace is included in the original deadline.
        let count = if let Some(until) = drain_deadline {
            match tokio::time::timeout_at(until, pipe.read(&mut buffer)).await {
                Ok(result) => result.map_err(|_| Failure::Io)?,
                Err(_) => return Ok(bytes),
            }
        } else {
            let ready = {
                use std::future::Future;
                let mut changed = std::pin::pin!(exited.changed());
                let mut read = std::pin::pin!(pipe.read(&mut buffer));
                std::future::poll_fn(|context| {
                    if changed.as_mut().poll(context).is_ready() {
                        std::task::Poll::Ready(None)
                    } else {
                        read.as_mut().poll(context).map(Some)
                    }
                })
                .await
            };
            match ready {
                Some(result) => result.map_err(|_| Failure::Io)?,
                None => {
                    drain_deadline = Some(std::cmp::min(
                        deadline,
                        Instant::now() + Duration::from_millis(50),
                    ));
                    continue;
                }
            }
        };
        if count == 0 {
            return Ok(bytes);
        }
        if count > limit.saturating_sub(total) {
            return Err(overflow);
        }
        total += count;
        if keep {
            bytes.extend_from_slice(&buffer[..count]);
        }
    }
}

#[cfg(not(windows))]
async fn spawn_with_pipes(
    mut command: tokio::process::Command,
    _deadline: Instant,
) -> Result<
    (
        tokio::process::Child,
        tokio::process::ChildStdin,
        tokio::process::ChildStdout,
        tokio::process::ChildStderr,
    ),
    Failure,
> {
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| Failure::Start)?;
    let stdin = child.stdin.take().ok_or(Failure::Io)?;
    let stdout = child.stdout.take().ok_or(Failure::Io)?;
    let stderr = child.stderr.take().ok_or(Failure::Io)?;
    Ok((child, stdin, stdout, stderr))
}

#[cfg(windows)]
struct PrivatePipe {
    server: tokio::net::windows::named_pipe::NamedPipeServer,
    unfinished: bool,
}

#[cfg(windows)]
impl tokio::io::AsyncRead for PrivatePipe {
    fn poll_read(
        mut self: std::pin::Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
        buffer: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        let before = buffer.filled().len();
        let result = std::pin::Pin::new(&mut self.server).poll_read(context, buffer);
        if matches!(result, std::task::Poll::Ready(Ok(()))) && before == buffer.filled().len() {
            self.unfinished = false;
        }
        result
    }
}

#[cfg(windows)]
impl tokio::io::AsyncWrite for PrivatePipe {
    fn poll_write(
        mut self: std::pin::Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
        bytes: &[u8],
    ) -> std::task::Poll<std::io::Result<usize>> {
        std::pin::Pin::new(&mut self.server).poll_write(context, bytes)
    }
    fn poll_flush(
        mut self: std::pin::Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::pin::Pin::new(&mut self.server).poll_flush(context)
    }
    fn poll_shutdown(
        mut self: std::pin::Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        // Tokio/Mio's named-pipe flush/shutdown is a no-op; write_all can
        // return while its final overlapped write is pending. An empty write
        // must first wait for that existing write to complete. It introduces
        // no request bytes, and its own pending I/O can safely be cancelled.
        match std::pin::Pin::new(&mut self.server).poll_write(context, &[]) {
            std::task::Poll::Ready(Ok(_)) => {
                self.unfinished = false;
                std::task::Poll::Ready(Ok(()))
            }
            std::task::Poll::Ready(Err(error)) => std::task::Poll::Ready(Err(error)),
            std::task::Poll::Pending => std::task::Poll::Pending,
        }
    }
}

#[cfg(windows)]
impl Drop for PrivatePipe {
    fn drop(&mut self) {
        use std::os::windows::io::AsRawHandle;
        // Mio intentionally flushes pending writes on Drop. Stop must cancel
        // those too, even when an inherited child never consumes stdin.
        unsafe {
            windows_sys::Win32::System::IO::CancelIoEx(
                self.server.as_raw_handle(),
                std::ptr::null(),
            )
        };
        if self.unfinished {
            let _ = self.server.disconnect();
        }
    }
}

#[cfg(windows)]
async fn private_pipe(
    write_to_child: bool,
    deadline: Instant,
) -> Result<(PrivatePipe, Stdio), Failure> {
    use tokio::net::windows::named_pipe::ServerOptions;
    use windows_sys::Win32::Security::Cryptography::{
        BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG,
    };
    let mut nonce = [0u8; 16];
    // Windows system CSPRNG; the endpoint never comes from frontend input.
    let status = unsafe {
        BCryptGenRandom(
            std::ptr::null_mut(),
            nonce.as_mut_ptr(),
            nonce.len() as u32,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status < 0 {
        return Err(Failure::Start);
    }
    let name = format!(
        r"\\.\pipe\qronos-ipc-{}-{:032x}",
        std::process::id(),
        u128::from_le_bytes(nonce)
    );
    let server = ServerOptions::new()
        .first_pipe_instance(true)
        .max_instances(1)
        .reject_remote_clients(true)
        .access_inbound(!write_to_child)
        .access_outbound(write_to_child)
        .create(name.as_str())
        .map_err(|_| Failure::Start)?;
    // Open our sole client synchronously while the fresh server is listening,
    // before launching the child. No external client or reconnect is needed.
    let client = std::fs::OpenOptions::new()
        .read(write_to_child)
        .write(!write_to_child)
        .open(&name)
        .map_err(|_| Failure::Start)?;
    tokio::time::timeout_at(deadline, server.connect())
        .await
        .map_err(|_| Failure::Timeout)?
        .map_err(|_| Failure::Io)?;
    Ok((
        PrivatePipe {
            server,
            unfinished: true,
        },
        Stdio::from(client),
    ))
}

#[cfg(windows)]
async fn spawn_with_pipes(
    mut command: tokio::process::Command,
    deadline: Instant,
) -> Result<(tokio::process::Child, PrivatePipe, PrivatePipe, PrivatePipe), Failure> {
    let (stdin, input) = private_pipe(true, deadline).await?;
    let (stdout, output) = private_pipe(false, deadline).await?;
    let (stderr, errors) = private_pipe(false, deadline).await?;
    if Instant::now() >= deadline {
        return Err(Failure::Timeout);
    }
    let child = command
        .stdin(input)
        .stdout(output)
        .stderr(errors)
        .kill_on_drop(true)
        .creation_flags(super::CREATE_NO_WINDOW)
        .spawn()
        .map_err(|_| Failure::Start)?;
    Ok((child, stdin, stdout, stderr))
}

async fn execute(
    command: Command,
    plan: Plan,
    payload: Option<Value>,
    cancellation: &AtomicBool,
    deadline: Instant,
) -> Result<Value, String> {
    let checkpoint = || {
        if cancellation.load(Ordering::Acquire) {
            Err(Failure::Cancelled)
        } else if Instant::now() >= deadline {
            Err(Failure::Timeout)
        } else {
            Ok(())
        }
    };
    checkpoint().map_err(|failure| plan.error(failure, false))?;
    let mut input = BoundedInput {
        bytes: Vec::new(),
        limit: plan.limits.input,
    };
    if !plan.doctor {
        serde_json::to_writer(
            &mut input,
            &payload.unwrap_or_else(|| serde_json::json!({})),
        )
        .map_err(|_| plan.error(Failure::Input, false))?;
    }
    checkpoint().map_err(|failure| plan.error(failure, false))?;
    let command = tokio::process::Command::from(command);
    let (mut child, mut stdin, stdout, stderr) = spawn_with_pipes(command, deadline)
        .await
        .map_err(|failure| plan.error(failure, false))?;
    let result = async {
        // Each task owns one pipe. Aborting a task drops the pipe even if a
        // descendant inherited the opposite end. Never kill a document app.
        let mut input = PipeTask(Some(tokio::spawn(async move {
            stdin
                .write_all(&input.bytes)
                .await
                .map_err(|_| Failure::Io)?;
            stdin.shutdown().await.map_err(|_| Failure::Io)
        })));
        let (exited, exit_signal) = tokio::sync::watch::channel(false);
        let mut output = PipeTask(Some(tokio::spawn(read_bounded(
            stdout,
            plan.limits.output,
            Failure::Stdout,
            true,
            exit_signal.clone(),
            deadline,
        ))));
        let mut errors = PipeTask(Some(tokio::spawn(read_bounded(
            stderr,
            plan.limits.stderr,
            Failure::Stderr,
            false,
            exit_signal,
            deadline,
        ))));
        let (mut written, mut bytes, mut drained, mut status) = (false, None, false, None);
        let result = async {
            loop {
                checkpoint()?;
                // Only await completed tasks. A single process-wide deadline
                // covers blocked stdin, headers/body, exit and inherited pipes.
                if !written && input.finished() {
                    input.0.take().unwrap().await.map_err(|_| Failure::Io)??;
                    written = true;
                }
                if bytes.is_none() && output.finished() {
                    bytes = Some(output.0.take().unwrap().await.map_err(|_| Failure::Io)??);
                }
                if !drained && errors.finished() {
                    errors.0.take().unwrap().await.map_err(|_| Failure::Io)??;
                    drained = true;
                }
                if status.is_none() {
                    status = child.try_wait().map_err(|_| Failure::Io)?;
                    if status.is_some() {
                        let _ = exited.send(true);
                    }
                }
                if written && drained && bytes.is_some() && status.is_some() {
                    if !status.unwrap().success() {
                        return Err(Failure::Exit);
                    }
                    return Ok(bytes.unwrap());
                }
                tokio::time::sleep_until(std::cmp::min(
                    deadline,
                    Instant::now() + Duration::from_millis(10),
                ))
                .await;
            }
        }
        .await;
        input.abort_and_join().await;
        output.abort_and_join().await;
        errors.abort_and_join().await;
        result
    }
    .await;
    // The pipe-task guards have already cancelled/dropped the I/O futures.
    // kill targets this Child handle only; wait reaps it even after nonzero
    // exit. The owning task continues cleanup if its caller was cancelled.
    if result.is_err() {
        let _ = child.start_kill();
    }
    if child.wait().await.is_err() {
        return Err(plan.error(Failure::Io, true));
    }
    let output = result.map_err(|failure| plan.error(failure, true))?;
    checkpoint().map_err(|failure| plan.error(failure, true))?;
    // All closed CLI entrypoints emit one compact JSON line. Do not accept
    // truncated JSON or extra frames, even when a descendant retains stdout.
    let newline = output
        .iter()
        .position(|byte| *byte == b'\n')
        .ok_or_else(|| plan.error(Failure::Reply, true))?;
    if output[newline + 1..]
        .iter()
        .any(|byte| !byte.is_ascii_whitespace())
    {
        return Err(plan.error(Failure::Reply, true));
    }
    let value: Value =
        serde_json::from_slice(&output[..newline]).map_err(|_| plan.error(Failure::Reply, true))?;
    let valid = if plan.doctor {
        value.get("schemaVersion").and_then(Value::as_u64) == Some(1)
            && value.get("features").is_some_and(Value::is_object)
            && value.get("modelServer").is_some_and(Value::is_object)
    } else {
        value.is_object() && value.get("ok").and_then(Value::as_bool).is_some()
    };
    if !valid {
        return Err(plan.error(Failure::Reply, true));
    }
    if !plan.doctor && value.get("ok").and_then(Value::as_bool) != Some(true) {
        if plan.typed_refusals {
            let code = value
                .get("code")
                .and_then(Value::as_str)
                .filter(|code| {
                    (1..=48).contains(&code.len())
                        && code.bytes().all(|byte| byte.is_ascii_lowercase() || byte == b'_')
                })
                .unwrap_or("refused");
            return Ok(serde_json::json!({ "ok": false, "code": code }));
        }
        return Err(plan.error(Failure::Refused, true));
    }
    checkpoint().map_err(|failure| plan.error(failure, true))?;
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::AtomicU64;
    use sysinfo::{Pid, ProcessesToUpdate, System};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture {
        directory: PathBuf,
        marker: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let directory = std::env::temp_dir().join(format!(
                "qronos-ipc-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir_all(&directory).unwrap();
            let marker = directory.join("peer.pid");
            Self { directory, marker }
        }
        fn command(&self, mode: &str) -> Command {
            let python = std::env::var_os("QRONOS_TEST_PYTHON").unwrap_or_else(|| {
                if cfg!(windows) {
                    "python".into()
                } else {
                    "python3".into()
                }
            });
            let mut command = Command::new(python);
            command
                .arg("-u")
                .arg(
                    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                        .join("tests/fixtures/service_ipc_child.py"),
                )
                .arg(mode)
                .arg(&self.marker);
            command
        }
        fn gone(&self) -> bool {
            !process_exists(&self.marker)
        }
        fn assert_parent_handles_closed_and_descendant_alive(&self) {
            assert!(
                process_exists(&self.marker.with_extension("descendant")),
                "The independently opened synthetic document process must survive"
            );
            std::fs::write(self.marker.with_extension("probe"), "probe").unwrap();
            let observed = self.marker.with_extension("probe-result");
            let deadline = std::time::Instant::now() + Duration::from_secs(1);
            while !observed.exists() {
                assert!(std::time::Instant::now() < deadline);
                std::thread::sleep(Duration::from_millis(10));
            }
            assert_eq!(
                std::fs::read_to_string(observed).unwrap(),
                "[true, true, true]",
                "All parent pipe handles must close before reporting completion"
            );
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let descendant = self.marker.with_extension("descendant");
            let _ = std::fs::write(descendant.with_extension("stop"), "stop");
            let deadline = std::time::Instant::now() + Duration::from_secs(2);
            while process_exists(&descendant) && std::time::Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
            }
            let _ = std::fs::remove_dir_all(&self.directory);
        }
    }
    fn process_exists(marker: &std::path::Path) -> bool {
        let pid = std::fs::read_to_string(marker)
            .ok()
            .and_then(|s| s.parse::<usize>().ok());
        match pid {
            Some(pid) => {
                let mut system = System::new();
                system.refresh_processes(ProcessesToUpdate::Some(&[Pid::from(pid)]), true);
                system.process(Pid::from(pid)).is_some()
            }
            None => false,
        }
    }
    fn plan(mutation: bool) -> Plan {
        Plan {
            label: "Fixture",
            doctor: false,
            typed_refusals: false,
            mutation,
            limits: Limits {
                input: MIB,
                output: 128 * 1024,
                stderr: 128 * 1024,
                duration: Duration::from_secs(2),
            },
        }
    }
    fn run(
        fixture: &Fixture,
        mode: &str,
        plan: Plan,
        payload: Option<Value>,
    ) -> Result<Value, String> {
        let command = fixture.command(mode);
        tauri::async_runtime::block_on(dispatch(
            move || Ok(command),
            plan,
            payload,
            (),
            Instant::now() + plan.limits.duration,
        ))
    }
    #[test]
    fn simultaneous_pipe_sized_input_output_and_diagnostics_do_not_deadlock() {
        let fixture = Fixture::new();
        let reply = run(
            &fixture,
            "duplex",
            plan(false),
            Some(serde_json::json!({"data": "x".repeat(200_000)})),
        )
        .unwrap();
        assert_eq!(reply["ok"], true);
        assert_eq!(reply["padding"].as_str().unwrap().len(), 100_000);
        assert_eq!(reply["inputBytes"].as_u64().unwrap(), 200_011);
        assert!(fixture.gone());
    }
    #[test]
    fn doctor_drains_large_valid_report_before_process_exit() {
        let fixture = Fixture::new();
        let mut plan = plan(false);
        plan.doctor = true;
        plan.limits.input = 0;
        let reply = run(&fixture, "doctor", plan, None).unwrap();
        assert_eq!(reply["schemaVersion"], 1);
        assert!(fixture.gone());
    }
    #[test]
    fn normal_eof_waits_for_complete_overlapped_input_with_slow_readers() {
        for _ in 0..4 {
            let fixture = Fixture::new();
            let reply = run(
                &fixture,
                "slow_reader",
                plan(true),
                Some(serde_json::json!({"data": "x".repeat(200_000)})),
            )
            .unwrap();
            assert_eq!(reply["inputBytes"], 200_011);
            assert_eq!(reply["dataMatches"], true);
            assert!(fixture.gone());
        }
    }
    #[test]
    fn stdout_and_stderr_overflow_close_and_reap_exact_peer_without_echo() {
        for (mode, diagnostic) in [
            ("stdout_flood", "response"),
            ("stderr_flood", "diagnostics"),
        ] {
            let fixture = Fixture::new();
            let error = run(&fixture, mode, plan(true), None).unwrap_err();
            assert!(error.contains(diagnostic) && error.contains("outcome is unknown"));
            assert!(!error.contains("PRIVATE_FIXTURE"));
            assert!(fixture.gone());
        }
    }
    #[test]
    fn oversized_input_is_rejected_before_any_child_or_mutation() {
        let fixture = Fixture::new();
        let mut plan = plan(true);
        plan.limits.input = 32;
        let error = run(
            &fixture,
            "ok",
            plan,
            Some(serde_json::json!({"secret": "PRIVATE_FIXTURE".repeat(20)})),
        )
        .unwrap_err();
        assert!(error.contains("input limit") && !error.contains("outcome is unknown"));
        assert!(!error.contains("PRIVATE_FIXTURE") && !fixture.marker.exists());
    }
    #[test]
    fn nonzero_malformed_and_wrong_shapes_are_sanitized_after_reaping() {
        for mode in [
            "nonzero",
            "malformed",
            "wrong_shape",
            "refused",
            "multi_frame",
            "truncated",
            "inherited_truncated",
        ] {
            let fixture = Fixture::new();
            let error = run(&fixture, mode, plan(true), None).unwrap_err();
            assert!(!error.contains("PRIVATE_FIXTURE") && error.contains("outcome is unknown"));
            assert!(fixture.gone());
        }
    }
    #[test]
    fn absolute_deadline_covers_blocked_input_and_partial_body() {
        for mode in ["hang_input", "hang_body", "inherited_blocked_input"] {
            let fixture = Fixture::new();
            let mut plan = plan(true);
            plan.limits.duration = Duration::from_millis(500);
            let started = std::time::Instant::now();
            let error = run(
                &fixture,
                mode,
                plan,
                Some(serde_json::json!({"data": "x".repeat(200_000)})),
            )
            .unwrap_err();
            assert!(error.contains("deadline") && error.contains("outcome is unknown"));
            assert!(started.elapsed() < Duration::from_secs(2));
            assert!(
                fixture.marker.exists(),
                "The fixture must reach the intended blocked phase"
            );
            assert!(fixture.gone());
            if mode == "inherited_blocked_input" {
                fixture.assert_parent_handles_closed_and_descendant_alive();
            }
        }
    }
    #[test]
    fn framed_reply_and_child_exit_do_not_wait_for_or_kill_a_document_process() {
        let fixture = Fixture::new();
        let mut plan = plan(false);
        plan.limits.duration = Duration::from_millis(800);
        let started = std::time::Instant::now();
        let reply = run(&fixture, "inherited", plan, None).unwrap();
        assert_eq!(reply["ok"], true);
        assert!(started.elapsed() < Duration::from_secs(2));
        assert!(fixture.gone());
        // Returning within the deadline alone is insufficient: Windows
        // Blocking<StdFile> could leave background reads holding these pipes.
        fixture.assert_parent_handles_closed_and_descendant_alive();
    }
    #[test]
    fn cancelled_caller_reaps_child_before_releasing_owned_settings_gate() {
        for mode in ["hang_body", "hang_input"] {
            let fixture = Fixture::new();
            let command = fixture.command(mode);
            let gate = Arc::new(Mutex::new(()));
            tauri::async_runtime::block_on(async {
                let guard = gate.clone().lock_owned().await;
                let request = tokio::spawn(dispatch(
                    move || Ok(command),
                    plan(true),
                    Some(serde_json::json!({"data": "x".repeat(200_000)})),
                    guard,
                    Instant::now() + Duration::from_secs(2),
                ));
                let started = Instant::now();
                while !fixture.marker.exists() {
                    assert!(started.elapsed() < Duration::from_secs(2));
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
                request.abort();
                assert!(request.await.unwrap_err().is_cancelled());
                let _guard =
                    tokio::time::timeout(Duration::from_secs(1), gate.clone().lock_owned())
                        .await
                        .unwrap();
                assert!(
                    fixture.gone(),
                    "Serialization must remain held until the cancelled child's reap completes"
                );
            });
        }
    }

    #[test]
    fn preparation_deadline_never_launches_a_late_built_command() {
        let fixture = Fixture::new();
        let command = fixture.command("ok");
        let finished = Arc::new(AtomicBool::new(false));
        let marker = finished.clone();
        let mut limits = plan(true);
        limits.limits.duration = Duration::from_millis(80);
        let started = std::time::Instant::now();
        let error = tauri::async_runtime::block_on(dispatch(
            move || {
                std::thread::sleep(Duration::from_millis(250));
                marker.store(true, Ordering::Release);
                Ok(command)
            },
            limits,
            None,
            (),
            Instant::now() + limits.limits.duration,
        ))
        .unwrap_err();
        assert!(error.contains("deadline") && !error.contains("outcome is unknown"));
        assert!(started.elapsed() < Duration::from_millis(200));
        while !finished.load(Ordering::Acquire) {
            assert!(started.elapsed() < Duration::from_secs(2));
            std::thread::sleep(Duration::from_millis(10));
        }
        std::thread::sleep(Duration::from_millis(50));
        assert!(
            !fixture.marker.exists(),
            "A late preparation result must be dropped, never executed"
        );
    }

    #[test]
    fn preparation_cancellation_releases_gate_without_launching_a_service() {
        let fixture = Fixture::new();
        let command = fixture.command("ok");
        let preparing = Arc::new(AtomicBool::new(false));
        let finished = Arc::new(AtomicBool::new(false));
        let entered = preparing.clone();
        let done = finished.clone();
        let gate = Arc::new(Mutex::new(()));
        tauri::async_runtime::block_on(async {
            let guard = gate.clone().lock_owned().await;
            let caller = tokio::spawn(dispatch(
                move || {
                    entered.store(true, Ordering::Release);
                    std::thread::sleep(Duration::from_millis(250));
                    done.store(true, Ordering::Release);
                    Ok(command)
                },
                plan(true),
                None,
                guard,
                Instant::now() + Duration::from_secs(2),
            ));
            let until = Instant::now() + Duration::from_secs(2);
            while !preparing.load(Ordering::Acquire) {
                assert!(Instant::now() < until);
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
            caller.abort();
            assert!(caller.await.unwrap_err().is_cancelled());
            let _guard = tokio::time::timeout(Duration::from_millis(150), gate.lock_owned())
                .await
                .unwrap();
            while !finished.load(Ordering::Acquire) {
                assert!(Instant::now() < until);
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
            assert!(!fixture.marker.exists());
        });
    }

    #[test]
    #[ignore = "requires an explicitly selected trusted frozen runtime directory"]
    fn frozen_closed_services_use_private_transport_with_isolated_profile() {
        let directory = PathBuf::from(
            std::env::var_os("QRONOS_TEST_BUNDLED_RUNTIME_DIRECTORY").expect(
                "Set QRONOS_TEST_BUNDLED_RUNTIME_DIRECTORY to the trusted frozen bundle directory",
            ),
        );
        assert!(directory.is_absolute() && directory.is_dir());
        let fixture = Fixture::new();
        let profile = fixture.directory.join("profile");
        let backend = Backend::Packaged {
            directory,
            data_root: profile.clone(),
            asset_root: fixture.directory.join("assets"),
        };
        for service in [
            Service::Settings,
            Service::Permissions,
            Service::Library,
            Service::Memory,
            Service::Doctor,
        ] {
            let operation = if matches!(service, Service::Doctor) {
                ""
            } else {
                "snapshot"
            };
            let plan = Plan::new(service, operation).unwrap();
            let mut command = backend.command(service).unwrap();
            command.arg(if plan.doctor { "--models" } else { operation });
            for (key, file) in [
                ("QRONOS_SETTINGS_PATH", "settings.json"),
                ("QRONOS_CONVERSATION_HISTORY_PATH", "history.json"),
                ("QRONOS_PERMISSION_POLICY_PATH", "permissions.json"),
                ("QRONOS_PERMISSION_AUDIT_PATH", "audit.jsonl"),
                ("QRONOS_APPROVAL_SECRET_PATH", "approval.sqlite3"),
                ("QRONOS_QUEUE_STATE_PATH", "queue.json"),
            ] {
                command.env(key, profile.join(file));
            }
            command
                .env("USERPROFILE", &profile)
                .env("APPDATA", profile.join("roaming"))
                .env("LOCALAPPDATA", profile.join("local"))
                .env("QRONOS_OLLAMA_AUTOSTART", "0")
                .env("QRONOS_MEMORY_AUTOSTART", "0");
            let reply = tauri::async_runtime::block_on(dispatch(
                move || Ok(command),
                plan,
                None,
                (),
                Instant::now() + plan.limits.duration,
            ))
            .unwrap();
            if plan.doctor {
                assert_eq!(reply["schemaVersion"], 1);
            } else {
                assert_eq!(reply["ok"], true);
            }
        }
    }
    #[test]
    fn owned_settings_gate_serializes_real_read_modify_write_children() {
        let fixture = Fixture::new();
        let counter = fixture.directory.join("counter");
        let gate = Arc::new(Mutex::new(()));
        tauri::async_runtime::block_on(async {
            let mut requests = Vec::new();
            for index in 0..2 {
                let mut command = fixture.command("serialize");
                command.arg(&counter);
                let gate = gate.clone();
                requests.push(tokio::spawn(async move {
                    let guard = gate.lock_owned().await;
                    let mut plan = plan(true);
                    plan.label = if index == 0 { "First" } else { "Second" };
                    dispatch(
                        move || Ok(command),
                        plan,
                        None,
                        guard,
                        Instant::now() + Duration::from_secs(2),
                    )
                    .await
                    .unwrap();
                }));
            }
            for request in requests {
                request.await.unwrap();
            }
        });
        assert_eq!(std::fs::read_to_string(counter).unwrap(), "2");
        assert!(fixture.gone());
    }
    #[test]
    fn production_routes_and_mutation_budgets_remain_closed() {
        assert!(Plan::new(Service::Bridge, "snapshot").is_err());
        assert!(Plan::new(Service::Update, "launch").is_err());
        assert!(Plan::new(Service::Update, " launch").is_err());
        for operation in [
            "status", "discover", "verify", "request", "approve", "discard", "startup_check", "pick_package",
            "pick_folder",
        ] {
            assert!(Plan::new(Service::Update, operation).is_ok(), "{operation}");
        }
        for operation in ["pick_package", "pick_folder"] {
            assert!(!Plan::new(Service::Update, operation).unwrap().mutation, "{operation}");
        }
        assert!(!Plan::new(Service::Update, "status").unwrap().mutation);
        assert!(Plan::new(Service::Update, "approve").unwrap().mutation);
        assert!(Plan::new(Service::Library, "-m core.runtime_bridge").is_err());
        assert!(Plan::new(Service::Doctor, "https://public.example").is_err());
        assert_eq!(
            Plan::new(Service::Library, "commit")
                .unwrap()
                .limits
                .duration,
            Duration::from_secs(1800)
        );
        assert_eq!(
            Plan::new(Service::Library, "pick_folder")
                .unwrap()
                .limits
                .duration,
            Duration::from_secs(300)
        );
        assert!(Plan::new(Service::Library, "commit").unwrap().mutation);
        assert!(!Plan::new(Service::Library, "snapshot").unwrap().mutation);
    }
}
