"""Where each agent sends its requests today, and how Latet goes in front: read from the agent's own
settings (never a repository's), with what cannot be routed said plainly."""

import json
from datetime import UTC, datetime

import pytest

from latet.config import Config, Destination
from latet.destinations import name_for
from latet.gateway.aws import profile_tag
from latet.integrations.routes import (
    claude_sources,
    codex_texts,
    destinations_pointed_at,
    detect_claude,
    detect_codex,
)

TOKEN = "bridge-token"
LATET = "http://127.0.0.1:5283"
VPC = "https://vpce-0abc1234.bedrock-runtime.us-east-1.vpce.amazonaws.com"  # a VPC endpoint for Bedrock
PUBLIC = "https://bedrock-runtime.eu-west-1.amazonaws.com"  # Amazon's own address for it
FIPS = "https://bedrock-runtime-fips.us-gov-west-1.amazonaws.com"  # and its FIPS address, in another region


class Registry:
    """The destinations and sessions a detection asks for (the wrapper registers them with the daemon)."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.sessions: list = []

    def destination(self, url: str) -> str:
        self.urls.append(url)
        return name_for(url)

    def session(self, credentials) -> str:
        self.sessions.append(credentials)
        return "f" * 32


def _claude(shell=None, user=None, managed=None, config=None, permanent=False, registry=None):
    registry = registry or Registry()
    sources = {"shell": shell or {}, "user": user or {}, "managed": managed or {}}
    session = None if permanent else registry.session
    route = detect_claude(sources, config or Config(), TOKEN, registry.destination, session)
    return route, registry


def test_claude_code_with_anthropic_goes_through_latet():
    route, registry = _claude()
    assert route.kind == "anthropic" and route.env == {"ANTHROPIC_BASE_URL": f"{LATET}/anthropic"}
    assert registry.urls == []


def test_a_company_gateway_stays_and_latet_goes_in_front():
    route, registry = _claude(user={"ANTHROPIC_BASE_URL": "https://llm.acme.com"})
    assert route.kind == "gateway" and registry.urls == ["https://llm.acme.com"]
    assert route.env == {"ANTHROPIC_BASE_URL": f"{LATET}/llm-acme-com"} and route.source == "user settings"
    assert "llm.acme.com" in route.summary


def test_the_user_s_settings_beat_the_shell_as_in_claude_code():
    _, registry = _claude(
        shell={"ANTHROPIC_BASE_URL": "https://shell.acme.com"}, user={"ANTHROPIC_BASE_URL": "https://user.acme.com"}
    )
    assert registry.urls == ["https://user.acme.com"]


def test_bedrock_with_an_api_key_keeps_its_key():
    shell = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "eu-west-1", "AWS_BEARER_TOKEN_BEDROCK": "ABSK..."}
    route, _ = _claude(shell=shell)
    assert route.kind == "bedrock" and route.env == {"ANTHROPIC_BEDROCK_BASE_URL": f"{LATET}/bedrock/eu-west-1"}
    assert "ABSK" not in json.dumps(route.env) + route.summary


def test_bedrock_with_an_aws_profile_is_signed_by_latet():
    route, _ = _claude(shell={"CLAUDE_CODE_USE_BEDROCK": "true", "AWS_REGION": "eu-west-1", "AWS_PROFILE": "work"})
    key = profile_tag(TOKEN, "work")
    assert route.env == {
        "ANTHROPIC_BEDROCK_BASE_URL": f"{LATET}/bedrock/eu-west-1/profile/work/{key}",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
    }
    assert "signed by Latet" in route.summary and "work" in route.summary


def test_bedrock_without_a_profile_uses_the_default_one_and_region():
    route, _ = _claude(shell={"CLAUDE_CODE_USE_BEDROCK": "1"})
    assert (
        route.env["ANTHROPIC_BEDROCK_BASE_URL"]
        == f"{LATET}/bedrock/us-east-1/profile/default/{profile_tag(TOKEN, 'default')}"
    )


def test_bedrock_with_keys_only_in_the_shell_gets_a_session():
    shell = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_DEFAULT_REGION": "eu-central-1",
        "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "the-secret",
        "AWS_SESSION_TOKEN": "the-token",
    }
    route, registry = _claude(shell=shell)
    assert route.env["ANTHROPIC_BEDROCK_BASE_URL"] == f"{LATET}/bedrock/eu-central-1/session/{'f' * 32}"
    [credentials] = registry.sessions
    assert (credentials.access_key, credentials.secret_key, credentials.token) == (
        "AKIDEXAMPLE",
        "the-secret",
        "the-token",
    )
    assert credentials.expires is None  # the shell gave no end
    assert "the-secret" not in json.dumps(route.env) + route.summary
    permanent, _ = _claude(shell=shell, permanent=True)
    assert permanent.kind == "blocked" and "AWS profile" in permanent.problem


SHELL_KEYS = {
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "the-secret",
}
SIX_PM = datetime(2026, 10, 9, 18, 0, tzinfo=UTC).timestamp()


def _shell_credentials(config_dir, **variables):
    """The credentials `latet claude` hands the daemon for a shell with these variables, which reach the
    detection the way the wrapper sends them: through `claude_sources`."""
    registry = Registry()
    sources = claude_sources({**SHELL_KEYS, "PATH": "x", **variables}, config_dir, [])
    detect_claude(sources, Config(), TOKEN, registry.destination, registry.session)
    [credentials] = registry.sessions
    return credentials


@pytest.mark.parametrize("variable", ["AWS_SESSION_EXPIRATION", "AWS_CREDENTIAL_EXPIRATION"])
@pytest.mark.parametrize(
    "given",
    [
        "2026-10-09T18:00:00Z",
        "2026-10-09T18:00:00+00:00",
        "2026-10-09T20:00:00+02:00",
        "2026-10-09T18:00:00",  # no zone: AWS's tools print UTC
    ],
)
def test_credentials_found_in_the_shell_end_when_the_shell_says(tmp_path, variable, given):
    assert _shell_credentials(tmp_path, **{variable: given}).expires == SIX_PM


@pytest.mark.parametrize("given", ["", "soon", "2026-13-45T99:00:00Z", "1791000000"])
def test_an_expiry_that_is_not_a_time_is_ignored(tmp_path, given):
    assert _shell_credentials(tmp_path, AWS_SESSION_EXPIRATION=given).expires is None


def test_the_earlier_of_two_expirations_counts_and_a_bad_one_hides_nothing(tmp_path):
    both = {"AWS_SESSION_EXPIRATION": "2026-10-09T19:00:00Z", "AWS_CREDENTIAL_EXPIRATION": "2026-10-09T18:00:00Z"}
    assert _shell_credentials(tmp_path, **both).expires == SIX_PM
    mixed = {"AWS_SESSION_EXPIRATION": "never", "AWS_CREDENTIAL_EXPIRATION": "2026-10-09T18:00:00Z"}
    assert _shell_credentials(tmp_path, **mixed).expires == SIX_PM


def test_the_expiry_is_read_from_the_settings_that_hold_the_keys_too(tmp_path):
    env = {**SHELL_KEYS, "AWS_CREDENTIAL_EXPIRATION": "2026-10-09T18:00:00Z"}
    (tmp_path / "settings.json").write_text(json.dumps({"env": env}))
    registry = Registry()
    detect_claude(claude_sources({}, tmp_path, []), Config(), TOKEN, registry.destination, registry.session)
    [credentials] = registry.sessions
    assert credentials.expires == SIX_PM


def test_bedrock_behind_a_gateway_is_a_destination():
    shell = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": "https://litellm.acme.com/bedrock",
    }
    route, registry = _claude(shell=shell)
    assert registry.urls == ["https://litellm.acme.com/bedrock"]
    assert route.env == {"ANTHROPIC_BEDROCK_BASE_URL": f"{LATET}/litellm-acme-com"}


@pytest.mark.parametrize(
    ("address", "login"),
    [
        (VPC, {"AWS_PROFILE": "work"}),
        (VPC, {}),  # Claude Code signs with the default profile
        (VPC, {"AWS_ACCESS_KEY_ID": "AKIDEXAMPLE", "AWS_SECRET_ACCESS_KEY": "the-secret"}),  # keys in the shell only
        ("https://vpce-0abc1234-us-east-1a.bedrock-runtime.us-east-1.vpce.amazonaws.com/", {"AWS_PROFILE": "work"}),
    ],
    ids=["profile", "default profile", "keys in the shell", "one zone's name"],
)
def test_bedrock_through_a_vpc_endpoint_is_refused_with_its_reason_not_taken_for_a_gateway(address, login):
    """Claude Code would sign for Latet's address, and Amazon would answer 403 to each request."""
    route, registry = _claude(shell={"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BEDROCK_BASE_URL": address, **login})
    assert route.kind == "blocked" and route.env == {}
    assert "VPC endpoint" in route.problem and "vpce-0abc1234" in route.problem
    assert registry.urls == [] and registry.sessions == []  # nothing for the daemon to keep
    settings, _ = _claude(user={"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BEDROCK_BASE_URL": address})
    assert settings.kind == "blocked" and settings.source == "user settings"


@pytest.mark.parametrize("address", [VPC, PUBLIC, FIPS], ids=["VPC endpoint", "public address", "FIPS address"])
@pytest.mark.parametrize(
    "key",
    [{"AWS_BEARER_TOKEN_BEDROCK": "ABSK..."}, {"CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1"}],
    ids=["API key", "no signing"],
)
def test_bedrock_with_an_api_key_or_no_signing_keeps_its_address_as_a_destination(address, key):
    """No signature of Claude Code's to replace: any address works behind Latet, as it did."""
    route, registry = _claude(shell={"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BEDROCK_BASE_URL": address, **key})
    assert route.kind == "gateway" and registry.urls == [address]
    assert route.env == {"ANTHROPIC_BEDROCK_BASE_URL": f"{LATET}/{name_for(address)}"}


@pytest.mark.parametrize(
    "login",
    [{"AWS_PROFILE": "work"}, {"AWS_ACCESS_KEY_ID": "AKIDEXAMPLE", "AWS_SECRET_ACCESS_KEY": "the-secret"}],
    ids=["profile", "keys in the shell"],
)
@pytest.mark.parametrize("address", [PUBLIC, PUBLIC + "/", "https://BEDROCK-RUNTIME.eu-west-1.amazonaws.com:443"])
def test_bedrock_s_public_address_written_out_is_signed_by_latet_as_when_it_is_not(address, login):
    shell = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "eu-west-1", **login}
    unwritten, nothing = _claude(shell=shell)
    route, registry = _claude(shell={**shell, "ANTHROPIC_BEDROCK_BASE_URL": address})
    assert route == unwritten and route.kind == "bedrock" and "signed by Latet" in route.summary
    assert registry.urls == [] and len(registry.sessions) == len(nothing.sessions)


@pytest.mark.parametrize("regions", [{}, {"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-west-2"}])
def test_the_region_of_the_address_written_out_is_the_one_latet_signs_for(regions):
    """The address is where the requests go: its region wins over AWS_REGION, and the summary names it."""
    shell = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "work", "ANTHROPIC_BEDROCK_BASE_URL": PUBLIC, **regions}
    route, _ = _claude(shell=shell)
    assert (
        route.env["ANTHROPIC_BEDROCK_BASE_URL"]
        == f"{LATET}/bedrock/eu-west-1/profile/work/{profile_tag(TOKEN, 'work')}"
    )
    assert "eu-west-1" in route.summary


@pytest.mark.parametrize(
    ("address", "login"),
    [
        (FIPS, {"AWS_PROFILE": "work"}),
        (FIPS, {}),  # Claude Code signs with the default profile
        (FIPS + "/", {"AWS_ACCESS_KEY_ID": "AKIDEXAMPLE", "AWS_SECRET_ACCESS_KEY": "the-secret"}),  # in the shell only
        ("https://BEDROCK-RUNTIME-FIPS.us-gov-west-1.amazonaws.com:443", {"AWS_PROFILE": "work"}),
    ],
    ids=["profile", "default profile", "keys in the shell", "capitals and the port"],
)
def test_bedrock_s_fips_address_is_refused_not_sent_to_the_standard_one(address, login):
    """Latet reaches only the standard address, which is not a FIPS endpoint: a FIPS requirement is not dropped."""
    route, registry = _claude(shell={"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BEDROCK_BASE_URL": address, **login})
    assert route.kind == "blocked" and route.env == {} and route.note is None
    assert "FIPS" in route.problem and "standard" in route.problem and "us-gov-west-1" in route.problem
    assert registry.urls == [] and registry.sessions == []  # nothing for the daemon to keep


@pytest.mark.parametrize(
    "address",
    [
        "https://bedrock-runtime.eu-west-1.amazonaws.com.evil.example",  # Amazon's name inside another host
        "https://evil-bedrock-runtime.eu-west-1.amazonaws.com",
        "https://bedrock-runtime.eu-west-1.amazonaws.com/bedrock",  # a path: a gateway's route, not Amazon's address
        "https://bedrock-runtime.eu-west-1.amazonaws.com:8443",
        "https://bedrock-runtime.not-a-region.amazonaws.com",
        "https://bedrock-runtime-fips.us-gov-west-1.amazonaws.com/bedrock",  # forwarded as written, never to the standard
        "https://bedrock-runtime-fips.not-a-region.amazonaws.com",
        "https://bedrock.eu-west-1.amazonaws.com",  # Bedrock's control plane, not its runtime
        "https://x.bedrock-runtime.eu-west-1.vpce.amazonaws.com.evil.example",  # a VPC endpoint's name in another's
        "https://vpce.amazonaws.com",
        "https://notvpce.amazonaws.com",
    ],
)
def test_only_amazon_s_own_bedrock_addresses_are_taken_for_them(address):
    """Anything else stays a company's gateway, as it was."""
    shell = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "work", "ANTHROPIC_BEDROCK_BASE_URL": address}
    route, registry = _claude(shell=shell)
    assert route.kind == "gateway" and registry.urls == [address]
    assert route.env == {"ANTHROPIC_BEDROCK_BASE_URL": f"{LATET}/{name_for(address)}"}


def test_bedrock_s_public_address_over_http_is_not_signed_for():
    route, registry = _claude(
        shell={"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BEDROCK_BASE_URL": PUBLIC.replace("https", "http")}
    )
    assert route.kind == "blocked" and "https" in route.problem and registry.urls == []


def test_vertex_goes_through_latet_with_the_google_login():
    route, _ = _claude(user={"CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "global"})
    assert route.kind == "vertex" and route.env == {"ANTHROPIC_VERTEX_BASE_URL": f"{LATET}/vertex/v1"}
    gateway, registry = _claude(
        user={"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_BASE_URL": "https://gw.acme.com/vertex_ai/v1"}
    )
    assert registry.urls == ["https://gw.acme.com/vertex_ai/v1"] and gateway.kind == "gateway"


def test_foundry_is_a_destination_from_its_resource_or_address():
    route, registry = _claude(user={"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_RESOURCE": "acme"})
    assert registry.urls == ["https://acme.services.ai.azure.com/anthropic"]
    assert route.kind == "foundry" and route.env == {"ANTHROPIC_FOUNDRY_BASE_URL": f"{LATET}/foundry-acme"}
    nothing, _ = _claude(user={"CLAUDE_CODE_USE_FOUNDRY": "1"})
    assert nothing.kind == "blocked" and "ANTHROPIC_FOUNDRY_RESOURCE" in nothing.problem


def test_what_latet_cannot_stand_in_front_of_is_said():
    mantle, _ = _claude(shell={"CLAUDE_CODE_USE_MANTLE": "1"})
    assert mantle.kind == "blocked" and "Mantle" in mantle.problem
    forced, registry = _claude(managed={"ANTHROPIC_BASE_URL": "https://llm.acme.com"})
    assert forced.kind == "blocked" and "managed settings" in forced.problem and registry.urls == []
    fine, _ = _claude(
        managed={"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "eu-west-1"}, shell={"AWS_PROFILE": "work"}
    )
    assert fine.kind == "bedrock"
    pointed, _ = _claude(managed={"ANTHROPIC_BASE_URL": f"{LATET}/anthropic"})
    assert pointed.kind == "anthropic"


def test_an_address_on_this_computer_is_left_alone_unless_declared():
    local = {"ANTHROPIC_BASE_URL": "http://localhost:4000"}
    route, registry = _claude(user=local)
    assert route.kind == "local" and route.env == {} and registry.urls == []
    assert "latet config add upstreams" in route.note
    declared = Config(upstreams=[Destination(name="litellm", url="http://localhost:4000")])
    routed, registry = _claude(user=local, config=declared)
    assert registry.urls == ["http://localhost:4000"] and routed.kind == "gateway"


def test_an_agent_already_pointed_at_latet_stays_so():
    route, registry = _claude(user={"ANTHROPIC_BASE_URL": f"{LATET}/llm-acme-com"})
    assert route.env == {"ANTHROPIC_BASE_URL": f"{LATET}/llm-acme-com"} and registry.urls == []


def test_only_the_user_s_and_the_organization_s_settings_are_read(tmp_path):
    config_dir, managed = tmp_path / "claude", tmp_path / "managed-settings.json"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://user.acme.com"}}))
    managed.write_text(json.dumps({"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}))
    project = tmp_path / "repo" / ".claude"
    project.mkdir(parents=True)
    (project / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://evil.example"}}))
    sources = claude_sources({"AWS_REGION": "eu-west-1", "PATH": "x"}, config_dir, [managed])
    assert sources == {
        "shell": {"AWS_REGION": "eu-west-1"},
        "user": {"ANTHROPIC_BASE_URL": "https://user.acme.com"},
        "managed": {"CLAUDE_CODE_USE_BEDROCK": "1"},
    }


def _codex(tmp_path, text, profiles=None, config=None, chatgpt=False, profile=None, session=False):
    (tmp_path / "config.toml").write_text(text, "utf-8")
    for name, body in (profiles or {}).items():
        (tmp_path / f"{name}.config.toml").write_text(body, "utf-8")
    registry = Registry()
    texts = codex_texts(tmp_path)
    route = detect_codex(texts, config or Config(), chatgpt, registry.destination, profile, session=session)
    return route, registry


def test_codex_with_openai_goes_through_latet(tmp_path):
    route, _ = _codex(tmp_path, "")
    assert route.overrides == {"openai_base_url": f"{LATET}/openai/v1"}
    signed_in, _ = _codex(tmp_path, "", chatgpt=True)
    assert signed_in.overrides == {"openai_base_url": f"{LATET}/backend-api/codex"}


def test_codex_with_a_company_gateway_for_openai(tmp_path):
    route, registry = _codex(tmp_path, 'openai_base_url = "https://gw.acme.com/v1"\n')
    assert registry.urls == ["https://gw.acme.com/v1"]
    assert route.overrides == {"openai_base_url": f"{LATET}/gw-acme-com"}


def test_codex_with_azure_moves_only_that_provider_s_address(tmp_path):
    text = (
        'model_provider = "azure"\n[model_providers.azure]\nname = "Azure"\n'
        'base_url = "https://acme.openai.azure.com/openai/v1"\nenv_key = "AZURE_OPENAI_API_KEY"\nwire_api = "responses"\n'
    )
    route, registry = _codex(tmp_path, text)
    assert registry.urls == ["https://acme.openai.azure.com/openai/v1"]
    assert route.overrides["model_providers.azure.base_url"] == f"{LATET}/azure-acme"
    assert "acme.openai.azure.com" in route.summary


def test_codex_providers_on_this_computer_are_left_alone(tmp_path):
    text = '[model_providers.local]\nname = "Local"\nbase_url = "http://localhost:11434/v1"\n'
    lan = '[model_providers.lan]\nname = "LAN"\nbase_url = "http://192.168.1.5:8080/v1"\n'
    route, registry = _codex(tmp_path, text + lan)
    assert "model_providers.local.base_url" not in route.overrides
    assert route.overrides["model_providers.lan.base_url"] == f"{LATET}/192-168-1-5-8080"
    assert registry.urls == ["http://192.168.1.5:8080/v1"]
    assert any("localhost:11434" in note for note in route.notes)


def test_codex_profiles_providers_are_routed_too(tmp_path):
    work = '[model_providers.acme]\nname = "Acme"\nbase_url = "https://gw.acme.com/v1"\n'
    route, _ = _codex(tmp_path, "", profiles={"work": work})
    assert route.edits["work.config.toml"]["model_providers.acme.base_url"] == f"{LATET}/gw-acme-com"


def test_codex_on_an_address_on_this_computer_is_said_to_be_left_as_it_is(tmp_path):
    local = 'openai_base_url = "http://localhost:4000"\n'
    for session in (False, True):  # what `agents apply` records, what `latet codex` says
        route, _ = _codex(tmp_path, local, session=session)
        assert route.summary == "localhost:4000, on this computer: left as it is"
    ollama = 'model_provider = "ollama"\n[model_providers.ollama]\nname = "Ollama"\n'
    provider, _ = _codex(tmp_path, ollama + 'base_url = "http://localhost:11434/v1"\n')
    assert provider.summary == "provider ollama (localhost:11434), on this computer: left as it is"
    pointed, _ = _codex(tmp_path, f'openai_base_url = "{LATET}/openai/v1"\n')
    assert pointed.summary == "OpenAI: Latet goes in front of it"  # Latet's own address


def test_codex_providers_pointed_at_latet_stay_so(tmp_path):
    text = f'[model_providers.acme]\nname = "Acme"\nbase_url = "{LATET}/gw-acme-com"\n'
    route, registry = _codex(tmp_path, text)
    assert "model_providers.acme.base_url" not in route.overrides and registry.urls == []


@pytest.mark.parametrize("text", ["model_provider = \n", "[model_providers.x\n"])
def test_a_broken_codex_config_is_said(tmp_path, text):
    route, _ = _codex(tmp_path, text)
    assert route.problem and "config.toml" in route.problem


def test_the_provider_s_own_address_is_no_company_gateway(tmp_path):
    route, registry = _claude(shell={"ANTHROPIC_BASE_URL": "https://api.anthropic.com/"})
    assert route.kind == "anthropic" and route.env == {"ANTHROPIC_BASE_URL": f"{LATET}/anthropic"}
    codex, codex_registry = _codex(tmp_path, 'openai_base_url = "https://api.openai.com/v1"\n')
    assert codex.overrides == {"openai_base_url": f"{LATET}/openai/v1"}
    assert registry.urls == [] and codex_registry.urls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example\\@127.0.0.1:5283/anthropic",
        "http://evil.example\\[/anthropic",
        "http://llm.corp.acme.com",
        "https://me:hunter2@llm.acme.com",
    ],
)
def test_an_address_latet_cannot_route_is_said_and_nothing_is_added(url):
    route, registry = _claude(user={"ANTHROPIC_BASE_URL": url})
    assert route.kind == "blocked" and route.problem and registry.urls == []
    assert "hunter2" not in route.problem


def test_an_address_that_only_starts_like_this_computer_is_a_gateway():
    route, registry = _claude(user={"ANTHROPIC_BASE_URL": "https://127.attacker.example"})
    assert route.kind == "gateway" and registry.urls == ["https://127.attacker.example"]


def test_two_clouds_at_once_are_not_guessed():
    route, _ = _claude(user={"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1"})
    assert route.kind == "blocked" and "CLAUDE_CODE_USE_BEDROCK" in route.problem


def test_codex_with_an_address_latet_cannot_route_says_so():
    toml = 'model_provider = "acme"\n[model_providers.acme]\nname = "Acme"\nbase_url = "http://llm.corp.acme.com/v1"\n'
    registry = Registry()
    route = detect_codex({"config.toml": toml}, Config(), False, registry.destination)
    assert route.problem and "https" in route.problem and registry.urls == []


def test_a_session_overrides_only_what_codex_reads(tmp_path):
    """`-c` for a provider Codex does not load makes it refuse to start ("provider name must not be
    empty"): a session overrides the providers of config.toml and of the profile it names, no other."""
    work = '[model_providers.acme]\nname = "Acme"\nbase_url = "https://gw.acme.com/v1"\n'
    route, registry = _codex(tmp_path, "", profiles={"work": work}, session=True)
    assert "model_providers.acme.base_url" not in route.overrides and registry.urls == []
    chosen, _ = _codex(tmp_path, "", profiles={"work": work}, session=True, profile="work")
    assert chosen.overrides["model_providers.acme.base_url"] == f"{LATET}/gw-acme-com"


def test_a_profile_s_company_address_for_openai_is_routed(tmp_path):
    work = 'openai_base_url = "https://gw.acme.com/v1"\n'
    session, _ = _codex(tmp_path, "", profiles={"work": work}, session=True, profile="work")
    assert session.overrides == {"openai_base_url": f"{LATET}/gw-acme-com"}
    for_good, _ = _codex(tmp_path, "", profiles={"work": work})
    assert for_good.edits == {
        "config.toml": {"openai_base_url": f"{LATET}/openai/v1"},
        "work.config.toml": {"openai_base_url": f"{LATET}/gw-acme-com"},
    }


@pytest.mark.parametrize(
    ("text", "openai"),
    [
        ('openai_base_url = "https://gw.acme.com/v1"\n', f"{LATET}/gw-acme-com"),  # moved to Latet
        (f'openai_base_url = "{LATET}/openai/v1"\n', f"{LATET}/openai/v1"),  # Latet's already
        ('openai_base_url = "http://localhost:4000"\n', "http://localhost:4000"),  # on this computer: left alone
        ("", f"{LATET}/openai/v1"),  # nothing written: OpenAI's, through Latet
    ],
    ids=["moved", "latet", "local", "nothing"],
)
def test_where_codex_s_openai_provider_goes_in_a_session(tmp_path, text, openai):
    """What a Codex older than 0.115 gets in OPENAI_BASE_URL, the only address it reads."""
    route, _ = _codex(tmp_path, text, session=True)
    assert route.openai == openai


def test_the_destinations_a_text_points_at_are_read_by_name():
    texts = [
        '{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/llm-acme-com"}}',
        'base_url = "http://localhost:5283/azure-acme/openai/v1"',
        "base_url = 'http://[::1]:5283/gw-acme'",
        "HTTP://LocalHost:5283/shouted?x=1",
        "ANTHROPIC_BASE_URL=http://127.0.0.1:5283/bare",  # the end of the text ends the name
    ]
    assert destinations_pointed_at(texts, 5283) == {"llm-acme-com", "azure-acme", "gw-acme", "shouted", "bare"}


def test_a_name_is_not_taken_for_the_start_of_a_longer_one():
    text = '"http://127.0.0.1:5283/acme-2/v1" "http://127.0.0.1:5283/acme.old"'
    assert destinations_pointed_at([text], 5283) == {"acme-2", "acme.old"}  # neither is acme


def test_only_an_address_of_latet_names_a_destination():
    texts = [
        '"http://127.0.0.1:5284/other-port"',
        '"http://127.0.0.1:66470/longer-port"',
        '"http://example.com:5283/elsewhere"',
        '"http://my-localhost:5283/lookalike"',
        '"http://sub.localhost:5283/lookalike"',
        '"http://127.0.0.1:5283"',
        '"http://127.0.0.1:5283/"',
        "",
    ]
    assert destinations_pointed_at(texts, 5283) == set()
    assert destinations_pointed_at(texts, 5284) == {"other-port"}  # the daemon's own port
