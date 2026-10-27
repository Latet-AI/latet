"""The value of a ``key=value`` pair whose key names a secret is masked wherever the pair stands in a line: after other
words in a log, in parentheses, in a URL's query, beside other pairs, quoted or not. It was masked only where the line
began with it (``password=...``) or in a run of two pairs apart by spaces, so ``INFO login ok password=Winter2024x``
went to the model in clear when the value was too plain for the rules that need entropy."""

import itertools

import pytest

from latet.config import Config
from latet.engine.build import build
from latet.engine.types import SlotKind
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY

KEYS = ["password", "token", "secret", "pwd", "api_key", "DB_PASSWORD", "access_token", "passwd", "client_secret"]
FORMS = {"bare": "{k}={v}", "double quotes": '{k}="{v}"', "single quotes": "{k}='{v}'"}
LINES = {
    "after words": "2026-10-26 12:00:01 INFO login ok {pair}",
    "before words": "{pair} from the login form",
    "between words": "INFO login ok {pair} and more words",
    "in parentheses": "login ok ({pair}) done",
    "in a query": "GET /x?{pair}&b=1 HTTP/1.1",
    "after a pair": "user=ada {pair}",
    "before a pair": "{pair} user=ada",
    "alone": "{pair}",
}
SLOTS = {"prose": SlotKind.USER_TEXT, "data": SlotKind.TOOL_RESULT, "input": SlotKind.TOOL_INPUT}
PROFILES = ["balanced", "secrets"]
_BUILT: dict[str, object] = {}
_VALUES = itertools.count(1)


def _built(profile):
    if profile not in _BUILT:
        _BUILT[profile] = build(Config(ner="off", profile=profile), Vault(ROOT_KEY))
    return _BUILT[profile]


def _value():
    """A value too plain for the rules that need entropy, and none the vault has seen before."""
    return f"Summer{next(_VALUES):05d}"


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize("line", LINES.values(), ids=LINES)
@pytest.mark.parametrize("form", FORMS.values(), ids=FORMS)
@pytest.mark.parametrize("key", KEYS)
def test_a_value_after_a_secret_named_key_is_masked_wherever_the_pair_stands(key, form, line, slot, profile):
    value = _value()
    text = line.format(pair=form.format(k=key, v=value))
    masked = _built(profile).masker.mask(text, slot)
    [found] = [r for r in masked.replacements if r.original == value]
    assert found.etype in ("PASSWORD", "ASSIGNED_SECRET") and found.eclass.name == "SECRET"
    assert value not in masked.text
    assert masked.text == text.replace(value, found.replacement)  # nothing else around it changes


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize(
    "key", ["Password", "SECRET", "Api_Key", "dbPassword", "clientSecret", "authToken", "db.password", "my-token"]
)
def test_a_key_in_any_case_or_style_is_read_as_a_secret_name(key, slot):
    value = _value()
    text = f"INFO login ok {key}={value}"
    masked = _built("balanced").masker.mask(text, slot)
    assert value not in masked.text and masked.text.startswith(f"INFO login ok {key}=")


STAYS = [
    "connect(host=h, password=pw)",  # an identifier too short to be a password
    "connect(host=h, password=password)",  # the name again
    "requests.post(url, token=auth_token)",  # a name in a call
    "run --password=$DB_PASSWORD now",  # a variable
    "set password=${PASSWORD}",
    "login ok password=changeme",  # a placeholder
    "login ok password=null token=None",
    "got token_count=12345678 and password_file=/etc/pw and secret_name=prod/db",  # names around a secret, not one
    "opts key=len pass=1 salt=ab",
    "tokenizer=bert-base author=Ada",
    "login ok token=12345",  # a number too short for a token
    "x password=",
    "auth=(user, pw)",
    "see https://example.com/login?next=/home&tab=password",
    "def f(secret=None, token=''): pass",
    "client = Client(api_key=api_key, timeout=30)",
    "x = dict(password=request.password)",  # an attribute
    "export PASSWORD=$(cat pw)",
    "password=<your password>",
    "token=[redacted]",
    "user=ada pass=xx",
    "client = connect(password=getpass(), user=u)",  # a call, not a value
    "run(secret=load_secret('db'), retries=3)",
    "x = f(token=tokens[0])",
    "login(password=input('password: '))",
]


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize("text", STAYS)
def test_a_pair_whose_value_is_a_name_a_placeholder_or_nothing_is_left_as_it_was(text, slot, profile):
    assert _built(profile).masker.mask(text, slot).text == text


@pytest.mark.parametrize("profile", PROFILES)
def test_the_pair_of_a_line_that_began_with_it_is_masked_as_before(profile):
    value = _value()
    text = f"password={value}\nuser=ada"
    masked = _built(profile).masker.mask(text, SlotKind.TOOL_RESULT)
    assert value not in masked.text and masked.text.endswith("\nuser=ada")
