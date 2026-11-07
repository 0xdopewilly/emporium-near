"""Buyer-side tools: emporium_browse and emporium_hire, plus the approval gate in front of hiring."""

from __future__ import annotations

from typing import Any, Optional

from decimal import Decimal

from . import buyer, guard
from .buyer import HireError, as_json
from .shop import usd

TOOLSET = "emporium"

BROWSE = {
    "name": "emporium_browse",
    "description": (
        "Look inside another agent's Emporium shop: the services it sells, the price of each, which network "
        "it is paid on and an example input, plus its track record checked on-chain (how many of its recent "
        "receipts are real USDC payments to it). Use this before emporium_hire to pick a service and decide "
        "whether to trust the shop."
    ),
    "parameters": {
        "type": "object",
        "properties": {"shop_url": {"type": "string", "description": "The shop's base URL, e.g. http://127.0.0.1:9402"}},
        "required": ["shop_url"],
    },
}

HIRE = {
    "name": "emporium_hire",
    "description": (
        "Hire another agent's Emporium shop to do one job and pay it in USDC over x402. Payment is only "
        "settled if the shop delivers; a failed job costs nothing. Returns the deliverable and an on-chain "
        "receipt. The owner's spending limits apply, and pricier jobs ask the owner first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "shop_url": {"type": "string", "description": "The shop's base URL"},
            "service": {"type": "string", "description": "Service id from emporium_browse"},
            "input": {"type": "string", "description": "The material for the job (text, code, a question)"},
            "max_price_usd": {"type": "number", "description": "Optional: the most to pay for this job, in USD"},
        },
        "required": ["shop_url", "service", "input"],
    },
}


def _ledger():
    try:
        from .ledger import Ledger
        return Ledger.open()
    except Exception:
        return None


def make_handlers(ctx: Any):
    def policy() -> buyer.Policy:
        return buyer.Policy(lambda k, d=None: ctx.get_config(k, default=d))

    def browse(args: dict, **_kw) -> str:
        try:
            return as_json(buyer.browse(str(args.get("shop_url", ""))))
        except HireError as e:
            return as_json({"error": str(e)})

    def hire(args: dict, session_id: str = "", task_id: str = "", **_kw) -> str:
        try:
            from agent.secret_scope import get_secret
            key = get_secret("EMPORIUM_BUYER_KEY")
            rules = guard.SESSIONS.job_for(session_id, task_id) if (session_id or task_id) else None
            if rules is None and guard.SESSIONS.is_shop_session(session_id, task_id):
                raise HireError("shop sessions can't hire")
            if rules is not None:
                # One of this Hermes's own shop jobs subcontracting: only to a shop the owner pinned on the
                # service, at most the pinned price, and all of the job's subcontracts within their budget.
                pin = rules.pin_for(args)
                if pin is None:
                    raise HireError("this job may only hire the shops its service lists")
                ledger = _ledger()
                if ledger is None:
                    raise HireError("the books aren't available, so nothing is spent")
                result = buyer.hire(pin.shop, pin.service, args.get("input", ""), policy=policy(), key=key,
                                    ledger=ledger, max_price=pin.max_price, for_job=rules.job_id,
                                    job_budget=sum((p.max_price * p.times for p in rules.pins), Decimal(0)))
                if ledger is not None:
                    ledger.add_sub(rules.job_id, Decimal(result.get("charged_usd") or "0"))
                return as_json(result)
            ledger = _ledger()
            if ledger is None:
                raise HireError("the books aren't available, so the daily budget can't be enforced; nothing was paid")
            result = buyer.hire(str(args.get("shop_url", "")), str(args.get("service", "")), args.get("input", ""),
                                policy=policy(), key=key, ledger=ledger, max_price=args.get("max_price_usd"))
            return as_json(result)
        except HireError as e:
            return as_json({"error": str(e), "charged": False})
        except Exception as e:  # never leak a traceback with headers in it
            return as_json({"error": f"hire failed: {e.__class__.__name__}", "charged": False})

    def gate(tool_name: str = "", args: Optional[dict] = None, session_id: str = "", task_id: str = "",
             **_kw) -> Optional[dict]:
        """pre_tool_call: block hires the policy forbids; send pricier ones to the owner's approval gate.
        Shop jobs are the shop guard's business (a pinned subcontract was approved by the owner's config)."""
        if tool_name != "emporium_hire" or guard.SESSIONS.is_shop_session(session_id, task_id):
            return None
        args = args or {}
        try:
            cat, svc, price = buyer.listed(str(args.get("shop_url", "")), str(args.get("service", "")))
            p = policy()
            p.check(price, str(cat.get("network") or ""), _ledger(), args.get("max_price_usd"))
        except HireError as e:
            return {"action": "block", "message": f"Not hiring: {e}. Nothing was paid."}
        except Exception as e:  # fail closed: money never moves on a check that couldn't run
            return {"action": "block", "message": f"Not hiring: couldn't check the shop ({e.__class__.__name__}). Nothing was paid."}
        if p.needs_approval(price):
            return {"action": "approve",
                    "message": f"Pay {usd(price)} USDC to {cat.get('name') or 'a shop'} for “{svc.get('title') or svc.get('id')}”?",
                    "rule_key": f"emporium:{buyer.norm_url(str(args.get('shop_url', '')))}"}
        return None

    return browse, hire, gate


def register_tools(ctx: Any) -> None:
    browse, hire, gate = make_handlers(ctx)
    ctx.register_tool(name="emporium_browse", toolset=TOOLSET, schema=BROWSE, handler=browse)
    ctx.register_tool(name="emporium_hire", toolset=TOOLSET, schema=HIRE, handler=hire)
    ctx.register_hook("pre_tool_call", gate)
