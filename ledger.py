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
CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, job TEXT NOT NULL, created REAL NOT NULL);
-- Money moved across chains through NEAR Intents (intents.py): a buyer funding its NEAR account from
-- another chain, or the shop cashing out to one. One row per 1Click deposit address (one per quote).
CREATE TABLE IF NOT EXISTS transfers (
    deposit TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                 -- fund | cashout
    usd TEXT NOT NULL,                  -- USDC arriving on NEAR (fund) or leaving it (cash-out)
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    recipient TEXT NOT NULL,
    refund_to TEXT NOT NULL,
    amount_in TEXT NOT NULL,
    amount_out TEXT NOT NULL,
    status TEXT NOT NULL,               -- quoted, registering, signed, sending, sent, then 1Click's status
    quote TEXT NOT NULL,                -- 1Click's signed quote response, kept to settle any dispute
    signature TEXT NOT NULL,
    signed TEXT NOT NULL DEFAULT '',    -- the one signed transfer that pays this deposit, kept before it's sent
    storage_tx TEXT NOT NULL DEFAULT '',
    deposit_tx TEXT NOT NULL DEFAULT '',
    dest_txs TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS transfers_created ON transfers(created);
-- Keyless demo hires (demo.py): this shop's own services, paid from the owner's demo account. The caller is
-- kept only as a salted hash of its address, enough to rate-limit it; the rows survive a restart, so the caps do.
CREATE TABLE IF NOT EXISTS demo_hires (
    id TEXT PRIMARY KEY,
    caller TEXT NOT NULL,
    service TEXT NOT NULL,
    status TEXT NOT NULL,               -- running, delivered, not_delivered, unknown
    tx TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS demo_hires_created ON demo_hires(created);
CREATE INDEX IF NOT EXISTS demo_hires_caller ON demo_hires(caller, created);
"""

# Columns added after 0.1.0, applied to existing ledgers with ALTER TABLE (SQLite has no ADD COLUMN IF NOT EXISTS).
_ADDED = {
    "jobs": [
        ("session", "TEXT NOT NULL DEFAULT ''"),       # the Hermes session that worked the job
        ("model", "TEXT NOT NULL DEFAULT ''"),
        ("llm_calls", "INTEGER NOT NULL DEFAULT 0"),
        ("tokens_in", "INTEGER NOT NULL DEFAULT 0"),
        ("tokens_out", "INTEGER NOT NULL DEFAULT 0"),
        ("tool_calls", "INTEGER NOT NULL DEFAULT 0"),
        ("cost_usd", "TEXT NOT NULL DEFAULT ''"),      # inference cost, from Hermes's own session pricing ('' = not known yet)
        ("sub_usd", "TEXT NOT NULL DEFAULT '0'"),      # paid to other shops for this job (subcontracts)
        ("output_sha256", "TEXT NOT NULL DEFAULT ''"),
    ],
    "purchases": [
        ("for_job", "TEXT NOT NULL DEFAULT ''"),       # set when a shop job subcontracted this purchase
    ],
}

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
UNKNOWN = "unknown"      # the shop never answered after we sent the signed payment: it may still settle


_SHARED: dict[str, "Ledger"] = {}
_SHARED_LOCK = threading.Lock()


class BudgetError(Exception):
    """A purchase that would break a budget (nothing was signed)."""


class DemoLimit(Exception):
    """A keyless demo hire over one of its caps. ``kind`` is "caller" or "daily"; ``retry_after`` is in seconds."""

    def __init__(self, kind: str, retry_after: int):
        super().__init__(kind)
        self.kind, self.retry_after = kind, max(1, int(retry_after))


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _start_of_day(now: Optional[float] = None) -> float:
    t = time.localtime(now if now is not None else time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def utc_day_start(now: Optional[float] = None) -> float:
    """00:00 UTC of the day ``now`` falls in: the demo's daily cap resets then, wherever the shop runs."""
    now = time.time() if now is None else now
    return now - (now % 86400)


def _nonce_key(nonce: str) -> str:
    """Hex nonces (EVM) compare case-insensitively; a NEAR one names a base58 key, which is case-sensitive."""
    return nonce if nonce.startswith("near:") else nonce.lower()


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
            for table, cols in _ADDED.items():
                have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols:
                    if name not in have:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            self._conn.commit()

    @classmethod
    def open(cls) -> "Ledger":
        """The profile's ledger, one shared connection per process (hooks call this on every model and
        tool call, so it must not open a new SQLite connection each time)."""
        from plugins.plugin_storage import plugin_data_dir, plugin_db
        key = str(plugin_data_dir(PLUGIN))
        with _SHARED_LOCK:
            led = _SHARED.get(key)
            if led is None:
                led = _SHARED[key] = cls(conn=plugin_db(PLUGIN))
            return led

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
                   status: str = PAID, input_chars: int = 0, error: str = "", job_id: str = "") -> str:
        jid = job_id or new_id("job")
        self._exec(
            "INSERT INTO jobs(id, service, title, price_usd, network, payer, status, input_chars, error, created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (jid, service, title, str(price), network, payer.lower(), status, input_chars, error, time.time()))
        return jid

    def update_job(self, jid: str, **fields: Any) -> None:
        allowed = {"status", "tx", "error", "output_chars", "started", "finished", "tools_blocked", "payer",
                   "session", "model", "cost_usd", "output_sha256"}
        keys = [k for k in fields if k in allowed]
        if not keys:
            return
        if "payer" in fields:
            fields["payer"] = str(fields["payer"] or "").lower()
        self._exec(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                   (*[fields[k] for k in keys], jid))

    def bump_blocked(self, jid: str) -> None:
        self._exec("UPDATE jobs SET tools_blocked = tools_blocked + 1 WHERE id = ?", (jid,))

    def bump_tool(self, jid: str) -> None:
        self._exec("UPDATE jobs SET tool_calls = tool_calls + 1 WHERE id = ?", (jid,))

    def add_usage(self, jid: str, tokens_in: int, tokens_out: int, model: str = "") -> None:
        """One model call made while working the job (from Hermes's post_api_request hook)."""
        self._exec("UPDATE jobs SET llm_calls = llm_calls + 1, tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, "
                   "model = CASE WHEN ? != '' THEN ? ELSE model END WHERE id = ?",
                   (int(tokens_in or 0), int(tokens_out or 0), model or "", model or "", jid))

    def add_sub(self, jid: str, amount: Decimal) -> None:
        with self._lock:
            row = self._conn.execute("SELECT sub_usd FROM jobs WHERE id = ?", (jid,)).fetchone()
            if row is not None:
                self._conn.execute("UPDATE jobs SET sub_usd = ? WHERE id = ?", (str(Decimal(row[0] or "0") + amount), jid))
                self._conn.commit()

    def claim_nonce(self, nonce: str, jid: str) -> bool:
        """Record a payment authorization's one-time nonce. False when it was already used here, so the
        same signed payment can't buy two jobs (sent twice at once, both would pass verification)."""
        if not nonce:
            return True
        with self._lock:
            try:
                self._conn.execute("INSERT INTO nonces(nonce, job, created) VALUES(?,?,?)", (_nonce_key(nonce), jid, time.time()))
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False

    def release_nonce(self, nonce: str, jid: str) -> None:
        """Free a nonce this job claimed but never got paid for (the job failed or didn't settle)."""
        if not nonce:
            return
        with self._lock:
            self._conn.execute("DELETE FROM nonces WHERE nonce = ? AND job = ?", (_nonce_key(nonce), jid))
            self._conn.commit()

    def fail_stale_jobs(self) -> int:
        """Jobs left mid-flight by a restart: their buyers got no response, and nothing was settled."""
        with self._lock:
            cur = self._conn.execute("UPDATE jobs SET status = ?, error = 'interrupted by a shop restart', finished = ? "
                                     "WHERE status IN (?, ?)", (FAILED, time.time(), WORKING, PAID))
            self._conn.commit()
            return cur.rowcount or 0

    def jobs_missing_cost(self, older_than: float = 3.0, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows("SELECT id, session, finished FROM jobs WHERE cost_usd = '' AND session != '' AND finished IS NOT NULL "
                          "AND finished < ? ORDER BY finished DESC LIMIT ?", (time.time() - older_than, int(limit)))

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
        """Earnings, costs and service quality. Money is Decimal; 'cost' is Hermes's own estimate of the
        inference each job used, plus anything paid to other shops for it."""
        day = _start_of_day()
        settled = self._rows("SELECT service, title, price_usd, created, started, finished, cost_usd, sub_usd, "
                             "tokens_in, tokens_out, llm_calls FROM jobs WHERE status = ?", (SETTLED,))
        dec = lambda v: Decimal(v) if v not in (None, "", "-") else Decimal(0)  # noqa: E731
        total = sum((dec(r["price_usd"]) for r in settled), Decimal(0))
        today = sum((dec(r["price_usd"]) for r in settled if r["created"] >= day), Decimal(0))
        priced = [r for r in settled if r["cost_usd"] not in ("", "-")]
        inference = sum((dec(r["cost_usd"]) for r in priced), Decimal(0))
        subs = sum((dec(r["sub_usd"]) for r in settled), Decimal(0))
        revenue_priced = sum((dec(r["price_usd"]) for r in priced), Decimal(0))
        per: dict[str, dict[str, Any]] = {}
        for r in settled:
            p = per.setdefault(r["service"], {"service": r["service"], "title": r["title"], "jobs": 0, "earned": Decimal(0),
                                              "cost": Decimal(0)})
            p["jobs"] += 1
            p["earned"] += dec(r["price_usd"])
            p["cost"] += dec(r["cost_usd"]) + dec(r["sub_usd"])
        counts = {r["status"]: r["n"] for r in self._rows("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        buyers = self._rows("SELECT COUNT(DISTINCT payer) AS n FROM jobs WHERE status = ?", (SETTLED,))
        secs = sorted(r["finished"] - r["started"] for r in settled if r["finished"] and r["started"])
        pct = lambda q: round(secs[min(len(secs) - 1, int(q * len(secs)))], 1) if secs else None  # noqa: E731
        finished = counts.get(SETTLED, 0) + counts.get(FAILED, 0) + counts.get(UNPAID, 0)
        blocked = self._rows("SELECT COALESCE(SUM(tools_blocked), 0) AS n, COALESCE(SUM(tokens_in + tokens_out), 0) AS t "
                             "FROM jobs")[0]
        # Margin over the jobs whose inference cost is known (a job's cost lands a few seconds after it settles).
        margin = None
        if revenue_priced > 0:
            psubs = sum((dec(r["sub_usd"]) for r in priced), Decimal(0))
            margin = float((revenue_priced - sum((dec(r["cost_usd"]) for r in priced), Decimal(0)) - psubs) / revenue_priced)
        return {
            "earned_total": total, "earned_today": today, "jobs_settled": counts.get(SETTLED, 0),
            "jobs_today": sum(1 for r in settled if r["created"] >= day), "counts": counts,
            "buyers": int(buyers[0]["n"]) if buyers else 0,
            "per_service": sorted(per.values(), key=lambda p: p["earned"], reverse=True),
            "inference_cost": inference, "subcontract_cost": subs, "profit": total - inference - subs,
            "margin": margin, "priced_jobs": len(priced),
            "p50_seconds": pct(0.5), "p95_seconds": pct(0.95),
            "delivery_rate": (counts.get(SETTLED, 0) / finished) if finished else None,
            "not_charged": counts.get(FAILED, 0) + counts.get(UNPAID, 0),
            "tools_refused": int(blocked["n"]), "tokens": int(blocked["t"]),
        }

    # ---- purchases (buyer side) ------------------------------------------------------------
    def create_purchase(self, shop_url: str, shop_name: str, service: str, price: Decimal,
                        network: str, pay_to: str, for_job: str = "") -> str:
        pid = new_id("buy")
        self._exec(
            "INSERT INTO purchases(id, shop_url, shop_name, service, price_usd, network, pay_to, status, created, for_job) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pid, shop_url, shop_name, service, str(price), network, pay_to.lower(), "pending", time.time(), for_job))
        return pid

    def reserve_purchase(self, shop_url: str, shop_name: str, service: str, price: Decimal, network: str, pay_to: str,
                         daily_budget: Decimal, for_job: str = "", job_budget: Optional[Decimal] = None) -> str:
        """Check the daily budget (and a job's subcontract budget) and record the purchase as pending in
        one step, so parallel hires can't both squeeze under the same remaining budget."""
        with self._lock:
            day = _start_of_day()
            rows = self._conn.execute("SELECT price_usd FROM purchases WHERE created >= ? AND status IN ('pending', ?, ?)",
                                      (day, BOUGHT, UNKNOWN)).fetchall()
            spent = sum((Decimal(r[0]) for r in rows), Decimal(0))
            if price > daily_budget - spent:
                left = max(daily_budget - spent, Decimal(0))
                raise BudgetError(f"only ${left} of today's ${daily_budget} budget is left")
            if for_job and job_budget is not None:
                rows = self._conn.execute("SELECT price_usd FROM purchases WHERE for_job = ? AND status IN ('pending', ?, ?)",
                                          (for_job, BOUGHT, UNKNOWN)).fetchall()
                used = sum((Decimal(r[0]) for r in rows), Decimal(0))
                if price > job_budget - used:
                    raise BudgetError(f"this job may spend at most ${job_budget} on subcontracts and ${used} is committed")
            pid = new_id("buy")
            self._conn.execute(
                "INSERT INTO purchases(id, shop_url, shop_name, service, price_usd, network, pay_to, status, created, for_job) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pid, shop_url, shop_name, service, str(price), network, pay_to.lower(), "pending", time.time(), for_job))
            self._conn.commit()
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
        rows = self._rows("SELECT price_usd FROM purchases WHERE created >= ? AND status IN ('pending', ?, ?)",
                          (_start_of_day(), BOUGHT, UNKNOWN))
        return sum((Decimal(r["price_usd"]) for r in rows), Decimal(0))

    # ---- transfers across chains (NEAR Intents) ----------------------------------------------
    _TRANSFER_COLS = ("deposit", "kind", "usd", "origin", "destination", "recipient", "refund_to", "amount_in",
                      "amount_out", "status", "quote", "signature")
    _TRANSFER_UPDATABLE = {"status", "signed", "storage_tx", "deposit_tx", "dest_txs", "error"}

    def add_transfer(self, row: dict[str, Any]) -> None:
        """Record a live quote. A deposit address already recorded is left as it is (a retry never resets it)."""
        now = time.time()
        cols = self._TRANSFER_COLS
        self._exec(f"INSERT OR IGNORE INTO transfers({', '.join(cols)}, created, updated) "
                   f"VALUES({', '.join('?' for _ in cols)}, ?, ?)", (*[str(row.get(c) or "") for c in cols], now, now))

    def update_transfer(self, deposit: str, **fields: Any) -> None:
        keys = [k for k in fields if k in self._TRANSFER_UPDATABLE]
        if keys:
            self._exec(f"UPDATE transfers SET {', '.join(f'{k} = ?' for k in keys)}, updated = ? WHERE deposit = ?",
                       (*[str(fields[k] or "") for k in keys], time.time(), deposit))

    def transfer(self, deposit: str) -> Optional[dict[str, Any]]:
        rows = self._rows("SELECT * FROM transfers WHERE deposit = ?", (deposit,))
        return rows[0] if rows else None

    def find_transfer(self, prefix: str) -> Optional[dict[str, Any]]:
        """A transfer by its deposit address or a unique start of it (at least 6 characters)."""
        prefix = (prefix or "").strip()
        if len(prefix) < 6 or not prefix.isalnum():  # deposit addresses are hex or base58: no LIKE wildcards
            return None
        rows = self._rows("SELECT * FROM transfers WHERE deposit = ? OR lower(deposit) LIKE ? ORDER BY created DESC LIMIT 2",
                          (prefix, prefix.lower() + "%"))
        exact = [r for r in rows if r["deposit"] == prefix]
        return exact[0] if exact else rows[0] if len(rows) == 1 else None

    def recent_transfers(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM transfers ORDER BY created DESC LIMIT ?", (int(limit),))

    # ---- keyless demo hires (demo.py) --------------------------------------------------------
    def demo_salt(self) -> str:
        """A random salt for hashing demo callers' addresses, made once per ledger and kept in it."""
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k = 'demo_salt'").fetchone()
            if row:
                return row[0]
            salt = secrets.token_hex(16)
            self._conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('demo_salt', ?)", (salt,))
            self._conn.commit()
            return self._conn.execute("SELECT v FROM meta WHERE k = 'demo_salt'").fetchone()[0]

    def reserve_demo(self, caller: str, service: str, per_caller: int, window: float, daily: int,
                     now: Optional[float] = None) -> str:
        """Check both demo caps and record the hire in one step, so parallel calls can't both slip under a
        cap. Raises DemoLimit. Every hire that starts counts, delivered or not (each one runs a job)."""
        now = time.time() if now is None else now
        day = utc_day_start(now)
        with self._lock:
            used = self._conn.execute("SELECT COUNT(*) FROM demo_hires WHERE created >= ?", (day,)).fetchone()[0]
            if used >= daily:
                raise DemoLimit("daily", day + 86400 - now)
            recent = [r[0] for r in self._conn.execute(
                "SELECT created FROM demo_hires WHERE caller = ? AND created >= ? ORDER BY created",
                (caller, now - window)).fetchall()]
            if len(recent) >= per_caller:
                raise DemoLimit("caller", recent[len(recent) - per_caller] + window - now)
            did = new_id("demo")
            self._conn.execute("INSERT INTO demo_hires(id, caller, service, status, created) VALUES(?,?,?,?,?)",
                               (did, caller, service, "running", now))
            self._conn.commit()
            return did

    def release_demo(self, did: str) -> None:
        """Forget a reserved demo hire that never started (the demo was busy or its wallet empty)."""
        self._exec("DELETE FROM demo_hires WHERE id = ? AND status = 'running'", (did,))

    def finish_demo(self, did: str, status: str, tx: str = "", error: str = "") -> None:
        self._exec("UPDATE demo_hires SET status = ?, tx = ?, error = ?, finished = ? WHERE id = ?",
                   (status, tx, error[:300], time.time(), did))

    def demo_used_today(self, now: Optional[float] = None) -> int:
        rows = self._rows("SELECT COUNT(*) AS n FROM demo_hires WHERE created >= ?", (utc_day_start(now),))
        return int(rows[0]["n"]) if rows else 0

    def purchase_summary(self) -> dict[str, Any]:
        rows = self._rows("SELECT price_usd, created FROM purchases WHERE status = ?", (BOUGHT,))
        return {"spent_total": sum((Decimal(r["price_usd"]) for r in rows), Decimal(0)),
                "spent_today": self.spent_today(), "bought": len(rows)}
