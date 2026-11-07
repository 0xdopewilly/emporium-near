"""Buyer-side tools: emporium_browse and emporium_hire, plus the approval gate in front of hiring."""

from __future__ import annotations

from typing import Any, Optional

from . import buyer
from .buyer import HireError, as_json
from .shop import usd

TOOLSET = "emporium"

BROWSE = {
    "name": "emporium_browse",
    "description": (
        "Look inside another agent's Emporium shop: the services it sells, the price of each, which network "
        "it is paid on and an example input. Use this before emporium_hire to pick a service."
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

    def hire(args: dict, **_kw) -> str:
        try:
            from agent.secret_scope import get_secret
            key = get_secret("EMPORIUM_BUYER_KEY")
            result = buyer.hire(str(args.get("shop_url", "")), str(args.get("service", "")), args.get("input", ""),
                                policy=policy(), key=key, ledger=_ledger(), max_price=args.get("max_price_usd"))
            return as_json(result)
        except HireError as e:
            return as_json({"error": str(e), "charged": False})
        except Exception as e:  # never leak a traceback with headers in it
            return as_json({"error": f"hire failed: {e.__class__.__name__}", "charged": False})

    def gate(tool_name: str = "", args: Optional[dict] = None, **_kw) -> Optional[dict]:
        """pre_tool_call: block hires the policy forbids; send pricier ones to the owner's approval gate."""
        if tool_name != "emporium_hire":
            return None
        args = args or {}
        try:
            cat, svc, price = buyer.listed(str(args.get("shop_url", "")), str(args.get("service", "")))
            p = policy()
            p.check(price, str(cat.get("network") or ""), _ledger(), args.get("max_price_usd"))
        except HireError as e:
            return {"action": "block", "message": f"Not hiring: {e}. Nothing was paid."}
        except Exception:
            return None  # let the tool itself report the problem
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
