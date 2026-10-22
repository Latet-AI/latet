"""scripts/gen_notices.py: the same THIRD_PARTY_NOTICES.md whichever operating system generates it."""

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "gen_notices", Path(__file__).resolve().parents[2] / "scripts" / "gen_notices.py"
)
assert _SPEC is not None and _SPEC.loader is not None
gen = importlib.util.module_from_spec(_SPEC)
sys.modules["gen_notices"] = gen  # its dataclasses look their module up there
_SPEC.loader.exec_module(gen)

PILLOW = """Pillow is the friendly PIL fork.

IN NO EVENT SHALL SECRET LABS AB OR THE AUTHOR BE LIABLE FOR ANY SPECIAL DAMAGES.
"""


def test_pillow_appendix_is_cut_the_same_on_every_platform():
    # The wheels list the libraries they bundle after Pillow's own license, each OS its own way.
    windows = PILLOW + "\n===== brotli-1.2.0 =====\n\nCopyright (c) 2009 by the Brotli Authors.\n"
    linux = PILLOW + "\n----\n\nAOM\n\nCopyright (c) 2016, Alliance for Open Media.\n"
    cut = gen._without_wheel_appendix(windows, "pillow")
    assert cut == gen._without_wheel_appendix(linux, "pillow")
    assert cut.startswith(PILLOW.rstrip()) and "Brotli" not in cut and "AOM" not in cut
    assert "license file inside the installed package" in cut


def test_a_license_without_an_appendix_is_left_alone():
    assert gen._without_wheel_appendix(PILLOW, "pillow") == PILLOW
    ruled = "Some license.\n\n----\n\nSECTION\n\nMore of the same license.\n"
    assert gen._without_wheel_appendix(ruled, "somepackage") == ruled  # "----" is Pillow's marker only


def test_render_carries_psycopg_lgpl_and_gpl_companion_texts(monkeypatch):
    # Runtime-image notices must be reproducible without the host's Python wheel metadata.
    monkeypatch.setattr(gen, "python_sections", lambda: [])
    monkeypatch.setattr(gen, "npm_sections", lambda: [])
    rendered = gen.render()

    assert "psycopg" in rendered
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in rendered
    assert "6. Revised Versions of the GNU Lesser General Public License." in rendered
    assert "GNU GENERAL PUBLIC LICENSE" in rendered
    assert "How to Apply These Terms to Your New Programs" in rendered


def test_render_carries_tzdata_apache_text_without_platform_metadata(monkeypatch):
    # tzdata's Windows-only lock edge must not discard license text needed in the image notice.
    monkeypatch.setattr(gen, "python_sections", lambda: [])
    monkeypatch.setattr(gen, "npm_sections", lambda: [])
    rendered = gen.render()

    assert "tzdata" in rendered
    assert "Copyright (c) 2020, Paul Ganssle (Google)" in rendered
    assert "Copyright (c) 2026, Stan Ulbrych" in rendered
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in rendered
    assert "9. Accepting Warranty or Additional Liability." in rendered
