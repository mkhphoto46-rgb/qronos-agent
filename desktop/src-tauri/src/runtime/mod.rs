use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager, State};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

const RUNTIME_EVENT_NAME: &str = "qronos://runtime-event";

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

pub struct RuntimeManagerState {
    process: Mutex<Option<RuntimeProcess>>,
    project_root: PathBuf,
}

impl RuntimeManagerState {
    pub fn new(project_root: PathBuf) -> Self {
        Self {
            process: Mutex::new(None),
            project_root,
        }
    }
}

fn find_project_root() -> Result<PathBuf, String> {
    let current = std::env::current_dir()
        .map_err(|error| format!("Could not read current directory: {error}"))?;

    for candidate in current.ancestors() {
        if candidate.join("core").is_dir() && candidate.join("desktop").is_dir() {
            return Ok(candidate.to_path_buf());
        }
    }

    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    for candidate in manifest_dir.ancestors() {
        if candidate.join("core").is_dir() && candidate.join("desktop").is_dir() {
            return Ok(candidate.to_path_buf());
        }
    }

    Err("Qronos project root could not be located.".to_string())
}

fn python_candidates(project_root: &Path) -> Vec<PathBuf> {
    let mut candidates = vec![
        project_root.join(".venv").join("Scripts").join("python.exe"),
        project_root.join("venv").join("Scripts").join("python.exe"),
    ];

    if let Ok(executable) = std::env::var("PYTHON") {
        if !executable.trim().is_empty() {
            candidates.push(PathBuf::from(executable));
        }
    }

    candidates.push(PathBuf::from("python"));
    candidates.push(PathBuf::from("py"));

    candidates
}

fn resolve_python(project_root: &Path) -> Result<PathBuf, String> {
    for candidate in python_candidates(project_root) {
        if candidate.is_absolute() {
            if candidate.is_file() {
                return Ok(candidate);
            }
            continue;
        }

        let probe = Command::new(&candidate)
            .arg("--version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();

        if probe.map(|status| status.success()).unwrap_or(false) {
            return Ok(candidate);
        }
    }

    Err(
        "No usable Python runtime was found for Qronos development mode."
            .to_string(),
    )
}

fn runtime_script(project_root: &Path) -> PathBuf {
    project_root.join("core").join("runtime_bridge.py")
}

fn emit_runtime_event(app: &AppHandle, event: RuntimeEvent) {
    let _ = app.emit(RUNTIME_EVENT_NAME, event);
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

fn write_runtime_command(
    process: &mut RuntimeProcess,
    command: &str,
) -> Result<(), String> {
    let payload = serde_json::json!({
        "command": command
    });

    writeln!(process.stdin, "{payload}")
        .map_err(|error| format!("Could not write to Qronos runtime: {error}"))?;

    process
        .stdin
        .flush()
        .map_err(|error| format!("Could not flush Qronos runtime command: {error}"))
}

pub fn initialize(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let project_root = find_project_root()
        .map_err(std::io::Error::other)?;

    app.manage(RuntimeManagerState::new(project_root));

    Ok(())
}

#[tauri::command]
pub fn get_runtime_status(
    state: State<'_, RuntimeManagerState>,
) -> Result<RuntimeStatus, String> {
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
                return Err(format!(
                    "Could not inspect Qronos runtime process: {error}"
                ));
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

    let script = runtime_script(&state.project_root);

    if !script.is_file() {
        return Err(format!(
            "Runtime bridge script was not found: {}",
            script.display()
        ));
    }

    let python = resolve_python(&state.project_root)?;

    let mut command = Command::new(python);
    command
        .arg("-u")
        .arg("-m")
        .arg("core.runtime_bridge")
        .current_dir(&state.project_root)
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

                let looks_like_error =
                    trimmed.starts_with("Traceback")
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

    *guard = Some(RuntimeProcess { child, stdin });

    Ok(RuntimeStatus {
        running: true,
        status: "starting".to_string(),
        message: "Qronos runtime process started.".to_string(),
    })
}

#[tauri::command]
pub fn ping_runtime(
    state: State<'_, RuntimeManagerState>,
) -> Result<(), String> {
    let mut guard = state
        .process
        .lock()
        .map_err(|_| "Runtime manager lock failed.".to_string())?;

    let process = guard
        .as_mut()
        .ok_or_else(|| "Qronos runtime is not running.".to_string())?;

    write_runtime_command(process, "ping")
}

#[tauri::command]
pub fn send_runtime_action(
    action_id: String,
    state: State<'_, RuntimeManagerState>,
) -> Result<(), String> {
    let cleaned = action_id.trim();

    if cleaned.is_empty() {
        return Err("Runtime action id must not be empty.".to_string());
    }

    let mut guard = state
        .process
        .lock()
        .map_err(|_| "Runtime manager lock failed.".to_string())?;

    let process = guard
        .as_mut()
        .ok_or_else(|| "Qronos runtime is not running.".to_string())?;

    let payload = serde_json::json!({
        "command": "action",
        "actionId": cleaned
    });

    writeln!(process.stdin, "{payload}")
        .map_err(|error| format!("Could not write runtime action: {error}"))?;

    process
        .stdin
        .flush()
        .map_err(|error| format!("Could not flush runtime action: {error}"))
}

#[tauri::command]
pub fn stop_runtime(
    state: State<'_, RuntimeManagerState>,
) -> Result<RuntimeStatus, String> {
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

    let _ = write_runtime_command(&mut process, "shutdown");
    let _ = process.child.kill();
}
