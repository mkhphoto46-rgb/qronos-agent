//! Closed backend dispatch: installed builds use only the bundled executable.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;

#[cfg(debug_assertions)]
use std::process::Stdio;

const PROTOCOL_VERSION: u64 = 1;
#[cfg(target_os = "windows")]
const EXECUTABLE: &str = "qronos-runtime.exe";
#[cfg(not(target_os = "windows"))]
const EXECUTABLE: &str = "qronos-runtime";

#[derive(Debug, Clone, Copy)]
pub(super) enum Service {
    Bridge,
    /// The supervised runtime: `qronos-runtime supervisor --desktop`, which
    /// starts the bridge itself and relays its standard input and output.
    Supervisor,
    Library,
    Memory,
    Audio,
    Settings,
    Permissions,
    Doctor,
}

/// The product version every backend service is told (QRONOS_APP_VERSION): the
/// desktop's own package version, set once from tauri's package information
/// (tauri.conf.json). There is no second version constant.
static APP_VERSION: OnceLock<String> = OnceLock::new();

pub(super) fn set_app_version(version: String) {
    let _ = APP_VERSION.set(version);
}

impl Service {
    fn name(self) -> &'static str {
        match self {
            Self::Bridge => "bridge",
            Self::Supervisor => "supervisor",
            Self::Library => "library",
            Self::Memory => "memory",
            Self::Audio => "audio",
            Self::Settings => "settings",
            Self::Permissions => "permissions",
            Self::Doctor => "doctor",
        }
    }

    /// Fixed words after the service name; nothing here comes from a caller.
    fn fixed_arguments(self) -> &'static [&'static str] {
        match self {
            Self::Supervisor => &["--desktop"],
            _ => &[],
        }
    }

    #[cfg(debug_assertions)]
    fn module(self) -> &'static str {
        match self {
            Self::Bridge => "core.runtime_bridge",
            Self::Supervisor => "core.supervisor",
            Self::Library => "core.library_cli",
            Self::Memory => "core.memory_cli",
            Self::Audio => "core.audio_output_cli",
            Self::Settings => "core.settings_cli",
            Self::Permissions => "core.permissions_cli",
            Self::Doctor => "core.runtime_preflight",
        }
    }
}

#[derive(Debug, Clone)]
pub(super) enum Backend {
    Packaged {
        directory: PathBuf,
        data_root: PathBuf,
        asset_root: PathBuf,
    },
    #[cfg(debug_assertions)]
    Development { root: PathBuf },
}

impl Backend {
    pub(super) fn playback_root(&self) -> PathBuf {
        match self {
            Self::Packaged { data_root, .. } => data_root.join("Temp").join("Chatterbox"),
            #[cfg(debug_assertions)]
            Self::Development { root } => root.join("runtime").join("chatterbox").join("temp"),
        }
    }

    pub(super) fn command(&self, service: Service) -> Result<Command, String> {
        let mut command = match self {
            Self::Packaged {
                directory,
                data_root,
                asset_root,
            } => {
                let mut command = Command::new(directory.join(EXECUTABLE));
                command
                    .arg(service.name())
                    .args(service.fixed_arguments())
                    .current_dir(directory)
                    .env("QRONOS_PACKAGED_RUNTIME", "1")
                    .env("QRONOS_DATA_ROOT", data_root)
                    .env("QRONOS_ASSET_ROOT", asset_root);
                command
            }
            #[cfg(debug_assertions)]
            Self::Development { root } => {
                let mut command = Command::new(resolve_python(root)?);
                command
                    .args(["-u", "-m", service.module()])
                    .args(service.fixed_arguments())
                    .current_dir(root);
                command
            }
        };
        command.env("PYTHONUTF8", "1").env("PYTHONUNBUFFERED", "1");
        if let Some(version) = APP_VERSION.get() {
            // The runtime's status hooks report the running version (core/runtime_hooks.py).
            command.env("QRONOS_APP_VERSION", version);
        }
        Ok(command)
    }
}

fn packaged_directory(resource_directory: &Path) -> Result<Option<PathBuf>, String> {
    let directory = resource_directory.join("qronos-runtime");
    if !directory.exists() {
        return Ok(None);
    }
    let manifest = std::fs::read(directory.join("runtime-manifest.json"))
        .map_err(|_| "The bundled Qronos backend manifest is missing.".to_string())?;
    let manifest: serde_json::Value = serde_json::from_slice(&manifest)
        .map_err(|_| "The bundled Qronos backend manifest is invalid.".to_string())?;
    if manifest.get("protocolVersion").and_then(|v| v.as_u64()) != Some(PROTOCOL_VERSION)
        || manifest.get("executable").and_then(|v| v.as_str()) != Some(EXECUTABLE)
        || !directory.join(EXECUTABLE).is_file()
        || !directory.join("_internal").is_dir()
    {
        return Err(
            "The bundled Qronos backend is incomplete or uses an unsupported protocol.".to_string(),
        );
    }
    Ok(Some(directory))
}

const ASSET_ROOT_VARIABLE: &str = "QRONOS_ASSET_ROOT";
/// Longest persisted value read, in UTF-16 units (the Windows environment-variable limit).
#[cfg(windows)]
const MAX_PERSISTED_UNITS: usize = 32_767;

/// A non-blank value, trimmed like the Python side; None when absent or blank.
fn configured_value(value: Option<OsString>) -> Option<OsString> {
    let value = value?;
    match value.to_str() {
        Some(text) if text.trim().is_empty() => None,
        Some(text) => Some(OsString::from(text.trim())),
        None => Some(value),
    }
}

/// The asset root, in the same precedence as core/config.py resolve_asset_root: a non-blank process value, then the
/// persisted user value, then the default. A configured value must be absolute; it is refused, never replaced.
pub(super) fn resolve_asset_root(
    process: Option<OsString>,
    persisted: impl FnOnce() -> Option<OsString>,
    default: PathBuf,
) -> Result<PathBuf, String> {
    let (value, source) = match configured_value(process) {
        Some(value) => (value, "process"),
        None => match configured_value(persisted()) {
            Some(value) => (value, "user"),
            None => return Ok(default),
        },
    };
    let path = PathBuf::from(value);
    if !path.is_absolute() {
        return Err(format!("{ASSET_ROOT_VARIABLE} ({source} environment) must be an absolute path."));
    }
    Ok(path)
}

/// The current value of one variable in the user's persisted environment (HKCU\Environment), read only.
///
/// A desktop started from an Explorer session older than the variable does not inherit it. RegGetValueW with
/// RRF_RT_REG_SZ accepts REG_SZ and expands REG_EXPAND_SZ natively; any other type, an oversized value or any error
/// gives None.
#[cfg(windows)]
fn read_persisted_user_variable(name: &str) -> Option<OsString> {
    use std::os::windows::ffi::OsStringExt;
    use windows_sys::Win32::Foundation::ERROR_SUCCESS;
    use windows_sys::Win32::System::Registry::{RegGetValueW, HKEY_CURRENT_USER, RRF_RT_REG_SZ};

    let wide = |text: &str| text.encode_utf16().chain(std::iter::once(0)).collect::<Vec<u16>>();
    let subkey = wide("Environment");
    let value = wide(name);
    let mut bytes: u32 = 0;
    // SAFETY: both names are NUL-terminated UTF-16 buffers that outlive the call; a null data pointer asks only for
    // the size.
    let status = unsafe {
        RegGetValueW(HKEY_CURRENT_USER, subkey.as_ptr(), value.as_ptr(), RRF_RT_REG_SZ,
                     std::ptr::null_mut(), std::ptr::null_mut(), &mut bytes)
    };
    if status != ERROR_SUCCESS || bytes < 2 || bytes as usize > (MAX_PERSISTED_UNITS + 1) * 2 {
        return None;
    }
    let mut buffer = vec![0u16; (bytes as usize).div_ceil(2)];
    let mut size = (buffer.len() * 2) as u32;
    // SAFETY: the buffer holds `size` bytes and outlives the call.
    let status = unsafe {
        RegGetValueW(HKEY_CURRENT_USER, subkey.as_ptr(), value.as_ptr(), RRF_RT_REG_SZ,
                     std::ptr::null_mut(), buffer.as_mut_ptr().cast(), &mut size)
    };
    if status != ERROR_SUCCESS {
        return None;
    }
    let units = (size as usize / 2).min(buffer.len());
    let end = buffer[..units].iter().position(|&unit| unit == 0).unwrap_or(units);
    Some(OsString::from_wide(&buffer[..end]))
}

#[cfg(not(windows))]
fn read_persisted_user_variable(_name: &str) -> Option<OsString> {
    None
}

pub(super) fn discover(
    resource_directory: Option<&Path>,
    local_data_directory: Option<&Path>,
) -> Result<Option<Backend>, String> {
    discover_with(resource_directory, local_data_directory, std::env::var_os(ASSET_ROOT_VARIABLE),
                  || read_persisted_user_variable(ASSET_ROOT_VARIABLE))
}

fn discover_with(
    resource_directory: Option<&Path>,
    local_data_directory: Option<&Path>,
    process_asset_root: Option<OsString>,
    persisted_asset_root: impl FnOnce() -> Option<OsString>,
) -> Result<Option<Backend>, String> {
    if let Some(resources) = resource_directory {
        if let Some(directory) = packaged_directory(resources)? {
            let local = local_data_directory.ok_or_else(|| {
                "The local Qronos user-data directory is unavailable.".to_string()
            })?;
            let data_root = local.join("Qronos");
            let asset_root = resolve_asset_root(process_asset_root, persisted_asset_root,
                                                data_root.join("RuntimeAssets"))?;
            return Ok(Some(Backend::Packaged {
                directory,
                data_root,
                asset_root,
            }));
        }
    }
    #[cfg(debug_assertions)]
    if let Some(root) = find_project_root() {
        return Ok(Some(Backend::Development { root }));
    }
    Ok(None)
}

#[cfg(debug_assertions)]
fn find_project_root() -> Option<PathBuf> {
    fn is_checkout(candidate: &Path) -> bool {
        candidate.join("core").is_dir() && candidate.join("desktop").is_dir()
    }
    if let Ok(current) = std::env::current_dir() {
        if let Some(found) = current.ancestors().find(|c| is_checkout(c)) {
            return Some(found.to_path_buf());
        }
    }
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest
        .ancestors()
        .find(|c| is_checkout(c))
        .map(Path::to_path_buf)
}

#[cfg(debug_assertions)]
fn resolve_python(root: &Path) -> Result<PathBuf, String> {
    let mut candidates = vec![
        root.join(".venv/Scripts/python.exe"),
        root.join("venv/Scripts/python.exe"),
        root.join(".venv/bin/python"),
    ];
    if let Some(executable) = std::env::var_os("PYTHON") {
        if !executable.is_empty() {
            candidates.push(PathBuf::from(executable));
        }
    }
    candidates.extend([PathBuf::from("python"), PathBuf::from("py")]);
    for candidate in candidates {
        if candidate.is_absolute() {
            if candidate.is_file() {
                return Ok(candidate);
            }
        } else if python_version_works(&candidate) {
            return Ok(candidate);
        }
    }
    Err("No usable Python runtime was found for Qronos development mode.".to_string())
}

#[cfg(debug_assertions)]
fn python_version_works(candidate: &Path) -> bool {
    let mut command = Command::new(candidate);
    command
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(super::CREATE_NO_WINDOW);
    }
    let Ok(mut child) = command.spawn() else {
        return false;
    };
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) if std::time::Instant::now() < deadline => {
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return false;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "qronos-backend-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir_all(&root).unwrap();
            Self(root)
        }
        fn bundle(&self) -> PathBuf {
            let bundle = self.0.join("qronos-runtime");
            std::fs::create_dir_all(bundle.join("_internal")).unwrap();
            std::fs::write(bundle.join(EXECUTABLE), b"test executable").unwrap();
            std::fs::write(
                bundle.join("runtime-manifest.json"),
                serde_json::json!({"protocolVersion": 1, "executable": EXECUTABLE}).to_string(),
            )
            .unwrap();
            bundle
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn packaged_commands_use_only_bundled_executable_and_closed_service() {
        let fixture = Fixture::new();
        let directory = fixture.bundle();
        let backend = Backend::Packaged {
            directory: directory.clone(),
            data_root: fixture.0.join("profile"),
            asset_root: fixture.0.join("assets"),
        };
        let command = backend.command(Service::Settings).unwrap();
        assert_eq!(command.get_program(), directory.join(EXECUTABLE));
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            vec![std::ffi::OsStr::new("settings")]
        );
        assert_eq!(command.get_current_dir(), Some(directory.as_path()));
        assert_eq!(
            backend.playback_root(),
            fixture.0.join("profile/Temp/Chatterbox")
        );
        assert!(!fixture.0.join("profile").exists());
    }

    #[test]
    fn every_service_is_told_the_desktop_package_version() {
        let fixture = Fixture::new();
        let directory = fixture.bundle();
        let backend = Backend::Packaged {
            directory: directory.clone(),
            data_root: fixture.0.join("profile"),
            asset_root: fixture.0.join("assets"),
        };
        set_app_version("9.8.7".to_string());
        for service in [Service::Settings, Service::Supervisor] {
            let command = backend.command(service).unwrap();
            let version = command
                .get_envs()
                .find(|(key, _)| *key == std::ffi::OsStr::new("QRONOS_APP_VERSION"))
                .and_then(|(_, value)| value.map(|v| v.to_owned()));
            assert_eq!(version, Some(std::ffi::OsString::from(APP_VERSION.get().unwrap())));
        }
    }

    #[test]
    fn the_supervised_runtime_is_a_closed_bundled_command() {
        let fixture = Fixture::new();
        let directory = fixture.bundle();
        let backend = Backend::Packaged {
            directory: directory.clone(),
            data_root: fixture.0.join("profile"),
            asset_root: fixture.0.join("assets"),
        };
        let command = backend.command(Service::Supervisor).unwrap();
        assert_eq!(command.get_program(), directory.join(EXECUTABLE));
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            vec![std::ffi::OsStr::new("supervisor"), std::ffi::OsStr::new("--desktop")]
        );
        assert_eq!(
            backend.command(Service::Bridge).unwrap().get_args().collect::<Vec<_>>(),
            vec![std::ffi::OsStr::new("bridge")]
        );
    }

    #[test]
    fn invalid_bundle_is_an_error_instead_of_development_fallback() {
        let fixture = Fixture::new();
        let directory = fixture.bundle();
        std::fs::write(directory.join("runtime-manifest.json"), "{}").unwrap();
        assert!(discover(Some(&fixture.0), Some(&fixture.0)).is_err());
        std::fs::remove_file(directory.join(EXECUTABLE)).unwrap();
        assert!(packaged_directory(&fixture.0).is_err());
    }

    #[test]
    fn installed_runtime_does_not_need_source_python_or_working_directory() {
        let fixture = Fixture::new();
        fixture.bundle();
        assert!(matches!(
            discover(Some(&fixture.0), Some(&fixture.0)).unwrap(),
            Some(Backend::Packaged { .. })
        ));
    }

    // Asset-root discovery: every case injects both sources, so the real user registry is never read.
    #[test]
    fn a_stale_desktop_without_the_variable_uses_the_persisted_user_root_for_its_children() {
        let fixture = Fixture::new();
        fixture.bundle();
        let persisted = fixture.0.join("persisted assets");
        let backend = discover_with(Some(&fixture.0), Some(&fixture.0), None,
                                    || Some(persisted.clone().into_os_string())).unwrap().unwrap();
        let command = backend.command(Service::Bridge).unwrap();
        let passed = command.get_envs()
            .find(|(name, _)| *name == std::ffi::OsStr::new(ASSET_ROOT_VARIABLE))
            .and_then(|(_, value)| value.map(PathBuf::from));
        assert_eq!(passed, Some(persisted));
    }

    #[test]
    fn an_explicit_process_value_wins_over_the_persisted_one() {
        let process = std::env::temp_dir().join("qronos-process-assets");
        let root = resolve_asset_root(Some(process.clone().into_os_string()),
                                      || panic!("the persisted value must not be read"),
                                      PathBuf::from("default"));
        assert_eq!(root.unwrap(), process);
    }

    #[test]
    fn blank_values_keep_the_default() {
        let default = std::env::temp_dir().join("qronos-default-assets");
        for (process, persisted) in [(Some(""), None), (Some("   "), Some("")), (None, Some("  ")), (None, None)] {
            let root = resolve_asset_root(process.map(OsString::from), || persisted.map(OsString::from),
                                          default.clone());
            assert_eq!(root.unwrap(), default);
        }
    }

    #[test]
    fn a_relative_override_is_refused_not_replaced_by_the_default() {
        let default = std::env::temp_dir().join("qronos-default-assets");
        assert!(resolve_asset_root(Some(OsString::from("relative/assets")), || None, default.clone()).is_err());
        assert!(resolve_asset_root(None, || Some(OsString::from("relative/assets")), default).is_err());
        let fixture = Fixture::new();
        fixture.bundle();
        assert!(discover_with(Some(&fixture.0), Some(&fixture.0), None,
                              || Some(OsString::from("relative/assets"))).is_err());
    }

    #[test]
    fn missing_bundle_is_unavailable_in_release() {
        let fixture = Fixture::new();
        assert!(packaged_directory(&fixture.0).unwrap().is_none());
        #[cfg(not(debug_assertions))]
        assert!(discover(Some(&fixture.0), Some(&fixture.0))
            .unwrap()
            .is_none());
    }
}
