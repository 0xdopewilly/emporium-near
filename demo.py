"""The keyless demo: anyone (a person, a script, a judge's coding agent) hires this shop's own services
with no wallet, paid from a small NEAR testnet account the owner set aside for it.

    POST /demo/hire  {"service": "code-review", "input": "def add(a, b): return a - b"}

runs the shop's normal x402 path against its own /jobs endpoint, with the plugin's own buyer code: the
shop answers 402, the demo account signs a NEP-366 USDC transfer, the job runs sealed like any other,
and the shop's relayer submits the transfer once the result is ready. The answer is the deliverable and
the same receipt a paying buyer gets (``scripts/hire.py``), with a real NEAR testnet transaction.

Off unless the owner sets ``demo_buyer_account`` and ``EMPORIUM_DEMO_BUYER_KEY``; NEAR testnet only.
The caller names a service and its input, nothing else: never a payee, an amount, a network or another
shop. Only services on this shop's own menu up to ``demo_max_price``; inputs up to
``demo_max_input_chars``; ``demo_jobs_per_address`` hires per caller address every 10 minutes and
``demo_daily_jobs`` a day in all (UTC). The caps are kept in the shop's ledger, so a restart doesn't
reset them, and a caller's address is kept only as a salted hash.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Optional

from . import ledger as books
from .shop import USDC, Shop, from_atomic, parse_usd, usd

logger = logging.getLogger(__name__)

SECRET = "EMPORIUM_DEMO_BUYER_KEY"
PATH = "/demo/hire"
NETWORK = "near:testnet"   # the only network the demo runs on
WINDOW = 600               # seconds: the per-address cap counts hires in the last 10 minutes
IN_FLIGHT = 2              # demo hires at once (the demo key pays for one job at a time; one more may wait)
DEFAULTS = {"demo_max_price": "$0.50", "demo_daily_jobs": 30, "demo_jobs_per_address": 3, "demo_max_input_chars": 2000}
OFF = ("this shop has no keyless demo; hire it over x402 from your own NEAR account "
       "(the catalog at /.well-known/emporium.json has the services, prices and network)")
_FIELDS = {"service", "input"}
_SHARED = ipaddress.ip_network("100.64.0.0/10")


@dataclass
class Demo:
    account: str = ""
    key: Optional[str] = None
    max_price: Decimal = Decimal("0.50")
    daily: int = 30
    per_address: int = 3
    max_input_chars: int = 2000
    off: str = ""             # why it's off (for the owner's log); "" when it's on

    @property
    def enabled(self) -> bool:
        return not self.off


def load(get: Callable[[str, Any], Any], shop: Shop, secret: Callable[[str], Optional[str]]) -> Demo:
    """The demo's settings, and whether it may run. ``secret(name)`` reads the profile's secret scope."""
    from .near import KeyPair, is_account_id

    def _int(key: str, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(get(key, DEFAULTS[key]))))
        except (TypeError, ValueError):
            return int(DEFAULTS[key])

    d = Demo(account=str(get("demo_buyer_account", "") or "").strip(),
             max_price=parse_usd(get("demo_max_price", DEFAULTS["demo_max_price"])) or Decimal("0.50"),
             daily=_int("demo_daily_jobs", 0, 1000), per_address=_int("demo_jobs_per_address", 1, 100),
             max_input_chars=_int("demo_max_input_chars", 100, 20000))
    if not d.account:
        d.off = "demo_buyer_account isn't set"
        return d
    if shop.network != NETWORK:
        d.off = f"the keyless demo runs on {NETWORK} only, and this shop is paid on {shop.network}"
        return d
    if not is_account_id(d.account):
        d.off = "demo_buyer_account is not a NEAR account id"
        return d
    own = {shop.pay_to: "pay_to", shop.near_relayer: "near_relayer_account",
           str(get("buyer_near_account", "") or "").strip(): "buyer_near_account"}
    if d.account in own:
        d.off = f"demo_buyer_account must be an account of its own, not the shop's {own[d.account]}"
        return d
    try:
        d.key = secret(SECRET) or None
    except Exception:
        d.key = None
    if not d.key:
        d.off = f"set {SECRET} in this profile's .env: the full-access key of {d.account}"
        return d
    try:
        KeyPair(d.key)
    except Exception:
        d.key, d.off = None, f"{SECRET} is not a NEAR ed25519 secret key (ed25519:…)"
    return d


def caller_address(handler: Any) -> str:
    """Who is calling, for the per-address cap. Behind a proxy (the socket's peer is a private address, as
    on Railway) the proxy's X-Real-IP, else the last X-Forwarded-For hop, names the caller; a caller
    reaching the shop directly can't choose its address. IPv6 callers are counted per /64."""
    peer = str((getattr(handler, "client_address", None) or ("",))[0])
    addr = _ip(peer)
    if addr is not None and (addr.is_loopback or addr.is_private or addr in _SHARED):
        real = _ip(handler.headers.get("X-Real-IP") or "")
        hops = [h.strip() for h in (handler.headers.get("X-Forwarded-For") or "").split(",") if h.strip()]
        fwd = _ip(hops[-1]) if hops else None
        addr = real or fwd or addr
    if addr is None:
        return peer or "unknown"
    if isinstance(addr, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _ip(text: str) -> Optional[Any]:
    try:
        a = ipaddress.ip_address(text.strip().split("%", 1)[0])
    except ValueError:
        return None
    return a.ipv4_mapped if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped else a


def caller_hash(ledger: books.Ledger, address: str) -> str:
    return hashlib.sha256((ledger.demo_salt() + "|" + address).encode()).hexdigest()[:32]


def advert(demo: Demo, shop: Shop, ledger: books.Ledger) -> dict[str, Any]:
    """What the catalog says about the demo (only when it's on)."""
    return {"enabled": True, "endpoint": shop.base_url() + PATH, "method": "POST",
            "input": {"service": "a service id from this catalog", "input": f"up to {demo.max_input_chars} characters"},
            "payer": demo.account, "max_price": usd(demo.max_price),
            "daily_jobs": demo.daily, "daily_left": max(0, demo.daily - ledger.demo_used_today()),
            "per_address": f"{demo.per_address} per {WINDOW // 60} minutes",
            "network": NETWORK, "note": "keyless: this shop's own demo account pays, over the same x402 path a buyer uses"}


def _wait(seconds: float) -> str:
    s = int(seconds)
    return f"{s} s" if s < 120 else f"{s // 60} min" if s < 3600 else f"{s // 3600} h {s % 3600 // 60} min"


class Runner:
    """Runs demo hires for one adapter: the caps, the in-flight limit, the hire itself."""

    def __init__(self, demo: Demo):
        self.demo = demo
        self._slots = threading.BoundedSemaphore(IN_FLIGHT)

    def handle(self, adapter: Any, h: Any, body: Any) -> tuple[int, dict, dict]:
        """(status, JSON body, extra headers) for one POST /demo/hire."""
        demo, shop = self.demo, adapter.shop
        if not demo.enabled:
            return 403, {"error": OFF, "demo": False}, {}
        if not isinstance(body, dict) or not body.keys() <= _FIELDS:
            extra = sorted(set(body) - _FIELDS) if isinstance(body, dict) else []
            return 400, {"error": "send exactly {\"service\": \"<id>\", \"input\": \"<text>\"}"
                         + (f"; the demo takes nothing else (not {', '.join(map(str, extra))}): it pays this shop, at its "
                            "listed price, on NEAR testnet" if extra else ""), "charged": False}, {}
        sid, text = body.get("service"), body.get("input")
        svc = shop.service(sid) if isinstance(sid, str) else None
        if svc is None:
            return 404, {"error": f"this shop has no service {sid!r}", "services": [s.id for s in shop.services]}, {}
        if svc.price > demo.max_price:
            return 403, {"error": f"{svc.id} costs {svc.price_label}, above the demo's {usd(demo.max_price)} limit; "
                                  "hire it over x402 from your own NEAR account"}, {}
        if not isinstance(text, str) or not text.strip():
            return 400, {"error": "input is empty: pass the material the shop should work on", "charged": False}, {}
        cap = min(demo.max_input_chars, svc.max_input_chars)
        if len(text) > cap:
            return 400, {"error": f"input is {len(text)} characters; the demo takes up to {cap}", "charged": False}, {}
        if not (shop.open and adapter.books.is_open()):
            return 503, {"error": "the shop is closed right now", "charged": False}, {}

        address = caller_address(h)
        caller = caller_hash(adapter.books, address)
        try:
            did = adapter.books.reserve_demo(caller, svc.id, demo.per_address, WINDOW, demo.daily)
        except books.DemoLimit as e:
            retry = {"Retry-After": str(e.retry_after)}
            if e.kind == "daily":
                return 429, {"error": f"the demo's {demo.daily} jobs for today are used up; it resets at 00:00 UTC "
                                      f"(in {_wait(e.retry_after)}). To hire now, pay over x402 from your own NEAR "
                                      "testnet account", "retry_after": e.retry_after}, retry
            return 429, {"error": f"{demo.per_address} demo jobs per {WINDOW // 60} minutes from one address; "
                                  f"try again in {_wait(e.retry_after)}", "retry_after": e.retry_after}, retry
        if not self._slots.acquire(blocking=False):
            adapter.books.release_demo(did)
            return 503, {"error": "the demo is serving other callers; try again in a minute", "charged": False}, \
                {"Retry-After": "60"}
        try:
            short = self._underfunded(adapter, svc.price)
            if short:
                adapter.books.release_demo(did)
                return 503, {"error": short, "charged": False}, {}
            logger.info("Emporium demo: %s for caller %s", svc.id, caller[:8])
            return self._hire(adapter, did, svc, text)
        finally:
            self._slots.release()

    def _underfunded(self, adapter: Any, price: Decimal) -> str:
        try:
            held = from_atomic(adapter._near_rpc().view(USDC[NETWORK], "ft_balance_of", {"account_id": self.demo.account}))
        except Exception:
            return ""  # can't tell: the payment's own checks will say
        if held < price:
            logger.warning("Emporium demo: %s holds %s USDC, less than a %s job", self.demo.account, held, usd(price))
            return "the demo's wallet is out of testnet USDC right now; the owner tops it up. Hire over x402 from your own NEAR account meanwhile"
        return ""

    def _hire(self, adapter: Any, did: str, svc: Any, text: str) -> tuple[int, dict, dict]:
        from . import buyer
        shop, demo = adapter.shop, self.demo
        limits = {"buyer_max_per_job": usd(demo.max_price), "buyer_daily_budget": "$1000", "buyer_networks": [NETWORK]}
        policy = buyer.Policy(lambda k, d=None: limits.get(k, d))
        started = time.time()
        try:
            out = buyer.hire(adapter.self_url(), svc.id, text, policy=policy, key=None, ledger=None,
                             near_account=demo.account, near_key=demo.key,
                             near_rpc_urls={NETWORK: shop.rpc_url} if shop.rpc_url else None)
        except buyer.HireError as e:
            if "didn't answer in time" in str(e):
                adapter.books.finish_demo(did, "unknown", error=str(e))
                return 504, {"error": "the job didn't finish in time; nothing was delivered", "service": svc.id}, {}
            adapter.books.finish_demo(did, "not_delivered", error=str(e))
            return 502, {"error": f"the demo hire wasn't delivered, and nothing was paid: {e}", "charged": False,
                         "service": svc.id}, {}
        receipt = out["receipt"]
        adapter.books.finish_demo(did, "delivered", tx=str(receipt.get("tx") or ""))
        return 200, {"shop": out.get("shop"), "service": svc.id, "title": svc.title, "job": out.get("job", ""),
                     "output": out["output"], "receipt": receipt, "seconds": round(time.time() - started, 1),
                     "demo": {"payer": demo.account, "daily_left": max(0, demo.daily - adapter.books.demo_used_today())}}, {}
