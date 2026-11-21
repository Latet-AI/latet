"""An edition that syncs terms, as Latet Enterprise does on a server: one LiveTerms for every runtime made with it."""

from latet.edition import Community
from latet.engine.detectors.terms import LiveTerms


class SyncedEdition(Community):
    def __init__(self) -> None:
        super().__init__()
        self.live = LiveTerms()
        self.unavailable: Exception | None = None  # raised by synced_terms when set: the terms cannot be read

    def synced_terms(self, identity):
        if self.unavailable is not None:
            raise self.unavailable
        return self.live
