//! The desktop's half of the update launch (Control C1-C4).
//!
//! `update_command("launch")` is the only way in, and only this module runs
//! the update service's `launch`:
//!
//! 1. The phase leaves `Idle` (a second launch is refused). Idle arms nothing:
//!    a launch starts only from an explicit request carrying the backend's
//!    single-use launch token, and the backend itself refuses
//!    (`launch_unavailable`) until its one enablement point is switched on.
//! 2. The service gate closes: every new backend service request, runtime start
//!    and native playback is refused at once, before any process starts. Requests
//!    already running keep their ticket until their child is reaped; the
//!    lifecycle waits for them (bounded). A drain that does not finish refuses
//!    the launch with the runtime untouched.
//! 3. Preflight: the read-only update status must say the launch is available,
//!    an approved launch is ready, no update is in progress and no recovery is
//!    pending. Otherwise the runtime is left alone.
//! 4. The runtime is stopped and the result is derived from the desktop-owned
//!    Job (`job.rs`): the exact supervisor exited and no process of the Job is
//!    alive. An unproved stop launches nothing; the busy ownership is retained.
//! 5. The update service is started with `launch`, never killed, never in a Job,
//!    and exactly one answer line is read with a bound.
//! 6. Only a typed refusal that proves no installer started brings the runtime
//!    back, and only if it was running. A launch, an unknown outcome or any
//!    transport failure ends in `ExitRequired`: the gate stays closed, nothing
//!    is started again and the app exits.

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Command, Stdio};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

/// The round-5 C3 list: refusals raised before the update service records a
/// start (`launch_failed` only when Windows created no installer process).
pub(super) const PROVED_NO_START: &[&str] = &[
    "launch_unavailable",
    "runtime_running",
    "update_in_progress",
    "writers_active",
    "recovery_pending",
    "not_approved",
    "approval_expired",
    "permission_denied",
    "not_staged",
    "package_mismatch",
    "launch_failed",
];

pub(super) const UPDATE_STARTING: &str =
    "An update is being started; this request was refused and nothing ran.";

const MAX_ANSWER_BYTES: u64 = 64 * 1024;

// ------------------------------------------------------------------ the gate

#[derive(Default)]
struct GateState {
    closed: bool,
    active: usize,
}

/// Admission for every backend service process the desktop starts.
#[derive(Default)]
pub(super) struct ServiceGate {
    state: Mutex<GateState>,
    changed: Condvar,
}

/// Held by one backend request until its child is reaped.
pub(super) struct Ticket(Arc<ServiceGate>);

impl Drop for Ticket {
    fn drop(&mut self) {
        let mut state = self
            .0
            .state
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        state.active -= 1;
        self.0.changed.notify_all();
    }
}

impl ServiceGate {
    pub(super) fn enter(self: &Arc<Self>) -> Result<Ticket, String> {
        let mut state = self.state.lock().map_err(|_| UPDATE_STARTING.to_string())?;
        if state.closed {
            return Err(UPDATE_STARTING.to_string());
        }
        state.active += 1;
        Ok(Ticket(self.clone()))
    }

    pub(super) fn is_closed(&self) -> bool {
        self.state.lock().map(|state| state.closed).unwrap_or(true)
    }

    /// Close (atomically with every `enter`), then wait for running requests.
    /// On timeout the gate stays closed; the caller decides whether to reopen.
    fn close_and_drain(&self, bound: Duration) -> Result<(), usize> {
        let deadline = Instant::now() + bound;
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        state.closed = true;
        while state.active > 0 {
            let now = Instant::now();
            if now >= deadline {
                return Err(state.active);
            }
            state = self
                .changed
                .wait_timeout(state, deadline - now)
                .unwrap_or_else(|poison| poison.into_inner())
                .0;
        }
        Ok(())
    }

    fn reopen(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        state.closed = false;
    }
}

// ------------------------------------------------------------------ the phase

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Phase {
    /// Nothing armed; the default at every app start.
    Idle,
    Launching,
    /// A launch happened or its outcome is unknown: this app process starts
    /// nothing again and exits.
    ExitRequired,
}

// ------------------------------------------------------------------ the launch line

/// What the one launch answer said, or why there is none.
#[derive(Debug)]
pub(super) enum LaunchLine {
    /// Windows refused to create the update service process: nothing of it ran.
    NotStarted(String),
    Answer(Value),
    /// No usable line (timeout, end of output, oversized, not JSON). The
    /// process, if alive, is left alone.
    NoAnswer(&'static str),
}

/// Start `command` with `launch`, send the token on stdin, read ONE line.
///
/// The process is never killed, waited for or put in a Job; dropping its
/// handle does not end it. After `bound` without a line it is left running.
pub(super) fn launch_one_line(mut command: Command, token: &str, bound: Duration) -> LaunchLine {
    command
        .arg("launch")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(super::CREATE_NO_WINDOW);
    }
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => return LaunchLine::NotStarted(error.to_string()),
    };
    if let Some(mut stdin) = child.stdin.take() {
        // Far below one pipe buffer, so this never waits on the reader.
        let _ = stdin.write_all(json!({ "launchToken": token }).to_string().as_bytes());
    }
    let Some(stdout) = child.stdout.take() else {
        return LaunchLine::NoAnswer("no output pipe");
    };
    drop(child); // never killed: the handle closes, the process keeps running
    let (send, receive) = std::sync::mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut line = Vec::new();
        let read = BufReader::new(stdout.take(MAX_ANSWER_BYTES + 1)).read_until(b'\n', &mut line);
        let _ = send.send(read.map(|_| line));
    });
    let line = match receive.recv_timeout(bound) {
        Ok(Ok(line)) => line,
        Ok(Err(_)) => return LaunchLine::NoAnswer("output pipe failed"),
        Err(_) => return LaunchLine::NoAnswer("no answer in time"),
    };
    if line.last() != Some(&b'\n') {
        return LaunchLine::NoAnswer(if line.len() as u64 > MAX_ANSWER_BYTES {
            "answer too large"
        } else {
            "output ended without an answer"
        });
    }
    match serde_json::from_slice::<Value>(&line) {
        Ok(value) if value.get("ok").and_then(Value::as_bool).is_some() => {
            LaunchLine::Answer(value)
        }
        _ => LaunchLine::NoAnswer("answer is not understood"),
    }
}

#[derive(Debug, PartialEq)]
pub(super) enum Classified {
    Launched(Value),
    NotStarted { code: String, message: String },
    Unknown(String),
}

pub(super) fn classify(line: LaunchLine) -> Classified {
    match line {
        LaunchLine::NotStarted(error) => Classified::NotStarted {
            code: "backend_not_started".into(),
            message: format!("The update service could not be started ({error}); nothing ran."),
        },
        LaunchLine::NoAnswer(why) => Classified::Unknown(why.into()),
        LaunchLine::Answer(value) => {
            let ok = value.get("ok").and_then(Value::as_bool) == Some(true);
            let handoff = value.get("handoff").filter(|handoff| handoff.is_object());
            let code = value.get("code").and_then(Value::as_str).unwrap_or("");
            match (ok, handoff) {
                (true, Some(handoff)) => Classified::Launched(handoff.clone()),
                (false, _) if PROVED_NO_START.contains(&code) => Classified::NotStarted {
                    code: code.into(),
                    message: value
                        .get("error")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .chars()
                        .take(400)
                        .collect(),
                },
                (false, _) if !code.is_empty() => Classified::Unknown(format!("refusal {code}")),
                _ => Classified::Unknown("answer is not understood".into()),
            }
        }
    }
}

// ------------------------------------------------------------------ orchestration

/// What the lifecycle needs from the app; tests supply fakes.
pub(super) trait LaunchHost {
    /// Read-only update status (a backend one-shot run inside the closed gate).
    fn update_status(&self) -> Result<Value, String>;
    /// End native playback through its existing stop (its own exact child).
    fn stop_playback(&self);
    /// Stop the runtime. Ok(was_running) only with the Job proof; Err(why) otherwise.
    fn stop_runtime(&self) -> Result<bool, String>;
    fn launch(&self, token: &str) -> LaunchLine;
    fn restart_runtime(&self) -> Result<(), String>;
}

pub(super) struct Lifecycle {
    pub(super) gate: Arc<ServiceGate>,
    phase: Mutex<Phase>,
    drain_bound: Duration,
}

fn refusal(code: &str, message: &str, runtime_restarted: bool) -> Value {
    json!({ "ok": false, "outcome": "not_started", "code": code, "error": message,
            "runtimeRestarted": runtime_restarted })
}

impl Lifecycle {
    pub(super) fn new(drain_bound: Duration) -> Self {
        Self {
            gate: Arc::new(ServiceGate::default()),
            phase: Mutex::new(Phase::Idle),
            drain_bound,
        }
    }

    pub(super) fn phase(&self) -> Phase {
        self.phase
            .lock()
            .map(|phase| *phase)
            .unwrap_or(Phase::ExitRequired)
    }

    /// Starting a runtime is allowed only while Idle and the gate is open.
    pub(super) fn runtime_start_allowed(&self) -> Result<(), String> {
        match self.phase() {
            Phase::ExitRequired => Err(
                "An update was handed to Windows or its outcome is unknown; Qronos closes and starts nothing."
                    .into(),
            ),
            _ if self.gate.is_closed() => Err(UPDATE_STARTING.into()),
            _ => Ok(()),
        }
    }

    fn set(&self, phase: Phase) {
        let mut current = self
            .phase
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        *current = phase;
    }

    fn back_to_idle(&self) {
        self.set(Phase::Idle);
        self.gate.reopen();
    }

    pub(super) fn launch(&self, host: &dyn LaunchHost, token: &str) -> Value {
        {
            let mut phase = self
                .phase
                .lock()
                .unwrap_or_else(|poison| poison.into_inner());
            match *phase {
                Phase::Idle => *phase = Phase::Launching,
                Phase::Launching => {
                    return refusal(
                        "update_busy",
                        "An update launch is already in progress.",
                        false,
                    )
                }
                Phase::ExitRequired => {
                    return refusal("exit_required", "Qronos is closing for an update.", false)
                }
            }
        }
        if !is_token(token) {
            self.set(Phase::Idle);
            return refusal(
                "not_approved",
                "The launch token is not valid; nothing was started.",
                false,
            );
        }
        // Playback opens no store, but no service process may outlive the close.
        if let Err(running) = {
            let drained = self.gate.close_and_drain(Duration::ZERO);
            host.stop_playback();
            drained.or_else(|_| self.gate.close_and_drain(self.drain_bound))
        } {
            self.back_to_idle();
            return refusal(
                "desktop_busy",
                &format!("{running} Qronos request(s) did not finish in time; nothing was stopped or started."),
                false,
            );
        }
        if let Some((code, message)) = preflight(host.update_status()) {
            self.back_to_idle();
            return refusal(code, &message, false);
        }
        let was_running = match host.stop_runtime() {
            Ok(was_running) => was_running,
            Err(why) => {
                // Not proved: launch nothing, restart nothing; the runtime keeps its busy ownership.
                self.back_to_idle();
                return refusal(
                    "runtime_stop_unproved",
                    &format!("The runtime did not provably stop: {why}"),
                    false,
                );
            }
        };
        match classify(host.launch(token)) {
            Classified::NotStarted { code, message } => {
                self.back_to_idle();
                let restarted = was_running && host.restart_runtime().is_ok();
                refusal(&code, &message, restarted)
            }
            Classified::Launched(handoff) => {
                self.set(Phase::ExitRequired);
                json!({ "ok": true, "outcome": "launched", "handoff": handoff, "exiting": true })
            }
            Classified::Unknown(why) => {
                self.set(Phase::ExitRequired);
                json!({ "ok": false, "outcome": "unknown", "code": "launch_outcome_unknown",
                        "error": format!("Whether the installer started is unknown ({why})."),
                        "runtimeRestarted": false, "exiting": true })
            }
        }
    }
}

fn is_token(token: &str) -> bool {
    token.len() == 43
        && token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

/// None when a launch may be tried; otherwise the typed refusal (runtime untouched).
fn preflight(status: Result<Value, String>) -> Option<(&'static str, String)> {
    let status = match status {
        Ok(value) => value,
        Err(error) => return Some(("status_unavailable", error)),
    };
    let status = status.get("status").unwrap_or(&Value::Null);
    let flag = |name: &str| status.get(name).and_then(Value::as_bool);
    if flag("updateInProgress") != Some(false) {
        return Some((
            "update_in_progress",
            "An update is already in progress.".into(),
        ));
    }
    if flag("recoveryPending") != Some(false) {
        return Some((
            "recovery_pending",
            "An interrupted update must be recovered first.".into(),
        ));
    }
    if flag("launchAvailable") != Some(true) {
        return Some((
            "launch_unavailable",
            "Starting an update installer is not enabled.".into(),
        ));
    }
    if !status.get("launchReady").is_some_and(Value::is_object) {
        return Some(("not_approved", "No approved launch is ready.".into()));
    }
    None
}

/// Where an update operation goes: the lifecycle for `launch`, one-shot IPC otherwise.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum UpdateRoute {
    OneShot,
    Lifecycle,
}

pub(super) fn route_update(operation: &str) -> UpdateRoute {
    if operation.trim().eq_ignore_ascii_case("launch") {
        UpdateRoute::Lifecycle
    } else {
        UpdateRoute::OneShot
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};

    const TOKEN: &str = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abcde";

    struct FakeHost {
        status: Value,
        running: bool,
        stop: Result<(), String>,
        line: Mutex<Option<LaunchLine>>,
        calls: Mutex<Vec<&'static str>>,
        launched_token: Mutex<Option<String>>,
    }

    impl FakeHost {
        fn new(line: LaunchLine) -> Self {
            Self {
                status: json!({"ok": true, "status": {"launchAvailable": true, "updateInProgress": false,
                               "recoveryPending": false, "launchReady": {"version": "0.2.0"}}}),
                running: true,
                stop: Ok(()),
                line: Mutex::new(Some(line)),
                calls: Mutex::new(Vec::new()),
                launched_token: Mutex::new(None),
            }
        }
        fn calls(&self) -> Vec<&'static str> {
            self.calls.lock().unwrap().clone()
        }
    }

    impl LaunchHost for FakeHost {
        fn update_status(&self) -> Result<Value, String> {
            self.calls.lock().unwrap().push("status");
            Ok(self.status.clone())
        }
        fn stop_playback(&self) {
            self.calls.lock().unwrap().push("stop_playback");
        }
        fn stop_runtime(&self) -> Result<bool, String> {
            self.calls.lock().unwrap().push("stop_runtime");
            self.stop.clone().map(|_| self.running)
        }
        fn launch(&self, token: &str) -> LaunchLine {
            self.calls.lock().unwrap().push("launch");
            *self.launched_token.lock().unwrap() = Some(token.to_string());
            self.line.lock().unwrap().take().expect("launched once")
        }
        fn restart_runtime(&self) -> Result<(), String> {
            self.calls.lock().unwrap().push("restart");
            Ok(())
        }
    }

    fn lifecycle() -> Lifecycle {
        Lifecycle::new(Duration::from_millis(300))
    }

    fn refusal_line(code: &str) -> LaunchLine {
        LaunchLine::Answer(json!({"ok": false, "code": code, "error": "fixture"}))
    }

    #[test]
    fn the_default_phase_is_idle_with_an_open_gate_and_nothing_armed() {
        let lifecycle = lifecycle();
        assert_eq!(lifecycle.phase(), Phase::Idle);
        assert!(!lifecycle.gate.is_closed());
        assert!(lifecycle.runtime_start_allowed().is_ok());
    }

    #[test]
    fn every_proved_no_start_refusal_restarts_a_runtime_that_was_running_and_reopens() {
        for code in PROVED_NO_START {
            for running in [true, false] {
                let lifecycle = lifecycle();
                let mut host = FakeHost::new(refusal_line(code));
                host.running = running;
                let answer = lifecycle.launch(&host, TOKEN);
                assert_eq!(answer["code"], *code);
                assert_eq!(answer["outcome"], "not_started");
                assert_eq!(answer["runtimeRestarted"], running, "{code}");
                let restarted = host.calls().contains(&"restart");
                assert_eq!(
                    restarted, running,
                    "{code}: restart only a runtime that was running"
                );
                assert_eq!(lifecycle.phase(), Phase::Idle);
                assert!(lifecycle.gate.enter().is_ok(), "{code}: the gate reopens");
            }
        }
    }

    #[test]
    fn a_launch_or_any_unknown_outcome_never_restarts_and_requires_exit() {
        let cases = vec![
            LaunchLine::Answer(
                json!({"ok": true, "handoff": {"version": "0.2.0", "handedOver": false}}),
            ),
            refusal_line("launch_outcome_unknown"),
            refusal_line("busy"),
            refusal_line("store"),
            refusal_line("internal"),
            LaunchLine::Answer(json!({"ok": true})),
            LaunchLine::Answer(json!({"ok": false})),
            LaunchLine::NoAnswer("no answer in time"),
            LaunchLine::NoAnswer("output ended without an answer"),
        ];
        for line in cases {
            let label = format!("{line:?}");
            let lifecycle = lifecycle();
            let host = FakeHost::new(line);
            let answer = lifecycle.launch(&host, TOKEN);
            assert_eq!(answer["exiting"], true, "{label}");
            assert!(
                !host.calls().contains(&"restart"),
                "{label}: never restarted"
            );
            assert_eq!(lifecycle.phase(), Phase::ExitRequired, "{label}");
            assert!(
                lifecycle.gate.enter().is_err(),
                "{label}: no service may start any more"
            );
            assert!(
                lifecycle.runtime_start_allowed().is_err(),
                "{label}: no runtime start"
            );
            let again = lifecycle.launch(&FakeHost::new(refusal_line("launch_failed")), TOKEN);
            assert_eq!(
                again["code"], "exit_required",
                "{label}: a second launch is refused"
            );
        }
    }

    #[test]
    fn a_service_process_windows_would_not_create_is_a_proved_non_start() {
        let lifecycle = lifecycle();
        let host = FakeHost::new(LaunchLine::NotStarted("access denied".into()));
        let answer = lifecycle.launch(&host, TOKEN);
        assert_eq!(answer["code"], "backend_not_started");
        assert_eq!(answer["runtimeRestarted"], true);
        assert_eq!(lifecycle.phase(), Phase::Idle);
    }

    #[test]
    fn an_unproved_stop_launches_nothing_restarts_nothing_and_reopens_the_gate() {
        let lifecycle = lifecycle();
        let mut host = FakeHost::new(refusal_line("launch_failed"));
        host.stop = Err("1 runtime process(es) are still running.".into());
        let answer = lifecycle.launch(&host, TOKEN);
        assert_eq!(answer["code"], "runtime_stop_unproved");
        assert_eq!(
            host.calls(),
            vec!["stop_playback", "status", "stop_runtime"]
        );
        assert_eq!(lifecycle.phase(), Phase::Idle);
        assert!(lifecycle.gate.enter().is_ok());
    }

    #[test]
    fn a_preflight_refusal_leaves_the_runtime_alone() {
        let cases = [
            ("launchAvailable", json!(false), "launch_unavailable"),
            ("updateInProgress", json!(true), "update_in_progress"),
            ("recoveryPending", json!(true), "recovery_pending"),
            ("launchReady", Value::Null, "not_approved"),
            ("launchAvailable", json!("true"), "launch_unavailable"),
        ];
        for (field, value, code) in cases {
            let lifecycle = lifecycle();
            let mut host = FakeHost::new(refusal_line("launch_failed"));
            host.status["status"][field] = value;
            let answer = lifecycle.launch(&host, TOKEN);
            assert_eq!(answer["code"], code, "{field}");
            assert!(
                !host.calls().contains(&"stop_runtime"),
                "{field}: runtime untouched"
            );
            assert!(!host.calls().contains(&"launch"));
        }
        let lifecycle = lifecycle();
        let mut host = FakeHost::new(refusal_line("launch_failed"));
        host.status = json!({"ok": true});
        assert_eq!(lifecycle.launch(&host, TOKEN)["code"], "update_in_progress");
    }

    #[test]
    fn a_malformed_token_is_refused_before_anything_is_closed_or_stopped() {
        for token in [
            "",
            "short",
            &"A".repeat(44),
            &format!("{}!", "A".repeat(42)),
        ] {
            let lifecycle = lifecycle();
            let host = FakeHost::new(refusal_line("launch_failed"));
            assert_eq!(lifecycle.launch(&host, token)["code"], "not_approved");
            assert!(host.calls().is_empty() && !lifecycle.gate.is_closed());
        }
    }

    #[test]
    fn the_token_reaches_the_launch_unchanged_and_only_after_the_proved_stop() {
        let lifecycle = lifecycle();
        let host = FakeHost::new(refusal_line("launch_failed"));
        lifecycle.launch(&host, TOKEN);
        assert_eq!(
            host.calls(),
            vec![
                "stop_playback",
                "status",
                "stop_runtime",
                "launch",
                "restart"
            ]
        );
        assert_eq!(host.launched_token.lock().unwrap().as_deref(), Some(TOKEN));
    }

    #[test]
    fn a_running_request_holds_the_launch_until_its_child_is_reaped_and_new_ones_are_refused() {
        let lifecycle = Arc::new(Lifecycle::new(Duration::from_secs(5)));
        let ticket = lifecycle.gate.enter().unwrap();
        let finished = Arc::new(AtomicBool::new(false));
        let peer = lifecycle.clone();
        let done = finished.clone();
        let launcher = std::thread::spawn(move || {
            let answer = peer.launch(&FakeHost::new(refusal_line("launch_failed")), TOKEN);
            done.store(true, Ordering::SeqCst);
            answer
        });
        let deadline = Instant::now() + Duration::from_secs(2);
        while !lifecycle.gate.is_closed() {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        assert!(
            lifecycle.gate.enter().is_err(),
            "a request after the close is refused"
        );
        assert!(
            lifecycle.runtime_start_allowed().is_err(),
            "no runtime start while draining"
        );
        std::thread::sleep(Duration::from_millis(200));
        assert!(
            !finished.load(Ordering::SeqCst),
            "the launch waits for the running request"
        );
        drop(ticket); // its child reaped
        let answer = launcher.join().unwrap();
        assert_eq!(answer["code"], "launch_failed");
    }

    #[test]
    fn a_drain_that_does_not_finish_refuses_with_the_runtime_untouched() {
        let lifecycle = lifecycle();
        let _held = lifecycle.gate.enter().unwrap();
        let host = FakeHost::new(refusal_line("launch_failed"));
        let answer = lifecycle.launch(&host, TOKEN);
        assert_eq!(answer["code"], "desktop_busy");
        assert_eq!(host.calls(), vec!["stop_playback"]);
        assert!(!lifecycle.gate.is_closed() && lifecycle.phase() == Phase::Idle);
    }

    #[test]
    fn a_second_launch_while_one_runs_is_refused() {
        let lifecycle = Arc::new(Lifecycle::new(Duration::from_secs(5)));
        let ticket = lifecycle.gate.enter().unwrap();
        let peer = lifecycle.clone();
        let first = std::thread::spawn(move || {
            peer.launch(&FakeHost::new(refusal_line("launch_failed")), TOKEN)
        });
        while lifecycle.phase() != Phase::Launching {
            std::thread::sleep(Duration::from_millis(5));
        }
        let second = FakeHost::new(refusal_line("launch_failed"));
        assert_eq!(lifecycle.launch(&second, TOKEN)["code"], "update_busy");
        assert!(second.calls().is_empty());
        drop(ticket);
        first.join().unwrap();
    }

    #[test]
    fn only_launch_takes_the_lifecycle_route() {
        assert_eq!(route_update("launch"), UpdateRoute::Lifecycle);
        assert_eq!(route_update(" LAUNCH "), UpdateRoute::Lifecycle);
        for operation in [
            "status",
            "approve",
            "discard",
            "pick_package",
            "launch_now",
            "",
        ] {
            assert_eq!(route_update(operation), UpdateRoute::OneShot, "{operation}");
        }
    }

    #[test]
    fn tickets_are_counted_exactly() {
        let gate = Arc::new(ServiceGate::default());
        let tickets: Vec<_> = (0..8).map(|_| gate.enter().unwrap()).collect();
        assert_eq!(gate.close_and_drain(Duration::from_millis(50)), Err(8));
        assert!(
            gate.enter().is_err(),
            "a timed-out drain leaves the gate closed"
        );
        gate.reopen();
        drop(tickets);
        assert_eq!(gate.close_and_drain(Duration::ZERO), Ok(()));
    }

    // ---------------------------------------------------------- the real one-line reader

    #[cfg(windows)]
    mod reader {
        use super::super::*;
        use crate::runtime::job::tests::TreeFixture;

        const TOKEN: &str = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abcde";

        /// The fixture's own recorded process is still running.
        fn running(fixture: &TreeFixture) -> bool {
            fixture.wait_for("child.pid");
            let pid: usize = std::fs::read_to_string(fixture.directory.join("child.pid"))
                .unwrap()
                .trim()
                .parse()
                .unwrap();
            let mut system = sysinfo::System::new();
            let pid = sysinfo::Pid::from(pid);
            system.refresh_processes(sysinfo::ProcessesToUpdate::Some(&[pid]), true);
            system.process(pid).is_some()
        }

        #[test]
        fn the_answer_line_is_read_and_the_process_is_left_running_with_the_token_only_on_stdin() {
            let fixture = TreeFixture::new();
            std::fs::write(
                fixture.directory.join("answer.txt"),
                "{\"ok\": true, \"handoff\": {\"version\": \"0.2.0\", \"handedOver\": false}}\n",
            )
            .unwrap();
            let line = launch_one_line(
                fixture.command("launch-answer"),
                TOKEN,
                Duration::from_secs(20),
            );
            assert!(matches!(classify(line), Classified::Launched(_)));
            let argv = std::fs::read_to_string(fixture.directory.join("argv.json")).unwrap();
            assert!(
                !argv.contains(TOKEN) && argv.ends_with("\"launch\"]"),
                "{argv}"
            );
            let stdin = std::fs::read_to_string(fixture.directory.join("stdin.txt")).unwrap();
            assert_eq!(
                serde_json::from_str::<Value>(&stdin).unwrap(),
                json!({ "launchToken": TOKEN })
            );
            assert!(
                running(&fixture),
                "the update service process must not be killed or waited for"
            );
        }

        #[test]
        fn no_line_in_time_is_unknown_and_the_process_is_not_killed() {
            let fixture = TreeFixture::new();
            let started = Instant::now();
            let line = launch_one_line(
                fixture.command("launch-silent"),
                TOKEN,
                Duration::from_millis(1500),
            );
            assert!(started.elapsed() < Duration::from_secs(10));
            assert!(matches!(classify(line), Classified::Unknown(_)));
            fixture.wait_for("stdin.txt");
            assert!(
                running(&fixture),
                "a silent update service is left running, never killed"
            );
        }

        #[test]
        fn an_exit_without_a_line_is_unknown_and_a_typed_refusal_is_classified() {
            let fixture = TreeFixture::new();
            let line = launch_one_line(
                fixture.command("launch-exit"),
                TOKEN,
                Duration::from_secs(20),
            );
            assert_eq!(
                classify(line),
                Classified::Unknown("output ended without an answer".into())
            );
            let fixture = TreeFixture::new();
            std::fs::write(
                fixture.directory.join("answer.txt"),
                "{\"ok\": false, \"code\": \"launch_failed\", \"error\": \"Windows did not start it.\"}\n",
            )
            .unwrap();
            let line = launch_one_line(
                fixture.command("launch-answer"),
                TOKEN,
                Duration::from_secs(20),
            );
            assert_eq!(
                classify(line),
                Classified::NotStarted {
                    code: "launch_failed".into(),
                    message: "Windows did not start it.".into()
                }
            );
        }

        #[test]
        fn a_service_windows_cannot_create_is_not_started() {
            let line = launch_one_line(
                Command::new(std::env::temp_dir().join("qronos-no-such-update-service.exe")),
                TOKEN,
                Duration::from_secs(5),
            );
            assert!(matches!(line, LaunchLine::NotStarted(_)));
        }
    }
}
