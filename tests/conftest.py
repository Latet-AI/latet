import atexit
import base64
import errno
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from latet.engine.detectors.gitleaks import GitleaksDetector
from latet.engine.detectors.pii import PiiDetector
from latet.engine.detectors.structural import AssignmentDetector, CredentialDetector, PrivateKeyDetector
from latet.engine.detectors.terms import IdentityDetector, LocalIdentity, Term, TermsDetector
from latet.engine.masker import Masker
from latet.engine.vault import Vault

ROOT_KEY = b"\x07" * 32

# The import-time baseline protects collection and module-scoped fixtures too. A fresh directory directly under
# the trusted checkout avoids a predictable scratch parent; runners can supply another owned root outside TEMP.
_ORIGINAL_HOME = os.path.normcase(os.path.abspath(Path.home()))
_PATH_HOME = Path.home
_HOME_OVERRIDE = os.environ.get("LATET_TEST_HOME_ROOT")
_HOME_ROOT = Path(_HOME_OVERRIDE).resolve() if _HOME_OVERRIDE else Path(__file__).resolve().parents[1]
if _HOME_ROOT.is_relative_to(Path(tempfile.gettempdir()).resolve()):
    raise RuntimeError("test home isolation requires LATET_TEST_HOME_ROOT outside system TEMP")
_HOMES = tempfile.mkdtemp(prefix="latet-test-homes-" if _HOME_OVERRIDE else ".latet-test-homes-", dir=_HOME_ROOT)
atexit.register(shutil.rmtree, _HOMES, ignore_errors=True)


def _home_environment(root: Path) -> dict[str, str]:
    paths = {
        "HOME": root,
        "USERPROFILE": root,
        "CLAUDE_CONFIG_DIR": root / ".claude",
        "CODEX_HOME": root / ".codex",
        "GEMINI_CLI_HOME": root,
        "XDG_CONFIG_HOME": root / ".config",
        "APPDATA": root / "roaming",
        "LOCALAPPDATA": root / "local",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    environment = {name: str(path) for name, path in paths.items()}
    if os.name == "nt":
        environment.update(HOMEDRIVE=root.drive, HOMEPATH=str(root)[len(root.drive) :])
    return environment


os.environ.update(_home_environment(Path(_HOMES) / "collection"))

# Tests never touch the user's real configuration, data (a downloaded model, say) or OS keyring. Removed when the run
# ends, whatever its tests did: the keys the tests save are written in it.
_HERMETIC = tempfile.mkdtemp(prefix="latet-tests-")
if os.name != "nt":
    # macOS's TEMP is under /var, a link to /private/var, and Latet goes through no link to its data folder: the real
    # path. Not on Windows, where it would also spell out TEMP's short 8.3 names.
    _HERMETIC = os.path.realpath(_HERMETIC)
atexit.register(shutil.rmtree, _HERMETIC, ignore_errors=True)
os.environ["LATET_DATA_DIR"] = os.path.join(_HERMETIC, "data")
os.environ["LATET_CONFIG"] = os.path.join(_HERMETIC, "config.yaml")
# Nor Gemini CLI's settings of this machine, which its wrapper reads above yours.
os.environ["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = os.path.join(_HERMETIC, "gemini-cli", "settings.json")
os.environ["GEMINI_CLI_SYSTEM_DEFAULTS_PATH"] = os.path.join(_HERMETIC, "gemini-cli", "system-defaults.json")
os.environ.pop("GEMINI_CLI_AUTH_OVERRIDE", None)
# Nor your connection to a Latet server, nor your server: no test reads, changes or removes your connection file, or asks
# your server with your administration token (a test that needs a connection makes its own, tests/unit/conftest.py).
for _name in ("LATET_CONNECTION", "LATET_SERVER_URL", "LATET_SERVER_FINGERPRINT", "LATET_SERVER_ADMIN_TOKEN"):
    os.environ.pop(_name, None)
# Nor the accounts you trust in Latet's folder checks: a test that trusts one sets it.
os.environ.pop("LATET_TRUSTED_ACCOUNTS", None)
os.environ["LATET_ROOT_KEY"] = base64.b64encode(ROOT_KEY).decode()
os.environ["LATET_KEYRING"] = "off"  # keys saved by the tests go to files in the data folder above
# And what asks the keyring all the same (a server token's removal clears the keyring with LATET_KEYRING off too) asks
# one that keeps nothing, in this process and in the ones it starts; a test that needs a keyring puts one in memory.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

# Property-test depth: "dev" locally, "ci" in CI, "deep" for release checks (HYPOTHESIS_PROFILE).
_SLOW_OK = {"deadline": None, "suppress_health_check": [HealthCheck.too_slow]}
settings.register_profile("dev", max_examples=300, **_SLOW_OK)
settings.register_profile("ci", max_examples=3_000, **_SLOW_OK)
settings.register_profile("deep", max_examples=10_000, **_SLOW_OK)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(autouse=True)
def isolated_homes():
    """A fresh home for every test, before nested fixtures can remove an agent-specific override."""
    root = Path(tempfile.mkdtemp(prefix="case-", dir=_HOMES))
    # Owning this context avoids changing when a test's shared monkeypatch is undone relative to its other fixtures.
    with pytest.MonkeyPatch.context() as isolation:
        for name, value in _home_environment(root).items():
            isolation.setenv(name, value)

        def checked_home(cls):
            # Do not even ask the platform for its account-home fallback when a fixture drops its home variable.
            if not os.environ.get("USERPROFILE" if os.name == "nt" else "HOME"):
                raise RuntimeError("test home isolation lost its home variable")
            home = _PATH_HOME()
            if os.path.normcase(os.path.abspath(home)) == _ORIGINAL_HOME:
                raise RuntimeError("test home isolation reached the original home")
            return home

        isolation.setattr(Path, "home", classmethod(checked_home))
        try:
            checked_home(Path)
            yield
            checked_home(Path)
        finally:
            shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def no_trusted_accounts_from_the_registry():
    """On Windows LATET_TRUSTED_ACCOUNTS is read from the registry's environments (R30): no test reads yours; one that
    trusts an account doubles the reader (tests/unit/test_folder_trust.py). The registry is never written."""
    from latet.storage import folders

    with pytest.MonkeyPatch.context() as isolation:
        isolation.setattr(folders, "environment_value", lambda scope: None, raising=False)
        yield


@pytest.fixture(autouse=True)
def repositories_read_in_this_process():
    """The repositories a computer remembers read by a helper in a thread of the test's process
    (``tests/repository_children.py``), where a test's monkeypatches reach it, in a place of the test's own: the
    daemon's helper process is the tests' that ask for it (``real_repository_helper``)."""
    from latet import repository_io

    from .repository_children import InProcessChild

    with pytest.MonkeyPatch.context() as reads:
        reads.setattr(repository_io, "launch", InProcessChild)
        reads.setattr(repository_io, "READER", repository_io.RepositoryReader())
        yield


@pytest.fixture
def real_repository_helper(repositories_read_in_this_process):
    """The daemon's own helper process reads the repositories remembered (``repository_io._launch``)."""
    from latet import repository_io

    with pytest.MonkeyPatch.context() as reads:
        reads.setattr(repository_io, "launch", repository_io._launch)
        reads.setattr(repository_io, "READER", repository_io.RepositoryReader())
        yield


@pytest.fixture
def chmod_refused(monkeypatch):
    """chmod as a mount that shows files as another owner's answers it: refused."""

    def refused(path, mode, *args, **kwargs):
        raise PermissionError(errno.EPERM, "Operation not permitted", os.fspath(path))

    monkeypatch.setattr(os, "chmod", refused)


@pytest.fixture
def vault():
    return Vault(ROOT_KEY)


@pytest.fixture
def masker(vault):
    detectors = [
        GitleaksDetector(),
        AssignmentDetector(),
        PrivateKeyDetector(),
        CredentialDetector(),
        PiiDetector(),
        TermsDetector([Term("Acme Corp", "CLIENT")]),
        IdentityDetector(LocalIdentity(username="marc", full_name="Marco Ferretti")),
    ]
    return Masker(vault, detectors)
