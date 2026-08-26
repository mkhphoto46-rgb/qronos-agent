use serde::Serialize;
use std::sync::Mutex;
use std::time::{Duration, Instant};
use sysinfo::{
    Components,
    DiskKind,
    Disks,
    MemoryRefreshKind,
    RefreshKind,
    System,
};

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

    memory_percent: f32,
    memory_used_bytes: u64,
    memory_total_bytes: u64,

    temperature_c: Option<f32>,

    disks: Vec<DiskSnapshot>,
    devices: Vec<DeviceSnapshot>,
}

struct TelemetryState {
    system: Mutex<System>,
    devices_cache: Mutex<DeviceCache>,
}

struct DeviceCache {
    last_refresh: Option<Instant>,
    devices: Vec<DeviceSnapshot>,
}

impl TelemetryState {
    fn new() -> Self {
        let refresh_kind =
            RefreshKind::nothing()
                .with_cpu(
                    sysinfo::CpuRefreshKind::everything(),
                )
                .with_memory(
                    MemoryRefreshKind::everything(),
                );

        let mut system =
            System::new_with_specifics(
                refresh_kind,
            );

        system.refresh_cpu_all();
        system.refresh_memory();

        Self {
            system:
                Mutex::new(
                    system,
                ),

            devices_cache:
                Mutex::new(
                    DeviceCache {
                        last_refresh:
                            None,

                        devices:
                            Vec::new(),
                    },
                ),
        }
    }
}

fn disk_kind_name(
    kind: DiskKind,
) -> String {
    match kind {
        DiskKind::HDD =>
            "HDD".to_string(),

        DiskKind::SSD =>
            "SSD".to_string(),

        DiskKind::Unknown(_) =>
            "STORAGE".to_string(),
    }
}

fn read_disks() -> Vec<DiskSnapshot> {
    let disks =
        Disks::new_with_refreshed_list();

    disks
        .iter()
        .map(
            |disk| {
                let total =
                    disk.total_space();

                let available =
                    disk.available_space();

                let used =
                    total.saturating_sub(
                        available,
                    );

                let used_percent =
                    if total == 0 {
                        0.0
                    } else {
                        (
                            used as f64
                            / total as f64
                            * 100.0
                        ) as f32
                    };

                let mount =
                    disk
                        .mount_point()
                        .to_string_lossy()
                        .to_string();

                let name =
                    disk
                        .name()
                        .to_string_lossy()
                        .to_string();

                let file_system =
                    disk
                        .file_system()
                        .to_string_lossy()
                        .to_string();

                DiskSnapshot {
                    id:
                        mount.clone(),

                    name,

                    mount_point:
                        mount,

                    file_system,

                    kind:
                        disk_kind_name(
                            disk.kind(),
                        ),

                    total_bytes:
                        total,

                    available_bytes:
                        available,

                    used_bytes:
                        used,

                    used_percent,

                    removable:
                        disk.is_removable(),
                }
            },
        )
        .collect()
}

fn read_temperature() -> Option<f32> {
    let components =
        Components::new_with_refreshed_list();

    let mut temperatures =
        Vec::<f32>::new();

    for component in &components {
        if let Some(temp) =
            component.temperature()
        {
            if temp.is_finite()
                && temp > 0.0
                && temp < 130.0
            {
                temperatures.push(
                    temp,
                );
            }
        }
    }

    if temperatures.is_empty() {
        None
    } else {
        Some(
            temperatures
                .iter()
                .sum::<f32>()
                / temperatures.len()
                    as f32,
        )
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

    let output =
        Command::new(
            "powershell.exe",
        )
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ])
        .creation_flags(
            CREATE_NO_WINDOW,
        )
        .output();

    let Ok(output) = output else {
        return Vec::new();
    };

    if !output.status.success() {
        return Vec::new();
    }

    let text =
        String::from_utf8_lossy(
            &output.stdout,
        )
        .trim()
        .to_string();

    if text.is_empty()
        || text == "null"
    {
        return Vec::new();
    }

    let Ok(value) =
        serde_json::from_str::<
            serde_json::Value,
        >(&text)
    else {
        return Vec::new();
    };

    let values =
        match value {
            serde_json::Value::Array(
                items,
            ) =>
                items,

            item @
            serde_json::Value::Object(
                _,
            ) =>
                vec![item],

            _ =>
                Vec::new(),
        };

    values
        .into_iter()
        .filter_map(
            |item| {
                let name =
                    item
                        .get(
                            "FriendlyName",
                        )?
                        .as_str()?
                        .trim()
                        .to_string();

                if name.is_empty() {
                    return None;
                }

                let class_name =
                    item
                        .get(
                            "Class",
                        )
                        .and_then(
                            |value| {
                                value.as_str()
                            },
                        )
                        .unwrap_or(
                            "Device",
                        )
                        .to_string();

                let status =
                    item
                        .get(
                            "Status",
                        )
                        .and_then(
                            |value| {
                                value.as_str()
                            },
                        )
                        .unwrap_or(
                            "Unknown",
                        )
                        .to_string();

                let id =
                    item
                        .get(
                            "InstanceId",
                        )
                        .and_then(
                            |value| {
                                value.as_str()
                            },
                        )
                        .unwrap_or(
                            &name,
                        )
                        .to_string();

                Some(
                    DeviceSnapshot {
                        id,
                        name,
                        class_name,
                        status,
                    },
                )
            },
        )
        .collect()
}

#[cfg(not(target_os = "windows"))]
fn read_windows_devices() -> Vec<DeviceSnapshot> {
    Vec::new()
}

fn get_devices_cached(
    state: &TelemetryState,
) -> Vec<DeviceSnapshot> {
    let mut cache =
        match state
            .devices_cache
            .lock()
        {
            Ok(cache) =>
                cache,

            Err(_) =>
                return Vec::new(),
        };

    let should_refresh =
        match cache.last_refresh {
            None =>
                true,

            Some(last) =>
                last.elapsed()
                    >=
                    Duration::from_secs(
                        8,
                    ),
        };

    if should_refresh {
        cache.devices =
            read_windows_devices();

        cache.last_refresh =
            Some(
                Instant::now(),
            );
    }

    cache.devices.clone()
}

#[tauri::command]
fn get_system_snapshot(
    state:
        tauri::State<
            '_,
            TelemetryState,
        >,
) -> Result<
    TelemetrySnapshot,
    String,
> {
    let (
        cpu_percent,
        cpu_brand,
        physical_cores,
        logical_cores,
        memory_percent,
        memory_used_bytes,
        memory_total_bytes,
    ) = {
        let mut system =
            state
                .system
                .lock()
                .map_err(
                    |_| {
                        "Telemetry system lock failed"
                            .to_string()
                    },
                )?;

        system.refresh_cpu_usage();
        system.refresh_memory();

        let cpu_percent =
            system
                .global_cpu_usage()
                .clamp(
                    0.0,
                    100.0,
                );

        let logical_cores =
            system.cpus().len();

        let physical_cores =
            System::physical_core_count()
                .unwrap_or(
                    logical_cores,
                );

        let cpu_brand =
            system
                .cpus()
                .first()
                .map(
                    |cpu| {
                        cpu
                            .brand()
                            .trim()
                            .to_string()
                    },
                )
                .filter(
                    |brand| {
                        !brand.is_empty()
                    },
                )
                .unwrap_or_else(
                    || {
                        "CPU".to_string()
                    },
                );

        let total =
            system.total_memory();

        let used =
            system.used_memory();

        let memory_percent =
            if total == 0 {
                0.0
            } else {
                (
                    used as f64
                    / total as f64
                    * 100.0
                ) as f32
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

    Ok(
        TelemetrySnapshot {
            cpu_percent,
            cpu_brand,
            physical_cores,
            logical_cores,

            memory_percent:
                memory_percent.clamp(
                    0.0,
                    100.0,
                ),

            memory_used_bytes,
            memory_total_bytes,

            temperature_c:
                read_temperature(),

            disks:
                read_disks(),

            devices:
                get_devices_cached(
                    &state,
                ),
        },
    )
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn open_storage_path(
    path: String,
) -> Result<(), String> {
    let clean_path =
        path.trim();

    if clean_path.is_empty() {
        return Err(
            "Storage path is empty."
                .to_string(),
        );
    }

    Command::new(
        "explorer.exe",
    )
    .arg(
        clean_path,
    )
    .spawn()
    .map_err(
        |error| {
            format!(
                "Failed to open storage path '{}': {}",
                clean_path,
                error
            )
        },
    )?;

    Ok(())
}

#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn open_storage_path(
    _path: String,
) -> Result<(), String> {
    Err(
        "Storage opening is currently supported on Windows only."
            .to_string(),
    )
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn open_device_properties(
    instance_id: String,
) -> Result<(), String> {
    let clean_id =
        instance_id.trim();

    if clean_id.is_empty() {
        return Err(
            "Device instance ID is empty."
                .to_string(),
        );
    }

    Command::new(
        "rundll32.exe",
    )
    .arg(
        "devmgr.dll,DeviceProperties_RunDLL",
    )
    .arg(
        "/DeviceID",
    )
    .arg(
        clean_id,
    )
    .spawn()
    .map_err(
        |error| {
            format!(
                "Failed to open device properties '{}': {}",
                clean_id,
                error
            )
        },
    )?;

    Ok(())
}

#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn open_device_properties(
    _instance_id: String,
) -> Result<(), String> {
    Err(
        "Device properties are currently supported on Windows only."
            .to_string(),
    )
}

#[cfg_attr(
    mobile,
    tauri::mobile_entry_point
)]
pub fn run() {
    tauri::Builder::default()
        .plugin(
            tauri_plugin_opener::init(),
        )
        .manage(
            TelemetryState::new(),
        )
        .invoke_handler(
            tauri::generate_handler![
                get_system_snapshot,
                open_storage_path,
                open_device_properties
            ],
        )
        .run(
            tauri::generate_context!(),
        )
        .expect(
            "error while running Qronos desktop application",
        );
}