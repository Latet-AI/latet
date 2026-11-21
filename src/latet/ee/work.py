"""Work of a server's routes outside the dashboard and the administration (single sign-on, SCIM) in E0b's work threads
(``latet.server.machines.in_work``), its refusals raised again here: ``in_work`` answers StoresUnavailable for
whatever a work raises, which would say a refusal as a server that cannot answer."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from latet.server.machines import in_work


async def working[T](work: Callable[..., T], *args: Any, refusals: tuple[type[Exception], ...]) -> T:
    """``work(*args)`` in E0b's work threads; an exception of ``refusals`` that it raises is raised again here, as it
    was; any other is E0b's StoresUnavailable (said by its type)."""

    def caught() -> tuple[Any, Exception | None]:
        try:
            return work(*args), None
        except refusals as exc:
            return None, exc

    result, refused = await in_work(caught)
    if refused is not None:
        raise refused
    return cast(T, result)
