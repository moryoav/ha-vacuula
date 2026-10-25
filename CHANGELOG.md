# Changelog

## 0.1.13

- Kept a working server address when zeroconf rediscovers the server. The advertised hostname, an HTTPS reverse proxy and an address set with Reconfigure are no longer replaced with the plain-HTTP IP address. A missing, malformed or unreachable address is still updated, and HTTPS is never replaced with plain HTTP.
- Recognized every Neato dustbin code. Dustbin missing and Dustbin full now also follow `ui_error_dust_bin_missing`, `ui_alert_dust_bin_full` and the full-bin errors reported when the robot stops.
- Treated Vacuula's 504 robot timeout on Return to base as uncertain delivery instead of a rejection. A Generation 3 robot that drives home after the timeout shows Returning, and a repeated Home press no longer pauses it and sends Home again.
- Reused map images until the map changes, for at most an hour, instead of downloading and rendering the floor grid on every five-minute image token rotation. The image's last-updated time now changes only when the picture does.
- Added a setup menu when servers are discovered: use a discovered server, or enter an address such as a hostname or an HTTPS proxy.
- Moved action names, descriptions and field labels into translations, and gave every action dropdown readable option labels.
- Removed an unreachable Return to base button command and merged two identical reply-unwrapping helpers.

## 0.1.12

- Preserved newer server events when an older HTTP robot-list reply finishes, preventing stale connection flags, names and removal decisions from clearing map recovery.
- Confirmed fresh state before advanced cleaning chooses a map. Required docked telemetry for explicit map or zone commands and refused Automatic fallback with an active map and unknown docking status, while preserving the known off-dock fallback and future schedules.
- Rejected duplicate boundary IDs before any network request, preventing silent loss of regions during a complete boundary replacement.
- Preserved Gen3 home tracking when Stop or a new task is rejected, cancelled or loses its response. Applied new-task intent only after acknowledgement and waited for terminal telemetry after Stop.
- Validated optional language lists and filtered dynamic diagnostic text on every update, preventing malformed choices, crashes and later disclosure of private-looking text.
- Reported Gen4 overall cleaning completion from its completion flag, so a successful final zone cannot conceal an incomplete job.

## 0.1.11

- Kept interrupted Gen4 map requests guarded until their server handlers are known to have ended, including delayed boundary commits, late promotions, reloads and external edits. Added server restart detection and an explicit restart-confirmation fallback; skipped redundant metadata and boundary writes.
- Confirmed fresh positive Home readiness and refused missing permission before docking. Preserved bounded home intent across delayed movement, pause/resume, lost acknowledgements and cancellation. Avoided duplicate Home commands and cleared old intent when a new task is explicitly selected.
- Preserved push updates received during an in-flight state read so an older HTTP reply cannot erase newer readiness or activity information.
- Disabled implicit HTTP retries for map DELETE requests and distinguished unsent connection failures from ambiguous delivery, avoiding false recovery blocks. Removed reconstructed recovery notices when their entry is deleted.

Upgrade note: existing uncertain Gen4 map operations require a confirmed Vacuula server restart before recovery writes, followed by successful promotion or deliberate demotion. Restarting Home Assistant alone cannot stop Vacuula's unfinished requests. Entries without pending recovery markers upgrade normally.

## 0.1.10

- Confirmed fresh state before Start chooses between resuming and starting a new run, and allowed connected command buttons to reach live readiness validation instead of refusing stale flags.
- Kept new Generation 4 boundary edits guarded before the save request, including lost replies, rejected promotions, restarts and failed attempts to promote another map. Confirmed unchanged failed saves clear only their own new guard.
- Cleared the Generation 3 returning marker after confirmed terminal states while retaining it through delayed movement after an acknowledged home command.
- Updated device firmware and model metadata after successful robot reads without reloading or changing user device names.

## 0.1.9

- Stopped sending a Generation 3 robot home while it reports `goToBase: false`. On the D5 that command ended the run where the robot stood instead of docking it. The run now stays paused, and the error says when the robot has not found its base during the run.
- Pause, Stop and Return to base are no longer refused for up to a minute after a start. A refusal based on cached readiness is now checked once against a fresh state read, and the state, including button availability, is read again 10 seconds after the robot starts driving, also for runs started by the app, a schedule or the robot.
- Showed Returning for the whole trip after Home Assistant sends a Generation 3 robot home, instead of Cleaning while the robot recovers its location.
- Showed Normal in the Navigation mode select until a default is chosen, matching what Start sends.
- Kept the Generation 4 robot language select working when only its language read fails.
- Kept map image and map switch names while the map list is temporarily unknown.
- Allowed turning a mapped Generation 4 schedule event off during floorplan recovery, and blocked switching one on with its switch.
- Clearer errors for unknown schedule-event IDs, whole-home cleaning combined with a map or zones, and reminder values between the allowed steps.

## 0.1.8

- Bundled official Vacuula branding as transparent 256- and 512-pixel icons, with automatic logo and dark-theme fallbacks in Home Assistant.
- Preserved upstream artwork, attribution and licensing, and documented reproducible image rendering and local verification.
- Added the Vacuula artwork to the README and updated the local branding quality checklist.

## 0.1.7

- Documented every Python module, class, function and method, including test fixtures, constructors, private helpers and nested callbacks.
- Enforced 100% docstring coverage with a dependency-free AST audit, regression tests and GitHub checks; empty docstrings and unreadable or invalid source fail validation.
- Added continuous lint, formatting, strict typing, test and Hassfest checks.

## 0.1.6

- Allowed a freshly verified, resumable Generation 3 run to return home despite a false `goToBase` flag, matching the app sequence and the D5's accepted command; aligned button and readiness state.
- Selected cleaning and schedule map targets under the same robot lock as their command, preventing a queued promotion from being followed by a stale mapless start.
- Rechecked robot connection and native command readiness after queued actions acquire the robot lock, including spot cleaning and exploration.
- Removed deleted map and schedule-event entities even when disabled, preserving their disabled preferences if records return.
- Rejected malformed map identifiers during image discovery.
- Preserved config-entry unique IDs when reconfiguration uses an address held by an ignored discovery.
- Rejected JSON exponent overflow in HTTP and event responses before non-finite values reach Home Assistant.

## 0.1.5

- Let Vacuula's robot list decide connection status, so one slow or failed robot read no longer makes the robot unavailable or silently drops stop, pause and return-to-base.
- Stopped failed reads from making entities available again during a server outage; **Refresh data** now reports failed reads.
- Stopped treating Generation 4 informational notices, such as charging or returning to base, as problems, and ignored `robots.error_codes` events, as the Vacuula app does.
- Created the device and entities again when a removed robot registers with Vacuula again; a robot that Vacuula stops listing keeps its entities, shown as unavailable.
- Required `use` before promoting a Generation 4 floorplan and refused `unuse` for the promoted floorplan, as the app does.
- Kept the recovery block and Repairs notice after a failed floorplan upload until a floorplan is promoted again, the map is no longer in use, or it is demoted with `map_action`; the schedule is now read again after every promotion attempt.
- Kept map names changed in the Vacuula app when a map switch is toggled; map, schedule-event and robot names now follow renames without a reload, and event names include their days.
- Removed the entities of deleted maps and schedule events; they return if the record returns.
- Accepted boundaries saved by the Vacuula app (empty names, older colours, points slightly outside the map) and computed a missing Generation 3 relevancy.
- Kept the cleaning-default selects and spot sizes editable while the robot is offline.
- Created the Generation 3 Alert sensor at setup and showed `none` when no alert is active.
- Refused `update_schedule_event` with `cleaning_type: auto` on a mapped event while no floorplan is promoted, instead of dropping its zones.
- Changed the default address to `http://vacuula.local` (port 80, Vacuula's default).
- Kept two servers behind one reverse-proxy address separate, avoided a unique-ID clash with an ignored discovery when a server's address changes, and stopped reporting an applied map change as failed when only the follow-up read fails.
- Translated the server-unavailable setup message and removed an unused string and icon.

## 0.1.4

- Refused offline floorplan promotion and active-map boundary edits before saving changes or creating recovery notices.
- Reconciled recovery notices and blocks with fresh map status, including changes made in the Vacuula app, inactive maps and removed maps or robots.
- Identified the affected robot and map ID in floorplan repair notices; retained recovery guards only while map status is unknown.
- Merged existing Generation 4 schedule-event changes with fresh server data, preserving omitted settings, zones and per-run options.
- Serialized schedule reads, edits and event-switch updates so stale cached data cannot overwrite newer app changes.

## 0.1.3

- Matched Automatic cleaning to the app's whole-home fallback when maps are inactive or the robot is off dock, while retaining explicit mapped cleaning.
- Kept manual address entry available when discovery finds only configured servers or advertises an unreachable address.
- Defaulted Generation 4 schedule events to the promoted floorplan with no-go protection, independent of current docking.
- Added persistent recovery notices and map-status readback after failed active-floorplan uploads, with guarded Automatic/mapped actions until confirmed recovery.
- Distinguished maintenance, software updates, menus and stationary map transfers from cleaning activity.
- Preserved explicit zone IDs in Generation 3 schedule round trips and rejected missing or conflicting nested boundary IDs before replacing a schedule.

## 0.1.2

- Fixed timestamp map and run IDs in raw HTTP paths and extended map-promotion timeouts beyond the server's 60-second wait.
- Fixed cleaning-default persistence across repeated changes and Home Assistant restarts.
- Changed new cleaning runs to use saved maps and no-go lines by default; added explicit map and mapless choices and migrated old house defaults to Automatic.
- Added serialized pause-before-docking behavior and matching return-to-base button and readiness availability.
- Converted Generation 3 schedule inputs from Home Assistant local time to UTC, with an explicit UTC option and weekday rollover handling.
- Re-promoted active Generation 4 maps after saving boundaries so robot zones are updated.
- Fixed busy vacuum activity states, transient optional-read retries, and empty-server discovery across hostname and IP aliases.
- Included allowlisted operational details and firmware metadata in cached diagnostics.
- Replaced full command refreshes with targeted readbacks and removed readback fan-out from read-only data actions.

## 0.1.1

- Added automatic local server discovery when starting setup through Add integration.
- Added server selection when multiple Vacuula servers are discovered.
- Kept address entry as a fallback when no server is found.
- Preserved the discovery confirmation screen when a connection needs retrying.

## 0.1.0

- Added the initial local Home Assistant integration, entities, actions, diagnostics and tests.
