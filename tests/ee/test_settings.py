"""The Enterprise settings, a server's alone: a section this version does not know is refused, never ignored; a section
is set by its name, as the administration API sets one; a refusal says what is wrong by its place, never what it was
given."""

import pytest

from latet.ee.settings import (
    EnterpriseSettings,
    EnterpriseSettingsError,
    UnknownSection,
    section_value,
    settings_from,
    with_section,
)


def test_a_mistake_is_said_by_its_place_never_by_what_was_given(demo_section):
    with pytest.raises(EnterpriseSettingsError, match=r"demo\.\*: Extra inputs") as refused:
        with_section(demo_section(), "demo", {"emily-carter": 3})  # a key no setting has, below a section: anything
    assert "emily" not in str(refused.value)
    assert refused.value.__context__ is None and refused.value.__cause__ is None
    with pytest.raises(EnterpriseSettingsError, match=r"demo\.level: ") as refused:
        with_section(demo_section(), "demo", {"level": "Emily Carter"})
    assert "Emily" not in str(refused.value)


def test_a_section_latet_does_not_know_is_refused_or_skipped():
    # A name no part uses (E3 adds `sso`, E4 `reports` and `siem`…): this test holds as the parts add theirs.
    with pytest.raises(UnknownSection, match="no Enterprise setting named 'teleport'"):
        with_section(EnterpriseSettings(), "teleport", {})
    with pytest.raises(EnterpriseSettingsError, match="teleport"):
        settings_from({"teleport": {}})
    assert settings_from({"teleport": {}}, skip_unknown=True) == EnterpriseSettings()  # a row a newer Latet left


def test_a_section_is_set_by_its_name_and_goes_back_to_its_defaults(demo_section):
    settings = demo_section()
    assert settings.demo.level == 1
    settings = with_section(settings, "demo", {"level": 3})
    assert settings.demo.level == 3 and section_value(settings, "demo") == {"level": 3, "note": None}
    with pytest.raises(UnknownSection, match="'teleport'"):
        section_value(settings, "teleport")
    with pytest.raises(EnterpriseSettingsError, match="level"):
        with_section(settings, "demo", {"level": "high"})
    assert settings.demo.level == 3  # what was refused changed nothing
    settings = with_section(settings, "demo", None)
    assert settings.demo.level == 1 and settings.model_dump(mode="json", exclude_defaults=True) == {}


def test_a_value_that_is_not_a_section_is_refused_saying_what_is_wrong(demo_section):
    with pytest.raises(EnterpriseSettingsError, match="demo: Input should be a valid dictionary") as refused:
        with_section(demo_section(), "demo", "Emily Carter")  # what an API body may hold
    assert "Emily" not in str(refused.value)
