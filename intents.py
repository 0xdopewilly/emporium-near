"""Money in and out of NEAR from any chain, through NEAR Intents (the 1Click API), without giving up
"paid only on delivery".

- **Fund (buyer side).** A buyer with no USDC on NEAR tops up its *own* NEAR account from USDC on
  Base, Arbitrum, Solana, ETH, SOL, BTC… with an ``EXACT_OUTPUT`` quote: the account receives exactly
  the USDC asked for. The money stays the buyer's until a shop delivers a job (then the x402 payment
  settles as usual), so cross-chain never means paying up front.
- **Cash out (shop side).** A shop withdraws earnings from USDC on NEAR to any chain or token with an
  ``EXACT_INPUT`` quote. The deposit address 1Click hands out on NEAR is a fresh implicit account: the
  shop's relayer registers it with the USDC contract, the shop signs a NEP-366 ``ft_transfer`` to it
  and the relayer submits that (near_x402's facilitator, so the shop pays no gas and a retry never
  pays twice), then 1Click is told the transaction and followed until SUCCESS / REFUNDED / FAILED.

Every quote is checked before anything is paid: 1Click's ed25519 signature over it (the same check
Veto's vault contract makes on-chain), and field by field that it is the quote we asked for (assets,
amount, recipient, refund address, deadline) and pays out what it says.

1Click runs on mainnet only. ``EMPORIUM_ONECLICK_JWT`` (optional) is sent as a Bearer token; without
one, 1Click adds its small public-API fee and everything works the same.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

import httpx

from . import near
from .near import KeyPair, PublicKey, Rpc, RpcError, is_account_id
from .shop import RPC_URLS, USDC, parse_usd

logger = logging.getLogger(__name__)

ONE_CLICK_URL = "https://1click.chaindefuser.com/v0"
# 1Click's quote-signing key (from @defuse-protocol/one-click-sdk-typescript; Veto's contract trusts the same one).
ONE_CLICK_MANAGER_KEY = "ed25519:reYaWhvwu8Jzo3WUM3zhn6VrhuMEF4eADL17qtRVifc"
JWT_SECRET = "EMPORIUM_ONECLICK_JWT"
NETWORK = "near:mainnet"                     # NEAR Intents has no testnet
NEAR_USDC = "nep141:" + USDC[NETWORK]
REFERRAL = "emporium"
STORAGE_DEPOSIT = 1_250_000_000_000_000_000_000  # 0.00125 NEAR: what USDC on NEAR charges to register an account
USDC_DECIMALS = 6
MAX_USD = Decimal("10000")                   # a typo guard per fund or cash-out
DEFAULT_SLIPPAGE_BPS = 100                   # 1Click's own example; on a fund, the unused part is refunded
STABLE_SLIPPAGE_BPS = 50                     # stablecoin to stablecoin: a smaller buffer to send and get back
DEFAULT_MAX_FEE_BPS = 300                    # refuse a quote that may cost more than 3% (slippage buffer included)
DELEGATE_SECONDS = 600                       # how long the shop's signed transfer stays valid

PENDING = ("PENDING_DEPOSIT", "KNOWN_DEPOSIT_TX", "INCOMPLETE_DEPOSIT", "PROCESSING")
TERMINAL = ("SUCCESS", "REFUNDED", "FAILED")

# The fields 1Click signs (docs.near-intents.org, "verify quote signature"), in the order the
# official SDK reads them. The first group is always present; the rest only when set.
_REQ_ALWAYS = ("dry", "swapType", "slippageTolerance", "originAsset", "depositType", "destinationAsset", "amount",
               "refundTo", "refundType", "recipient", "recipientType", "deadline")
_REQ_IF_SET = ("quoteWaitingTimeMs", "referral", "virtualChainRecipient", "virtualChainRefundRecipient",
               "customRecipientMsg")
_QUOTE_ALWAYS = ("amountIn", "amountInFormatted", "amountInUsd", "minAmountIn", "amountOut", "amountOutFormatted",
                 "amountOutUsd", "minAmountOut")
_QUOTE_LIVE_IF_SET = ("depositAddress", "depositMemo", "deadline", "timeWhenInactive", "timeEstimate", "refundFee",
                      "withdrawFee")

# What people call chains, mapped to 1Click's blockchain ids; and symbols a chain lists under another name.
CHAIN_ALIASES = {"ethereum": "eth", "arbitrum": "arb", "solana": "sol", "bitcoin": "btc", "polygon": "pol",
                 "matic": "pol", "bnb": "bsc", "binance": "bsc", "optimism": "op", "avalanche": "avax",
                 "xdai": "gnosis", "berachain": "bera", "dogecoin": "doge", "litecoin": "ltc", "ripple": "xrp",
                 "stellar": "stellar", "zcash": "zec", "tron": "tron", "ton": "ton", "sui": "sui", "aptos": "aptos"}
SYMBOL_FALLBACKS = {"near": ("wnear",), "usdt": ("usdt0",), "usdc": ("usdc.e",), "xdai": ("xdai",)}
EVM_CHAINS = {"eth": 1, "base": 8453, "arb": 42161, "op": 10, "pol": 137, "bsc": 56, "avax": 43114, "gnosis": 100,
              "bera": 80094, "monad": 143, "xlayer": 196, "scroll": 534352, "plasma": 9745, "abs": 2741,
              "hood": 0, "adi": 0, "hlevm": 999}
# Public JSON-RPC endpoints for the chains a buyer's own wallet can pay a deposit from (optional top-up).
EVM_RPC = {"base": "https://mainnet.base.org", "arb": "https://arb1.arbitrum.io/rpc",
           "eth": "https://ethereum-rpc.publicnode.com", "op": "https://mainnet.optimism.io",
           "pol": "https://polygon-rpc.com", "bsc": "https://bsc-dataseed.bnbchain.org",
           "avax": "https://api.avax.network/ext/bc/C/rpc", "gnosis": "https://rpc.gnosischain.com"}
# Stand-in refund/recipient addresses for *dry* quotes only. A dry quote makes no deposit address, so
# nothing can ever be sent to or refunded at these; 1Click just wants a well-formed address for the chain.
SAMPLE_ADDRESS = {"evm": "0xa11ce57b2f0c88de41a9b3c07e6d1f45a3c0d9f2", "near": "intents.near",
                  "sol": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "btc": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
                  "tron": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "ton": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
                  "sui": "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7",
                  "aptos": "0xbae207659db88bea0cbead6da0ed00aac12edcdda169e591cd41c94180b46f3b",
                  "stellar": "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
                  "xrp": "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"}

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_IMPLICIT = re.compile(r"^[0-9a-f]{64}$")
_BTC_ADDRESS = re.compile(r"^(bc1[02-9ac-hj-np-z]{11,71}|[13][1-9A-HJ-NP-Za-km-z]{25,34})$")
_MIN_AMOUNT = re.compile(r"try at least (\d+)")
_MIN_USD = re.compile(r"minimum swap amount is \$([\d.,]+)")


class IntentsError(Exception):
    """A refusal before any money moved, or a step that didn't complete (the message says which)."""


class QuoteRejected(IntentsError):
    """1Click's quote isn't signed by 1Click, or isn't the quote we asked for. ``field`` names what differs."""

    def __init__(self, field: str, message: str):
        super().__init__(f"quote rejected ({field}): {message}")
        self.field = field


class OneClickError(IntentsError):
    """1Click answered with an error. When the amount was below a route's minimum, ``min_amount`` is that
    minimum in the request's ``amount`` units, or ``min_usd`` when 1Click states it in dollars."""

    def __init__(self, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status, self.body = status, body
        m = _MIN_AMOUNT.search(message or "")
        self.min_amount = int(m.group(1)) if m else None
        u = _MIN_USD.search(message or "")
        self.min_usd = Decimal(u.group(1).replace(",", "")) if u else None


# ---- amounts --------------------------------------------------------------------------------------
def usd_atomic(value: Any, decimals: int = USDC_DECIMALS) -> int:
    """'$5' / '5.25' / '1,000.50' -> USDC base units. Refuses zero, negatives, more precision than USDC
    has (rather than rounding someone's money) and amounts above MAX_USD."""
    amount = parse_usd(value)
    if amount is None:
        raise IntentsError(f"{value!r} is not a positive USD amount")
    if amount > MAX_USD:
        raise IntentsError(f"${amount} is above the ${MAX_USD} limit per transfer")
    step = Decimal(1).scaleb(-decimals)
    if amount != amount.quantize(step):
        raise IntentsError(f"${amount} has more decimals than USDC's {decimals}")
    return int(amount.scaleb(decimals))


def fmt_amount(atomic: Any, decimals: int) -> str:
    """Base units -> a plain decimal string with no trailing zeros (12345, 6 -> '0.012345')."""
    try:
        d = Decimal(int(str(atomic))).scaleb(-int(decimals))
    except (TypeError, ValueError, InvalidOperation):
        return "?"
    text = format(d, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _int(value: Any, field: str) -> int:
    try:
        n = int(str(value))
    except (TypeError, ValueError) as e:
        raise QuoteRejected(field, f"{value!r} is not an amount") from e
    if n < 0:
        raise QuoteRejected(field, "negative amount")
    return n


def _dec(value: Any) -> Optional[Decimal]:
    try:
        d = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None
    return d if d.is_finite() and d > 0 else None


def iso(ts: float) -> str:
    """A UTC timestamp the way 1Click writes them: 2026-11-12T21:53:28.677Z."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(text: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


# ---- tokens ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Token:
    asset_id: str
    chain: str
    symbol: str
    decimals: int
    price: Optional[Decimal] = None
    contract: str = ""

    @classmethod
    def from_api(cls, t: dict) -> "Token":
        return cls(asset_id=str(t["assetId"]), chain=str(t.get("blockchain") or ""), symbol=str(t.get("symbol") or ""),
                   decimals=int(t.get("decimals") or 0), price=_dec(t.get("price")),
                   contract=str(t.get("contractAddress") or ""))

    @property
    def label(self) -> str:
        return f"{self.symbol} on {chain_label(self.chain)}"

    @property
    def stable(self) -> bool:
        return self.symbol.upper().rstrip("0") in ("USDC", "USDT", "USDC.E", "DAI", "USDG", "USD1", "USDE")


_CHAIN_LABELS = {"eth": "Ethereum", "base": "Base", "arb": "Arbitrum", "op": "Optimism", "pol": "Polygon",
                 "bsc": "BNB Chain", "avax": "Avalanche", "sol": "Solana", "btc": "Bitcoin", "near": "NEAR",
                 "gnosis": "Gnosis", "tron": "Tron", "ton": "TON", "sui": "Sui", "aptos": "Aptos", "xrp": "XRP Ledger",
                 "doge": "Dogecoin", "ltc": "Litecoin", "bera": "Berachain", "stellar": "Stellar", "zec": "Zcash"}


def chain_label(chain: str) -> str:
    return _CHAIN_LABELS.get(chain, chain)


def chain_family(chain: str) -> str:
    return "evm" if chain in EVM_CHAINS else chain


def resolve(tokens: list[Token], spec: str) -> Token:
    """``base:usdc``, ``arb:usdc``, ``sol:sol``, ``eth:eth``, ``near:near`` (wNEAR)… or a full asset id
    (``nep141:…``) -> the 1Click token. When a chain lists a symbol twice, the NEP-141 one wins."""
    spec = str(spec or "").strip()
    if re.match(r"^(nep141|nep245|1cs_v1):", spec):
        hit = next((t for t in tokens if t.asset_id == spec), None)
        if hit is None:
            raise IntentsError(f"1Click doesn't list the asset {spec}")
        return hit
    chain, sep, symbol = spec.lower().partition(":")
    if not sep or not chain or not symbol:
        raise IntentsError(f"{spec!r}: name a token as chain:symbol, e.g. base:usdc, sol:sol, eth:eth")
    chain = CHAIN_ALIASES.get(chain, chain)
    on_chain = [t for t in tokens if t.chain == chain]
    if not on_chain:
        chains = sorted({t.chain for t in tokens})
        raise IntentsError(f"1Click doesn't support the chain {chain!r} (it supports {', '.join(chains)})")
    order = {"nep141": 0, "nep245": 1}
    for want in (symbol, *SYMBOL_FALLBACKS.get(symbol, ())):
        hits = sorted((t for t in on_chain if t.symbol.lower() == want), key=lambda t: order.get(t.asset_id.split(":")[0], 2))
        if hits:
            return hits[0]
    have = ", ".join(sorted({t.symbol for t in on_chain if "(" not in t.symbol}))
    raise IntentsError(f"1Click has no {symbol.upper()} on {chain_label(chain)} (it has {have})")


def check_address(chain: str, address: str, role: str = "address") -> str:
    """A well-formed address on ``chain`` (EVM ones lowercased: 1Click refuses a mixed-case address whose
    checksum is off, and lowercase always passes). 1Click checks the rest itself."""
    a = str(address or "").strip()
    fam = chain_family(chain)
    ok = True
    if fam == "evm":
        ok = bool(_EVM_ADDRESS.match(a))
        a = a.lower()
    elif fam == "near":
        ok = is_account_id(a)
    elif fam == "sol":
        try:
            ok = 32 <= len(a) <= 44 and len(near.b58decode(a)) == 32
        except ValueError:
            ok = False
    elif fam == "btc":
        ok = bool(_BTC_ADDRESS.match(a))
    else:
        ok = 0 < len(a) <= 128 and not re.search(r"\s", a)
    if not ok:
        raise IntentsError(f"{address!r} is not a valid {chain_label(chain)} {role}")
    return a


# ---- quote signatures -----------------------------------------------------------------------------
def _canonical(value: Any) -> str:
    """JSON with sorted keys and no spaces, as JavaScript's JSON.stringify writes it."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _canonical(v)
                              for k, v in sorted(value.items())) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def signed_quote_json(resp: dict) -> str:
    """The exact JSON 1Click signs for a quote response: the request's fields, then the quote's (a live
    quote adds its deposit address, deadline and fees), then the timestamp; sorted keys, unset values
    dropped. Pinned to real 1Click signatures in tests/fixtures/oneclick_quotes.json."""
    req, quote = resp.get("quoteRequest") or {}, resp.get("quote") or {}
    fields: dict[str, Any] = {}
    for k in _REQ_ALWAYS:
        if k in req:
            fields[k] = req[k]
    for k in _REQ_IF_SET:
        if req.get(k):
            fields[k] = req[k]
    for k in _QUOTE_ALWAYS:
        if k in quote:
            fields[k] = quote[k]
    if not req.get("dry"):
        for k in _QUOTE_LIVE_IF_SET:
            if quote.get(k):
                fields[k] = quote[k]
            else:
                fields.pop(k, None)  # a live quote's own deadline replaces the request's (and unset drops it)
    fields["timestamp"] = resp.get("timestamp")
    return _canonical(fields)


def quote_message(signed_json: str) -> bytes:
    """What 1Click's key signs: base58(sha256(json)), as text."""
    return near.b58encode(hashlib.sha256(signed_json.encode("utf-8")).digest()).encode("ascii")


def verify_quote(resp: dict, manager_key: str = ONE_CLICK_MANAGER_KEY) -> bool:
    try:
        sig = near.b58decode(str(resp.get("signature") or "").removeprefix("ed25519:"))
        return PublicKey.parse(manager_key).verify(sig, quote_message(signed_quote_json(resp)))
    except Exception:
        return False


# ---- the 1Click API -------------------------------------------------------------------------------
_TOKENS: dict[str, tuple[float, list[Token]]] = {}
_TOKENS_LOCK = threading.Lock()


class OneClick:
    """A small client for 1Click: tokens (cached), quotes, deposit notices and status. Network errors,
    429s and 5xx answers are retried with backoff; a 4xx is 1Click's answer and is raised."""

    tokens_ttl = 600.0

    def __init__(self, url: str = "", jwt: str = "", manager_key: str = "", timeout: float = 30.0, retries: int = 3,
                 backoff: float = 1.0):
        self.url = (url or ONE_CLICK_URL).rstrip("/")
        self.jwt, self.manager_key = (jwt or "").strip(), manager_key or ONE_CLICK_MANAGER_KEY
        self.timeout, self.retries, self.backoff = timeout, max(0, retries), backoff

    def _call(self, method: str, path: str, body: Any = None, params: Optional[dict] = None) -> Any:
        headers = {"Accept": "application/json"}
        if self.jwt:
            headers["Authorization"] = f"Bearer {self.jwt}"
        last: Optional[OneClickError] = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(min(15.0, self.backoff * 2 ** (attempt - 1)))
            try:
                r = httpx.request(method, self.url + path, json=body, params=params, headers=headers,
                                  timeout=self.timeout, follow_redirects=False)
            except httpx.HTTPError as e:
                last = OneClickError(f"couldn't reach 1Click ({e.__class__.__name__})")
                continue
            try:
                data = r.json()
            except ValueError:
                data = {"message": r.text[:200]}
            if r.status_code == 429 or r.status_code >= 500:
                last = OneClickError(f"1Click is busy (HTTP {r.status_code})", r.status_code, data)
                try:
                    time.sleep(min(10.0, float(r.headers.get("retry-after") or 0)))
                except ValueError:
                    pass
                continue
            if r.status_code >= 400:
                msg = data.get("message") if isinstance(data, dict) else None
                if isinstance(msg, list):
                    msg = "; ".join(map(str, msg))
                raise OneClickError(f"1Click refused: {str(msg or data)[:300]}", r.status_code, data)
            return data
        raise last or OneClickError("1Click didn't answer")

    def tokens(self, fresh: bool = False) -> list[Token]:
        with _TOKENS_LOCK:
            hit = _TOKENS.get(self.url)
            if hit and not fresh and time.time() - hit[0] < self.tokens_ttl:
                return hit[1]
        raw = self._call("GET", "/tokens")
        if not isinstance(raw, list):
            raise OneClickError("1Click's token list isn't a list")
        toks = [Token.from_api(t) for t in raw if isinstance(t, dict) and t.get("assetId")]
        with _TOKENS_LOCK:
            _TOKENS[self.url] = (time.time(), toks)
        return toks

    def token(self, spec: str) -> Token:
        return resolve(self.tokens(), spec)

    def quote(self, request: dict) -> dict:
        resp = self._call("POST", "/quote", body=request)
        if not isinstance(resp, dict) or not isinstance(resp.get("quote"), dict):
            raise OneClickError("1Click's quote has no quote in it")
        return resp

    def status(self, deposit_address: str, memo: str = "") -> dict:
        params = {"depositAddress": deposit_address}
        if memo:
            params["depositMemo"] = memo
        return self._call("GET", "/status", params=params)

    def submit_deposit(self, tx_hash: str, deposit_address: str, near_sender: str = "", memo: str = "") -> dict:
        body = {"txHash": tx_hash, "depositAddress": deposit_address}
        if near_sender:
            body["nearSenderAccount"] = near_sender
        if memo:
            body["memo"] = memo
        return self._call("POST", "/deposit/submit", body=body)


# ---- checking a quote -----------------------------------------------------------------------------
@dataclass
class Quote:
    """A 1Click quote that verified: signed by 1Click and matching the request we sent."""

    request: dict
    response: dict
    origin: Token
    destination: Token

    @property
    def q(self) -> dict:
        return self.response["quote"]

    @property
    def dry(self) -> bool:
        return bool(self.request.get("dry"))

    @property
    def kind(self) -> str:
        return "fund" if self.request["swapType"] == "EXACT_OUTPUT" else "cashout"

    @property
    def signed_json(self) -> str:
        return signed_quote_json(self.response)

    @property
    def signature(self) -> str:
        return str(self.response.get("signature") or "")

    @property
    def deposit_address(self) -> str:
        return str(self.q.get("depositAddress") or "")

    @property
    def deposit_memo(self) -> str:
        return str(self.q.get("depositMemo") or "")

    amount_in = property(lambda self: int(str(self.q["amountIn"])))
    min_amount_in = property(lambda self: int(str(self.q["minAmountIn"])))
    amount_out = property(lambda self: int(str(self.q["amountOut"])))
    min_amount_out = property(lambda self: int(str(self.q["minAmountOut"])))

    @property
    def time_estimate(self) -> Optional[int]:
        try:
            return int(self.q.get("timeEstimate"))
        except (TypeError, ValueError):
            return None

    @property
    def pay_by(self) -> str:
        """The request's deadline: a deposit not swapped by then is refunded."""
        return str(self.request["deadline"])

    @property
    def address_expires(self) -> str:
        return str(self.q.get("deadline") or self.request["deadline"])

    def fee_bps(self) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """(expected, worst case) cost of the swap in basis points of its value, from 1Click's own USD
        prices. Worst case counts the whole slippage buffer; on EXACT_OUTPUT the unused part of the
        buffer comes back to the refund address."""
        in_usd, out_usd = _dec(self.q.get("amountInUsd")), _dec(self.q.get("amountOutUsd"))
        if in_usd is None or out_usd is None or not self.amount_in or not self.amount_out:
            return None, None
        if self.request["swapType"] == "EXACT_OUTPUT":
            expected = in_usd * self.min_amount_in / self.amount_in / out_usd - 1
            worst = in_usd / out_usd - 1
        else:
            expected = 1 - out_usd / in_usd
            worst = 1 - out_usd * self.min_amount_out / self.amount_out / in_usd
        return (expected * 10_000).quantize(Decimal("0.1")), (worst * 10_000).quantize(Decimal("0.1"))

    def summary(self) -> dict:
        expected, worst = self.fee_bps()
        return {
            "kind": self.kind, "dry": self.dry, "origin": self.origin.label, "origin_asset": self.origin.asset_id,
            "destination": self.destination.label, "destination_asset": self.destination.asset_id,
            "amount_in": fmt_amount(self.amount_in, self.origin.decimals),
            "min_amount_in": fmt_amount(self.min_amount_in, self.origin.decimals),
            "amount_out": fmt_amount(self.amount_out, self.destination.decimals),
            "min_amount_out": fmt_amount(self.min_amount_out, self.destination.decimals),
            "amount_in_usd": self.q.get("amountInUsd"), "amount_out_usd": self.q.get("amountOutUsd"),
            "fee_pct": None if expected is None else str(expected / 100),
            "fee_pct_worst": None if worst is None else str(worst / 100),
            "time_estimate_s": self.time_estimate, "recipient": self.request["recipient"],
            "refund_to": self.request["refundTo"], "deposit_address": self.deposit_address or None,
            "deposit_memo": self.deposit_memo or None, "pay_by": self.pay_by,
            "address_expires": None if self.dry else self.address_expires, "signature_ok": True,
        }


# ---- describing a quote ---------------------------------------------------------------------------
def fee_line(q: Quote) -> str:
    expected, worst = q.fee_bps()
    eta = f" · about {q.time_estimate}s once the deposit confirms" if q.time_estimate else ""
    if expected is None:
        return "fee: unknown (1Click has no USD price for one side)" + eta
    return f"fee ≈ {expected / 100}% expected, at most {worst / 100}% with the slippage buffer{eta}"


def quote_lines(q: Quote) -> list[str]:
    """A verified quote in plain words: what goes in, what comes out, the fee and how long it takes."""
    o, d = q.origin, q.destination
    if q.kind == "fund":
        lines = [f"  in:  {fmt_amount(q.amount_in, o.decimals)} {o.label} (about {fmt_amount(q.min_amount_in, o.decimals)} "
                 "is used; the rest of the buffer comes back to the refund address)",
                 f"  out: exactly {fmt_amount(q.amount_out, d.decimals)} {d.label} to {q.request['recipient']}"]
    else:
        lines = [f"  in:  exactly {fmt_amount(q.amount_in, o.decimals)} {o.label} from {q.request['refundTo']}",
                 f"  out: about {fmt_amount(q.amount_out, d.decimals)} {d.label} (at least "
                 f"{fmt_amount(q.min_amount_out, d.decimals)}) to {q.request['recipient']}"]
    return lines + [f"  {fee_line(q)}", "  ✓ signed by 1Click, and it is the quote that was asked for"]


def _same(field: str, got: Any, want: Any) -> bool:
    if field in ("recipient", "refundTo") and isinstance(want, str) and _EVM_ADDRESS.match(want):
        return isinstance(got, str) and got.lower() == want.lower()
    if type(got) is not type(want) and not (isinstance(got, (int, float)) and isinstance(want, (int, float))
                                            and not isinstance(got, bool) and not isinstance(want, bool)):
        return False
    return got == want


def check_quote(resp: dict, sent: dict, origin: Token, destination: Token, *,
                manager_key: str = ONE_CLICK_MANAGER_KEY, max_fee_bps: Optional[int] = DEFAULT_MAX_FEE_BPS,
                min_out: int = 0, now: Optional[float] = None) -> Quote:
    """Verify 1Click's signature and that the quote is the one we asked for, field by field, before a cent
    moves: the same assets, amount, recipient, refund address and deadline; it pays out at least what we
    asked (EXACT_OUTPUT) or takes exactly what we send (EXACT_INPUT); a live quote has a deposit address of
    the right kind that stays open; and the fee is inside ``max_fee_bps`` (worst case). Raises QuoteRejected."""
    now = time.time() if now is None else now
    if not isinstance(resp, dict) or not isinstance(resp.get("quote"), dict) or not isinstance(resp.get("quoteRequest"), dict):
        raise QuoteRejected("shape", "not a 1Click quote response")
    if not verify_quote(resp, manager_key):
        raise QuoteRejected("signature", "1Click's signature doesn't verify; the quote may have been altered")
    got, q = resp["quoteRequest"], resp["quote"]
    for field, want in sent.items():
        if not _same(field, got.get(field), want):
            raise QuoteRejected(field, f"the quote has {got.get(field)!r} where we asked for {want!r}")
    amount = int(sent["amount"])
    amount_in, min_in = _int(q.get("amountIn"), "amountIn"), _int(q.get("minAmountIn"), "minAmountIn")
    amount_out, min_out_q = _int(q.get("amountOut"), "amountOut"), _int(q.get("minAmountOut"), "minAmountOut")
    if sent["swapType"] == "EXACT_OUTPUT":
        if amount_out < amount or min_out_q < amount:
            raise QuoteRejected("amountOut", f"pays out {min_out_q}, less than the {amount} asked for")
        if amount_in <= 0 or min_in > amount_in:
            raise QuoteRejected("amountIn", "the deposit it asks for doesn't add up")
    else:
        if amount_in != amount or min_in > amount:
            raise QuoteRejected("amountIn", f"takes {amount_in} where we send exactly {amount}")
        if amount_out <= 0 or min_out_q > amount_out:
            raise QuoteRejected("amountOut", "the payout it promises doesn't add up")
        if min_out and min_out_q < min_out:
            raise QuoteRejected("minAmountOut", f"may pay out as little as {min_out_q}, below the {min_out} floor")
    quote = Quote(dict(sent), resp, origin, destination)
    if max_fee_bps is not None:
        _, worst = quote.fee_bps()
        if worst is not None and worst > max_fee_bps:
            raise QuoteRejected("fee", f"may cost {worst / 100}% (slippage buffer included), above the "
                                       f"{Decimal(max_fee_bps) / 100}% limit; a larger amount spreads fixed fees, or "
                                       f"accept it with max-fee {int(worst) + 1} (basis points)")
    pay_by = parse_iso(sent["deadline"])
    if pay_by is None or pay_by <= now:
        raise QuoteRejected("deadline", "the quote's deadline has passed")
    if not sent["dry"]:
        deposit = str(q.get("depositAddress") or "")
        fam = chain_family(origin.chain)
        if not deposit:
            raise QuoteRejected("depositAddress", "a live quote without a deposit address")
        # On NEAR, 1Click hands out a fresh implicit account (64 hex); on EVM chains, an address.
        if (fam == "near" and not _IMPLICIT.match(deposit)) or (fam == "evm" and not _EVM_ADDRESS.match(deposit)):
            raise QuoteRejected("depositAddress", f"{deposit!r} isn't an address on {chain_label(origin.chain)}")
        expires = parse_iso(q.get("deadline") or sent["deadline"])
        if expires is None or expires < pay_by:
            raise QuoteRejected("deadline", "the deposit address closes before the quote's deadline")
    return quote


def quote_request(*, dry: bool, swap_type: str, origin: Token, destination: Token, amount: int, refund_to: str,
                  recipient: str, window: float, slippage_bps: int = DEFAULT_SLIPPAGE_BPS, now: Optional[float] = None,
                  referral: str = REFERRAL) -> dict:
    if not 0 <= int(slippage_bps) <= 5000:
        raise IntentsError("slippage must be between 0 and 5000 basis points")
    return {"dry": bool(dry), "swapType": swap_type, "slippageTolerance": int(slippage_bps),
            "originAsset": origin.asset_id, "depositType": "ORIGIN_CHAIN", "destinationAsset": destination.asset_id,
            "amount": str(int(amount)), "refundTo": refund_to, "refundType": "ORIGIN_CHAIN", "recipient": recipient,
            "recipientType": "DESTINATION_CHAIN", "deadline": iso((time.time() if now is None else now) + window),
            "referral": referral}


def ask_quote(client: OneClick, request: dict) -> tuple[dict, dict]:
    """(the request as finally sent, 1Click's unchecked response). Some chains (Stellar, …) take deposits
    only with a memo; when 1Click says so, the quote is asked again for a memo deposit address."""
    try:
        return request, client.quote(request)
    except OneClickError as e:
        if "depositMode" in request or "depositmode" not in str(e).lower():
            raise
    request = {**request, "depositMode": "MEMO"}
    return request, client.quote(request)


def _ask(client: OneClick, request: dict, origin: Token, destination: Token, **checks: Any) -> Quote:
    request, resp = ask_quote(client, request)
    return check_quote(resp, request, origin, destination, manager_key=client.manager_key, **checks)


def _usdc_on_near(client: OneClick) -> Token:
    try:
        return resolve(client.tokens(), NEAR_USDC)
    except IntentsError:
        return Token(NEAR_USDC, "near", "USDC", USDC_DECIMALS, Decimal(1), USDC[NETWORK])


def _mainnet_account(account: str, role: str) -> str:
    account = str(account or "").strip()
    if not is_account_id(account):
        raise IntentsError(f"{role} {account!r} is not a NEAR account id")
    if account.endswith(".testnet"):
        raise IntentsError(f"{role} {account} is a testnet account; NEAR Intents runs on mainnet only")
    return account


def _rpc(rpc_url: str = "") -> Rpc:
    return Rpc(rpc_url or RPC_URLS[NETWORK])


# ---- fund: any chain -> USDC on the buyer's own NEAR account ----------------------------------------
def default_slippage(src: Token, dst: Token) -> int:
    return STABLE_SLIPPAGE_BPS if src.stable and dst.stable else DEFAULT_SLIPPAGE_BPS


def fund_quote(client: OneClick, *, usd: Any, origin: str, recipient: str, refund_to: str = "", dry: bool = False,
               slippage_bps: Optional[int] = None, max_fee_bps: Optional[int] = DEFAULT_MAX_FEE_BPS,
               window: Optional[float] = None, rpc_url: str = "", check_registered: bool = True) -> Quote:
    """A quote that delivers exactly ``usd`` USDC on NEAR to ``recipient`` (the buyer's own account) for a
    deposit of ``origin`` (e.g. base:usdc). Anything 1Click can't swap goes back to ``refund_to`` on the
    origin chain. A live quote first checks the recipient is registered with USDC on NEAR, so the swap's
    payout can't bounce."""
    amount = usd_atomic(usd)
    recipient = _mainnet_account(recipient, "the NEAR account to fund")
    src, dst = client.token(origin), _usdc_on_near(client)
    if src.asset_id == dst.asset_id:
        raise IntentsError("that's already USDC on NEAR; send it to the account directly")
    if refund_to:
        refund_to = check_address(src.chain, refund_to, "refund address")
    elif dry:
        refund_to = SAMPLE_ADDRESS.get(chain_family(src.chain), "")
        if not refund_to:
            raise IntentsError(f"name a refund address on {chain_label(src.chain)} (refund <address>)")
    else:
        raise IntentsError(f"name the {chain_label(src.chain)} address you pay from: 1Click refunds anything it "
                           "can't swap there (refund <address>)")
    if not dry and check_registered:
        if _rpc(rpc_url).view(USDC[NETWORK], "storage_balance_of", {"account_id": recipient}) is None:
            raise IntentsError(
                f"{recipient} isn't registered with USDC on NEAR yet, so the swap's payout could bounce. Register it "
                f"first (0.00125 NEAR, from any account): near call {USDC[NETWORK]} storage_deposit "
                f"'{{\"account_id\":\"{recipient}\",\"registration_only\":true}}' --deposit 0.00125 --accountId <payer>")
    if window is None:
        window = 7200 if src.chain in ("btc", "doge", "ltc", "bch", "zec", "dash") else 3600
    req = quote_request(dry=dry, swap_type="EXACT_OUTPUT", origin=src, destination=dst, amount=amount,
                        refund_to=refund_to, recipient=recipient, window=window,
                        slippage_bps=default_slippage(src, dst) if slippage_bps is None else slippage_bps)
    return _ask(client, req, src, dst, max_fee_bps=max_fee_bps)


# ---- cash out: USDC on the shop's NEAR account -> any chain -----------------------------------------
def cashout_quote(client: OneClick, *, usd: Any, destination: str, recipient: str, shop: str, dry: bool = False,
                  slippage_bps: Optional[int] = None, max_fee_bps: Optional[int] = DEFAULT_MAX_FEE_BPS,
                  window: float = 1800, min_out: int = 0) -> Quote:
    """A quote that swaps exactly ``usd`` USDC from the shop's NEAR account into ``destination`` (e.g.
    base:usdc) paid to ``recipient`` there. Refunds go back to the shop's NEAR account."""
    amount = usd_atomic(usd)
    shop = _mainnet_account(shop, "the shop's NEAR account")
    src, dst = _usdc_on_near(client), client.token(destination)
    if src.asset_id == dst.asset_id:
        raise IntentsError("that's USDC on NEAR already; a plain transfer does it")
    recipient = check_address(dst.chain, recipient, "recipient")
    req = quote_request(dry=dry, swap_type="EXACT_INPUT", origin=src, destination=dst, amount=amount,
                        refund_to=shop, recipient=recipient, window=window,
                        slippage_bps=default_slippage(src, dst) if slippage_bps is None else slippage_bps)
    return _ask(client, req, src, dst, max_fee_bps=max_fee_bps, min_out=min_out)


def quote_from_record(row: dict, client: OneClick, now: Optional[float] = None) -> Quote:
    """Rebuild a stored quote (the ledger keeps 1Click's signed response) and verify it again: its
    signature, that it is the request we kept, and that its deadline hasn't passed."""
    resp = json.loads(row["quote"])
    toks = client.tokens()
    src, dst = resolve(toks, resp["quoteRequest"]["originAsset"]), resolve(toks, resp["quoteRequest"]["destinationAsset"])
    sent = {k: resp["quoteRequest"][k] for k in (*_REQ_ALWAYS, "referral") if k in resp["quoteRequest"]}
    if (sent.get("recipient"), sent.get("refundTo"), resp["quote"].get("depositAddress")) != \
            (row.get("recipient"), row.get("refund_to"), row.get("deposit")):
        raise QuoteRejected("record", "the kept quote doesn't match its own record")
    return check_quote(resp, sent, src, dst, manager_key=client.manager_key, max_fee_bps=None, now=now)


def ensure_registered(rpc: Rpc, relayer: str, relayer_key: KeyPair, account: str, token: str = USDC[NETWORK],
                      wait: float = 60.0, poll: float = 2.0) -> str:
    """Register ``account`` with the token contract, paid by the relayer (0.00125 NEAR). Returns the
    transaction hash, or "" when it was registered already. A send that timed out is never sent again
    blind: its hash is looked up and the registration re-read first."""
    if rpc.view(token, "storage_balance_of", {"account_id": account}) is not None:
        return ""
    args = json.dumps({"account_id": account, "registration_only": True}, separators=(",", ":")).encode()
    action = near.FunctionCall("storage_deposit", args, 30 * near.TGAS, STORAGE_DEPOSIT)
    outcome, tx_hash = None, ""
    for attempt in (1, 2):
        access = rpc.access_key(relayer, str(relayer_key.public_key))
        if access is None:
            raise IntentsError(f"the relayer {relayer} has no key {relayer_key.public_key}")
        block_hash = near.b58decode(rpc.final_block()["hash"])
        tx_hash, signed = near.Transaction(relayer, relayer_key.public_key, int(access["nonce"]) + 1, token, block_hash,
                                           (action,)).sign(relayer_key)
        try:
            outcome = rpc.send_tx(signed, wait_until="FINAL", timeout=60.0)
            break
        except RpcError as e:
            if e.name in ("TIMEOUT_ERROR", "RPC_UNREACHABLE", "PROVIDER_LIMIT"):
                break  # it may well have landed: look for it below rather than pay again
            if attempt == 1 and "nonce" in str(e.data or e).lower():
                continue  # the shop's gateway used the relayer key meanwhile: read its nonce again
            raise IntentsError(f"the relayer couldn't register {account} with USDC: {e}") from e
    if outcome is None:
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                outcome = rpc.tx_status(tx_hash, relayer, wait_until="FINAL", timeout=20.0)
                break
            except RpcError:
                if rpc.view(token, "storage_balance_of", {"account_id": account}) is not None:
                    return tx_hash
                time.sleep(poll)
        if outcome is None:
            raise IntentsError(f"the registration of {account} (tx {tx_hash}) hasn't shown up yet; run it again "
                               "in a minute (it checks first, so nothing is paid twice)")
    why = near.failures(outcome)
    if why and rpc.view(token, "storage_balance_of", {"account_id": account}) is None:
        raise IntentsError(f"registering {account} with USDC failed: {json.dumps(why)[:200]}")
    return tx_hash


def _requirements(asset: str, pay_to: str, amount: int, seconds: int = DELEGATE_SECONDS) -> Any:
    from x402.schemas import PaymentRequirements
    return PaymentRequirements(scheme="exact", network=NETWORK, asset=asset, amount=str(amount), pay_to=pay_to,
                               max_timeout_seconds=seconds, extra={})


def _payload(requirements: Any, signed_b64: str) -> Any:
    from x402.schemas import PaymentPayload
    return PaymentPayload(x402_version=2, payload={"signedDelegateAction": signed_b64}, accepted=requirements)


def _delegate_dead(rpc: Rpc, sda: near.SignedDelegateAction) -> bool:
    """A signed transfer that can never run now: past its last block and its nonce never used."""
    d = sda.delegate_action
    head = int(rpc.final_block()["height"])
    access = rpc.access_key(d.sender_id, str(d.public_key))
    return head > d.max_block_height and access is not None and int(access.get("nonce", 0)) < d.nonce


def _deposit_seen(client: OneClick, rpc: Rpc, deposit: str, amount: int, token: str = USDC[NETWORK]) -> bool:
    """Whether the deposit address already got its money: 1Click says it moved past PENDING_DEPOSIT, or the
    address holds the amount (1Click sweeps it soon after)."""
    try:
        if client.status(deposit).get("status") not in (None, "PENDING_DEPOSIT"):
            return True
    except IntentsError:
        pass
    try:
        return int(str(rpc.view(token, "ft_balance_of", {"account_id": deposit}) or 0)) >= amount
    except RpcError:
        return False


def _landed(rpc: Rpc, tx: str, relayer: str, token: str, payer: str, deposit: str, amount: int) -> Optional[bool]:
    """Whether the relayer transaction ``tx`` moved ``amount`` to the deposit address (None: unknown yet)."""
    try:
        out = rpc.tx_status(tx, relayer, wait_until="FINAL", timeout=20.0)
    except RpcError:
        return None
    if not near.final(out):
        return None
    return near.paid(out, token, payer, deposit) == amount


class Books:
    """Where fund and cash-out records live: the shop's ledger (ledger.Ledger), or memory for one-off use."""

    def __init__(self, ledger: Any = None):
        self.ledger = ledger
        self._rows: dict[str, dict] = {}

    def get(self, deposit: str) -> Optional[dict]:
        return self.ledger.transfer(deposit) if self.ledger is not None else self._rows.get(deposit)

    def add(self, row: dict) -> None:
        if self.ledger is not None:
            self.ledger.add_transfer(row)
        else:
            self._rows.setdefault(row["deposit"], {**row, "created": time.time(), "updated": time.time()})

    def update(self, deposit: str, **fields: Any) -> None:
        if self.ledger is not None:
            self.ledger.update_transfer(deposit, **fields)
        elif deposit in self._rows:
            self._rows[deposit].update(fields, updated=time.time())


def record(books: Books, quote: Quote, status: str = "quoted") -> dict:
    """Keep a live quote (1Click's signed response, to settle any dispute) before anything is paid."""
    if quote.dry:
        raise IntentsError("a dry quote has no deposit address to keep")
    usd_value = fmt_amount(quote.amount_out if quote.kind == "fund" else quote.amount_in, USDC_DECIMALS)
    row = {"deposit": quote.deposit_address, "kind": quote.kind, "usd": usd_value, "origin": quote.origin.asset_id,
           "destination": quote.destination.asset_id, "recipient": quote.request["recipient"],
           "refund_to": quote.request["refundTo"], "amount_in": str(quote.amount_in), "amount_out": str(quote.amount_out),
           "status": status, "quote": json.dumps(quote.response, separators=(",", ":")), "signature": quote.signature}
    books.add(row)
    return books.get(quote.deposit_address) or row


def cashout_send(client: OneClick, quote: Quote, *, shop: str, shop_key: KeyPair, relayer: str, relayer_key: KeyPair,
                 books: Optional[Books] = None, rpc_url: str = "", wait: float = 0.0, poll: float = 5.0,
                 on_status: Optional[Callable[[str], None]] = None) -> dict:
    """Pay a verified cash-out quote from the shop's NEAR account, gaslessly and at most once:

    1. the relayer registers the deposit address with USDC (if it isn't);
    2. the shop's key signs one NEP-366 ``ft_transfer`` of exactly the quoted amount to the deposit
       address; it is kept before it's sent, and a retry reuses it (its nonce lets the chain run it once).
       A new one is signed only when the old one provably can't run: past its last block, nonce unused;
    3. the relayer submits it and pays the gas (near_x402's facilitator: a timed-out send is looked up,
       not resent; a transaction the network lost goes again carrying the same signed transfer);
    4. 1Click is told the transaction hash, and (with ``wait``) followed until it finishes.

    Returns {deposit, storage_tx, near_tx, status, destination_txs, …}."""
    from .near_x402 import ExactNearClient, ExactNearFacilitator, key_lock
    if quote.kind != "cashout" or quote.dry:
        raise IntentsError("only a live cash-out quote can be paid this way")
    shop = _mainnet_account(shop, "the shop's NEAR account")
    relayer = _mainnet_account(relayer, "the relayer")
    if shop == relayer:
        raise IntentsError("the relayer must be a different account from the shop")
    if quote.request["refundTo"] != shop:
        raise IntentsError("this quote refunds to another account than the shop's")
    token = USDC[NETWORK]
    if quote.origin.asset_id != NEAR_USDC:
        raise IntentsError("a cash-out spends USDC on NEAR")
    deposit, amount = quote.deposit_address, quote.amount_in
    if not _IMPLICIT.match(deposit):
        raise IntentsError(f"the deposit address {deposit!r} isn't a NEAR implicit account")
    books = books or Books()
    row = books.get(deposit) or record(books, quote)
    out = {"deposit": deposit, "amount": fmt_amount(amount, USDC_DECIMALS), "storage_tx": row.get("storage_tx") or "",
           "near_tx": row.get("deposit_tx") or "", "status": row.get("status") or "quoted", "destination_txs": []}
    if out["status"] in TERMINAL:
        return out
    rpc = _rpc(rpc_url)
    rpc_urls = {NETWORK: rpc_url} if rpc_url else None
    req = _requirements(token, deposit, amount)
    fac = ExactNearFacilitator(relayer, relayer_key, rpc_urls)

    with key_lock(shop, shop_key.public_key):
        signed = row.get("signed") or ""
        if signed and out["near_tx"] and _landed(rpc, out["near_tx"], relayer, token, shop, deposit, amount):
            paid = True
        else:
            paid = False
        if not paid:
            if quote.pay_by and (parse_iso(quote.pay_by) or 0) - time.time() < 120:
                if not signed:
                    raise IntentsError("this quote's deadline is too close to pay it safely; ask for a new one")
            if signed:
                sda = near.SignedDelegateAction.from_base64(signed)
                if _delegate_dead(rpc, sda):
                    try:
                        seen = client.status(deposit).get("status")
                    except IntentsError:
                        seen = None
                    if seen not in (None, "PENDING_DEPOSIT"):
                        raise IntentsError(f"1Click already reports {seen} for {deposit}; not paying again")
                    signed = ""  # it can never run: a fresh one may be signed
            if not signed:
                balance = int(str(rpc.view(token, "ft_balance_of", {"account_id": shop}) or 0))
                if balance < amount:
                    raise IntentsError(f"{shop} holds {fmt_amount(balance, USDC_DECIMALS)} USDC, less than the "
                                       f"{out['amount']} to cash out")
            if not out["storage_tx"]:
                books.update(deposit, status="registering")
                out["storage_tx"] = ensure_registered(rpc, relayer, relayer_key, deposit, token) or "already"
                books.update(deposit, storage_tx=out["storage_tx"])
            if not signed:
                body = ExactNearClient(shop, shop_key, rpc_urls).create_payment_payload(req)
                signed = body["signedDelegateAction"]
                books.update(deposit, signed=signed, status="signed")  # kept before it's sent
            payload = _payload(req, signed)
            books.update(deposit, status="sending")
            s = fac.settle(payload, req)
            if s.transaction:
                out["near_tx"] = s.transaction
                books.update(deposit, deposit_tx=s.transaction)
            if not s.success:
                landed = out["near_tx"] and _landed(rpc, out["near_tx"], relayer, token, shop, deposit, amount)
                if not landed and s.error_reason == "delegate_action_nonce_already_used":
                    # Our signed transfer's nonce is spent; if it was this transfer, the deposit shows it.
                    landed = _deposit_seen(client, rpc, deposit, amount, token)
                if not landed:
                    why = f"{s.error_reason}: {s.error_message}" if s.error_message != s.error_reason else s.error_reason
                    if s.error_reason == "transfer_failed":
                        # The chain ran the signed transfer and the token refused it: its nonce is spent and
                        # nothing moved, so a retry may sign a fresh one.
                        books.update(deposit, signed="", deposit_tx="", status="failed", error=str(why)[:300])
                        raise IntentsError(f"the USDC transfer failed on-chain and nothing moved ({why}); "
                                           "run it again to retry")
                    books.update(deposit, status="signed" if s.error_reason == "settlement_pending" else "failed",
                                 error=str(why)[:300])
                    raise IntentsError(f"the transfer to 1Click didn't complete ({why}); run it again to resume: "
                                       "the same signed transfer is reused, so nothing is paid twice")
    try:
        client.submit_deposit(out["near_tx"], deposit, near_sender=relayer)
    except IntentsError as e:  # optional: 1Click watches the address anyway
        logger.info("intents: deposit notice for %s: %s", deposit, e)
    if out["status"] not in PENDING:
        out["status"] = "PENDING_DEPOSIT"
        books.update(deposit, status="sent", error="")
    if wait > 0:
        out.update(track(client, deposit, books=books, wait=wait, poll=poll, on_status=on_status))
    return out


# ---- following a transfer -------------------------------------------------------------------------
def describe_status(st: dict) -> dict:
    details = st.get("swapDetails") or {}
    dest = [{"hash": t.get("hash"), "explorer": t.get("explorerUrl")} for t in details.get("destinationChainTxHashes") or []
            if isinstance(t, dict) and t.get("hash")]
    origin = [t.get("hash") for t in details.get("originChainTxHashes") or [] if isinstance(t, dict) and t.get("hash")]
    return {"status": str(st.get("status") or "UNKNOWN"), "updated": st.get("updatedAt"), "destination_txs": dest,
            "origin_txs": origin, "near_txs": list(details.get("nearTxHashes") or []),
            "amount_out": details.get("amountOutFormatted"), "refunded": details.get("refundedAmountFormatted"),
            "refund_reason": details.get("refundReason")}


def track(client: OneClick, deposit: str, *, books: Optional[Books] = None, memo: str = "", wait: float = 0.0,
          poll: float = 5.0, on_status: Optional[Callable[[str], None]] = None) -> dict:
    """Where a deposit stands at 1Click, followed for up to ``wait`` seconds until it is SUCCESS, REFUNDED
    or FAILED. ``timed_out`` is set when it was still going. The quote 1Click returns with the status is
    checked against 1Click's signature too (``quote_signed``)."""
    deadline = time.time() + max(0.0, wait)
    seen = ""
    row = books.get(deposit) if books is not None else None
    if not memo and row and row.get("quote"):
        try:
            memo = str(json.loads(row["quote"])["quote"].get("depositMemo") or "")
        except (ValueError, KeyError, TypeError):
            memo = ""
    while True:
        st = client.status(deposit, memo)
        info = describe_status(st)
        qr = st.get("quoteResponse")
        info["quote_signed"] = verify_quote(qr, client.manager_key) if isinstance(qr, dict) and qr.get("signature") else None
        if info["status"] != seen:
            seen = info["status"]
            if on_status:
                on_status(seen)
            if books is not None:
                extra = {"dest_txs": json.dumps(info["destination_txs"])} if info["destination_txs"] else {}
                books.update(deposit, status=seen, **extra)
        if info["status"] in TERMINAL:
            return {**info, "timed_out": False}
        if time.time() + poll > deadline:
            return {**info, "timed_out": wait > 0}
        time.sleep(poll)


# ---- optional: a buyer's own EVM wallet pays the deposit -------------------------------------------
_ERC20_TRANSFER = "0xa9059cbb"
_ERC20_BALANCE = "0x70a08231"


def _evm(url: str, method: str, params: list, timeout: float = 20.0) -> Any:
    try:
        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise IntentsError(f"the {method} call to the EVM endpoint failed ({e.__class__.__name__})") from e
    if body.get("error"):
        raise IntentsError(f"{method}: {str(body['error'].get('message') if isinstance(body['error'], dict) else body['error'])[:200]}")
    return body.get("result")


def evm_address(key: str) -> str:
    from eth_account import Account
    try:
        return Account.from_key(str(key).strip()).address.lower()
    except Exception as e:
        raise IntentsError("EMPORIUM_BUYER_KEY is not a valid EVM private key") from e


def evm_pay(client: OneClick, quote: Quote, key: str, *, books: Optional[Books] = None, rpc_url: str = "",
            wait: float = 120.0, poll: float = 2.0) -> dict:
    """Send a fund quote's deposit from the buyer's own EVM wallet (EMPORIUM_BUYER_KEY): an ERC-20
    ``transfer`` (or plain ETH) of exactly ``amountIn`` to the deposit address. The wallet needs the token
    and a little of the chain's gas coin. The signed transaction is kept before it's broadcast and a retry
    rebroadcasts that same one (its nonce lets the chain take it once), so a deposit is never paid twice."""
    from eth_account import Account
    if quote.kind != "fund" or quote.dry:
        raise IntentsError("only a live fund quote is paid from the wallet")
    chain = quote.origin.chain
    chain_id = EVM_CHAINS.get(chain)
    url = rpc_url or EVM_RPC.get(chain, "")
    if not chain_id or not url:
        raise IntentsError(f"paying from a wallet works on {', '.join(sorted(EVM_RPC))}; pay this "
                           f"{chain_label(chain)} deposit by hand")
    acct = Account.from_key(str(key).strip())
    sender = acct.address.lower()
    if quote.request["refundTo"].lower() != sender:
        raise IntentsError("this quote refunds to another address than the wallet that would pay it")
    deposit, amount = quote.deposit_address, quote.amount_in
    books = books or Books()
    row = books.get(deposit) or record(books, quote)
    raw, tx_hash = row.get("signed") or "", row.get("deposit_tx") or ""
    if not raw:
        if (parse_iso(quote.pay_by) or 0) - time.time() < 300:
            raise IntentsError("this quote's deadline is too close to pay it safely; ask for a new one")
        token = quote.origin.contract
        if token:
            data = _ERC20_TRANSFER + deposit.lower()[2:].rjust(64, "0") + format(amount, "x").rjust(64, "0")
            tx = {"to": _checksum(token), "value": 0, "data": data}
            held = int(_evm(url, "eth_call", [{"to": token, "data": _ERC20_BALANCE + sender[2:].rjust(64, "0")}, "latest"]), 16)
        else:
            tx = {"to": _checksum(deposit), "value": amount, "data": "0x"}
            held = int(_evm(url, "eth_getBalance", [sender, "latest"]), 16)
        if held < amount:
            raise IntentsError(f"the wallet {sender} holds {fmt_amount(held, quote.origin.decimals)} {quote.origin.symbol}, "
                               f"less than the {fmt_amount(amount, quote.origin.decimals)} to deposit")
        gas = int(_evm(url, "eth_estimateGas", [{"from": sender, "to": tx["to"], "value": hex(tx["value"]), "data": tx["data"]}]), 16)
        base = int((_evm(url, "eth_getBlockByNumber", ["latest", False]) or {}).get("baseFeePerGas", "0x0"), 16)
        try:
            tip = int(_evm(url, "eth_maxPriorityFeePerGas", []), 16)
        except IntentsError:
            tip = 10**8
        tx.update({"chainId": chain_id, "nonce": int(_evm(url, "eth_getTransactionCount", [sender, "pending"]), 16),
                   "gas": gas * 12 // 10, "maxPriorityFeePerGas": tip, "maxFeePerGas": base * 2 + tip, "type": 2})
        signed = acct.sign_transaction(tx)
        raw_bytes = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        raw = "0x" + bytes(raw_bytes).hex()
        tx_hash = "0x" + bytes(signed.hash).hex()
        books.update(deposit, signed=raw, deposit_tx=tx_hash, status="signed")  # kept before it's broadcast
    try:
        _evm(url, "eth_sendRawTransaction", [raw])
    except IntentsError as e:
        if not re.search(r"already known|nonce too low|already imported", str(e), re.I):
            raise
    books.update(deposit, status="sending")
    receipt, end = None, time.time() + wait
    while time.time() < end:
        receipt = _evm(url, "eth_getTransactionReceipt", [tx_hash])
        if receipt:
            break
        time.sleep(poll)
    if not receipt:
        return {"deposit": deposit, "tx": tx_hash, "status": "sending", "confirmed": False}
    if str(receipt.get("status")) != "0x1":
        books.update(deposit, status="failed", error="the deposit transaction reverted")
        raise IntentsError(f"the deposit transaction {tx_hash} reverted")
    try:
        client.submit_deposit(tx_hash, deposit)
    except IntentsError as e:
        logger.info("intents: deposit notice for %s: %s", deposit, e)
    books.update(deposit, status="sent")
    return {"deposit": deposit, "tx": tx_hash, "status": "PENDING_DEPOSIT", "confirmed": True}


def _checksum(address: str) -> str:
    from eth_utils import to_checksum_address
    return to_checksum_address(address)


# ---- the words people type ------------------------------------------------------------------------
def parse_words(words: list[str]) -> dict:
    """``5 from base:usdc [refund 0x…] [to acct.near] [send]`` (fund) or ``5 to base:usdc 0x… [min 4.9]``
    (cash-out) -> {usd, direction, token, recipient, refund, send, slippage, max_fee}. Case is kept: Solana
    and other addresses are case-sensitive."""
    if not words:
        raise IntentsError("give an amount, e.g. 5 from base:usdc")
    out: dict[str, Any] = {"usd": words[0], "direction": "", "token": "", "recipient": "", "refund": "",
                           "send": False, "slippage": None, "max_fee": None, "min": None}
    usd_atomic(out["usd"])
    i = 1
    while i < len(words):
        w = words[i].lower()
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if w in ("from", "to") and not out["direction"]:
            if not nxt:
                raise IntentsError(f"'{w}' needs a token, e.g. {w} base:usdc")
            out["direction"], out["token"] = w, nxt
            i += 2
            if w == "to" and i < len(words) and words[i].lower() not in ("refund", "min", "slippage", "max-fee", "send"):
                out["recipient"] = words[i]
                i += 1
            continue
        if w in ("refund", "to", "min", "slippage", "max-fee", "maxfee") and nxt:
            key = {"to": "recipient", "maxfee": "max_fee", "max-fee": "max_fee"}.get(w, w)
            out[key] = nxt
            i += 2
            continue
        if w == "send":
            out["send"] = True
            i += 1
            continue
        raise IntentsError(f"didn't understand {words[i]!r}")
    if not out["direction"]:
        raise IntentsError("say where the money comes from (from base:usdc) or goes to (to base:usdc <address>)")
    for k, lo, hi in (("slippage", 0, 5000), ("max_fee", 0, 10000)):
        if out[k] is not None:
            try:
                out[k] = int(str(out[k]).rstrip("bps"))
            except ValueError as e:
                raise IntentsError(f"{k} is a number of basis points (100 = 1%)") from e
            if not lo <= out[k] <= hi:
                raise IntentsError(f"{k} must be between {lo} and {hi} basis points")
    return out


def jwt_from(get_secret: Optional[Callable[[str], Optional[str]]] = None) -> str:
    try:
        return (get_secret(JWT_SECRET) if get_secret else "") or ""
    except Exception:
        return ""
