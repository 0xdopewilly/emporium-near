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


def load_keys(path: str) -> list[str]:
    """A JSON list of private keys, or {"buyers": [{"key": ...}, ...]}. Use throwaway testnet wallets."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = data.get("buyers") or data.get("keys") or []
    return [d["key"] if isinstance(d, dict) else str(d) for d in data]


def address(key: str) -> str:
    from eth_account import Account
    return Account.from_key(key).address


def usdc_balance(network: str, addr: str) -> Decimal:
    plugin()
    from emporium import chain
    from emporium.shop import RPC_URLS, USDC, from_atomic
    for attempt in range(4):
        try:
            data = "0x70a08231" + addr.lower().removeprefix("0x").rjust(64, "0")
            raw = chain._rpc(RPC_URLS[network], "eth_call", [{"to": USDC[network], "data": data}, "latest"])
            return from_atomic(int(raw, 16))
        except Exception:
            time.sleep(1 + attempt)
    raise RuntimeError(f"couldn't read the USDC balance of {addr}")


def pct(values: list[float], q: float):
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * len(v)))], 1)
