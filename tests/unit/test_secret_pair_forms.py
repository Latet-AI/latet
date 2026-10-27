"""The pair of a secret's name in the middle of a line, round 5: a comparison is no pair, a status word after a secret's
name is no secret, a closing backtick, a ``{name}`` placeholder and a concatenation are no part of a value, a pair after
``--`` is read, and a quoted literal after a strong name is masked after a colon or a spaced ``=`` as well."""

import itertools

import pytest

from latet.config import Config
from latet.engine.build import build
from latet.engine.detectors import structural
from latet.engine.types import SlotKind
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY

SLOTS = {"prose": SlotKind.USER_TEXT, "data": SlotKind.TOOL_RESULT, "input": SlotKind.TOOL_INPUT}
_BUILT: dict[str, object] = {}
_VALUES = itertools.count(1)


def _value():
    """A value too plain for the rules that need entropy, and none the vault has seen: a value it knows is masked
    wherever it stands, which would hide a pair that is not read."""
    return f"Autumn{next(_VALUES):05d}"


def _mask(text, slot, profile="balanced"):
    if profile not in _BUILT:
        _BUILT[profile] = build(Config(ner="off", profile=profile), Vault(ROOT_KEY))
    return _BUILT[profile].masker.mask(text, slot)


def _left(text, slot):
    return _mask(text, slot).text == text


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize(
    "text",
    [
        "if (token==expectedTokenValue) { return true }",
        "while (secret===storedSecretValue) next()",
        "assert password==expectedPassword",
        "if token!=expectedTokenValue: stop",
        "ok = (api_key>=minimumKeyVersion)",
        "return token=>expectedTokenValue",
    ],
)
def test_a_comparison_is_no_pair(text, slot):
    assert _left(text, slot), text


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
def test_an_assignment_is_still_a_pair_beside_a_comparison(slot):
    value = _value()
    masked = _mask(f"if (a==b) log(token={value})", slot)
    assert value not in masked.text and masked.text.startswith("if (a==b) log(token=")


STATUS_WORDS = [
    *("ok", "failed", "denied", "success", "expired", "missing", "invalid", "valid", "required", "present", "absent"),
    *("revoked", "rotated", "pending", "accepted", "rejected", "unknown", "anonymous", "refused", "granted"),
]
SECRET_KEYS = ["auth", "token", "password", "secret", "api_key", "cookie", "pwd", "credentials", "access_token"]


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize("word", STATUS_WORDS)
@pytest.mark.parametrize("key", SECRET_KEYS)
def test_a_status_word_after_a_secrets_name_is_no_secret(key, word, slot):
    for text in (
        f"INFO login for ada: {key}={word}",
        f"level=info user=ada {key}={word} path=/x",
        f"{key}={word}",
        f"2026-10-26 GET /login ({key}={word}) 401",
    ):
        assert _left(text, slot), text


STATUS_KEYS = [
    *(
        "auth_status",
        "token_status",
        "auth_result",
        "auth_results",
        "password_changed",
        "auth_state",
        "session_key_state",
    ),
    *("auth_scheme", "token_error", "auth_reason", "auth_outcome", "authStatus", "tokenState", "AUTH_STATUS"),
]


def _letters(n: int) -> str:
    """``n`` written in the letters a to j: a lowercase word that is new each time."""
    return "".join("abcdefghij"[int(digit)] for digit in str(n))


def _card(n: int) -> str:
    """A card number (16 digits, the check digit of Luhn) that is new each time."""
    body = f"4{n:014d}"
    total = sum(sum(divmod(int(d) * (2 if i % 2 == 0 else 1), 10)) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


# What a value under such a name is left for: it has the shape of a status. A lowercase word of 20 characters at most,
# with one `-` or `_` at most, a number of 10 digits at most, a time, a date or a boolean.
STATUS_SHAPES = [
    *("saml", "bearer-local", "some_thing", "disabled-admin", "abcdefghijklmnopqrst", "3600", "-1", "0.5"),
    *("3600.25", "12:00:01", "09:30", "2026-10-26", "2026-10-26T12:00:01Z", "26/10/2026"),
    *("2026-10-26 12:00:01", "true", "False", "TRUE"),
]
# Values of no status shape, made new each time: a capital, a digit among letters, a sign; and what is too long for a
# status: a passphrase of lowercase words, a word of 21 letters, more than one `-` or `_`, a number of 11 digits or more
# (20 digits, a card number).
OTHER_SHAPES = [
    lambda n: f"Winter{n:05d}x",
    lambda n: f"Ab{n:05d}Kq",
    lambda n: f"hunter{n}",
    lambda n: f"sha{n}sum",
    lambda n: f"Tr{n}!x3",
    lambda n: f"p@ss{n}!",
    lambda n: f"Mixed-Case-{n}",
    lambda n: f"correct-horse-{_letters(n)}-battery-staple",
    lambda n: f"qwertyuiopasdfghjklz{_letters(n)}",
    lambda n: f"a-{_letters(n)}-c",
    lambda n: f"snake_case_{_letters(n)}",
    lambda n: f"1{n:010d}",
    lambda n: f"{n:020d}",
    lambda n: _card(n),
]


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize("key", STATUS_KEYS)
def test_a_value_of_a_status_shape_under_a_status_name_is_left(key, slot):
    """The nine last words that say what a check found about a secret (status, state, result, results, outcome, scheme,
    changed, reason, error): under such a name a lowercase word, a number, a time, a date or a boolean is left."""
    for shape in STATUS_SHAPES:
        for template in ("INFO login for ada: {k}={v}", "{k}={v}", "user=ada {k}={v} path=/x", '{{"{k}": "{v}"}}'):
            text = template.format(k=key, v=shape)
            assert _left(text, slot), text


@pytest.mark.parametrize(
    ("value", "status"),
    [
        *[
            (v, True)
            for v in ("saml", "a", "bearer-local", "some_thing", "abcdefghijklmnopqrst", "abcdefghi-jklmnopqrs")
        ],
        *[(v, True) for v in ("0", "-1", "3600", "1234567890", "+1234567890", "3600.25", "1234567890.123456")],
        *[
            (v, True)
            for v in ("12:00:01", "09:30", "12:00:01.250+02:00", "2026-10-26", "26/10/2026", "2026-10-26T12:00:01Z")
        ],
        *[(v, True) for v in ("true", "false", "TRUE", "FALSE", "True", "False")],
        # too long for a status: a lowercase passphrase, a word of 21 letters, two separators, a number of 11 digits
        *[(v, False) for v in ("correct-horse-battery-staple", "abcdefghijklmnopqrstu", "abcdefghijklmnopqrstuvwxyz")],
        *[
            (v, False)
            for v in ("a-b-c", "a_b_c", "a-b_c", "disabled-by-admin", "snake_case_name", "-leading", "trailing-")
        ],
        *[
            (v, False)
            for v in ("12345678901", "12345678901234567890", "-12345678901", "1234567890.1234567", "4111111111111111")
        ],
        *[(v, False) for v in ("Winter2024x", "sha256", "Ab3x9Kq2Lm", "Mixed", "yes please", "12:00:01x", "")],
    ],
)
def test_the_shape_of_a_status_is_short(value, status):
    """A word of 20 characters at most with one `-` or `_` at most, a number of 10 digits at most (and a fraction of 6),
    a time, a date or a boolean; anything longer is a passphrase, a number or a card, and is masked as it was."""
    from latet.engine.detectors.structural import _status_shaped

    assert _status_shaped(value) is status, value


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize("key", STATUS_KEYS)
def test_any_other_value_under_a_status_name_is_masked_as_it_was(key, slot):
    """`password_changed=Winter2024x`, `token_state=Ab3x9Kq2Lm`: no status in them."""
    for make in OTHER_SHAPES:
        for template in (
            "{k}={v}",  # a line's start
            "{k}: {v}",
            "INFO login for ada: {k}={v}",  # the middle of a line
            "user=ada {k}={v} path=/x",  # a run of pairs
            '{{"user": "ada", "{k}": "{v}"}}',  # JSON
            '  "{k}": "{v}",',  # a member on its own line
        ):
            value = make(next(_VALUES))
            text = template.format(k=key, v=value)
            assert value not in _mask(text, slot).text, text


# Names that end in a word which does not say a check's outcome: what 0.9.10 masked under them is masked still, a
# password that is reset, the source of a token, the arguments, the options and the flags of a secret.
KEPT_KEYS = [
    *("password_reset", "token_source", "tokenSource", "PASSWORD_RESET", "api_key_args", "SECRET_KEY_OPTION"),
    *("auth_flags", "client_secret_argument", "secret_options", "password_arguments", "token_flag", "pwd_arg"),
]


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize("key", KEPT_KEYS)
def test_a_key_that_ends_in_reset_source_arg_option_or_flag_still_names_a_secret(key, slot):
    for template in (
        "{k}={v}",  # a line's start
        "{k}: {v}",
        "INFO login for ada: {k}={v}",  # the middle of a line
        "user=ada {k}={v} path=/x",  # a run of pairs
        '{{"user": "ada", "{k}": "{v}"}}',  # JSON
        '  "{k}": "{v}",',  # a member on its own line
    ):
        value = _value()
        text = template.format(k=key, v=value)
        assert value not in _mask(text, slot).text, text


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize("key", ["password_reset", "token_source", "api_key_args", "auth_flags"])
@pytest.mark.parametrize("word", ["success", "failed", "expired", "ok", "pending", "unknown"])
def test_a_status_word_under_those_names_is_left_as_under_any_name(key, word, slot):
    for template in ("{k}={w}", "INFO login for ada: {k}={w}", '{{"user": "ada", "{k}": "{w}"}}'):
        text = template.format(k=key, w=word)
        assert _left(text, slot), text


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize("key", ["auth", "token", "password", "api_key"])
def test_a_value_that_is_no_status_word_after_the_same_names_is_still_a_secret(key, slot):
    for template in ("INFO login for ada: {k}={v}", "level=info user=ada {k}={v} path=/x"):
        value = _value()
        assert value not in _mask(template.format(k=key, v=value), slot).text, template


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize(
    "text",
    [
        'raise ValueError("Cannot use `token=False` with `whoami` as it requires authentication.")',
        "set `token=False` or set the `HF_HUB_DISABLE_IMPLICIT_TOKEN` variable",
        "read it from a `DEMO_API_KEY=`-style variable",
        ">>> for i in tqdm(iterable, token='{token}', channel_id='{channel_id}'):",
        "client.send(secret='{secret}', retries=3)",
        'out = "API_KEY=" + GH_PAT + "x"',
        'token="latet_pat_" + "q" * 43,',
        'url = base + "?token=" + user_token_from_session',
    ],
)
def test_a_backtick_a_placeholder_and_a_concatenation_are_no_part_of_a_secret(text, slot):
    assert _left(text, slot)


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize(
    ("line", "field"),
    [
        ('db = Database(f"postgresql://latet:{PASSWORD}@{HERE}:5/latet", schema=SCHEMA)', "{PASSWORD}"),
        ('address = f"postgresql://{role}:{password}@{HOST}:{PORT}/{DBNAME}"', "{password}"),
        ("- staging: postgres://app:{PASSWORD}@db.internal:5432/shop", "{PASSWORD}"),
    ],
)
def test_a_password_written_as_a_field_in_the_address_of_a_database_is_still_masked(line, field, slot):
    """As in 0.9.10: a ``{name}`` is no placeholder for the password of an address (only for a mid-line value)."""
    assert field not in _mask(line, slot).text


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
def test_the_value_of_a_pair_in_a_markdown_code_span_stops_before_the_closing_backtick(slot):
    value = _value()
    masked = _mask(f"to log in use `password={value}` and then `exit`", slot)
    assert value not in masked.text and masked.text.startswith("to log in use `password=")
    assert masked.text.endswith("` and then `exit`")


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize(
    "template",
    [
        "mysql -u root --password={v} shop",
        "docker login --password={v} registry.acme.lan",
        "tool --api-key={v} run",
        "curl -s --client-secret={v} https://id.acme.lan",
        "tool -password={v}",
        "run --token={v} --verbose",
    ],
)
def test_a_pair_after_dashes_is_read(template, slot):
    value = _value()
    line = template.format(v=value)
    masked = _mask(line, slot)
    assert value not in masked.text and masked.text.count("=") == line.count("=")


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize(
    "line",
    [
        "mysql -u root --password=$DB_PASSWORD shop",
        "docker login --password=changeme x",
        "tool --api-key=${API_KEY} run",
        "tool --token-file=/run/secrets/token run",
        "run --password-stdin --verbose",
        "git commit --author=Ada --message=x",
    ],
)
def test_a_pair_after_dashes_that_holds_a_variable_or_names_no_secret_is_left_alone(line, slot):
    assert _left(line, slot)


STRONG = ["password", "passwd", "pwd", "passphrase", "secret", "client_secret", "api_key", "private_key"]
STRONG += ["access_token", "auth_token", "secret_key", "apiKey", "clientSecret", "privateKey", "accessToken"]
STRONG += ["secretKey", "authToken", "DB_PASSWORD", "Password", "SECRET"]


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize("separator", [": ", ":", " = ", " := ", " : ", "  =  "])
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("key", STRONG)
def test_a_quoted_literal_after_a_strong_name_is_masked_after_a_colon_or_a_spaced_equal_sign(
    key, quote, separator, slot
):
    for template in (
        "{{ username: 'Alice', {k}{s}{q}{v}{q} }}",
        "INFO login ok {k}{s}{q}{v}{q} for ada",
        "SET {k}{s}{q}{v}{q};",
    ):
        value = _value()
        line = template.format(k=key, s=separator, q=quote, v=value)
        masked = _mask(line, slot)
        assert value not in masked.text, line
        assert masked.text.count(quote) == line.count(quote), line  # the quotes stay


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize(
    "template",
    [
        "login ok password: {v}",  # unquoted: left alone
        "user, password = proxy_{v}",
        "def f(password: Optional[str] = None): pass  # {v}",
        "if data.password === data.confirm{v}: pass",
        'x = {{"client_secret": secret_{v}, "id": 1}}',
        "function sign(privateKey: KeyObject, data: Buffer) {{}}",
        "INFO login ok token: '{v}'",  # not a strong name
        "INFO login ok auth: '{v}'",
        "INFO login ok cookie = '{v}'",
        "x = {{password: 'changeme'}}  # {v}",  # a placeholder
        "x = {{password: ''}}  # {v}",
        "x = {{password: 'ab'}}  # {v}",  # too short
        "x = {{secret: '${{SECRET}}'}}  # {v}",
        "x = {{secret: '{{name}}'}}  # {v}",
        "x = {{api_key: 'None'}}  # {v}",
        'x = {{password: "a" + suffix_{v}}}',  # a concatenation
        "x = {{password_file: '/run/secrets/{v}'}}",  # not a secret's name
        "x = {{secret_name: 'prod/{v}'}}",
        "x = {{token_count: '12345678'}}  # {v}",
        "x = {{password_status: 'rotated-yesterday'}}",
    ],
)
def test_what_is_not_a_quoted_literal_after_a_strong_name_is_left_alone(template, slot):
    line = template.format(v=_value())
    assert _left(line, slot), line


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize(
    "line",
    [
        '<code>api_key = "<span class="fake">value</span>"</code>',  # markup, not a value
        'text = f\'BILLING_API_KEY = "{tokens["key"]}"\'',  # a template
        'USER_PASSWD = "%s:%s" % (USER, PASSWD)',  # a format
        'y = f("{x}", password = "%(password)s")',
        'print(1); secret = "prefix-{name}-suffix"',
        'pairs = [("API_KEY = ", None, None), (\'"\', None, ACCENTS[1])]',  # the key is in a string that ends there
        "user, password = 'wile', 'coyote'",  # unpacking
        "for user, password = 'ada', 'lovelace': pass",
        "wasm-opt in.wasm -O --pass-arg=asyncify-imports@env.await_promise_sync",  # an option's argument
        "tool --auth-flags=verbose-and-long --secret-options=basic-and-long",
    ],
)
def test_a_template_a_format_markup_or_an_unpacking_holds_no_literal_secret(line, slot):
    assert _left(line, slot), line


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
@pytest.mark.parametrize(
    "template",
    [
        "TEXT = 'TEST_PASSWORD = \"{v}\"\\n'",  # a secret written in a text
        "env = {{'API_KEY': \"{v}\"}}",
        "SET password = '{v}';",
        "assert render({{'SECRET': '{v}'}}) == 'x'",
    ],
)
def test_a_literal_secret_written_in_a_string_or_a_dict_is_masked(template, slot):
    value = _value()
    masked = _mask(template.format(v=value), slot)
    assert value not in masked.text, template


@pytest.mark.parametrize("slot", SLOTS.values(), ids=SLOTS)
def test_a_quoted_literal_with_spaces_is_masked_whole(slot):
    value = _value()
    masked = _mask(f'INFO login ok secret: "two words {value}" and more', slot)
    assert "two words" not in masked.text and masked.text.startswith('INFO login ok secret: "')


def test_the_name_of_a_pair_is_read_once_for_all_its_lines():
    structural._key_kind.cache_clear()
    for _ in range(3):
        assert structural._key_kind("DB_PASSWORD") == "password"
    assert structural._key_kind.cache_info().hits >= 2
