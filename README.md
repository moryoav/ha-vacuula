# Vacuula for Home Assistant

Local Home Assistant integration for Neato robots already connected to a
[Vacuula server](https://github.com/vacuula/vacuula). Home Assistant talks to the
same local app API as the Vacuula web interface. It needs no Neato account,
robot credentials, or cloud connection.

This is a local development integration, tested against Home Assistant
**2026.10.2**. It has no official Home Assistant quality rating. Platinum is the
engineering target; the remaining work is tracked in
[the quality assessment](docs/quality-scale.md).

## Requirements and installation

1. Set up Vacuula separately and confirm the robot appears connected in its web
   interface. This integration does not provision robots or install the server.
2. Copy `custom_components/vacuula` into Home Assistant's
   `/config/custom_components/vacuula` directory, then restart Home Assistant.
3. Open **Settings > Devices & services > Add integration**, then choose
   **Vacuula**. A server advertising `_vacuula._tcp.local.` can also appear as a
   discovered integration.
4. Enter the server's HTTP or HTTPS origin, for example
   `http://vacuula.local:81`, and submit. The hostname must resolve from Home
   Assistant; use the server's LAN address if it does not.

The only setup field is **Server URL**. Include the protocol and app API port;
omit `/web`, `/api`, ingress paths, credentials, query parameters, and fragments.
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
| Sensors | Battery, state, action, errors, reported battery details, saved map/run counts, schedule count, and last-run area, duration, timestamps, and status. Additional reported scalar telemetry is available as diagnostics disabled by default. |
| Binary sensors | Server-observed robot connection; charging, docked, and problem state; Generation 3 dustbin state; reported command readiness. Optional Generation 4 quickboost and cleaning-center telemetry and additional reported boolean diagnostics appear when present. |
| Buttons | Start/resume, stop, pause, return to base, locate, and refresh; supported Generation 3 spot cleaning, mapping exploration, dismiss alert, and diagnostic log upload. Log upload and other maintenance controls may be disabled by default. |
| Switches | Generation 3 robot sounds, schedule enabled state, and one enabled toggle per saved map; Generation 4 force-floorplan preference and one enabled toggle per existing schedule event. |
| Selects | Cleaning mode and navigation defaults; Generation 3 house/spot default when spot cleaning is supported; Generation 4 language from the robot's advertised choices. |
| Numbers | Generation 3 bin reminder (30–150 minutes), filter reminder (30–90 days), brush reminder (120–240 days), and supported spot width/height (100–400 cm). |
| Images | One locally rendered PNG per stored floor map. |

Use the vacuum entity for starting or resuming cleaning, pausing, stopping,
returning to base, locating the robot, and selecting a cleaning mode. Additional
controls cover generation-specific schedules, preferences, spot cleaning,
mapping, and maintenance actions. Cleaning defaults stored by Home Assistant do
not start a run when changed.

Additional scalar diagnostics cover robot state, general information, cleaning
statistics, the current subrun, the latest dated run, and its latest subrun.
Numbers and text become diagnostic sensors; booleans become diagnostic binary
sensors. These extras are disabled by default and read-only. They keep raw
values without inventing units, exclude identity and credential fields, and
omit arrays and complete object dumps. Latest-run selection compares timestamps
as absolute instants and skips malformed or timezone-free records.

Map switches keep each map's stored name when changing its enabled setting.
Schedule-event switches change only `enabled`, preserving the existing days,
time, and complete cleaning command. New maps and events add switches without
reloading the integration. Removed records make their switches unavailable.

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
| `vacuula.clean` | Optional `map_id`, `zone_ids`, `mode`, `navigation`, `nogo_enabled` | Both. Starts cleaning; zones require a map. Generation 3 accepts at most one zone. |
| `vacuula.set_schedule` | Required `events` | Generation 3. Replaces every schedule event; `[]` clears them. Does not enable the schedule. |
| `vacuula.update_schedule_event` | Required `enabled`, `days`, `time`; optional `event_id` and cleaning fields | Generation 4. Adds an event or updates an existing ID. |
| `vacuula.delete_schedule_event` | Required `event_id` | Generation 4. Deletes one event. |
| `vacuula.set_boundaries` | Required `map_id`, `boundaries` | Both. Replaces the complete boundary list; `[]` removes all boundaries. Read and preserve existing boundaries first. |
| `vacuula.rename_robot` | Required `name` | Both. Changes the name stored by Vacuula. |
| `vacuula.update_map` | Required `map_id`, `name`, `enabled` | Both. Changes the map name; `enabled` applies to Generation 3 maps. |
| `vacuula.map_action` | Required `map_id`, `action` | Generation 4. `promote`, `use`, `unuse`, or `demote`; demotion removes active floorplan status. |
| `vacuula.get_data` | Required `resource`; conditional `map_id` or `run_id` | Both. Returns `data` and requires a response variable. See resources below. |
| `vacuula.factory_reset` | Required `confirm: true` | Generation 4 only. Erases robot settings and may require provisioning again. `false` is rejected. |

Cleaning fields default to `mode: eco`, `navigation: normal`, and
`nogo_enabled: true`. Generation 3 modes are `eco` and `turbo`; navigation is
`normal`, `extra_care`, or `deep`. Generation 4 supports `normal` or `gentle`
navigation and, for applicable models, `auto` or `max` cleaning. At most 20 zones
can be supplied to a Generation 4 action. No-go selection is a Generation 4
cleaning option.

Generation 3 schedule events contain `mode` (`1` eco, `2` turbo), `day` (`0`
Sunday through `6` Saturday), `startTime` (`HH:MM`), and optional `boundaryId`.
Generation 4 events use `days` from `mon` through `sun` and `time: HH:MM` in the
robot's configured timezone. Up to 100 Generation 3 schedule events are accepted.

A boundary contains `id`, `name`, `color` (`#RRGGBB`), `type` (`polygon` or
`polyline`), `vertices`, and `enabled`. Vertices are normalized `[x, y]` pairs
between 0 and 1, with at least three for a polygon or two for a polyline, and at
most 100. Generation 3 polygons also require a normalized `relevancy` point.
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
schedules, map lists, and run lists are refreshed about every five minutes and
after commands. Map PNGs are fetched on demand and cached for five minutes.

Event streams reconnect with backoff from 1 to 60 seconds. The independent
polling timer recovers missed events and discovers newly added robots. A robot
losing its connection makes its live controls and telemetry unavailable while
other robots keep working. Its connection sensor and stored map images can
remain available through the server. The integration logs connection loss and
recovery without printing command payloads.

Generation 3 map switches also remain available while the robot is offline
because their settings belong to the server. Schedule-event switches require a
connected robot. Both use readback after changes.

Robot commands are serialized per robot and sent once. A timeout does not prove
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

Download integration diagnostics from **Settings > Devices & services** when
investigating a fault. Diagnostics use cached data only and omit the server URL,
robot identities, map geometry, and complete schedule contents. Review any data
before sharing it. `get_data` responses are intentionally richer than diagnostics.

To remove the integration, delete its entry in **Settings > Devices & services**.
To remove the custom code as well, delete `/config/custom_components/vacuula`
and restart Home Assistant. Removing the integration does not delete robots,
maps, or schedules from Vacuula. A robot no longer listed by the server can be
removed from Home Assistant's device registry.

## Development

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
