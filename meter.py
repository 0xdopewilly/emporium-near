"""What each paid job cost to run, from Hermes's own books.

Hermes prices every session it runs (``estimated_cost_usd`` / ``actual_cost_usd`` in the profile's
state.db, through ``hermes_state.SessionDB``). Each shop job runs in its own session, so its inference
cost is that session's cost. Token deltas reach state.db a moment after the turn ends, so costs are
filled in lazily, a few seconds after a job finishes, by whoever reads the books next.
"""

from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

UNKNOWN = "-"          # Hermes has no price for the model (e.g. a local model): cost stays unknown
_GIVE_UP_AFTER = 900   # seconds after a job finishes
_last_fill = 0.0
_fill_lock = threading.Lock()


def _session_cost(db: Any, session_id: str) -> tuple[str, bool]:
    """(cost, final). cost is a decimal string, UNKNOWN, or '' when the session isn't priced yet."""
    row = db.get_session(session_id)
    if not row:
        return "", False
    for key in ("actual_cost_usd", "estimated_cost_usd"):
        value = row.get(key)
        if value is None:
            continue
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            continue
        if amount.is_finite() and amount >= 0:
            return format(amount.quantize(Decimal("0.0000001")).normalize(), "f"), True
    status = str(row.get("cost_status") or "")
    if status in ("unknown", "included"):
        return (UNKNOWN if status == "unknown" else "0"), True
    return "", False


def fill_costs(books: Any, *, min_interval: float = 2.0, limit: int = 50) -> int:
    """Copy finished jobs' inference costs from Hermes's session store into the ledger. Cheap to call
    often: it runs at most every ``min_interval`` seconds and only touches jobs still missing a cost."""
    global _last_fill
    with _fill_lock:
        now = time.time()
        if now - _last_fill < min_interval:
            return 0
        _last_fill = now
    try:
        rows = books.jobs_missing_cost(limit=limit)
    except Exception:
        return 0
    if not rows:
        return 0
    try:
        from hermes_state import SessionDB
        db = SessionDB(read_only=True)
    except Exception:
        logger.debug("Emporium: Hermes's session store isn't readable here", exc_info=True)
        return 0
    filled = 0
    try:
        for r in rows:
            try:
                cost, final = _session_cost(db, r["session"])
            except Exception:
                continue
            if not final and r.get("finished") and now - float(r["finished"]) > _GIVE_UP_AFTER:
                cost, final = UNKNOWN, True
            if final:
                books.update_job(r["id"], cost_usd=cost)
                filled += 1
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    return filled
