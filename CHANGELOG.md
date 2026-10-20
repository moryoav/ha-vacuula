# Changelog

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
