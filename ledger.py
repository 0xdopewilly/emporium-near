"""The books: every job sold and every job bought, in one SQLite file under the profile's plugin-data.

Shared by the gateway (the shop), the CLI/TUI (the /shop command and the buyer tools) and the
dashboard, which may all be different processes: SQLite in WAL mode is the meeting point.
Inputs and outputs are never stored, only their sizes; the ledger is safe to show on a dashboard.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

PLUGIN = "emporium"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    price_usd TEXT NOT NULL,
    network TEXT NOT NULL,
    payer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    tx TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    input_chars INTEGER NOT NULL DEFAULT 0,
    output_chars INTEGER NOT NULL DEFAULT 0,
    tools_blocked INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    started REAL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created);
CREATE INDEX IF NOT EXISTS jobs_payer ON jobs(payer, created);
CREATE TABLE IF NOT EXISTS purchases (
    id TEXT PRIMARY KEY,
    shop_url TEXT NOT NULL,
    shop_name TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL,
    price_usd TEXT NOT NULL,
    network TEXT NOT NULL DEFAULT '',
    pay_to TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    tx TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    output_chars INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS purchases_created ON purchases(created);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

# Job lifecycle. Only SETTLED moves money; every other end state means the buyer was not charged.
PAID = "paid"            # payment verified by the facilitator, nothing moved yet
WORKING = "working"      # the agent is on it
SETTLED = "settled"      # delivered and the payment settled on-chain
FAILED = "failed"        # the agent failed or timed out: not charged
UNPAID = "unpaid"        # settlement failed after the work: the result was withheld
REFUSED = "refused"      # refused before any payment (closed, busy, bad input, rate limit)

# Purchase lifecycle (buyer side).
BOUGHT = "bought"
NOT_CHARGED = "not_charged"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _start_of_day(now: Optional[float] = None) -> float:
    t = time.localtime(now if now is not None else time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


class Ledger:
    """Thread-safe wrapper over one connection. Open with ``Ledger.open()`` (the profile's
    plugin-data) or ``Ledger(path)`` in tests."""

    def __init__(self, path: Optional[str | Path] = None, conn: Optional[sqlite3.Connection] = None):
        if conn is None:
            conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
        self._conn = conn
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @classmethod
    def open(cls) -> "Ledger":
        from plugins.plugin_storage import plugin_db
        return cls(conn=plugin_db(PLUGIN))

    # ---- low level -------------------------------------------------------------------------
    def _exec(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, args)
            self._conn.commit()

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---- meta (shared flags such as open/closed) -------------------------------------------
    def get_meta(self, key: str, default: str = "") -> str:
        rows = self._rows("SELECT v FROM meta WHERE k = ?", (key,))
        return rows[0]["v"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        self._exec("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))

    def is_open(self) -> bool:
        return self.get_meta("open", "1") == "1"

    def set_open(self, value: bool) -> None:
        self.set_meta("open", "1" if value else "0")

    # ---- jobs (seller side) ----------------------------------------------------------------
    def create_job(self, service: str, title: str, price: Decimal, network: str, payer: str,
                   status: str = PAID, input_chars: int = 0, error: str = "") -> str:
        jid = new_id("job")
        self._exec(
            "INSERT INTO jobs(id, service, title, price_usd, network, payer, status, input_chars, error, created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (jid, service, title, str(price), network, payer.lower(), status, input_chars, error, time.time()))
        return jid

    def update_job(self, jid: str, **fields: Any) -> None:
        allowed = {"status", "tx", "error", "output_chars", "started", "finished", "tools_blocked", "payer"}
        keys = [k for k in fields if k in allowed]
        if not keys:
            return
        if "payer" in fields:
            fields["payer"] = str(fields["payer"] or "").lower()
        self._exec(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                   (*[fields[k] for k in keys], jid))

    def bump_blocked(self, jid: str) -> None:
        self._exec("UPDATE jobs SET tools_blocked = tools_blocked + 1 WHERE id = ?", (jid,))

    def job(self, jid: str) -> Optional[dict[str, Any]]:
        rows = self._rows("SELECT * FROM jobs WHERE id = ?", (jid,))
        return rows[0] if rows else None

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (int(limit),))

    def payer_jobs_since(self, payer: str, since: float) -> int:
        rows = self._rows("SELECT COUNT(*) AS n FROM jobs WHERE payer = ? AND created >= ? AND status != ?",
                          (payer.lower(), since, REFUSED))
        return int(rows[0]["n"]) if rows else 0

    def sales_summary(self) -> dict[str, Any]:
        day = _start_of_day()
        settled = self._rows("SELECT service, title, price_usd, created FROM jobs WHERE status = ?", (SETTLED,))
        total = sum((Decimal(r["price_usd"]) for r in settled), Decimal(0))
        today = sum((Decimal(r["price_usd"]) for r in settled if r["created"] >= day), Decimal(0))
        per: dict[str, dict[str, Any]] = {}
        for r in settled:
            p = per.setdefault(r["service"], {"service": r["service"], "title": r["title"], "jobs": 0, "earned": Decimal(0)})
            p["jobs"] += 1
            p["earned"] += Decimal(r["price_usd"])
        counts = {r["status"]: r["n"] for r in self._rows("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        buyers = self._rows("SELECT COUNT(DISTINCT payer) AS n FROM jobs WHERE status = ?", (SETTLED,))
        return {
            "earned_total": total, "earned_today": today, "jobs_settled": counts.get(SETTLED, 0),
            "jobs_today": sum(1 for r in settled if r["created"] >= day), "counts": counts,
            "buyers": int(buyers[0]["n"]) if buyers else 0,
            "per_service": sorted(per.values(), key=lambda p: p["earned"], reverse=True),
        }

    # ---- purchases (buyer side) ------------------------------------------------------------
    def create_purchase(self, shop_url: str, shop_name: str, service: str, price: Decimal,
                        network: str, pay_to: str) -> str:
        pid = new_id("buy")
        self._exec(
            "INSERT INTO purchases(id, shop_url, shop_name, service, price_usd, network, pay_to, status, created) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (pid, shop_url, shop_name, service, str(price), network, pay_to.lower(), "pending", time.time()))
        return pid

    def update_purchase(self, pid: str, **fields: Any) -> None:
        allowed = {"status", "tx", "error", "output_chars", "finished", "price_usd"}
        keys = [k for k in fields if k in allowed]
        if keys:
            self._exec(f"UPDATE purchases SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                       (*[str(fields[k]) if k == "price_usd" else fields[k] for k in keys], pid))

    def recent_purchases(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM purchases ORDER BY created DESC LIMIT ?", (int(limit),))

    def spent_today(self) -> Decimal:
        """Money committed today: bought, plus anything still in flight (so parallel hires can't overshoot)."""
        rows = self._rows("SELECT price_usd FROM purchases WHERE created >= ? AND status IN ('pending', ?)",
                          (_start_of_day(), BOUGHT))
        return sum((Decimal(r["price_usd"]) for r in rows), Decimal(0))

    def purchase_summary(self) -> dict[str, Any]:
        rows = self._rows("SELECT price_usd, created FROM purchases WHERE status = ?", (BOUGHT,))
        return {"spent_total": sum((Decimal(r["price_usd"]) for r in rows), Decimal(0)),
                "spent_today": self.spent_today(), "bought": len(rows)}
