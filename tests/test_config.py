"""Tests for Boxer configuration loading."""
from __future__ import annotations

from pathlib import Path

import pytest

from boxer import config
from boxer.config import ConfigLoadError


@pytest.fixture(autouse=True)
def reset_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BOXER_CONFIG_PATH", raising=False)
    monkeypatch.delenv("BOXER_IMAGES_PATH", raising=False)
    config.reload()
    yield
    config.reload()


def test_config_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / "boxer.yaml"
    cfg_path.write_text("state_dir: /tmp/boxer-test\nadmin_group: test-admins\n")
    monkeypatch.setenv("BOXER_CONFIG_PATH", str(cfg_path))

    cfg = config.get_config()

    assert cfg.state_dir == Path("/tmp/boxer-test")
    assert cfg.admin_group == "test-admins"


def test_missing_config_env_override_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    monkeypatch.setenv("BOXER_CONFIG_PATH", str(missing))

    with pytest.raises(ConfigLoadError, match="BOXER_CONFIG_PATH points to missing"):
        config.get_config()


def test_unreadable_config_raises_helpful_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / "boxer.yaml"
    cfg_path.write_text("state_dir: /tmp/boxer-test\n")
    cfg_path.chmod(0)
    monkeypatch.setenv("BOXER_CONFIG_PATH", str(cfg_path))

    try:
        with pytest.raises(ConfigLoadError, match="Cannot read Boxer config file"):
            config.get_config()
    finally:
        cfg_path.chmod(0o644)
