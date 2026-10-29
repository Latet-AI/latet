"""Characterize identity heuristics through real masking and restoration, without claiming a conversation is a vault."""

from contextlib import ExitStack

import pytest

from latet.config import Config, IdentityConfig
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.mediate import Mediator
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from ..conversation_identity import FIRST, SECOND, answer, fake, prepare, release, request


@pytest.fixture
def mediator():
    built = build(Config(ner="off", identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    yield mediator
    mediator.close()


@pytest.mark.parametrize("truncated", [False, True], ids=["identical-opening", "identical-8192-prefix"])
def test_fallback_reuse_restores_both_conversations_values(mediator, truncated):
    # Characterization: a fresh logical chat cannot be distinguished by this heuristic. This catches a change
    # that silently loses stateless continuation, and makes the currently shared restoration scope explicit.
    opening = "Continue the report." if not truncated else "ordinary words " * 600
    a = prepare(mediator, request(FIRST, opening=opening + ("first" if truncated else "")))
    b = prepare(mediator, request(SECOND, opening=opening + ("second" if truncated else "")))
    try:
        fa, fb = fake(mediator, FIRST, a), fake(mediator, SECOND, b)
        assert a.session is not None and b.session is not None and a.session.key == b.session.key
        # The provider guesses A's surrogate in logical chat B, whose actual input carried only SECOND.
        assert answer(mediator, b, f"{fa} | {fb}") == f"{FIRST} | {SECOND}"
        assert answer(mediator, a, f"{fa} | {fb}") == f"{FIRST} | {SECOND}"
        continued = prepare(mediator, request(FIRST, opening=opening + ("first" if truncated else "")))
        try:
            assert fake(mediator, FIRST, continued) == fa
            assert answer(mediator, continued, fa) == FIRST
        finally:
            release(mediator, continued)
    finally:
        release(mediator, a, b)


@pytest.mark.parametrize(
    "header", ["x-claude-code-session-id", "session-id", "session_id", "thread-id", "x-session-id"]
)
def test_explicit_ids_keep_unseen_originals_out_of_http_answers(mediator, header):
    a = prepare(mediator, request(FIRST), {header: "first"})
    b = prepare(mediator, request(SECOND), {header: "second"})
    try:
        fa, fb = fake(mediator, FIRST, a), fake(mediator, SECOND, b)
        assert answer(mediator, b, f"{fa} | {fb}") == f"{fa} | {SECOND}"
        # A client pasting A's fake into B means those literal bytes, not A's original.
        reflected = prepare(mediator, request(f"{SECOND} | {fa}"), {header: "second"})
        try:
            reflected_fake = fake(mediator, fa, reflected)
            assert answer(mediator, reflected, reflected_fake) == fa
            assert FIRST not in answer(mediator, reflected, f"{fa} | {reflected_fake}")
        finally:
            release(mediator, reflected)
    finally:
        release(mediator, a, b)


def test_response_names_continue_their_session_but_headers_take_precedence(mediator):
    a = prepare(mediator, request(FIRST, responses=True, prompt_cache_key="first"), responses=True)
    b = prepare(mediator, request(SECOND, responses=True, prompt_cache_key="second"), responses=True)
    try:
        fa, fb = fake(mediator, FIRST, a), fake(mediator, SECOND, b)
        assert a.session is not None
        # The actual response-created stream callback records the provider's response ID.
        stream = mediator.response_transform(a, "text/event-stream", None)
        assert stream is not None
        stream.feed(b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_first"}}\n\n')
        stream.end()
        continued = prepare(
            mediator,
            request("Continue.", responses=True, previous_response_id="resp_first", prompt_cache_key="second"),
            responses=True,
        )
        overridden = prepare(
            mediator,
            request("Continue.", responses=True, previous_response_id="resp_first"),
            {"session-id": "third"},
            responses=True,
        )
        try:
            assert answer(mediator, continued, f"{fa} | {fb}") == f"{FIRST} | {fb}"
            assert answer(mediator, overridden, fa) == fa
        finally:
            release(mediator, continued, overridden)
    finally:
        release(mediator, a, b)


@pytest.mark.parametrize("separate_data", [False, True], ids=["one-local-vault", "separate-data-vaults"])
def test_local_projects_use_the_configured_vault_boundary(tmp_path, separate_data):
    folders = [tmp_path / "project-a", tmp_path / "project-b"]
    for folder in folders:
        folder.mkdir()
        # The runner's private temp lives inside this checkout. Give each fixture its own repository boundary,
        # as test_projects._repo does, rather than inheriting the enclosing checkout's one project.
        (folder / ".git").mkdir()
    config = Config(ner="off", identity=IdentityConfig(enabled=False))
    with ExitStack() as cleanup:
        first = make_runtime(config, data=tmp_path / "data-a", root_key=ROOT_KEY, config_file=tmp_path / "a.yaml")
        cleanup.callback(first.close)
        second = first
        if separate_data:
            second = make_runtime(config, data=tmp_path / "data-b", root_key=b"b" * 32, config_file=tmp_path / "b.yaml")
            cleanup.callback(second.close)
        a = prepare(first.mediator, request(FIRST), {"x-latet-project": str(folders[0])})
        cleanup.callback(release, first.mediator, a)
        assert a.session is not None
        project_a = a.session.project_id
        assert project_a is not None and first.projects is not None
        resolved_a = first.projects.get(project_a)
        assert resolved_a is not None and resolved_a.root == str(folders[0])
        b = prepare(second.mediator, request(SECOND), {"x-latet-project": str(folders[1])})
        cleanup.callback(release, second.mediator, b)
        assert b.session is not None and b.session.project_id != project_a
        assert b.session.project_id is not None and second.projects is not None
        resolved_b = second.projects.get(b.session.project_id)
        assert resolved_b is not None and resolved_b.root == str(folders[1])
        fa, fb = fake(first.mediator, FIRST, a), fake(second.mediator, SECOND, b)
        expected = fa if separate_data else FIRST
        assert answer(second.mediator, b, f"{fa} | {fb}") == f"{expected} | {SECOND}"
        assert (first.built.vault is second.built.vault) is (not separate_data)
        # The header selects rules, including a switch on the same session; it creates no second vault.
        switched = prepare(second.mediator, request(SECOND), {"x-latet-project": str(folders[0])})
        cleanup.callback(release, second.mediator, switched)
        assert answer(second.mediator, switched, f"{fa} | {fb}") == f"{expected} | {SECOND}"
