# Vacuula for Home Assistant

[![HACS custom repository](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=flat-square)](#installation)

---

Local Home Assistant integration for Neato robots already connected to a
[Vacuula server](https://github.com/vacuula/vacuula). Home Assistant talks to the
same local app API as the Vacuula web interface. It needs no Neato account,
robot credentials, or cloud connection.

## Requirements

- Home Assistant **2026.10.2 or newer**.
- A running [Vacuula server](https://github.com/vacuula/vacuula) with your robot
  already connected. This integration does not provision robots or install the
  Vacuula app.
- [HACS](https://www.hacs.xyz/docs/use/download/download/) for the recommended
  installation method. Manual installation is also available below.

## Installation

### 1. Install the integration

#### HACS (recommended)

**Vacuula is not in the default HACS catalog. Add this repository as a custom
repository with type Integration before downloading it.**

[![Open the Vacuula HACS repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=moryoav&repository=ha-vacuula&category=integration)

1. Select the button above. If prompted, confirm **Add** to add the custom
   repository to HACS.
2. Open **Vacuula** in HACS and choose **Download**.
3. Restart Home Assistant.

If you prefer to add the custom repository manually:

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu in the top right and select **Custom repositories**.
3. Enter this repository URL:

   ```text
   https://github.com/moryoav/ha-vacuula
   ```

4. Select **Integration** as the type, then select **Add**.
5. Open **Vacuula** in HACS, choose **Download**, and restart Home Assistant.

#### Manual installation (alternative)

Download this repository and copy:

```text
custom_components/vacuula
```

to:

```text
/config/custom_components/vacuula
```

Then restart Home Assistant and continue with setup below.

### 2. Add the integration

[![Add the Vacuula integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=vacuula)

Use the button above after Home Assistant restarts. It opens the **Vacuula**
setup flow, which searches for local servers automatically.

You can also open **Settings > Devices & services** and configure the discovered
**Vacuula** server, or choose **Add integration > Vacuula**.

1. Confirm the discovered server. If several servers are found, choose one
   first. Home Assistant reads the address and actual port from the server's
   `_vacuula._tcp.local.` announcement. Already configured servers are excluded.
2. If no new server can be reached, enter its HTTP or HTTPS origin, for example
   `http://vacuula.local`, or `http://vacuula.local:81` if you changed Vacuula's
   app port from its default of 80. The hostname must resolve from Home
   Assistant; use the server's LAN address if it does not.

The **Server URL** field also appears when discovered servers are already
configured, or a discovered address cannot be reached. This allows adding a
second server or correcting an address advertised by a VPN or Docker host.
Discovery needs local mDNS traffic to reach Home Assistant; isolated networks
may require the manual address. Include the protocol, and the app port unless it
is the default 80; omit `/web`, `/api`, ingress paths, credentials, query
parameters, and fragments.
The direct API does not require authentication. Authenticated reverse proxies
and redirects are not supported. Keep access to this API on a trusted network.

One config entry represents a server and includes its supported robots. Duplicate
server URLs or overlapping robot identities are rejected. Use the integration
entry's **Reconfigure** option to change the URL. Reconfiguration checks that the
new server still contains a previously known robot when both lists are nonempty.
There is no separate options flow; cleaning defaults are entity controls.

## Robots and verification

| Robot generation | Status |
| --- | --- |
| `neato.gen3` | Implemented from the app protocol. D5 telemetry was checked on real hardware using read-only requests. Hardware command execution has not been validated. |
| `neato.gen4` | Implemented and tested with fixtures derived from the app protocol. No Generation 4 hardware validation yet. |
| Other generations | Not exposed by this integration. |

Available features depend on the robot and reported capabilities. A feature in
the app protocol is not a promise that every model supports it. Generation 3
multi-zone cleaning is deliberately rejected: only the app's single-boundary
payload is established. Generation 4 `auto` and `max` cleaning modes are offered
only for the app's `N2` model marker.

## Entities and use cases

Each supported robot is a Home Assistant device with a standard vacuum entity.
Additional entities expose supported telemetry, preferences, and cleaning
defaults through sensors, binary sensors, switches, numbers, selects, buttons,
and floor-map images. Optional controls appear only when supported by the robot
or generation. Diagnostic controls may need enabling in the entity registry.

| Controls | Available functionality |
| --- | --- |
| Sensors | Battery, state, action, errors, Generation 3 alerts, reported battery details, saved map/run counts, schedule count, and last-run area, duration, timestamps, and status. Additional reported scalar telemetry is available as diagnostics disabled by default. |
| Binary sensors | Server-observed robot connection; charging, docked, and problem state; Generation 3 dustbin state; reported command readiness. Optional Generation 4 quickboost and cleaning-center telemetry and additional reported boolean diagnostics appear when present. |
| Buttons | Start/resume, stop, pause, return to base, locate, and refresh; supported Generation 3 spot cleaning, mapping exploration, dismiss alert, and diagnostic log upload. Log upload and other maintenance controls may be disabled by default. |
| Switches | Generation 3 robot sounds, schedule enabled state, and one enabled toggle per saved map; Generation 4 force-floorplan preference and one enabled toggle per existing schedule event. |
| Selects | Cleaning mode, navigation and cleaning type defaults: Automatic, saved map, or explicit whole-home cleaning without a map; supported Generation 3 spot cleaning; Generation 4 language from the robot's advertised choices. |
| Numbers | Generation 3 bin reminder (30–150 minutes), filter reminder (30–90 days), brush reminder (120–240 days), and supported spot width/height (100–400 cm). |
| Images | One locally rendered PNG per stored floor map. |

Use the vacuum entity for starting or resuming cleaning, pausing, stopping,
returning to base, locating the robot, and selecting a cleaning mode. Additional
controls cover generation-specific schedules, preferences, spot cleaning,
mapping, and maintenance actions. Cleaning defaults stored by Home Assistant do
not start a run when changed.

New runs default to **Automatic (use saved map)**. Before starting, the
integration reads the current saved-map list. While docked, Generation 3 uses
the robot's dock matching against enabled maps; Generation 4 uses the single
promoted floorplan with no-go areas enabled. When no map is enabled/promoted,
or the robot is off its dock, Automatic starts whole-home cleaning, matching
the Vacuula app. This fallback does not apply saved zones or no-go lines.
Choose **Saved map** to require mapped cleaning and reject those fallback
conditions. Failed map reads, malformed maps and ambiguous promoted floorplans
still report an error.

**Whole home without a map** is an explicit choice that ignores saved zones and
no-go lines. Spot cleaning also runs without the saved map. Upgrading from
0.1.0 or 0.1.1 changes an old saved `house` default to Automatic, preserving
mode, navigation, spot dimensions and other settings. If mapless cleaning is
intended, select it again after upgrading. Resuming continues the existing run.

Returning to base during cleaning pauses the run, waits 3.5 seconds and checks
the robot's latest command readiness before sending it home. The sequence is
serialized so another action cannot interrupt the pause-to-home transition.
Some Generation 3 firmware keeps `goToBase` false after pausing. A fresh state
confirming an error-free, resumable paused cleaning run permits the home command
in that case, matching the app. Command acceptance does not prove docking;
the vacuum activity and docked sensor continue to show the reported robot state.

The vacuum's activity distinguishes cleaning and exploration from maintenance.
Log uploads, software updates, menus and stationary map transfers show docked
or idle; the Robot action sensor retains the precise phase.

The Error sensor and the Problem binary sensor ignore Generation 4
informational notices, such as charging on the base or returning to base
(severity `invalid`, which the Vacuula app labels "Info"). Warnings and errors
still count. The Generation 3 Alert sensor shows `none` while no alert is active.

Additional scalar diagnostics cover robot state, general information, cleaning
statistics, the current subrun, the latest dated run, and its latest subrun.
Numbers and text become diagnostic sensors; booleans become diagnostic binary
sensors. These extras are disabled by default and read-only. They keep raw
values without inventing units, exclude identity and credential fields, and
omit arrays and complete object dumps. Latest-run selection compares timestamps
as absolute instants and skips malformed or timezone-free records.

Map switches read the map's current name from Vacuula before changing its
enabled setting, so a rename made in the Vacuula app is kept. Schedule-event
switches change only `enabled`, preserving the existing days, time, and
complete cleaning command; their names include the days and time. New maps and
events add switches without reloading the integration. When a successful read
shows that a map or schedule event was deleted, its switch and map image are
removed; they come back if the record returns. Map, schedule-event and robot
names follow renames made in Vacuula without a reload.

Floor-map images are rendered locally as PNGs from stored floor grids. They do
not include a live path or editable boundary overlay. Maps and run history are
also available as structured action responses. Map geometry and history can
contain private information about the home.

Useful automations include cleaning when the home becomes empty, returning to
base before a quiet period, and displaying battery, connection status, and maps
on a dashboard. Use ordinary Home Assistant state triggers and conditions for
these entities. This integration provides no custom triggers or conditions.

## Actions

All ten `vacuula` actions take the robot's Home Assistant `device_id` in `data`.
They do not accept a robot serial or arbitrary protocol commands. IDs such as
`map_id`, zone IDs, and event IDs come from `vacuula.get_data` responses.
These advanced actions require an administrator when called by a signed-in
user. Home Assistant automations and scripts can call them with their managed
context. Standard vacuum entity controls use the usual entity permissions.

| Action | Fields besides `device_id` | Generation and behavior |
| --- | --- | --- |
| `vacuula.clean` | Optional `map_id`, `cleaning_type`, `zone_ids`, `mode`, `navigation`, `nogo_enabled` | Both. Starts cleaning, automatically using saved maps by default; zones require a map. Generation 3 accepts at most one zone. |
| `vacuula.set_schedule` | Required `events`; optional `time_basis` | Generation 3. Replaces every schedule event; `[]` clears them. Times default to Home Assistant local time. Does not enable the schedule. |
| `vacuula.update_schedule_event` | New event: required `enabled`, `days`, `time`. Existing event: `event_id` plus fields to change. | Generation 4. Adds an event or merges changes into a freshly read existing event. Omitted settings and zones are preserved. |
| `vacuula.delete_schedule_event` | Required `event_id` | Generation 4. Deletes one event. |
| `vacuula.set_boundaries` | Required `map_id`, `boundaries` | Both. Replaces the complete boundary list; `[]` removes all boundaries. Generation 4 re-promotes an active map after saving so the robot receives the new zones. Inactive maps stay inactive. Read and preserve existing boundaries first. |
| `vacuula.rename_robot` | Required `name` | Both. Changes the name stored by Vacuula. |
| `vacuula.update_map` | Required `map_id`, `name`, `enabled` | Both. Changes the map name; `enabled` applies to Generation 3 maps. |
| `vacuula.map_action` | Required `map_id`, `action` | Generation 4. `promote`, `use`, `unuse`, or `demote`; demotion removes active floorplan status. As in the Vacuula app, only a floorplan marked with `use` can be promoted, and the active floorplan must be demoted before `unuse`. |
| `vacuula.get_data` | Required `resource`; conditional `map_id` or `run_id` | Both. Returns `data` and requires a response variable. See resources below. |
| `vacuula.factory_reset` | Required `confirm: true` | Generation 4 only. Erases robot settings and may require provisioning again. `false` is rejected. |

Cleaning fields default to `cleaning_type: auto`, `mode: eco`, `navigation: normal`, and
`nogo_enabled: true`. Generation 3 modes are `eco` and `turbo`; navigation is
`normal`, `extra_care`, or `deep`. Generation 4 supports `normal` or `gentle`
navigation and, for applicable models, `auto` or `max` cleaning. At most 20 zones
can be supplied to a Generation 4 action. No-go selection is a Generation 4
cleaning option. Without `map_id`, `cleaning_type: auto` follows the same safe
map selection as the vacuum entity. `cleaning_type: map` requires a saved map;
`cleaning_type: house` explicitly requests mapless cleaning. A `map_id` selects
a specific map and cannot be combined with `cleaning_type: house`.

Generation 3 schedule events contain `mode` (`1` eco, `2` turbo), `day` (`0`
Sunday through `6` Saturday), `startTime` (`HH:MM`), and optional `boundaryId`.
`set_schedule` interprets these times in Home Assistant's configured timezone
and converts time and weekday to the robot's UTC clock. Use `time_basis: utc`
when supplying raw UTC events, including events read by `get_data`. The robot
stores fixed UTC weekly times: write a local-time schedule again after a DST
offset change to keep the same local clock time, matching Vacuula's behavior.
Returned events with a nested `boundary` object are accepted only when it has
an explicit valid `id`; that ID becomes `boundaryId`. Missing or conflicting
zone IDs reject the complete write before changing the schedule. The exact
zone response on real Gen3 hardware remains unverified.

Generation 4 events use `days` from `mon` through `sun` and `time: HH:MM` in the
robot's configured timezone. For new events, the default `cleaning_type: auto` uses the
current promoted floorplan with no-go protection, even if the robot is off
dock while the event is being created. Without a promoted floorplan it uses
whole-home cleaning. `cleaning_type: map` requires a promoted floorplan;
`cleaning_type: house` explicitly schedules mapless cleaning. The selected map
is stored in the event; it is not reselected when the schedule runs. Up to 100
Generation 3 schedule events are accepted.

With `event_id`, only supplied fields change. Omitted time, enabled state,
days, modes, navigation, zones, no-go settings and other stored options keep
their current server values. The integration reads and updates the event
under one robot lock; a missing or ambiguous event ID is rejected. Supplying
`mode`, `navigation` or `nogo_enabled` changes that setting across its runs.
An explicit `zone_ids` list replaces the zones; `zone_ids: []` selects the
whole floorplan. Selecting `cleaning_type: house` removes the mapped target.
`cleaning_type: auto` is rejected for an event that targets a map while no
floorplan is promoted, instead of silently dropping its map and zones.
When changing floorplans, an event with existing zones requires an explicit
`zone_ids` list for the new map, or `[]` for its whole floorplan.

An active Gen4 boundary update re-promotes the map. Vacuula demotes the previous
floorplan first, so a rejected or timed-out promotion can leave the robot
without an active floorplan or no-go protection. Promotion and active-map
boundary edits are refused while the robot is offline, before saving changes.
The robot firmware may also disable schedule events affected by a map change
(the Vacuula app warns about this), so the integration re-reads the schedule
after each promotion and the event switches show the result.

After a failed upload the integration keeps a recovery marker: a Repairs notice
names the robot and map ID, and new Automatic or mapped starts and mapped
schedule commands are blocked, so Automatic does not quietly clean without the
no-go zones you just edited. The marker clears when a floorplan is active again
(promoted here or in the Vacuula app), when you demote the map with
`vacuula.map_action` to continue without it, when the map is no longer in use,
or when the robot is removed. Vacuula reports a failed upload and a deliberate
demotion the same way, so a demotion made only in the Vacuula app does not
clear it; demote with the action instead. Explicit whole-home cleaning remains
available. Existing robot schedules and controls outside this integration are
not blocked. Robot commands are never retried automatically.

A boundary contains `id`, `name`, `color`, `type` (`polygon` or `polyline`),
`vertices`, and `enabled`. Values are accepted as the Vacuula app stores them,
so boundaries read with `get_data` can be written back: names may be empty,
colours are usually `#RRGGBB` but older values are kept, and vertices are
normalized `[x, y]` pairs that can sit slightly outside 0–1 where a point was
placed off the map. A polygon needs at least three vertices and a polyline two,
up to 1000. Generation 3 polygons use a normalized `relevancy` point; when it is
omitted it is computed as the average of the vertices, as the app does.
Generation 4 boundaries require `function: cleaning` or `function: nogo` and must
omit `relevancy`. An action accepts up to 100 boundaries.

`get_data` resources are `state`, `info`, `stats`, `preferences`, `schedule`, `maps`,
`runs`, `map`, `run`, and `boundaries`. Generation 3 also supports
`log_upload_state`. Generation 4 information uses `info.robot` where supported.
`map` and `boundaries` require `map_id`; `run` requires
`run_id`. Other resources need neither. Unsupported reads report an action error.
Other actions optionally return the server result under `response`.

Read map IDs before using them in an automation:

```yaml
action: vacuula.get_data
data:
  device_id: YOUR_ROBOT_DEVICE_ID
  resource: maps
response_variable: robot_maps
```

Clean one named zone after replacing the IDs with values from the server:

```yaml
action: vacuula.clean
data:
  device_id: YOUR_ROBOT_DEVICE_ID
  map_id: YOUR_MAP_ID
  zone_ids:
    - YOUR_ZONE_ID
  mode: eco
  navigation: normal
```

## Updates and availability

Server-sent events (SSE) track the robot list and state changes. Each connected
robot has its own event stream. An independent 60-second timer reads server and
robot state even while events are arriving. Optional details, preferences,
schedules, map lists, and run lists are refreshed about every five minutes.
Control commands read back state and only the setting or schedule they changed.
Map changes refresh the map list. Read-only `get_data` calls return their
requested resource without triggering other reads. Map PNGs are fetched on
demand and cached for five minutes. Optional reads with transient errors,
including 404 responses while idle, are retried on later detail polls; only
explicit unsupported-command responses disable a query until reload.

Event streams reconnect with backoff from 1 to 60 seconds. The independent
polling timer recovers missed events and discovers newly added robots. A
robot's connection follows Vacuula's robot list. A slow or failed read, such as
Vacuula's 30-second robot timeout, is logged but does not make the robot or its
controls unavailable. When Vacuula reports the robot disconnected, its live
controls and telemetry become unavailable while other robots keep working. Its
connection sensor and stored map images can remain available through the
server. A robot that Vacuula stops listing keeps its device and entities, shown
as unavailable, until it is listed again. The integration logs unanswered reads and recovery without printing
command payloads. Only the server poll and event stream decide whether the
server is reachable; a failed read during an outage never makes entities
available again with stale values. **Refresh data** reports an error when the
robot or its maps cannot be read.

Generation 3 map switches also remain available while the robot is offline
because their settings belong to the server. The cleaning-default selects and
spot sizes stay editable too, because they are stored in Home Assistant.
Schedule-event switches require a connected robot. Both kinds of switch read
back after changes; a change the server applied is not reported as failed when
only the follow-up read fails.

Robot commands are serialized per robot and sent once. Map selection and native
command-readiness checks happen inside the same lock as execution, so a queued
action is checked again when it executes. A timeout does not prove
that the robot ignored a command, so commands are never automatically retried.
After a successful command, the integration reads state back from the robot.

## Troubleshooting and removal

- **Cannot connect:** check the same direct server URL from the Home Assistant
  network. The app API port is distinct from robot-facing ports. Remove `/web`
  or an ingress path and avoid a proxy that redirects or requires a login.
- **No devices:** check that Vacuula lists a supported generation. A successful
  server connection with an empty robot list is allowed.
- **Robot unavailable:** confirm the robot is connected in Vacuula. Reconnecting
  the robot should restore its entities without reinstalling the integration.
- **Feature absent or rejected:** the model may not advertise it. Advanced
  mapping, scheduling, and settings support varies between models and firmware.
- **Map image missing:** a valid stored floor grid must be present on the server.
  Run maps, live robot paths, and boundary editing are not image features.
- **Server address changed:** use **Reconfigure** with the new origin. Zeroconf
  rediscovery can also update the URL when a known robot identifies the server.
- **Floorplan upload not confirmed:** check **Settings > System > Repairs** for
  the robot and map ID. Promote the floorplan again in Vacuula or with
  `vacuula.map_action`, or demote it with the action to continue without it.

Download integration diagnostics from **Settings > Devices & services** when
investigating a fault. Diagnostics use cached data only and omit the server URL,
robot identities, map geometry, and complete schedule contents. Review any data
before sharing it. `get_data` responses are intentionally richer than diagnostics.

To remove the integration, delete its entry in **Settings > Devices & services**.
To remove the custom code as well, remove **Vacuula** from HACS, or delete
`/config/custom_components/vacuula` if installed manually, then restart Home
Assistant. Removing the integration does not delete robots,
maps, or schedules from Vacuula. A robot no longer listed by the server can be
removed from Home Assistant's device registry. If it registers with Vacuula
again later, its device and entities are created again without a reload.

## Development

This custom integration is tested against Home Assistant **2026.10.2**. It has
no official Home Assistant quality rating. Platinum is the engineering target;
the remaining work is tracked in [the quality assessment](docs/quality-scale.md).

The HTTP/SSE client is an original implementation in `client.py`; no Vacuula app
or server implementation code is bundled. It uses Home Assistant's shared
`aiohttp` session. See [protocol notes](docs/protocol.md) for endpoints,
generation differences, validation limits, and source evidence.

Run checks in an environment with Python 3.14 and the pinned Home Assistant test
dependencies:

```sh
python -m pytest --cov=custom_components.vacuula --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
python -m mypy custom_components/vacuula
```

See [local validation results](docs/local-validation.md) and
[the quality assessment](docs/quality-scale.md) for outstanding release
requirements. No command, schedule change,
map edit, or factory reset is needed for read-only hardware validation.
