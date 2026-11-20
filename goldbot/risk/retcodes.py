"""MetaTrader 5 trade-server return codes and what to do about each (section 7.2)."""

from __future__ import annotations

from enum import Enum


class Action(Enum):
    DONE = "record the fill; recompute actual risk"
    RECONCILE = "reconcile the remainder"
    RETRY_REFRESH = "refresh the tick; retry at most twice; never chase"
    REJECT = "log and alert; no retry"
    FIX_ROUNDING = "fix volume/price normalisation; no blind retry"
    WIDEN_OR_SKIP = "widen to the minimum distance AND recompute size, or skip"
    SKIP = "skip"
    HALT = "CRITICAL: halt trading"
    BACKOFF = "back off"
    SWITCH_FILLING = "switch the filling mode once"
    RECONNECT = "run the reconnect flow"
    UNKNOWN = "log and alert; no automatic retry"


_TABLE = {
    10009: Action.DONE,
    10008: Action.RECONCILE, 10010: Action.RECONCILE,
    10004: Action.RETRY_REFRESH, 10020: Action.RETRY_REFRESH, 10021: Action.RETRY_REFRESH,
    10006: Action.REJECT,
    10013: Action.FIX_ROUNDING, 10014: Action.FIX_ROUNDING, 10015: Action.FIX_ROUNDING,
    10016: Action.WIDEN_OR_SKIP,
    10018: Action.SKIP,
    10019: Action.HALT,
    10024: Action.BACKOFF,
    10027: Action.HALT,
    10030: Action.SWITCH_FILLING,
    10031: Action.RECONNECT,
}


def action_for(retcode: int) -> Action:
    return _TABLE.get(retcode, Action.UNKNOWN)


def is_known(retcode: int) -> bool:
    return retcode in _TABLE
