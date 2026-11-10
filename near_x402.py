"""x402's "exact" scheme on NEAR, for all three x402 roles, registered with the official x402 SDK the
same way its own EVM and SVM mechanisms are.

The wire format is the x402 v2 spec's (specs/schemes/exact/scheme_exact_near.md in
x402-foundation/x402), so these interoperate with the other implementations (FastNEAR's @x402/near,
r402-near):
- the requirements name a NEP-141 ``asset`` (USDC), ``payTo`` and ``amount``, and carry no ``extra``;
- the payload is ``{"signedDelegateAction": base64(borsh(SignedDelegateAction))}``: a NEP-366 delegate
  in which the buyer signs exactly one ``ft_transfer`` of that amount to the shop.

Nothing moves until a relayer submits the delegate. A shop is its own facilitator: it checks the
delegate against the requirements and the chain (verify), runs the job, and only then submits the
delegate in a transaction from its relayer account, which pays the gas (settle). The buyer pays USDC
and needs no NEAR, and "charged only on delivery" holds on NEAR as it does on Base.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from typing import Any, Iterable, Optional

from . import near
from .near import (DelegateAction, FunctionCall, KeyPair, Rpc, RpcError, SignedDelegateAction, Transaction,
                   is_account_id)
from .shop import RPC_URLS, USDC

logger = logging.getLogger(__name__)

SCHEME = "exact"
NETWORKS = ("near:testnet", "near:mainnet")
FT_TRANSFER_GAS = 30 * near.TGAS
MAX_SPONSORED_GAS = 100 * near.TGAS
USDC_DECIMALS = 6
ESTIMATED_BLOCK_SECONDS = 1       # the spec turns maxTimeoutSeconds into blocks at one block a second
_EMPTY_CODE = "11111111111111111111111111111111"
_AMOUNT = re.compile(r"^[1-9]\d{0,38}$")


def timeout_blocks(max_timeout_seconds: Any) -> int:
    return max(1, math.ceil(int(max_timeout_seconds) / ESTIMATED_BLOCK_SECONDS))


def window_for(seconds: float, rpc: Optional[Rpc] = None) -> int:
    """The ``maxTimeoutSeconds`` to ask for so a delegate stays valid for ``seconds`` of wall time. The
    spec counts one block a second; NEAR makes blocks faster than that (testnet ~0.6 s), so the window
    is stated in blocks at the measured rate, with a quarter more to spare."""
    secs = near.block_seconds(rpc) if rpc is not None else 0.5
    return int(math.ceil(seconds / secs * 1.25))


def signed_delegate(payload: Any) -> SignedDelegateAction:
    body = getattr(payload, "payload", None)
    if not isinstance(body, dict):
        raise ValueError("no payload")
    return SignedDelegateAction.from_base64(body.get("signedDelegateAction"))


def payer_of(payload: Any) -> str:
    try:
        return signed_delegate(payload).delegate_action.sender_id
    except Exception:
        return ""


def nonce_of(payload: Any) -> str:
    """One signed payment buys one job: the delegate's (sender, key, nonce) is spent by the chain once."""
    try:
        d = signed_delegate(payload).delegate_action
        return f"near:{d.sender_id}:{d.public_key}:{d.nonce}"
    except Exception:
        return ""


def expires_in(payload: Any, rpc: Rpc) -> Optional[float]:
    """About how many seconds this payment's delegate can still be submitted, at the chain's pace."""
    try:
        d = signed_delegate(payload).delegate_action
        head = int(rpc.final_block()["height"])
        return (d.max_block_height - head) * near.block_seconds(rpc)
    except Exception:
        return None


# A NEAR access key's nonces must land in order, so one key pays for one job at a time.
_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


def key_lock(account_id: str, public_key: Any) -> threading.Lock:
    with _KEY_LOCKS_GUARD:
        return _KEY_LOCKS.setdefault(f"{account_id}/{public_key}", threading.Lock())


def _rpc_for(network: str, rpc_urls: Optional[dict] = None) -> Rpc:
    url = (rpc_urls or {}).get(network) or RPC_URLS.get(network, "")
    return Rpc(url)


# ---- buyer -------------------------------------------------------------------------------------
class ExactNearClient:
    """Signs the payment: a delegate whose one action is ``ft_transfer(payTo, amount)`` on the asset,
    valid for the requirements' window, with the key's next nonce. Implements SchemeNetworkClient."""

    scheme = SCHEME

    def __init__(self, account_id: str, key: KeyPair, rpc_urls: Optional[dict] = None):
        if not is_account_id(account_id):
            raise ValueError(f"{account_id!r} is not a NEAR account id")
        self.account_id, self.key, self._rpc_urls = account_id, key, rpc_urls or {}

    def create_payment_payload(self, requirements: Any) -> dict[str, Any]:
        network = str(requirements.network)
        asset, pay_to, amount = str(requirements.asset), str(requirements.pay_to), str(requirements.amount)
        if not (is_account_id(asset) and is_account_id(pay_to) and _AMOUNT.match(amount)):
            raise ValueError("the payment requirements don't name a NEAR token, payee and amount")
        rpc = _rpc_for(network, self._rpc_urls)
        access = rpc.access_key(self.account_id, str(self.key.public_key))
        if access is None:
            raise ValueError(f"{self.account_id} has no access key {self.key.public_key} on {network}")
        if access.get("permission") != "FullAccess":
            raise ValueError("a NEP-141 transfer attaches 1 yoctoNEAR, which only a full-access key can sign")
        head = int(rpc.final_block()["height"])
        args = json.dumps({"receiver_id": pay_to, "amount": amount}, separators=(",", ":")).encode()
        delegate = DelegateAction(
            sender_id=self.account_id, receiver_id=asset,
            actions=(FunctionCall("ft_transfer", args, FT_TRANSFER_GAS, 1),),
            nonce=int(access["nonce"]) + 1,
            max_block_height=head + timeout_blocks(requirements.max_timeout_seconds),
            public_key=self.key.public_key)
        return {"signedDelegateAction": delegate.sign(self.key).to_base64()}


# ---- shop: pricing -----------------------------------------------------------------------------
class ExactNearServer:
    """Prices in USDC on NEAR and writes the requirements. Implements SchemeNetworkServer."""

    scheme = SCHEME
    default_asset_transfer_method = "default"
    # Verify before the job, settle after it: "charged only on delivery".
    payment_flows = {"default": {"supported": ("authorization",), "default": "authorization"}}

    def get_asset_decimals(self, asset: str, network: Any) -> Optional[int]:
        return USDC_DECIMALS if asset == USDC.get(str(network)) else None

    def parse_price(self, price: Any, network: Any) -> Any:
        from x402.schemas import AssetAmount
        from x402.schemas.helpers import convert_to_token_amount, parse_money

        if isinstance(price, AssetAmount) or (isinstance(price, dict) and "amount" in price):
            amount = price.amount if isinstance(price, AssetAmount) else price["amount"]
            asset = price.asset if isinstance(price, AssetAmount) else price.get("asset")
            if not asset:
                raise ValueError(f"a NEP-141 token account is required for an AssetAmount on {network}")
            return AssetAmount(amount=str(amount), asset=str(asset), extra={})
        parsed = parse_money(price)
        if parsed.get("symbol") not in (None, "", "USD", "USDC"):
            raise ValueError(f"only USD prices are supported on {network}")
        asset = USDC.get(str(network))
        if not asset:
            raise ValueError(f"no USDC contract known for {network}")
        return AssetAmount(amount=str(convert_to_token_amount(parsed["amount"], USDC_DECIMALS)), asset=asset, extra={})

    def enhance_payment_requirements(self, requirements: Any, supported_kind: Any, extension_keys: list) -> Any:
        if not requirements.asset:
            requirements.asset = USDC.get(str(requirements.network), "")
        requirements.extra = {}  # this scheme carries no extra (r402-near refuses unknown keys)
        return requirements


# ---- shop: verifying and settling --------------------------------------------------------------
class _Invalid(Exception):
    def __init__(self, reason: str, message: str = "", payer: str = ""):
        super().__init__(message or reason)
        self.reason, self.message, self.payer = reason, message or reason, payer


class ExactNearFacilitator:
    """Verifies a buyer's signed delegate against the requirements and the chain, and settles it by
    submitting it from the relayer account (which pays the gas). Implements SchemeNetworkFacilitator.

    Settling is idempotent per delegate: a retry finds the transaction it already sent and reports
    that transaction's outcome, so one signed payment can never be submitted twice."""

    scheme = SCHEME
    caip_family = "near:*"

    def __init__(self, relayer_id: str, relayer_key: KeyPair, rpc_urls: Optional[dict] = None,
                 max_gas: int = MAX_SPONSORED_GAS, wait_seconds: float = 90.0):
        if not is_account_id(relayer_id):
            raise ValueError(f"{relayer_id!r} is not a NEAR account id")
        self.relayer_id, self._key, self._rpc_urls = relayer_id, relayer_key, rpc_urls or {}
        self.max_gas, self.wait_seconds = max_gas, wait_seconds
        self._lock = threading.Lock()          # one relayer transaction at a time: its nonces land in order
        self._nonce: dict[str, int] = {}       # network -> last nonce this relayer key used
        self._sent: dict[str, str] = {}        # sha256(signed delegate) -> the tx hash that carried it

    def get_extra(self, network: Any) -> Optional[dict]:
        return None

    def get_signers(self, network: Any) -> list[str]:
        return [self.relayer_id]

    # -- verify --
    def _check(self, payload: Any, requirements: Any, rpc: Rpc) -> SignedDelegateAction:
        accepted = getattr(payload, "accepted", None)
        if int(getattr(payload, "x402_version", 0) or 0) != 2:
            raise _Invalid("invalid_x402_version", "the NEAR exact scheme is x402 v2 only")
        if str(requirements.scheme) != SCHEME or accepted is None or str(accepted.scheme) != SCHEME:
            raise _Invalid("unsupported_scheme")
        network = str(requirements.network)
        if network not in NETWORKS or str(accepted.network) != network:
            raise _Invalid("network_mismatch")
        if (str(accepted.asset), str(accepted.pay_to), str(accepted.amount)) != \
                (str(requirements.asset), str(requirements.pay_to), str(requirements.amount)):
            raise _Invalid("requirements_mismatch", "the payment was signed for different terms")
        try:
            max_timeout = int(requirements.max_timeout_seconds)
        except (TypeError, ValueError):
            max_timeout = 0
        if max_timeout <= 0 or not _AMOUNT.match(str(requirements.amount)):
            raise _Invalid("invalid_requirements")
        try:
            sda = signed_delegate(payload)
        except Exception as e:
            raise _Invalid("invalid_payload", f"not a signed delegate action ({e})") from e
        d = sda.delegate_action
        payer = d.sender_id
        if payer == self.relayer_id:
            raise _Invalid("relayer_is_payer", "the relayer can't pay itself", payer)
        if d.public_key.key_type != near.ED25519 or sda.signature_type != near.ED25519:
            raise _Invalid("unsupported_key_type", "only ed25519 keys are accepted", payer)
        if not sda.verify():
            raise _Invalid("invalid_signature", "the delegate's signature doesn't match its key", payer)
        if len(d.actions) != 1:
            raise _Invalid("invalid_action_count", "a payment is exactly one ft_transfer", payer)
        call = d.actions[0]
        if call.method_name != "ft_transfer":
            raise _Invalid("invalid_action", "a payment is exactly one ft_transfer", payer)
        if d.receiver_id != str(requirements.asset):
            raise _Invalid("asset_mismatch", "the transfer is on a different token", payer)
        try:
            args = call.json_args()
        except ValueError as e:
            raise _Invalid("invalid_payload", "the transfer's arguments aren't JSON", payer) from e
        if not isinstance(args, dict) or set(args) - {"receiver_id", "amount", "memo"} or \
                not isinstance(args.get("memo", ""), (str, type(None))):
            raise _Invalid("invalid_action", "unexpected transfer arguments", payer)
        if args.get("receiver_id") != str(requirements.pay_to):
            raise _Invalid("recipient_mismatch", "the transfer pays someone else", payer)
        if str(args.get("amount")) != str(requirements.amount):
            raise _Invalid("amount_mismatch", "the transfer is for a different amount", payer)
        if call.deposit != 1:
            raise _Invalid("invalid_deposit", "an ft_transfer attaches exactly 1 yoctoNEAR", payer)
        if not 0 < call.gas <= self.max_gas:
            raise _Invalid("gas_above_policy", f"gas must be at most {self.max_gas // near.TGAS} TGas", payer)

        head = int(rpc.final_block()["height"])
        left = d.max_block_height - head
        if left <= 0:
            raise _Invalid("delegate_action_expired", "the signed payment has expired", payer)
        if left > timeout_blocks(max_timeout):
            raise _Invalid("delegate_action_timeout_window_exceeds_maxTimeout", "the payment's window is longer than asked", payer)
        access = rpc.access_key(payer, str(d.public_key))
        if access is None:
            raise _Invalid("unknown_access_key", f"{payer} has no such key", payer)
        if d.nonce <= int(access.get("nonce", 0)):
            raise _Invalid("delegate_action_nonce_already_used", "this signed payment's nonce was already used", payer)
        if d.nonce >= head * 1_000_000:
            raise _Invalid("delegate_action_nonce_too_large", "the nonce is out of range", payer)
        if access.get("permission") != "FullAccess":
            raise _Invalid("access_key_not_full_access", "a function-call key can't attach the 1 yoctoNEAR deposit", payer)

        token = rpc.account(d.receiver_id)
        if not token or token.get("code_hash") in (None, "", _EMPTY_CODE):
            raise _Invalid("asset_not_a_contract", "the token account has no contract", payer)
        try:
            balance = int(str(rpc.view(d.receiver_id, "ft_balance_of", {"account_id": payer})))
        except (ValueError, TypeError) as e:
            raise _Invalid("insufficient_funds", "couldn't read the payer's balance", payer) from e
        if balance < int(requirements.amount):
            raise _Invalid("insufficient_funds", "the payer's USDC balance is too low", payer)
        if rpc.view(d.receiver_id, "storage_balance_of", {"account_id": str(requirements.pay_to)}) is None:
            raise _Invalid("recipient_not_registered", "the shop isn't registered with the token contract", payer)
        return sda

    def verify(self, payload: Any, requirements: Any, context: Any = None) -> Any:
        from x402.schemas import VerifyResponse
        try:
            sda = self._check(payload, requirements, _rpc_for(str(requirements.network), self._rpc_urls))
        except _Invalid as e:
            return VerifyResponse(is_valid=False, invalid_reason=e.reason, invalid_message=e.message, payer=e.payer)
        except RpcError as e:
            return VerifyResponse(is_valid=False, invalid_reason="rpc_unavailable", invalid_message=str(e)[:200],
                                  payer=payer_of(payload))
        return VerifyResponse(is_valid=True, payer=sda.delegate_action.sender_id)

    # -- settle --
    def _submit(self, rpc: Rpc, network: str, sda: SignedDelegateAction) -> str:
        """Send the relayer transaction carrying the delegate; returns its hash once the chain has it."""
        pk = str(self._key.public_key)
        for attempt in (1, 2):
            nonce = self._nonce.get(network)
            if nonce is None or attempt == 2:
                access = rpc.access_key(self.relayer_id, pk)
                if access is None:
                    raise RpcError("RELAYER_KEY_UNKNOWN", f"{self.relayer_id} has no key {pk}")
                nonce = int(access["nonce"])
            block_hash = near.b58decode(rpc.final_block()["hash"])
            tx = Transaction(self.relayer_id, self._key.public_key, nonce + 1, sda.delegate_action.sender_id,
                             block_hash, (near.Delegate(sda),))
            tx_hash, signed = tx.sign(self._key)
            try:
                rpc.send_tx(signed, wait_until="INCLUDED", timeout=30.0)
            except RpcError as e:
                if e.name in ("TIMEOUT_ERROR", "RPC_UNREACHABLE"):
                    self._nonce[network] = nonce + 1  # it may well have landed; the outcome check decides
                    return tx_hash
                if attempt == 1 and "nonce" in str(e.data or e).lower():
                    continue  # another process used the relayer key; read its nonce and try once more
                raise
            self._nonce[network] = nonce + 1
            return tx_hash
        raise RpcError("INVALID_NONCE", "the relayer's nonce kept moving")

    def _outcome(self, rpc: Rpc, tx_hash: str) -> Optional[dict]:
        deadline = time.time() + self.wait_seconds
        while time.time() < deadline:
            try:
                out = rpc.tx_status(tx_hash, self.relayer_id, wait_until="FINAL", timeout=30.0)
                if near.final(out):
                    return out
            except RpcError as e:
                if e.name not in ("UNKNOWN_TRANSACTION", "TIMEOUT_ERROR", "RPC_UNREACHABLE", "REQUEST_VALIDATION_ERROR"):
                    logger.info("near: status of %s: %s", tx_hash, e)
            time.sleep(1.5)
        return None

    def settle(self, payload: Any, requirements: Any, context: Any = None) -> Any:
        from x402.schemas import SettleResponse
        network = str(requirements.network)

        def fail(reason: str, message: str = "", tx: str = "", payer: str = "") -> Any:
            return SettleResponse(success=False, error_reason=reason, error_message=message[:300] or reason,
                                  transaction=tx, network=network, payer=payer or None)

        try:
            sda = signed_delegate(payload)
        except Exception:
            return fail("invalid_payload")
        payer = sda.delegate_action.sender_id
        key = hashlib.sha256(sda.to_borsh()).hexdigest()
        rpc = _rpc_for(network, self._rpc_urls)
        with self._lock:
            tx_hash = self._sent.get(key)
            if not tx_hash:
                try:
                    self._check(payload, requirements, rpc)  # the chain may have moved since verify
                except _Invalid as e:
                    return fail(e.reason, e.message, payer=payer)
                except RpcError as e:
                    return fail("rpc_unavailable", str(e), payer=payer)
                try:
                    tx_hash = self._submit(rpc, network, sda)
                except RpcError as e:
                    return fail("settlement_failed", str(e), payer=payer)
                self._sent[key] = tx_hash
        outcome = self._outcome(rpc, tx_hash)
        if outcome is None:
            return fail("settlement_pending", "the transaction hasn't finalized yet", tx_hash, payer)
        moved = near.paid(outcome, str(requirements.asset), payer, str(requirements.pay_to))
        if moved != int(requirements.amount):
            why = near.failures(outcome)
            return fail("transfer_failed", json.dumps(why)[:300] if why else "no matching ft_transfer in the outcome",
                        tx_hash, payer)
        return SettleResponse(success=True, transaction=tx_hash, network=network, payer=payer,
                              amount=str(requirements.amount))


# ---- registration ------------------------------------------------------------------------------
def register_exact_near_server(server: Any, networks: Iterable[str]) -> Any:
    scheme = ExactNearServer()
    for n in networks:
        server.register(n, scheme)
    return server


def register_exact_near_client(client: Any, account_id: str, key: KeyPair, networks: Iterable[str],
                               rpc_urls: Optional[dict] = None) -> Any:
    scheme = ExactNearClient(account_id, key, rpc_urls)
    for n in networks:
        client.register(n, scheme)
    return client


def near_facilitator(relayer_id: str, relayer_key: KeyPair, networks: Iterable[str],
                     rpc_urls: Optional[dict] = None) -> Any:
    """An in-process x402 facilitator for NEAR, usable wherever the SDK takes a facilitator client."""
    from x402 import x402FacilitatorSync
    fac = x402FacilitatorSync()
    fac.register(list(networks), ExactNearFacilitator(relayer_id, relayer_key, rpc_urls))
    return fac
