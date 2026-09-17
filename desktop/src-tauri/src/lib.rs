use serde::Serialize;
use std::sync::Mutex;
use tauri::Manager;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{
    MouseButton,
    MouseButtonState,
    TrayIconBuilder,
    TrayIconEvent,
};
use tauri_plugin_autostart::{
    MacosLauncher,
    ManagerExt as AutostartManagerExt,
};
use std::time::{Duration, Instant};
use sysinfo::{Components, DiskKind, Disks, MemoryRefreshKind, RefreshKind, System};

mod hotkeys;
mod runtime;

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[cfg(target_os = "windows")]
use std::process::Command;

const CREATE_NO_WINDOW: u32 = 0x08000000;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DiskSnapshot {
    id: String,
    name: String,
    mount_point: String,
    file_system: String,
    kind: String,
    total_bytes: u64,
    available_bytes: u64,
    used_bytes: u64,
    used_percent: f32,
    removable: bool,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DeviceSnapshot {
    id: String,
    name: String,
    class_name: String,
    status: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct TelemetrySnapshot {
    cpu_percent: f32,
    cpu_brand: String,
    physical_cores: usize,
    logical_cores: usize,

    gpu_percent: Option<f32>,

    memory_percent: f32,
    memory_used_bytes: u64,
    memory_total_bytes: u64,

    temperature_c: Option<f32>,

    disks: Vec<DiskSnapshot>,
    devices: Vec<DeviceSnapshot>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct SystemIntegrationSnapshot {
    launch_at_startup: bool,
    keep_in_tray: bool,
}

struct SystemIntegrationState {
    keep_in_tray: Mutex<bool>,
}

impl SystemIntegrationState {
    fn new() -> Self {
        Self {
            keep_in_tray: Mutex::new(false),
        }
    }

    fn keep_in_tray(&self) -> Result<bool, String> {
        self.keep_in_tray
            .lock()
            .map(|value| *value)
            .map_err(|_| "System tray state lock failed.".to_string())
    }

    fn set_keep_in_tray(&self, enabled: bool) -> Result<(), String> {
        let mut value = self
            .keep_in_tray
            .lock()
            .map_err(|_| "System tray state lock failed.".to_string())?;

        *value = enabled;
        Ok(())
    }
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn system_integration_snapshot(
    app: &tauri::AppHandle,
    state: &SystemIntegrationState,
) -> Result<SystemIntegrationSnapshot, String> {
    let launch_at_startup = app
        .autolaunch()
        .is_enabled()
        .map_err(|error| format!("Could not read Windows autostart state: {error}"))?;

    Ok(SystemIntegrationSnapshot {
        launch_at_startup,
        keep_in_tray: state.keep_in_tray()?,
    })
}

#[tauri::command]
fn get_system_integration_state(
    app: tauri::AppHandle,
    state: tauri::State<'_, SystemIntegrationState>,
) -> Result<SystemIntegrationSnapshot, String> {
    system_integration_snapshot(&app, &state)
}

#[tauri::command]
fn set_launch_at_startup(
    enabled: bool,
    app: tauri::AppHandle,
    state: tauri::State<'_, SystemIntegrationState>,
) -> Result<SystemIntegrationSnapshot, String> {
    let manager = app.autolaunch();

    if enabled {
        manager
            .enable()
            .map_err(|error| format!("Could not enable Windows autostart: {error}"))?;
    } else {
        manager
            .disable()
            .map_err(|error| format!("Could not disable Windows autostart: {error}"))?;
    }

    system_integration_snapshot(&app, &state)
}

#[tauri::command]
fn set_keep_in_tray(
    enabled: bool,
    app: tauri::AppHandle,
    state: tauri::State<'_, SystemIntegrationState>,
) -> Result<SystemIntegrationSnapshot, String> {
    let tray = app
        .tray_by_id("qronos-main-tray")
        .ok_or_else(|| "Qronos tray icon is not initialized.".to_string())?;

    tray
        .set_visible(enabled)
        .map_err(|error| format!("Could not change tray visibility: {error}"))?;

    state.set_keep_in_tray(enabled)?;

    system_integration_snapshot(&app, &state)
}

struct TelemetryState {
    system: Mutex<System>,
    devices_cache: Mutex<DeviceCache>,
}

struct DeviceCache {
    last_refresh: Option<Instant>,
    devices: Vec<DeviceSnapshot>,
}

#[derive(Debug, Clone, Copy)]
struct GpuTelemetry {
    utilization_percent: Option<f32>,
    temperature_c: Option<f32>,
}

impl TelemetryState {
    fn new() -> Self {
        let refresh_kind = RefreshKind::nothing()
            .with_cpu(sysinfo::CpuRefreshKind::everything())
            .with_memory(MemoryRefreshKind::everything());

        let mut system = System::new_with_specifics(refresh_kind);

        system.refresh_cpu_all();
        system.refresh_memory();

        Self {
            system: Mutex::new(system),

            devices_cache: Mutex::new(DeviceCache {
                last_refresh: None,

                devices: Vec::new(),
            }),
        }
    }
}

fn disk_kind_name(kind: DiskKind) -> String {
    match kind {
        DiskKind::HDD => "HDD".to_string(),

        DiskKind::SSD => "SSD".to_string(),

        DiskKind::Unknown(_) => "STORAGE".to_string(),
    }
}

fn read_disks() -> Vec<DiskSnapshot> {
    let disks = Disks::new_with_refreshed_list();

    disks
        .iter()
        .map(|disk| {
            let total = disk.total_space();

            let available = disk.available_space();

            let used = total.saturating_sub(available);

            let used_percent = if total == 0 {
                0.0
            } else {
                (used as f64 / total as f64 * 100.0) as f32
            };

            let mount = disk.mount_point().to_string_lossy().to_string();

            let name = disk.name().to_string_lossy().to_string();

            let file_system = disk.file_system().to_string_lossy().to_string();

            DiskSnapshot {
                id: mount.clone(),

                name,

                mount_point: mount,

                file_system,

                kind: disk_kind_name(disk.kind()),

                total_bytes: total,

                available_bytes: available,

                used_bytes: used,

                used_percent,

                removable: disk.is_removable(),
            }
        })
        .collect()
}

#[cfg(target_os = "windows")]
fn read_nvidia_gpu() -> Option<GpuTelemetry> {
    const NVIDIA_SMI_CANDIDATES: [&str; 3] = [
        "nvidia-smi.exe",
        r"C:\Windows\System32\nvidia-smi.exe",
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    ];

    let mut output = None;

    for executable in NVIDIA_SMI_CANDIDATES {
        let result = Command::new(executable)
            .args([
                "--query-gpu=utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ])
            .creation_flags(CREATE_NO_WINDOW)
            .output();

        match result {
            Ok(value) if value.status.success() => {
                output = Some(value);

                break;
            }

            _ => {}
        }
    }

    let output = output?;

    let text = String::from_utf8_lossy(&output.stdout);

    let mut utilization_values = Vec::<f32>::new();

    let mut temperature_values = Vec::<f32>::new();

    for line in text.lines() {
        let mut parts = line.split(',');

        let utilization = parts
            .next()
            .map(str::trim)
            .and_then(|value| value.parse::<f32>().ok())
            .filter(|value| value.is_finite() && *value >= 0.0 && *value <= 100.0);

        let temperature = parts
            .next()
            .map(str::trim)
            .and_then(|value| value.parse::<f32>().ok())
            .filter(|value| value.is_finite() && *value > 0.0 && *value < 130.0);

        if let Some(value) = utilization {
            utilization_values.push(value);
        }

        if let Some(value) = temperature {
            temperature_values.push(value);
        }
    }

    if utilization_values.is_empty() && temperature_values.is_empty() {
        return None;
    }

    let utilization_percent = if utilization_values.is_empty() {
        None
    } else {
        Some(utilization_values.iter().sum::<f32>() / utilization_values.len() as f32)
    };

    let temperature_c = temperature_values.into_iter().reduce(f32::max);

    Some(GpuTelemetry {
        utilization_percent,
        temperature_c,
    })
}

#[cfg(not(target_os = "windows"))]
fn read_nvidia_gpu() -> Option<GpuTelemetry> {
    None
}

fn read_temperature() -> Option<f32> {
    let components = Components::new_with_refreshed_list();

    let mut temperatures = Vec::<f32>::new();

    for component in &components {
        if let Some(temp) = component.temperature() {
            if temp.is_finite() && temp > 0.0 && temp < 130.0 {
                temperatures.push(temp);
            }
        }
    }

    if temperatures.is_empty() {
        None
    } else {
        Some(temperatures.iter().sum::<f32>() / temperatures.len() as f32)
    }
}

#[cfg(target_os = "windows")]
fn read_windows_devices() -> Vec<DeviceSnapshot> {
    let script = r#"
$ErrorActionPreference = 'SilentlyContinue'

$wanted = @(
  'Printer',
  'Camera',
  'AudioEndpoint',
  'Bluetooth',
  'Media',
  'Image',
  'SmartCardReader'
)

Get-PnpDevice -PresentOnly |
Where-Object {
  $_.FriendlyName -and
  ($wanted -contains $_.Class)
} |
Select-Object -First 30 `
  FriendlyName,
  Class,
  Status,
  InstanceId |
ConvertTo-Json -Compress
"#;

    let output = Command::new("powershell.exe")
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ])
        .creation_flags(CREATE_NO_WINDOW)
        .output();

    let Ok(output) = output else {
        return Vec::new();
    };

    if !output.status.success() {
        return Vec::new();
    }

    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();

    if text.is_empty() || text == "null" {
        return Vec::new();
    }

    let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) else {
        return Vec::new();
    };

    let values = match value {
        serde_json::Value::Array(items) => items,

        item @ serde_json::Value::Object(_) => vec![item],

        _ => Vec::new(),
    };

    values
        .into_iter()
        .filter_map(|item| {
            let name = item.get("FriendlyName")?.as_str()?.trim().to_string();

            if name.is_empty() {
                return None;
            }

            let class_name = item
                .get("Class")
                .and_then(|value| value.as_str())
                .unwrap_or("Device")
                .to_string();

            let status = item
                .get("Status")
                .and_then(|value| value.as_str())
                .unwrap_or("Unknown")
                .to_string();

            let id = item
                .get("InstanceId")
                .and_then(|value| value.as_str())
                .unwrap_or(&name)
                .to_string();

            Some(DeviceSnapshot {
                id,
                name,
                class_name,
                status,
            })
        })
        .collect()
}

#[cfg(not(target_os = "windows"))]
fn read_windows_devices() -> Vec<DeviceSnapshot> {
    Vec::new()
}

fn get_devices_cached(state: &TelemetryState) -> Vec<DeviceSnapshot> {
    let mut cache = match state.devices_cache.lock() {
        Ok(cache) => cache,

        Err(_) => return Vec::new(),
    };

    let should_refresh = match cache.last_refresh {
        None => true,

        Some(last) => last.elapsed() >= Duration::from_secs(8),
    };

    if should_refresh {
        cache.devices = read_windows_devices();

        cache.last_refresh = Some(Instant::now());
    }

    cache.devices.clone()
}

#[tauri::command]
fn get_system_snapshot(
    state: tauri::State<'_, TelemetryState>,
) -> Result<TelemetrySnapshot, String> {
    let (
        cpu_percent,
        cpu_brand,
        physical_cores,
        logical_cores,
        memory_percent,
        memory_used_bytes,
        memory_total_bytes,
    ) = {
        let mut system = state
            .system
            .lock()
            .map_err(|_| "Telemetry system lock failed".to_string())?;

        system.refresh_cpu_usage();
        system.refresh_memory();

        let cpu_percent = system.global_cpu_usage().clamp(0.0, 100.0);

        let logical_cores = system.cpus().len();

        let physical_cores = System::physical_core_count().unwrap_or(logical_cores);

        let cpu_brand = system
            .cpus()
            .first()
            .map(|cpu| cpu.brand().trim().to_string())
            .filter(|brand| !brand.is_empty())
            .unwrap_or_else(|| "CPU".to_string());

        let total = system.total_memory();

        let used = system.used_memory();

        let memory_percent = if total == 0 {
            0.0
        } else {
            (used as f64 / total as f64 * 100.0) as f32
        };

        (
            cpu_percent,
            cpu_brand,
            physical_cores,
            logical_cores,
            memory_percent,
            used,
            total,
        )
    };

    let gpu = read_nvidia_gpu();

    let gpu_percent = gpu
        .and_then(|value| value.utilization_percent)
        .map(|value| value.clamp(0.0, 100.0));

    let temperature_c = gpu
        .and_then(|value| value.temperature_c)
        .or_else(read_temperature);

    Ok(TelemetrySnapshot {
        cpu_percent,
        cpu_brand,
        physical_cores,
        logical_cores,

        gpu_percent,

        memory_percent: memory_percent.clamp(0.0, 100.0),

        memory_used_bytes,
        memory_total_bytes,

        temperature_c,

        disks: read_disks(),

        devices: get_devices_cached(&state),
    })
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn open_storage_path(path: String) -> Result<(), String> {
    let clean_path = path.trim();

    if clean_path.is_empty() {
        return Err("Storage path is empty.".to_string());
    }

    Command::new("explorer.exe")
        .arg(clean_path)
        .spawn()
        .map_err(|error| format!("Failed to open storage path '{}': {}", clean_path, error))?;

    Ok(())
}

#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn open_storage_path(_path: String) -> Result<(), String> {
    Err("Storage opening is currently supported on Windows only.".to_string())
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn open_device_properties(instance_id: String) -> Result<(), String> {
    let clean_id = instance_id.trim();

    if clean_id.is_empty() {
        return Err("Device instance ID is empty.".to_string());
    }

    Command::new("rundll32.exe")
        .arg("devmgr.dll,DeviceProperties_RunDLL")
        .arg("/DeviceID")
        .arg(clean_id)
        .spawn()
        .map_err(|error| format!("Failed to open device properties '{}': {}", clean_id, error))?;

    Ok(())
}

#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn open_device_properties(_instance_id: String) -> Result<(), String> {
    Err("Device properties are currently supported on Windows only.".to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    hotkeys::handle_global_shortcut(app, shortcut, event);
                })
                .build(),
        )
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            None,
        ))
        .manage(TelemetryState::new())
        .manage(SystemIntegrationState::new())
        .setup(|app| {
            hotkeys::initialize(app)?;
            runtime::initialize(app)?;

            let show_item = MenuItem::with_id(
                app,
                "qronos-tray-show",
                "Show Qronos",
                true,
                None::<&str>,
            )?;

            let quit_item = MenuItem::with_id(
                app,
                "qronos-tray-quit",
                "Quit Qronos",
                true,
                None::<&str>,
            )?;

            let menu = Menu::with_items(
                app,
                &[&show_item, &quit_item],
            )?;

            let mut tray_builder = TrayIconBuilder::with_id(
                "qronos-main-tray"
            )
            .tooltip("Qronos")
            .menu(&menu)
            .show_menu_on_left_click(false)
            .on_menu_event(|app, event| {
                match event.id().as_ref() {
                    "qronos-tray-show" => {
                        show_main_window(app);
                    }
                    "qronos-tray-quit" => {
                        app.exit(0);
                    }
                    _ => {}
                }
            })
            .on_tray_icon_event(|tray, event| {
                if let TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                } = event
                {
                    show_main_window(
                        tray.app_handle()
                    );
                }
            });

            if let Some(icon) = app.default_window_icon() {
                tray_builder = tray_builder.icon(icon.clone());
            }

            let tray = tray_builder.build(app)?;
            tray.set_visible(false)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return;
            }

            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let state = window.state::<SystemIntegrationState>();

                if state.keep_in_tray().unwrap_or(false) {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_system_snapshot,
            open_storage_path,
            open_device_properties,
            get_system_integration_state,
            set_launch_at_startup,
            set_keep_in_tray,
            hotkeys::get_hotkey_settings,
            hotkeys::validate_hotkey,
            hotkeys::update_hotkey,
            hotkeys::set_hotkey_enabled,
            hotkeys::reset_hotkeys,
            runtime::get_runtime_status,
            runtime::start_runtime,
            runtime::ping_runtime,
            runtime::send_runtime_action,
            runtime::library_command,
            runtime::memory_command,
            runtime::settings_command,
            runtime::native_audio_output_play,
            runtime::native_audio_output_stop,
            runtime::permissions_command,
            runtime::send_text_message,
            runtime::queue_list,
            runtime::queue_submit,
            runtime::queue_cancel,
            runtime::queue_override,
            runtime::queue_set_paused,
            runtime::runtime_set_paused,
            runtime::stop_runtime
        ])
        .build(tauri::generate_context!())
        .expect("error while building Qronos desktop application");

    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            runtime::shutdown(app_handle);
        }
    });
}
