# Acknowledgements

Latet stands on the work of these projects. Thank you to their authors and contributors.
The license texts of the components Latet distributes are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

**Vendored or ported code**

| Project | Used for | License |
| --- | --- | --- |
| [gitleaks](https://github.com/gitleaks/gitleaks) | Default secret-detection ruleset, vendored and converted for RE2 | MIT |
| [Presidio](https://github.com/data-privacy-stack/presidio) (formerly Microsoft Presidio) | PII recognizers ported (patterns, context words, checksum validation) | MIT |

**Runtime dependencies**

| Project | Used for | License |
| --- | --- | --- |
| [phonenumbers](https://github.com/daviddrysdale/python-phonenumbers) | Phone number parsing and validation | Apache-2.0 |
| [google-re2](https://pypi.org/project/google-re2/) / [RE2](https://github.com/google/re2) | Linear-time regular expressions | BSD-3-Clause |
| [Starlette](https://github.com/Kludex/starlette) | ASGI framework for the daemon | BSD-3-Clause |
| [Uvicorn](https://github.com/Kludex/uvicorn) | ASGI server | BSD-3-Clause |
| [HTTPX](https://github.com/encode/httpx) | HTTP client for upstream requests | BSD-3-Clause |
| [websockets](https://github.com/python-websockets/websockets) | WebSocket relay | BSD-3-Clause |
| [Pydantic](https://github.com/pydantic/pydantic) | Data validation | MIT |
| [Typer](https://github.com/fastapi/typer) | Command-line interface | MIT |
| [python-zstandard](https://github.com/indygreg/python-zstandard) | Zstandard compression | BSD-3-Clause |
| [Brotli](https://github.com/google/brotli) | Brotli compression | MIT |
| [TOML Kit](https://github.com/python-poetry/tomlkit) | Style-preserving TOML reading and editing | MIT |
| [keyring](https://github.com/jaraco/keyring) | Access to the operating system's credential store | MIT |
| [cryptography](https://github.com/pyca/cryptography) | Cryptographic primitives | Apache-2.0 OR BSD-3-Clause |
| [truststore](https://github.com/sethmlarson/truststore) | TLS verification with the operating system's certificate store | MIT |
| [platformdirs](https://github.com/tox-dev/platformdirs) | Per-platform data and configuration directories | MIT |
| [PyYAML](https://github.com/yaml/pyyaml) | YAML parsing | MIT |

**Optional local name detection (`latet[ner]`)**

| Project | Used for | License |
| --- | --- | --- |
| [Privacy Filter](https://huggingface.co/openai/privacy-filter) by OpenAI | The name and address model, downloaded on request | Apache-2.0 |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | Local model inference | MIT |
| [Tokenizers](https://github.com/huggingface/tokenizers) | Model tokenization | Apache-2.0 |
| [NumPy](https://github.com/numpy/numpy) | Model inputs and outputs | BSD-3-Clause |

**Optional text in images (`latet[ocr]`)**

| Project | Used for | License |
| --- | --- | --- |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) PP-OCRv6 small, [detection](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_det_onnx) and [recognition](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec_onnx) in ONNX | The text recognition models, downloaded on request | Apache-2.0 |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | Local model inference | MIT |
| [Pillow](https://github.com/python-pillow/Pillow) | Reading, painting and writing images | MIT-CMU |
| [IBM Plex Sans](https://github.com/IBM/plex) | The font the fakes are written in, included unmodified | OFL-1.1 |
| [NumPy](https://github.com/numpy/numpy) | Model inputs and outputs | BSD-3-Clause |

**Optional Latet on a server (`latet[server]`)**

| Project | Used for | License |
| --- | --- | --- |
| [psycopg](https://github.com/psycopg/psycopg) | PostgreSQL driver and connection pool, used as a separate, unmodified package | LGPL-3.0-only |
| [redis-py](https://github.com/redis/redis-py) | Client for Valkey and Redis | MIT |
| [PyJWT](https://github.com/jpadilla/pyjwt) | Checking the signature of an ID token in single sign-on (Enterprise Edition) | MIT |

**Optional LDAP directories (`latet[ldap]`, Latet Enterprise)**

| Project | Used for | License |
| --- | --- | --- |
| [ldap3](https://github.com/cannatag/ldap3) | Reading people and groups from an LDAP directory or Active Directory for term sync, used as a separate, unmodified package | LGPL-3.0-or-later |
| [pyasn1](https://github.com/pyasn1/pyasn1) | The LDAP protocol's encoding, for ldap3 | BSD-2-Clause |

**Dashboard and documentation site**

| Project | Used for | License |
| --- | --- | --- |
| [React](https://github.com/react/react) | Dashboard UI | MIT |
| [React Router](https://github.com/remix-run/react-router) | Dashboard navigation | MIT |
| [Vite](https://github.com/vitejs/vite) | Dashboard build | MIT |
| [Tailwind CSS](https://github.com/tailwindlabs/tailwindcss) | Dashboard styling | MIT |
| [i18next](https://github.com/i18next/i18next) and [react-i18next](https://github.com/i18next/react-i18next) | Dashboard translations | MIT |
| [Astro](https://github.com/withastro/astro) and [Starlight](https://github.com/withastro/starlight) | Documentation site | MIT |
| [Pagefind](https://github.com/Pagefind/pagefind) | Local, static search for the documentation | MIT |

**Development**

| Project | Used for | License |
| --- | --- | --- |
| [pytest](https://github.com/pytest-dev/pytest) | Test runner | MIT |
| [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | Async tests | Apache-2.0 |
| [pytest-socket](https://github.com/miketheman/pytest-socket) | Blocking network access in tests | MIT |
| [Hypothesis](https://github.com/HypothesisWorks/hypothesis) | Property-based testing | MPL-2.0 |
| [python-stdnum](https://github.com/arthurdejong/python-stdnum) | Reference validation of identifiers, in tests only (not distributed) | LGPL-2.1-or-later |
| [Ruff](https://github.com/astral-sh/ruff) | Linting and formatting | MIT |
| [Pyright](https://github.com/microsoft/pyright) (via [pyright-python](https://github.com/RobertCraigie/pyright-python)) | Static type checking | MIT |
| [uv](https://github.com/astral-sh/uv) | Python packaging and environments | Apache-2.0 OR MIT |
| [Hatchling](https://github.com/pypa/hatch) | Build backend | MIT |
| [TypeScript](https://github.com/microsoft/TypeScript), [ESLint](https://github.com/eslint/eslint) and [Vitest](https://github.com/vitest-dev/vitest) | Dashboard type checking, linting and tests | Apache-2.0, MIT, MIT |
| [CycloneDX Python](https://github.com/CycloneDX/cyclonedx-python) | Release SBOM | Apache-2.0 |
