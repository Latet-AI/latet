"""Your text recognition models, as the tutorial finds them: in your own data folder, each time it looks (they may
be installed while it runs), and none without the `ocr` extra."""

from latet.tutorial.sandbox import user_reader
from latet.vision import models
from latet.vision.ocr import OnnxOcr


def test_your_models_are_looked_for_in_your_data_folder_each_time(tmp_path, monkeypatch):
    there = {"installed": False}
    monkeypatch.setattr(models, "runtime_available", lambda: True)
    monkeypatch.setattr(models, "installed", lambda data: there["installed"] and data == tmp_path)
    find = user_reader(tmp_path)
    assert find() is None
    there["installed"] = True  # `latet ocr install`, while the tutorial runs
    reader = find()
    assert isinstance(reader, OnnxOcr) and reader.folders == models.folders(tmp_path)


def test_without_the_ocr_extra_there_is_no_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "runtime_available", lambda: False)
    monkeypatch.setattr(models, "installed", lambda data: True)
    assert user_reader(tmp_path)() is None
