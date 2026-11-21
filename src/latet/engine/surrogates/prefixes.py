"""The public prefix of a secret: what of it its fake keeps (``generators.gen_secret``), and what ``latet leak``'s hint
shows of it (``leak.scan.hint``), one and the same.

A vendor writes the same characters before every key of a kind (`ghp_`, `sk-ant-api03-`, `AKIA`): they say whose key it
is and nothing of it, and a fake that keeps them reads as that vendor's key. Nothing else of a secret is public: the
random part of a token holds `-`, `_` and `.` as any other character (base64url), and the words of a value assigned to a
name (`my-company-prod-`) are the value's own. So a value's public prefix is, by its type:

* one of Latet's tokens (``TOKEN_TYPES``): `latet_<kind>_`;
* a type of a Gitleaks rule: the characters the rule's pattern writes before anything it lets vary in its secret (`ghp_`
  for `ghp_[0-9a-zA-Z]{36}`, one of `AKIA`, `ASIA`... for AWS), the longest of them the value begins with. A rule that
  finds a value by the name written before it (`adafruit ... = <32 characters>`) writes none: its values keep nothing;
* any other type, nothing: one any value can have (``ANY_VALUE``: Gitleaks' generic rule, a JWT, a private key), a
  password, a value assigned to a name, a bearer token, a field's, a recognizer's of yours.

A prefix is kept only before eight letters or decimal digits, which a fake draws (``secret_shape``): before signs alone
(`abc-` and ten dots) the fake would be the value itself, and no fake could be drawn (57de9a6d).

What each rule writes first is a table here (``RULE_PREFIXES``), of the ruleset Latet ships (``RULESET``), not read from
the rules as a fake is drawn: tests/unit/test_fake_prefix.py reads it again from each rule's pattern, the secret as
Gitleaks takes it (``gitleaks._secret_span``), with Python's own parser of patterns, and fails where a new ruleset or a
new Python gives other prefixes, so that what a fake keeps changes only with this table, read by eye.
"""

from __future__ import annotations

from latet.engine.detectors.latet import TOKEN_TYPES

# Gitleaks' generic rule, a JWT (as written and in base64) and a private key: what any value can be, or a block whose
# head says nothing of it. Their values keep nothing.
ANY_VALUE = frozenset({"GENERIC_API_KEY", "JWT", "JWT_BASE64", "PRIVATE_KEY"})
# What a fake draws after its prefix, at least: letters and decimal digits (57de9a6d's guard, kept from before).
DRAWN_AFTER = 8
_LATET_KINDS = {etype: kind for kind, etype in TOKEN_TYPES.items()}


def public_prefix(etype: str, value: str) -> str:
    """The characters of ``value`` its vendor writes before every value of ``etype`` (``ghp_``, ``sk-ant-api03-``,
    ``AKIA``, ``latet_adm_``), or "": what its fake keeps, and ``latet leak``'s hint shows, of it."""
    kind = _LATET_KINDS.get(etype)
    if kind is not None:
        start = f"latet_{kind}_"
        prefix = start if value.startswith(start) else ""
    elif etype in ANY_VALUE or "PASSWORD" in etype:
        prefix = ""
    else:
        prefix = next((value[: len(start)] for start in RULE_PREFIXES.get(etype, ()) if _begins(value, start)), "")
    drawn = sum(c.isalpha() or c.isdecimal() for c in value[len(prefix) :])
    return prefix if drawn >= DRAWN_AFTER else ""


def _begins(value: str, start: str) -> bool:
    """Whether ``value`` begins with ``start``, whatever their case (a rule may read either): in ASCII, as ``start``
    is, so that no other character that lowers to one of it (the Kelvin sign) is kept for it."""
    head = value[: len(start)]
    return len(head) == len(start) and head.isascii() and head.lower() == start.lower()


# The Gitleaks ruleset whose patterns ``RULE_PREFIXES`` was read from: ``gitleaks.RULESET_VERSION``, which a test holds
# it to.
RULESET = "v8.30.1"
# What the rules of each type write before anything they let vary in the secret they find, the longest first: a type of
# no rule's, or of rules that write nothing first, is not here; nor a type any value can have (``ANY_VALUE``), nor a
# password's, whose values keep nothing.
RULE_PREFIXES: dict[str, tuple[str, ...]] = {
    "ADOBE_CLIENT_SECRET": ("p8e-",),
    "AGE_SECRET_KEY": ("AGE-SECRET-KEY-1",),
    "AIRTABLE_PERSONNAL_ACCESS_TOKEN": ("pat",),
    "ALIBABA_ACCESS_KEY_ID": ("LTAI",),
    "ANTHROPIC_ADMIN_API_KEY": ("sk-ant-admin01-",),
    "ANTHROPIC_API_KEY": ("sk-ant-api03-",),
    "ARTIFACTORY_API_KEY": ("AKCp",),
    "ARTIFACTORY_REFERENCE_TOKEN": ("cmVmd",),
    "ATLASSIAN_API_TOKEN": ("ATATT3",),
    "AUTHRESS_SERVICE_CLIENT_ACCESS_KEY": ("authress_", "scauth_", "ext_", "sc_"),
    "AWS_ACCESS_TOKEN": ("ABIA", "ACCA", "AKIA", "ASIA", "A3T"),
    "AWS_AMAZON_BEDROCK_API_KEY_LONG_LIVED": ("ABSK",),
    "AWS_AMAZON_BEDROCK_API_KEY_SHORT_LIVED": ("bedrock-api-key-YmVkcm9jay5hbWF6b25hd3MuY29t",),
    "BEAMER_API_TOKEN": ("b_",),
    "CLICKHOUSE_CLOUD_API_SECRET_KEY": ("4b1d",),
    "CLOJARS_API_TOKEN": ("CLOJARS_",),
    "CLOUDFLARE_ORIGIN_CA_KEY": ("v1.0-",),
    "DATABRICKS_API_TOKEN": ("dapi",),
    "DEFINED_NETWORKING_API_TOKEN": ("dnkey-",),
    "DIGITALOCEAN_ACCESS_TOKEN": ("doo_v1_",),
    "DIGITALOCEAN_PAT": ("dop_v1_",),
    "DIGITALOCEAN_REFRESH_TOKEN": ("dor_v1_",),
    "DOPPLER_API_TOKEN": ("dp.pt.",),
    "DROPBOX_SHORT_LIVED_API_TOKEN": ("sl.",),
    "DUFFEL_API_TOKEN": ("duffel_live_", "duffel_test_"),
    "DYNATRACE_API_TOKEN": ("dt0c01.",),
    "EASYPOST_API_TOKEN": ("EZAK",),
    "EASYPOST_TEST_API_TOKEN": ("EZTK",),
    "FACEBOOK_PAGE_ACCESS_TOKEN": ("EAAC", "EAAM"),
    "FLUTTERWAVE_ENCRYPTION_KEY": ("FLWSECK_TEST-",),
    "FLUTTERWAVE_PUBLIC_KEY": ("FLWPUBK_TEST-",),
    "FLUTTERWAVE_SECRET_KEY": ("FLWSECK_TEST-",),
    "FLYIO_ACCESS_TOKEN": ("fm1a_", "fm1r_", "fm2_", "fo1_"),
    "FRAMEIO_API_TOKEN": ("fio-u-",),
    "FREEMIUS_SECRET_KEY": ("sk_",),
    "GCP_API_KEY": ("AIza",),
    "GITHUB_APP_TOKEN": ("ghs_", "ghu_"),
    "GITHUB_FINE_GRAINED_PAT": ("github_pat_",),
    "GITHUB_OAUTH": ("gho_",),
    "GITHUB_PAT": ("ghp_",),
    "GITHUB_REFRESH_TOKEN": ("ghr_",),
    "GITLAB_CICD_JOB_TOKEN": ("glcbt-",),
    "GITLAB_DEPLOY_TOKEN": ("gldt-",),
    "GITLAB_FEATURE_FLAG_CLIENT_TOKEN": ("glffct-",),
    "GITLAB_FEED_TOKEN": ("glft-",),
    "GITLAB_INCOMING_MAIL_TOKEN": ("glimt-",),
    "GITLAB_KUBERNETES_AGENT_TOKEN": ("glagent-",),
    "GITLAB_OAUTH_APP_SECRET": ("gloas-",),
    "GITLAB_PAT": ("glpat-",),
    "GITLAB_PAT_ROUTABLE": ("glpat-",),
    "GITLAB_PTT": ("glptt-",),
    "GITLAB_RRT": ("GR1348941",),
    "GITLAB_RUNNER_AUTHENTICATION_TOKEN": ("glrt-",),
    "GITLAB_RUNNER_AUTHENTICATION_TOKEN_ROUTABLE": ("glrt-t",),
    "GITLAB_SCIM_TOKEN": ("glsoat-",),
    "GITLAB_SESSION_COOKIE": ("_gitlab_session=",),
    "GOCARDLESS_API_TOKEN": ("live_",),
    "GRAFANA_API_KEY": ("eyJrIjoi",),
    "GRAFANA_CLOUD_API_TOKEN": ("glc_",),
    "GRAFANA_SERVICE_ACCOUNT_TOKEN": ("glsa_",),
    "HARNESS_API_KEY": ("pat.", "sat."),
    "HEROKU_API_KEY_V2": ("HRKU-AA",),
    "HUGGINGFACE_ACCESS_TOKEN": ("hf_",),
    "HUGGINGFACE_ORGANIZATION_API_TOKEN": ("api_org_",),
    "INFRACOST_API_TOKEN": ("ico-",),
    "INTRA42_CLIENT_SECRET": ("s-s4t2af-", "s-s4t2ud-"),
    "LINEAR_API_KEY": ("lin_api_",),
    "LOB_API_KEY": ("live_", "test_"),
    "LOB_PUB_API_KEY": ("live_pub_", "test_pub_"),
    "MAILGUN_PRIVATE_API_TOKEN": ("key-",),
    "MAILGUN_PUB_KEY": ("pubkey-",),
    "MAPBOX_API_TOKEN": ("pk.",),
    "NEW_RELIC_BROWSER_API_TOKEN": ("NRJS-",),
    "NEW_RELIC_INSERT_KEY": ("NRII-",),
    "NEW_RELIC_USER_API_KEY": ("NRAK-",),
    "NOTION_API_TOKEN": ("ntn_",),
    "NPM_ACCESS_TOKEN": ("npm_",),
    "OCTOPUS_DEPLOY_API_KEY": ("API-",),
    "OKTA_ACCESS_TOKEN": ("00",),
    "OPENAI_API_KEY": ("sk-svcacct-", "sk-admin-", "sk-proj-", "sk-"),
    "OPENSHIFT_USER_TOKEN": ("sha256",),
    "PERPLEXITY_API_KEY": ("pplx-",),
    "PLAID_API_TOKEN": ("access-development-", "access-production-", "access-sandbox-"),
    "PLANETSCALE_API_TOKEN": ("pscale_tkn_",),
    "PLANETSCALE_OAUTH_TOKEN": ("pscale_oauth_",),
    "POSTMAN_API_TOKEN": ("PMAK-",),
    "PREFECT_API_TOKEN": ("pnu_",),
    "PULUMI_API_TOKEN": ("pul-",),
    "PYPI_UPLOAD_TOKEN": ("pypi-AgEIcHlwaS5vcmc",),
    "README_API_TOKEN": ("rdme_",),
    "RUBYGEMS_API_TOKEN": ("rubygems_",),
    "SCALINGO_API_TOKEN": ("tk-us-",),
    "SENDGRID_API_TOKEN": ("SG.",),
    "SENDINBLUE_API_TOKEN": ("xkeysib-",),
    "SENTRY_ORG_TOKEN": ("sntrys_eyJpYXQiO",),
    "SENTRY_USER_TOKEN": ("sntryu_",),
    "SETTLEMINT_APPLICATION_ACCESS_TOKEN": ("sm_aat_",),
    "SETTLEMINT_PERSONAL_ACCESS_TOKEN": ("sm_pat_",),
    "SETTLEMINT_SERVICE_ACCESS_TOKEN": ("sm_sat_",),
    "SHIPPO_API_TOKEN": ("shippo_live_", "shippo_test_"),
    "SHOPIFY_ACCESS_TOKEN": ("shpat_",),
    "SHOPIFY_CUSTOM_ACCESS_TOKEN": ("shpca_",),
    "SHOPIFY_PRIVATE_APP_ACCESS_TOKEN": ("shppa_",),
    "SHOPIFY_SHARED_SECRET": ("shpss_",),
    "SLACK_APP_TOKEN": ("xapp-",),
    "SLACK_BOT_TOKEN": ("xoxb-",),
    "SLACK_CONFIG_ACCESS_TOKEN": ("xoxe",),
    "SLACK_CONFIG_REFRESH_TOKEN": ("xoxe-",),
    "SLACK_LEGACY_BOT_TOKEN": ("xoxb-",),
    "SLACK_LEGACY_TOKEN": ("xoxo-", "xoxs-"),
    "SLACK_LEGACY_WORKSPACE_TOKEN": ("xoxa-", "xoxr-"),
    "SLACK_USER_TOKEN": ("xoxe", "xoxp"),
    "SOURCEGRAPH_ACCESS_TOKEN": ("sgp_local", "sgp_"),
    "SQUARE_ACCESS_TOKEN": ("sq0atp-", "EAAA"),
    "STRIPE_ACCESS_TOKEN": ("rk_live_", "rk_prod_", "rk_test_", "sk_live_", "sk_prod_", "sk_test_"),
    "SUMOLOGIC_ACCESS_ID": ("su",),
    "TWILIO_API_KEY": ("SK",),
    "TYPEFORM_API_TOKEN": ("tfp_",),
    "VAULT_BATCH_TOKEN": ("hvb.",),
    "VAULT_SERVICE_TOKEN": ("hvs.", "s."),
    "YANDEX_ACCESS_TOKEN": ("t1.",),
    "YANDEX_API_KEY": ("AQVN",),
    "YANDEX_AWS_ACCESS_TOKEN": ("YC",),
}
