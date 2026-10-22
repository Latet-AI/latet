"""One module per country: the national ID and tax numbers it issues."""

from __future__ import annotations

from latet.engine.detectors.ids.regions import be, ca, ch, de, es, fi, fr, gb, it, lv, nl, pl, pt, se, us
from latet.engine.detectors.ids.spec import IdSpec

REGIONS: dict[str, tuple[IdSpec, ...]] = {
    module.SPECS[0].country: module.SPECS for module in (it, es, fr, de, nl, be, pt, pl, ch, se, fi, gb, us, ca, lv)
}
