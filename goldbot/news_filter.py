"""C. High-impact economic news blackout engine.

Source: the public ForexFactory weekly calendar JSON feed
(``nfs.faireconomy.media``). The feed is rate-limited, so it is fetched at most
every ``refresh_minutes`` and cached to disk; restarts reuse the cache.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

import requests

from .config import NewsConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsEvent:
    time: datetime  # UTC
    currency: str
    impact: str
    title: str

    @property
    def event_id(self) -> str:
        return f"{self.currency}|{self.title}|{self.time.isoformat()}"


@dataclass
class NewsState:
    blackout: bool
    reason: str = ""
    next_event: Optional[NewsEvent] = None
    imminent: List[NewsEvent] = field(default_factory=list)


def parse_calendar(raw_events: list, cfg: NewsConfig) -> List[NewsEvent]:
    """Keep only events matching the configured currencies/impacts/keywords."""
    currencies = {c.upper() for c in cfg.currencies}
    impacts = {i.lower() for i in cfg.impacts}
    keywords = [k.lower() for k in cfg.title_keywords]
    events = []
    for item in raw_events:
        try:
            if str(item.get("country", "")).upper() not in currencies:
                continue
            if str(item.get("impact", "")).lower() not in impacts:
                continue
            title = str(item.get("title", ""))
            if keywords and not any(k in title.lower() for k in keywords):
                continue
            when = datetime.fromisoformat(str(item["date"]))
            if when.tzinfo is None:
                continue  # all-day/tentative entries carry no usable time
            events.append(NewsEvent(time=when.astimezone(timezone.utc),
                                    currency=item["country"].upper(),
                                    impact=item["impact"], title=title))
        except (KeyError, ValueError, TypeError) as exc:
            log.debug("Skipping malformed calendar row %s: %s", item, exc)
    return sorted(events, key=lambda e: e.time)


class NewsFilterEngine:
    """Downloads the calendar and answers 'are we in a blackout right now?'."""

    def __init__(self, cfg: NewsConfig,
                 http_get: Callable = requests.get,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.cfg = cfg
        self._http_get = http_get
        self._clock = clock
        self._events: List[NewsEvent] = []
        self._fetched_at: Optional[datetime] = None
        self._last_attempt: Optional[datetime] = None
        self._load_cache()

    # ----------------------------------------------------------------- #
    def _load_cache(self) -> None:
        path = Path(self.cfg.cache_file)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._fetched_at = datetime.fromisoformat(data["fetched_at"])
            self._events = parse_calendar(data["events"], self.cfg)
            log.info("Loaded %d cached calendar events (fetched %s)",
                     len(self._events), self._fetched_at.isoformat())
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Ignoring unreadable calendar cache: %s", exc)

    def _save_cache(self, raw: list, fetched_at: datetime) -> None:
        path = Path(self.cfg.cache_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"fetched_at": fetched_at.isoformat(),
                                        "events": raw}), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write calendar cache: %s", exc)

    def refresh(self, force: bool = False) -> None:
        """Re-download the calendar if the refresh interval has elapsed."""
        now = self._clock()
        interval = timedelta(minutes=self.cfg.refresh_minutes)
        # A fresh on-disk cache counts as a recent attempt (respects rate limit).
        last = self._last_attempt or self._fetched_at
        if not force and last and now - last < interval:
            return
        self._last_attempt = now
        try:
            resp = self._http_get(self.cfg.calendar_url,
                                  timeout=self.cfg.request_timeout_s,
                                  headers={"User-Agent": "goldbot/1.0"})
            resp.raise_for_status()
            raw = resp.json()
            if not isinstance(raw, list):
                raise ValueError("calendar payload is not a list")
        except (requests.RequestException, ValueError) as exc:
            log.warning("Calendar download failed (%s); using cached data", exc)
            return
        self._events = parse_calendar(raw, self.cfg)
        self._fetched_at = now
        self._save_cache(raw, now)
        log.info("Calendar refreshed: %d matching high-impact events this week",
                 len(self._events))

    # ----------------------------------------------------------------- #
    def calendar_is_valid(self) -> bool:
        if self._fetched_at is None:
            return False
        age = self._clock() - self._fetched_at
        return age <= timedelta(hours=self.cfg.max_cache_age_hours)

    def state(self) -> NewsState:
        if not self.cfg.enabled:
            return NewsState(blackout=False, reason="news filter disabled")
        now = self._clock()
        before = timedelta(minutes=self.cfg.minutes_before)
        after = timedelta(minutes=self.cfg.minutes_after)

        imminent = [e for e in self._events if now <= e.time <= now + before]
        upcoming = [e for e in self._events if e.time > now]
        next_event = upcoming[0] if upcoming else None

        for ev in self._events:
            if ev.time - before <= now <= ev.time + after:
                return NewsState(blackout=True,
                                 reason=f"{ev.currency} {ev.title} @ {ev.time:%H:%M}Z",
                                 next_event=next_event, imminent=imminent)

        if not self.calendar_is_valid():
            if self.cfg.fail_closed:
                return NewsState(blackout=True,
                                 reason="calendar unavailable (fail-closed)",
                                 next_event=next_event, imminent=imminent)
            return NewsState(blackout=False, reason="calendar unavailable",
                             next_event=next_event, imminent=imminent)
        return NewsState(blackout=False, next_event=next_event, imminent=imminent)
