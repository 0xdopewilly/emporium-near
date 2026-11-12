"""NEAR, from first principles: the few protocol pieces Emporium needs to take and make x402
payments on NEAR, with no NEAR SDK.

- base58 and NEAR's key strings (``ed25519:…``);
- Borsh encoding of the types involved: a FunctionCall action, a NEP-366 delegate action (the
  buyer's signed ``ft_transfer``) and the relayer's outer transaction that carries it;
- NEP-461 signing and checking of delegate actions (ed25519, through ``cryptography``);
- a small JSON-RPC client over httpx, and helpers that read a transaction's outcome.

The encodings follow nearcore's ``core/primitives``. tests/test_near_codec.py pins them to bytes that
near-api-js produced, so a delegate signed here is the delegate any NEAR wallet would sign.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

ED25519, SECP256K1 = 0, 1
_KEY_BYTES = {ED25519: 32, SECP256K1: 64}
_SIG_BYTES = {ED25519: 64, SECP256K1: 65}
NEP366_PREFIX = 2**30 + 366         # NEP-461 discriminant that prefixes a delegate action before hashing
ACTION_FUNCTION_CALL, ACTION_DELEGATE = 2, 8
TGAS = 10**12

# 2-64 chars; lowercase letters and digits in parts joined by "-" or "_", and parts joined by ".".
# Named accounts (ada.testnet) and implicit ones (64 hex chars) both match.
ACCOUNT_ID = re.compile(r"^(?=.{2,64}$)(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$")
TX_HASH = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{43,44}$")


def is_account_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ACCOUNT_ID.match(value))


# ---- base58 ------------------------------------------------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(_B58[r])
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + "".join(reversed(out))


def b58decode(text: str) -> bytes:
    n = 0
    for c in text:
        if c not in _B58_INDEX:
            raise ValueError("not base58")
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


# ---- keys --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PublicKey:
    key_type: int
    data: bytes

    @classmethod
    def parse(cls, text: str) -> "PublicKey":
        kind, _, body = str(text or "").strip().partition(":")
        if kind != "ed25519" or not body:
            raise ValueError("expected an ed25519:… public key")
        data = b58decode(body)
        if len(data) != 32:
            raise ValueError("an ed25519 public key is 32 bytes")
        return cls(ED25519, data)

    def __str__(self) -> str:
        return ("ed25519:" if self.key_type == ED25519 else "secp256k1:") + b58encode(self.data)

    def to_borsh(self) -> bytes:
        return bytes([self.key_type]) + self.data

    def verify(self, signature: bytes, message: bytes) -> bool:
        if self.key_type != ED25519 or len(signature) != 64:
            return False
        try:
            Ed25519PublicKey.from_public_bytes(self.data).verify(signature, message)
            return True
        except (InvalidSignature, ValueError):
            return False


class KeyPair:
    """An ed25519 key from NEAR's secret-key string: ``ed25519:`` + base58 of seed+public (64 bytes,
    what near-cli and wallets export) or of the 32-byte seed alone."""

    def __init__(self, secret: str):
        kind, _, body = str(secret or "").strip().partition(":")
        if kind != "ed25519" or not body:
            raise ValueError("expected an ed25519:… secret key")
        raw = b58decode(body)
        if len(raw) not in (32, 64):
            raise ValueError("an ed25519 secret key is 32 or 64 bytes")
        self._sk = Ed25519PrivateKey.from_private_bytes(raw[:32])
        pub = self._sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if len(raw) == 64 and raw[32:] != pub:
            raise ValueError("the secret key's public half doesn't match it")
        self.public_key = PublicKey(ED25519, pub)

    @classmethod
    def generate(cls) -> "KeyPair":
        seed = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        return cls("ed25519:" + b58encode(seed))

    def secret(self) -> str:
        seed = self._sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        return "ed25519:" + b58encode(seed + self.public_key.data)

    def sign(self, message: bytes) -> bytes:
        return self._sk.sign(message)

    def __repr__(self) -> str:  # never the secret
        return f"KeyPair({self.public_key})"


# ---- Borsh -------------------------------------------------------------------------------------
def _u32(v: int) -> bytes:
    return int(v).to_bytes(4, "little")


def _u64(v: int) -> bytes:
    return int(v).to_bytes(8, "little")


def _u128(v: int) -> bytes:
    return int(v).to_bytes(16, "little")


def _string(s: str) -> bytes:
    b = s.encode("utf-8")
    return _u32(len(b)) + b


def _blob(b: bytes) -> bytes:
    return _u32(len(b)) + b


class _Reader:
    """Strict Borsh reading: every length is bounded and nothing may trail the value."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError("truncated")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def u128(self) -> int:
        return int.from_bytes(self.take(16), "little")

    def blob(self, limit: int) -> bytes:
        n = self.u32()
        if n > limit:
            raise ValueError("field too long")
        return self.take(n)

    def string(self, limit: int = 256) -> str:
        return self.blob(limit).decode("utf-8")

    def account_id(self) -> str:
        s = self.string(64)
        if not is_account_id(s):
            raise ValueError("not a NEAR account id")
        return s

    def public_key(self) -> PublicKey:
        kind = self.u8()
        if kind not in _KEY_BYTES:
            raise ValueError("unknown key type")
        return PublicKey(kind, self.take(_KEY_BYTES[kind]))

    def signature(self) -> tuple[int, bytes]:
        kind = self.u8()
        if kind not in _SIG_BYTES:
            raise ValueError("unknown signature type")
        return kind, self.take(_SIG_BYTES[kind])

    def end(self) -> None:
        if self.pos != len(self.data):
            raise ValueError("trailing bytes")


@dataclass(frozen=True)
class FunctionCall:
    method_name: str
    args: bytes
    gas: int
    deposit: int

    def to_borsh(self) -> bytes:
        return (bytes([ACTION_FUNCTION_CALL]) + _string(self.method_name) + _blob(self.args)
                + _u64(self.gas) + _u128(self.deposit))

    def json_args(self) -> Any:
        return json.loads(self.args.decode("utf-8"))


@dataclass(frozen=True)
class DelegateAction:
    """NEP-366: actions a sender signed for someone else (a relayer) to submit and pay gas for."""

    sender_id: str
    receiver_id: str
    actions: tuple[FunctionCall, ...]
    nonce: int
    max_block_height: int
    public_key: PublicKey

    def to_borsh(self) -> bytes:
        return (_string(self.sender_id) + _string(self.receiver_id) + _u32(len(self.actions))
                + b"".join(a.to_borsh() for a in self.actions) + _u64(self.nonce) + _u64(self.max_block_height)
                + self.public_key.to_borsh())

    def signing_hash(self) -> bytes:
        """What the sender signs (NEP-461): sha256 over the discriminant, then the delegate action."""
        return hashlib.sha256(_u32(NEP366_PREFIX) + self.to_borsh()).digest()

    def sign(self, key: KeyPair) -> "SignedDelegateAction":
        if key.public_key != self.public_key:
            raise ValueError("signing with a key other than the delegate's own public key")
        return SignedDelegateAction(self, ED25519, key.sign(self.signing_hash()))


@dataclass(frozen=True)
class SignedDelegateAction:
    delegate_action: DelegateAction
    signature_type: int
    signature: bytes

    def to_borsh(self) -> bytes:
        return self.delegate_action.to_borsh() + bytes([self.signature_type]) + self.signature

    def to_base64(self) -> str:
        return base64.b64encode(self.to_borsh()).decode("ascii")

    def verify(self) -> bool:
        return (self.signature_type == self.delegate_action.public_key.key_type
                and self.delegate_action.public_key.verify(self.signature, self.delegate_action.signing_hash()))

    @classmethod
    def from_borsh(cls, data: bytes) -> "SignedDelegateAction":
        """Decode a signed delegate. Only FunctionCall actions are decoded; a delegate carrying any other
        action is refused here, which suits a payment (exactly one ft_transfer is allowed)."""
        r = _Reader(data)
        sender, receiver = r.account_id(), r.account_id()
        count = r.u32()
        if count > 16:
            raise ValueError("too many actions")
        actions = []
        for _ in range(count):
            tag = r.u8()
            if tag != ACTION_FUNCTION_CALL:
                raise ValueError(f"action type {tag} is not a function call")
            actions.append(FunctionCall(r.string(256), r.blob(16 * 1024), r.u64(), r.u128()))
        nonce, max_height, public_key = r.u64(), r.u64(), r.public_key()
        sig_type, sig = r.signature()
        r.end()
        return cls(DelegateAction(sender, receiver, tuple(actions), nonce, max_height, public_key), sig_type, sig)

    @classmethod
    def from_base64(cls, text: str) -> "SignedDelegateAction":
        if not isinstance(text, str) or len(text) > 32 * 1024:
            raise ValueError("not a signed delegate action")
        return cls.from_borsh(base64.b64decode(text, validate=True))


@dataclass(frozen=True)
class Delegate:
    """The relayer's action that submits a signed delegate."""

    signed: SignedDelegateAction

    def to_borsh(self) -> bytes:
        return bytes([ACTION_DELEGATE]) + self.signed.to_borsh()


# Account set-up actions (scripts/near_accounts.py makes a shop's accounts with these).
@dataclass(frozen=True)
class CreateAccount:
    def to_borsh(self) -> bytes:
        return bytes([0])


@dataclass(frozen=True)
class Transfer:
    deposit: int  # yoctoNEAR

    def to_borsh(self) -> bytes:
        return bytes([3]) + _u128(self.deposit)


@dataclass(frozen=True)
class AddFullAccessKey:
    public_key: PublicKey

    def to_borsh(self) -> bytes:
        return bytes([5]) + self.public_key.to_borsh() + _u64(0) + bytes([1])  # AccessKey{nonce 0, FullAccess}


@dataclass(frozen=True)
class Transaction:
    signer_id: str
    public_key: PublicKey
    nonce: int
    receiver_id: str
    block_hash: bytes
    actions: tuple[Any, ...]

    def to_borsh(self) -> bytes:
        return (_string(self.signer_id) + self.public_key.to_borsh() + _u64(self.nonce) + _string(self.receiver_id)
                + self.block_hash + _u32(len(self.actions)) + b"".join(a.to_borsh() for a in self.actions))

    def hash(self) -> bytes:
        return hashlib.sha256(self.to_borsh()).digest()

    def sign(self, key: KeyPair) -> tuple[str, str]:
        """(tx hash in base58, the signed transaction in base64 for send_tx)."""
        digest = self.hash()
        signed = self.to_borsh() + bytes([ED25519]) + key.sign(digest)
        return b58encode(digest), base64.b64encode(signed).decode("ascii")


# ---- JSON-RPC ----------------------------------------------------------------------------------
class RpcError(Exception):
    """An RPC refusal. ``name`` is NEAR's error cause (UNKNOWN_ACCESS_KEY, TIMEOUT_ERROR, …)."""

    def __init__(self, name: str, message: str = "", data: Any = None):
        super().__init__(f"{name}: {message}"[:300] if message else name)
        self.name, self.data = name, data


_RATE_LIMITED = re.compile(r"rate.?limit|too many requests", re.I)
# A provider's own plan limits (dRPC's "Request timeout on the free plan, please upgrade"): the request may
# have been worked on, so only reads move on to the next endpoint.
_PLAN_LIMIT = re.compile(r"free plan|free tier|upgrade to (?:a )?paid|quota exceeded", re.I)


class Rpc:
    """NEAR JSON-RPC over httpx: the handful of calls a payment needs.

    ``url`` may name several endpoints, comma-separated. A call an endpoint refuses for its rate limit, or
    that can't reach it or answers with its plan's limits, goes to the next one; when every endpoint has refused, it waits a moment (longer
    each round) and goes round again. A transaction sent to an endpoint that didn't answer is not sent
    again here: it may already be on its way, and the relayer's settle looks it up instead."""

    def __init__(self, url: str, timeout: float = 20.0, rounds: int = 4):
        urls = [u.strip() for u in str(url or "").split(",") if u.strip()]
        if not urls:
            raise ValueError("no NEAR RPC endpoint configured")
        self.urls, self.url, self.timeout, self.rounds = urls, urls[0], timeout, max(1, rounds)

    def _post(self, url: str, payload: dict, timeout: float) -> Any:
        try:
            r = httpx.post(url, json=payload, timeout=timeout)
        except httpx.HTTPError as e:
            raise RpcError("RPC_UNREACHABLE", e.__class__.__name__) from e
        if r.status_code == 429:
            raise RpcError("RATE_LIMITED", f"HTTP 429 from {url}")
        try:
            body = r.json()
        except ValueError as e:
            raise RpcError("RPC_UNREACHABLE", f"HTTP {r.status_code}, not JSON") from e
        err = body.get("error") if isinstance(body, dict) else None
        if err and not isinstance(err, dict):  # a gateway's own error, not NEAR's {name, cause, data}
            text = str(err)
            if _RATE_LIMITED.search(text):
                raise RpcError("RATE_LIMITED", text, err)
            if _PLAN_LIMIT.search(text):
                raise RpcError("PROVIDER_LIMIT", text, err)
            raise RpcError("RPC_UNREACHABLE" if r.status_code >= 500 else "RPC_ERROR", text[:200], err)
        if err:
            cause = err.get("cause") or {}
            name = str(cause.get("name") or err.get("name") or "RPC_ERROR")
            text = str(err.get("data") or err.get("message") or "")
            if _RATE_LIMITED.search(text) or _RATE_LIMITED.search(name):
                raise RpcError("RATE_LIMITED", text, err)
            if _PLAN_LIMIT.search(text):
                raise RpcError("PROVIDER_LIMIT", text, err)
            raise RpcError(name, text, err)
        result = body.get("result")
        if isinstance(result, dict) and isinstance(result.get("error"), str):  # older nodes report view errors here
            raise RpcError("QUERY_ERROR", result["error"])
        return result

    def call(self, method: str, params: Any, timeout: Optional[float] = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": "emporium", "method": method, "params": params}
        last: Optional[RpcError] = None
        for n in range(self.rounds):
            if n:
                time.sleep(min(4.0, 0.5 * 2 ** (n - 1)))
            for url in self.urls:
                try:
                    return self._post(url, payload, timeout or self.timeout)
                except RpcError as e:
                    if e.name == "RATE_LIMITED" or (e.name in ("RPC_UNREACHABLE", "PROVIDER_LIMIT") and method != "send_tx"):
                        last = e
                        continue
                    raise
        raise last or RpcError("RPC_UNREACHABLE", "no endpoint answered")

    def final_block(self) -> dict:
        return self.call("block", {"finality": "final"})["header"]

    def access_key(self, account_id: str, public_key: str) -> Optional[dict]:
        """The key's {nonce, permission, block_height, block_hash}, or None when the account has no such key."""
        try:
            return self.call("query", {"request_type": "view_access_key", "finality": "final",
                                       "account_id": account_id, "public_key": public_key})
        except RpcError as e:
            if e.name in ("UNKNOWN_ACCESS_KEY", "UNKNOWN_ACCOUNT", "INVALID_ACCOUNT") or "does not exist" in str(e):
                return None
            raise

    def account(self, account_id: str) -> Optional[dict]:
        try:
            return self.call("query", {"request_type": "view_account", "finality": "final", "account_id": account_id})
        except RpcError as e:
            if e.name in ("UNKNOWN_ACCOUNT", "INVALID_ACCOUNT") or "does not exist" in str(e):
                return None
            raise

    def view(self, contract: str, method: str, args: dict) -> Any:
        res = self.call("query", {"request_type": "call_function", "finality": "final", "account_id": contract,
                                  "method_name": method,
                                  "args_base64": base64.b64encode(json.dumps(args).encode()).decode()})
        raw = bytes(res.get("result") or b"")
        return json.loads(raw.decode() or "null")

    def send_tx(self, signed_b64: str, wait_until: str = "FINAL", timeout: float = 60.0) -> dict:
        return self.call("send_tx", {"signed_tx_base64": signed_b64, "wait_until": wait_until}, timeout=timeout)

    def tx_status(self, tx_hash: str, sender_id: str, wait_until: str = "FINAL", timeout: float = 30.0) -> dict:
        return self.call("tx", {"tx_hash": tx_hash, "sender_account_id": sender_id, "wait_until": wait_until},
                         timeout=timeout)


def transact(rpc: Rpc, signer_id: str, key: KeyPair, receiver_id: str, actions: tuple, wait_until: str = "FINAL") -> dict:
    """Sign and send one ordinary transaction from ``signer_id`` (set-up and tooling; payments go
    through a relayer instead). Returns the final outcome; raises RpcError if the chain refuses it."""
    access = rpc.access_key(signer_id, str(key.public_key))
    if access is None:
        raise RpcError("UNKNOWN_ACCESS_KEY", f"{signer_id} has no key {key.public_key}")
    block_hash = b58decode(rpc.final_block()["hash"])
    _, signed = Transaction(signer_id, key.public_key, int(access["nonce"]) + 1, receiver_id, block_hash,
                            tuple(actions)).sign(key)
    return rpc.send_tx(signed, wait_until=wait_until, timeout=90.0)


NEAR = 10**24  # yoctoNEAR per NEAR

_BLOCK_TIME: dict[str, tuple[float, float]] = {}
_BLOCK_TIME_LOCK = threading.Lock()


def block_seconds(rpc: Rpc, sample: int = 600, ttl: float = 900.0) -> float:
    """Seconds per block lately (testnet is ~0.6 s), measured and cached. Clamped to a sane range."""
    with _BLOCK_TIME_LOCK:
        hit = _BLOCK_TIME.get(rpc.url)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        head = rpc.final_block()
        old = rpc.call("block", {"block_id": int(head["height"]) - sample})["header"]
        secs = (int(head["timestamp"]) - int(old["timestamp"])) / 1e9 / max(1, int(head["height"]) - int(old["height"]))
    except Exception:
        secs = 1.0
    secs = min(max(secs, 0.25), 2.0)
    with _BLOCK_TIME_LOCK:
        _BLOCK_TIME[rpc.url] = (time.time(), secs)
    return secs


# ---- reading outcomes --------------------------------------------------------------------------
def _receipts(outcome: dict) -> Iterable[dict]:
    for r in outcome.get("receipts_outcome") or []:
        yield r.get("outcome") or {}


def failures(outcome: dict) -> list[Any]:
    """Every failed receipt or transaction status in a final outcome (an empty list means it all ran)."""
    out = []
    status = outcome.get("status")
    if isinstance(status, dict) and "Failure" in status:
        out.append(status["Failure"])
    tx_status = (outcome.get("transaction_outcome") or {}).get("outcome", {}).get("status")
    if isinstance(tx_status, dict) and "Failure" in tx_status:
        out.append(tx_status["Failure"])
    for o in _receipts(outcome):
        st = o.get("status")
        if isinstance(st, dict) and "Failure" in st:
            out.append(st["Failure"])
    return out


def ft_transfers(outcome: dict, token: str) -> list[dict]:
    """NEP-141 ``ft_transfer`` events the token contract logged in this outcome:
    [{"old_owner_id", "new_owner_id", "amount", "memo"?}, …], from receipts that succeeded."""
    found = []
    for o in _receipts(outcome):
        if o.get("executor_id") != token:
            continue
        st = o.get("status")
        if isinstance(st, dict) and "Failure" in st:
            continue
        for line in o.get("logs") or []:
            if not isinstance(line, str) or not line.startswith("EVENT_JSON:"):
                continue
            try:
                event = json.loads(line[len("EVENT_JSON:"):])
            except ValueError:
                continue
            if event.get("standard") == "nep141" and event.get("event") == "ft_transfer":
                found.extend(d for d in event.get("data") or [] if isinstance(d, dict))
    return found


def paid(outcome: dict, token: str, payer: str, pay_to: str) -> int:
    """Atomic units of ``token`` this outcome moved from ``payer`` to ``pay_to`` (0 if anything failed)."""
    if failures(outcome):
        return 0
    total = 0
    for t in ft_transfers(outcome, token):
        if t.get("old_owner_id") == payer and t.get("new_owner_id") == pay_to:
            try:
                total += int(str(t.get("amount")))
            except ValueError:
                continue
    return total


def final(outcome: dict) -> bool:
    """Whether every receipt of this outcome has finished (the outcome is the last word)."""
    status = outcome.get("final_execution_status")
    return status in (None, "FINAL", "EXECUTED") and bool(outcome.get("status"))
