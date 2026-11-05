"""G1: bridge observations stay in their identity's PostgreSQL history across workers, even when two plugins
report the same Claude Code session. A call bearing the other identity is rejected before anything is recorded."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from ..unit.test_request_observations import ENGINE, SECRET
from ..unit.test_request_outcomes import EMAIL
from .support import URL, listening

pytestmark = pytest.mark.server


async def test_instances_keep_same_session_observations_in_their_own_identitys_history(instances):
    a, b = instances
    identities = [a.identities.add_person(name)[0] for name in ("First observer", "Second observer")]
    async with listening(a, b):
        for instance in instances:
            for identity in identities:
                space = await instance.spaces.app(identity)

                async def scoped(
                    scope: Any, receive: Any, send: Any, space: Any = space, who: str = identity.id
                ) -> None:
                    await space({**scope, "latet.identity": who}, receive, send)

                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=scoped), base_url=URL) as client:
                    masked = await client.post(
                        f"{ENGINE}/mask", json={"session": "shared", "items": [{"text": f"write to {EMAIL}"}]}
                    )
                    assert masked.status_code == 200
                    fake = masked.json()["items"][0]["text"].removeprefix("write to ")
                    assert fake and fake != EMAIL
                    calls = [
                        ("route-check", {"session": "shared", "env": {}, "sources": {}, "entrypoint": "cli"}),
                        ("outbound", {"session": "shared", "tool": "web_fetch", "values": [fake]}),
                        ("verify", {"session": "shared", "slots": [{"text": f"{identity.id} {SECRET}"}]}),
                    ]
                    for endpoint, body in calls:
                        answer = await client.post(f"{ENGINE}/{endpoint}", json=body)
                        assert answer.status_code == 200
                        if endpoint == "verify":
                            assert answer.json()["blocking"]
        # Present the first identity directly to the second space on each worker, as in P1-5's unit cell.
        for instance in instances:
            space = await instance.spaces.app(identities[1])

            async def spoofed(scope: Any, receive: Any, send: Any, space: Any = space) -> None:
                await space({**scope, "latet.identity": identities[0].id}, receive, send)

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=spoofed), base_url=URL) as client:
                for endpoint, body in [
                    ("route-check", {"session": "shared", "env": {}, "sources": {}, "entrypoint": "cli"}),
                    ("outbound", {"session": "shared", "tool": "web_fetch", "values": ["irrelevant"]}),
                    ("verify", {"session": "shared", "slots": [{"text": SECRET}]}),
                ]:
                    assert (await client.post(f"{ENGINE}/{endpoint}", json=body)).status_code == 401
        for instance in instances:
            for identity in identities:
                runtime = await instance.spaces.runtime(identity)
                assert runtime.recorder is not None and runtime.exchanges is not None
                assert await asyncio.to_thread(runtime.recorder.drain)
        # Either worker sees both batches of this identity's history and none of the other's.
        for instance in instances:
            for identity in identities:
                runtime = await instance.spaces.runtime(identity)
                history = runtime.exchanges
                assert history is not None
                group = history.history_group("cc:shared")
                summaries, _ = await asyncio.to_thread(history.list, session=group)
                assert len(summaries) == 6
                checks = ["/plugin/route-check", "/plugin/tool-call", "/plugin/final-check"] * 2
                paths = [summary["path"] for summary in summaries]
                assert sorted(paths) == sorted(checks)  # each check by its fixed path, never a value of the request
                private_paths = []
                for summary in summaries:
                    assert summary["session"] == group and summary["transport"] == "plugin"
                    assert summary["coverage"] == "unknown" and summary["verified"] is False
                    stopped = "blocked" in summary["flags"]
                    assert summary["block_reason"] == ("refused" if stopped else None)  # a category, never words
                    assert summary["model"] is None
                    detail = await asyncio.to_thread(history.detail, summary["id"])
                    assert detail is not None
                    assert detail["sensitive"] is not None
                    private_path = detail["sensitive"]["path"]
                    private_paths.append(private_path)
                    if private_path == "/plugin/final-check":
                        assert [slot["masked"] for slot in detail["request"]["slots"]] == [f"{identity.id} {SECRET}"]
                        assert summary["block_reason"] == "refused" and SECRET not in json.dumps(summary)
                    else:
                        assert all(SECRET not in slot["masked"] for slot in detail["request"]["slots"])
                assert sorted(private_paths) == sorted(
                    ["/plugin/route-check", "/plugin/tool-call", "/plugin/final-check"] * 2
                )
                other = next(person for person in identities if person.id != identity.id)
                other_runtime = await instance.spaces.runtime(other)
                assert other_runtime.exchanges is not None
                for summary in summaries:
                    assert await asyncio.to_thread(other_runtime.exchanges.detail, summary["id"]) is None
