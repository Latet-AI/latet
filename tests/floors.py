"""A floor of the tests' own, and an edition that has it: the free code's side of locked rules is tested with them,
without ee/ (the free suite runs with ee/ removed)."""

from pathlib import Path

from latet.config import Config, Profile, TermConfig
from latet.edition import Community


class CompanyFloor:
    """The company sets the profile at max, counts none of a person's own exceptions, and keeps Latet on in every
    project."""

    locks_terms = carves_terms = decides_exceptions = False

    def below(self, config: Config) -> list[str]:
        out = [] if config.profile == Profile.MAX else [f"profile: from max to {config.profile.value}"]
        if config.allow_values:
            out.append(f"values never to mask: {len(config.allow_values)} added")
        out += [f"project {Path(e.path).name}: Latet turned off" for e in config.projects if not e.enabled]
        return out

    def raise_to(self, config: Config) -> Config:
        projects = [e.model_copy(update={"enabled": True}) for e in config.projects]
        return config.model_copy(update={"profile": Profile.MAX, "allow_values": [], "projects": projects})


class TermsFloor:
    """The company locks these terms (as Latet Enterprise's floor does): they join the settings first, an entry of
    yours equal to one left out, and go to the build as locked (``Config.locked_terms``). It refuses nothing."""

    locks_terms, carves_terms, decides_exceptions = True, False, False

    def __init__(self, *terms: TermConfig) -> None:
        self.terms = list(terms)

    def below(self, config: Config) -> list[str]:
        return []

    def raise_to(self, config: Config) -> Config:
        out = config.model_copy(deep=True)
        out.terms = [*(t.model_copy() for t in self.terms), *(t for t in config.terms if t not in self.terms)]
        out.lock_terms(t.model_copy() for t in self.terms)
        return out


class FloorEdition(Community):
    """The Community Edition with a floor: what the Enterprise Edition gives the runtime."""

    def __init__(self, floor=None):
        super().__init__()
        self.locked = floor
        self.events = []
        self.watchers = []

    def floor(self, identity):
        return self.locked

    def record(self, event):
        self.events.append(event)

    def watch(self, changed):
        self.watchers.append(changed)
        return lambda: self.watchers.remove(changed)
