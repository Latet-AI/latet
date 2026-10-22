"""National ID and tax numbers of 15 countries, and VAT numbers of 30 European countries.

``REGIONS`` maps each country (ISO 3166-1 alpha-2) to the numbers it issues; ``id_regions`` in
config.yaml chooses the countries. VAT numbers carry their country prefix and are always looked for.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from latet.engine.detectors.ids import vat
from latet.engine.detectors.ids.matcher import IdMatcher
from latet.engine.detectors.ids.regions import REGIONS
from latet.engine.detectors.ids.spec import IdSpec, Rng

SPECS: dict[str, IdSpec] = {spec.etype: spec for specs in REGIONS.values() for spec in specs}
ID_TYPES = tuple(SPECS)


def types_for(regions: Iterable[str]) -> list[str]:
    """The ID types of these countries, in registry order."""
    wanted = {r.upper() for r in regions}
    return [spec.etype for country, specs in REGIONS.items() if country in wanted for spec in specs]


def generators() -> dict[str, Callable[[str, Rng], str]]:
    out: dict[str, Callable[[str, Rng], str]] = {etype: spec.generate for etype, spec in SPECS.items()}
    out["VAT_NUMBER"] = vat.generate
    return out


__all__ = ["ID_TYPES", "REGIONS", "SPECS", "IdMatcher", "IdSpec", "generators", "types_for", "vat"]
