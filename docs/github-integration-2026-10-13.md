# GitHub integration: 13 October 2026

## Scope

The integrated source starts at `ad89443`, the fetched tip of
`review/release-runtime-2026-10-12`. It includes the activity-history,
media-library and sidecar-tree-proof branches and adds 71 commits to the
previous local source `0898ce4`. The heads of all 48 open pull requests
(29 through 76) were checked as ancestors of this source.

The old voice-output and pre-main-validation branches retain historical
commits outside this ancestry. Their persistent voice implementation has
been superseded by the current lifecycle/resource-managed implementation;
their historical source is preserved remotely rather than overlaid onto the
newer implementation.

## Integration repairs

- Keep test profile checks based on exact temporary-directory containment,
  rather than directory-name spelling.
- Import the private Windows profile fixture for queue restart processes.
- Use a plainly relative filename in the print refusal test, including when
  the checkout and temporary directory are on different Windows drives.
- Keep the native installer fixture's registry inventory exact: the new
  Visual C++ prerequisite reads four DWORD values in addition to the three
  existing WebView2 string reads. All reads remain redirected to a private INI.
  The previous-installation fixture also retains the synthetic VC++ values
  when rewriting that INI; no real prerequisite installer is executed.
- Generate synthetic media with PCM audio so its authored timestamps do not
  depend on AAC encoder priming delay.
- Bind the missing-Whisper test to its private dummy asset directory, never
  the developer's actual provisioned model files.
- Tell the frozen-runtime smoke test the desktop package version, as the
  production desktop does.
- Initialize the Media host field in the legacy bare-runtime Stop fixture.
- Show complete synthetic index status in failing test diagnostics.

## Validation before final aggregate run

- Frontend: TypeScript/Vite Build and 143 tests passed; production dependency
  audit reported zero vulnerabilities.
- Rust: 72 passed, two intentionally ignored; the trusted frozen-runtime IPC
  test passed separately.
- Audited Python 3.14.8 backend and optimized Windows desktop built. Frozen
  services, startup/ping/shutdown and complete resource manifests passed.
- Media: 369 passed, one skipped with an explicit development FFmpeg override
  and synthetic media on a drive with enough free space.
- Native installer fixtures: 89 passed, two skipped in the first repair run;
  the eleven transaction failures were corrected and all 28 transaction tests
  passed in their complete rerun.
- Stop fixture: all three tests passed after adding the new constructor field.
- The first aggregate run recorded 6518 passed, 73 failed, 113 skipped and
  12 setup errors, plus 1523 passed subtests. The failures above were repaired
  or traced to low free disk space. It is retained as a failed run, not
  described as green. A final full run follows on a sufficiently large drive.

## Delivery limits

The application remains offline-first; no cloud inference was added. Update
launch remains disabled. A source merge is not a signed production release,
deployment, or proof of live desktop/mobile/model acceptance.

Whisper v1.9.5 files can be provisioned from the pinned official archive and
verified individually. The exact reproducible FFmpeg build referenced by
PR76 has no published repository Release asset at the time of this audit.
The old local FFmpeg is not admitted as the production Media decoder; an
explicit development override is used only with synthetic test videos.
Unresolved wake/TTS asset provenance and licensing gates are retained.

Validation results are recorded in the integration pull request after the
local checks complete.
