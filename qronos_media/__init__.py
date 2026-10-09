"""Qronos Media Intelligence: a local index of the user's own video files.

This package is separate from ``core`` on purpose (handoff s4): indexing is
long-running, can fail per file, and must not take the assistant down with it.
It owns its database, its job graph and its cache. It does not own the GPU:
anything that would load a model has to reserve through the single Qronos
resource authority (see :mod:`qronos_media.resources`), and no stage in this
package loads a model yet.

What exists is listed by :data:`qronos_media.stages.STAGES`; what does not is
listed by :data:`qronos_media.stages.PENDING_CAPABILITIES` and reported as
pending, never as done.
"""

PIPELINE_SCHEMA_VERSION = 1
