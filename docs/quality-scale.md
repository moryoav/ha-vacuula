# Quality assessment

**Target: Platinum. Official rating: none.** This is a local custom integration,
not a Home Assistant Core integration or an approved quality-tier submission.
No tier is declared in the manifest. A working feature or passing local test
suite alone does not establish an official rating.

This assessment follows the current
[Home Assistant quality scale](https://contributors.home-assistant.io/core/quality-scale/),
reviewed on 2026-10-19. The adjacent
[`quality_scale.yaml`](../custom_components/vacuula/quality_scale.yaml) lists all
54 rules. `done` records inspected local implementation evidence, `todo` records
unfinished work or missing verification, and `exempt` gives a specific reason.
These statuses are a development checklist, subject to independent review.

## Implemented foundations

| Area | Evidence |
| --- | --- |
| Local asynchronous transport | `client.py` uses asynchronous HTTP and SSE with the Home Assistant-managed `aiohttp` session injected by setup and the config flow. |
| Setup | `config_flow.py` searches for advertised servers during manual setup, validates the server and robot list, normalizes URL origins, prevents duplicate URLs or robots, supports Zeroconf and reconfiguration, and updates known server URLs on rediscovery. `discovery.py` uses the shared Home Assistant mDNS instance with bounded browsing and complete task cleanup. |
| Lifecycle | Typed config-entry runtime data owns the coordinator. Setup performs a first refresh; unload removes the polling timer and cancels and awaits streams. |
| Updates | Independent 60-second polling, server and per-robot event streams, five-minute optional detail/resource caches, targeted command readbacks, per-robot locks, and bounded reconnect backoff. Transient optional-read errors do not disable later polls. Robot connection follows the server's robot list, and only the server poll and event stream decide availability. |
| Entities | Named controls plus optional scalar diagnostics for state, information, statistics, current and latest runs/subruns. Extra telemetry is read-only and disabled by default. Maps and schedule events dynamically add configuration switches that preserve existing names or commands, follow renames, and are removed when the record is deleted. |
| Actions | Ten globally registered administrator actions, explicit schemas and generation checks, translated errors, safe mapped-cleaning defaults, atomic pause/dock and Gen4 boundary upload sequences, local-to-UTC Gen3 schedules, no raw command passthrough, and no automatic command retries. |
| Diagnostics | Cached allowlisted telemetry including reviewed details/meta fields; server URL, robot identities, maps, and complete schedule contents are excluded. No diagnostic network requests. |
| Repairs | A persisted marker records an unconfirmed Gen4 floorplan upload. Offline promotions are refused before writes. After a failed upload, the guard and a notice naming the robot and map ID stay until a floorplan is promoted again (here or in the app), the map leaves use, or it is demoted with `map_action`. |
| Documentation | README includes installation/removal, setup fields, entities, all actions, examples, data updates, supported generations, limitations, and troubleshooting. Protocol notes identify the reviewed API and validation limits. All Python definitions have docstrings, with 100% presence coverage enforced by a repository-wide AST audit in pytest and GitHub checks. |

## Verification boundary

The test environment pins **Home Assistant 2026.10.2**, Python 3.14, and
`pytest-homeassistant-custom-component` in `requirements_test.txt`. Tests cover
the client, config flow, coordinator, integration lifecycle, actions, images,
diagnostics, and regression cases. The [local validation](local-validation.md)
records 711 passing tests, 99.50% combined line and branch coverage, at least
97.12% in every module, 99.32% config-flow coverage, and clean strict mypy
results for all 19 integration modules. Local coverage rules are marked `done`.
The strict-typing rule remains `todo` for separate client packaging and formal
Core registration.

D5 hardware checks include telemetry, Locate, Start, Pause, Resume and a short
cleaning run followed by Home Assistant's Return to base action. Fresh reads
confirmed automatic docking after the pause-then-home sequence. Preference
changes, schedule writes, map changes, log uploads and factory reset were not
exercised on hardware. Generation 4 remains source-verified and fixture-tested.
The local test suite uses mocked transports and does not contact the robot.

## Work before a formal claim

| Requirement | Remaining work |
| --- | --- |
| Dependency transparency | Extract the independent protocol client into its own typed package, publish it on PyPI with public source and build/release CI, and pin it in the integration manifest. No such package publication is claimed here. |
| Brands | Submit and obtain approval for the required brand icon/logo assets. |
| Coverage | Local results meet the [coverage rule](https://contributors.home-assistant.io/core/quality-scale/test-coverage/). Preserve this coverage through client extraction and repeat checks when preparing an upstream submission. |
| Strict typing | Local strict mypy passes. Ship `py.typed` with the separate client and complete the Core `.strict-typing` registration and checks when preparing an upstream submission. See the [strict typing rule](https://contributors.home-assistant.io/core/quality-scale/strict-typing/). |
| Upstream review | Complete applicable Core validation, publish user documentation in the required location, and obtain maintainer review of every rule and exemption. Local status files cannot award a tier. |

Authentication and reauthentication are exempt because the supported direct API
does not use credentials. Custom trigger/condition documentation is exempt
because no custom triggers or conditions are provided. Unconfirmed Gen4 map
promotions have a persistent repair notice with recovery instructions, following
the [Repairs guidance](https://contributors.home-assistant.io/core/integrations/repairs/).
Transient connection failures use setup retry and entity availability.
