"""A line that ends right after a secret's name (``DB_PASSWORD=``, a YAML ``secrets:`` with its block below, ``const
accessToken =`` with the value on the next line) is a line like any other: no detector raises on it. The assignment
detector did (``IndexError`` on an empty value), so that masking failed and the gateway refused the request, and every
later one of the conversation that carried the file."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from latet.config import Config, Profile
from latet.engine.build import build
from latet.engine.detectors.base import DetectContext
from latet.engine.types import SlotKind
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY

NAMES = [
    *("DB_PASSWORD", "password", "passwd", "pwd", "secrets", "secret", "auth", "token", "api_key", "apiKey"),
    *("accessToken", "const accessToken", "export API_KEY", "$env:TOKEN", "  token", "\ttoken", "client_secret"),
    *("private_key", "connectionString", "connection_string", "dsn", "cookie", "salt", "bearer", "credentials"),
    *("var secretKey", "String password", "services.db.password", '"password"', "'token'", "`secret`", "- secret"),
]
SEPARATORS = ["=", ":", ":=", " = ", ": ", " := ", "  =  ", "\t=\t", "=\n", ":\n", " =\n", "=\r\n"]
TAILS = [
    *("", " ", "  ", "\t", "'", '"', "`", "''", '""', "``", "'''", '"""', "'x", '"x', "`x", "'\\", '"\\'),
    *("#", " #", " # x", "//", " //", " // x", ";", " ;", "--", "-- x", "/*", " /*", " /* x */", ",", ")", ");"),
    *("{", "[", "$", "\\", "\"'", "'\"", "' #", '" //', "' ", '"  '),
]
SLOTS = [SlotKind.TOOL_RESULT, SlotKind.USER_TEXT, SlotKind.TOOL_INPUT]


def _built(profile):
    return build(Config(ner="off", profile=profile), Vault(ROOT_KEY))


BUILT = {profile: _built(profile) for profile in (Profile.BALANCED, Profile.MAX, Profile.SECRETS)}
LINE = st.builds(lambda a, b, c: a + b + c, st.sampled_from(NAMES), st.sampled_from(SEPARATORS), st.sampled_from(TAILS))
TEXT = st.lists(LINE, min_size=1, max_size=5).map("\n".join)


def _every_detector_reads(text):
    for built in BUILT.values():
        for slot in SLOTS:
            context = DetectContext(slot)
            for detector in built.masker.detectors:
                list(detector.detect(text, context))  # none raises, whatever it finds
            # and the whole masking goes through: the gateway refuses a request whose masking fails
            built.masker.mask(text, slot)


@given(TEXT)
def test_no_detector_raises_on_a_line_made_of_a_secrets_name_and_nothing_or_marks(text):
    _every_detector_reads(text)


@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD=",
        "DB_PASSWORD=\nUSER=ada",
        "password =\n    os.environ['X']",
        "export const accessToken =\n  await getToken();",
        "function f() {\n  const firstToken =\n    sourceCode.getFirstToken(node);\n}\n",
        "services:\n  db:\n    image: postgres\nsecrets:\n  db_password:\n    file: ./pw.txt\n",
        "registry:\n  auth:\n    username: ada\n",
        "jobs:\n  deploy:\n    secrets: inherit\n",
        "password:",
        "token=   ",
        "secret = ''",
        'auth: ""',
        "api_key=`",
        "password= # nothing yet",
    ],
)
def test_the_files_that_stopped_a_request_are_masked(text):
    _every_detector_reads(text)
    assert BUILT[Profile.BALANCED].masker.mask(text, SlotKind.TOOL_RESULT).text.count("\n") == text.count("\n")
