# Local app protocol

This integration communicates with the local Vacuula app API. It does not speak
the robot's provisioning, cloud, or TLS transport protocols directly. The client
is an independent asynchronous implementation of the observed HTTP interface.
Vacuula implementation code is not included.

## Evidence and compatibility

The reference is the Vacuula source snapshot associated with revision `bf18585`,
reviewed on 2026-10-19. Relevant files are `app/src/http.ts`,
`app/src/http/gen3.ts`, `app/src/http/gen4.ts`, the app's generation-specific
types, and `server/src/vacuula_server/handlers/{app,events,route_matcher}.py`.
The [upstream project](https://github.com/vacuula/vacuula) remains the authority
for its own protocol; these notes describe the reviewed interface, not a stable
API guarantee.

D5 state, telemetry, Locate, Start, Pause, Resume, Stop, spot cleaning, return
to base, preference changes and Gen3 schedule writes were exercised on the live
robot; see the [local validation](local-validation.md).
Generation 4 behavior is source-verified and fixture-tested, without hardware
verification. Generation 3 multi-boundary cleaning was explicitly questioned in
the app source; this integration accepts only a single `boundaryId`.

## HTTP and events

All paths below are relative to the configured origin. IDs occupy one URL path
segment. Timestamp colons remain literal because the reviewed server matches
raw paths without decoding them; slash, query, fragment and percent delimiters
remain escaped. Real HTTP tests check the raw paths for timestamp map/run IDs.
The integration accepts only a plain HTTP or
HTTPS origin without user information, a path prefix, query, or fragment.
Requests do not follow redirects. The direct app API requires no authentication;
there is no integration support for an authenticated reverse proxy.

| Method | Path | Purpose and response |
| --- | --- | --- |
| GET | `/api/server` | Server facts as an object, including version and timezone. |
| GET | `/api/robots` | Object keyed by robot serial, with generation, name, and connection status. |
| GET | `/api/events` | SSE stream; `robots` events contain complete robot snapshots. |
| GET | `/api/robot/{serial}/connect` | Per-robot SSE stream with event objects containing `serial` and `data`. |
| POST | `/api/robot/{serial}/command` | Send a Generation 3 `cmd` or Generation 4 `ability` object and await its response. |
| POST | `/api/robot/{serial}` | Update the server-side robot name with `{name}`. |
| GET | `/api/robot/{serial}/maps` | List stored maps. |
| GET | `/api/robot/{serial}/maps/{map_id}` | Read floor-grid data and map metadata. |
| POST | `/api/robot/{serial}/maps/{map_id}` | Update `{name, enabled}`; Generation 4 uses the name. |
| GET | `/api/robot/{serial}/runs` | List recorded cleaning runs. |
| GET | `/api/robot/{serial}/run/{run_id}` | Read one stored run map. The path uses singular `run`. |
| GET | `/api/robot/{serial}/maps/{map_id}/boundaries` | Read Generation 4 boundaries. Generation 3 uses `getMapBoundaries`. |
| POST | `/api/robot/{serial}/maps/{map_id}/boundaries` | Replace Generation 4 boundaries with an array. |
| POST | `/api/robot/{serial}/maps/{map_id}/{action}` | Generation 4 `promote`, `use`, or `unuse`. |
| DELETE | `/api/robot/{serial}/maps/{map_id}` | Generation 4 floorplan demotion. |

Metadata writes can return literal `OK` rather than JSON. Reads validate that
the response has the expected object or list shape. Generation 3 command replies
must include `result: ok`; Generation 4 replies must include an integer success
`code`. Robot-level errors are checked even when HTTP status is 200. A response
`body` is unwrapped for Generation 4; Generation 3 `data` is merged with root
state fields so battery and action values are preserved.

SSE supports LF, CRLF, and CR line endings, comments, a UTF-8 BOM, network chunks
that divide frames, and multiline `data` fields. Server robot snapshots are
filtered by the `robots` event name. A robot stream is subscribed before its
initial state read, avoiding a gap between the read and subscription. Generation
3 state events use `type: state` or `eventId: 77`. Generation 4 state messages
identify `peer.state` in `to.service`. Like the app (`AppContext.tsx`), the
integration reads Generation 4 errors only from `peer.state` and state reads and
ignores `robots.error_codes` events, which also carry informational notices.
Error entries with severity `invalid`, labelled "Info" by the app (for example
`charging_base` or `return_to_base`), are not reported as problems.

A robot's connection follows the server's robot list. A failed or slow robot
read, such as the server's 504 after its 30-second robot wait, is logged but does
not mark the robot disconnected. Entity notifications after partial reads do not
mark the coordinator healthy; only the server poll and the server event stream
decide whether the server is reachable.

## Generation command mapping

The integration exposes validated entity controls and ten named Home Assistant
actions. It intentionally does not offer an arbitrary raw-command action.
The ten advanced actions use Home Assistant's administrator action registration;
signed-in callers must be administrators. Standard vacuum controls follow normal
entity permissions, and Home Assistant-managed automations/scripts can call the
advanced actions.

| Operation | Generation 3 `cmd` | Generation 4 `ability` |
| --- | --- | --- |
| State | `getRobotState` | `state.show` |
| Information | `getGeneralInfo` | `info.robot` |
| Cleaning statistics | `getLocalStats` | `cleaning.show` |
| Preferences | `getPreferences`, `setPreferences` | `settings.cleaning_options`, `settings.set_cleaning_options` |
| Robot language | Not exposed as a Generation 3 language control | `settings.robot_language`, `settings.set_robot_language` |
| Start | `startCleaning` | `cleaning.start` |
| Pause / resume | `pauseCleaning`, `resumeCleaning` | `cleaning.pause`, `cleaning.resume` |
| Stop | `stopCleaning` | `cleaning.cancel` |
| Return to base | `sendToBase` | `navigation.return_to_base` |
| Locate | `findMe` | `utilities.find_me` |
| Read schedule | `getSchedule` | `scheduling.show` |
| Write schedule | `setSchedule` | `scheduling.update`, `scheduling.delete` |
| Schedule enabled | `enableSchedule`, `disableSchedule` | Per-event `enabled` |
| Saved map enabled | App HTTP map update with unchanged name | Not exposed as an enabled switch |
| Map boundaries | `getMapBoundaries`, `setMapBoundaries` | App HTTP map endpoints |
| Explore / map | `startPersistentMapExploration` | Not exposed as a separate mapping command |
| Dismiss alert | `dismissCurrentAlert` | Not exposed |
| Upload logs / upload state | `uploadLogs`, `getLogsUploadState` | Not exposed |
| Factory reset | Not exposed | `settings.factory_reset`; explicit action confirmation required |

Generation 3 cleaning uses `params.category` (`2` house, `3` spot, `4` map),
`mode` (`1` eco, `2` turbo), and `navigationMode` (`1` normal, `2` extra care,
`3` deep). Zone cleaning adds `mapId` and a single `boundaryId`. Spot cleaning
adds dimensions. Generation 4 `cleaning.start` uses `runs`, each containing
`settings` and either `map: null` or a map object with `rank_id`, `track_id`, and
`nogo_enabled`.

New runs use Automatic map selection by default. Before selecting a map the
integration refreshes the stored map list. While docked with an active map, Gen3 uses category
`4` and lets the robot match its dock against enabled maps, as the app does.
Gen4 uses the unique promoted floorplan ID with `nogo_enabled: true`. A saved
map is used only when enabled (Gen3) or promoted (Gen4). Automatic falls back
to mapless whole-home cleaning when no map is active or the robot is off dock,
matching `home.tsx`. The Saved map default (`cleaning_type: map` without a
`map_id`) rejects these conditions. An explicit map-ID action supplies that ID
directly to the robot rather than running automatic active-map/dock selection.
Malformed maps, ambiguous promoted maps and failed reads remain errors.
Explicit house and spot controls remain mapless.

Returning to base pauses a pausable run, waits 3.5 seconds and re-reads command
readiness before sending the docking command. The entire sequence holds the
robot lock; failed pause, state read or cancellation prevents the later command.
When `goToBase` (Gen4 `return_to_base`) is still false, the command is refused
and the run stays paused. The Vacuula app (`home.tsx`) disables its home button
for a robot that is already paused in that state, but after its own pause it
sends `sendToBase` without checking the flag again. On the live D5, `sendToBase`
in that state was acknowledged but ended the run idle off the dock, with
`dockHasBeenSeen: false` each time, so the integration follows the flag; the
refusal names the missing base when the robot is off its dock.
No activity or docking result is inferred from an acknowledgement.

The D5 sends a state event when its state changes or when it touches the dock,
but not when command flags or the action change within a run. Pause becomes
available and charging ends a few seconds after it leaves the dock, and a robot
sent home switches from recovering location (action 9) to docking (action 4)
without an event. The integration therefore confirms a Pause, Stop or Return
to base refusal based on cached readiness with one fresh state read, and reads
state again 10 seconds after a start, resume, exploration or return to base and
after any event that reports the robot busy. It also reports action 9 as
returning after it sent a Gen3 robot home, until the robot's state shows the
trip ended; the trip is marked before `sendToBase` because the robot's event
usually arrives before the command returns.

Generation 3 preference writes read and merge the full preferences object under
the same lock as the write before
applying changed values. This avoids dropping unrelated preference fields.
Generation 4 settings changes use the operation and keys defined by the app.
Home Assistant cleaning defaults are stored in config-entry options and do not
send a robot command until a cleaning action is requested. Each options change
uses a new nested snapshot so Home Assistant invalidates its serialized entry
and saves every change. Entry minor version 2 migrates old `house` defaults to
`auto`; new explicit mapless choices are preserved.

Gen3 `setSchedule` stores UTC weekly times. The integration converts local
inputs using Home Assistant's configured timezone and the current UTC offset,
including weekday rollover, matching the app's `utils/clockTime.ts` behavior.
`time_basis: utc` accepts raw events without conversion. `get_data` keeps raw
UTC schedule responses. A DST offset change requires rewriting local schedules.
The source's Gen3 `ScheduleEvent` type declares `boundaryId`, but its renderer
reads `event.boundary.name`; no captured zone schedule establishes the robot's
exact response. The integration accepts a nested boundary only with a valid
explicit `id`, normalizes it to `boundaryId`, and rejects unresolved or
conflicting IDs before replacing the schedule. This compatibility shape is
fixture-tested, not a claim of hardware verification.

Gen4 schedule creation defaults to the unique promoted floorplan and enabled
no-go protection. Current docking does not affect this future cleaning command.
No promoted map means whole-home cleaning, unless explicit mapped cleaning was
requested. Explicit `cleaning_type: house` stores mapless cleaning. The map ID
is selected when writing the event; the robot does not reselect it at run time.
With `event_id`, the integration reads `scheduling.show`, locates exactly one
event, merges only supplied fields, then writes `scheduling.update` under the
same robot lock. Unknown event/command/run fields and omitted settings survive.
Mode, navigation and no-go edits apply across runs. Explicit zone lists replace
zones; an empty list means the whole floorplan. Event-enabled switches use the
same fresh read/merge/write path so a stale cache cannot revert an app edit.
Changing floorplans with existing zones requires an explicit zone list so IDs
from the previous floorplan are neither carried over nor silently removed.

Gen4 HTTP boundary replacement stores data on the server. For an already
promoted map, the integration saves then re-promotes it under one robot lock so
the robot receives updated zones. Fresh map metadata determines active status;
inactive maps stay inactive. Save failure prevents promotion, and promotion
failure is reported even though the server may already have stored boundaries.
The server demotes the active floorplan before upload; failure can therefore
remove active no-go protection. Offline promotion is refused before creating
a pending marker; active boundary edits are refused before saving boundaries.
Before an upload, a pending marker records uncertainty across reload/restart.
The robot firmware may disable schedule events affected by a map change (the
app's `gen4UpdateBugHint`), so `scheduling.show` is re-read after every
promotion attempt.

The read API has no field that tells a failed upload from a deliberate
demotion: both leave the floorplan `promoted=False`. After this integration's
own upload fails, a demoted floorplan therefore keeps the pending marker, a
Repairs notice naming the robot and map ID, and the block on new Automatic or
mapped starts and mapped schedule commands (switching a mapped event on
included; switching one off stays allowed), so Automatic does not quietly run
without the edited no-go zones. Fresh map reads clear it when a floorplan is
promoted again (here or in the app), another floorplan is active, or the map is
no longer in use; demotion through `map_action` and robot removal also clear it.
A demotion made only in the app is indistinguishable from the failure, so the
notice asks the user to demote with the action instead. Reads use the robot lock
so an in-progress upload cannot be reconciled against stale pre-upload status.
No commands are retried or rolled back automatically. Explicit house
cleaning/schedules, existing robot schedules and outside controls remain
available. Unreadable status invalidates the stale promoted-map cache.

The server lists only floorplans marked for use (`in_maps_list`), and new
floorplans arrive unmarked. As in the app (`map.tsx`), `promote` requires a
floorplan in the list, and `unuse` is refused for the promoted floorplan;
either would otherwise hide the robot's active floorplan from Automatic.

Generation 3 map-enabled switches use the app's map update endpoint, which
writes the name and the flag together, so they read the current map list under
the robot lock and send the stored name. Generation 4 event-enabled switches send
`scheduling.update` with the complete existing event and only its `enabled`
value changed. Both discover new records dynamically and read back changes; a
change the server applied is not reported as failed when only the follow-up read
fails. When a successful read no longer lists a map or event, its entities are
removed and can return later. Map metadata can be updated while a robot is
disconnected.

Additional scalar diagnostic entities traverse `state`, `info`, `stats`,
`active_run`, `last_run`, and `last_subrun`. The latter two select the latest
record with a valid timezone-aware start timestamp. Booleans are exposed as
binary sensors; finite numbers and bounded text become sensors without inferred
units. These entities are disabled by default. Traversal limits nesting depth,
omits arrays and identity/credential keys, and rejects strings containing URL,
email, or certificate markers. Path segments are independently encoded in entity
keys to avoid collisions between nested fields.

## Timing, safety, and privacy

The coordinator owns one server SSE task, one task per connected robot, and an
independent 60-second polling timer. Details refresh every 300 seconds. Control
commands read back state and only the setting or schedule they changed; map
operations read back maps, and read-only data actions issue no extra queries.
Event reconnect delay doubles from 1 second to a 60-second cap.
Only explicit unsupported-command replies are cached for the entry lifetime;
HTTP 404/405 and other transient robot responses are retried on later polls.
Unloading cancels and awaits all event tasks and removes the timer; Home
Assistant retains ownership of the shared HTTP session.

Normal requests have a 45-second total timeout, a 10-second connect timeout, and
a 40-second socket-read timeout. Map promotion uses 90 seconds total and an
80-second socket-read timeout to exceed the server's 60-second robot wait.
SSE has no total timeout, a 10-second connect
timeout, and a 45-second socket-read timeout. JSON bodies are limited to 16 MiB
and SSE frames to 2 MiB. Invalid JSON and non-finite JSON numbers are rejected.

Robot commands are serialized per robot. Cleaning and schedule payload builders
refresh and select maps inside the lock before sending; native control readiness
and connection checks also run after acquiring it, and a readiness refusal is
confirmed with one fresh state read. This prevents a completed
promotion or earlier command from being followed by a stale queued target or
readiness decision. Commands are never retried automatically,
including after a timeout, because execution might already have occurred.
Successful commands trigger a state readback. Map writes first read the map
under the target robot to check ownership. Action schemas validate identifiers,
names, generations, settings, and geometry before sending a request. Boundary
schemas accept what the app stores (empty names, older colour strings, finite
vertices slightly outside 0–1) so `get_data` output can be written back, and a
missing Generation 3 `relevancy` is the vertex average, as in the app's
`calcRelevancy`. Boundary
and schedule replacement actions can remove existing data; the factory-reset
action requires the literal boolean `confirm: true` and Generation 4.

Map images validate dimensions, RLE runs, and a maximum of 4,194,304 pixels;
rendering runs in Home Assistant's executor. No external image service is used.
Diagnostics are built from cached, allowlisted telemetry, redact identities and
URLs, include reviewed `details` and `meta` fields such as charge, docking and
firmware, and omit map geometry, run contents, and complete schedule events.
Structured `get_data` responses are intended for local automation and can
contain richer private data.
