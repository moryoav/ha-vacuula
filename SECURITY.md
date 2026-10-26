# Security policy

I support security fixes for the latest published release and the current
`main` branch. Older releases may need an upgrade before I can investigate.

## Report a vulnerability

Please use [GitHub's private vulnerability report form](https://github.com/moryoav/ha-vacuula/security/advisories/new).
I ask that exploit details, credentials and private configuration stay out of
public issues while I investigate.

Include the affected version, a description of the impact and steps to
reproduce in a test environment. Sanitized logs and a proposed fix are helpful.
I will review the report and coordinate any public disclosure after a fix or
mitigation is available. I cannot promise a response or fix within a set time.

## Scope

This repository contains the Home Assistant integration. Relevant areas include
server discovery and address validation, HTTP and event streams, action target
resolution, map and schedule writes, diagnostics, logs and release packaging.
The local Vacuula API does not require authentication, so the integration
expects a server reachable on a trusted network.

If a report appears to involve the upstream Vacuula server or installer,
please say so in the private report. I can help identify which project needs
to investigate without publishing sensitive details.

Test only systems and robots you own or have permission to use. Remove tokens,
credentials, private URLs, robot identifiers and private Home Assistant data
from shared evidence.
