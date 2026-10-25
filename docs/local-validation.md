# Local validation, 25 October 2026

Integration version: **0.1.13**. Runtime tested: **Home Assistant 2026.10.2**, Python **3.14.8** on Linux. The 0.1.13 changes were checked with tests, protocol fixtures and controlled failure sequences only; no live robot check was made for this release. Generation 4 remains source-verified without Gen4 hardware.

## Automated checks

- **1,099 tests passed**, including 29 additional cases since the released 0.1.12 suite. These cover rediscovery of working, HTTPS, malformed and unreachable addresses, every Neato dustbin code, Vacuula's 504 robot timeout on Return to base, map image reuse, the setup menu for discovered servers and translated action texts, alongside the earlier protocol and recovery regressions.
- **99.54% combined line and branch coverage** across all 19 integration modules. The lowest module is **97.12%**. Configuration flow coverage is **99.38%** and the discovery helper has **100%**.
- Strict mypy: **passed**, all 21 integration and tool modules.
- **100% docstring coverage: 1,090/1,090 items in 80 Python files**, including integration code, tests, tools and the smoke script. The AST check requires nonempty docstrings for all modules, classes, functions and methods, including constructors, private helpers and nested callbacks.
- Ruff lint and formatting: **passed** for all Python source, test and smoke-check files.
- Official Hassfest: **passed**, one integration checked, zero invalid integrations. This includes manifest, translation, icons, service, quality-scale and discovery validation.
- Actionlint: **passed** for 0.1.12, GitHub workflow syntax and expressions validated. The workflow file is unchanged in 0.1.13.

Run from the source folder on this development machine:

```sh
# Use a Python 3.14 environment with requirements_test.txt installed.
TEST_PYTHON=.venv/bin/python
$TEST_PYTHON tools/check_docstrings.py
$TEST_PYTHON -m pytest -q --cov=custom_components.vacuula --cov-report=term-missing --cov-report=json
$TEST_PYTHON -m mypy custom_components/vacuula tools
$TEST_PYTHON -m ruff check .
$TEST_PYTHON -m ruff format --check .
```

The installed test dependency versions are pinned in `requirements_test.txt`. Hassfest was run from the existing Home Assistant Core checkout with its development environment and an isolated Ruff 0.17.0 installation, matching the checkout's current requirement.

## Initial read-only hardware verification

The client connected to the existing Vacuula **1.0.0** app and one **Neato D5 / Gen3** robot. Server and robot event streams opened successfully. State, general information, preferences, schedule, maps and cleaning-history reads worked. The robot reported **docked**, **100% battery**, and **dustbin_missing**.

The D5 explicitly returned `command_not_found` for `getLocalStats`. The integration records this as unsupported and continues normally. No maps or cleaning runs existed during this check.

During this initial check, no cleaning, movement, preference, schedule, map-write, reset, firmware or provisioning command was sent to the physical robot. Later authorized control testing is recorded below. Gen4 protocol behavior is verified against the app source and fixtures, with hardware testing still pending.

## Installation

The component is installed in Home Assistant's `custom_components/vacuula` directory. The initial 0.1.0 installation contained 24 files, checked against the tested source by SHA-256. Configuration validation passed before and after the copy. Home Assistant was restarted to load the component; the Vacuula app remained running.

Vacuula was then discovered through its real Zeroconf announcement and configured through Home Assistant's config-flow API. The entry is **loaded**, with one Neato D5 device running firmware **4.5.3-189**. The live entity registry contains **72 entities: 36 enabled and 36 optional diagnostics**:

| Platform | Registered | Enabled |
| --- | ---: | ---: |
| Vacuum | 1 | 1 |
| Sensor | 37 | 9 |
| Binary sensor | 13 | 6 |
| Button | 11 | 10 |
| Number | 5 | 5 |
| Select | 3 | 3 |
| Switch | 2 | 2 |

No saved maps exist yet, so map images and per-map switches will be added after the server receives maps. Gen4 event switches similarly follow discovered schedule events. No Gen4 device is present on this installation.

Live readback confirmed battery charge, the expected missing-dustbin error, disabled robot sounds and schedule, a 90-minute bin reminder, a 30-day filter reminder and a 180-day brush reminder. Battery charge varied between 99% and 100% during the checks. Home Assistant's native **Refresh data** button succeeded, all **10 advanced actions** were registered, and diagnostics downloaded successfully without the actual robot name or serial. The system log contained **zero Vacuula warnings or errors**, excluding Home Assistant's standard custom-integration notice.

## Manual setup discovery, 0.1.1

Manual setup browses `_vacuula._tcp.local.` for three seconds using Home Assistant's shared mDNS instance. Tests cover dynamically named services, advertised ports, IPv4/IPv6, no server, multiple servers, malformed and disappearing advertisements, cancellation, configured servers and confirmation retries. Resolver tasks and the temporary browser are fully cleaned up.

All 25 files in the 0.1.1 installation matched the tested source by SHA-256. Configuration validation passed and Home Assistant was restarted to load the update. The existing Vacuula entry returned to **loaded**.

A real manually initiated setup found the existing local server in **3.01 seconds** and returned **already_configured**, with **no address form**. The existing entry was retained. Successful confirmation for a new server and multiple-server selection are covered by the config-flow tests. The app was not modified and no movement or robot-setting command was sent.

## Protocol and state regressions, 0.1.2

The fixes were checked against the same Vacuula 1.0.0 source revision, `bf18585`.

| Behavior | Regression evidence |
| --- | --- |
| Timestamp map/run IDs | Real HTTP requests assert literal timestamp colons in raw paths for reads, writes and boundary routes; path delimiters remain escaped. |
| Cleaning defaults | Repeated edits are saved through Home Assistant's actual entry store, read back and loaded into a new coordinator; unrelated options and earlier snapshots remain intact. |
| Saved-map cleaning | Both generations' command payloads use enabled/promoted maps and no-go lines, including maps created after the previous cache refresh. Disabled, ambiguous or unavailable maps fail closed. Explicit mapless/spot choices remain available. |
| Safe upgrade | Minor version 2 migrates old house defaults to Automatic while preserving other choices and settings; new explicit mapless choices survive. |
| Return to base | Both generations pause, wait 3.5 seconds, read readiness and dock under one lock. Failure and cancellation tests prevent the later command; concurrent commands cannot interleave. Button and readiness sensors match the new capability. |
| Gen3 schedule clock | Local-to-UTC conversion covers summer/winter offsets, half-hour zones, midnight and weekday rollover. Explicit UTC input remains unchanged. Fixed UTC schedules need rewriting after a DST offset change. |
| Gen4 boundaries | Fresh map metadata decides whether promotion is needed. Active maps save then promote under one lock; inactive maps stay inactive. Save/promotion errors and concurrent commands are tested. |
| Promotion wait | The promotion request uses 90 seconds total and 80 seconds read time, beyond the server's 60-second acknowledgement wait. |
| Vacuum and optional telemetry | Busy mapping/maintenance states have HA activity; transient 404/405/503 optional reads recover, while explicit unsupported commands remain suppressed. |
| Empty server identity | Hostname/IP aliases and bounded shared-resolver lookups prevent duplicates without matching unrelated server versions, ports or conflicting endpoints. |
| Diagnostics and readbacks | Allowlisted details/meta preserve charge, docking, charging, model and firmware without identity leakage or I/O. Read-only data actions do not fan out; controls refresh only state and changed resources. |

These behaviors were tested with mocked robot transports; only the raw HTTP routing test uses a local test server. No movement, schedule, boundary, preference or reset command was sent to the physical robot during this work.

All 25 installed 0.1.2 files matched the tested source by SHA-256. Home Assistant configuration validation passed before and after installation. After one restart, the existing entry was **loaded**, its persisted schema was **version 1 / minor version 2**, and the D5 remained connected. A real diagnostics download identified runtime **0.1.2** and included charge, docked, charging and firmware fields without the actual robot serial. The Vacuula app was not modified.

## Cleaning, discovery and recovery regressions, 0.1.3

- Automatic cleaning uses whole-home fallback with disabled/unpromoted maps or an off-dock robot, matching the app. Saved map selection retains validation, including ambiguous promoted maps when off dock.
- Manual discovery opens the address form for configured-only servers, unreachable advertisements and failed confirmation. Tests create a second server through that fallback while retaining hostname/IP duplicate protection.
- Gen4 schedule events default to the promoted floorplan and no-go protection without requiring the robot to be docked while writing the event. Explicit mapless schedules remain supported.
- Active boundary updates and standalone promotions persist a recovery marker before upload, refresh map status and retain a repair notice after failure/cancellation. Confirmed promotion/readback clears it. Reload, entry removal and stale-map invalidation are covered.
- Queued Automatic/mapped cleaning and schedule writes check recovery under the robot command lock. Concurrency tests prevent them from bypassing a failed upload; explicit whole-home commands remain available.
- Source-specific maintenance, menu, firmware and stationary map-transfer states remain docked/idle rather than triggering cleaning automations. Exploration and cleaning recovery retain active navigation behavior.
- Gen3 schedule round trips preserve explicit nested boundary IDs and reject missing/conflicting IDs before a write. The real hardware's zone response remains unconfirmed; these tests establish supported inputs rather than a captured robot response.

All robot writes were tested with mocked transports. No movement, schedule, boundary, preference or reset command was sent to the physical robot.

All 25 installed 0.1.3 files matched the tested source by SHA-256. Configuration validation passed before and after copying. After one Home Assistant restart, the existing entry was **loaded**, diagnostics confirmed runtime **0.1.3**, and the D5 remained connected and docked. A real Add integration flow found the already configured server and displayed the **Server URL** form for another server. That temporary flow was removed without creating an entry. The Vacuula app was not modified.

## Recovery lifecycle and schedule patches, 0.1.4

- Offline promotion and active-floorplan boundary edits are rejected before any save or recovery marker. Inactive map boundaries remain server-side metadata and can be edited offline.
- A successful authoritative map read resolves pending status when the map is promoted, inactive or removed. The server exposes the same inactive state after a failed promotion and deliberate demotion, so known inactive maps use normal Automatic whole-home fallback without no-go protection. Unknown status or failed readback retains the guard.
- Tests cover harmless promotion failures with an unchanged active map, actual failed uploads with inactive maps, app-side promotion/demotion, deleted maps, removed robots, reloaded pending options and multi-robot notice isolation. Notices include the robot name and map ID.
- Pending status bypasses the five-minute resource cache. Map polling shares the robot lock so a read started during upload cannot clear uncertainty using stale pre-upload state.
- Existing Gen4 schedule updates read the current event, merge only explicit fields and write under one lock. Tests preserve multiple zones, different per-run settings and unknown options through metadata, mode, navigation and no-go edits. Explicit zone lists replace zones; cross-floorplan changes require explicit zone IDs.
- Concurrent edits preserve both changes. Missing, duplicated or malformed events and failed reads prevent stale writes. Event-enabled switches use the same fresh merge path and preserve newer app edits.

All robot writes were tested with mocked transports. No movement, schedule, boundary, preference or reset command was sent to the physical robot. The Vacuula app was not modified.

All 25 installed 0.1.4 files matched the tested source by SHA-256. Home Assistant configuration validation passed before and after copying. After one restart, the existing entry was **loaded**, a real diagnostics download confirmed runtime **0.1.4**, and the D5 remained connected and docked. The previous installation was backed up outside the source repository.

## Review fixes, 0.1.5

- A failed or timed-out robot read (Vacuula's 504) keeps the robot connected and its controls usable on both generations. A failed map read during a server outage keeps entities unavailable, and **Refresh data** reports failed reads.
- Gen4 notices with severity `invalid`, labelled "Info" by the app, are not reported as problems, and `robots.error_codes` events are ignored, as in the app.
- A removed robot's device and entities are created again when it returns. A robot the server stops listing keeps its map entities, shown as unavailable, and is not read until it is listed again, including when it returns through a server event before its maps are read.
- Promotion requires a floorplan marked for use, and `unuse` is refused for the promoted floorplan. After a failed upload the guard and notice stay while the map is demoted, and clear on promotion here or in the app, demotion through `map_action`, or when the map leaves use. The schedule is read again after every promotion attempt.
- Map switches send the server's current map name. Map, schedule-event and device names follow renames without a reload; event names include their days. Deleted maps and events lose their entities, which return with the record.
- Boundaries saved by the app (empty names, older colours, points slightly outside the map) are accepted, and a missing Gen3 relevancy is computed as in the app. `update_schedule_event` with Automatic on a mapped event is refused while no floorplan is promoted.
- The address form defaults to `http://vacuula.local`. Two servers behind one proxy address with different robots get separate entries, and a Zeroconf address update cannot clash with an ignored discovery.

The regression tests for these problems were checked to fail on the 0.1.4 code. All robot writes were tested with mocked transports; no movement, schedule, boundary, preference or reset command was sent to the physical robot. The Vacuula app was not modified.

All 25 installed 0.1.5 files matched the committed source by SHA-256. Home Assistant configuration validation passed before and after copying. After one restart, the existing entry was **loaded**, a real diagnostics download confirmed runtime **0.1.5** without the robot serial or server URL, and the D5 remained connected and docked. The 37 existing entities kept their states, and the Generation 3 Alert sensor was added with the value `none`. The log showed no Vacuula warnings or errors apart from Home Assistant's standard custom-integration notice. The previous installation was backed up outside the source repository.

## Independent review, 0.1.6

The review compared the integration with Vacuula 1.0.0 source revision `bf18585`
and used independent reviews of controls, services and entity lifecycle.
Concurrency, registry, reconfiguration and numeric-response failures were
reproduced before their fixes; the paused-home issue was found on the real D5.

| Finding | Fix and regression evidence |
| --- | --- |
| A promotion could complete after map selection but before a queued cleaning/schedule command, leaving that command mapless | Deferred builders refresh and choose maps under the command lock. Concurrency tests cover vacuum, advanced cleaning and schedule creation/edits. |
| Native readiness and connection checks became stale while waiting for the command lock | Start, Pause, Stop, Resume, spot cleaning, exploration and docking validate at execution; queued disconnect and changed-readiness tests prevent invalid sends. |
| The real D5 kept `goToBase: false` during a resumable paused run, so the integration refused the app's home sequence | A Gen3-only exception requires fresh, error-free, resumable paused state. Tests use the observed response and reject failed reads, errors, busy/docked states and non-resumable runs. |
| Disabled map and event entities never ran their own deletion callbacks | Platform listeners reconcile registry records even when disabled. Real HA tests cover deletion/return, rapid transitions, preserved disable preferences and robot isolation. |
| Manual reconfiguration could duplicate a unique ID held by an ignored discovery | Preserve the original entry ID while updating its URL; regression verifies one owner per unique ID and keeps the ignored entry intact. |
| Invalid map IDs could create bogus image entities | Image discovery accepts only nonempty string IDs. |
| JSON exponent overflow bypassed rejection of non-finite response values | Finite-number parsing rejects overflow for HTTP and SSE; finite exponents remain supported. |

Gen4 behavior and saved-map commands remain source-verified and fixture-tested;
no Gen4 hardware is available on this installation. The Vacuula app is unchanged.

### Authorized D5 control test

Locate, Start, Pause and the Resume button were acknowledged through Home
Assistant. Fresh reads confirmed idle/docked → cleaning/off dock → paused →
cleaning. The native return-home sequence then paused but refused the false
`goToBase` flag. A fresh read confirmed state `3`, action `1`, `resume: true`,
`pause: false`, `goToBase: false`, no error and not docked. The exact app
`sendToBase` request was sent once and returned `result: ok`.

The subsequent robot state was idle/off dock, with `dockHasBeenSeen: false` and
no error. Successful physical docking was not confirmed. The integration does
not report returning or docked based only on command acknowledgement. The
paused-run fix is covered against this live response by regression tests;
Gen4 and saved-map writes remain mocked. No preferences, schedules, boundaries,
firmware, reset or provisioning settings were changed on the physical robot.

All 25 final 0.1.6 component files matched the tested source by SHA-256.
Configuration validation passed, and a restart loaded 0.1.6 successfully with
the D5 connected. The original 0.1.5 installation was backed up outside Git.
Final telemetry continued to report idle/off dock with no error.

### Follow-up D5 docking test

After the robot was manually placed on its dock, a new authorized test started a
short cleaning run and called Home Assistant's native **Return to base** action.
The integration used **Pause → Send to base**, without cancelling the run. The
robot reported location recovery, then `isDocked: true` within about a minute.
Two fresh reads confirmed that it stayed docked, at 98% battery and with no error.
Charging remained false. This verifies return to base during an active run; the
cancelled-run case was not exercised.

## Complete docstrings and enforcement, 0.1.7

Every Python module, class, function and method has a descriptive docstring,
including test fixtures, constructors, private helpers and nested protocol
callbacks. All 52 pre-existing Python files have identical executable ASTs after
removing docstrings; integration and test behavior is unchanged.

`python tools/check_docstrings.py` audits source without importing it and requires
**100%** presence coverage. It reports every missing definition with its file and
line, rejects empty docstrings, and fails on unreadable or invalid source and
symbolic links. Only named generated, cache, virtual-environment and
version-control directories are excluded. New source directories are included
automatically. Descriptive accuracy still requires review.

The audit runs in pytest and before dependency installation in GitHub checks.
Regression tests cover missing module, class, constructor, property, async-method
and nested-callback documentation, whitespace-only strings, new source paths,
generated files, syntax errors, empty/missing roots and linked source. GitHub
checks also run lint, formatting, strict typing, the complete test suite and
Hassfest, with actions pinned to reviewed commit IDs. Actionlint validates the
workflow locally.

All 25 installed 0.1.7 component files matched the validated source by SHA-256.
Home Assistant configuration validation passed before and after copying. A restart
loaded the existing Vacuula entry with runtime version 0.1.7, a healthy coordinator
and the D5 connected and docked without an error. The previous 0.1.6 installation
was backed up outside the repository.

## Bundled branding, 0.1.8

The official Vacuula SVG was rendered without changes to its colors, shapes or
effects, trimmed to remove excess transparency, centered in a square and resized.
`brand/icon.png` is 256 × 256 and `brand/icon@2x.png` is 512 × 512. Both are
lossless, interlaced RGBA8 PNGs; image dimensions, transparency and all PNG chunk
checksums were verified. Visual review covered white and dark backgrounds.
The original SVG, complete upstream license and attribution notice ship in the
same directory. [Artwork provenance and reproduction](branding/README.md)
records the pinned upstream source and rendering commands.

Home Assistant 2026.10.2 initially returned HTTP 404 for the missing Vacuula icon
with `placeholder=no`. After installation and restart, all eight supported
icon/logo, normal/high-resolution and light/dark requests returned HTTP 200,
`image/png` and SHA-256 hashes matching the expected packaged icon or documented
fallback. No generic placeholder was accepted as a successful result. The
square artwork needs only the two icon files, following the current guide.

Hassfest passed with zero invalid integrations, and the focused docstring and
integration tests passed (33 tests). The prior complete 711-test run and 99.50%
test coverage remain applicable: no Python implementation or test code changed
in this branding update. Docstring coverage remains 779/779 items, or 100%.
The 0.1.7 installation was backed up outside Git, and configuration validation
passed before and after installing all 30 component files.

## Live D5 control review, 0.1.9

An authorized test of the D5 on 20 October 2026 ran Home Assistant 0.1.8
controls while sampling Home Assistant states, fresh `getRobotState` reads and
the robot's Vacuula event stream.

| Control | Result on the D5 |
| --- | --- |
| Locate, Start, Pause, Resume and Stop (vacuum services and buttons) | Acknowledged; fresh reads and Home Assistant agreed within seconds. |
| Return to base after the robot had seen its dock during the run | Pause, then `goToBase: true`, `sendToBase`, location recovery (action 9), docking (action 4) and docked within about a minute, four times (two of these runs started away from the dock and passed it first). |
| Return to base before the robot had seen its dock (run started away from it) | Pause left `goToBase: false` with `dockHasBeenSeen: false`. The 0.1.6 override sent `sendToBase`; the robot replied `ok` and went idle where it stood, the run ended and the action reported success. |
| Return to base while idle off the dock | Refused, matching the robot's `goToBase: false`. |
| Spot cleaning | Started from the dock and docked by itself when finished. |
| Robot sounds, bin and filter reminders, Gen3 schedule | Written, read back and restored. Local schedule times were stored as UTC with the correct weekday rollover. |
| Advanced clean with extra-care navigation | Sent `navigationMode: 2`; the robot reported it in its state. |

The event stream showed when the D5 reports changes. It sends a state event
when its state changes and when it touches the dock, but not when command flags
or the action change within a run. After a start, its only event carried
`pause: false` and charging on; pause became available and charging ended about
5 to 15 seconds later without an event. Home Assistant therefore kept the Pause
button unavailable, and refused Pause and Return to base, until its next
60-second poll. On the way home the robot sent one event for action 9 and none
for the switch to docking, so Home Assistant showed Cleaning for the whole trip.

0.1.9 follows the robot's `goToBase` flag again and leaves the run paused, with
an error naming the missing base when the robot is off its dock and
`dockHasBeenSeen` is false. Pause, Stop and Return to base refusals based on
cached readiness are checked once against a fresh read, state is read again 10
seconds after a motion command or an event reporting the robot busy, and action
9 after a Home Assistant return is shown as Returning. Regression tests replay the observed sequences;
they fail on 0.1.8. A mapping run was started and interrupted, so no saved map
exists on this installation; saved-map and boundary writes, log uploads and
factory reset remain fixture-tested. Gen4 behavior remains source-verified.

The same release fixes findings from an independent review of 0.1.8: the
Navigation mode select now shows Normal, which Start sends, until a default is
chosen; a failed Gen4 language read no longer empties the language select; map
names stay while the map list is unknown; a mapped Gen4 schedule event can be
turned off, but not switched on, during floorplan recovery; and errors for
unknown event IDs, whole-home cleaning with a map and off-step reminder values
name the actual problem.

## Full review after 0.1.9

Reviewed commit `820cec096709fd4fd2fee6618a3ffd2527a52741` from setup and
transport through coordinator lifecycle, entity updates, controls, schedules,
map operations, diagnostics and failure recovery. Protocol behavior was checked
against the local Vacuula 1.0.0 source. Independent review passes and failing
regression cases confirmed these defects:

- Cached Start and spot guards could bypass the fresh-read fallback. Start also
  chose Resume before acquiring the command lock. Start now confirms state and
  chooses the operation under that lock, reading maps only for a new mapped run.
  Supported command buttons remain callable while connected; execution validates
  readiness, while readiness sensors retain the robot's reported flags.
- An active Gen4 boundary save could commit before its response was lost, or
  remain stored after promotion was refused before demotion. An unchanged
  promoted flag then authorized cleaning with the robot's previous zones.
  Recovery now distinguishes these edits from ordinary promotion uncertainty,
  persists before the save, and survives reloads and failed alternate-map
  promotions. Unchanged rejected saves clear only their own new guard. Recovery
  tests cover acknowledged uploads, observed app demotion and re-promotion,
  deliberate abandonment, removal, lost reads and cancellation.
- An acknowledged Gen3 home command could restore its returning marker after a
  fresh terminal state had cleared it. Terminal states now keep it cleared,
  while a briefly unchanged paused readback retains intent until movement.
- Later successful robot-info reads did not update device registry firmware.
  Confirmed model and firmware changes now update the registry without changing
  user names or creating entities.

The revised source was exercised directly against the D5, leaving the HACS
installation unchanged. Start, Pause, Resume through Start, and fresh state
readbacks worked. A return request paused the resumed run, but the robot still
reported `goToBase: false` with `dockHasBeenSeen: true`, no error and Resume
available. The revised integration refused the home command and left it paused.
One direct app-protocol `sendToBase` request in that same state returned `ok`
and ended the run idle off dock. This confirms that the seen-base flag alone
cannot safely override a native home refusal. Regression cases now replay this
sequence for vacuum and button actions. Successful docking was not established
in this run; the robot required a manual return to its dock.

After the manual return, a fresh robot read confirmed idle and docked, 98%
battery and no error. Charging was not active at that read.

No firmware, reset, provisioning, schedule, preference or boundary writes were
performed on the real robot during this review. Gen4 map failure paths remain
verified against server source and simulated transactions, without Gen4 hardware.

## Follow-up review of 0.1.10

Reviewed released commit `c1e9a801a8b76421e079d37ac17ba72cf2657aeb`, with
independent control, map, transport and state-update reviews. Real loopback HTTP
failures reproduced these map races before the coordinator fixes:

- Vacuula's threaded boundary handler can continue after the client disconnects.
  A fresh unchanged boundary read can precede that later commit. The old guard
  could clear even though the robot never received the committed zones.
- An interrupted promotion can leave the previous map looking active before its
  handler demotes it. A later request could overlap that handler and be undone.
  External matching values or observed app promotions cannot identify which
  writer finished. Unknown requests now remain fenced across reloads until a
  later server process is observed or its actual restart is explicitly confirmed.
- A formerly inactive map can become active through the app before a delayed
  boundary save finishes. An interrupted `unuse` can hide an active floorplan.
  Restart recovery now keeps the appropriate upload guard, refreshes consequential
  metadata after the boot evidence, and prevents conflicting writes to another
  map during existing recovery. Confirming that the old handler stopped does
  not by itself confirm which zones reached the robot.

Known completed Vacuula rejections remain distinct from unknown delivery.
Connection setup failures that prove a request was not sent introduce no new
guard; an earlier saved boundary change still requires its upload. Real aiohttp
transport tests also found and fixed its implicit DELETE retry after a lost
reply. A dropped map DELETE now dispatches once. Persistent notices reconstructed
from a pre-dispatch operation record are removed with the entry too, and notices
retain the original guarded map ID during an uncertain alternate-map request.

Home readiness tests showed that stale positive or missing flags could bypass
the old fallback. Home now always starts with fresh state and requires explicit
permission after any pause. Lost acknowledgements and repeated paused reports
no longer erase observed or pending Gen3 departure. Pending intent expires after
60 seconds; observed travel has a fixed 15-minute bound through pause/resume.
Repeated Home requests do not resend or interrupt an accepted trip. Explicit
new tasks clear older unobserved intent, and a rejected new Home preserves an
already paused trip. Cancellation still schedules only a state read, never a
motion retry.

Both generations also lost newer push updates when an older in-flight HTTP state
read completed. Received state deltas now overlay that reply, preserving unrelated
fresh fields and cleaning up on failure or cancellation. The protocol has no
shared source sequence, so this follows arrival order rather than establishing
the robot's ordering of arbitrarily delayed messages.

The complete suite and all automated checks listed above passed. A read-only
check of the installed 0.1.10 integration confirmed the D5
idle and docked, 98% battery, with no reported error. No movement, preferences,
schedules, map changes or other writes were sent to the physical robot during
this follow-up review. Gen4 remains source-verified and simulated, without Gen4
hardware. The Vacuula app was not modified.

## Full review of 0.1.11

Reviewed released commit `2e0e9650b9d8e47446b709eb1e966d3123a428e7` across
transport, server and robot event handling, controls, map recovery, actions,
schedules, setup, discovery, entities, diagnostics and lifecycle. Three independent
review passes and failing regressions established these further defects:

- An older HTTP robot-list reply could overwrite a newer server event. This
  restored old connection flags and names or incorrectly treated a listed robot
  as removed, clearing its map-recovery guard. A server-event revision now keeps
  an overlapping older poll from replacing that newer complete snapshot.
- Advanced `vacuula.clean` chose its docking policy from cached state. A robot
  that had docked since the cache could receive a mapless run despite an active
  map. Advanced cleaning now reads state under the same lock as target selection.
  Unknown docking is also distinct from an explicit off-dock report: with an
  active map, it cannot silently authorize the Automatic fallback. Future
  schedules do not depend on present docking. An additional app-source check
  showed explicit map/zone requests bypassed that dock guard too, although the
  app offers those controls only when docked. Immediate explicit targets now
  require confirmed docking instead of forwarding a request the app would not
  offer or silently replacing it with whole-home cleaning. The physical
  behavior of that old override was not tested.
- Duplicate boundary IDs were accepted. The exact Gen4 handler, executed
  unchanged against scratch SQLite data, replaced the same existing region twice
  and returned success with only the final region. Both generations now reject
  duplicate IDs before network I/O; valid order and empty replacements remain.
- The coordinator cleared home intent before a new task was accepted. Rejected,
  lost or cancelled Stop/task requests could therefore publish Cleaning during
  the same unchanged trip home, including through concurrent state events. New
  tasks replace intent after acknowledgement; Stop waits for terminal telemetry.
- A malformed optional language list could crash the language selector or turn
  a string into individual-letter options. Validated choices keep the selector
  safe and allow recovery when a proper list returns.
- Dynamic diagnostic text was filtered only when first discovered. Later URLs,
  emails or private-key text could become entity states. Dynamic text now uses
  the same privacy check on every read and recovers when safe text returns.
- Gen4 last-run status used the final zone's state when overall completion was
  false. The app displays `cleaning_complete` as its overall result
  (`app/src/pages/runs/runs.tsx`, lines 72 and 94). Status now reports `done`
  only for true, `incomplete` for false and unknown for missing or malformed
  flags. Gen3 status is unchanged.

The complete suite passed in 119.57 seconds. Ruff, strict mypy, the repository-wide
docstring check, Hassfest and workflow validation passed. A fresh live check
confirmed Vacuula app 1.0.0 and the installed 0.1.11 integration healthy, with the
D5 idle and docked at 99% and no reported error. The app was not modified. No
cleaning, schedule, preference, map or destructive robot action was needed to
reproduce these failures. Gen4 behavior remains source-verified and simulated.

## Full review of 0.1.12

Reviewed released commit `957e42bd24affa8d689d6786a708ade036ae04a9` across the
client, coordinator, actions, setup, discovery, entities, images and
diagnostics. All 1,070 existing tests passed with 99% branch coverage, so the
review looked for behavior that was tested as intended but wrong in practice.
Probe tests and Home Assistant source checks established these defects:

- Zeroconf rediscovery replaced any configured URL with the announced
  plain-HTTP IP address and reloaded the entry. A probe confirmed that the
  server's own advertised hostname and a deliberately configured HTTPS reverse
  proxy were both rewritten, which also undid Reconfigure. The URL now follows
  discovery only when the stored address is missing, malformed, unreachable
  within 10 seconds, or reaches other robots; HTTPS is never downgraded.
- The dustbin sensors matched only `dustbin_missing` (error) and `dustbin_full`
  (alert). Home Assistant's former Neato integration lists the robot codes
  `ui_error_dust_bin_missing`, `ui_error_dust_bin_full`,
  `ui_alert_dust_bin_full` and `dustbin_full` as an error too. Both sensors now
  accept every listed code. Which codes the D5 reports was not rechecked.
- Vacuula's 504 after its robot wait was a plain `CommandError`, which Return to
  base treated as an explicit rejection, clearing pending Gen3 home intent. It
  is now `CommandTimedOut`, handled like a lost acknowledgement.
- Home Assistant rotates image tokens every five minutes, and the five-minute
  map cache meant each viewer refetch downloaded and rendered the floor grid
  again and changed the image state. Images are now reused until the map's list
  record changes, for at most an hour, and the timestamp changes only with the
  picture.
- Setup from Add integration offered no address entry once a new server was
  discovered. A menu now offers the discovered server or an address.
- Action names and descriptions were fixed English in `services.yaml`; they now
  live in the translation files with labelled dropdown options.
- An unreachable Return to base entry in the button command table and two
  identical reply-unwrapping helpers were removed.

Ruff, strict mypy, the repository-wide docstring check and official Hassfest,
run from the existing Home Assistant Core checkout, passed. The complete suite
passed in 171.36 seconds. No command was sent to the physical robot and the
Vacuula app was not modified. Gen4 behavior remains source-verified and
simulated.

## Quality target

The implementation follows the current quality-scale checklist and has no official tier claim. Public brands submission, separately published typed client packaging, and formal Core assessment remain pending. Source, tests and documentation are maintained in `moryoav/ha-vacuula` while owner testing continues.
