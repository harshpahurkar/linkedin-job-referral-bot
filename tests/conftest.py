import pytest


@pytest.fixture(autouse=True)
def freeze_file(tmp_path, monkeypatch):
    """Tests flag warnings on purpose. Keep them from freezing the real bot."""
    path = tmp_path / "FROZEN.txt"
    monkeypatch.setattr("antidetect.FREEZE_FILE", path)
    return path
