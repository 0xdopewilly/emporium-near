"""/shop in any chat, and `hermes emporium ...` in a terminal."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Callable

from . import ledger as books
from .buyer import Policy
from .shop import STARTER_SERVICES, load_shop, usd

_HELP = ("/shop — status, earnings and the menu\n"
         "/shop jobs — the last jobs sold\n"
         "/shop buys — the last jobs this Hermes bought, and today's budget\n"
         "/shop open | /shop close — start or stop taking new jobs")


def _ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    return f"{s}s ago" if s < 60 else f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago" if s < 86400 else f"{s // 86400}d ago"


def _money(d: Decimal) -> str:
    return usd(d) if d else "$0.00"


def status_text(get: Callable[[str, Any], Any], profile: str) -> str:
    shop = load_shop(get, profile)
    try:
        ledger = books.Ledger.open()
    except Exception as e:
        return f"Emporium: couldn't open the ledger ({e})"
    s = ledger.sales_summary()
    is_open = shop.open and ledger.is_open()
    lines = [f"🏛  {shop.name} — {'open for business' if is_open else 'closed'}"]
    if shop.problems:
        lines.append("Not ready yet: " + "; ".join(shop.problems))
        lines.append("Run `hermes emporium init --pay-to 0xYourWallet` to set up a starter shop.")
        return "\n".join(lines)
    lines.append(f"{shop.base_url()} · paid in USDC on {shop.network_label} to {shop.pay_to}")
    lines.append(f"Earned {_money(s['earned_total'])} ({_money(s['earned_today'])} today) · "
                 f"{s['jobs_settled']} jobs delivered · {s['buyers']} buyers")
    lines.append("")
    lines.append("Menu:")
    per = {p["service"]: p for p in s["per_service"]}
    for svc in shop.services:
        p = per.get(svc.id)
        sold = f" · sold {p['jobs']} ({_money(p['earned'])})" if p else ""
        tools = f" · tools: {', '.join(svc.tools)}" if svc.tools else ""
        lines.append(f"  {svc.price_label:>7}  {svc.title} [{svc.id}]{tools}{sold}")
    failed = s["counts"].get(books.FAILED, 0) + s["counts"].get(books.UNPAID, 0)
    if failed:
        lines.append(f"\n{failed} job(s) not charged (failed, timed out or unsettled).")
    return "\n".join(lines)


def jobs_text(limit: int = 10) -> str:
    ledger = books.Ledger.open()
    rows = ledger.recent_jobs(limit)
    if not rows:
        return "No jobs yet."
    out = ["Recent jobs:"]
    for j in rows:
        tx = f" · tx {j['tx'][:12]}…" if j["tx"] else ""
        blocked = f" · {j['tools_blocked']} tool call(s) refused" if j["tools_blocked"] else ""
        out.append(f"  {j['id']}  {usd(Decimal(j['price_usd'])):>7}  {j['status']:<8} {j['title']} · "
                   f"{(j['payer'] or 'anon')[:10]}… · {_ago(j['created'])}{tx}{blocked}")
    return "\n".join(out)


def buys_text(get: Callable[[str, Any], Any]) -> str:
    ledger = books.Ledger.open()
    p = Policy(get)
    spent = ledger.spent_today()
    rows = ledger.recent_purchases(10)
    out = [f"Buying: {usd(spent) if spent else '$0.00'} of {usd(p.daily_budget)} spent today · "
           f"max {usd(p.max_per_job)} per job · asks you above {usd(p.approve_over) if p.approve_over else 'never'}"]
    for b in rows:
        tx = f" · tx {b['tx'][:12]}…" if b["tx"] else ""
        out.append(f"  {usd(Decimal(b['price_usd'])):>7}  {b['status']:<11} {b['service']} @ {b['shop_name'] or b['shop_url']} · "
                   f"{_ago(b['created'])}{tx}")
    if not rows:
        out.append("  Nothing bought yet.")
    return "\n".join(out)


def make_slash(ctx: Any) -> Callable[[str], str]:
    get = lambda k, d=None: ctx.get_config(k, default=d)  # noqa: E731

    def shop_cmd(raw_args: str = "") -> str:
        arg = (raw_args or "").strip().lower()
        try:
            if arg in ("", "status"):
                return status_text(get, getattr(ctx, "profile_name", ""))
            if arg == "jobs":
                return jobs_text()
            if arg in ("buys", "bought", "purchases"):
                return buys_text(get)
            if arg in ("open", "close", "closed"):
                books.Ledger.open().set_open(arg == "open")
                return "The shop is open for business." if arg == "open" else "The shop is closed: new jobs get a 503 and nobody is charged."
            return _HELP
        except Exception as e:
            return f"Emporium: {e}"

    return shop_cmd


def make_cli(ctx: Any):
    get = lambda k, d=None: ctx.get_config(k, default=d)  # noqa: E731

    def setup(sub):
        cmds = sub.add_subparsers(dest="emporium_command")
        init = cmds.add_parser("init", help="Set up a starter shop (3 services) for this profile")
        init.add_argument("--pay-to", required=False, help="Wallet address (0x…) that receives your USDC")
        init.add_argument("--name", required=False, help="Shop name")
        init.add_argument("--network", default=None, help="eip155:84532 (Base Sepolia, default) or eip155:8453 (Base)")
        init.add_argument("--port", type=int, default=None, help="Port for the shop (default 9402)")
        cmds.add_parser("status", help="Status, earnings and the menu")
        cmds.add_parser("jobs", help="Recent jobs sold")
        cmds.add_parser("buys", help="Recent jobs bought and today's budget")
        cmds.add_parser("open", help="Start taking jobs")
        cmds.add_parser("close", help="Stop taking new jobs")
        sub.set_defaults(func=handle)

    def handle(args):
        cmd = getattr(args, "emporium_command", None) or "status"
        if cmd == "init":
            if args.pay_to:
                ctx.set_config("pay_to", args.pay_to.strip())
            if args.name:
                ctx.set_config("shop_name", args.name.strip())
            if args.network:
                ctx.set_config("network", args.network.strip())
            if args.port:
                ctx.set_config("port", int(args.port))
            if not get("services", None):
                ctx.set_config("services", STARTER_SERVICES)
            shop = load_shop(get, getattr(ctx, "profile_name", ""))
            print(status_text(get, getattr(ctx, "profile_name", "")))
            if shop.ready:
                print("\nNext: run `hermes gateway` in this profile. The shop opens at " + shop.base_url())
            return
        if cmd == "jobs":
            print(jobs_text(20))
        elif cmd == "buys":
            print(buys_text(get))
        elif cmd in ("open", "close"):
            books.Ledger.open().set_open(cmd == "open")
            print("Open for business." if cmd == "open" else "Closed to new jobs.")
        else:
            print(status_text(get, getattr(ctx, "profile_name", "")))

    return setup, handle
