
use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{sync_channel, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, Manager, State};

mod backend;
mod service_ipc;
use backend::{Backend, Service};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

const RUNTIME_EVENT_NAME: &str = "qronos://runtime-event";

// Device Link pairing. The runtime's answer holds a new phone's key, so it is
// never broadcast: it goes only to the window that asked, on its own event.
const LINK_PAIR_OPEN_ACTION: &str = "qronos.link_pair_open";
const LINK_PAIR_CANCEL_ACTION: &str = "qronos.link_pair_cancel";
const LINK_PAIRING_EVENT_TYPE: &str = "link_pairing";
const LINK_PAIRING_EVENT_NAME: &str = "qronos://link-pairing";
const PAIRING_URI_PREFIX: &str = "qronos://pair?";
// The runtime's pairing window is 120 s; a reply later than this is dropped.
const PAIRING_PENDING_SECONDS: u64 = 130;
const MAX_PENDING_PAIRINGS: usize = 4;
const MAX_PAIRING_NAME_CHARS: usize = 40;
const MAX_PAIRING_URI_CHARS: usize = 400;
// The capability that grants a window the app's commands; only its windows may pair.
const DEFAULT_CAPABILITY: &str = include_str!("../../capabilities/default.json");

const MAX_TEXT_MESSAGE_CHARS: usize = 16_000;
const SCREEN_ACTION_ID: &str = "qronos.look_at_screen";
const APPROVAL_ID_MIN_CHARS: usize = 22;
const APPROVAL_ID_MAX_CHARS: usize = 64;
const MAX_TEXT_REQUEST_ID_CHARS: usize = 128;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeEvent {
    pub event_type: String,
    pub status: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeStatus {
    pub running: bool,
    pub status: String,
    pub message: String,
}

struct RuntimeProcess {
    child: Child,
    input: RuntimeInput,
}

// ------------------------------------------------------------ command input

const MAX_QUEUED_COMMANDS: usize = 64;
const MAX_QUEUED_COMMAND_BYTES: usize = 4 * 1024 * 1024;
const MAX_COMMAND_BYTES: usize = 1024 * 1024;
const COMMAND_NOT_READ: &str =
    "The Qronos runtime is not reading commands; the command was not delivered and nothing ran.";
const COMMAND_TOO_LARGE: &str = "The command is too large; it was not delivered and nothing ran.";
const COMMAND_INPUT_CLOSED: &str =
    "The Qronos runtime is not accepting commands; the command was not delivered and nothing ran.";
const COMMAND_UNCONFIRMED: &str = "Sending a command to the Qronos runtime failed part-way; \
it may or may not have been received. It is not sent again.";

/// The runtime's standard input, written by one dedicated thread.
///
/// Nothing that holds the manager lock ever writes to the pipe: a command is
/// queued (at most `MAX_QUEUED_COMMANDS` and `MAX_QUEUED_COMMAND_BYTES`) and
/// the writer thread writes it. A runtime that stops reading fills the
/// queue, and further commands are refused at once, before any byte is
/// written. A write that fails part-way is reported once as unconfirmed and
/// ends the input; nothing is ever re-sent.
struct RuntimeInput {
    sender: Option<SyncSender<Vec<u8>>>,
    queued_bytes: Arc<AtomicUsize>,
    broken: Arc<AtomicBool>,
}

impl RuntimeInput {
    fn spawn<W, F>(mut writer: W, on_unconfirmed: F) -> Self
    where
        W: Write + Send + 'static,
        F: Fn() + Send + 'static,
    {
        let (sender, receiver) = sync_channel::<Vec<u8>>(MAX_QUEUED_COMMANDS);
        let queued_bytes = Arc::new(AtomicUsize::new(0));
        let broken = Arc::new(AtomicBool::new(false));
        let (bytes, failed) = (queued_bytes.clone(), broken.clone());

        std::thread::spawn(move || {
            for line in receiver {
                let size = line.len();
                let written = writer.write_all(&line).and_then(|_| writer.flush());
                bytes.fetch_sub(size, Ordering::SeqCst);
                if written.is_err() {
                    failed.store(true, Ordering::SeqCst);
                    on_unconfirmed();
                    // Dropping the receiver refuses every later command.
                    return;
                }
            }
        });

        Self {
            sender: Some(sender),
            queued_bytes,
            broken,
        }
    }

    /// Queue one command, or refuse it without writing anything. Never waits.
    fn send(&self, payload: &serde_json::Value) -> Result<(), String> {
        let mut line = payload.to_string().into_bytes();
        line.push(b'\n');
        if line.len() > MAX_COMMAND_BYTES {
            return Err(COMMAND_TOO_LARGE.to_string());
        }
        let Some(sender) = self.sender.as_ref() else {
            return Err(COMMAND_INPUT_CLOSED.to_string());
        };
        if self.broken.load(Ordering::SeqCst) {
            return Err(COMMAND_INPUT_CLOSED.to_string());
        }

        let size = line.len();
        if self.queued_bytes.fetch_add(size, Ordering::SeqCst) + size > MAX_QUEUED_COMMAND_BYTES {
            self.queued_bytes.fetch_sub(size, Ordering::SeqCst);
            return Err(COMMAND_NOT_READ.to_string());
        }
        match sender.try_send(line) {
            Ok(()) => Ok(()),
            Err(TrySendError::Full(_)) => {
                self.queued_bytes.fetch_sub(size, Ordering::SeqCst);
                Err(COMMAND_NOT_READ.to_string())
            }
            Err(TrySendError::Disconnected(_)) => {
                self.queued_bytes.fetch_sub(size, Ordering::SeqCst);
                Err(COMMAND_INPUT_CLOSED.to_string())
            }
        }
    }

    /// No more commands. Ones already queued still go out.
    fn close(&mut self) {
        self.sender = None;
    }
}

struct NativeAudioPlaybackState {
    child: Option<Child>,
    stop_generation: u64,
}

pub struct RuntimeManagerState {
    process: Mutex<Option<RuntimeProcess>>,

    // Native WASAPI playback runs in a short-lived Python child. Keep the
    // exact child handle plus a cancellation generation so Stop Response
    // cannot miss a race during process startup.
    native_audio: Mutex<NativeAudioPlaybackState>,

    // Settings mutations run through a short-lived Python CLI process. Serialize
    // them here so two UI requests cannot both read the same old file and then
    // race to overwrite each other.
    settings_command: Arc<tokio::sync::Mutex<()>>,

    // Which window asked for each open pairing request, by request nonce.
    pairings: Mutex<PendingPairings>,

    backend: Option<Backend>,
}

// ------------------------------------------------------------ pairing routes

/// One pairing request: the nonce the window sent and the window that sent it.
#[derive(Debug, Clone)]
struct PendingPairing {
    nonce: String,
    window: String,
    expires_at: std::time::Instant,
}

/// Bounded, single-use routes from a pairing reply to the window that asked.
#[derive(Debug, Default)]
struct PendingPairings {
    entries: Vec<PendingPairing>,
}

impl PendingPairings {
    fn prune(&mut self, now: std::time::Instant) {
        self.entries.retain(|entry| entry.expires_at > now);
    }

    /// Record a request from `window`. A window has at most one open request.
    fn begin(
        &mut self,
        nonce: &str,
        window: &str,
        allowed_windows: &[String],
        now: std::time::Instant,
    ) -> Result<(), String> {
        if !allowed_windows.iter().any(|allowed| allowed == window) {
            return Err("This window may not pair a phone.".to_string());
        }

        if !valid_pairing_nonce(nonce) {
            return Err("Invalid pairing request.".to_string());
        }

        self.prune(now);

        if self.entries.iter().any(|entry| entry.nonce == nonce) {
            return Err("Invalid pairing request.".to_string());
        }

        self.entries.retain(|entry| entry.window != window);

        if self.entries.len() >= MAX_PENDING_PAIRINGS {
            return Err("Too many pairing requests are open.".to_string());
        }

        self.entries.push(PendingPairing {
            nonce: nonce.to_string(),
            window: window.to_string(),
            expires_at: now + std::time::Duration::from_secs(PAIRING_PENDING_SECONDS),
        });

        Ok(())
    }

    /// The window that asked for `nonce`, once; None when unknown or late.
    fn take(&mut self, nonce: &str, now: std::time::Instant) -> Option<String> {
        self.prune(now);
        let index = self.entries.iter().position(|entry| entry.nonce == nonce)?;
        Some(self.entries.remove(index).window)
    }

    fn cancel_window(&mut self, window: &str) {
        self.entries.retain(|entry| entry.window != window);
    }

    fn clear(&mut self) {
        self.entries.clear();
    }
}

fn valid_pairing_nonce(nonce: &str) -> bool {
    (16..=64).contains(&nonce.len())
        && nonce
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

/// Control, line-break and bidi override/isolate characters; Persian joiners are fine.
fn refused_name_character(character: char) -> bool {
    let code = character as u32;
    code < 0x20
        || (0x7F..=0x9F).contains(&code)
        || code == 0x2028
        || code == 0x2029
        || (0x202A..=0x202E).contains(&code)
        || (0x2066..=0x2069).contains(&code)
}

fn clean_pairing_name(name: &str) -> Result<String, String> {
    let name = name.trim();

    if name.chars().count() > MAX_PAIRING_NAME_CHARS || name.chars().any(refused_name_character) {
        return Err("Invalid device name.".to_string());
    }

    Ok(name.to_string())
}

/// Windows granted the default capability: the only ones that may pair.
fn pairing_windows() -> Vec<String> {
    serde_json::from_str::<serde_json::Value>(DEFAULT_CAPABILITY)
        .ok()
        .and_then(|capability| capability.get("windows").cloned())
        .and_then(|windows| serde_json::from_value::<Vec<String>>(windows).ok())
        .unwrap_or_default()
        .into_iter()
        .filter(|label| !label.contains('*'))
        .collect()
}

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
struct PairingDelivery {
    request_nonce: String,
    uri: String,
}

/// Where one runtime event goes.
#[derive(Debug, PartialEq)]
enum EventRoute {
    Broadcast,
    /// A pairing reply for the window that asked (looked up by nonce).
    Pairing(PairingDelivery),
    /// Never shown anywhere: a malformed pairing reply, or a key out of place.
    Drop,
}

fn route_runtime_event(event: &RuntimeEvent) -> EventRoute {
    if event.event_type == LINK_PAIRING_EVENT_TYPE {
        let parsed = serde_json::from_str::<serde_json::Value>(&event.message).ok();
        let field = |name: &str| {
            parsed
                .as_ref()
                .and_then(|value| value.get(name))
                .and_then(|value| value.as_str())
                .map(str::to_string)
        };

        return match (field("requestNonce"), field("uri")) {
            (Some(nonce), Some(uri))
                if valid_pairing_nonce(&nonce)
                    && uri.starts_with(PAIRING_URI_PREFIX)
                    && uri.chars().count() <= MAX_PAIRING_URI_CHARS =>
            {
                EventRoute::Pairing(PairingDelivery {
                    request_nonce: nonce,
                    uri,
                })
            }
            _ => EventRoute::Drop,
        };
    }

    // A pairing key in any other event or log line is never broadcast.
    if event.message.contains(PAIRING_URI_PREFIX) {
        return EventRoute::Drop;
    }

    EventRoute::Broadcast
}

/// A failed pairing request leaves nothing pending.
fn ends_pairing_requests(event: &RuntimeEvent) -> bool {
    event.event_type == "link_error"
        && serde_json::from_str::<serde_json::Value>(&event.message)
            .ok()
            .and_then(|value| value.get("action").and_then(|a| a.as_str()).map(str::to_string))
            .as_deref()
            == Some(LINK_PAIR_OPEN_ACTION)
}

fn clear_pairings(app: &AppHandle) {
    if let Some(state) = app.try_state::<RuntimeManagerState>() {
        if let Ok(mut pairings) = state.pairings.lock() {
            pairings.clear();
        }
    }
}

/// The bridge that took a pairing request is gone: it stopped, or its
/// supervisor is replacing it. A new bridge cannot answer the old request.
fn ends_runtime_session(event: &RuntimeEvent) -> bool {
    event.event_type == "runtime_stopping"
}

fn deliver_runtime_event(app: &AppHandle, event: RuntimeEvent) {
    if ends_pairing_requests(&event) || ends_runtime_session(&event) {
        clear_pairings(app);
    }

    match route_runtime_event(&event) {
        EventRoute::Broadcast => emit_runtime_event(app, event),
        EventRoute::Drop => {}
        EventRoute::Pairing(delivery) => {
            let window = app.try_state::<RuntimeManagerState>().and_then(|state| {
                state
                    .pairings
                    .lock()
                    .ok()
                    .and_then(|mut pairings| pairings.take(&delivery.request_nonce, std::time::Instant::now()))
            });

            // Unknown, late or replayed replies are dropped without a trace.
            if let Some(label) = window {
                let _ = app.emit_to(
                    tauri::EventTarget::WebviewWindow { label },
                    LINK_PAIRING_EVENT_NAME,
                    delivery,
                );
            }
        }
    }
}

impl RuntimeManagerState {
    fn new(backend: Option<Backend>) -> Self {
        Self {
            process: Mutex::new(None),
            native_audio: Mutex::new(NativeAudioPlaybackState {
                child: None,
                stop_generation: 0,
            }),
            settings_command: Arc::new(tokio::sync::Mutex::new(())),
            pairings: Mutex::new(PendingPairings::default()),
            backend,
        }
    }

    fn require_backend(&self) -> Result<&Backend, String> {
        self.backend.as_ref().ok_or_else(||
            "The bundled Qronos backend is unavailable. Install a complete Qronos build to enable runtime services.".to_string())
    }
}

fn emit_runtime_event(app: &AppHandle, event: RuntimeEvent) {
    let _ = app.emit(RUNTIME_EVENT_NAME, event);
}

#[tauri::command]
pub async fn runtime_setup_snapshot(
    state: State<'_, RuntimeManagerState>,
) -> Result<serde_json::Value, String> {
    service_ipc::request(state.require_backend()?, Service::Doctor, "", None, None).await
}

fn spawn_runtime_reader(app: AppHandle, stdout: std::process::ChildStdout) {
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);

        for line in reader.lines() {
            let Ok(line) = line else {
                clear_pairings(&app);
                emit_runtime_event(
                    &app,
                    RuntimeEvent {
                        event_type: "runtime_error".to_string(),
                        status: "error".to_string(),
                        message: "Runtime output stream closed unexpectedly.".to_string(),
                    },
                );
                break;
            };

            let trimmed = line.trim();

            if trimmed.is_empty() {
                continue;
            }

            match serde_json::from_str::<RuntimeEvent>(trimmed) {
                Ok(event) => deliver_runtime_event(&app, event),
                Err(_) => deliver_runtime_event(
                    &app,
                    RuntimeEvent {
                        event_type: "runtime_log".to_string(),
                        status: "running".to_string(),
                        message: trimmed.to_string(),
                    },
                ),
            }
        }

        // The runtime is gone: no reply can belong to a pending request now.
        clear_pairings(&app);
    });
}

/// Send one command to the runtime.
///
/// There used to be two of these: this one, which could only send a bare
/// command, and a copy inside `send_runtime_action` that inlined the write so
/// it could carry an argument. A third caller is where two copies quietly
/// become two behaviours, so they are one function that takes a payload.
fn write_runtime_payload(
    process: &mut RuntimeProcess,
    payload: serde_json::Value,
) -> Result<(), String> {
    process.input.send(&payload)
}

fn write_runtime_command(process: &mut RuntimeProcess, command: &str) -> Result<(), String> {
    write_runtime_payload(process, serde_json::json!({ "command": command }))
}

/// Send a command to a runtime that may not be there.
///
/// Every queue command shares this shape: take the lock, complain clearly if
/// nothing is running, write, flush. The answer arrives as an event rather
/// than a return value, because a queue change concerns anybody looking at
/// the queue and not only whoever pressed the button.
fn send_to_runtime(
    state: &State<'_, RuntimeManagerState>,
    payload: serde_json::Value,
) -> Result<(), String> {
    let mut guard = state
        .process
        .lock()
        .map_err(|_| "Runtime manager lock failed.".to_string())?;

    let process = guard
        .as_mut()
        .ok_or_else(|| "Qronos runtime is not running.".to_string())?;

    write_runtime_payload(process, payload)
}

fn require_task_id(task_id: &str) -> Result<&str, String> {
    let cleaned = task_id.trim();

    if cleaned.is_empty() {
        return Err("A queue command needs a task id.".to_string());
    }

    Ok(cleaned)
}

pub fn initialize(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let resources = app.path().resource_dir().ok();
    let local_data = app.path().local_data_dir().ok();
    let backend = backend::discover(resources.as_deref(), local_data.as_deref())?;
    if let Some(backend) = backend.as_ref() {
        app.asset_protocol_scope()
            .allow_directory(backend.playback_root(), true)?;
    }
    app.manage(RuntimeManagerState::new(backend));
    Ok(())
}

#[tauri::command]
pub fn get_runtime_status(state: State<'_, RuntimeManagerState>) -> Result<RuntimeStatus, String> {
    inspect_runtime_status(&state)
}

fn inspect_runtime_status(state: &RuntimeManagerState) -> Result<RuntimeStatus, String> {
    let mut guard = state
        .process
        .try_lock()
        .map_err(|error| match error {
            std::sync::TryLockError::WouldBlock =>
                "Runtime status is temporarily unavailable because the manager is busy.".to_string(),
            std::sync::TryLockError::Poisoned(_) =>
                "Runtime status is unavailable because the manager lock failed.".to_string(),
        })?;

    if let Some(process) = guard.as_mut() {
        match process.child.try_wait() {
            Ok(Some(status)) => {
                *guard = None;

                return Ok(RuntimeStatus {
                    running: false,
                    status: "stopped".to_string(),
                    message: format!("Runtime exited with status {status}."),
                });
            }
            Ok(None) => {
                return Ok(RuntimeStatus {
                    running: true,
                    status: "running".to_string(),
                    message: "Qronos runtime is running.".to_string(),
                });
            }
            Err(error) => {
                return Err(format!("Could not inspect Qronos runtime process: {error}"));
            }
        }
    }

    Ok(RuntimeStatus {
        running: false,
        status: "stopped".to_string(),
        message: "Qronos runtime is not running.".to_string(),
    })
}

#[cfg(test)]
mod runtime_status_tests {
    use super::*;
    use std::sync::mpsc;
    use std::time::Duration;

    #[test]
    fn held_process_slot_returns_busy_without_claiming_stopped() {
        let state = Arc::new(RuntimeManagerState::new(None));
        let guard = state.process.lock().unwrap();
        let peer = state.clone();
        let (send, receive) = mpsc::channel();
        let worker = std::thread::spawn(move || {
            send.send(inspect_runtime_status(&peer)).unwrap();
        });
        // Release the held slot even if the bounded observation fails, so a
        // regression cannot strand a worker or hang the test process.
        let observed = receive.recv_timeout(Duration::from_millis(250));
        drop(guard);
        worker.join().unwrap();
        let error = observed.expect("Status must not wait behind a runtime writer")
            .expect_err("Lock contention must not produce running=false");
        assert!(error.contains("busy") && error.contains("unavailable"));
        let actual = inspect_runtime_status(&state).unwrap();
        assert!(!actual.running && actual.status == "stopped");
    }

    #[test]
    fn poisoned_process_slot_is_unavailable_without_resetting_it() {
        let state = Arc::new(RuntimeManagerState::new(None));
        let peer = state.clone();
        assert!(std::thread::spawn(move || {
            let _held = peer.process.lock().unwrap();
            panic!("Synthetic poisoned-slot fixture");
        }).join().is_err());
        let error = inspect_runtime_status(&state).expect_err("Poison is not evidence of a stopped runtime");
        assert!(error.contains("unavailable") && error.contains("lock failed"));
        assert!(state.process.is_poisoned());
    }
}

#[tauri::command]
pub fn start_runtime(
    app: AppHandle,
    state: State<'_, RuntimeManagerState>,
) -> Result<RuntimeStatus, String> {
    let mut guard = state
        .process
        .lock()
        .map_err(|_| "Runtime manager lock failed.".to_string())?;

    if let Some(process) = guard.as_mut() {
        if process.child.try_wait().ok().flatten().is_none() {
            return Ok(RuntimeStatus {
                running: true,
                status: "running".to_string(),
                message: "Qronos runtime is already running.".to_string(),
            });
        }

        *guard = None;
    }

    // A new runtime: no earlier pairing request can be answered by it.
    if let Ok(mut pairings) = state.pairings.lock() {
        pairings.clear();
    }

    // The supervisor starts the bridge itself, relays this process's pipes
    // to it, restarts it within a bounded budget and reports
    // `supervisor_state` events; see core/supervisor_relay.py.
    let mut command = state.require_backend()?.command(Service::Supervisor)?;
    command
        .env("QRONOS_MEMORY_RUNTIME", "1")
        .env("QRONOS_DESKTOP_WAKE_BOOTSTRAP", "1")
        // The one product version (tauri.conf.json), for runtime status hooks.
        .env("QRONOS_APP_VERSION", app.package_info().version.to_string())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);

    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start Qronos runtime: {error}"))?;

    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| "Qronos runtime stdin was unavailable.".to_string())?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Qronos runtime stdout was unavailable.".to_string())?;

    if let Some(stderr) = child.stderr.take() {
        let app_for_stderr = app.clone();

        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);

            for line in reader.lines().map_while(Result::ok) {
                let trimmed = line.trim();

                if trimmed.is_empty() {
                    continue;
                }

                let looks_like_error = trimmed.starts_with("Traceback")
                    || trimmed.starts_with("Exception")
                    || trimmed.starts_with("Error:")
                    || trimmed.contains("ModuleNotFoundError")
                    || trimmed.contains("UnicodeEncodeError");

                emit_runtime_event(
                    &app_for_stderr,
                    RuntimeEvent {
                        event_type: if looks_like_error {
                            "runtime_error".to_string()
                        } else {
                            "runtime_log".to_string()
                        },
                        status: if looks_like_error {
                            "error".to_string()
                        } else {
                            "running".to_string()
                        },
                        message: line,
                    },
                );
            }
        });
    }

    spawn_runtime_reader(app.clone(), stdout);

    /*
     * Wake listening is a runtime invariant, not a frontend timing concern.
     *
     * Queue the wake-listener action directly into the child's stdin as soon
     * as the runtime process exists. The OS pipe safely buffers this command
     * until runtime_bridge reaches its command loop.
     *
     * The frontend may still send the same action after runtime_ready as a
     * recovery path; start_wake_listener() is idempotent.
     */
    let app_for_input = app.clone();
    let input = RuntimeInput::spawn(stdin, move || {
        emit_runtime_event(
            &app_for_input,
            RuntimeEvent {
                event_type: "runtime_error".to_string(),
                status: "error".to_string(),
                message: COMMAND_UNCONFIRMED.to_string(),
            },
        );
    });
    let mut runtime_process = RuntimeProcess { child, input };

    if let Err(error) = write_runtime_payload(
        &mut runtime_process,
        serde_json::json!({
            "command": "action",
            "actionId": "qronos.wake_listener_start"
        }),
    ) {
        let _ = runtime_process.child.kill();

        return Err(format!(
            "Qronos runtime started but wake-listener bootstrap failed: {error}"
        ));
    }

    *guard = Some(runtime_process);

    Ok(RuntimeStatus {
        running: true,
        status: "starting".to_string(),
        message: "Qronos runtime supervisor started with native wake bootstrap.".to_string(),
    })
}

#[tauri::command]
pub fn ping_runtime(state: State<'_, RuntimeManagerState>) -> Result<(), String> {
    let mut guard = state
        .process
        .lock()
        .map_err(|_| "Runtime manager lock failed.".to_string())?;

    let process = guard
        .as_mut()
        .ok_or_else(|| "Qronos runtime is not running.".to_string())?;

    write_runtime_command(process, "ping")
}

#[tauri::command(rename_all = "camelCase")]
pub fn send_runtime_action(
    action_id: String,
    playback_id: Option<u64>,
    data: Option<serde_json::Value>,
    webview_window: tauri::WebviewWindow,
    state: State<'_, RuntimeManagerState>,
) -> Result<(), String> {
    let cleaned = action_id.trim();

    if cleaned.is_empty() {
        return Err("Runtime action id must not be empty.".to_string());
    }

    reject_typed_only_action(cleaned)?;

    if cleaned == LINK_PAIR_CANCEL_ACTION {
        // A cancelled request's late reply, if any, goes nowhere.
        if let Ok(mut pairings) = state.pairings.lock() {
            pairings.cancel_window(webview_window.label());
        }
    }

    let mut payload = serde_json::json!({
        "command": "action",
        "actionId": cleaned
    });

    if let Some(playback_id) = playback_id {
        if playback_id == 0 {
            return Err("Playback id must be positive.".to_string());
        }

        payload["playbackId"] = serde_json::json!(playback_id);
    }

    if let Some(data) = data {
        if !data.is_object() {
            return Err("Runtime action data must be a JSON object.".to_string());
        }

        // Keep caller-supplied fields nested. They can never overwrite the
        // command or actionId chosen by the trusted Tauri command.
        payload["data"] = data;
    }

    send_to_runtime(&state, payload)
}

/// Screen/Paint decisions carry a single-use approval identity and are
/// validated only by the typed `look_at_screen` command; the generic action
/// path must not be able to send them with arbitrary data.
fn reject_typed_only_action(action_id: &str) -> Result<(), String> {
    if action_id.trim() == SCREEN_ACTION_ID {
        return Err("Screen decisions must use the look_at_screen command.".to_string());
    }

    if action_id.trim() == LINK_PAIR_OPEN_ACTION {
        return Err("Pairing must use the link_pair_open command.".to_string());
    }
    Ok(())
}

/// A backend approval identity: an opaque URL-safe token, never a path or text.
fn is_valid_approval_id(value: &str) -> bool {
    let length = value.chars().count();
    (APPROVAL_ID_MIN_CHARS..=APPROVAL_ID_MAX_CHARS).contains(&length)
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

/// Build the runtime payload for one screen/Paint decision.
///
/// Exactly one of two shapes is accepted:
/// - a prompt answer: the prompt's approval identity plus yes/no. No question
///   text is sent; the backend ticket decides what the answer applies to.
/// - an explicit direct read: `direct = true` plus a non-empty question.
///
/// Anything else is refused here, before it reaches the runtime.
pub(crate) fn screen_decision_payload(
    approval_id: Option<&str>,
    approved: bool,
    question: Option<&str>,
    window_only: bool,
    direct: bool,
) -> Result<serde_json::Value, String> {
    let approval_id = approval_id.map(str::trim).unwrap_or("");

    let data = if !approval_id.is_empty() {
        if direct {
            return Err(
                "A screen decision is either a prompt answer or a direct read, not both."
                    .to_string(),
            );
        }
        if !is_valid_approval_id(approval_id) {
            return Err("Screen approval id is malformed.".to_string());
        }
        serde_json::json!({
            "approvalId": approval_id,
            "approved": approved,
            "windowOnly": window_only
        })
    } else if direct {
        let question = question.map(str::trim).unwrap_or("");
        if question.is_empty() {
            return Err("A direct screen read must say what to look for.".to_string());
        }
        if question.chars().count() > MAX_TEXT_MESSAGE_CHARS {
            return Err("Screen question is too long.".to_string());
        }
        serde_json::json!({
            "direct": true,
            "approved": approved,
            "question": question,
            "windowOnly": window_only
        })
    } else {
        return Err(
            "A screen decision must name its approval prompt or be an explicit direct read."
                .to_string(),
        );
    };

    Ok(serde_json::json!({
        "command": "action",
        "actionId": SCREEN_ACTION_ID,
        "data": data
    }))
}

#[tauri::command(rename_all = "camelCase")]
pub fn look_at_screen(
    approval_id: Option<String>,
    approved: bool,
    question: Option<String>,
    window_only: Option<bool>,
    direct: Option<bool>,
    state: State<'_, RuntimeManagerState>,
) -> Result<(), String> {
    let payload = screen_decision_payload(
        approval_id.as_deref(),
        approved,
        question.as_deref(),
        window_only.unwrap_or(false),
        direct.unwrap_or(false),
    )?;

    send_to_runtime(&state, payload)
}

/// Ask the runtime to open a pairing window for one new phone.
///
/// The request nonce is bound here to the window that actually invoked the
/// command (never to anything the payload says), and only a window granted
/// the default capability may ask. The reply carrying the phone's key is
/// delivered to that window alone, once, on `qronos://link-pairing`.
#[tauri::command(rename_all = "camelCase")]
pub fn link_pair_open(
    request_nonce: String,
    name: String,
    webview_window: tauri::WebviewWindow,
    state: State<'_, RuntimeManagerState>,
) -> Result<(), String> {
    let name = clean_pairing_name(&name)?;
    let window = webview_window.label().to_string();

    state
        .pairings
        .lock()
        .map_err(|_| "Runtime manager lock failed.".to_string())?
        .begin(&request_nonce, &window, &pairing_windows(), std::time::Instant::now())?;

    let sent = send_to_runtime(
        &state,
        serde_json::json!({
            "command": "action",
            "actionId": LINK_PAIR_OPEN_ACTION,
            "data": { "requestNonce": request_nonce, "name": name }
        }),
    );

    if sent.is_err() {
        if let Ok(mut pairings) = state.pairings.lock() {
            pairings.cancel_window(&window);
        }
    }

    sent
}

#[tauri::command(rename_all = "camelCase")]
pub async fn library_command(
    operation: String,
    payload: Option<serde_json::Value>,
    state: State<'_, RuntimeManagerState>,
) -> Result<serde_json::Value, String> {
    service_ipc::request(
        state.require_backend()?,
        Service::Library,
        &operation,
        payload,
        None,
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub async fn memory_command(
    operation: String,
    payload: Option<serde_json::Value>,
    state: State<'_, RuntimeManagerState>,
) -> Result<serde_json::Value, String> {
    service_ipc::request(
        state.require_backend()?,
        Service::Memory,
        &operation,
        payload,
        None,
    )
    .await
}

fn native_audio_output_play_blocking(
    path: String,
    device_id: String,
    state: State<'_, RuntimeManagerState>,
) -> Result<serde_json::Value, String> {
    let cleaned_path = path.trim();
    let cleaned_device = device_id.trim();

    if cleaned_path.is_empty() {
        return Err("Native audio playback path must not be empty.".to_string());
    }

    if cleaned_device.is_empty() {
        return Err(
            "Native audio playback needs an explicit output device.".to_string(),
        );
    }

    let start_generation = {
        let mut audio = state
            .native_audio
            .lock()
            .map_err(|_| "Native audio state lock failed.".to_string())?;

        if let Some(child) = audio.child.as_mut() {
            match child.try_wait() {
                Ok(Some(_)) => {
                    audio.child = None;
                }
                Ok(None) => {
                    return Err(
                        "Qronos native audio output is already playing.".to_string(),
                    );
                }
                Err(error) => {
                    return Err(format!(
                        "Could not inspect Qronos native audio output: {error}"
                    ));
                }
            }
        }

        audio.stop_generation
    };

    let mut command = state.require_backend()?.command(Service::Audio)?;
    command
        .arg("play")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);

    let mut child = command
        .spawn()
        .map_err(|error| format!(
            "Could not start Qronos native audio output backend: {error}"
        ))?;

    if let Some(mut stdin) = child.stdin.take() {
        let body = serde_json::json!({
            "path": cleaned_path,
            "deviceId": cleaned_device,
        });

        write!(stdin, "{}", body).map_err(|error| format!(
            "Could not send native audio output payload: {error}"
        ))?;
    }

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Native audio output stdout was unavailable.".to_string())?;

    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "Native audio output stderr was unavailable.".to_string())?;

    let cancelled_before_registration = {
        let mut audio = state
            .native_audio
            .lock()
            .map_err(|_| "Native audio state lock failed.".to_string())?;

        if audio.stop_generation != start_generation {
            let _ = child.kill();
            true
        } else {
            audio.child = Some(child);
            false
        }
    };

    let stdout_reader = std::thread::spawn(move || {
        BufReader::new(stdout)
            .lines()
            .map_while(Result::ok)
            .collect::<Vec<_>>()
            .join("\n")
    });

    let stderr_reader = std::thread::spawn(move || {
        BufReader::new(stderr)
            .lines()
            .map_while(Result::ok)
            .collect::<Vec<_>>()
            .join("\n")
    });

    if cancelled_before_registration {
        let _ = stdout_reader.join();
        let _ = stderr_reader.join();

        return Ok(serde_json::json!({
            "ok": true,
            "stopped": true,
        }));
    }

    let status = loop {
        let maybe_status = {
            let mut audio = state
                .native_audio
                .lock()
                .map_err(|_| "Native audio state lock failed.".to_string())?;

            let child = audio
                .child
                .as_mut()
                .ok_or_else(|| "Native audio process state was lost.".to_string())?;

            child
                .try_wait()
                .map_err(|error| format!(
                    "Could not inspect native audio output process: {error}"
                ))?
        };

        if let Some(status) = maybe_status {
            break status;
        }

        std::thread::sleep(
            std::time::Duration::from_millis(20),
        );
    };

    let stopped = {
        let mut audio = state
            .native_audio
            .lock()
            .map_err(|_| "Native audio state lock failed.".to_string())?;

        let stopped =
            audio.stop_generation != start_generation;

        audio.child = None;
        stopped
    };

    let stdout = stdout_reader
        .join()
        .unwrap_or_default()
        .trim()
        .to_string();

    let stderr = stderr_reader
        .join()
        .unwrap_or_default()
        .trim()
        .to_string();

    if stopped {
        return Ok(serde_json::json!({
            "ok": true,
            "stopped": true,
        }));
    }

    if stdout.is_empty() {
        return Err(if stderr.is_empty() {
            "Native audio output backend returned no data.".to_string()
        } else {
            stderr
        });
    }

    let value: serde_json::Value =
        serde_json::from_str(&stdout).map_err(|error| {
            format!(
                "Invalid native audio output response: {error}; output={stdout}"
            )
        })?;

    if !status.success()
        || value.get("ok").and_then(|value| value.as_bool()) == Some(false)
    {
        let message = value
            .get("error")
            .and_then(|value| value.as_str())
            .unwrap_or(&stderr);

        return Err(if message.is_empty() {
            "Native audio output playback failed.".to_string()
        } else {
            message.to_string()
        });
    }

    Ok(value)
}

#[tauri::command(rename_all = "camelCase")]
pub async fn native_audio_output_play(
    path: String,
    device_id: String,
    app: AppHandle,
) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<RuntimeManagerState>();

        native_audio_output_play_blocking(
            path,
            device_id,
            state,
        )
    })
    .await
    .map_err(|error| format!(
        "Native audio output task failed: {error}"
    ))?
}

#[tauri::command]
pub fn native_audio_output_stop(
    state: State<'_, RuntimeManagerState>,
) -> Result<serde_json::Value, String> {
    let killed = {
        let mut audio = state
            .native_audio
            .lock()
            .map_err(|_| "Native audio state lock failed.".to_string())?;

        audio.stop_generation =
            audio.stop_generation.wrapping_add(1);

        match audio.child.as_mut() {
            None => false,
            Some(child) => {
                match child.try_wait() {
                    Ok(Some(_)) => false,
                    Ok(None) => {
                        child
                            .kill()
                            .map_err(|error| format!(
                                "Could not stop Qronos native audio output: {error}"
                            ))?;

                        true
                    }
                    Err(error) => {
                        return Err(format!(
                            "Could not inspect Qronos native audio output: {error}"
                        ));
                    }
                }
            }
        }
    };

    Ok(serde_json::json!({
        "ok": true,
        "stopped": killed,
    }))
}

#[tauri::command(rename_all = "camelCase")]
pub async fn settings_command(
    operation: String,
    payload: Option<serde_json::Value>,
    state: State<'_, RuntimeManagerState>,
) -> Result<serde_json::Value, String> {
    service_ipc::request(
        state.require_backend()?,
        Service::Settings,
        &operation,
        payload,
        Some(state.settings_command.clone()),
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub async fn permissions_command(
    operation: String,
    payload: Option<serde_json::Value>,
    state: State<'_, RuntimeManagerState>,
) -> Result<serde_json::Value, String> {
    service_ipc::request(
        state.require_backend()?,
        Service::Permissions,
        &operation,
        payload,
        None,
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub fn send_text_message(
    text: String,
    request_id: String,
    state: State<'_, RuntimeManagerState>,
) -> Result<(), String> {
    let cleaned_text = text.trim();
    let cleaned_request_id = request_id.trim();

    if cleaned_text.is_empty() {
        return Err("Text message must not be empty.".to_string());
    }

    if cleaned_text.chars().count() > MAX_TEXT_MESSAGE_CHARS {
        return Err(format!(
            "Text message exceeds the {MAX_TEXT_MESSAGE_CHARS} character limit."
        ));
    }

    if cleaned_request_id.is_empty() {
        return Err("Text request id must not be empty.".to_string());
    }

    if cleaned_request_id.chars().count() > MAX_TEXT_REQUEST_ID_CHARS {
        return Err(format!(
            "Text request id exceeds the {MAX_TEXT_REQUEST_ID_CHARS} character limit."
        ));
    }

    send_to_runtime(
        &state,
        serde_json::json!({
            "command": "text_message",
            "text": cleaned_text,
            "requestId": cleaned_request_id
        }),
    )
}

// ---------------------------------------------------------------------------
// The smart queue.
//
// All fire-and-forget. Each resolves when the write succeeds, not when the
// runtime has done anything, and the result comes back as a `queue_changed`
// event — the same shape `send_runtime_action` has always had.
// ---------------------------------------------------------------------------

#[tauri::command]
pub fn queue_list(state: State<'_, RuntimeManagerState>) -> Result<(), String> {
    send_to_runtime(&state, serde_json::json!({ "command": "queue_list" }))
}

/// Queue a reasoning request. `text` + `request_id` tie the eventual answer
/// to the conversation request it belongs to; a bare `summary` is still
/// accepted and becomes the request text. The runtime validates everything
/// again and refuses anything that is not plain Fast/Heavy reasoning.
#[tauri::command(rename_all = "camelCase")]
pub fn queue_submit(
    text: Option<String>,
    request_id: Option<String>,
    summary: Option<String>,
    weight: Option<String>,
    state: State<'_, RuntimeManagerState>,
) -> Result<(), String> {
    let weight = weight.unwrap_or_else(|| "light".to_string());

    let payload = match text.as_deref().map(str::trim) {
        Some(cleaned) if !cleaned.is_empty() => {
            let mut payload = serde_json::json!({
                "command": "queue_submit",
                "text": cleaned,
                "weight": weight
            });

            if let Some(request_id) = request_id.as_deref().map(str::trim) {
                if request_id.is_empty() {
                    return Err("A queued request id must not be empty.".to_string());
                }

                payload["requestId"] = serde_json::Value::from(request_id);
            }

            payload
        }
        _ => {
            let cleaned = summary.as_deref().map(str::trim).unwrap_or("");

            if cleaned.is_empty() {
                return Err("A queued task must say what it is.".to_string());
            }

            serde_json::json!({
                "command": "queue_submit",
                "summary": cleaned,
                "weight": weight
            })
        }
    };

    send_to_runtime(&state, payload)
}

#[tauri::command(rename_all = "camelCase")]
pub fn queue_cancel(task_id: String, state: State<'_, RuntimeManagerState>) -> Result<(), String> {
    let cleaned = require_task_id(&task_id)?;

    send_to_runtime(
        &state,
        serde_json::json!({
            "command": "queue_cancel",
            "taskId": cleaned
        }),
    )
}

#[tauri::command(rename_all = "camelCase")]
pub fn queue_override(
    task_id: String,
    state: State<'_, RuntimeManagerState>,
) -> Result<(), String> {
    let cleaned = require_task_id(&task_id)?;

    send_to_runtime(
        &state,
        serde_json::json!({
            "command": "queue_override",
            "taskId": cleaned
        }),
    )
}

/// Hold one waiting request until it is resumed.
#[tauri::command(rename_all = "camelCase")]
pub fn queue_pause_task(
    task_id: String,
    state: State<'_, RuntimeManagerState>,
) -> Result<(), String> {
    let cleaned = require_task_id(&task_id)?;

    send_to_runtime(
        &state,
        serde_json::json!({
            "command": "queue_pause_task",
            "taskId": cleaned
        }),
    )
}

/// Explicitly run a paused or interrupted request again. The only way an
/// interrupted request ever runs again.
#[tauri::command(rename_all = "camelCase")]
pub fn queue_resume_task(
    task_id: String,
    state: State<'_, RuntimeManagerState>,
) -> Result<(), String> {
    let cleaned = require_task_id(&task_id)?;

    send_to_runtime(
        &state,
        serde_json::json!({
            "command": "queue_resume_task",
            "taskId": cleaned
        }),
    )
}

#[tauri::command(rename_all = "camelCase")]
pub fn queue_set_paused(paused: bool, state: State<'_, RuntimeManagerState>) -> Result<(), String> {
    send_to_runtime(
        &state,
        serde_json::json!({
            "command": "queue_set_paused",
            "paused": paused
        }),
    )
}

#[tauri::command(rename_all = "camelCase")]
pub fn runtime_set_paused(paused: bool, state: State<'_, RuntimeManagerState>) -> Result<(), String> {
    send_to_runtime(
        &state,
        serde_json::json!({
            "command": "runtime_set_paused",
            "paused": paused
        }),
    )
}

/// 100 x 50 ms = 5 s for an orderly stop. The supervisor's own shutdown
/// grace (3 s) plus its terminate step fits inside it, so the desktop's
/// kill is only the backstop.
const STOP_WAIT_STEPS: u32 = 100;

#[tauri::command]
pub fn stop_runtime(state: State<'_, RuntimeManagerState>) -> Result<RuntimeStatus, String> {
    let mut guard = state
        .process
        .lock()
        .map_err(|_| "Runtime manager lock failed.".to_string())?;

    if let Ok(mut pairings) = state.pairings.lock() {
        pairings.clear();
    }

    let Some(mut process) = guard.take() else {
        return Ok(RuntimeStatus {
            running: false,
            status: "stopped".to_string(),
            message: "Qronos runtime is already stopped.".to_string(),
        });
    };
    // Status and other commands are answered while this waits.
    drop(guard);

    // Queued, never written here: a runtime that is not reading cannot hold
    // Stop. If even the queue is full, the bounded wait and kill still run.
    let _ = write_runtime_command(&mut process, "shutdown");
    process.input.close();

    for _ in 0..STOP_WAIT_STEPS {
        match process.child.try_wait() {
            Ok(Some(_)) => {
                return Ok(RuntimeStatus {
                    running: false,
                    status: "stopped".to_string(),
                    message: "Qronos runtime stopped safely.".to_string(),
                });
            }
            Ok(None) => {
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(_) => break,
        }
    }

    process
        .child
        .kill()
        .map_err(|error| format!("Could not stop Qronos runtime: {error}"))?;

    Ok(RuntimeStatus {
        running: false,
        status: "stopped".to_string(),
        message: "Qronos runtime was force-stopped after shutdown timeout.".to_string(),
    })
}

pub fn shutdown(app: &AppHandle) {
    let Some(state) = app.try_state::<RuntimeManagerState>() else {
        return;
    };

    let Ok(mut guard) = state.process.lock() else {
        return;
    };

    if let Ok(mut pairings) = state.pairings.lock() {
        pairings.clear();
    }

    let Some(mut process) = guard.take() else {
        return;
    };
    drop(guard);

    // Give the Python runtime the same graceful-shutdown opportunity used by
    // stop_runtime(). This matters because Python owns child resources such as
    // the persistent whisper-server process. Killing Python immediately after
    // writing "shutdown" can orphan those children and leave GPU memory and
    // localhost ports allocated after the desktop has exited.
    let _ = write_runtime_command(&mut process, "shutdown");
    process.input.close();

    for _ in 0..STOP_WAIT_STEPS {
        match process.child.try_wait() {
            Ok(Some(_)) => {
                return;
            }
            Ok(None) => {
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(_) => {
                break;
            }
        }
    }

    // Shutdown is bounded: if the supervisor has not exited within
    // STOP_WAIT_STEPS x 50 ms, force-stop only the process owned by this
    // manager. Its kill-on-close Job Object then ends the bridge too.
    let _ = process.child.kill();
}

#[cfg(test)]
mod runtime_input_tests {
    use super::*;
    use std::time::{Duration, Instant};

    fn command(size: usize) -> serde_json::Value {
        serde_json::json!({ "command": "echo", "text": "y".repeat(size) })
    }

    #[test]
    fn a_runtime_that_stops_reading_gets_refusals_and_never_holds_the_caller() {
        // A real anonymous pipe whose reading end is kept open and never read.
        let (_reader, writer) = std::io::pipe().unwrap();
        let input = RuntimeInput::spawn(writer, || {});
        let started = Instant::now();
        let mut refused = 0;
        for _ in 0..500 {
            match input.send(&command(16 * 1024)) {
                Ok(()) => {}
                Err(message) => {
                    assert_eq!(message, COMMAND_NOT_READ);
                    refused += 1;
                }
            }
        }
        assert!(refused > 0, "a full pipe must be refused, not queued without end");
        assert!(started.elapsed() < Duration::from_secs(2), "sending must never wait on the pipe");
        assert!(input.queued_bytes.load(Ordering::SeqCst) <= MAX_QUEUED_COMMAND_BYTES);
    }

    #[test]
    fn the_queue_is_bounded_by_bytes_as_well_as_count() {
        let (_reader, writer) = std::io::pipe().unwrap();
        let input = RuntimeInput::spawn(writer, || {});
        // Each is just under the per-command limit; the byte budget refuses
        // long before MAX_QUEUED_COMMANDS is reached.
        let accepted = (0..MAX_QUEUED_COMMANDS)
            .filter(|_| input.send(&command(MAX_COMMAND_BYTES - 100)).is_ok())
            .count();
        assert!(accepted <= MAX_QUEUED_COMMAND_BYTES / (MAX_COMMAND_BYTES - 100) + 1);
        assert_eq!(input.send(&command(MAX_COMMAND_BYTES)).unwrap_err(), COMMAND_TOO_LARGE);
    }

    #[test]
    fn a_write_that_fails_is_unconfirmed_once_and_later_commands_are_refused() {
        let (reader, writer) = std::io::pipe().unwrap();
        drop(reader); // the runtime is gone
        let reported = Arc::new(AtomicUsize::new(0));
        let counter = reported.clone();
        let input = RuntimeInput::spawn(writer, move || {
            counter.fetch_add(1, Ordering::SeqCst);
        });
        input.send(&command(10)).unwrap(); // queued; the write then fails
        let deadline = Instant::now() + Duration::from_secs(5);
        while reported.load(Ordering::SeqCst) == 0 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(reported.load(Ordering::SeqCst), 1);
        assert_eq!(input.send(&command(10)).unwrap_err(), COMMAND_INPUT_CLOSED);
        assert_eq!(reported.load(Ordering::SeqCst), 1);
    }

    /// Run only as a child of the test below: an owned process that never
    /// reads its standard input, and exits on its own after 30 s.
    #[test]
    #[ignore]
    fn helper_child_that_never_reads_its_input() {
        std::thread::sleep(Duration::from_secs(30));
    }

    #[test]
    fn an_owned_child_that_never_reads_cannot_hold_commands_or_its_stop() {
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "runtime::runtime_input_tests::helper_child_that_never_reads_its_input",
                "--ignored",
                "--quiet",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let stdin = child.stdin.take().unwrap();
        let reported = Arc::new(AtomicUsize::new(0));
        let counter = reported.clone();
        let mut input = RuntimeInput::spawn(stdin, move || {
            counter.fetch_add(1, Ordering::SeqCst);
        });

        let started = Instant::now();
        let refused = (0..500).filter(|_| input.send(&command(16 * 1024)).is_err()).count();
        assert!(refused > 0 && started.elapsed() < Duration::from_secs(2));

        // Stop: queue the shutdown (or have it refused), close, kill after a
        // bounded wait. Nothing here waits on the child's full pipe.
        let started = Instant::now();
        let _ = input.send(&serde_json::json!({ "command": "shutdown" }));
        input.close();
        child.kill().unwrap();
        child.wait().unwrap();
        assert!(started.elapsed() < Duration::from_secs(2));

        // The writer, blocked on the full pipe, is released by the exit and
        // reports the command it was writing as unconfirmed, once.
        let deadline = Instant::now() + Duration::from_secs(5);
        while reported.load(Ordering::SeqCst) == 0 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(reported.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn a_closed_input_refuses_without_writing_and_queued_commands_still_go_out() {
        let (mut reader, writer) = std::io::pipe().unwrap();
        let mut input = RuntimeInput::spawn(writer, || {});
        input.send(&serde_json::json!({ "command": "shutdown" })).unwrap();
        input.close();
        assert_eq!(input.send(&command(1)).unwrap_err(), COMMAND_INPUT_CLOSED);
        let mut received = String::new();
        std::io::Read::read_to_string(&mut reader, &mut received).unwrap();
        assert_eq!(received, "{\"command\":\"shutdown\"}\n");
    }
}

#[cfg(test)]
mod screen_decision_tests {
    use super::*;

    const ID: &str = "AbCdEfGhIjKlMnOpQrStUv_-";

    #[test]
    fn prompt_answer_carries_only_the_identity_and_decision() {
        let payload = screen_decision_payload(Some(ID), true, Some("ignored text"), true, false)
            .expect("valid answer");
        assert_eq!(payload["command"], "action");
        assert_eq!(payload["actionId"], SCREEN_ACTION_ID);
        assert_eq!(payload["data"]["approvalId"], ID);
        assert_eq!(payload["data"]["approved"], true);
        assert_eq!(payload["data"]["windowOnly"], true);
        assert!(payload["data"].get("question").is_none());
        assert!(payload["data"].get("direct").is_none());
    }

    #[test]
    fn an_answer_without_identity_is_refused() {
        assert!(screen_decision_payload(None, true, Some("look"), true, false).is_err());
        assert!(screen_decision_payload(Some("   "), true, Some("look"), true, false).is_err());
    }

    #[test]
    fn malformed_identities_are_refused() {
        for bad in ["short", "../../etc/passwd/aaaaaaaaaaaaaaa", "has spaces in it aaaaaaaaaaa", &"a".repeat(65)] {
            assert!(screen_decision_payload(Some(bad), true, None, false, false).is_err(), "{bad}");
        }
    }

    #[test]
    fn direct_read_is_explicit_and_needs_a_question() {
        let payload = screen_decision_payload(None, true, Some(" what is this? "), false, true)
            .expect("valid direct read");
        assert_eq!(payload["data"]["direct"], true);
        assert_eq!(payload["data"]["question"], "what is this?");
        assert!(payload["data"].get("approvalId").is_none());
        assert!(screen_decision_payload(None, true, Some("  "), false, true).is_err());
        assert!(screen_decision_payload(None, true, None, false, true).is_err());
    }

    #[test]
    fn an_answer_cannot_also_be_a_direct_read() {
        assert!(screen_decision_payload(Some(ID), true, Some("look"), false, true).is_err());
    }

    #[test]
    fn generic_action_path_rejects_screen_decisions() {
        assert!(reject_typed_only_action("qronos.look_at_screen").is_err());
        assert!(reject_typed_only_action(" qronos.look_at_screen ").is_err());
        assert!(reject_typed_only_action("qronos.stop_response").is_ok());
        assert!(reject_typed_only_action("qronos.file_action_decision").is_ok());
    }
}

#[cfg(test)]
mod pairing_route_tests {
    use super::*;
    use std::time::{Duration, Instant};

    const NONCE: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const OTHER: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    const URI: &str = "qronos://pair?v=1&h=192.168.1.20&p=47711&d=0123456789abcdef&k=KEY&e=1";

    fn main_only() -> Vec<String> {
        vec!["main".to_string()]
    }

    fn event(event_type: &str, message: &str) -> RuntimeEvent {
        RuntimeEvent {
            event_type: event_type.to_string(),
            status: "ready".to_string(),
            message: message.to_string(),
        }
    }

    fn pairing_message(nonce: &str, uri: &str) -> String {
        serde_json::json!({ "requestNonce": nonce, "uri": uri }).to_string()
    }

    #[test]
    fn the_reply_goes_only_to_the_window_that_asked_and_only_once() {
        let mut pairings = PendingPairings::default();
        let now = Instant::now();
        pairings.begin(NONCE, "main", &main_only(), now).expect("main may pair");
        assert_eq!(pairings.take(OTHER, now), None, "another nonce finds nothing");
        assert_eq!(pairings.take(NONCE, now).as_deref(), Some("main"));
        assert_eq!(pairings.take(NONCE, now), None, "a replayed reply finds nothing");
    }

    #[test]
    fn only_windows_granted_the_default_capability_may_pair() {
        let mut pairings = PendingPairings::default();
        let now = Instant::now();
        assert!(pairings.begin(NONCE, "settings-popup", &main_only(), now).is_err());
        assert!(pairings.begin(NONCE, "main", &[], now).is_err(), "no capability, no pairing");
        assert_eq!(pairing_windows(), vec!["main".to_string()], "read from capabilities/default.json");
    }

    #[test]
    fn malformed_or_duplicate_nonces_are_refused() {
        let mut pairings = PendingPairings::default();
        let now = Instant::now();
        for bad in ["short", "has space here!!!!!", &"x".repeat(65)] {
            assert!(pairings.begin(bad, "main", &main_only(), now).is_err(), "{bad}");
        }
        pairings.begin(NONCE, "main", &main_only(), now).unwrap();
        assert!(pairings.begin(NONCE, "main", &main_only(), now).is_err());
    }

    #[test]
    fn a_new_request_replaces_the_window_s_earlier_one() {
        let mut pairings = PendingPairings::default();
        let now = Instant::now();
        pairings.begin(NONCE, "main", &main_only(), now).unwrap();
        pairings.begin(OTHER, "main", &main_only(), now).unwrap();
        assert_eq!(pairings.take(NONCE, now), None);
        assert_eq!(pairings.take(OTHER, now).as_deref(), Some("main"));
    }

    #[test]
    fn late_cancelled_and_cleared_requests_are_not_answered() {
        let mut pairings = PendingPairings::default();
        let now = Instant::now();
        pairings.begin(NONCE, "main", &main_only(), now).unwrap();
        let late = now + Duration::from_secs(PAIRING_PENDING_SECONDS + 1);
        assert_eq!(pairings.take(NONCE, late), None);

        pairings.begin(NONCE, "main", &main_only(), now).unwrap();
        pairings.cancel_window("main");
        assert_eq!(pairings.take(NONCE, now), None);

        pairings.begin(NONCE, "main", &main_only(), now).unwrap();
        pairings.clear();
        assert_eq!(pairings.take(NONCE, now), None);
    }

    #[test]
    fn pending_requests_are_bounded() {
        let mut pairings = PendingPairings::default();
        let now = Instant::now();
        let windows: Vec<String> = (0..=MAX_PENDING_PAIRINGS).map(|i| format!("w{i}")).collect();
        for (i, window) in windows.iter().enumerate().take(MAX_PENDING_PAIRINGS) {
            pairings.begin(&format!("{NONCE}{i:02}"), window, &windows, now).unwrap();
        }
        let extra = format!("{NONCE}zz");
        assert!(pairings.begin(&extra, &windows[MAX_PENDING_PAIRINGS], &windows, now).is_err());
    }

    #[test]
    fn a_pairing_reply_is_never_broadcast() {
        let routed = route_runtime_event(&event(LINK_PAIRING_EVENT_TYPE, &pairing_message(NONCE, URI)));
        assert_eq!(
            routed,
            EventRoute::Pairing(PairingDelivery { request_nonce: NONCE.to_string(), uri: URI.to_string() })
        );
        for message in [
            "not json".to_string(),
            pairing_message("short", URI),
            pairing_message(NONCE, "https://example.org/?k=1"),
            pairing_message(NONCE, &format!("{URI}{}", "x".repeat(400))),
            serde_json::json!({ "uri": URI }).to_string(),
        ] {
            assert_eq!(route_runtime_event(&event(LINK_PAIRING_EVENT_TYPE, &message)), EventRoute::Drop, "{message}");
        }
    }

    #[test]
    fn a_pairing_key_in_any_other_event_or_log_line_is_dropped() {
        assert_eq!(route_runtime_event(&event("runtime_log", URI)), EventRoute::Drop);
        assert_eq!(route_runtime_event(&event("link_status", &format!("{{\"x\":\"{URI}\"}}"))), EventRoute::Drop);
        assert_eq!(route_runtime_event(&event("link_status", "{\"running\":true}")), EventRoute::Broadcast);
    }

    #[test]
    fn a_failed_pairing_request_ends_pending_requests() {
        assert!(ends_pairing_requests(&event(
            "link_error",
            "{\"action\":\"qronos.link_pair_open\",\"code\":\"not_running\"}"
        )));
        assert!(!ends_pairing_requests(&event(
            "link_error",
            "{\"action\":\"qronos.link_revoke\",\"code\":\"unknown_device\"}"
        )));
    }

    #[test]
    fn a_runtime_that_stopped_or_is_being_restarted_ends_pending_requests() {
        assert!(ends_runtime_session(&event("runtime_stopping", "restarting")));
        assert!(!ends_runtime_session(&event("supervisor_state", "running")));
        assert!(!ends_runtime_session(&event("runtime_pong", "")));
    }

    #[test]
    fn the_generic_action_path_cannot_open_pairing() {
        assert!(reject_typed_only_action(LINK_PAIR_OPEN_ACTION).is_err());
        assert!(reject_typed_only_action(" qronos.link_pair_open ").is_err());
        assert!(reject_typed_only_action(LINK_PAIR_CANCEL_ACTION).is_ok());
    }

    #[test]
    fn device_names_follow_the_runtime_s_rules() {
        assert_eq!(clean_pairing_name("  گوشی\u{200C}ها ").unwrap(), "گوشی\u{200C}ها");
        assert!(clean_pairing_name(&"x".repeat(41)).is_err());
        assert!(clean_pairing_name("evil\u{202E}gpj").is_err());
        assert!(clean_pairing_name("a\u{7}b").is_err());
    }
}
