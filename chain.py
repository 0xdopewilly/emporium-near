"""Reading the chain directly: did a USDC payment really land?

Two uses:
- the shop, when a settlement retry reports the authorization's nonce as already used: if our own
  earlier attempt did land on-chain, the buyer has paid and must get the result;
- the buyer, checking a shop's published receipts before trusting it.

Plain JSON-RPC over httpx against the network's public endpoint (or ``rpc_url`` in settings). On NEAR
a transaction is looked up by its hash and the account that signed it (the shop's relayer), and the
payment is the token contract's NEP-141 ``ft_transfer`` event (near_paid).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

import httpx

from .shop import RPC_URLS, USDC, from_atomic

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"           # Transfer(address,address,uint256)
AUTHORIZATION_USED = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"  # AuthorizationUsed(address,bytes32)


def _rpc(url: str, method: str, params: list, timeout: float = 15) -> Any:
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)
    body = r.json()
    if body.get("error"):
        raise RuntimeError(str(body["error"])[:200])
    return body.get("result")


def _topic_addr(addr: str) -> str:
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


def paid_to(network: str, tx: str, pay_to: str, rpc_url: str = "") -> Decimal:
    """USDC this transaction moved to ``pay_to`` (0 if it failed, isn't found or paid someone else)."""
    rpc, usdc = rpc_url or RPC_URLS.get(network, ""), USDC.get(network, "")
    if not (rpc and usdc and tx):
        return Decimal(0)
    receipt = _rpc(rpc, "eth_getTransactionReceipt", [tx]) or {}
    if str(receipt.get("status", "")).lower() != "0x1":
        return Decimal(0)
    total = Decimal(0)
    want = pay_to.lower().removeprefix("0x")
    for log in receipt.get("logs") or []:
        topics = [str(t).lower() for t in log.get("topics") or []]
        if (str(log.get("address", "")).lower() == usdc and len(topics) == 3 and topics[0] == TRANSFER
                and topics[2][-40:] == want):
            total += from_atomic(int(str(log.get("data") or "0x0"), 16))
    return total


def near_paid(network: str, tx: str, signer: str, payer: str, pay_to: str, rpc_url: str = "") -> Decimal:
    """USDC a NEAR transaction moved to ``pay_to`` (from ``payer``, or from anyone when it's empty);
    0 if any part of it failed, it isn't found, or it paid someone else."""
    from .near import Rpc, RpcError, failures, ft_transfers
    rpc, usdc = rpc_url or RPC_URLS.get(network, ""), USDC.get(network, "")
    if not (rpc and usdc and tx and signer):
        return Decimal(0)
    try:
        outcome = Rpc(rpc).tx_status(tx, signer, wait_until="FINAL")
    except RpcError:
        return Decimal(0)
    if failures(outcome):
        return Decimal(0)
    total = 0
    for t in ft_transfers(outcome, usdc):
        if t.get("new_owner_id") == pay_to and (not payer or t.get("old_owner_id") == payer):
            try:
                total += int(str(t.get("amount")))
            except ValueError:
                continue
    return from_atomic(total)


AUTHORIZATION_STATE = "0xe94a0102"  # authorizationState(address,bytes32)
_LOG_SPAN = 200                      # public RPCs cap eth_getLogs ranges (Base's at 200 blocks)


def authorization_used(network: str, authorizer: str, nonce: str, rpc_url: str = "") -> bool:
    """Whether USDC has consumed this EIP-3009 authorization (or the buyer cancelled it)."""
    rpc, usdc = rpc_url or RPC_URLS.get(network, ""), USDC.get(network, "")
    n = str(nonce).lower().removeprefix("0x").rjust(64, "0")
    data = AUTHORIZATION_STATE + authorizer.lower().removeprefix("0x").rjust(64, "0") + n
    return int(_rpc(rpc, "eth_call", [{"to": usdc, "data": data}, "latest"]) or "0x0", 16) != 0


def authorization_tx(network: str, authorizer: str, nonce: str, rpc_url: str = "", lookback: int = 1000) -> Optional[str]:
    """The transaction that used this EIP-3009 authorization, from USDC's AuthorizationUsed event in
    recent blocks (Base makes a block every 2 s, so 1000 blocks is about half an hour). Searched in
    200-block windows, newest first, and only once the contract says the authorization was used."""
    rpc, usdc = rpc_url or RPC_URLS.get(network, ""), USDC.get(network, "")
    if not (rpc and usdc and authorizer and nonce):
        return None
    if not authorization_used(network, authorizer, nonce, rpc_url):
        return None
    latest = int(_rpc(rpc, "eth_blockNumber", []), 16)
    n = "0x" + str(nonce).lower().removeprefix("0x").rjust(64, "0")
    hi = latest
    while hi > latest - lookback and hi >= 0:
        lo = max(0, hi - _LOG_SPAN + 1)
        logs = _rpc(rpc, "eth_getLogs", [{"address": usdc, "fromBlock": hex(lo), "toBlock": hex(hi),
                                          "topics": [AUTHORIZATION_USED, _topic_addr(authorizer), n]}]) or []
        if logs:
            return str(logs[-1]["transactionHash"])
        hi = lo - 1
    return None
