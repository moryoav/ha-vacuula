# Changelog

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
