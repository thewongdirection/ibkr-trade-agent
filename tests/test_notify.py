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


# --- one-way report delivery: summary text + dashboard attachment -----------

def test_send_document_posts_multipart(monkeypatch, tmp_path):
    f = tmp_path / "dash.html"
    f.write_text("<h1>hi</h1>")
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["ctype"] = req.headers.get("Content-type", "")
        captured["body"] = req.data
        return _FakeResp(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert notify.send_telegram_document(str(f), caption="cap", token="T", chat_id="9") is True
    assert "sendDocument" in captured["url"]
    assert "multipart/form-data" in captured["ctype"]
    assert b'name="chat_id"' in captured["body"] and b"9" in captured["body"]
    assert b'filename="dash.html"' in captured["body"]
    assert b"<h1>hi</h1>" in captured["body"]


def test_send_document_missing_file_is_graceful(monkeypatch):
    assert notify.send_telegram_document("/nope/missing.html", token="T", chat_id="1") is False


def test_send_document_noops_when_unconfigured(monkeypatch, tmp_path):
    f = tmp_path / "d.html"; f.write_text("x")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.send_telegram_document(str(f)) is False


def test_caption_truncated_to_telegram_limit(monkeypatch, tmp_path):
    f = tmp_path / "d.html"; f.write_text("x")
    captured = {}
    def fake_urlopen(req, timeout=0):
        captured["body"] = req.data
        return _FakeResp(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    notify.send_telegram_document(str(f), caption="c" * 3000, token="T", chat_id="1")
    assert captured["body"].count(b"c") <= 1024 + 50


def test_deliver_report_sends_summary_and_attachment(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    dash = tmp_path / "dash.html"; dash.write_text("<p>d</p>")
    sent = {}

    def fake_text(text):
        sent["text"] = text
        return True

    def fake_doc(path, **kw):
        sent["doc"] = (path, kw.get("caption"))
        return True

    monkeypatch.setattr(notify, "send_telegram", fake_text)
    monkeypatch.setattr(notify, "html_to_pdf", lambda p, *a, **k: None)  # no browser -> HTML
    monkeypatch.setattr(notify, "send_telegram_document", fake_doc)

    res = notify.deliver_report("line1\nEquity 250,000 SGD", dashboard_path=str(dash))
    assert res == {"telegram": True, "telegram_document": True}
    assert sent["text"].startswith("line1")
    assert sent["doc"][0] == str(dash)              # fell back to HTML
    assert "Equity 250,000 SGD" in sent["doc"][1]   # caption uses the informative line


def test_deliver_report_prefers_pdf_when_available(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    dash = tmp_path / "dash.html"; dash.write_text("<p>d</p>")
    pdf = tmp_path / "dash.pdf"; pdf.write_bytes(b"%PDF-x")
    monkeypatch.setattr(notify, "send_telegram", lambda text: True)
    monkeypatch.setattr(notify, "html_to_pdf", lambda p, *a, **k: str(pdf))
    seen = {}

    def fake_doc(path, **kw):
        seen["path"] = path
        return True

    monkeypatch.setattr(notify, "send_telegram_document", fake_doc)
    notify.deliver_report("brief", dashboard_path=str(dash))
    assert seen["path"] == str(pdf)


def test_deliver_report_without_dashboard_is_text_only(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(notify, "send_telegram", lambda text: True)
    assert notify.deliver_report("brief") == {"telegram": True}


def test_pdf_render_returns_none_without_browser(monkeypatch, tmp_path):
    f = tmp_path / "x.html"; f.write_text("<p>x</p>")
    monkeypatch.setattr(notify, "_find_chromium", lambda: None)
    assert notify.html_to_pdf(str(f)) is None
