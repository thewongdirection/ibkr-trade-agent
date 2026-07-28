"""Tests for optional out-of-band delivery (reporting/notify.py)."""

from __future__ import annotations

import io
import json
from urllib.error import URLError

import pytest

from reporting import notify


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def test_configured_requires_both_vars(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.telegram_configured() is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert notify.telegram_configured() is False  # chat id still missing
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    assert notify.telegram_configured() is True


def test_send_noops_when_unconfigured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.send_telegram("hi") is False


def test_send_success(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["data"] = req.data
        return _FakeResp(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert notify.send_telegram("hello", token="TOK", chat_id="42") is True
    assert "botTOK/sendMessage" in captured["url"]
    assert b"chat_id=42" in captured["data"]
    assert b"hello" in captured["data"]


def test_send_returns_false_on_network_error(monkeypatch):
    def boom(req, timeout=0):
        raise URLError("no network")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.send_telegram("x", token="T", chat_id="1") is False


def test_send_returns_false_when_telegram_rejects(monkeypatch):
    def fake_urlopen(req, timeout=0):
        return _FakeResp(json.dumps({"ok": False, "description": "bad"}).encode())

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert notify.send_telegram("x", token="T", chat_id="1") is False


def test_long_message_truncated(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["data"] = req.data
        return _FakeResp(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    notify.send_telegram("a" * 5000, token="T", chat_id="1")
    # urlencoded body includes the text field; ensure it didn't send 5000 raw chars.
    assert len(captured["data"]) < 5000 + 200


def test_deliver_brief_includes_telegram_when_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(notify, "send_telegram", lambda text: True)
    assert notify.deliver_brief("brief") == {"telegram": True}


def test_deliver_brief_empty_when_unconfigured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.deliver_brief("brief") == {}
