"""Hiring another agent's shop: browse its menu, then pay for a job over x402, only on delivery.

Money rules, all checked before anything is signed:
- a per-job ceiling (the tool's max_price_usd and the profile's buyer_max_per_job, whichever is lower);
- a daily budget across all hires (buyer_daily_budget), counting jobs still in flight;
- only networks the owner allowed (testnet by default);
- the 402 must ask for the catalog's price, in USDC, to the shop's published wallet;
- above buyer_approve_over, Hermes's own approval gate asks the owner first (see __init__.py).
The wallet key comes from Hermes's secret scope (EMPORIUM_BUYER_KEY) and never leaves this process.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

from . import ledger as books
from .shop import NETWORKS, from_atomic, parse_usd, to_atomic, usd

DEFAULTS = {"buyer_max_per_job": "$0.50", "buyer_daily_budget": "$2.00", "buyer_approve_over": "$0.10",
            "buyer_networks": ["eip155:84532"]}
_CATALOG_TTL = 60.0
_catalog_cache: dict[str, tuple[float, dict]] = {}


class HireError(Exception):
    """A refusal with a reason the agent can act on (nothing was charged)."""


def norm_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise HireError("shop_url is required, e.g. http://127.0.0.1:9402")
    if "://" not in url:
        url = "http://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise HireError(f"{url!r} is not an http(s) URL")
    return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"


def catalog(shop_url: str, fresh: bool = False) -> dict:
    url = norm_url(shop_url)
    hit = _catalog_cache.get(url)
    if hit and not fresh and time.time() - hit[0] < _CATALOG_TTL:
        return hit[1]
    try:
        r = httpx.get(url + "/.well-known/emporium.json", timeout=15, follow_redirects=False)
    except httpx.HTTPError as e:
        raise HireError(f"couldn't reach the shop at {url}: {e.__class__.__name__}") from e
    if r.status_code != 200:
        raise HireError(f"{url} has no Emporium catalog (HTTP {r.status_code})")
    try:
        cat = r.json()
    except ValueError as e:
        raise HireError(f"{url} returned a catalog that isn't JSON") from e
    if not isinstance(cat, dict) or not isinstance(cat.get("services"), list):
        raise HireError(f"{url} returned an unexpected catalog")
    _catalog_cache[url] = (time.time(), cat)
    return cat


def listed(shop_url: str, service_id: str) -> tuple[dict, dict, Decimal]:
    cat = catalog(shop_url)
    svc = next((s for s in cat["services"] if s.get("id") == service_id), None)
    if svc is None:
        raise HireError(f"{cat.get('name', 'the shop')} has no service {service_id!r}; it sells: "
                        + ", ".join(s.get("id", "?") for s in cat["services"]))
    price = parse_usd(svc.get("price_usd") or svc.get("price"))
    if price is None:
        raise HireError("the shop lists no valid price for that service")
    return cat, svc, price


class Policy:
    def __init__(self, get: Callable[[str, Any], Any]):
        self.max_per_job = parse_usd(get("buyer_max_per_job", DEFAULTS["buyer_max_per_job"])) or Decimal("0.50")
        self.daily_budget = parse_usd(get("buyer_daily_budget", DEFAULTS["buyer_daily_budget"])) or Decimal("2.00")
        self.approve_over = parse_usd(get("buyer_approve_over", DEFAULTS["buyer_approve_over"]))  # None = never ask
        nets = get("buyer_networks", DEFAULTS["buyer_networks"]) or DEFAULTS["buyer_networks"]
        self.networks = [n for n in (nets if isinstance(nets, list) else [nets]) if n in NETWORKS]

    def ceiling(self, max_price: Any) -> Decimal:
        asked = parse_usd(max_price) if max_price not in (None, "") else None
        return min(self.max_per_job, asked) if asked is not None else self.max_per_job

    def check(self, price: Decimal, network: str, ledger: Optional[books.Ledger], max_price: Any = None) -> None:
        if network not in self.networks:
            raise HireError(f"this profile only pays on {', '.join(self.networks) or 'no network'}; the shop wants {network}")
        cap = self.ceiling(max_price)
        if price > cap:
            raise HireError(f"the job costs {usd(price)}, above the {usd(cap)} per-job limit")
        if ledger is not None:
            left = self.daily_budget - ledger.spent_today()
            if price > left:
                raise HireError(f"the job costs {usd(price)} but only {usd(max(left, Decimal(0)))} of today's "
                                f"{usd(self.daily_budget)} budget is left")

    def needs_approval(self, price: Decimal) -> bool:
        return self.approve_over is not None and price > self.approve_over


def _signer(key: str):
    from eth_account import Account
    from x402.mechanisms.evm.signers import EthAccountSigner
    try:
        return EthAccountSigner(Account.from_key(key.strip()))
    except Exception as e:
        raise HireError("EMPORIUM_BUYER_KEY is not a valid EVM private key") from e


def hire(shop_url: str, service_id: str, buyer_input: str, *, policy: Policy, key: Optional[str],
         ledger: Optional[books.Ledger], max_price: Any = None) -> dict:
    """Run one paid job end to end. Returns {output, receipt, ...}; raises HireError (never charged)."""
    from x402 import x402ClientSync
    from x402.http.x402_http_client import x402HTTPClientSync
    from x402.mechanisms.evm.exact import register_exact_evm_client

    url = norm_url(shop_url)
    if not isinstance(buyer_input, str) or not buyer_input.strip():
        raise HireError("input is empty: pass the material the shop should work on")
    cat, svc, price = listed(url, service_id)
    network = str(cat.get("network") or "")
    pay_to = str(cat.get("pay_to") or "").lower()
    policy.check(price, network, ledger, max_price)
    if not key:
        raise HireError("no buyer wallet: set EMPORIUM_BUYER_KEY in this profile's .env (a dedicated wallet "
                        "holding a little USDC)")
    signer = _signer(key)
    endpoint = f"{url}/jobs/{service_id}"
    body = {"input": buyer_input}
    timeout = float(svc.get("timeout_seconds") or 180) + 60

    with httpx.Client(timeout=timeout, follow_redirects=False) as http:
        first = http.post(endpoint, json=body)
        if first.status_code != 402:
            try:
                err = first.json().get("error")
            except ValueError:
                err = first.text[:200]
            raise HireError(f"the shop refused before payment (HTTP {first.status_code}): {err}")
        client = x402ClientSync()
        register_exact_evm_client(client, signer, policy.networks)
        hc = x402HTTPClientSync(client)
        try:
            required = hc.get_payment_required_response(lambda n: first.headers.get(n), first.json())
        except Exception as e:
            raise HireError(f"the shop's 402 is not valid x402: {e}") from e
        cap_atomic = to_atomic(min(price, policy.ceiling(max_price)))
        ok = [r for r in required.accepts
              if r.scheme == "exact" and r.network in policy.networks and str(r.pay_to).lower() == pay_to
              and int(r.amount) <= cap_atomic]
        if not ok:
            asked = ", ".join(f"{usd(from_atomic(r.amount))} on {r.network} to {r.pay_to}" for r in required.accepts)
            raise HireError(f"the shop's payment request ({asked}) doesn't match its listed {usd(price)} to {pay_to}; not paying")
        required.accepts = ok[:1]
        charged = from_atomic(ok[0].amount)
        pid = ledger.create_purchase(url, str(cat.get("name") or ""), service_id, charged, network, pay_to) if ledger else ""
        try:
            payload = hc.create_payment_payload(required)
            headers = hc.encode_payment_signature_header(payload)
            started = time.time()
            paid = http.post(endpoint, json=body, headers=headers)
        except Exception as e:
            if ledger and pid:
                ledger.update_purchase(pid, status=books.NOT_CHARGED, error=str(e)[:300], finished=time.time())
            raise HireError(f"the paid request failed before delivery: {e.__class__.__name__}: {e}") from e
        try:
            data = paid.json()
        except ValueError:
            data = {"error": paid.text[:300]}
        if paid.status_code != 200 or not isinstance(data, dict) or "output" not in data:
            if ledger and pid:
                ledger.update_purchase(pid, status=books.NOT_CHARGED, error=str(data.get("error"))[:300], finished=time.time())
            raise HireError(f"no delivery (HTTP {paid.status_code}): {data.get('error')}; you were not charged")
        tx = ""
        try:
            settle = hc.get_payment_settle_response(lambda n: paid.headers.get(n))
            tx = getattr(settle, "transaction", "") or ""
        except Exception:
            tx = str((data.get("receipt") or {}).get("tx") or "")
        if ledger and pid:
            ledger.update_purchase(pid, status=books.BOUGHT, tx=tx, output_chars=len(str(data.get("output", ""))),
                                   finished=time.time())
        receipt = dict(data.get("receipt") or {})
        receipt.update({"tx": tx or receipt.get("tx", ""), "paid": usd(charged), "seconds": round(time.time() - started, 1)})
        explorer = NETWORKS.get(network, ("", "", False))[1]
        if receipt.get("tx") and explorer:
            receipt["explorer"] = explorer + receipt["tx"]
        return {"shop": cat.get("name"), "service": svc.get("title") or service_id, "output": data["output"],
                "receipt": receipt}


def browse(shop_url: str) -> dict:
    cat = catalog(shop_url, fresh=True)
    return {
        "shop": cat.get("name"), "tagline": cat.get("tagline"), "open": cat.get("open", True),
        "network": cat.get("network_label") or cat.get("network"), "pays_to": cat.get("pay_to"),
        "charged": cat.get("charged"),
        "services": [{"id": s.get("id"), "title": s.get("title"), "price": s.get("price"),
                      "description": s.get("description"), "example": s.get("example")} for s in cat["services"]],
    }


def as_json(obj: Any) -> str:
    return json.dumps(obj, indent=1, default=str, ensure_ascii=False)
