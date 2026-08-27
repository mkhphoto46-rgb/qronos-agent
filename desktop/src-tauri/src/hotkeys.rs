use serde::{Deserialize, Serialize};
use std::{fs, path::PathBuf, sync::Mutex};
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

const SETTINGS_FILE: &str = "hotkeys.json";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct HotkeyBinding {
    pub action_id: String,
    pub title: String,
    pub english: String,
    pub description: String,
    pub accelerator: Option<String>,
    pub default_accelerator: Option<String>,
    pub scope: String,
    pub enabled: bool,
    pub status: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct HotkeySettings {
    pub schema_version: u32,
    pub bindings: Vec<HotkeyBinding>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct HotkeyEvent {
    action_id: String,
}

pub struct HotkeyManagerState {
    settings: Mutex<HotkeySettings>,
    settings_path: PathBuf,
}

fn binding(
    action_id: &str,
    title: &str,
    english: &str,
    description: &str,
    accelerator: Option<&str>,
    scope: &str,
) -> HotkeyBinding {
    HotkeyBinding {
        action_id: action_id.to_string(),
        title: title.to_string(),
        english: english.to_string(),
        description: description.to_string(),
        accelerator: accelerator.map(str::to_string),
        default_accelerator: accelerator.map(str::to_string),
        scope: scope.to_string(),
        enabled: true,
        status: if accelerator.is_some() { "ACTIVE" } else { "UNASSIGNED" }.to_string(),
    }
}

fn defaults() -> HotkeySettings {
    HotkeySettings {
        schema_version: 1,
        bindings: vec![
            binding("qronos.toggle_window", "نمایش یا مخفی‌کردن Qronos", "Toggle Qronos Window", "پنجره Qronos را در هر برنامه‌ای نمایش دهید یا مخفی کنید.", Some("Ctrl+Shift+Q"), "global"),
            binding("qronos.push_to_talk", "Push to Talk", "Push to Talk", "بدون Wake Word فرمان صوتی را شروع کنید.", Some("Ctrl+Alt+Space"), "global"),
            binding("qronos.stop_response", "توقف پاسخ", "Stop Response", "پردازش یا پاسخ فعلی Qronos را متوقف کنید.", Some("Ctrl+Alt+X"), "global"),
            binding("qronos.toggle_voice", "قطع یا وصل صدای Qronos", "Toggle Qronos Voice", "خروجی صوتی Qronos را قطع یا دوباره فعال کنید.", None, "global"),
            binding("qronos.focus_command", "تمرکز روی فرمان", "Focus Command Input", "نشانگر را به ورودی فرمان منتقل کنید.", Some("Ctrl+L"), "inApp"),
            binding("navigation.home", "خانه", "Home", "به نمای اصلی Qronos بروید.", Some("Alt+1"), "inApp"),
            binding("navigation.conversations", "گفتگوها", "Conversations", "بخش گفتگوها را باز کنید.", Some("Alt+2"), "inApp"),
            binding("navigation.library", "کتابخانه", "Library", "کتابخانه را باز کنید.", Some("Alt+3"), "inApp"),
            binding("navigation.permissions", "مجوزها", "Permissions", "بخش مجوزها را باز کنید.", Some("Alt+4"), "inApp"),
            binding("navigation.settings", "تنظیمات", "Settings", "بخش تنظیمات را باز کنید.", Some("Alt+5"), "inApp"),
        ],
    }
}

fn save_settings(path: &PathBuf, settings: &HotkeySettings) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    let temporary = path.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(settings).map_err(|error| error.to_string())?;
    fs::write(&temporary, bytes).map_err(|error| error.to_string())?;
    if path.exists() {
        fs::remove_file(path).map_err(|error| error.to_string())?;
    }
    fs::rename(temporary, path).map_err(|error| error.to_string())
}

fn load_settings(path: &PathBuf) -> HotkeySettings {
    fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<HotkeySettings>(&bytes).ok())
        .filter(|settings| settings.schema_version == 1)
        .unwrap_or_else(defaults)
}

fn normalized(value: &str) -> String {
    value.chars().filter(|character| !character.is_whitespace()).collect::<String>().to_ascii_lowercase()
}

fn is_reserved(value: &str) -> bool {
    matches!(normalized(value).as_str(), "alt+f4" | "ctrl+alt+delete" | "ctrl+shift+escape" | "meta+l" | "super+l")
}

fn validate_candidate(settings: &HotkeySettings, action_id: &str, accelerator: &str) -> Result<(), String> {
    if accelerator.trim().is_empty() {
        return Err("میانبر نمی‌تواند خالی باشد.".to_string());
    }
    accelerator.parse::<Shortcut>().map_err(|_| "ترکیب کلید معتبر نیست.".to_string())?;
    if is_reserved(accelerator) {
        return Err("این ترکیب توسط سیستم رزرو شده است.".to_string());
    }
    if settings.bindings.iter().any(|item| {
        item.action_id != action_id
            && item.enabled
            && item.accelerator.as_deref().map(normalized).as_deref() == Some(normalized(accelerator).as_str())
    }) {
        return Err("این میانبر قبلاً برای عملکرد دیگری استفاده شده است.".to_string());
    }
    Ok(())
}

fn register_accelerator(app: &AppHandle, accelerator: &str) -> Result<(), String> {
    let shortcut = accelerator.parse::<Shortcut>().map_err(|_| "ترکیب کلید معتبر نیست.".to_string())?;
    app.global_shortcut().register(shortcut).map_err(|error| error.to_string())
}

fn unregister_accelerator(app: &AppHandle, accelerator: &str) {
    if let Ok(shortcut) = accelerator.parse::<Shortcut>() {
        let _ = app.global_shortcut().unregister(shortcut);
    }
}

fn register_globals(app: &AppHandle, settings: &mut HotkeySettings) {
    for item in settings.bindings.iter_mut().filter(|item| item.scope == "global") {
        if !item.enabled {
            item.status = "DISABLED".to_string();
            continue;
        }
        let Some(accelerator) = item.accelerator.as_deref() else {
            item.status = "UNASSIGNED".to_string();
            continue;
        };
        item.status = match register_accelerator(app, accelerator) {
            Ok(_) => "ACTIVE".to_string(),
            Err(_) => "CONFLICT".to_string(),
        };
    }
}

pub fn initialize(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let settings_path = app.path().app_config_dir()?.join(SETTINGS_FILE);
    let mut settings = load_settings(&settings_path);
    register_globals(app.handle(), &mut settings);
    let _ = save_settings(&settings_path, &settings);
    app.manage(HotkeyManagerState { settings: Mutex::new(settings), settings_path });
    Ok(())
}

pub fn handle_global_shortcut(app: &AppHandle, shortcut: &Shortcut, event: tauri_plugin_global_shortcut::ShortcutEvent) {
    if event.state() != ShortcutState::Pressed {
        return;
    }
    let action_id = app.try_state::<HotkeyManagerState>().and_then(|state| {
        state.settings.lock().ok().and_then(|settings| {
            settings.bindings.iter().find(|item| {
                item.scope == "global" && item.enabled && item.accelerator.as_deref().and_then(|value| value.parse::<Shortcut>().ok()).as_ref() == Some(shortcut)
            }).map(|item| item.action_id.clone())
        })
    });
    let Some(action_id) = action_id else { return; };
    if action_id == "qronos.toggle_window" {
        if let Some(window) = app.get_webview_window("main") {
            let visible = window.is_visible().unwrap_or(false);
            if visible { let _ = window.hide(); } else { let _ = window.show(); let _ = window.set_focus(); }
        }
    }
    let _ = app.emit("qronos://hotkey", HotkeyEvent { action_id });
}

#[tauri::command]
pub fn get_hotkey_settings(state: State<'_, HotkeyManagerState>) -> Result<HotkeySettings, String> {
    state.settings.lock().map(|settings| settings.clone()).map_err(|_| "Hotkey settings lock failed.".to_string())
}

#[tauri::command(rename_all = "camelCase")]
pub fn validate_hotkey(action_id: String, accelerator: String, state: State<'_, HotkeyManagerState>) -> Result<(), String> {
    let settings = state.settings.lock().map_err(|_| "Hotkey settings lock failed.".to_string())?;
    validate_candidate(&settings, &action_id, &accelerator)
}

#[tauri::command(rename_all = "camelCase")]
pub fn update_hotkey(action_id: String, accelerator: Option<String>, app: AppHandle, state: State<'_, HotkeyManagerState>) -> Result<HotkeySettings, String> {
    let mut settings = state.settings.lock().map_err(|_| "Hotkey settings lock failed.".to_string())?;
    if let Some(value) = accelerator.as_deref() { validate_candidate(&settings, &action_id, value)?; }
    let index = settings.bindings.iter().position(|item| item.action_id == action_id).ok_or_else(|| "Unknown hotkey action.".to_string())?;
    let previous = settings.bindings[index].clone();
    if previous.scope == "global" && previous.enabled {
        if let Some(value) = previous.accelerator.as_deref() { unregister_accelerator(&app, value); }
    }
    settings.bindings[index].accelerator = accelerator.filter(|value| !value.trim().is_empty());
    settings.bindings[index].status = if settings.bindings[index].accelerator.is_some() { "ACTIVE" } else { "UNASSIGNED" }.to_string();
    if settings.bindings[index].scope == "global" && settings.bindings[index].enabled {
        if let Some(value) = settings.bindings[index].accelerator.as_deref() {
            if let Err(error) = register_accelerator(&app, value) {
                settings.bindings[index] = previous.clone();
                if let Some(old) = previous.accelerator.as_deref() { let _ = register_accelerator(&app, old); }
                return Err(format!("این میانبر در Windows قابل ثبت نیست: {error}"));
            }
        }
    }
    save_settings(&state.settings_path, &settings)?;
    let result = settings.clone();
    let _ = app.emit("qronos://hotkeys-updated", &result);
    Ok(result)
}

#[tauri::command(rename_all = "camelCase")]
pub fn set_hotkey_enabled(action_id: String, enabled: bool, app: AppHandle, state: State<'_, HotkeyManagerState>) -> Result<HotkeySettings, String> {
    let mut settings = state.settings.lock().map_err(|_| "Hotkey settings lock failed.".to_string())?;
    let index = settings.bindings.iter().position(|item| item.action_id == action_id).ok_or_else(|| "Unknown hotkey action.".to_string())?;
    let current = settings.bindings[index].clone();
    if current.scope == "global" {
        if enabled {
            if let Some(value) = current.accelerator.as_deref() { register_accelerator(&app, value).map_err(|error| format!("این میانبر در Windows قابل ثبت نیست: {error}"))?; }
        } else if let Some(value) = current.accelerator.as_deref() { unregister_accelerator(&app, value); }
    }
    settings.bindings[index].enabled = enabled;
    settings.bindings[index].status = if !enabled { "DISABLED" } else if current.accelerator.is_none() { "UNASSIGNED" } else { "ACTIVE" }.to_string();
    save_settings(&state.settings_path, &settings)?;
    let result = settings.clone();
    let _ = app.emit("qronos://hotkeys-updated", &result);
    Ok(result)
}

#[tauri::command]
pub fn reset_hotkeys(app: AppHandle, state: State<'_, HotkeyManagerState>) -> Result<HotkeySettings, String> {
    let mut settings = state.settings.lock().map_err(|_| "Hotkey settings lock failed.".to_string())?;
    app.global_shortcut().unregister_all().map_err(|error| error.to_string())?;
    let mut next = defaults();
    register_globals(&app, &mut next);
    save_settings(&state.settings_path, &next)?;
    *settings = next.clone();
    let _ = app.emit("qronos://hotkeys-updated", &next);
    Ok(next)
}
