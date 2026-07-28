"""Tests for the Telegram command poller (reporting/telegram_bot.py).

Focus: only the authorized chat is answered, offsets advance so messages aren't re-processed,
and replies are routed. Network is faked via injected ``fetch``/``reply``.
"""

from __future__ import annotations

from reporting import telegram_bot as tb


class DummyRouter:
    def __init__(self):
        self.seen = []

    def handle(self, text):
        self.seen.append(text)
        return f"echo:{text}"


def _update(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def test_poll_answers_only_authorized_chat():
    router = DummyRouter()
    replies = []

    def fetch(token, offset=0, timeout=0):
        return [_update(10, "42", "/account"), _update(11, "999", "/positions")]

    def reply(text, token=None, chat_id=None):
        replies.append((chat_id, text))
        return True

    next_offset, handled = tb.poll_once(
        router, token="T", authorized_chat_id="42", offset=0, fetch=fetch, reply=reply)

    assert handled == 1
    assert replies == [("42", "echo:/account")]
    assert router.seen == ["/account"]        # stranger's command never routed
    assert next_offset == 12                   # advanced past the highest update id


def test_poll_ignores_non_text_updates():
    router = DummyRouter()

    def fetch(token, offset=0, timeout=0):
        return [{"update_id": 5, "message": {"chat": {"id": "42"}}}]  # no text

    def reply(text, token=None, chat_id=None):
        raise AssertionError("should not reply")

    next_offset, handled = tb.poll_once(
        router, token="T", authorized_chat_id="42", offset=0, fetch=fetch, reply=reply)
    assert handled == 0 and next_offset == 6


def test_poll_empty_keeps_offset():
    next_offset, handled = tb.poll_once(
        DummyRouter(), token="T", authorized_chat_id="42", offset=7,
        fetch=lambda *a, **k: [], reply=lambda *a, **k: True)
    assert (next_offset, handled) == (7, 0)


def test_offset_persistence_roundtrip(tmp_path):
    from agent.settings import RiskLimits, Settings

    settings = Settings(
        mode="paper", config_mode="paper", base_currency="SGD", account_verify={},
        strategy_style="blend", asset_classes=("stock",), new_ideas_count=20,
        risk=RiskLimits(5000, 15, 35, 3, 5, 8, 3, 22), management={}, universe={}, schedule={},
        recommend_skill_path="x", grader_skill_path="y",
        journal_db_path=str(tmp_path / "j.db"))
    assert tb.load_offset(settings) == 0
    tb.save_offset(settings, 123)
    assert tb.load_offset(settings) == 123
