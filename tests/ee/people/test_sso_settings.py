"""The sso section of the Enterprise settings refuses what cannot work (ee/settings.py)."""

import pytest
from pydantic import ValidationError

from latet.ee.oidc import secure_address
from latet.ee.people import group_key, group_name
from latet.ee.settings import EnterpriseSettings, EnterpriseSettingsError, SsoSettings, settings_from

BASE = {"issuer": "https://login.acme.com/realms/acme", "client_id": "latet", "accounts": "open"}
GROUP = {**BASE, "accounts": "group", "access_group": "latet-users"}
GOOGLE = {"issuer": "https://accounts.google.com", "client_id": "latet", "accounts": "open"}


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("issuer", "http://login.acme.com/realms/acme", "https"),
        ("issuer", "https://login.acme.com/realms/acme?x=1", "no query"),
        ("client_secret", "Q7v9-KxW2~mP4.zR8", "never goes in the settings"),
        ("scopes", ["profile", "email"], "openid"),
        ("groups", {"payments-leads": " "}, "1 to 80"),
        ("groups", {"payments-leads": "Pay\x1bments"}, "control or bidirectional"),
        ("groups", {"payments-leads": "Pay\N{RIGHT-TO-LEFT OVERRIDE}ments"}, "control or bidirectional"),
        ("groups", {"payments-leads": "g_abcdefghij"}, "shaped like a group's id"),
        ("groups", {"Latet-Admins": "Admins", "/latet-admins": "Others"}, "written twice"),
        ("groups", {" ": "Admins"}, "by its name or its ID"),
        ("groups", {"latet\x1b-admins": "Admins"}, "control or bidirectional"),
        ("accounts", "everyone", "scim"),
        ("access_group", "latet\N{RIGHT-TO-LEFT OVERRIDE}users", "control or bidirectional"),
        ("company_changes", "anyone", "any-session"),
        ("allowed_domains", ["acme.com"], "Google"),
    ],
)
def test_sso_settings_refuse_what_cannot_work(field, value, why):
    with pytest.raises(ValidationError, match=why):
        SsoSettings.model_validate({**BASE, field: value})


def test_who_may_get_an_account_is_written_out():
    with pytest.raises(ValidationError, match="accounts"):
        SsoSettings.model_validate({"issuer": BASE["issuer"], "client_id": "latet"})
    with pytest.raises(ValidationError, match="access_group"):
        SsoSettings.model_validate({**BASE, "accounts": "group"})
    with pytest.raises(ValidationError, match="access_group"):
        SsoSettings.model_validate({**BASE, "access_group": "latet-users"})  # open: everyone the provider signs in
    assert SsoSettings.model_validate(GROUP).access_group == "latet-users"
    assert SsoSettings.model_validate({**BASE, "accounts": "scim"}).accounts == "scim"


def test_google_needs_the_domains_its_hd_claim_is_checked_against():
    with pytest.raises(ValidationError, match="allowed_domains"):
        SsoSettings.model_validate(GOOGLE)
    sso = SsoSettings.model_validate({**GOOGLE, "allowed_domains": [" Acme.com "]})
    assert sso.allowed_domains == ["acme.com"]
    with pytest.raises(ValidationError, match="domain"):
        SsoSettings.model_validate({**GOOGLE, "allowed_domains": ["acme com"]})


def test_keycloak_on_this_computer_may_be_http_and_the_defaults_name_the_secret():
    sso = SsoSettings(issuer="http://localhost:8080/realms/acme", client_id="latet", accounts="open")
    assert sso.client_secret == "sso-client-secret"
    assert sso.scopes == ["openid", "profile", "email"]
    assert (sso.user_name_claim, sso.groups_claim, sso.access_group, sso.groups) == (
        "preferred_username",
        "groups",
        None,
        {},
    )
    assert (sso.company_changes, sso.allowed_domains) == ("sso", [])


def test_the_enterprise_settings_have_an_sso_section():
    settings = EnterpriseSettings.model_validate({"sso": {**BASE, "groups": {"latet-admins": " Latet admins "}}})
    assert settings.sso is not None and settings.sso.groups == {"latet-admins": "Latet admins"}
    assert EnterpriseSettings().sso is None


def test_a_refusal_quotes_nothing_it_was_given():
    with pytest.raises(EnterpriseSettingsError) as caught:
        settings_from({"sso": {**BASE, "groups": {"Secret-Group-7": "A", "/secret-group-7": "B"}}})
    assert "ecret" not in str(caught.value)


@pytest.mark.parametrize(
    "name",
    ["Latet admins", " Payments leads ", "Pay\x1bments", "Pay\N{RIGHT-TO-LEFT OVERRIDE}ments", "", "x" * 81,
     "g_abcdefghij", "Ünïcode Gruppe", "tab\there"],
)  # fmt: skip
def test_the_mapping_keeps_a_group_name_as_people_does(name):
    """F16: SsoSettings._groups and group_name are twins: one refuses what the other refuses, keeps what it keeps."""
    try:
        kept = group_name(name)
    except ValueError:
        kept = None
    try:
        mapped = SsoSettings.model_validate({**BASE, "groups": {"latet-admins": name}}).groups["latet-admins"]
    except ValidationError:
        mapped = None
    assert mapped == kept


@pytest.mark.parametrize("claimed", ["latet-admins", "/Latet-Admins", " Latet-Admins "])
def test_the_mapping_compares_groups_of_the_token_as_people_does(claimed):
    sso = SsoSettings.model_validate({**BASE, "groups": {claimed: "Admins"}})
    [key] = sso.groups
    assert group_key(key) == group_key("latet-admins")


@pytest.mark.parametrize(
    "issuer",
    ["https://login.acme.com/realms/acme", "http://localhost:8080/realms/acme", "http://127.0.0.1:8080/r",
     "http://[::1]:8080/r", "http://login.acme.com/r", "http://10.0.0.1/r", "http://127.1/r", "ftp://login.acme.com/r"],
)  # fmt: skip
def test_the_settings_take_an_issuer_as_the_sign_in_reaches_one(issuer):
    """F16: SsoSettings._issuer and secure_address agree."""
    try:
        SsoSettings.model_validate({**BASE, "issuer": issuer})
        taken = True
    except ValidationError:
        taken = False
    assert taken is secure_address(issuer)
