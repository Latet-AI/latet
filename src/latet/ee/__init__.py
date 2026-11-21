"""Latet Enterprise Edition. The code in this folder, and in dashboard/src/ee/, is licensed under the Latet Enterprise
License (ee/LICENSE at the top of the repository), not under the Latet Sustainable Use License of the rest of Latet:
LICENSE says which portions each covers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from latet.edition import Edition, EditionContext


def make_edition(context: EditionContext) -> Edition:
    """The Enterprise Edition of a Latet server (``context.server``; a computer's context is refused, ValueError): its
    hooks act as far as the license goes."""
    from latet.ee.edition import EnterpriseEdition

    return EnterpriseEdition(context)
