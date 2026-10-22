"""Assemble detectors, vault, masker, engine and verifier from the user configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from latet.config import Config, IdentityConfig, Profile, data_dir
from latet.engine.detectors import ner
from latet.engine.detectors.base import Detector
from latet.engine.detectors.fields import FieldDetector
from latet.engine.detectors.gitleaks import GitleaksDetector
from latet.engine.detectors.latet import LATET_TYPES, LatetCredentialDetector
from latet.engine.detectors.misread import MisreadDetector
from latet.engine.detectors.pii import ALL_PII, PiiDetector, effective_pii
from latet.engine.detectors.recognizers import Recognizer, RecognizerDetector, Status, compile_all
from latet.engine.detectors.structural import AssignmentDetector, CredentialDetector, PrivateKeyDetector
from latet.engine.detectors.terms import IdentityDetector, LocalIdentity, Term, TermsDetector
from latet.engine.masker import AllowList, Masker
from latet.engine.pipeline import Engine
from latet.engine.vault import Vault
from latet.engine.verifier import Verifier

if TYPE_CHECKING:
    from latet.state.protocols import Conversations, Given


@dataclass(slots=True)
class Built:
    vault: Vault
    masker: Masker
    engine: Engine
    verifier: Verifier
    recognizers: list[Status] = field(default_factory=list)  # the user's own recognizers, as loaded


def reserved_types() -> set[str]:
    """Types Latet has already: a recognizer must pick another (its fakes and policies are its own)."""
    from latet.engine.detectors.gitleaks import rule_types

    structural = {"PASSWORD", "ASSIGNED_SECRET", "PRIVATE_KEY", "URL_PASSWORD", "JWT", "BEARER_TOKEN"}
    identity = {"USERNAME", "HOSTNAME", "PERSON", "DOMAIN", "STREET_ADDRESS", "CITY", "CUSTOM"}
    return set(ALL_PII) | structural | identity | rule_types() | LATET_TYPES


def recognizers_for(config: Config) -> tuple[list[Recognizer], list[Status]]:
    from latet.engine.corpus import benign_sample

    return compile_all(config.recognizers, reserved_types(), benign_sample() if config.recognizers else None)


def detectors_for(
    config: Config,
    identity: IdentityConfig | None,
    models: Path | None = None,
    recognizers: list[Recognizer] | None = None,
    *,
    bounded: bool = False,
) -> list[Detector]:
    """The detectors of ``config``. ``bounded``: the terms matched within an RE2 budget sized to them
    (``TermsDetector``), for a masker that is one of many in its process."""
    detectors: list[Detector] = [
        LatetCredentialDetector(),
        GitleaksDetector(disabled_rules=config.disabled_secret_rules),
        AssignmentDetector(),
        PrivateKeyDetector(),
        CredentialDetector(),
        # Secret column names always; your field rules, and strict data mode when on (max turns it on).
        FieldDetector(
            config.fields,
            strict=config.data_mode() == "strict",
            tools=config.data_tools,
            files=config.data_files,
            hosts=config.data_hosts,
        ),
    ]
    if config.profile != Profile.SECRETS:
        pii = effective_pii(config.pii, config.id_regions, every_region=config.profile == Profile.MAX)
        private_ips = (
            config.include_private_ips if config.include_private_ips is not None else config.profile == Profile.MAX
        )
        detectors.append(PiiDetector(pii, config.phone_regions, private_ips))
        detectors.append(MisreadDetector(pii))  # in the text of images: values read with a letter wrong
    if config.terms:
        detectors.append(
            TermsDetector(
                (Term(t.value, t.type.upper(), t.case_sensitive, t.whole_word) for t in config.terms), bounded=bounded
            )
        )
    if recognizers:
        detectors.append(RecognizerDetector(recognizers))
    if identity is not None and identity.enabled and config.profile != Profile.SECRETS:
        detectors.append(
            IdentityDetector(
                LocalIdentity(
                    usernames=tuple(identity.usernames),
                    username=identity.username,
                    hostname=identity.hostname,
                    full_name=identity.full_name,
                    emails=tuple(identity.emails),
                    hostnames=tuple(identity.hostnames),
                )
            )
        )
    if config.profile != Profile.SECRETS and config.ner == "auto":
        names = ner.detector_for(models or data_dir(), config.profile.value)
        if names is not None:
            detectors.append(names)
    return detectors


def build_masker(
    config: Config,
    vault: Vault,
    identity: IdentityConfig | None = None,
    models: Path | None = None,
    *,
    bounded: bool = False,
) -> tuple[Masker, list[Status]]:
    """The masker of a configuration (the global one, or a project's), and its recognizers as loaded. ``bounded``: its
    terms matched within an RE2 budget sized to them (a project's masker in an identity's space on a server)."""
    recognizers, statuses = recognizers_for(config)
    # A recognizer's own policy is its type's default; an entry in `policies` wins.
    policies = {r.etype: r.spec.policy for r in recognizers if r.spec.policy is not None} | dict(config.policies)
    masker = Masker(
        vault,
        detectors_for(config, identity, models, recognizers, bounded=bounded),
        policies=policies,
        allow=AllowList(values=set(config.allow_values), patterns=list(config.allow_patterns)),
        generators={r.etype: r.generate for r in recognizers},
    )
    return masker, statuses


def build(
    config: Config,
    vault: Vault,
    identity: IdentityConfig | None = None,
    models: Path | None = None,
    conversations: Conversations | None = None,
    given: Given | None = None,
) -> Built:
    masker, statuses = build_masker(config, vault, identity, models)
    engine = Engine(vault, masker, conversations=conversations)
    return Built(vault, masker, engine, Verifier(masker, given=given), statuses)
