"""Emporium dashboard backend, mounted at /api/plugins/emporium/.

Reads the shop's settings and the ledger the gateway writes (the same SQLite file under this
profile's plugin-data). Never returns job inputs or outputs: there are none in the ledger.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()
_ROOT = Path(__file__).resolve().parent.parent
_PKG = "emporium_dashboard_lib"


def _lib():
    """The plugin's own shop/ledger/buyer modules, loaded once as a private package."""
    mod = sys.modules.get(_PKG)
    if mod is None:
        spec = importlib.util.spec_from_file_location(_PKG, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return (importlib.import_module(f"{_PKG}.shop"), importlib.import_module(f"{_PKG}.ledger"),
            importlib.import_module(f"{_PKG}.buyer"))


def _settings() -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly() or {}
    return (((cfg.get("plugins") or {}).get("entries") or {}).get("emporium") or {}).get("settings") or {}


def _money(shop_mod, d: Decimal) -> str:
    return shop_mod.usd(d) if d else "$0.00"


def _summary() -> dict[str, Any]:
    shop_mod, ledger_mod, buyer_mod = _lib()
    settings = _settings()
    get = lambda k, d=None: settings.get(k, d)  # noqa: E731
    shop = shop_mod.load_shop(get)
    books = ledger_mod.Ledger.open()
    s = books.sales_summary()
    policy = buyer_mod.Policy(get)
    per = {p["service"]: p for p in s["per_service"]}
    jobs = []
    for j in books.recent_jobs(30):
        jobs.append({"id": j["id"], "service": j["service"], "title": j["title"],
                     "price": shop_mod.usd(Decimal(j["price_usd"])), "status": j["status"],
                     "payer": j["payer"], "tx": j["tx"], "explorer": shop.explorer(j["tx"]),
                     "created": j["created"], "finished": j["finished"], "blocked": j["tools_blocked"],
                     "seconds": round(j["finished"] - j["started"], 1) if j["finished"] and j["started"] else None})
    buys = [{"id": b["id"], "shop": b["shop_name"] or b["shop_url"], "service": b["service"],
             "price": shop_mod.usd(Decimal(b["price_usd"])), "status": b["status"], "tx": b["tx"],
             "created": b["created"]} for b in books.recent_purchases(12)]
    spent = books.spent_today()
    return {
        "shop": {"name": shop.name, "tagline": shop.tagline, "url": shop.base_url(), "network": shop.network_label,
                 "testnet": shop.testnet, "pay_to": shop.pay_to, "open": shop.open and books.is_open(),
                 "ready": shop.ready, "problems": shop.problems},
        "sales": {"earned_total": _money(shop_mod, s["earned_total"]), "earned_today": _money(shop_mod, s["earned_today"]),
                  "earned_total_raw": str(s["earned_total"]), "jobs_settled": s["jobs_settled"], "jobs_today": s["jobs_today"],
                  "buyers": s["buyers"], "not_charged": s["counts"].get("failed", 0) + s["counts"].get("unpaid", 0),
                  "refused_tools": sum(j["blocked"] or 0 for j in jobs)},
        "menu": [{"id": sv.id, "title": sv.title, "price": sv.price_label, "tools": sv.tools,
                  "sold": per.get(sv.id, {}).get("jobs", 0),
                  "earned": _money(shop_mod, per.get(sv.id, {}).get("earned", Decimal(0)))} for sv in shop.services],
        "jobs": jobs,
        "buying": {"spent_today": _money(shop_mod, spent), "budget": shop_mod.usd(policy.daily_budget),
                   "spent_ratio": float(spent / policy.daily_budget) if policy.daily_budget else 0.0,
                   "max_per_job": shop_mod.usd(policy.max_per_job),
                   "approve_over": shop_mod.usd(policy.approve_over) if policy.approve_over else "never",
                   "purchases": buys},
    }


@router.get("/summary")
async def summary():
    return await asyncio.to_thread(_summary)


@router.post("/open")
async def set_open(body: dict):
    _, ledger_mod, _ = _lib()
    want = bool((body or {}).get("open", True))
    await asyncio.to_thread(lambda: ledger_mod.Ledger.open().set_open(want))
    return {"open": want}
