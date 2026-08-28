"""Shared test fixtures.

``web_responses`` holds recorded provider output used by the web-research
tests: real output captured from a live provider, trimmed to a few results.
Tests parse these rather than calling the network, so the suite stays offline,
deterministic, and spends none of the real search budget.

``clock`` holds a hand-driven clock, and ``link_harness`` the device-link
scaffolding. Both are here for the same reason as the recorded responses: so a
test can be deterministic without each file inventing its own way to get there.
"""
