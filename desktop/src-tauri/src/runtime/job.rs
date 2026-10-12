//! The desktop-owned Job Object around the supervised runtime.
//!
//! The supervisor is created suspended, assigned to this Job and only then
//! resumed, so it and every process it (or the bridge, a sidecar, a media
//! worker) starts is a member from its first instruction. The supervisor's own
//! kill-on-close Job and the bridge's sidecar/media Jobs nest inside it.
//!
//! This Job never kills: it sets no limit (no kill-on-close, no breakaway), so an
//! ordinary app exit keeps the runtime's graceful shutdown. It is the PROOF of a
//! stop: the runtime has ended only when the exact supervisor has exited AND the
//! Job's active process count is zero. A response line, a frontend value or the
//! supervisor's exit alone is not that proof.

use std::process::{Child, Command};
use std::time::{Duration, Instant};

#[cfg(windows)]
pub(super) struct OwnedJob {
    handle: windows_sys::Win32::Foundation::HANDLE,
}

// The handle is only used through thread-safe kernel calls.
#[cfg(windows)]
unsafe impl Send for OwnedJob {}
#[cfg(windows)]
unsafe impl Sync for OwnedJob {}

#[cfg(windows)]
impl OwnedJob {
    /// An unnamed Job with no limits, owned by this process (handle not inheritable).
    pub(super) fn new() -> std::io::Result<Self> {
        use windows_sys::Win32::System::JobObjects::CreateJobObjectW;
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(std::io::Error::last_os_error());
        }
        Ok(Self { handle })
    }

    /// Processes in this Job (nested Jobs included) that have not ended.
    pub(super) fn active_processes(&self) -> std::io::Result<u32> {
        use windows_sys::Win32::System::JobObjects::{
            JobObjectBasicAccountingInformation, QueryInformationJobObject,
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
        };
        let mut info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
        let ok = unsafe {
            QueryInformationJobObject(
                self.handle,
                JobObjectBasicAccountingInformation,
                (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                std::mem::size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                std::ptr::null_mut(),
            )
        };
        if ok == 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(info.ActiveProcesses)
    }

    /// Start `command` suspended, make it a member, then let it run.
    ///
    /// If it cannot be made a member or resumed, the still-suspended process (it
    /// never executed) is ended through its own handle and reaped.
    pub(super) fn spawn(&self, command: &mut Command, flags: u32) -> std::io::Result<Child> {
        use std::os::windows::io::AsRawHandle;
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::System::JobObjects::AssignProcessToJobObject;
        use windows_sys::Win32::System::Threading::CREATE_SUSPENDED;

        let mut child = command.creation_flags(flags | CREATE_SUSPENDED).spawn()?;
        let joined = unsafe { AssignProcessToJobObject(self.handle, child.as_raw_handle() as _) };
        let started = if joined == 0 {
            Err(std::io::Error::last_os_error())
        } else {
            resume_only_thread(child.id())
        };
        if let Err(error) = started {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
        Ok(child)
    }
}

#[cfg(windows)]
impl Drop for OwnedJob {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.handle) };
    }
}

/// A process created suspended has exactly one thread; resume exactly that one.
#[cfg(windows)]
fn resume_only_thread(pid: u32) -> std::io::Result<()> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
    };
    use windows_sys::Win32::System::Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME};

    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err(std::io::Error::last_os_error());
    }
    let mut threads = Vec::new();
    let mut entry = THREADENTRY32 {
        dwSize: std::mem::size_of::<THREADENTRY32>() as u32,
        ..Default::default()
    };
    let mut more = unsafe { Thread32First(snapshot, &mut entry) } != 0;
    while more {
        if entry.th32OwnerProcessID == pid {
            threads.push(entry.th32ThreadID);
        }
        more = unsafe { Thread32Next(snapshot, &mut entry) } != 0;
    }
    unsafe { CloseHandle(snapshot) };
    let [thread] = threads[..] else {
        return Err(std::io::Error::other(
            "The new runtime process did not have exactly one suspended thread.",
        ));
    };
    let handle = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, thread) };
    if handle.is_null() {
        return Err(std::io::Error::last_os_error());
    }
    let previous = unsafe { ResumeThread(handle) };
    unsafe { CloseHandle(handle) };
    if previous == u32::MAX {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

/// Off Windows there is no Job: the proof is the exact child's exit alone.
#[cfg(not(windows))]
pub(super) struct OwnedJob;

#[cfg(not(windows))]
impl OwnedJob {
    pub(super) fn new() -> std::io::Result<Self> {
        Ok(Self)
    }
    pub(super) fn active_processes(&self) -> std::io::Result<u32> {
        Ok(0)
    }
    pub(super) fn spawn(&self, command: &mut Command, _flags: u32) -> std::io::Result<Child> {
        command.spawn()
    }
}

impl OwnedJob {
    /// True once no process of this Job is alive, polled until `bound`. An
    /// unanswerable query is never taken as empty.
    pub(super) fn wait_empty(&self, bound: Duration) -> Result<(), String> {
        let deadline = Instant::now() + bound;
        loop {
            match self.active_processes() {
                Ok(0) => return Ok(()),
                Ok(count) if Instant::now() >= deadline => {
                    return Err(format!("{count} runtime process(es) are still running."))
                }
                Ok(_) => std::thread::sleep(Duration::from_millis(25)),
                Err(error) => {
                    return Err(format!(
                        "Whether the runtime processes ended is unknown: {error}"
                    ))
                }
            }
        }
    }
}

#[cfg(all(test, windows))]
pub(super) mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    pub(crate) struct TreeFixture {
        pub(crate) directory: PathBuf,
    }
    impl TreeFixture {
        pub(crate) fn new() -> Self {
            let directory = std::env::temp_dir().join(format!(
                "qronos-job-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir_all(&directory).unwrap();
            Self { directory }
        }
        pub(crate) fn command(&self, mode: &str) -> Command {
            let python = std::env::var_os("QRONOS_TEST_PYTHON").unwrap_or_else(|| "python".into());
            let mut command = Command::new(python);
            command
                .arg("-B")
                .arg(
                    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                        .join("tests/fixtures/lifecycle_child.py"),
                )
                .arg(mode)
                .arg(&self.directory);
            command
        }
        pub(crate) fn wait_for(&self, name: &str) {
            let deadline = Instant::now() + Duration::from_secs(10);
            while !self.directory.join(name).exists() {
                assert!(Instant::now() < deadline, "fixture never wrote {name}");
                std::thread::sleep(Duration::from_millis(10));
            }
        }
        pub(crate) fn release(&self) {
            std::fs::write(self.directory.join("go"), "go").unwrap();
        }
    }
    impl Drop for TreeFixture {
        fn drop(&mut self) {
            // Every fixture process ends on "go" within its own bound; nothing is killed by name.
            let _ = std::fs::write(self.directory.join("go"), "go");
            std::thread::sleep(Duration::from_millis(300));
            let _ = std::fs::remove_dir_all(&self.directory);
        }
    }

    #[test]
    fn a_grandchild_started_at_once_is_a_member_and_the_job_proves_only_when_all_ended() {
        let fixture = TreeFixture::new();
        let job = OwnedJob::new().unwrap();
        let mut child = job
            .spawn(&mut fixture.command("tree-immediate"), 0x0800_0000)
            .unwrap();
        fixture.wait_for("grandchild.pid");
        assert!(job.active_processes().unwrap() >= 2);
        // The exact child ends; the grandchild it started first thing is still a member.
        child.kill().unwrap();
        child.wait().unwrap();
        let unproved = job.wait_empty(Duration::from_millis(300));
        assert!(
            unproved.is_err(),
            "a live grandchild must keep the stop unproved"
        );
        fixture.release();
        job.wait_empty(Duration::from_secs(10))
            .expect("all members ended");
    }

    #[test]
    fn a_job_with_no_member_is_empty_and_never_kills_anything_on_drop() {
        let fixture = TreeFixture::new();
        let job = OwnedJob::new().unwrap();
        let mut child = job
            .spawn(&mut fixture.command("wait-go"), 0x0800_0000)
            .unwrap();
        fixture.wait_for("child.pid");
        drop(job); // no kill-on-close: the member keeps running
        std::thread::sleep(Duration::from_millis(200));
        assert!(
            child.try_wait().unwrap().is_none(),
            "dropping the Job must not end its members"
        );
        fixture.release();
        child.wait().unwrap();
        assert!(OwnedJob::new().unwrap().wait_empty(Duration::ZERO).is_ok());
    }
}
