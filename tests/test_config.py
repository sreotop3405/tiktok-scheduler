"""Tests for environment-driven configuration parsing."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def settings_module(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_IDS", raising=False)
    import tiktok_scheduler.config as config

    importlib.reload(config)
    return config


def test_owner_ids_single_int(monkeypatch, settings_module):
    monkeypatch.setenv("TELEGRAM_OWNER_IDS", "123456789")
    importlib.reload(settings_module)
    s = settings_module.Settings(_env_file=None)
    assert s.owner_ids == [123456789]


def test_owner_ids_csv(monkeypatch, settings_module):
    monkeypatch.setenv("TELEGRAM_OWNER_IDS", "111,222 ,  333")
    importlib.reload(settings_module)
    s = settings_module.Settings(_env_file=None)
    assert s.owner_ids == [111, 222, 333]


def test_owner_ids_empty(settings_module):
    s = settings_module.Settings(_env_file=None)
    assert s.owner_ids == []


def test_owner_ids_garbage_ignored(monkeypatch, settings_module):
    monkeypatch.setenv("TELEGRAM_OWNER_IDS", "111,abc,222")
    importlib.reload(settings_module)
    s = settings_module.Settings(_env_file=None)
    assert s.owner_ids == [111, 222]
