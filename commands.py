"""/shop in any chat, and `hermes emporium ...` in a terminal."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Callable

from . import ledger as books, meter
from .buyer import Policy
from .shop import STARTER_SERVICES, load_shop, money, usd

def risk_lines(profile: str, allow_unsealed: bool = False) -> list[str]:
    from .shop import SEAL_HINT, UNSEALED_SETTING, profile_risks
    risks = profile_risks(profile)
    if not risks:
        return []
    head = (f"⚠ Open on an unsealed profile ({UNSEALED_SETTING} is on):" if allow_unsealed
            else "⛔ The shop won't open on this profile:")
    return [head] + [f"  · {r}." for r in risks] + [f"  To seal it, {SEAL_HINT}."]

def seal_memory() -> str:
    """Turn built-in memory off in this profile's config.yaml (the shop's own profile only; init never
    calls it on the main profile). Returns what it turned off."""
    from hermes_cli.config import load_config, save_config
    cfg = load_config()
    mem = cfg.get("memory") if isinstance(cfg.get("memory"), dict) else {}
    changed = [k for k in ("memory_enabled", "user_profile_enabled") if mem.get(k) is not False]
    if changed:
        cfg["memory"] = {**mem, **{k: False for k in changed}}
        save_config(cfg)
    return " and ".join(f"memory.{k}" for k in changed)


_HELP = ("/shop — status, earnings and the menu\n"
         "/shop jobs — the last jobs sold\n"
         "/shop buys — the last jobs this Hermes bought, and today's budget\n"
         "/shop open | /shop close — start or stop taking new jobs\n"
         "Any chain, through NEAR Intents (mainnet):\n"
         "/shop quote <usd> from|to <chain:token> — what moving money across chains costs (a dry quote, no funds)\n"
         "/shop fund <usd> from <chain:token> refund <your address there> [send] — top up this Hermes's NEAR "
         "buyer account; the USDC stays yours until a shop delivers\n"
         "/shop cashout <usd> to <chain:token> <address> — withdraw earnings to any chain (asks before sending)\n"
         "/shop track <deposit address> — where a fund or cash-out stands · /shop transfers — the latest ones")


def _ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    return f"{s}s ago" if s < 60 else f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago" if s < 86400 else f"{s // 86400}d ago"


def _money(d: Decimal) -> str:
    return money(d)


def status_text(get: Callable[[str, Any], Any], profile: str) -> str:
    shop = load_shop(get, profile)
    try:
        ledger = books.Ledger.open()
    except Exception as e:
        return f"Emporium: couldn't open the ledger ({e})"
    meter.fill_costs(ledger)
    s = ledger.sales_summary()
    risks = risk_lines(shop.profile, shop.allow_unsealed) if not shop.problems else []
    is_open = shop.open and ledger.is_open()
    state = ("closed: this profile isn't sealed" if risks and not shop.allow_unsealed
             else "open for business" if is_open else "closed")
    lines = [f"🏛  {shop.name} — {state}"]
    if shop.problems:
        lines.append("Not ready yet: " + "; ".join(shop.problems))
        lines.append("Run `hermes emporium init --pay-to you.testnet --relayer you-relay.testnet` to set up a starter shop "
                     "(and put the relayer's key in EMPORIUM_NEAR_RELAYER_KEY).")
        return "\n".join(lines)
    lines.append(f"{shop.base_url()} · paid in USDC on {shop.network_label} to {shop.pay_to}")
    lines.extend(risks)
    lines.append(f"Earned {_money(s['earned_total'])} ({_money(s['earned_today'])} today) · "
                 f"{s['jobs_settled']} jobs delivered · {s['buyers']} buyers")
    if s["margin"] is not None:
        cost_txt = _money(s["inference_cost"])
        subs = f" and {_money(s['subcontract_cost'])} paid to other shops" if s["subcontract_cost"] else ""
        lines.append(f"Profit {_money(s['profit'])} after {cost_txt} of AI cost{subs} · {s['margin'] * 100:.1f}% margin")
    quality = []
    if s["delivery_rate"] is not None:
        quality.append(f"{s['delivery_rate'] * 100:.0f}% delivered")
    if s["p50_seconds"] is not None:
        quality.append(f"median {s['p50_seconds']}s, p95 {s['p95_seconds']}s")
    if s["tools_refused"]:
        quality.append(f"{s['tools_refused']} tool calls refused by the lock")
    if quality:
        lines.append(" · ".join(quality))
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
        if j["cost_usd"] not in ("", "-"):
            tx += f" · cost ${Decimal(j['cost_usd']):.4f}"
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


# ---- any chain, through NEAR Intents (intents.py) --------------------------------------------------
INTENTS_COMMANDS = ("quote", "fund", "cashout", "cash-out", "track", "transfers")


def _secret(name: str) -> str:
    from agent.secret_scope import get_secret
    return get_secret(name) or ""


def _intents_books():
    from . import intents
    try:
        return intents.Books(books.Ledger.open())
    except Exception:
        return intents.Books()


def _fee_line(q: Any) -> str:
    expected, worst = q.fee_bps()
    eta = f" · about {q.time_estimate}s once the deposit confirms" if q.time_estimate else ""
    if expected is None:
        return "fee: unknown (1Click has no USD price for one side)" + eta
    return f"fee ≈ {expected / 100}% expected, at most {worst / 100}% with the slippage buffer{eta}"


def _quote_lines(q: Any) -> list[str]:
    from .intents import fmt_amount
    o, d = q.origin, q.destination
    if q.kind == "fund":
        lines = [f"  in:  {fmt_amount(q.amount_in, o.decimals)} {o.label} (about {fmt_amount(q.min_amount_in, o.decimals)} "
                 "is used; the rest of the buffer comes back to the refund address)",
                 f"  out: exactly {fmt_amount(q.amount_out, d.decimals)} {d.label} to {q.request['recipient']}"]
    else:
        lines = [f"  in:  exactly {fmt_amount(q.amount_in, o.decimals)} {o.label} from {q.request['refundTo']}",
                 f"  out: about {fmt_amount(q.amount_out, d.decimals)} {d.label} (at least "
                 f"{fmt_amount(q.min_amount_out, d.decimals)}) to {q.request['recipient']}"]
    return lines + [f"  {_fee_line(q)}", "  ✓ signed by 1Click, and it is the quote that was asked for"]


def _mainnet_or(account: str, fallback: str) -> str:
    from .near import is_account_id
    account = (account or "").strip()
    return account if is_account_id(account) and not account.endswith(".testnet") else fallback


def quote_text(get: Callable[[str, Any], Any], words: list[str]) -> str:
    """A dry quote either way (no deposit address is made and nothing can be paid)."""
    from . import intents
    p = intents.parse_words(words)
    client = intents.OneClick(jwt=_secret(intents.JWT_SECRET))
    if p["direction"] == "from":
        q = intents.fund_quote(client, usd=p["usd"], origin=p["token"], dry=True, refund_to=p["refund"],
                               recipient=_mainnet_or(p["recipient"] or str(get("buyer_near_account", "") or ""),
                                                     intents.SAMPLE_ADDRESS["near"]),
                               **_limits(p))
    else:
        dst = client.token(p["token"])
        recipient = p["recipient"] or intents.SAMPLE_ADDRESS.get(intents.chain_family(dst.chain), "")
        q = intents.cashout_quote(client, usd=p["usd"], destination=p["token"], recipient=recipient, dry=True,
                                  shop=_mainnet_or(str(get("pay_to", "") or ""), intents.SAMPLE_ADDRESS["near"]),
                                  **_limits(p))
    return "\n".join(["NEAR Intents quote (dry: nothing to pay, no deposit address made):"] + _quote_lines(q))


def _limits(p: dict) -> dict:
    out: dict[str, Any] = {}
    if p.get("slippage") is not None:
        out["slippage_bps"] = p["slippage"]
    if p.get("max_fee") is not None:
        out["max_fee_bps"] = p["max_fee"]
    return out


def fund_text(get: Callable[[str, Any], Any], words: list[str], ctx: Any = None) -> str:
    """Top up this Hermes's own NEAR buyer account from another chain. Its USDC is spent only when a shop
    delivers a job (x402 settles after delivery), so funding never pays anyone up front."""
    from . import intents
    p = intents.parse_words(words)
    if p["direction"] != "from":
        return "Fund from another chain: /shop fund 5 from base:usdc refund <your Base address>"
    account = (p["recipient"] or str(get("buyer_near_account", "") or "")).strip()
    if not account:
        return ("Set buyer_near_account (this Hermes's NEAR mainnet account) first, or name one: "
                "/shop fund 5 from base:usdc refund 0x… to you.near")
    client = intents.OneClick(jwt=_secret(intents.JWT_SECRET))
    src = client.token(p["token"])
    key = _secret("EMPORIUM_BUYER_KEY") if intents.chain_family(src.chain) == "evm" else ""
    refund = p["refund"] or (intents.evm_address(key) if key else "")
    if p["send"] and not key:
        return "send pays the deposit from EMPORIUM_BUYER_KEY, an EVM wallet; it isn't set (or this isn't an EVM chain)."
    if p["send"] and refund.lower() != intents.evm_address(key):
        return "send pays from EMPORIUM_BUYER_KEY's wallet, so refunds go back there too: leave out refund, or pay by hand."
    q = intents.fund_quote(client, usd=p["usd"], origin=p["token"], recipient=account, refund_to=refund, **_limits(p))
    b = _intents_books()
    intents.record(b, q)
    o = q.origin
    lines = [f"Top up {account} with exactly {intents.fmt_amount(q.amount_out, 6)} USDC on NEAR, from {o.label}:"]
    lines += _quote_lines(q)[2:]
    if p["send"]:
        res = intents.evm_pay(client, q, key, books=b)
        state = "confirmed" if res["confirmed"] else "sent, not confirmed yet"
        lines.append(f"Paid the deposit from {refund} ({state}): tx {res['tx']}")
    else:
        memo = f" with memo {q.deposit_memo}" if q.deposit_memo else ""
        token = f" (token contract {o.contract})" if o.contract else ""
        lines += [f"Send {intents.fmt_amount(q.amount_in, o.decimals)} {o.symbol} on {intents.chain_label(o.chain)}{token}",
                  f"  to {q.deposit_address}{memo}",
                  f"  before {q.pay_by} (whatever isn't swapped by then goes back to {q.request['refundTo']})."]
    lines.append(f"Nothing is spent until a shop delivers. Follow it: /shop track {q.deposit_address}")
    return "\n".join(lines)


def _cashout_keys(get: Callable[[str, Any], Any], profile: str) -> tuple[Any, Any, Any]:
    from .intents import IntentsError, NETWORK
    from .near import KeyPair
    shop = load_shop(get, profile)
    if shop.network != NETWORK:
        raise IntentsError(f"cash-outs run on NEAR mainnet (NEAR Intents has no testnet); this shop is on {shop.network_label}")
    account_problems = [x for x in shop.problems if x.startswith(("pay_to", "near_relayer_account"))]
    if account_problems:
        raise IntentsError("; ".join(account_problems))
    try:
        payout = KeyPair(_secret("EMPORIUM_NEAR_PAYOUT_KEY"))
        relayer = KeyPair(_secret("EMPORIUM_NEAR_RELAYER_KEY"))
    except ValueError as e:
        raise IntentsError("a cash-out needs EMPORIUM_NEAR_PAYOUT_KEY (pay_to's full-access key, which signs the "
                           "transfer) and EMPORIUM_NEAR_RELAYER_KEY (which pays the gas) in this profile's .env") from e
    return shop, payout, relayer


def cashout_text(get: Callable[[str, Any], Any], words: list[str], profile: str = "", ctx: Any = None) -> str:
    """Withdraw shop earnings from USDC on NEAR to any chain. Shows the verified quote and asks before
    anything moves (in chat with a choice, or with /shop cashout confirm <deposit>)."""
    from . import intents
    client = intents.OneClick(jwt=_secret(intents.JWT_SECRET))
    b = _intents_books()
    if words and words[0].lower() == "confirm":
        if len(words) < 2 or b.ledger is None:
            return "Confirm a quoted cash-out by its deposit address: /shop cashout confirm <deposit>"
        row = b.ledger.find_transfer(words[1])
        if row is None or row["kind"] != "cashout":
            return f"No cash-out quote starts with {words[1]!r}; /shop transfers lists them."
        if row["status"] == "cancelled":
            return "That cash-out was cancelled; ask for a new quote."
        q = intents.quote_from_record(row, client)
        return _send_cashout(get, profile, client, q, b, ctx)
    p = intents.parse_words(words)
    if p["direction"] != "to" or not p["recipient"]:
        return "Cash out to another chain: /shop cashout 5 to base:usdc <your Base address>"
    shop, _payout, _relayer = _cashout_keys(get, profile)
    dst = client.token(p["token"])
    min_out = intents.usd_atomic(p["min"], dst.decimals) if p["min"] else 0
    q = intents.cashout_quote(client, usd=p["usd"], destination=p["token"], recipient=p["recipient"], shop=shop.pay_to,
                              min_out=min_out, **_limits(p))
    intents.record(b, q)
    preview = [f"Cash out {intents.fmt_amount(q.amount_in, 6)} USDC from {shop.pay_to} to {q.destination.label}:"]
    preview += _quote_lines(q)
    preview.append(f"  deposit address on NEAR: {q.deposit_address} (the relayer {shop.near_relayer} registers it "
                   "and pays the gas)")
    ask = getattr(ctx, "ask_choice", None)
    choice = None
    if callable(ask):
        try:
            choice = ask("\n".join(preview + ["Send it?"]),
                         [{"value": "send", "label": f"Send {intents.fmt_amount(q.amount_in, 6)} USDC"},
                          {"value": "cancel", "label": "Cancel"}])
        except Exception:
            choice = None
    if choice == "send":
        return _send_cashout(get, profile, client, q, b, ctx)
    if choice == "cancel":
        b.update(q.deposit_address, status="cancelled")
        return "Cancelled: nothing was sent."
    return "\n".join(preview + [f"Nothing has moved. To send it: /shop cashout confirm {q.deposit_address[:12]} "
                                 f"(before {q.pay_by})"])


def _send_cashout(get: Callable[[str, Any], Any], profile: str, client: Any, q: Any, b: Any, ctx: Any) -> str:
    from . import intents
    shop, payout, relayer = _cashout_keys(get, profile)
    handle = None
    post = getattr(ctx, "post_message", None)
    if callable(post):
        try:
            handle = post(f"Cashing out {intents.fmt_amount(q.amount_in, 6)} USDC to {q.destination.label}…")
        except Exception:
            handle = None

    def progress(state: str) -> None:
        if handle is not None and hasattr(handle, "edit"):
            try:
                handle.edit(f"Cash-out {q.deposit_address[:12]}…: {state}")
            except Exception:
                pass

    out = intents.cashout_send(client, q, shop=shop.pay_to, shop_key=payout, relayer=shop.near_relayer,
                               relayer_key=relayer, books=b, rpc_url=shop.rpc_url, wait=45, poll=3,
                               on_status=progress)
    lines = [f"Sent {out['amount']} USDC from {shop.pay_to} to 1Click (NEAR tx {out['near_tx']}; the relayer paid the gas).",
             f"Status: {out['status']}"]
    for t in out.get("destination_txs") or []:
        lines.append(f"  paid out on {q.destination.label}: {t.get('explorer') or t.get('hash')}")
    if out["status"] not in intents.TERMINAL:
        lines.append(f"Still going; follow it: /shop track {q.deposit_address}")
    return "\n".join(lines)


def track_text(words: list[str]) -> str:
    from . import intents
    if not words:
        return "Which one? /shop track <deposit address> (or the start of one from /shop transfers)"
    b = _intents_books()
    row = b.ledger.find_transfer(words[0]) if b.ledger is not None else None
    deposit = row["deposit"] if row else words[0]
    info = intents.track(intents.OneClick(jwt=_secret(intents.JWT_SECRET)), deposit, books=b if row else None)
    lines = [f"{deposit}: {info['status']}"]
    if row:
        lines[0] += f" · {row['kind']} of ${row['usd']}"
    if info.get("quote_signed") is False:
        lines.append("  ⚠ 1Click's signature on this quote doesn't verify")
    for t in info["destination_txs"]:
        lines.append(f"  paid out: {t.get('explorer') or t.get('hash')}")
    if info["status"] == "REFUNDED":
        lines.append(f"  refunded {info.get('refunded') or ''} ({info.get('refund_reason') or 'no reason given'})")
    return "\n".join(lines)


def transfers_text(limit: int = 10) -> str:
    rows = books.Ledger.open().recent_transfers(limit)
    if not rows:
        return "No funds or cash-outs yet."
    out = ["Across chains (NEAR Intents):"]
    for r in rows:
        out.append(f"  {r['kind']:<7} ${r['usd']:>8}  {r['status']:<15} {r['deposit'][:14]}… · {_ago(r['created'])}")
    return "\n".join(out)


def intents_text(get: Callable[[str, Any], Any], raw_args: str, profile: str = "", ctx: Any = None) -> str:
    """The cross-chain subcommands. Arguments keep their case: Solana and other addresses are case-sensitive."""
    words = (raw_args or "").split()
    sub, rest = words[0].lower(), words[1:]
    if sub == "quote":
        return quote_text(get, rest)
    if sub == "fund":
        return fund_text(get, rest, ctx)
    if sub in ("cashout", "cash-out"):
        return cashout_text(get, rest, profile, ctx)
    if sub == "track":
        return track_text(rest)
    return transfers_text()


def make_slash(ctx: Any) -> Callable[[str], str]:
    get = lambda k, d=None: ctx.get_config(k, default=d)  # noqa: E731

    def shop_cmd(raw_args: str = "") -> str:
        arg = (raw_args or "").strip().lower()
        try:
            if arg.split(" ", 1)[0] in INTENTS_COMMANDS:
                return intents_text(get, raw_args, getattr(ctx, "profile_name", ""), ctx)
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
        init.add_argument("--pay-to", required=False, help="NEAR account (you.testnet) or 0x address that receives your USDC")
        init.add_argument("--name", required=False, help="Shop name")
        init.add_argument("--network", default=None, help="near:testnet (default), near:mainnet, eip155:84532 (Base Sepolia) or eip155:8453 (Base)")
        init.add_argument("--relayer", required=False, help="NEAR only: the account that submits buyers' payments and pays the gas "
                          "(its key goes in EMPORIUM_NEAR_RELAYER_KEY)")
        init.add_argument("--port", type=int, default=None, help="Port for the shop (default 9402)")
        cmds.add_parser("status", help="Status, earnings and the menu")
        cmds.add_parser("jobs", help="Recent jobs sold")
        cmds.add_parser("buys", help="Recent jobs bought and today's budget")
        cmds.add_parser("open", help="Start taking jobs")
        cmds.add_parser("close", help="Stop taking new jobs")
        for name, text in (("quote", "A dry NEAR Intents quote: 5 from base:usdc, or 5 to base:usdc"),
                           ("fund", "Top up the NEAR buyer account from any chain: 5 from base:usdc refund 0x…"),
                           ("cashout", "Withdraw earnings to any chain: 5 to base:usdc 0x…"),
                           ("track", "Where a fund or cash-out stands: <deposit address>"),
                           ("transfers", "Recent funds and cash-outs")):
            cmds.add_parser(name, help=text).add_argument("words", nargs="*")
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
            if getattr(args, "relayer", None):
                ctx.set_config("near_relayer_account", args.relayer.strip())
            if args.port:
                ctx.set_config("port", int(args.port))
            if not get("services", None):
                ctx.set_config("services", STARTER_SERVICES)
            profile = getattr(ctx, "profile_name", "")
            if profile in ("", "default"):
                print("This is your main profile: the shop won't open here, because buyers' jobs would load its memory "
                      "and persona. Give the shop a profile of its own: hermes profile create shop, then "
                      "hermes -p shop emporium init.\n")
            else:
                sealed = seal_memory()
                if sealed:
                    print(f"Sealed this profile for the shop: turned off {sealed}, so buyers' jobs carry no memory "
                          "and none of their text is saved.\n")
            shop = load_shop(get, profile)
            print(status_text(get, getattr(ctx, "profile_name", "")))
            if shop.ready:
                print("\nNext: run `hermes gateway` in this profile. The shop opens at " + shop.base_url())
            return
        if cmd in INTENTS_COMMANDS:
            try:
                print(intents_text(get, " ".join([cmd, *getattr(args, "words", [])]), getattr(ctx, "profile_name", ""), ctx))
            except Exception as e:
                print(f"Emporium: {e}")
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
