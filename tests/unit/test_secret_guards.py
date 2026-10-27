"""The guards that refuse a secret of Latet's pasted where it does not belong (a name, an id, a label, a token file) know
every prefix: the SCIM token's and the bridge token's with the four before them, a one-time code by its whole shape (a
word that only begins like one, `latet_code_review`, is a word)."""

import json

import pytest
import typer

from latet.cli.server import _not_secret
from latet.engine.detectors.latet import holds_a_secret_start, starts_a_secret
from latet.server.admin import _key_token_asked, _token_asked

TAIL = "Q7wZ2k4NBX5pL3mD_r8S-v6Hj9Ta0Bc1Eo2Fi3Gu4J5"
SECRETS = [f"latet_{prefix}_{TAIL}" for prefix in ("pat", "ck", "key", "adm", "scim", "brg")] + [
    "latet_code_" + TAIL[:32]
]
WORDS = ["latet_code_review", "latet_code_review_comments_for_the_dashboard_and_more", "latet_codec_x", "latet_brg"]


@pytest.mark.parametrize("secret", SECRETS)
def test_a_secret_is_known_where_it_starts_a_text_and_where_it_sits_in_one(secret):
    assert starts_a_secret(secret) and starts_a_secret(secret + " and more")
    assert holds_a_secret_start(f"before {secret}") and holds_a_secret_start(secret[:-5]) == (
        not secret.startswith("latet_code_")  # a code is known by its whole shape, a cut one is a word
    )


@pytest.mark.parametrize("word", WORDS)
def test_a_word_that_only_begins_like_a_secret_is_not_one(word):
    assert not starts_a_secret(word) and not holds_a_secret_start(f"see {word} and {word}.")


@pytest.mark.parametrize("secret", SECRETS)
def test_a_cli_argument_that_is_a_secret_is_refused_in_words_that_never_repeat_it(secret, capsys):
    with pytest.raises(typer.Exit):
        _not_secret(secret)
    assert secret not in capsys.readouterr().err


@pytest.mark.parametrize("word", WORDS)
def test_a_cli_argument_that_is_a_word_is_let_through(word):
    _not_secret(word)


@pytest.mark.parametrize("secret", SECRETS)
def test_a_token_label_holding_a_secret_is_refused_in_words_that_never_repeat_it(secret):
    for label in (secret, f"laptop {secret}", f"{secret.upper()}"):
        refused = _token_asked(json.dumps({"label": label, "days": 30}).encode())
        assert isinstance(refused, str) and "is no place for a secret" in refused and TAIL[:12] not in refused


@pytest.mark.parametrize("word", ["laptop", "ci-runner", *WORDS])
def test_a_token_label_that_is_a_word_is_let_through(word):
    assert _token_asked(json.dumps({"label": word, "days": 30}).encode()) == (word, 30, False)


@pytest.mark.parametrize("secret", SECRETS)
def test_a_key_tokens_name_holding_a_secret_is_refused_in_words_that_never_repeat_it(secret):
    """The final review's M3 and the security review's L1: an API key's added token's name is kept in clear and listed
    to every administrator, as an administration token's label is: the administration API refuses one that holds a
    secret of Latet's anywhere, whatever its case, as it refuses such a label."""
    for name in (secret, f"logs {secret}", secret.upper()):
        refused = _key_token_asked(json.dumps({"name": name, "permissions": ["mask"]}).encode())
        assert isinstance(refused, str) and "is no place for a secret" in refused and TAIL[:12] not in refused


@pytest.mark.parametrize("word", ["logs", "ci-runner", *WORDS])
def test_a_key_tokens_name_that_is_a_word_is_let_through(word):
    asked = _key_token_asked(json.dumps({"name": word, "permissions": ["mask"]}).encode())
    assert not isinstance(asked, str) and asked[0] == word
