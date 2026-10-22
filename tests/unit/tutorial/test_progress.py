"""The chapters you finished: chapter ids only, in tutorial.json in your own data folder, written whole or not at
all, so the menu shows them from one run of `latet tutorial` to the next."""

import json

import pytest

from latet.tutorial.progress import Progress


def test_nothing_is_done_at_first_and_a_finished_chapter_is_kept(tmp_path):
    progress = Progress(tmp_path / "user" / "tutorial.json")
    assert progress.done() == []
    progress.mark("terms")
    progress.mark("basics")
    progress.mark("basics")  # twice is once
    assert progress.done() == ["basics", "terms"]  # in the menu's order
    assert json.loads((tmp_path / "user" / "tutorial.json").read_text("utf-8")) == {"done": ["basics", "terms"]}
    assert [p.name for p in (tmp_path / "user").iterdir()] == ["tutorial.json"]  # no temporary file left
    assert Progress(tmp_path / "user" / "tutorial.json").done() == ["basics", "terms"]  # the next run sees them


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]", '{"done": "basics"}', '{"done": [3, null]}'])
def test_a_file_that_cannot_be_read_counts_as_nothing_done(tmp_path, text):
    path = tmp_path / "tutorial.json"
    path.write_text(text, "utf-8")
    progress = Progress(path)
    assert progress.done() == []
    progress.mark("basics")
    assert progress.done() == ["basics"]


def test_a_chapter_this_version_does_not_know_is_kept_but_not_listed(tmp_path):
    """Written by a later version with more chapters: kept for it, not shown by this one."""
    path = tmp_path / "tutorial.json"
    path.write_text(json.dumps({"done": ["a-later-chapter", "basics"]}), "utf-8")
    progress = Progress(path)
    assert progress.done() == ["basics"]
    progress.mark("live")
    assert json.loads(path.read_text("utf-8")) == {"done": ["basics", "live", "a-later-chapter"]}
