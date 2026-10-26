# Support

I maintain this independent integration in my spare time. I can investigate
integration problems, but I cannot promise an immediate response or repair
every upstream server, firmware or hardware problem.

## Getting help

Start with the [README](README.md) for installation, setup, actions and
troubleshooting, and the [changelog](CHANGELOG.md) for version-specific changes.
The [validation record](docs/local-validation.md) describes what has been tested.

If a problem also occurs in the Vacuula app, it may belong in the
[upstream Vacuula repository](https://github.com/vacuula/vacuula/issues).
For a problem limited to Home Assistant, use this repository's
[bug report form](https://github.com/moryoav/ha-vacuula/issues/new?template=bug_report.yml).

Please include:

- Integration, Home Assistant, Vacuula server and installer versions.
- Robot model and generation, and whether the app reports it connected.
- Reproduction steps, expected behavior and actual behavior.
- Whether the same operation works in the Vacuula app.
- Relevant sanitized logs or downloaded integration diagnostics.

Remove credentials, tokens, private addresses, robot serial numbers and private
map or room information before posting. For security concerns, use the private
process in [SECURITY.md](SECURITY.md).

## Compatibility

I test against Home Assistant 2026.10.2 and Python 3.14. The integration supports
Gen3 and Gen4 robots through the local Vacuula API. Gen4 behavior is verified
against source and fixtures; I do not currently have Gen4 hardware for testing.
Available features also depend on the robot's reported capabilities.

Use the [feature request form](https://github.com/moryoav/ha-vacuula/issues/new?template=feature_request.yml)
for an improvement, with the workflow you want to use and the hardware involved.
I welcome clear reports of accessibility barriers too; see
[ACCESSIBILITY.md](ACCESSIBILITY.md).
