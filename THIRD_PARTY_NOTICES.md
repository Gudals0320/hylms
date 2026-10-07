# Third-party software

This repository contains original application source and synthetic test fixtures. It does not vendor dependency source, wheels, executables, university documents, or model output containing private course data.

The pinned runtime dependencies in `requirements-service-account.txt` are installed separately from PyPI. Their installed distribution metadata identifies these licenses:

| Distribution | Version | License |
|---|---|---|
| google-auth | 2.58.0 | Apache 2.0 |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| cffi | 2.1.1 | MIT-0 |
| pycparser | 3.0 | BSD-3-Clause |
| pyasn1 | 0.6.4 | BSD-2-Clause |
| pyasn1-modules | 0.4.2 | BSD (distribution metadata) |

Consult each installed distribution's license files for its complete terms and any bundled native-code notices. Python and Windows are separate prerequisites. Gitleaks is a release-checking tool, not part of the application distribution. GitHub Actions references are pinned to commits and are not vendored here.
