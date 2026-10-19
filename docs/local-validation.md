# Local validation, 19 October 2026

Integration version: **0.1.1**. Runtime tested: **Home Assistant 2026.10.2**, Python **3.14.8** on Linux.

## Automated checks

- **305 tests passed**, including real Home Assistant platform loading for both protocol generations, native entity services, reloading, unloading, late discovery, offline maps, reconnection, error handling, concurrent preference writes and manual setup discovery.
- **99.79% combined line and branch coverage** across all 19 integration modules. The lowest module is **98.80%**. Configuration flow and discovery helper coverage are **100%**.
- Strict mypy: **passed**, all 19 modules.
- Ruff lint and formatting: **passed**, all 37 Python source, test and smoke-check files.
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

The installed test dependency versions are pinned in `requirements_test.txt`. Hassfest was run from the existing Home Assistant Core checkout with its development environment and Ruff 0.16.10.

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

## Quality target

The implementation follows the current quality-scale checklist and has no official tier claim. Public brands submission, separately published typed client packaging, and formal Core assessment remain pending. Source, tests and documentation are maintained in the private `moryoav/ha-vacuula` repository while local owner testing continues. Public release remains pending.
