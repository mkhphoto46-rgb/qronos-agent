//! The service gate every backend service process the desktop starts passes.
//!
//! Each backend request and native playback takes a `Ticket` before its child
//! process starts and holds it until that child is reaped, so the desktop
//! always knows how many backend service processes are in flight.

use std::sync::{Arc, Mutex};

/// Admission for every backend service process the desktop starts.
#[derive(Default)]
pub(super) struct ServiceGate {
    active: Mutex<usize>,
}

/// Held by one backend request until its child is reaped.
pub(super) struct Ticket(Arc<ServiceGate>);

impl Drop for Ticket {
    fn drop(&mut self) {
        let mut active = self
            .0
            .active
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        *active -= 1;
    }
}

impl ServiceGate {
    pub(super) fn enter(self: &Arc<Self>) -> Result<Ticket, String> {
        let mut active = self
            .active
            .lock()
            .map_err(|_| "The backend service gate is unavailable.".to_string())?;
        *active += 1;
        Ok(Ticket(self.clone()))
    }

    #[cfg(test)]
    fn active(&self) -> usize {
        *self.active.lock().unwrap()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tickets_are_counted_exactly() {
        let gate = Arc::new(ServiceGate::default());
        let tickets: Vec<_> = (0..8).map(|_| gate.enter().unwrap()).collect();
        assert_eq!(gate.active(), 8);
        drop(tickets);
        assert_eq!(gate.active(), 0);
    }
}
