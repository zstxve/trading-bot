import urllib.request

from tradingbot.alerts import Notifier
from tradingbot.config import AlertsConfig


def test_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    n = Notifier(AlertsConfig(telegram_enabled=True))
    assert not n.enabled
    assert n.send("hello") is False


def test_disabled_by_config_even_with_credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    n = Notifier(AlertsConfig(telegram_enabled=False))
    assert not n.enabled


def test_send_posts_to_telegram(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    captured = {}

    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode()
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    n = Notifier(AlertsConfig(telegram_enabled=True))
    assert n.enabled
    assert n.send("kill switch engaged") is True
    assert "botTOKEN/sendMessage" in captured["url"]
    assert "kill switch engaged" in captured["body"]
    assert '"chat_id": "42"' in captured["body"]


def test_send_never_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    n = Notifier(AlertsConfig(telegram_enabled=True))
    assert n.send("x") is False           # swallowed, loop survives
