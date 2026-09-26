import json
from datetime import datetime, timedelta, timezone

import pytest

from goldbot.config import SentimentConfig
from goldbot.sentiment_engine import (LLMHeadlineScorer, SentimentEngine,
                                      headline_id, parse_feed)

NOW = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S GMT')}"
        f"</pubDate></item>" for t, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'


ATOM = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><title>Gold hits record as Fed signals cuts</title>'
        '<updated>2026-09-23T13:30:00Z</updated></entry></feed>')


class Resp:
    def __init__(self, text="", payload=None, status=200):
        self.text, self.payload, self.status_code = text, payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


def test_parse_rss_and_atom():
    items = parse_feed(rss([("Gold jumps - Reuters", NOW)]), NOW)
    assert items[0].title == "Gold jumps" and items[0].published == NOW
    atom = parse_feed(ATOM, NOW)
    assert atom[0].published == datetime(2026, 9, 23, 13, 30, tzinfo=timezone.utc)


def test_same_story_from_two_outlets_is_one_headline():
    assert headline_id("Fed holds rates - Reuters") == headline_id("Fed holds rates - CNBC")


def test_llm_reply_parsed_and_clamped():
    heads = parse_feed(rss([("A", NOW), ("B", NOW)]), NOW)
    reply = ('Sure! {"scores": [{"i": 0, "impact": 3, "relevance": 0.9},'
             ' {"i": 1, "impact": -0.4, "relevance": -1}, {"i": 7, "impact": 1}]}')
    out = LLMHeadlineScorer._parse(reply, heads)
    assert out[heads[0].hid] == (1.0, 0.9)
    assert out[heads[1].hid] == (-0.4, 0.0)
    assert len(out) == 2


class FakeScorer:
    def __init__(self, table):
        self.table, self.calls = table, 0

    def score(self, headlines):
        self.calls += 1
        return {h.hid: self.table[h.title] for h in headlines if h.title in self.table}


def make_engine(tmp_path, feed_xml, table, now=NOW, **cfg):
    config = SentimentConfig(cache_file=str(tmp_path / "s.json"),
                             feeds=["http://feed"], **cfg)
    scorer = FakeScorer(table)
    eng = SentimentEngine(config, scorer=scorer,
                          http_get=lambda *a, **k: Resp(text=feed_xml),
                          clock=lambda: now)
    return eng, scorer


BULLISH = [("Fed signals rate cuts", NOW - timedelta(minutes=20)),
           ("Dollar slides after weak jobs data", NOW - timedelta(minutes=40)),
           ("Central banks keep buying gold", NOW - timedelta(hours=1)),
           ("Local football results", NOW - timedelta(minutes=10))]
TABLE = {"Fed signals rate cuts": (0.8, 0.9),
         "Dollar slides after weak jobs data": (0.6, 0.8),
         "Central banks keep buying gold": (0.5, 0.7),
         "Local football results": (-1.0, 0.0)}   # irrelevant -> ignored


def test_bullish_news_scores_positive(tmp_path):
    eng, _ = make_engine(tmp_path, rss(BULLISH), TABLE)
    eng.refresh()
    snap = eng.snapshot()
    assert snap.relevant_count == 3
    assert 0.6 < snap.sentiment < 0.8
    assert snap.score == 7                      # round(0.7 * 10)
    assert "Fed signals rate cuts" in snap.top[0]


def test_too_few_headlines_scores_zero(tmp_path):
    eng, _ = make_engine(tmp_path, rss(BULLISH[:2]), TABLE)
    eng.refresh()
    assert eng.snapshot().score == 0


def test_old_headlines_decay(tmp_path):
    items = [("Fed signals rate cuts", NOW - timedelta(minutes=5)),
             ("Dollar slides after weak jobs data", NOW - timedelta(minutes=5)),
             ("Gold slumps on hawkish Fed", NOW - timedelta(hours=5)),
             ("Gold slumps as yields jump", NOW - timedelta(hours=5))]
    table = dict(TABLE)
    table["Gold slumps on hawkish Fed"] = (-1.0, 1.0)
    table["Gold slumps as yields jump"] = (-1.0, 1.0)
    eng, _ = make_engine(tmp_path, rss(items), table)
    eng.refresh()
    assert eng.snapshot().sentiment > 0          # fresh bullish news dominates


def test_cache_avoids_rescoring_after_restart(tmp_path):
    eng, scorer = make_engine(tmp_path, rss(BULLISH), TABLE)
    eng.refresh()
    eng.refresh()
    assert scorer.calls == 1
    eng2, scorer2 = make_engine(tmp_path, rss(BULLISH), TABLE)
    eng2.refresh()
    assert scorer2.calls == 0 and eng2.snapshot().score == 7


def test_api_failure_is_contained(tmp_path):
    class Broken:
        def score(self, headlines):
            raise RuntimeError("AI API rate limit reached (429)")
    config = SentimentConfig(cache_file=str(tmp_path / "s.json"), feeds=["http://f"])
    eng = SentimentEngine(config, scorer=Broken(),
                          http_get=lambda *a, **k: Resp(text=rss(BULLISH)),
                          clock=lambda: NOW)
    eng.refresh()
    snap = eng.snapshot()
    assert snap.score == 0 and "429" in snap.error


def test_missing_api_key_disables_engine(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    eng = SentimentEngine(SentimentConfig(cache_file=str(tmp_path / "s.json")))
    assert not eng.active and eng.snapshot().score == 0


def test_real_client_request_shape(tmp_path):
    sent = {}

    def post(url, json, timeout, headers):
        sent.update(url=url, body=json, headers=headers)
        content = '{"scores": [{"i": 0, "impact": 0.5, "relevance": 1}]}'
        return Resp(payload={"choices": [{"message": {"content": content}}]})

    heads = parse_feed(rss([("Gold rallies", NOW)]), NOW)
    out = LLMHeadlineScorer(SentimentConfig(), "k-123", http_post=post).score(heads)
    assert sent["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer k-123"
    assert sent["body"]["response_format"] == {"type": "json_object"}
    assert "0. Gold rallies" in sent["body"]["messages"][1]["content"]
    assert out[heads[0].hid] == (0.5, 1.0)


def test_rate_limit_raises_clear_error():
    heads = parse_feed(rss([("Gold rallies", NOW)]), NOW)
    scorer = LLMHeadlineScorer(SentimentConfig(), "k",
                               http_post=lambda *a, **k: Resp(status=429))
    with pytest.raises(RuntimeError, match="429"):
        scorer.score(heads)


def test_cache_file_is_json(tmp_path):
    eng, _ = make_engine(tmp_path, rss(BULLISH), TABLE)
    eng.refresh()
    data = json.loads((tmp_path / "s.json").read_text())
    assert len(data) == 4
