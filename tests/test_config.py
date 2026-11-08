from pathlib import Path

from alert_colors.config import App

CONFIG = Path(__file__).resolve().parents[1] / "config.toml"


def test_shipped_config_builds_everything(monkeypatch):
    monkeypatch.setenv("UKRAINEALARM_API_KEY", "test-key")
    app = App.load(CONFIG)
    assert app.source().name == "siren_pp_ua"
    scenes = app.scenes()
    app.policy()
    assert {"clear", "unknown", "alert"} <= scenes.keys()
    assert [b.name for b in app.backends()] == ["aura_sdk", "razer_chroma"]
    assert [b.name for b in app.backends(dry_run=True)] == ["console"]
    app.settings()


def test_lighting_entry_without_module_is_skipped(monkeypatch, caplog):
    from alert_colors import config as config_module

    app = App.load(CONFIG)
    monkeypatch.setattr(config_module.BACKENDS, "names", lambda: ["console", "razer_chroma"])
    assert [e["type"] for e in app.lighting_entries()] == ["razer_chroma"]
    assert "aura_sdk" in caplog.text and "not in this build" in caplog.text


def test_poller_only_has_no_outputs(monkeypatch):
    from alert_colors import config as config_module

    app = App.load(CONFIG)
    monkeypatch.setattr(config_module.BACKENDS, "names", lambda: ["console"])
    assert app.lighting_entries() == []


def test_every_optional_module_declares_its_needs():
    from alert_colors.lighting import AVAILABLE_MODULES, MODULE_NEEDS, OPTIONAL_MODULES

    assert set(AVAILABLE_MODULES) == set(OPTIONAL_MODULES)  # the source tree has all modules
    assert all({"admin", "armoury_crate"} <= set(needs) for needs in MODULE_NEEDS.values())
