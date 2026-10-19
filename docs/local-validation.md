# Local validation, 19 October 2026

Integration version: **0.1.3**. Runtime tested: **Home Assistant 2026.10.2**, Python **3.14.8** on Linux.

## Automated checks

- **555 tests passed**, including real Home Assistant platform loading for both protocol generations, native entity services, reloading, unloading, late discovery, offline maps, reconnection, error handling, concurrent preference writes, manual setup discovery and the protocol/state regressions below.
- **99.65% combined line and branch coverage** across all 19 integration modules. The lowest module is **98.96%**. Configuration flow coverage is **99.29%** and the discovery helper has **100%**.
- Strict mypy: **passed**, all 19 modules.
- Ruff lint and formatting: **passed** for all Python source, test and smoke-check files.
- Official Hassfest: **passed**, one integration checked, zero invalid integrations. This includes manifest, translation, icons, service, quality-scale and discovery validation.

Run from the source folder on this development machine:

```sh
# Use a Python 3.14 environment with requirements_test.txt installed.
TEST_PYTHON=.venv/bin/python
$TEST_PYTHON -m pytest -q --cov=custom_components.vacuula --cov-report=term-missing --cov-report=json
$TEST_PYTHON -m mypy custom_components/vacuula
$TEST_PYTHON -m ruff check .
$TEST_PYTHON -m ruff format --check .
```

The installed test dependency versions are pinned in `requirements_test.txt`. Hassfest was run from the existing Home Assistant Core checkout with its development environment and an isolated Ruff 0.17.0 installation, matching the checkout's current requirement.

## Read-only hardware verification

The client connected to the existing Vacuula **1.0.0** app and one **Neato D5 / Gen3** robot. Server and robot event streams opened successfully. State, general information, preferences, schedule, maps and cleaning-history reads worked. The robot reported **docked**, **100% battery**, and **dustbin_missing**.

The D5 explicitly returned `command_not_found` for `getLocalStats`. The integration records this as unsupported and continues normally. No maps or cleaning runs existed during this check.

No cleaning, movement, preference, schedule, map-write, reset, firmware or provisioning command was sent to the physical robot. Movement and write behavior are covered using mocked transports and remain for the owner's physical testing. Gen4 protocol behavior is verified against the app source and fixtures, with hardware testing still pending.

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

## Quality target

The implementation follows the current quality-scale checklist and has no official tier claim. Public brands submission, separately published typed client packaging, and formal Core assessment remain pending. Source, tests and documentation are maintained in `moryoav/ha-vacuula` while owner testing continues.
