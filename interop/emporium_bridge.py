"""Emporium's side of the interop check: its x402 ``exact``-on-NEAR client and facilitator, driven over JSON
lines on stdin/stdout by run.mjs (which drives @x402/near on the other side).

Everything crosses as x402 v2 wire JSON (camelCase ``PaymentRequired`` / ``PaymentPayload`` /
``PaymentRequirements``), parsed by the x402 Python SDK exactly as a shop parses a PAYMENT-SIGNATURE header,
and run through ``x402ClientSync`` / ``x402FacilitatorSync`` with Emporium's NEAR scheme registered, which is
how the shop runs them. Requests carry an ``id`` and are answered in any order (several run at once).

Keys never cross the pipe in online mode: this side reads them from the wallet file itself. In offline mode
it starts tests/fake_near.py's in-memory chain and hands run.mjs the throwaway keys it made for it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

if "emporium" not in sys.modules:  # the plugin directory, as the package Hermes loads it as
    _spec = importlib.util.spec_from_file_location("emporium", ROOT / "__init__.py",
                                                   submodule_search_locations=[str(ROOT)])
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["emporium"] = _mod
    _spec.loader.exec_module(_mod)

from emporium import near, near_x402  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from x402 import x402ClientSync, x402FacilitatorSync  # noqa: E402
from x402.schemas import PaymentPayload, PaymentRequired, PaymentRequirements  # noqa: E402

NETWORKS = ["near:testnet", "near:mainnet"]
BLACKHOLE = "http://127.0.0.1:9"  # mainnet is never contacted: its RPC goes nowhere

STATE: dict[str, Any] = {}
OUT_LOCK = threading.Lock()


def dump(model: Any) -> dict:
    return model.model_dump(by_alias=True, exclude_none=True, mode="json")


def seeded(text: str) -> near.KeyPair:
    return near.KeyPair("ed25519:" + near.b58encode(hashlib.sha256(text.encode()).digest()))


# ---- set-up ------------------------------------------------------------------------------------
def setup(req: dict) -> dict:
    relayer = req["relayer"]
    if req.get("offline"):
        sys.path.insert(0, str(ROOT / "tests"))
        from fake_near import FakeNear
        chain = FakeNear().start()
        keys: dict[str, near.KeyPair] = {}
        for account in [relayer] + req["buyers"] + req["pay_to"]:
            if account in keys:
                continue
            keys[account] = seeded("interop-offline:" + account)
            if account == relayer:
                chain.add_account(account, keys[account], near_amount=5, register=False)
            elif account in req["buyers"]:
                chain.add_account(account, keys[account], usdc=5_000_000)
            else:
                chain.add_account(account, keys[account])
        STATE.update(chain=chain, keys=keys, rpc_url=chain.url)
        secrets = {a: k.secret() for a, k in keys.items()}  # throwaway keys for an in-memory chain
    else:
        wallets = json.loads(Path(req["wallets"]).read_text())
        keys = {a: near.KeyPair(v["secret"]) for a, v in wallets.items() if isinstance(v, dict) and "secret" in v}
        if relayer not in keys:
            raise ValueError(f"no key for the relayer {relayer} in the wallet file")
        STATE.update(keys=keys, rpc_url=req["rpc_url"])
        secrets = {}
    STATE["rpc_urls"] = {"near:testnet": STATE["rpc_url"], "near:mainnet": BLACKHOLE}
    scheme = near_x402.ExactNearFacilitator(relayer, keys[relayer], STATE["rpc_urls"])
    fac = x402FacilitatorSync()
    fac.register(NETWORKS, scheme)
    STATE.update(facilitator=fac, scheme=scheme, relayer=relayer, clients={})
    return {"rpc_url": STATE["rpc_url"], "secrets": secrets}


def client_for(account: str) -> x402ClientSync:
    clients = STATE["clients"]
    if account not in clients:
        c = x402ClientSync()
        near_x402.register_exact_near_client(c, account, STATE["keys"][account], NETWORKS, STATE["rpc_urls"])
        clients[account] = c
    return clients[account]


# ---- the x402 roles ----------------------------------------------------------------------------
def sign(req: dict) -> dict:
    """Emporium as the buyer: a PaymentPayload for a PaymentRequired, as buyer.py makes one."""
    required = PaymentRequired.model_validate(req["paymentRequired"])
    return {"paymentPayload": dump(client_for(req["account"]).create_payment_payload(required))}


def _parse(req: dict) -> tuple[Any, Any, dict | None]:
    try:
        return PaymentPayload.model_validate(req["paymentPayload"]), \
            PaymentRequirements.model_validate(req["paymentRequirements"]), None
    except ValidationError as e:
        where = ".".join(str(x) for x in e.errors()[0].get("loc", ())) if e.errors() else ""
        return None, None, {"isValid": False, "invalidReason": "unparseable_x402", "invalidMessage": where}


def verify(req: dict) -> dict:
    payload, requirements, bad = _parse(req)
    if bad:
        return bad
    try:
        return dump(STATE["facilitator"].verify(payload, requirements))
    except Exception as e:  # SchemeNotFoundError and friends: the SDK found no scheme for these terms
        return {"isValid": False, "invalidReason": "sdk_" + type(e).__name__, "invalidMessage": str(e)[:200]}


def settle(req: dict) -> dict:
    payload, requirements, bad = _parse(req)
    if bad:
        return {"success": False, "errorReason": bad["invalidReason"], "transaction": "",
                "network": str(req["paymentRequirements"].get("network"))}
    try:
        return dump(STATE["facilitator"].settle(payload, requirements))
    except Exception as e:
        return {"success": False, "errorReason": "sdk_" + type(e).__name__, "errorMessage": str(e)[:200],
                "transaction": "", "network": str(requirements.network)}


def decode(req: dict) -> dict:
    """near.py's reading of a signed delegate (for the report), or why it refuses the bytes."""
    try:
        sda = near.SignedDelegateAction.from_base64(req["signedDelegateAction"])
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    d = sda.delegate_action
    return {"ok": True, "senderId": d.sender_id, "receiverId": d.receiver_id, "nonce": str(d.nonce),
            "maxBlockHeight": str(d.max_block_height), "publicKey": str(d.public_key),
            "actions": [{"methodName": a.method_name, "args": a.args.decode("utf-8", "replace"), "gas": str(a.gas),
                         "deposit": str(a.deposit)} for a in d.actions]}


def outcome(req: dict) -> dict:
    """What the chain did in a transaction: NEP-141 transfers of the token, and any failed receipt."""
    rpc = near.Rpc(STATE["rpc_url"])
    out = rpc.tx_status(req["tx"], req["sender"], wait_until="FINAL", timeout=60.0)
    return {"final": near.final(out), "failures": near.failures(out),
            "transfers": near.ft_transfers(out, req["token"]),
            "paid": str(near.paid(out, req["token"], req["payer"], req["payTo"]))}


def relay_unchecked(req: dict) -> dict:
    """Ask the chain itself: submit a signed delegate from the relayer WITHOUT verifying it first, and report
    what the token contract did. Used only for the edge cases, to see which verifier the chain agrees with."""
    scheme, rpc = STATE["scheme"], near.Rpc(STATE["rpc_url"])
    try:
        sda = near.SignedDelegateAction.from_base64(req["signedDelegateAction"])
    except Exception as e:
        return {"submitted": False, "error": f"near.py can't decode it: {e}"[:300]}
    with scheme._lock:
        try:
            tx = scheme._submit(rpc, "near:testnet", sda)
        except near.RpcError as e:
            return {"submitted": False, "error": str(e)[:400], "refusedBy": "chain"}
    _, out, lost = scheme._outcome(rpc, [(tx, time.time())], req["token"], req["payer"], req["payTo"], int(req["amount"] or 0))
    if out is None:
        return {"submitted": True, "tx": tx, "final": False, "lost": lost}
    return {"submitted": True, "tx": tx, "final": True, "failures": near.failures(out),
            "transfers": near.ft_transfers(out, req["token"]), "paid": str(near.paid(out, req["token"], req["payer"], req["payTo"]))}


def chain_state(req: dict) -> dict:
    """Balances and key nonces, read by near.py (offline: straight from the fake chain)."""
    rpc = near.Rpc(STATE["rpc_url"])
    res = {}
    for account in req.get("accounts", []):
        bal = rpc.view(req["token"], "ft_balance_of", {"account_id": account})
        res[account] = {"usdc": str(bal)}
    return res


OPS = {"setup": setup, "sign": sign, "verify": verify, "settle": settle, "decode": decode, "outcome": outcome,
       "chain_state": chain_state, "relay_unchecked": relay_unchecked, "versions": lambda req: {
           "x402": __import__("x402").__version__, "python": sys.version.split()[0]}}


def handle(line: str) -> None:
    msg: dict = {}
    try:
        msg = json.loads(line)
        result = {"id": msg.get("id"), "ok": True, "result": OPS[msg["op"]](msg.get("args") or {})}
    except Exception as e:
        result = {"id": msg.get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"[:500]}
        traceback.print_exc(file=sys.stderr)
    text = json.dumps(result, default=str)
    with OUT_LOCK:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def main() -> None:
    pool = ThreadPoolExecutor(max_workers=8)
    for line in sys.stdin:
        if not line.strip():
            continue
        if '"op": "setup"' in line or '"op":"setup"' in line:
            handle(line)  # set-up finishes before anything that needs it starts
        else:
            pool.submit(handle, line)
    pool.shutdown(wait=True)
    if STATE.get("chain"):
        STATE["chain"].stop()


if __name__ == "__main__":
    main()
