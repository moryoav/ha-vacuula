# Contributing

I welcome bug reports, documentation improvements and focused fixes for this
independent Home Assistant integration. I maintain the integration separately
from the [Vacuula server](https://github.com/vacuula/vacuula).

## Before starting

Please check existing issues and the [support guide](SUPPORT.md). For a large
change or a change to robot control, map recovery or scheduling, open an issue
to discuss the behavior first. A small, focused fix can go straight to a pull
request.

Use the issue forms for bugs and feature requests. Include the integration,
Home Assistant, Vacuula server and installer versions, robot model and
generation, reproduction steps and the expected result. Mention whether the
same operation works in the Vacuula app. Remove credentials, private addresses,
robot identifiers and private map or room information from evidence.

## Development

The integration lives in `custom_components/vacuula/`, tests in `tests/`, and
development tools in `tools/`. The test environment uses Linux, Python 3.14 and
the pinned dependencies in `requirements_test.txt`.

Create an isolated Python environment, then run:

```sh
python -m pip install -r requirements_test.txt
python tools/check_docstrings.py
python -m ruff check .
python -m ruff format --check .
python -m mypy custom_components/vacuula tools
python -m pytest --cov=custom_components.vacuula --cov-report=term-missing
```

I require nonempty docstrings on every Python module, class and function,
including test helpers and nested callbacks. I use focused regression tests
for changed behavior and the full checks before a release. See the
[protocol notes](docs/protocol.md) and [validation record](docs/local-validation.md)
for the verified API and hardware limits.

Keep pull requests focused and explain the behavior before and after the
change. Update the README, changelog, action descriptions and translations
when they are affected. Preserve the existing notices for third-party artwork.

## Hardware and artifacts

Mocked tests must not contact a real server or robot. For hardware checks,
state which reads or commands were tested and distinguish observed movement
from command acknowledgements. Use hardware you own or have permission to test.
Keep firmware resets and provisioning changes separate from routine integration
validation.

Store captures, extracted apps, binaries, databases and release ZIPs outside the
Git workspace. Keep Git commands sequential. Do not commit credentials, private
logs, robot serial numbers or Home Assistant configuration.

## Releases

The release tag must match `custom_components/vacuula/manifest.json` and a
nonempty section in `CHANGELOG.md`. The Release workflow checks the integration
before publishing the tagged component as `vacuula.zip`.

To check an existing tag locally, choose a directory outside the checkout:

```sh
python tools/build_hacs_release.py v0.1.14 /tmp/vacuula-release
```

The builder uses committed tag contents, not working files. It preserves the
integration's license and the separate artwork source, license and attribution.
Please follow the [code of conduct](CODE_OF_CONDUCT.md), and use
[private vulnerability reporting](SECURITY.md) for security concerns.
