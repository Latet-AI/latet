"""scripts/bench_apps.py: the log lines it measures with are the same at every run and hold what it says, and the token
is read from a file, never from the command line."""

import importlib.util
import ipaddress
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "bench_apps", Path(__file__).resolve().parents[2] / "scripts" / "bench_apps.py"
)
assert _SPEC is not None and _SPEC.loader is not None
bench = importlib.util.module_from_spec(_SPEC)
sys.modules["bench_apps"] = bench  # its dataclasses look their module up there
_SPEC.loader.exec_module(bench)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")


def test_the_lines_are_the_same_at_every_run_and_another_seed_gives_others():
    assert bench.lines(500, 1) == bench.lines(500, 1)
    assert bench.lines(500, 1) != bench.lines(500, 2)


def test_each_line_holds_one_email_and_one_public_ipv4_address_in_about_200_characters():
    found = bench.lines(2000, 7)
    assert len(found) == 2000
    for line in found:
        assert len(_EMAIL.findall(line)) == 1, line
        [address] = _IPV4.findall(line)
        ip = ipaddress.ip_address(address)  # what Latet masks: a private one, or a multicast one, it leaves as it is
        assert ip.is_global and not (ip.is_multicast or ip.is_reserved), line
    assert 180 <= sum(map(len, found)) / len(found) <= 220
    assert all(120 <= len(line) <= 280 for line in found)


@pytest.mark.parametrize("repeat", [0.0, 0.3, 0.5, 0.8])
def test_the_repeat_share_of_the_lines_names_a_customer_seen_before(repeat):
    found = bench.lines(4000, 3, repeat=repeat)
    seen: set[str] = set()
    again = 0
    for line in found:
        [email] = _EMAIL.findall(line)
        again += email in seen
        seen.add(email)
    assert abs(again / len(found) - repeat) < 0.03


def test_the_customers_named_first_are_the_same_whatever_the_repeat_share():
    """The customers a run names for the first time come in one order, whatever share repeats: a vault filled with
    the first ones (repeat 0) holds those a later run names."""
    first = [_EMAIL.findall(line)[0] for line in bench.lines(600, 5, repeat=0.0)]
    later: list[str] = []
    for line in bench.lines(600, 5, repeat=0.5):
        [email] = _EMAIL.findall(line)
        if email not in later:
            later.append(email)
    assert 200 < len(later) < 400 and later == first[: len(later)]


def test_with_session_tokens_each_line_ends_with_one_of_its_own_and_is_otherwise_the_same():
    """The final review's I1: a log with a short secret in every line, which a key's space holds in memory glued (its
    newest ``key_held_secrets``), to measure an unmask with that set full."""
    plain, tokened = bench.lines(1500, 4), bench.lines(1500, 4, tokens=True)
    assert tokened == bench.lines(1500, 4, tokens=True)
    found = [re.fullmatch(r"(.*) session_token=([0-9a-f]{16})", line, re.DOTALL) for line in tokened]
    assert all(found) and [m.group(1) for m in found if m] == plain
    assert len({m.group(2) for m in found if m}) == 1500


def test_the_parser_takes_the_defaults_and_the_token_from_a_file():
    args = bench.parser().parse_args(["--url", "http://127.0.0.1:5283", "--token-file", "token.txt"])
    assert (args.url, args.token_file) == ("http://127.0.0.1:5283", Path("token.txt"))
    assert (args.lines, args.batch, args.documents, args.repeat, args.seed) == (20000, 500, 50, 0.5, 1)
    assert args.unmask is True and args.session_tokens is False
    assert bench.parser().parse_args(["--url", "u", "--token-file", "t", "--no-unmask"]).unmask is False
    assert bench.parser().parse_args(["--url", "u", "--token-file", "t", "--session-tokens"]).session_tokens is True


@pytest.mark.parametrize("given", [["--token", "latet_key_x"], ["--tok", "latet_key_x"], ["--token=latet_key_x"]])
def test_no_option_takes_the_token_itself(given, capsys):
    """A token on the command line would be in the shell's history and the process list: the parser has no option for
    it, and takes no abbreviation of ``--token-file`` for one."""
    with pytest.raises(SystemExit):
        bench.parser().parse_args(["--url", "http://127.0.0.1:5283", "--token-file", "token.txt", *given])
    assert "latet_key_x" not in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["--url", "--token-file"])
def test_the_url_and_the_token_file_are_required(missing):
    given = {"--url": "http://127.0.0.1:5283", "--token-file": "token.txt"}
    del given[missing]
    with pytest.raises(SystemExit):
        bench.parser().parse_args([part for pair in given.items() for part in pair])


@pytest.mark.parametrize("given", [["--repeat", "1.5"], ["--repeat", "-0.1"], ["--batch", "0"], ["--lines", "-1"]])
def test_values_out_of_range_are_refused(given):
    with pytest.raises(SystemExit):
        bench.parser().parse_args(["--url", "http://127.0.0.1:5283", "--token-file", "token.txt", *given])


def test_a_token_file_written_with_a_byte_order_mark_is_read_without_it(tmp_path):
    """Notepad and PowerShell 5 write UTF-8 with a mark at the start: the token is the text after it."""
    path = tmp_path / "token.txt"
    path.write_bytes(b"\xef\xbb\xbflatet_key_x\r\n")
    assert bench._token(path) == "latet_key_x"


def _answering(results_of):
    """A client whose server answers each call's texts with ``results_of(texts)``."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": results_of(json.loads(request.content)["texts"])})

    return httpx.Client(base_url="http://127.0.0.1:5283", transport=httpx.MockTransport(handle))


def test_a_measurement_says_how_many_values_each_text_had_replaced_or_restored():
    """A run that masks nothing must not look good: the line says the values replaced (mask) or restored (unmask) for
    each text, beside the texts a second."""
    places = [{"type": "EMAIL", "class": "pii", "start": 0, "end": 1}]
    with _answering(lambda texts: [{"text": t, "replacements": places * 2, "counts": {}} for t in texts]) as client:
        masked = bench.run(client, "mask", ["a", "b", "c"], 2, "data")
    assert (masked.texts, masked.replaced) == (3, 6)
    assert "values/text" in bench.HEADER
    assert masked.line("mask", "log lines").split()[-1] == "2.00"
    with _answering(lambda texts: [{"text": t, "restored": 1} for t in texts]) as client:
        restored = bench.run(client, "unmask", ["a", None, "c", "d"], 2, None)
    assert (restored.texts, restored.replaced) == (3, 3)
    assert restored.line("unmask", "log lines").split()[-1] == "1.00"
    with _answering(lambda texts: [{"text": t, "replacements": [], "counts": {}} for t in texts]) as client:
        assert bench.run(client, "mask", ["a"], 1, "data").line("mask", "log lines").split()[-1] == "0.00"
