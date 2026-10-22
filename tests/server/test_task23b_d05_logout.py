"""D05: a real cookie signs out through the dashboard pool and ends on both instances."""

import asyncio
import threading

import httpx
import pytest

from latet.api import rest
from latet.server.app import create_server_app

from .support import URL, listening, until

pytestmark = pytest.mark.server


class _NoDashboardDefaultExecutor:
    """Forbid the dashboard's to_thread without changing the instances' background jobs."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def to_thread(self, *args, **kwargs):
        raise AssertionError("the server dashboard used the default executor")


async def test_logout_ends_a_real_cookie_on_both_instances_in_the_dashboard_pool(instances, monkeypatch):
    first, second = instances
    emily, _ = first.identities.add_person("Emily Carter")
    sid = first.sign_ins.open(emily.id)
    headers = {"cookie": f"latet_session={sid}"}
    loop_thread, ended = threading.current_thread(), []

    async with (
        listening(first, second),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_server_app(first)),
            base_url=URL,
            headers=headers,
            trust_env=False,
        ) as one,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_server_app(second)),
            base_url=URL,
            headers=headers,
            trust_env=False,
        ) as two,
    ):
        # Both real gates accept the same cookie, build their actual apps, and retain the session.
        assert (await one.get(f"{rest.API}/me")).status_code == 200
        assert (await two.get(f"{rest.API}/me")).status_code == 200
        # Actual Valkey GETs refresh both holds after the potentially slow first space builds.
        assert first.sign_ins.subject_of(sid) == second.sign_ins.subject_of(sid) == emily.id
        assert first.sign_ins.held(sid) == second.sign_ins.held(sid) == emily.id
        assert first.spaces._spaces[emily.id].runtime.backends.sign_ins is first.sign_ins
        end = first.sign_ins.end

        def observed_end(sid=None, subject=None):
            thread = threading.current_thread()
            ended.append((thread is loop_thread, thread.name, sid, subject))
            return end(sid=sid, subject=subject)  # Actual Valkey deletion and signed bus publication.

        with monkeypatch.context() as patch:
            patch.setattr(first.sign_ins, "end", observed_end)
            patch.setattr(rest, "asyncio", _NoDashboardDefaultExecutor())
            out = await one.post(f"{rest.API}/logout", headers={"x-latet-ui": "1"})

        assert out.status_code == 200 and out.json() == {"ok": True}
        assert len(ended) == 1
        on_loop, name, ended_sid, ended_subject = ended[0]
        assert ended_sid == sid and ended_subject is None
        cookie = out.headers["set-cookie"].lower()
        assert "latet_session=" in cookie and "max-age=0" in cookie and "secure" in cookie

        # subject_of reads the real store, while held observes the second instance's bus invalidation.
        assert first.sign_ins.subject_of(sid) is None
        assert second.sign_ins.subject_of(sid) is None
        await until(lambda: second.sign_ins.held(sid) is None)
        assert first.sign_ins.held(sid) is None
        # Both clients still explicitly present the copied cookie: it no longer opens either real gate.
        assert (await one.get(f"{rest.API}/me")).status_code == 401
        assert (await two.get(f"{rest.API}/me")).status_code == 401
        assert not on_loop and name.startswith("latet-dashboard")
