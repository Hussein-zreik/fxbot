from goldbot.config import AlertsConfig
from goldbot.notifier import TelegramNotifier


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self.payload = status, payload or {}

    def json(self):
        return self.payload


def make(**cfg):
    sent = []

    def post(url, json, timeout):
        sent.append((url, json))
        return Resp()
    n = TelegramNotifier(AlertsConfig(**cfg), http_post=post, token="T0K", chat_id="42")
    return n, sent


def test_send_and_deliver():
    n, sent = make()
    assert n.send("Opened BUY", "trade")
    n.deliver(n._queue.get_nowait())
    url, body = sent[0]
    assert url.endswith("/botT0K/sendMessage")
    assert body["chat_id"] == "42" and body["text"].endswith("Opened BUY")
    assert "parse_mode" not in body            # plain text: headlines can't inject markup


def test_category_filter_and_dedupe():
    n, _ = make(categories=["risk"])
    assert not n.send("Opened BUY", "trade")
    assert n.send("Daily limit", "risk")
    assert not n.send("Daily limit", "risk")   # duplicate within 60 s


def test_inactive_without_credentials():
    n = TelegramNotifier(AlertsConfig(), token="", chat_id="")
    assert not n.active and not n.send("x", "trade")


def test_background_worker_delivers():
    n, sent = make()
    n.start()
    n.send("hello", "system")
    n.stop()
    assert sent and sent[0][1]["text"].endswith("hello")


def test_http_error_is_contained():
    n = TelegramNotifier(AlertsConfig(), http_post=lambda *a, **k: Resp(401),
                         token="bad", chat_id="1")
    assert n.deliver("x") is False
