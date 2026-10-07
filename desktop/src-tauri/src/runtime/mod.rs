
use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Stdio};
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager, State};

mod backend;
use backend::{Backend, Service};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

const RUNTIME_EVENT_NAME: &str = "qronos://runtime-event";

const MAX_TEXT_MESSAGE_CHARS: usize = 16_000;
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
    stdin: ChildStdin,
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
    settings_command: Mutex<()>,

    backend: Option<Backend>,
}

impl RuntimeManagerState {
    fn new(backend: Option<Backend>) -> Self {
        Self {
            process: Mutex::new(None),
            native_audio: Mutex::new(NativeAudioPlaybackState {
                child: None,
                stop_generation: 0,
            }),
            settings_command: Mutex::new(()),
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

fn setup_snapshot_output(mut command: std::process::Command) -> Result<serde_json::Value, String> {
    command.arg("--models").stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null());
    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);
    let mut child = command.spawn().map_err(|_| "Could not inspect local Qronos setup.".to_string())?;
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if std::time::Instant::now() < deadline => {
                std::thread::sleep(std::time::Duration::from_millis(25));
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("Local setup inspection timed out or became unavailable.".to_string());
            }
        }
    }
    let output = child.wait_with_output().map_err(|_| "Local setup inspection failed.".to_string())?;
    if !output.status.success() || output.stdout.len() > 64 * 1024 {
        return Err("Local setup inspection returned no usable report.".to_string());
    }
    let value: serde_json::Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| "Local setup inspection returned an invalid report.".to_string())?;
    if value.get("schemaVersion").and_then(|v| v.as_u64()) != Some(1)
        || !value.get("features").is_some_and(|v| v.is_object())
        || !value.get("modelServer").is_some_and(|v| v.is_object()) {
        return Err("Local setup report uses an unsupported format.".to_string());
    }
    Ok(value)
}

#[tauri::command]
pub async fn runtime_setup_snapshot(state: State<'_, RuntimeManagerState>) -> Result<serde_json::Value, String> {
    let command = state.require_backend()?.command(Service::Doctor)?;
    tauri::async_runtime::spawn_blocking(move || setup_snapshot_output(command))
        .await.map_err(|_| "Local setup inspection could not finish.".to_string())?
}

fn spawn_runtime_reader(app: AppHandle, stdout: std::process::ChildStdout) {
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);

        for line in reader.lines() {
            let Ok(line) = line else {
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
                Ok(event) => emit_runtime_event(&app, event),
                Err(_) => emit_runtime_event(
                    &app,
                    RuntimeEvent {
                        event_type: "runtime_log".to_string(),
                        status: "running".to_string(),
                        message: trimmed.to_string(),
                    },
                ),
            }
        }
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
    writeln!(process.stdin, "{payload}")
        .map_err(|error| format!("Could not write to Qronos runtime: {error}"))?;

    process
        .stdin
        .flush()
        .map_err(|error| format!("Could not flush Qronos runtime command: {error}"))
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
    let mut guard = state
        .process
        .lock()
        .map_err(|_| "Runtime manager lock failed.".to_string())?;

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

    let mut command = state.require_backend()?.command(Service::Bridge)?;
    command
        .env("QRONOS_MEMORY_RUNTIME", "1")
        .env("QRONOS_DESKTOP_WAKE_BOOTSTRAP", "1")
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

    spawn_runtime_reader(app, stdout);

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
    let mut runtime_process = RuntimeProcess { child, stdin };

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
        message: "Qronos runtime process started with native wake bootstrap.".to_string(),
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
    state: State<'_, RuntimeManagerState>,
) -> Result<(), String> {
    let cleaned = action_id.trim();

    if cleaned.is_empty() {
        return Err("Runtime action id must not be empty.".to_string());
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

#[tauri::command(rename_all = "camelCase")]
pub fn library_command(
    operation: String,
    payload: Option<serde_json::Value>,
    state: State<'_, RuntimeManagerState>,
) -> Result<serde_json::Value, String> {
    let cleaned = operation.trim();
    if cleaned.is_empty() {
        return Err("Library operation must not be empty.".to_string());
    }
    let mut command = state.require_backend()?.command(Service::Library)?;
    command
        .arg(cleaned)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);
    let mut child = command.spawn().map_err(|error| format!("Could not start Qronos Library backend: {error}"))?;
    if let Some(mut stdin) = child.stdin.take() {
        let body = payload.unwrap_or_else(|| serde_json::json!({}));
        write!(stdin, "{}", body).map_err(|error| format!("Could not send Library payload: {error}"))?;
    }
    let output = child.wait_with_output().map_err(|error| format!("Library backend failed to finish: {error}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    if stdout.is_empty() {
        return Err(if stderr.is_empty() { "Library backend returned no data.".to_string() } else { stderr });
    }
    let value: serde_json::Value = serde_json::from_str(&stdout)
        .map_err(|error| format!("Invalid Library backend response: {error}; output={stdout}"))?;
    if !output.status.success() || value.get("ok").and_then(|v| v.as_bool()) == Some(false) {
        let message = value.get("error").and_then(|v| v.as_str()).unwrap_or(&stderr);
        return Err(if message.is_empty() { "Library operation failed.".to_string() } else { message.to_string() });
    }
    Ok(value)
}

#[tauri::command(rename_all = "camelCase")]
pub fn memory_command(
    operation: String,
    payload: Option<serde_json::Value>,
    state: State<'_, RuntimeManagerState>,
) -> Result<serde_json::Value, String> {
    let cleaned = operation.trim();
    if cleaned.is_empty() {
        return Err("Memory operation must not be empty.".to_string());
    }
    let mut command = state.require_backend()?.command(Service::Memory)?;
    command
        .arg(cleaned)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);
    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start Qronos Memory backend: {error}"))?;
    if let Some(mut stdin) = child.stdin.take() {
        let body = payload.unwrap_or_else(|| serde_json::json!({}));
        write!(stdin, "{}", body)
            .map_err(|error| format!("Could not send Memory payload: {error}"))?;
    }
    let output = child
        .wait_with_output()
        .map_err(|error| format!("Memory backend failed to finish: {error}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    if stdout.is_empty() {
        return Err(if stderr.is_empty() {
            "Memory backend returned no data.".to_string()
        } else {
            stderr
        });
    }
    let value: serde_json::Value = serde_json::from_str(&stdout)
        .map_err(|error| format!("Invalid Memory backend response: {error}; output={stdout}"))?;
    if !output.status.success() || value.get("ok").and_then(|v| v.as_bool()) == Some(false) {
        let message = value
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or(&stderr);
        return Err(if message.is_empty() {
            "Memory operation failed.".to_string()
        } else {
            message.to_string()
        });
    }
    Ok(value)
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
pub fn settings_command(
    operation: String,
    payload: Option<serde_json::Value>,
    state: State<'_, RuntimeManagerState>,
) -> Result<serde_json::Value, String> {
    let cleaned = operation.trim();

    if cleaned.is_empty() {
        return Err("Settings operation must not be empty.".to_string());
    }

    let _settings_guard = state
        .settings_command
        .lock()
        .map_err(|_| "Settings command lock failed.".to_string())?;

    let mut command = state.require_backend()?.command(Service::Settings)?;
    command
        .arg(cleaned)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);

    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start Qronos Settings backend: {error}"))?;

    if let Some(mut stdin) = child.stdin.take() {
        let body = payload.unwrap_or_else(|| serde_json::json!({}));

        write!(stdin, "{}", body)
            .map_err(|error| format!("Could not send Settings payload: {error}"))?;
    }

    let output = child
        .wait_with_output()
        .map_err(|error| format!("Settings backend failed to finish: {error}"))?;

    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();

    if stdout.is_empty() {
        return Err(if stderr.is_empty() {
            "Settings backend returned no data.".to_string()
        } else {
            stderr
        });
    }

    let value: serde_json::Value = serde_json::from_str(&stdout).map_err(|error| {
        format!("Invalid Settings backend response: {error}; output={stdout}")
    })?;

    if !output.status.success() || value.get("ok").and_then(|v| v.as_bool()) == Some(false) {
        let message = value
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or(&stderr);

        return Err(if message.is_empty() {
            "Settings operation failed.".to_string()
        } else {
            message.to_string()
        });
    }

    Ok(value)
}

#[tauri::command(rename_all = "camelCase")]
pub fn permissions_command(
    operation: String,
    payload: Option<serde_json::Value>,
    state: State<'_, RuntimeManagerState>,
) -> Result<serde_json::Value, String> {
    let cleaned = operation.trim();
    if cleaned.is_empty() {
        return Err("Permissions operation must not be empty.".to_string());
    }

    let mut command = state.require_backend()?.command(Service::Permissions)?;
    command
        .arg(cleaned)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);

    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start Qronos Permissions backend: {error}"))?;

    if let Some(mut stdin) = child.stdin.take() {
        let body = payload.unwrap_or_else(|| serde_json::json!({}));
        write!(stdin, "{}", body)
            .map_err(|error| format!("Could not send Permissions payload: {error}"))?;
    }

    let output = child
        .wait_with_output()
        .map_err(|error| format!("Permissions backend failed to finish: {error}"))?;

    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();

    if stdout.is_empty() {
        return Err(if stderr.is_empty() {
            "Permissions backend returned no data.".to_string()
        } else {
            stderr
        });
    }

    let value: serde_json::Value = serde_json::from_str(&stdout).map_err(|error| {
        format!("Invalid Permissions backend response: {error}; output={stdout}")
    })?;

    if !output.status.success() || value.get("ok").and_then(|v| v.as_bool()) == Some(false) {
        let message = value
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or(&stderr);

        return Err(if message.is_empty() {
            "Permissions operation failed.".to_string()
        } else {
            message.to_string()
        });
    }

    Ok(value)
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

#[tauri::command]
pub fn stop_runtime(state: State<'_, RuntimeManagerState>) -> Result<RuntimeStatus, String> {
    let mut guard = state
        .process
        .lock()
        .map_err(|_| "Runtime manager lock failed.".to_string())?;

    let Some(mut process) = guard.take() else {
        return Ok(RuntimeStatus {
            running: false,
            status: "stopped".to_string(),
            message: "Qronos runtime is already stopped.".to_string(),
        });
    };

    let _ = write_runtime_command(&mut process, "shutdown");

    for _ in 0..20 {
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

    let Some(mut process) = guard.take() else {
        return;
    };

    // Give the Python runtime the same graceful-shutdown opportunity used by
    // stop_runtime(). This matters because Python owns child resources such as
    // the persistent whisper-server process. Killing Python immediately after
    // writing "shutdown" can orphan those children and leave GPU memory and
    // localhost ports allocated after the desktop has exited.
    let _ = write_runtime_command(&mut process, "shutdown");

    for _ in 0..20 {
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

    // Shutdown is bounded: if Python does not exit within roughly one second,
    // force-stop only the runtime process owned by this manager.
    let _ = process.child.kill();
}
