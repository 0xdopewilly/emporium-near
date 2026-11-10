"""Shared bench plumbing: load the plugin from this checkout, read wallets, read balances on-chain.

Run the benches with a Python that has the plugin's dependencies (Hermes's venv works):
    /path/to/hermes-agent/.venv/bin/python bench/stress.py --help
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def plugin():
    if "emporium" not in sys.modules:
        spec = importlib.util.spec_from_file_location("emporium", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["emporium"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["emporium"]


class Wallet:
    """One throwaway buyer: an EVM private key, or a NEAR account with its ed25519 key."""

    def __init__(self, key: str, account: str = ""):
        self.key, self.account = key, account
        self.addr = account or address(key)

    def hire(self, url: str, service: str, text: str, policy) -> dict:
        from emporium import buyer
        if self.account:
            return buyer.hire(url, service, text, policy=policy, key=None, ledger=None,
                              near_account=self.account, near_key=self.key)
        return buyer.hire(url, service, text, policy=policy, key=self.key, ledger=None)


def load_wallets(path: str) -> list[Wallet]:
    """Throwaway testnet buyers from a JSON file: a list of EVM private keys, or {"buyers": [...]} where each
    entry is {"key": "0x…"} (EVM) or {"account": "you.testnet", "key": "ed25519:…"} (NEAR;
    `scripts/near_accounts.py fleet` writes one)."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = data.get("buyers") or data.get("keys") or []
    return [Wallet(d["key"], d.get("account", "")) if isinstance(d, dict) else Wallet(str(d)) for d in data]


def address(key: str) -> str:
    from eth_account import Account
    return Account.from_key(key).address


def usdc_balance(network: str, addr: str) -> Decimal:
    plugin()
    from emporium import chain
    from emporium.shop import RPC_URLS, USDC, from_atomic
    for attempt in range(4):
        try:
            if network.startswith("near:"):
                from emporium.near import Rpc
                return from_atomic(int(Rpc(RPC_URLS[network]).view(USDC[network], "ft_balance_of", {"account_id": addr})))
            data = "0x70a08231" + addr.lower().removeprefix("0x").rjust(64, "0")
            raw = chain._rpc(RPC_URLS[network], "eth_call", [{"to": USDC[network], "data": data}, "latest"])
            return from_atomic(int(raw, 16))
        except Exception:
            time.sleep(1 + attempt)
    raise RuntimeError(f"couldn't read the USDC balance of {addr}")


def paid_on_chain(network: str, tx: str, pay_to: str, payer: str = "", relayer: str = "") -> Decimal:
    """USDC a receipt's transaction moved to the shop. A NEAR transaction is found by its hash and its
    signer, the shop's relayer (named in its catalog)."""
    plugin()
    from emporium import chain
    if network.startswith("near:"):
        return chain.near_paid(network, tx, relayer, payer, pay_to)
    return chain.paid_to(network, tx, pay_to)


def pct(values: list[float], q: float):
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * len(v)))], 1)
