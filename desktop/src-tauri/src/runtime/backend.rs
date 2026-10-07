//! Closed backend dispatch: installed builds use only the bundled executable.

use std::path::{Path, PathBuf};
use std::process::Command;

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
    Library,
    Memory,
    Audio,
    Settings,
    Permissions,
    Doctor,
}

impl Service {
    fn name(self) -> &'static str {
        match self {
            Self::Bridge => "bridge",
            Self::Library => "library",
            Self::Memory => "memory",
            Self::Audio => "audio",
            Self::Settings => "settings",
            Self::Permissions => "permissions",
            Self::Doctor => "doctor",
        }
    }

    #[cfg(debug_assertions)]
    fn module(self) -> &'static str {
        match self {
            Self::Bridge => "core.runtime_bridge",
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
                    .current_dir(root);
                command
            }
        };
        command.env("PYTHONUTF8", "1").env("PYTHONUNBUFFERED", "1");
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

pub(super) fn discover(
    resource_directory: Option<&Path>,
    local_data_directory: Option<&Path>,
) -> Result<Option<Backend>, String> {
    if let Some(resources) = resource_directory {
        if let Some(directory) = packaged_directory(resources)? {
            let local = local_data_directory.ok_or_else(|| {
                "The local Qronos user-data directory is unavailable.".to_string()
            })?;
            let data_root = local.join("Qronos");
            let asset_root = match std::env::var_os("QRONOS_ASSET_ROOT") {
                Some(value) if !value.is_empty() => {
                    let path = PathBuf::from(value);
                    if !path.is_absolute() {
                        return Err("QRONOS_ASSET_ROOT must be an absolute path.".to_string());
                    }
                    path
                }
                _ => data_root.join("RuntimeAssets"),
            };
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
