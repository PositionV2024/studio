from pathlib import Path

from studio.settings_store import SettingsStore


def test_settings_store_round_trip(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    result = store.set_element_patch("hero.title", {"text": "Hello", "font_size": 42})
    assert result["hero.title"]["text"] == "Hello"
    assert store.load_draft()["hero.title"]["font_size"] == 42

    store.publish()
    assert store.load_published()["hero.title"]["text"] == "Hello"

    store.undo()
    assert store.load_draft() == {}
    store.redo()
    assert store.load_draft()["hero.title"]["text"] == "Hello"
