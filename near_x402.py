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
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Iterable, Optional

from . import near
from .near import (DelegateAction, FunctionCall, KeyPair, Rpc, RpcError, SignedDelegateAction, Transaction,
                   is_account_id)
from .shop import RPC_URLS, USDC

logger = logging.getLogger(__name__)

SCHEME = "exact"
NETWORKS = ("near:testnet", "near:mainnet")
FT_TRANSFER_GAS = 30 * near.TGAS
MAX_SPONSORED_GAS = 100 * near.TGAS
# Less than this and the token's receipt runs out of gas after the job is done: testnet USDC's ft_transfer
# burns 2.3 TGas, and a delegate signed with 1 TGas fails on chain with "Exceeded the prepaid gas"
# (interop/README.md). Both reference clients sign 30 TGas.
MIN_TRANSFER_GAS = 5 * near.TGAS
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


def _strict_object(pairs: list) -> dict:
    """A JSON object whose keys are all distinct: the token contract refuses ``{"amount":"1","amount":"5"}``
    ("duplicate field") while a lenient parser keeps the last one and would pass it."""
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is repeated")
    return dict(pairs)


def transfer_args(call: FunctionCall) -> Any:
    return json.loads(call.args.decode("utf-8"), object_pairs_hook=_strict_object)


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


def _gather(calls: dict[str, Callable[[], Any]]) -> dict[str, Future]:
    """Run independent RPC reads at once (each is a round trip); ``.result()`` re-raises a call's own error."""
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return {name: pool.submit(fn) for name, fn in calls.items()}


# ---- buyer -------------------------------------------------------------------------------------
class ExactNearClient:
    """Signs the payment: a delegate whose one action is ``ft_transfer(payTo, amount)`` on the asset,
    valid for the requirements' window, with the key's next nonce. Implements SchemeNetworkClient."""

    scheme = SCHEME

    def __init__(self, account_id: str, key: KeyPair, rpc_urls: Optional[dict] = None):
        if not is_account_id(account_id):
            raise ValueError(f"{account_id!r} is not a NEAR account id")
        self.account_id, self.key, self._rpc_urls = account_id, key, rpc_urls or {}

    @staticmethod
    def find_default_asset(asset: str, network: str) -> Optional[dict]:
        """USDC on NEAR is a known stablecoin to the SDK's spend controls (which by default pay only
        those, up to their per-payment cap), as Base's and Solana's USDC are to theirs."""
        usdc = USDC.get(str(network))
        if usdc and str(asset).lower() == usdc:
            return {"asset": usdc, "name": "USD Coin", "symbol": "USDC", "decimals": USDC_DECIMALS}
        return None

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
    that transaction's outcome. A delegate is sent again only when the network lost the first
    transaction (the chain never saw it and the delegate's nonce is unused), and even then the chain
    runs its transfer at most once: the delegate's nonce is spent by the first one to land."""

    scheme = SCHEME
    caip_family = "near:*"
    wait_seconds = 90.0   # how long settle waits for the relayer's transaction to finalize
    lost_after = 20.0     # seconds: a relayer transaction the chain still doesn't know by then was dropped
    poll_seconds = 1.5    # between looks at a transaction's status

    def __init__(self, relayer_id: str, relayer_key: KeyPair, rpc_urls: Optional[dict] = None,
                 max_gas: int = MAX_SPONSORED_GAS, wait_seconds: Optional[float] = None):
        if not is_account_id(relayer_id):
            raise ValueError(f"{relayer_id!r} is not a NEAR account id")
        self.relayer_id, self._key, self._rpc_urls = relayer_id, relayer_key, rpc_urls or {}
        self.max_gas = max_gas
        if wait_seconds is not None:
            self.wait_seconds = wait_seconds
        self._lock = threading.Lock()          # one relayer transaction at a time: its nonces land in order
        self._nonce: dict[str, int] = {}       # network -> last nonce this relayer key used
        self._sent: dict[str, list[tuple[str, float]]] = {}  # sha256(signed delegate) -> [(tx hash, sent at)]
        # Facts about the token that don't change between payments, once the chain has confirmed them:
        # (rpc, asset) is a contract; (rpc, asset, pay_to) is registered with it. Fewer reads per payment.
        self._known: set[tuple[str, ...]] = set()

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
        # No payer is named until the signature checks out: until then sender_id is only the client's claim.
        if d.public_key.key_type != near.ED25519 or sda.signature_type != near.ED25519:
            raise _Invalid("unsupported_key_type", "only ed25519 keys are accepted")
        if not sda.verify():
            raise _Invalid("invalid_signature", "the delegate's signature doesn't match its key")
        payer = d.sender_id
        if payer == self.relayer_id:
            raise _Invalid("relayer_is_payer", "the relayer can't pay itself", payer)
        if len(d.actions) != 1:
            raise _Invalid("invalid_action_count", "a payment is exactly one ft_transfer", payer)
        call = d.actions[0]
        if call.method_name != "ft_transfer":
            raise _Invalid("invalid_action", "a payment is exactly one ft_transfer", payer)
        if d.receiver_id != str(requirements.asset):
            raise _Invalid("asset_mismatch", "the transfer is on a different token", payer)
        # The arguments as the token contract will read them: NEP-141's ft_transfer(receiver_id, amount: U128
        # as a JSON string, memo: Option<String>). Anything the contract would refuse is refused here, before
        # the job runs (testnet USDC panics on a numeric amount, a numeric memo, or a repeated key).
        try:
            args = transfer_args(call)
        except ValueError as e:
            if "repeated" in str(e):
                raise _Invalid("invalid_action", "the transfer's arguments repeat a key", payer) from e
            raise _Invalid("invalid_payload", "the transfer's arguments aren't JSON", payer) from e
        if not isinstance(args, dict) or set(args) - {"receiver_id", "amount", "memo"} or \
                not isinstance(args.get("memo", ""), (str, type(None))):
            raise _Invalid("invalid_action", "unexpected transfer arguments", payer)
        if args.get("receiver_id") != str(requirements.pay_to):
            raise _Invalid("recipient_mismatch", "the transfer pays someone else", payer)
        if not isinstance(args.get("amount"), str):
            raise _Invalid("invalid_action", "the transfer's amount must be a JSON string", payer)
        if args["amount"] != str(requirements.amount):
            raise _Invalid("amount_mismatch", "the transfer is for a different amount", payer)
        if call.deposit != 1:
            raise _Invalid("invalid_deposit", "an ft_transfer attaches exactly 1 yoctoNEAR", payer)
        if call.gas > self.max_gas:
            raise _Invalid("gas_above_policy", f"gas must be at most {self.max_gas // near.TGAS} TGas", payer)
        if call.gas < MIN_TRANSFER_GAS:
            raise _Invalid("gas_below_policy", f"gas must be at least {MIN_TRANSFER_GAS // near.TGAS} TGas, or the "
                           "transfer runs out of gas on chain", payer)

        # What the chain says, read at once; the checks still run in this order.
        is_token, registered = (rpc.url, d.receiver_id), (rpc.url, d.receiver_id, str(requirements.pay_to))
        reads = {
            "head": lambda: int(rpc.final_block()["height"]),
            "access": lambda: rpc.access_key(payer, str(d.public_key)),
            "balance": lambda: rpc.view(d.receiver_id, "ft_balance_of", {"account_id": payer}),
        }
        if is_token not in self._known:
            reads["token"] = lambda: rpc.account(d.receiver_id)
        if registered not in self._known:
            reads["storage"] = lambda: rpc.view(d.receiver_id, "storage_balance_of", {"account_id": str(requirements.pay_to)})
        chain = _gather(reads)
        head = chain["head"].result()
        left = d.max_block_height - head
        if left <= 0:
            raise _Invalid("delegate_action_expired", "the signed payment has expired", payer)
        if left > timeout_blocks(max_timeout):
            raise _Invalid("delegate_action_timeout_window_exceeds_maxTimeout", "the payment's window is longer than asked", payer)
        access = chain["access"].result()
        if access is None:
            raise _Invalid("unknown_access_key", f"{payer} has no such key", payer)
        if d.nonce <= int(access.get("nonce", 0)):
            raise _Invalid("delegate_action_nonce_already_used", "this signed payment's nonce was already used", payer)
        if d.nonce >= head * 1_000_000:
            raise _Invalid("delegate_action_nonce_too_large", "the nonce is out of range", payer)
        if access.get("permission") != "FullAccess":
            raise _Invalid("access_key_not_full_access", "a function-call key can't attach the 1 yoctoNEAR deposit", payer)

        if "token" in chain:
            token = chain["token"].result()
            if not token or token.get("code_hash") in (None, "", _EMPTY_CODE):
                raise _Invalid("asset_not_a_contract", "the token account has no contract", payer)
            self._known.add(is_token)
        try:
            balance = int(str(chain["balance"].result()))
        except (ValueError, TypeError) as e:
            raise _Invalid("insufficient_funds", "couldn't read the payer's balance", payer) from e
        if balance < int(requirements.amount):
            raise _Invalid("insufficient_funds", "the payer's USDC balance is too low", payer)
        if "storage" in chain:
            if chain["storage"].result() is None:
                raise _Invalid("recipient_not_registered", "the shop isn't registered with the token contract", payer)
            self._known.add(registered)
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
                if e.name in ("TIMEOUT_ERROR", "RPC_UNREACHABLE", "PROVIDER_LIMIT"):
                    self._nonce[network] = nonce + 1  # it may well have landed; the outcome check decides
                    return tx_hash
                if attempt == 1 and "nonce" in str(e.data or e).lower():
                    continue  # another process used the relayer key; read its nonce and try once more
                raise
            self._nonce[network] = nonce + 1
            return tx_hash
        raise RpcError("INVALID_NONCE", "the relayer's nonce kept moving")

    def _lost(self, rpc: Rpc, sda: SignedDelegateAction, tx_hash: str, sent_at: float) -> bool:
        """Whether a relayer transaction carrying this delegate never reached the chain: the chain still
        doesn't know it ``lost_after`` seconds on, and the delegate's nonce is unused."""
        if time.time() - sent_at < self.lost_after:
            return False
        try:
            rpc.tx_status(tx_hash, self.relayer_id, wait_until="NONE", timeout=10.0)
            return False
        except RpcError as e:
            if e.name != "UNKNOWN_TRANSACTION":
                return False
        d = sda.delegate_action
        try:
            access = rpc.access_key(d.sender_id, str(d.public_key))
        except RpcError:
            return False
        return access is not None and int(access.get("nonce", 0)) < d.nonce

    def _outcome(self, rpc: Rpc, txs: list[tuple[str, float]], asset: str, payer: str, pay_to: str,
                 amount: int) -> tuple[str, Optional[dict], bool]:
        """Wait for the relayer transactions that carried this delegate to finalize. Returns
        (tx, outcome, lost): the one whose outcome paid, else the newest with its final outcome (None
        if it didn't finalize in time); ``lost`` when the chain still doesn't know the newest one."""
        deadline = time.time() + self.wait_seconds
        finals: dict[str, dict] = {}
        newest, newest_at = txs[-1]
        while True:
            lost = False
            for tx, sent_at in reversed(txs):
                if tx in finals:
                    continue
                try:
                    out = rpc.tx_status(tx, self.relayer_id, wait_until="FINAL", timeout=30.0)
                except RpcError as e:
                    if e.name == "UNKNOWN_TRANSACTION":
                        lost = lost or (tx == newest and time.time() - newest_at >= self.lost_after)
                    elif e.name not in ("TIMEOUT_ERROR", "RPC_UNREACHABLE", "REQUEST_VALIDATION_ERROR", "RATE_LIMITED",
                                        "PROVIDER_LIMIT"):
                        logger.info("near: status of %s: %s", tx, e)
                    continue
                if near.final(out):
                    finals[tx] = out
                    if near.paid(out, asset, payer, pay_to) == amount:
                        return tx, out, False
            if newest in finals or lost or time.time() >= deadline:
                return newest, finals.get(newest), lost
            time.sleep(self.poll_seconds)

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
        payer, asset, pay_to = sda.delegate_action.sender_id, str(requirements.asset), str(requirements.pay_to)
        key = hashlib.sha256(sda.to_borsh()).hexdigest()
        rpc = _rpc_for(network, self._rpc_urls)
        tx_hash, outcome, lost = "", None, False
        for _ in range(2):  # a relayer transaction the network lost is sent once more
            with self._lock:
                sent = self._sent.setdefault(key, [])
                if not sent or self._lost(rpc, sda, *sent[-1]):
                    try:
                        self._check(payload, requirements, rpc)  # the chain may have moved since verify
                    except _Invalid as e:
                        return fail(e.reason, e.message, payer=e.payer)
                    except RpcError as e:
                        return fail("rpc_unavailable", str(e), payer=payer)
                    try:
                        sent.append((self._submit(rpc, network, sda), time.time()))
                    except RpcError as e:
                        if not sent:
                            return fail("settlement_failed", str(e), payer=payer)
                txs = list(sent)
            tx_hash, outcome, lost = self._outcome(rpc, txs, asset, payer, pay_to, int(requirements.amount))
            if not lost:
                break
        if outcome is None:
            return fail("settlement_pending", "the relayer's transaction didn't reach the chain" if lost else
                        "the transaction hasn't finalized yet", tx_hash, payer)
        if near.paid(outcome, asset, payer, pay_to) != int(requirements.amount):
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
