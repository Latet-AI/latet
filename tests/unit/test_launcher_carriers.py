"""Finite CLI assembly and source-derived carrier semantics; no client/provider execution."""

import json
import re
import tomllib

import pytest
from typer.testing import CliRunner

from latet import wrappers
from latet.cli.main import app
from latet.config import Config

from .test_gemini_wrapper import gemini_home  # noqa: F401 - fixture
from .test_wrapper_routes import REAL_RUN, launched  # noqa: F401 - fixture

KEY = "latet_lk_" + "A" * 43
OLD = "latet_lk_" + "B" * 43
BASE = "http://127.0.0.1:5283"
HEADER = "x-latet-launch-key"


def _configuration():
    return (
        'model_provider = "acme"\n[model_providers.acme]\n'
        'base_url = "https://llm.acme.example/v1"\nwire_api = "responses"\n'
        f'http_headers = {{ "X-Company" = "kept", "X-Latet-Launch-Key" = "{OLD}" }}\n'
        'env_http_headers = { "x-company-env" = "COMPANY_HEADER", '
        '"X-LATET-LAUNCH-KEY" = "OLD_HEADER", "x-latet-launch-key" = "OLDER_HEADER" }\n'
    )


def _merge(left, right):
    """Pinned Codex merge.rs table behavior for this small source fixture."""
    out = dict(left)
    for name, value in right.items():
        if isinstance(out.get(name), dict) and isinstance(value, dict):
            out[name] = _merge(out[name], value)
        else:
            out[name] = value
    return out


def _source_headers(table, env):
    """Pinned build_header_map subset: valid names/ASCII fixture values only.

    Static then env insertion, case normalization, blank-env omission, invalid
    newline rejection. This is source-derived, not an external Codex execution.
    """
    result = {}
    for name, value in table.get("http_headers", {}).items():
        if "\n" not in value and "\r" not in value:
            result[name.lower()] = value
    for name, variable in table.get("env_http_headers", {}).items():
        value = env.get(variable)
        if value is not None and value.strip() and "\n" not in value and "\r" not in value:
            result[name.lower()] = value
    return result


@pytest.mark.parametrize("agent", ["codex", "copilot"])
@pytest.mark.parametrize("capacity", [False, True], ids=["owned", "capacity"])
def test_other_carrier_cli_ack_before_publication(launched, monkeypatch, agent, capacity):  # noqa: F811
    _, _, tmp_path = launched
    monkeypatch.setattr(wrappers, "_codex_policy_paths", lambda: (tmp_path / "absent-policy.toml",))
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 162, 0))
    for name in ("COPILOT_PROVIDER_WIRE_API", "COPILOT_PROVIDER_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_PROVIDER_TYPE", "openai")
    monkeypatch.setenv("COPILOT_PROVIDER_HEADERS", f"X-Company: kept\nX-Latet-Launch-Key: {OLD}\nX-Company: second")
    monkeypatch.setenv("COMPANY_HEADER", "env-kept")
    monkeypatch.setenv("OLD_HEADER", OLD)
    monkeypatch.setenv("OLDER_HEADER", OLD)
    monkeypatch.setenv("LATET_CODEX_LAUNCH_KEY", OLD)
    monkeypatch.setenv("LATET_CODEX_NO_LAUNCH_KEY", OLD)
    original = _configuration()
    source = tmp_path / "codex" / "config.toml"
    source.write_text(original, "utf-8")
    events = []
    recorded = []
    profiles = []
    base = "/_latet/engine/v1/launches"

    class Hold:
        generation = 1

        def wait(self):
            pass

        def close(self):
            events.append("hold-close")

    def call(method, path, body, **kwargs):
        assert method == "POST"
        if path == base:
            events.append("open")
            if capacity:
                return {"key": None, "launch_id": None, "reason": "launch-capacity"}
            return {"key": KEY, "launch_id": "1" * 32}
        if path.endswith("/end"):
            events.append("end")
            return {"ended": True}
        if path.endswith("/prepare"):
            return {"epoch": "2" * 32, "retirement": 0}
        if path.endswith("/exit"):
            events.append("exit")
            return {"retired": True, "epoch": "2" * 32, "retirement": 1}
        pytest.fail("unexpected control operation")

    def hold(identifier, generation, **kwargs):
        assert identifier == "1" * 32 and generation == 0
        events.append("ack")
        return Hold()

    write = wrappers.write_atomic

    def publish(path, content, **kwargs):
        if path.name.startswith("latet-") and path.name.endswith(".config.toml"):
            assert events[:1] == ["open"]
            assert ("ack" in events) is not capacity
            assert KEY not in content and OLD not in content
            events.append("publish")
            profiles.append(path)
        return write(path, content, **kwargs)

    def child(argv, env, owner=None):
        assert events[:1] == ["open"]
        assert ("ack" in events) is not capacity
        assert (owner is None) is capacity
        assert KEY not in json.dumps(argv)
        assert env["LATET_CODEX_NO_LAUNCH_KEY"] == OLD
        if agent == "codex":
            path = tmp_path / "codex" / f"{argv[argv.index('--profile') + 1]}.config.toml"
            private = path.read_text("utf-8")
            effective = _merge(tomllib.loads(original), tomllib.loads(private))
            headers = _source_headers(effective["model_providers"]["acme"], env)
            assert headers["x-company"] == "kept" and headers["x-company-env"] == "env-kept"
            assert headers.get(HEADER) == (None if capacity else KEY)
            assert KEY not in private and OLD not in private
        else:
            headers = env["COPILOT_PROVIDER_HEADERS"]
            assert headers.splitlines()[:2] == ["X-Company: kept", "X-Company: second"]
            assert headers.count(HEADER + ":") == (0 if capacity else 1)
            assert (KEY in headers) is not capacity and OLD not in headers
            assert env["LATET_CODEX_LAUNCH_KEY"] == OLD
        events.append("child")
        recorded.append(argv)
        return 0

    monkeypatch.setattr(wrappers, "_launch_call", call)
    monkeypatch.setattr(wrappers, "_launch_hold", hold)
    monkeypatch.setattr(wrappers, "write_atomic", publish)
    monkeypatch.setattr(wrappers, "run", REAL_RUN)
    monkeypatch.setattr(wrappers.subprocess, "call", lambda argv, env: child(argv, env))
    monkeypatch.setattr("latet.launch_session.run_owned", lambda owner, argv, env: child(argv, env, owner))
    result = CliRunner().invoke(app, [agent, "exec", "hello"] if agent == "codex" else [agent, "-p", "hello"])
    assert result.exit_code == 0
    assert len(recorded) == 1 and events.count("open") == 1
    assert KEY not in result.output and OLD not in result.output
    assert source.read_text("utf-8") == original
    assert all(not path.exists() for path in profiles)
    if not capacity:
        assert events.index("end") > events.index("child")


@pytest.mark.parametrize(
    "selection",
    [
        {"COPILOT_PROVIDER_WIRE_API": "responses"},
        {"COPILOT_PROVIDER_TYPE": "anthropic"},
        {"COPILOT_PROVIDER_TRANSPORT": "websockets"},
    ],
    ids=["responses", "anthropic", "websocket"],
)
def test_unqualified_copilot_carriers_do_not_open(launched, monkeypatch, selection):  # noqa: F811
    runs, _, _ = launched
    for name in ("COPILOT_PROVIDER_WIRE_API", "COPILOT_PROVIDER_TRANSPORT", "COPILOT_PROVIDER_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_PROVIDER_TYPE", "openai")
    monkeypatch.setenv("COPILOT_PROVIDER_HEADERS", f"X-Company: kept\nX-Latet-Launch-Key: {OLD}")
    for name, value in selection.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(wrappers, "_launch_call", lambda *args, **kwargs: pytest.fail("unqualified must not OPEN"))
    result = CliRunner().invoke(app, ["copilot", "-p", "hello"])
    assert result.exit_code == 0
    assert len(runs) == 1 and runs[0]["env"]["COPILOT_PROVIDER_HEADERS"] == "X-Company: kept"
    assert "loosenings do not apply" in result.output and OLD not in result.output


@pytest.mark.parametrize("owned", [False, True], ids=["keyless", "owned"])
def test_codex_source_derived_alias_tombstones(tmp_path, monkeypatch, owned):
    monkeypatch.setattr(wrappers, "_codex_policy_paths", lambda: (tmp_path / "absent.toml",))
    texts = {"config.toml": _configuration()}
    live, keyless, _, carrier_names = wrappers.codex_carrier_profiles(
        texts, None, ["exec", "hello"], {"model_providers.acme.base_url": f"{BASE}/acme"}, Config(), BASE, False
    )
    assert live is not None and keyless is not None and carrier_names is not None
    active, absent = carrier_names
    private = live if owned else keyless
    table = _merge(tomllib.loads(texts["config.toml"]), tomllib.loads(private))["model_providers"]["acme"]
    # Every old static spelling is neutralized; every noncanonical env alias
    # names the variable wrappers.run always removes, not the old caller variable.
    assert table["http_headers"]["X-Latet-Launch-Key"] == "\n"
    assert table["env_http_headers"]["X-LATET-LAUNCH-KEY"] == absent
    env = {"COMPANY_HEADER": "env-kept", "OLD_HEADER": OLD, "OLDER_HEADER": OLD}
    if owned:
        env[active] = KEY
    headers = _source_headers(table, env)
    assert headers == {"x-company": "kept", "x-company-env": "env-kept", **({HEADER: KEY} if owned else {})}
    assert KEY not in private and OLD not in private


def test_codex_policy_and_selection_unknowns_stay_unqualified(tmp_path, monkeypatch):
    policy = tmp_path / "requirements.toml"
    monkeypatch.setattr(wrappers, "_codex_policy_paths", lambda: (policy,))
    assert wrappers._codex_policy_clear()
    policy.write_text('model_provider = "managed"\n', "utf-8")
    assert not wrappers._codex_policy_clear()
    texts = {"config.toml": _configuration()}
    arguments = (
        texts,
        None,
        ["exec", "hello"],
        {"model_providers.acme.base_url": f"{BASE}/acme"},
        Config(),
        BASE,
        False,
    )
    assert wrappers.codex_carrier_profiles(*arguments)[0] is None
    assert policy.read_text("utf-8") == 'model_provider = "managed"\n'
    monkeypatch.setattr(wrappers, "_codex_policy_paths", lambda: None)
    assert not wrappers._codex_policy_clear()

    def unavailable():
        raise OSError("fixture lookup failure")

    monkeypatch.setattr(wrappers, "_codex_policy_paths", unavailable)
    assert not wrappers._codex_policy_clear()
    monkeypatch.setattr(wrappers, "_codex_policy_paths", lambda: (tmp_path / "absent.toml",))
    overridden = ["-c", 'model_provider="other"', "exec", "hello"]
    assert wrappers.codex_carrier_profiles(texts, None, overridden, {}, Config(), BASE, False)[:2] == (None, None)
    builtin = {"config.toml": 'model_provider = "openai"\n'}
    assert wrappers.codex_carrier_profiles(builtin, None, [], {}, Config(), BASE, False)[:2] == (None, None)


def _record_codex_invocation(launch_fixture, monkeypatch, text, version):
    """Actual CLI/owner assembly, inert control transport and child recorder."""
    _, _, tmp_path = launch_fixture
    source = tmp_path / "codex" / "config.toml"
    source.write_text(text, "utf-8")
    monkeypatch.setattr(wrappers, "_codex_policy_paths", lambda: (tmp_path / "absent-policy.toml",))
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": version)
    calls = []
    children = []
    base = "/_latet/engine/v1/launches"

    class Hold:
        generation = 1

        def wait(self):
            pass

        def close(self):
            pass

    def call(method, path, body, **kwargs):
        calls.append((method, path))
        if path == base:
            return {"key": KEY, "launch_id": "1" * 32}
        if path.endswith("/end"):
            return {"ended": True}
        if path.endswith("/prepare"):
            return {"epoch": "2" * 32, "retirement": 0}
        if path.endswith("/exit"):
            return {"retired": True, "epoch": "2" * 32, "retirement": 1}
        pytest.fail("unexpected operation")

    def record(argv, env):
        private = None
        if "--profile" in argv:
            name = argv[argv.index("--profile") + 1]
            private = (tmp_path / "codex" / f"{name}.config.toml").read_text("utf-8")
        children.append((argv, env, private))
        return 0

    monkeypatch.setattr(wrappers, "_launch_call", call)
    monkeypatch.setattr(wrappers, "_launch_hold", lambda *args, **kwargs: Hold())
    monkeypatch.setattr(wrappers, "run", REAL_RUN)
    monkeypatch.setattr(wrappers.subprocess, "call", record)
    monkeypatch.setattr("latet.launch_session.run_owned", lambda owner, argv, env: record(argv, env))
    result = CliRunner().invoke(app, ["codex", "exec", "hello"])
    assert result.exit_code == 0
    assert source.read_text("utf-8") == text
    assert len(children) == 1
    assert KEY not in result.output
    assert not tuple((tmp_path / "codex").glob("latet-*.config.toml"))
    return calls, children[0]


def test_codex_carrier_does_not_repurpose_ordinary_environment(launched, monkeypatch):  # noqa: F811
    # First generated pair conflicts with an inactive provider's ordinary map;
    # second conflicts with an actual inherited variable in different case.
    nonce = "C" * 32
    inactive = f"LATET_CARRIER_KEY_{nonce}_0"
    inherited = f"latet_carrier_none_{nonce.lower()}_1"
    monkeypatch.setattr(wrappers.secrets, "token_hex", lambda count: "c" * (count * 2))
    monkeypatch.setenv("LATET_CODEX_LAUNCH_KEY", "company-value")
    monkeypatch.setenv("LATET_CODEX_NO_LAUNCH_KEY", "other-company-value")
    monkeypatch.setenv(inherited, "preserve-this-too")
    text = _configuration().replace(
        '"x-company-env" = "COMPANY_HEADER"',
        '"x-company-env" = "LATET_CODEX_LAUNCH_KEY", "x-company-other" = "LATET_CODEX_NO_LAUNCH_KEY"',
    )
    text += f'\n[model_providers.inactive]\nenv_http_headers = {{ "X-Inactive" = "{inactive.lower()}" }}\n'
    calls, (argv, env, private) = _record_codex_invocation(launched, monkeypatch, text, (0, 162, 0))
    assert calls[0] == ("POST", "/_latet/engine/v1/launches")
    assert private is not None
    effective = _merge(tomllib.loads(text), tomllib.loads(private))
    table = effective["model_providers"]["acme"]
    headers = _source_headers(table, env)
    # These assertions are the causal distinction from fix1's fixed-name leak
    # and unconditional deletion, after actual owner cleanup has completed.
    assert headers["x-company-env"] == "company-value"
    assert headers["x-company-other"] == "other-company-value"
    assert headers[HEADER] == KEY and KEY not in {value for name, value in headers.items() if name != HEADER}
    assert env["LATET_CODEX_LAUNCH_KEY"] == "company-value"
    assert env["LATET_CODEX_NO_LAUNCH_KEY"] == "other-company-value"
    assert next(value for name, value in env.items() if name.upper() == inherited.upper()) == "preserve-this-too"
    active = table["env_http_headers"][HEADER]
    absent = table["env_http_headers"]["X-LATET-LAUNCH-KEY"]
    ordinary = {"LATET_CODEX_LAUNCH_KEY", "LATET_CODEX_NO_LAUNCH_KEY", inactive, inherited.upper()}
    assert active.upper() not in ordinary and absent.upper() not in ordinary
    assert absent not in env and env[active] == KEY
    assert KEY not in private and KEY not in json.dumps(argv)


def test_legacy_codex_without_actual_profile_never_opens(launched, monkeypatch):  # noqa: F811
    text = _configuration().replace("https://llm.acme.example/v1", f"{BASE}/acme")
    calls, (argv, env, private) = _record_codex_invocation(launched, monkeypatch, text, (0, 110, 0))
    # codex_argv's legacy success returns OPENAI_BASE_URL and no generated
    # profile. Inspect the actual child output, not its prospective path.
    assert calls == []
    assert "--profile" not in argv and private is None
    assert env["OPENAI_BASE_URL"] == f"{BASE}/openai/v1"
    assert KEY not in env.values()


def _remaining_carrier_invocation(launch_fixture, settings_home, monkeypatch, agent, capacity=False, blocked=None):
    """Real CLI/wrapper/session assembly; inert transport and child only."""
    _, _, tmp_path = launch_fixture
    from latet.integrations import jsonc

    original = (
        '{\n // preserve the company comment\n "model": "openai/gpt-4.1",'
        ' "instructions": ["ordinary,}"], "provider": {"openai": {"options": {'
        '"headers": {"X-Company": "{env:COMPANY_VALUE}", "X-Latet-Launch-Key": "' + OLD + '"}}}}}\n'
    )
    monkeypatch.setenv("COMPANY_VALUE", "company-value")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", original)
    monkeypatch.delenv("OPENCODE_AUTH_CONTENT", raising=False)
    policy = tmp_path / "policy.json"
    monkeypatch.setattr(wrappers, "_opencode_sources", lambda: ([], [policy], []))
    settings = settings_home / ".gemini" / "settings.json"
    settings.write_text('{"security":{"auth":{"selectedType":"gemini-api-key"}}}', "utf-8")
    monkeypatch.setenv("GEMINI_CLI_CUSTOM_HEADERS", f"X-Company: a,b, X-Url: https://example.test:443, {HEADER}: {OLD}")
    if blocked == "policy-opencode":
        policy.write_text('{"provider":{}}', "utf-8")
    elif blocked == "dynamic-opencode":
        original = original.replace("{env:COMPANY_VALUE}", "{file:company.txt}")
        monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", original)
    elif blocked == "policy-gemini":
        settings.write_text(
            '{"security":{"auth":{"selectedType":"gemini-api-key"}},"customHeaders":{"X-Latet-Launch-Key":"old"}}',
            "utf-8",
        )
    selected_base = BASE
    if blocked == "escape-gemini":
        selected_base += "/u/own"
        monkeypatch.setattr("latet.server.connection._agent_base", lambda *_args: selected_base)
        settings.write_text(
            json.dumps(
                {
                    "security": {"auth": {"selectedType": "gemini-api-key"}},
                    "baseUrl": selected_base + "/../../elsewhere",
                }
            ),
            "utf-8",
        )
    settings_before = settings.read_text("utf-8")
    events = []
    captured = []
    base = "/_latet/engine/v1/launches"

    class Hold:
        generation = 1

        def wait(self):
            pass

        def close(self):
            events.append("hold-close")

    def call(method, path, body, **_kwargs):
        assert method == "POST"
        if path == base:
            assert blocked is None
            events.append("open")
            if capacity:
                return {"key": None, "launch_id": None, "reason": "launch-capacity"}
            return {"key": KEY, "launch_id": "1" * 32}
        if path.endswith("/end"):
            events.append("end")
            return {"ended": True}
        if path.endswith("/prepare"):
            return {"epoch": "2" * 32, "retirement": 0}
        if path.endswith("/exit"):
            return {"retired": True, "epoch": "2" * 32, "retirement": 1}
        pytest.fail("unexpected launch operation")

    def hold(identifier, generation, **_kwargs):
        assert identifier == "1" * 32 and generation == 0
        events.append("ack")
        return Hold()

    def child(argv, env, owner=None):
        owned = blocked is None and not capacity
        assert (owner is not None) is owned
        assert ("ack" in events) is owned
        assert KEY not in json.dumps(argv)
        if agent == "opencode":
            text = env["OPENCODE_CONFIG_CONTENT"]
            assert "// preserve the company comment" in text and '"ordinary,}"' in text
            assert KEY not in text and OLD not in text
            assert env["COMPANY_VALUE"] == "company-value"
            headers = jsonc.loads(text)["provider"]["openai"]["options"]["headers"]
            expected = "{file:company.txt}" if blocked == "dynamic-opencode" else "{env:COMPANY_VALUE}"
            assert headers["X-Company"] == expected
            if owned:
                match = re.fullmatch(r"\{env:([^{}]+)\}", headers[HEADER])
                assert match is not None and env[match[1]] == KEY
                assert match[1] != "COMPANY_VALUE"
            else:
                assert all(name.lower() != HEADER for name in headers)
        else:
            headers = env["GEMINI_CLI_CUSTOM_HEADERS"]
            assert headers.startswith("X-Company: a,b, X-Url: https://example.test:443")
            assert (KEY in headers) is owned and OLD not in headers
            assert env["GOOGLE_GEMINI_BASE_URL"] == selected_base + "/gemini"
        events.append("child")
        captured.append((argv, env))
        return 0

    monkeypatch.setattr(wrappers, "_launch_call", call)
    monkeypatch.setattr(wrappers, "_launch_hold", hold)
    monkeypatch.setattr(wrappers, "run", REAL_RUN)
    monkeypatch.setattr(wrappers.subprocess, "call", lambda argv, env: child(argv, env))
    monkeypatch.setattr("latet.launch_session.run_owned", lambda owner, argv, env: child(argv, env, owner))
    result = CliRunner().invoke(app, [agent, "-p", "hello"])
    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert events.count("open") == (1 if blocked is None else 0)
    assert KEY not in result.output and OLD not in result.output
    if blocked is None and not capacity:
        assert events.index("ack") < events.index("child") < events.index("end")
    else:
        assert "loosenings do not apply" in result.output
    assert settings.read_text("utf-8") == settings_before
    return captured[0]


@pytest.mark.parametrize("agent", ["opencode", "gemini"])
@pytest.mark.parametrize("capacity", [False, True], ids=["owned", "capacity"])
def test_remaining_carriers_cli_ack_before_publication(
    launched,  # noqa: F811
    gemini_home,  # noqa: F811
    monkeypatch,
    agent,
    capacity,
):
    _remaining_carrier_invocation(launched, gemini_home, monkeypatch, agent, capacity)


@pytest.mark.parametrize("blocked", ["policy-opencode", "dynamic-opencode", "policy-gemini"])
def test_remaining_carrier_unknowns_start_without_new_key(launched, gemini_home, monkeypatch, blocked):  # noqa: F811
    agent = "gemini" if blocked.endswith("gemini") else "opencode"
    _remaining_carrier_invocation(launched, gemini_home, monkeypatch, agent, blocked=blocked)


def test_remaining_carrier_source_grammar_preserves_ordinary_values():
    # Explicit pinned JS split/trim cases: embedded comma, first colon, BOM,
    # case aliases, repeated ordinary entries. This is not a Gemini execution.
    original = f" X-One: a,b, X-Url: https://host:443, \ufeffX-Latet-Launch-Key: {OLD}, X-One: last"
    safe = wrappers.gemini_headers(original, KEY)
    assert safe == f" X-One: a,b, X-Url: https://host:443, X-One: last,{HEADER}: {KEY}"
    assert wrappers.gemini_headers(safe, None) == " X-One: a,b, X-Url: https://host:443, X-One: last"
    raw = (
        '{"provider":{"openai":{"options":{"headers":{"X-One":"ordinary,}",'
        '"x-latet-launch-key":"old1","x-latet-launch-key":"old2","X-Latet-Launch-Key":"old3"}}}}}'
    )
    clean = wrappers.opencode_keyless_content(raw)
    assert '"X-One":"ordinary,}"' in clean and "old" not in clean and HEADER not in clean.lower()
    commented = (
        '{"provider":{"openai":{"options":{"headers":{'
        '"x-latet-launch-key":"old1" /* keep , comment */, "X-One":"ordinary,}",'
        '"X-Latet-Launch-Key":"old2"}}}}}'
    )
    clean = wrappers.opencode_keyless_content(commented)
    assert "old1" not in clean and "old2" not in clean
    assert "/* keep , comment */" in clean and '"X-One":"ordinary,}"' in clean
    from latet.integrations import jsonc

    assert set(jsonc.loads(clean)["provider"]["openai"]["options"]["headers"]) == {"X-One"}
    only = '{"provider":{"openai":{"options":{"headers":{"x-latet-launch-key":"old" /* only */}}}}}'
    clean_only = wrappers.opencode_keyless_content(only)
    assert "/* only */" in clean_only
    assert jsonc.loads(clean_only)["provider"]["openai"]["options"]["headers"] == {}
    malformed = '{"provider": unknown'
    assert wrappers.opencode_routed_content(malformed, BASE) == malformed


def test_generic_run_stays_keyless_and_scrubs_controlled_containers(launched, monkeypatch):  # noqa: F811
    monkeypatch.setenv("GEMINI_CLI_CUSTOM_HEADERS", f"X-Company: kept,{HEADER}:{OLD}")
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT", '{"provider":{"openai":{"options":{"headers":{"x-latet-launch-key":"old"}}}}}'
    )
    monkeypatch.setenv("LATET_LAUNCH_ID", "old-id")
    captured = []
    monkeypatch.setattr(wrappers, "run", REAL_RUN)
    monkeypatch.setattr(wrappers.subprocess, "call", lambda argv, env: captured.append((argv, env)) or 0)
    result = CliRunner().invoke(app, ["run", "ordinary-tool", "--ordinary"])
    assert result.exit_code == 0 and "loosenings do not apply" in result.output
    [(argv, env)] = captured
    assert argv[1:] == ["--ordinary"] and "LATET_LAUNCH_ID" not in env
    assert env["GEMINI_CLI_CUSTOM_HEADERS"] == "X-Company: kept"
    assert HEADER not in env["OPENCODE_CONFIG_CONTENT"]
    assert KEY not in env.values()


def test_opencode_named_source_inventory_does_not_misread_false_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(wrappers.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OPENCODE_TEST_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENCODE_TEST_MANAGED_CONFIG_DIR", str(tmp_path / "managed"))
    monkeypatch.setenv("OPENCODE_DISABLE_PROJECT_CONFIG", "false")
    monkeypatch.chdir(tmp_path)
    result = wrappers._opencode_sources()
    assert result is not None
    sources, blocked, directories = result
    assert tmp_path / "opencode.jsonc" in sources
    assert tmp_path / "managed" / "opencode.json" in blocked
    assert tmp_path / "data" / "opencode" / "auth.json" in blocked
    assert tmp_path / ".opencode" in directories


def test_gemini_settings_route_escape_stays_keyless(launched, gemini_home, monkeypatch):  # noqa: F811
    _remaining_carrier_invocation(launched, gemini_home, monkeypatch, "gemini", blocked="escape-gemini")
    # Same selected scope, exact existing canonical-path proof; no URL resolver.
    base = BASE + "/u/own"
    assert wrappers._header_sources_safe({"baseUrl": base + "/gemini"}, base)
    assert not wrappers._header_sources_safe({"baseUrl": base + "/%2e%2e/other"}, base)
    assert not wrappers._header_sources_safe({"baseUrl": base + "/gemini?x=1"}, base)
    assert not wrappers._header_sources_safe({"baseUrl": base + "/{env:TAIL}"}, base)
    assert not wrappers._header_sources_safe({"baseUrl": base + "/{file:tail}"}, base)
