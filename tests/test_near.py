"""NEAR payments, offline.

1. near.py's Borsh is pinned to bytes NEAR's own JS library (near-api-js 7.4.0) made for the same
   delegate and relayer transaction (fixtures/near_vectors.json, from fixtures/near_oracle.mjs).
2. x402 "exact" on NEAR (near_x402.py) is verified and settled against tests/fake_near.py, a chain
   in memory that executes delegates the way nearcore does.
3. Whole paid jobs over HTTP: a buyer pays a NEAR shop in USDC holding no NEAR for gas, the shop's
   relayer pays the gas, and nothing moves for a job that isn't delivered.
"""

import base64
import hashlib
import json
import threading
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from conftest import as_settings, start_shop
from fake_near import USDC, FakeNear

from emporium import buyer, chain as onchain, ledger as books, near, near_x402
from emporium import shop as shop_mod

VEC = json.loads((Path(__file__).parent / "fixtures" / "near_vectors.json").read_text())
NET = "near:testnet"
TGAS = near.TGAS


def seeded(text: str) -> near.KeyPair:
    return near.KeyPair("ed25519:" + near.b58encode(hashlib.sha256(text.encode()).digest()))


# Throwaway keys, used only against the fake chain.
BUYER, RELAYER, OTHER = seeded("test-near-buyer"), seeded("test-near-relayer"), seeded("test-near-other")
POLICY = buyer.Policy(as_settings({"buyer_max_per_job": "$1.00", "buyer_daily_budget": "$2.00"}))


# ---- 1. the encoding ----------------------------------------------------------------------------
def test_encoding_matches_near_api_js():
    b, r = seeded(VEC["buyer_seed"]), seeded(VEC["relayer_seed"])
    assert str(b.public_key) == VEC["buyer_public_key"]
    args = json.dumps({"receiver_id": "ada-shop.testnet", "amount": "50000"}, separators=(",", ":")).encode()
    da = near.DelegateAction("buyer-1.testnet", USDC, (near.FunctionCall("ft_transfer", args, 30 * TGAS, 1),),
                             123456789012345, 277045412, b.public_key)
    assert base64.b64encode(near._u32(near.NEP366_PREFIX) + da.to_borsh()).decode() == VEC["delegate_action_b64"]
    assert da.signing_hash().hex() == VEC["nep461_hash_hex"]
    sda = da.sign(b)
    assert sda.to_base64() == VEC["signed_delegate_b64"]  # ed25519 is deterministic: the signature matches too
    tx = near.Transaction("relayer-1.testnet", r.public_key, 99, "buyer-1.testnet", bytes([7]) * 32, (near.Delegate(sda),))
    assert base64.b64encode(tx.to_borsh()).decode() == VEC["tx_b64"]
    tx_hash, signed = tx.sign(r)
    assert tx_hash == VEC["tx_hash_b58"] and signed == VEC["signed_tx_b64"]


def test_decoding_a_delegate_is_strict():
    raw = base64.b64decode(VEC["signed_delegate_b64"])
    sda = near.SignedDelegateAction.from_borsh(raw)
    assert sda.to_borsh() == raw and sda.verify()
    assert sda.delegate_action.actions[0].json_args() == {"receiver_id": "ada-shop.testnet", "amount": "50000"}
    for bad in (raw + b"\0", raw[:-1], b"\x05\0\0\0Bad!!" + raw[19:]):
        with pytest.raises(ValueError):
            near.SignedDelegateAction.from_borsh(bad)
    # a delegate carrying anything but function calls is refused (a payment is one ft_transfer)
    da = near.DelegateAction("buyer.testnet", USDC, (near.Transfer(10**24),), 5, 10, BUYER.public_key)
    with pytest.raises(ValueError, match="not a function call"):
        near.SignedDelegateAction.from_base64(da.sign(BUYER).to_base64())
    # a changed byte breaks the signature
    tampered = sda.delegate_action.to_borsh().replace(b"50000", b"90000")
    assert not near.SignedDelegateAction.from_borsh(tampered + raw[-65:]).verify()


def test_keys_base58_and_account_ids():
    assert near.b58decode(near.b58encode(b"\0\0near")) == b"\0\0near"
    seed_only = near.KeyPair("ed25519:" + near.b58encode(hashlib.sha256(b"x").digest()))
    assert near.KeyPair(seed_only.secret()).public_key == seed_only.public_key  # 64-byte form (seed + public)
    wrong_half = near.b58decode(seed_only.secret()[8:])[:32] + bytes(32)
    with pytest.raises(ValueError, match="public half"):
        near.KeyPair("ed25519:" + near.b58encode(wrong_half))
    assert "ed25519:" in repr(seed_only) and seed_only.secret()[8:] not in repr(seed_only)
    for ok in ("ada.testnet", "buyer-1.emporium.testnet", "a1_b2.near", "0" * 64):
        assert near.is_account_id(ok), ok
    for bad in ("Ada.testnet", "a", "-ada.testnet", "ada..testnet", "ada.testnet.", "x" * 65, "ada testnet"):
        assert not near.is_account_id(bad), bad


# ---- 2. verify and settle ----------------------------------------------------------------------
@pytest.fixture
def net(monkeypatch):
    """A fresh fake chain: a buyer with 2 USDC and a dust of NEAR, the shop, its relayer."""
    c = FakeNear().start()
    c.add_account("buyer.testnet", BUYER, near_amount=0.03, usdc=2_000_000)
    c.add_account("relay.testnet", RELAYER, near_amount=1)
    c.add_account("shop.testnet")
    c.add_account("unregistered.testnet", register=False)
    monkeypatch.setitem(shop_mod.RPC_URLS, NET, c.url)  # every NEAR RPC client in the plugin reads this map
    monkeypatch.setitem(shop_mod.ARCHIVAL_RPC_URLS, NET, "")  # tests never reach a real archival node
    monkeypatch.setattr(near_x402.ExactNearFacilitator, "poll_seconds", 0.01)
    yield c
    c.stop()


def terms(amount: str = "50000", pay_to: str = "shop.testnet", seconds: int = 300):
    from x402.schemas import PaymentRequirements
    return PaymentRequirements(scheme="exact", network=NET, asset=USDC, amount=amount, pay_to=pay_to,
                               max_timeout_seconds=seconds, extra={})


def wrap(req, sda=None, b64: str = "", version: int = 2, accepted=None):
    from x402.schemas import PaymentPayload
    return PaymentPayload(x402_version=version, payload={"signedDelegateAction": b64 or sda.to_base64()},
                          accepted=accepted or req)


def paid_by_client(req, account="buyer.testnet", key=BUYER):
    from x402.schemas import PaymentPayload
    body = near_x402.ExactNearClient(account, key).create_payment_payload(req)
    return PaymentPayload(x402_version=2, payload=body, accepted=req)


def delegate(c: FakeNear, *, sender="buyer.testnet", key=BUYER, token=USDC, method="ft_transfer", to="shop.testnet",
             amount="50000", args=None, gas=30 * TGAS, deposit=1, nonce=None, height=None, actions=None):
    """A buyer's delegate with any field changed, signed by ``key``."""
    call_args = json.dumps(args if args is not None else {"receiver_id": to, "amount": amount}).encode()
    calls = actions or (near.FunctionCall(method, call_args, gas, deposit),)
    pk = key.public_key
    try:
        current = c.key_nonce(sender, pk)
    except KeyError:
        current = 0
    da = near.DelegateAction(sender, token, tuple(calls), current + 1 if nonce is None else nonce,
                             c.height + 100 if height is None else height, pk)
    return da.sign(key)


def facilitator():
    return near_x402.ExactNearFacilitator("relay.testnet", RELAYER)


def test_a_payment_settles_once_and_the_buyer_pays_no_gas(net, monkeypatch):
    req = terms()
    payload = paid_by_client(req)
    fac = facilitator()
    v = fac.verify(payload, req)
    assert v.is_valid and v.payer == "buyer.testnet"
    assert net.included == [], "verifying moves nothing"
    buyer_near, relay_near = net.near_balance("buyer.testnet"), net.near_balance("relay.testnet")
    s = fac.settle(payload, req)
    assert s.success and s.payer == "buyer.testnet" and near.TX_HASH.match(s.transaction)
    assert net.usdc("buyer.testnet") == 2_000_000 - 50_000 and net.usdc("shop.testnet") == 50_000
    assert net.near_balance("buyer.testnet") == buyer_near, "the buyer spends no NEAR"
    assert net.near_balance("relay.testnet") < relay_near, "the relayer pays the gas and the 1 yoctoNEAR"
    tx = net.included[0]
    assert tx["signer_id"] == "relay.testnet" and tx["receiver_id"] == "buyer.testnet"
    assert [a[0] for a in tx["actions"]] == ["Delegate"]
    # settling again is idempotent, and the spent payment no longer verifies
    again = fac.settle(payload, req)
    assert again.success and again.transaction == s.transaction and len(net.included) == 1
    v2 = fac.verify(payload, req)
    assert not v2.is_valid and v2.invalid_reason == "delegate_action_nonce_already_used"
    # the payment is what chain.near_paid (receipts, track records) reads back
    assert onchain.near_paid(NET, s.transaction, "relay.testnet", "buyer.testnet", "shop.testnet") == Decimal("0.05")
    assert onchain.near_paid(NET, s.transaction, "relay.testnet", "buyer.testnet", "thief.testnet") == 0
    # a node that has dropped the transaction (public nodes keep about 30 h) hands over to the archival one
    pruned = FakeNear().start()
    try:
        monkeypatch.setitem(shop_mod.RPC_URLS, NET, pruned.url)
        assert onchain.near_paid(NET, s.transaction, "relay.testnet", "buyer.testnet", "shop.testnet") == 0
        monkeypatch.setitem(shop_mod.ARCHIVAL_RPC_URLS, NET, net.url)
        assert onchain.near_paid(NET, s.transaction, "relay.testnet", "buyer.testnet", "shop.testnet") == Decimal("0.05")
    finally:
        pruned.stop()


def _cases(c: FakeNear):
    fc_key = OTHER
    c.add_key("buyer.testnet", fc_key.public_key, {"FunctionCall": {"allowance": None, "receiver_id": USDC,
                                                                     "method_names": ["ft_transfer"]}})
    return {
        "recipient_mismatch": (terms(), delegate(c, to="thief.testnet")),
        "amount_mismatch": (terms(), delegate(c, amount="40000")),
        "asset_mismatch": (terms(), delegate(c, token="fake-usdc.testnet")),
        "invalid_action": (terms(), delegate(c, method="ft_transfer_call")),
        "invalid_action_count": (terms(), delegate(c, actions=[near.FunctionCall("ft_transfer", json.dumps(
            {"receiver_id": "shop.testnet", "amount": "50000"}).encode(), 30 * TGAS, 1)] * 2)),
        "invalid_deposit": (terms(), delegate(c, deposit=0)),
        "gas_above_policy": (terms(), delegate(c, gas=300 * TGAS)),
        "extra_arguments": (terms(), delegate(c, args={"receiver_id": "shop.testnet", "amount": "50000", "msg": "x"})),
        "delegate_action_expired": (terms(), delegate(c, height=c.height - 1)),
        "delegate_action_timeout_window_exceeds_maxTimeout": (terms(seconds=300), delegate(c, height=c.height + 5000)),
        "delegate_action_nonce_already_used": (terms(), delegate(c, nonce=c.key_nonce("buyer.testnet", BUYER.public_key))),
        "delegate_action_nonce_too_large": (terms(), delegate(c, nonce=c.height * 1_000_000)),
        "access_key_not_full_access": (terms(), delegate(c, key=fc_key)),
        "unknown_access_key": (terms(), delegate(c, key=seeded("a key the buyer doesn't have"))),
        "insufficient_funds": (terms(amount="5000000"), delegate(c, amount="5000000")),
        "recipient_not_registered": (terms(pay_to="unregistered.testnet"), delegate(c, to="unregistered.testnet")),
        "relayer_is_payer": (terms(), delegate(c, sender="relay.testnet", key=RELAYER)),
    }


EXPECTED = {"extra_arguments": "invalid_action"}


@pytest.mark.parametrize("case", ["recipient_mismatch", "amount_mismatch", "asset_mismatch", "invalid_action",
                                  "invalid_action_count", "invalid_deposit", "gas_above_policy", "extra_arguments",
                                  "delegate_action_expired", "delegate_action_timeout_window_exceeds_maxTimeout",
                                  "delegate_action_nonce_already_used", "delegate_action_nonce_too_large",
                                  "access_key_not_full_access", "unknown_access_key", "insufficient_funds",
                                  "recipient_not_registered", "relayer_is_payer"])
def test_verify_refuses_a_bad_payment(net, case):
    req, sda = _cases(net)[case]
    fac = facilitator()
    v = fac.verify(wrap(req, sda), req)
    assert not v.is_valid and v.invalid_reason == EXPECTED.get(case, case), v
    s = fac.settle(wrap(req, sda), req)
    assert not s.success and net.included == [], "a refused payment is never sent"
    assert net.usdc("buyer.testnet") == 2_000_000


def test_verify_refuses_forged_and_malformed_payloads(net):
    req, fac = terms(), facilitator()
    good = delegate(net)
    forged = near.SignedDelegateAction(good.delegate_action, near.ED25519, bytes(64))
    assert fac.verify(wrap(req, forged), req).invalid_reason == "invalid_signature"
    assert fac.verify(wrap(req, b64="bm90IGEgZGVsZWdhdGU="), req).invalid_reason == "invalid_payload"
    assert fac.verify(wrap(req, good, version=1), req).invalid_reason == "invalid_x402_version"
    assert fac.verify(wrap(req, good, accepted=terms(amount="1")), req).invalid_reason == "requirements_mismatch"
    smuggled = near.DelegateAction("buyer.testnet", USDC, (near.Transfer(10**24),), 1, net.height + 9, BUYER.public_key)
    assert fac.verify(wrap(req, b64=smuggled.sign(BUYER).to_base64()), req).invalid_reason == "invalid_payload"
    assert net.included == []


def test_the_client_signs_only_with_a_full_access_key(net):
    net.add_key("buyer.testnet", OTHER.public_key, {"FunctionCall": {"allowance": None, "receiver_id": USDC,
                                                                      "method_names": []}})
    with pytest.raises(ValueError, match="full-access"):
        near_x402.ExactNearClient("buyer.testnet", OTHER).create_payment_payload(terms())
    with pytest.raises(ValueError, match="no access key"):
        near_x402.ExactNearClient("buyer.testnet", seeded("nobody's key")).create_payment_payload(terms())
    with pytest.raises(ValueError, match="don't name"):
        near_x402.ExactNearClient("buyer.testnet", BUYER).create_payment_payload(terms(pay_to="Shop.Testnet"))


def test_settle_rechecks_the_chain_after_the_job(net):
    req, fac = terms(), facilitator()
    payload = paid_by_client(req)
    assert fac.verify(payload, req).is_valid
    net.balances["buyer.testnet"] = 10  # the buyer spent the USDC elsewhere while the job ran
    s = fac.settle(payload, req)
    assert not s.success and s.error_reason == "insufficient_funds" and net.included == []


def test_a_failed_transfer_is_not_a_payment(net):
    """nearcore's trap: the delegate's receipt (and the transaction's status) succeeds while the
    token's receipt fails. The facilitator reads every receipt, so this is no payment."""
    req, fac = terms(), facilitator()
    payload = paid_by_client(req)
    net.fail_transfers = 1
    s = fac.settle(payload, req)
    assert not s.success and s.error_reason == "transfer_failed" and s.transaction
    outcome = net.outcomes[s.transaction]
    assert "SuccessValue" in outcome["status"] and near.failures(outcome)
    assert net.usdc("buyer.testnet") == 2_000_000 and net.usdc("shop.testnet") == 0
    again = fac.settle(payload, req)  # the delegate's nonce is spent: it can't be sent again
    assert not again.success and again.transaction == s.transaction and len(net.included) == 1


def test_a_timed_out_send_that_landed_is_found(net):
    req, fac = terms(), facilitator()
    net.timeout_next = 1  # the RPC gives up waiting, but the transaction is in
    s = fac.settle(paid_by_client(req), req)
    assert s.success and len(net.included) == 1 and net.usdc("shop.testnet") == 50_000


def test_a_lost_transaction_is_sent_again_and_charges_once(net, monkeypatch):
    monkeypatch.setattr(near_x402.ExactNearFacilitator, "lost_after", 0.2)
    req, fac = terms(), facilitator()
    net.drop_next = 1  # the RPC answers a timeout and the transaction never reaches a block
    s = fac.settle(paid_by_client(req), req)
    assert s.success and len(net.included) == 1
    assert net.usdc("buyer.testnet") == 2_000_000 - 50_000 and net.usdc("shop.testnet") == 50_000


def test_a_slow_transaction_reports_pending_and_then_settles(net, monkeypatch):
    monkeypatch.setattr(near_x402.ExactNearFacilitator, "wait_seconds", 0.0)
    req, fac = terms(), facilitator()
    payload = paid_by_client(req)
    net.pending_polls = 1
    s = fac.settle(payload, req)
    assert not s.success and s.error_reason == "settlement_pending" and s.transaction
    again = fac.settle(payload, req)  # nothing is re-sent; the same transaction is looked up
    assert again.success and again.transaction == s.transaction and len(net.included) == 1


def test_the_relayer_recovers_when_its_nonce_moved(net):
    req, fac = terms(), facilitator()
    assert fac.settle(paid_by_client(req), req).success
    net.accounts["relay.testnet"]["keys"][str(RELAYER.public_key)]["nonce"] += 5  # another process used the key
    s = fac.settle(paid_by_client(req), req)
    assert s.success and len(net.included) == 2 and net.usdc("shop.testnet") == 100_000


def test_two_delegates_signed_on_one_nonce_pay_once(net):
    """Why a buyer key pays for one job at a time: two delegates signed before either lands share a nonce."""
    req, fac = terms(), facilitator()
    first, same = paid_by_client(req), paid_by_client(req)
    assert first.payload == same.payload, "signed at the same height, the same payment comes out byte for byte"
    net.advance(1)
    second = paid_by_client(req)  # a block later: a different delegate on the same nonce
    assert second.payload != first.payload
    ok = fac.settle(first, req)
    assert ok.success and fac.settle(same, req).transaction == ok.transaction
    s = fac.settle(second, req)
    assert not s.success and s.error_reason == "delegate_action_nonce_already_used"
    assert net.usdc("shop.testnet") == 50_000 and len(net.included) == 1


def test_timing_and_pricing(net):
    rpc = near.Rpc(net.url)
    assert abs(near.block_seconds(rpc) - 0.6) < 1e-9
    assert near_x402.window_for(120, rpc) == 250  # 120 s at 0.6 s a block, with a quarter to spare
    assert near_x402.timeout_blocks(300) == 300   # the spec's one block a second
    server = near_x402.ExactNearServer()
    price = server.parse_price("$0.05", NET)
    assert (price.amount, price.asset, price.extra) == ("50000", USDC, {})
    with pytest.raises(ValueError):
        server.parse_price("€1", NET)


# ---- 3. paid jobs over HTTP --------------------------------------------------------------------
@pytest.fixture
def near_shop(net, monkeypatch):
    import agent.secret_scope as ss
    monkeypatch.setattr(ss, "get_secret", lambda name, *a, **k: RELAYER.secret() if name == "EMPORIUM_NEAR_RELAYER_KEY" else None)
    settings = {
        "pay_to": "shop.testnet", "network": NET, "near_relayer_account": "relay.testnet", "rpc_url": net.url,
        "shop_name": "Test NEAR Emporium", "host": "127.0.0.1", "port": 0,
        "services": [{"id": "echo", "title": "Echo", "price": "$0.05", "instructions": "Repeat the input.", "tools": []}],
    }
    adapter, stop = start_shop(settings)
    adapter.chain = net
    yield adapter
    stop()


def hire(shop, text="hello shop", **kw):
    return buyer.hire(shop.test_url, kw.pop("service", "echo"), text, policy=kw.pop("policy", POLICY), key=None,
                      ledger=kw.pop("ledger", None), near_account=kw.pop("near_account", "buyer.testnet"),
                      near_key=kw.pop("near_key", BUYER.secret()), **kw)


def test_a_near_job_is_delivered_and_paid_on_near(near_shop, tmp_path):
    net = near_shop.chain
    cat = httpx.get(near_shop.test_url + "/.well-known/emporium.json").json()
    assert cat["network"] == NET and cat["pay_to"] == "shop.testnet" and cat["relayer"] == "relay.testnet"
    led = books.Ledger(tmp_path / "buyer.db")
    buyer_near = net.near_balance("buyer.testnet")
    out = hire(near_shop, text="ping 123", ledger=led)
    assert out["output"] == "ECHO: ping 123"
    r = out["receipt"]
    assert r["network"] == NET and r["pay_to"] == "shop.testnet" and r["payer"] == "buyer.testnet"
    assert near.TX_HASH.match(r["tx"]) and r["explorer"] == "https://testnet.nearblocks.io/txns/" + r["tx"]
    assert r["asset_contract"] == USDC and r["settled_by"] == "relay.testnet"
    assert net.usdc("shop.testnet") == 50_000 and net.usdc("buyer.testnet") == 1_950_000
    assert net.near_balance("buyer.testnet") == buyer_near
    job = near_shop.books.recent_jobs(1)[0]
    assert job["status"] == books.SETTLED and job["tx"] == r["tx"]
    assert led.recent_purchases(1)[0]["status"] == books.BOUGHT
    # a buyer checking this shop's record finds the payment on-chain
    record = buyer.track_record(near_shop.test_url, buyer.catalog(near_shop.test_url, fresh=True))
    assert (record["checked"], record["verified"], record["verified_usd"]) == (1, 1, "$0.05")


def test_a_failed_near_job_is_never_charged(near_shop):
    near_shop.stub.fail = True
    with pytest.raises(buyer.HireError, match="not charged"):
        hire(near_shop)
    assert near_shop.chain.included == [] and near_shop.chain.usdc("buyer.testnet") == 2_000_000
    assert near_shop.books.recent_jobs(1)[0]["status"] == books.FAILED


def test_a_near_buyer_pays_again_after_an_undelivered_job(near_shop):
    """After a job that failed, nothing was submitted, so the buyer's next payment signs the same key
    nonce. The shop takes it (only one of the two could ever land), and the buyer is charged once."""
    near_shop.stub.fail = True
    with pytest.raises(buyer.HireError, match="not charged"):
        hire(near_shop)
    near_shop.stub.fail = False
    out = hire(near_shop, text="second try")
    assert out["output"] == "ECHO: second try"
    net = near_shop.chain
    assert len(net.included) == 1 and net.usdc("buyer.testnet") == 1_950_000 and net.usdc("shop.testnet") == 50_000


def _signed_header(shop, mutate=None):
    """Do the 402 dance by hand: returns (headers, payload) for one signed payment."""
    from x402 import x402ClientSync
    from x402.http.x402_http_client import x402HTTPClientSync
    client = x402ClientSync()
    near_x402.register_exact_near_client(client, "buyer.testnet", BUYER, [NET])
    hc = x402HTTPClientSync(client)
    first = httpx.post(shop.test_url + "/jobs/echo", json={"input": "hi"})
    assert first.status_code == 402
    required = hc.get_payment_required_response(lambda n: first.headers.get(n), first.json())
    payload = hc.create_payment_payload(required)
    if mutate:
        payload.payload["signedDelegateAction"] = mutate(payload).to_base64()
    return hc.encode_payment_signature_header(payload), payload


def test_a_near_payment_buys_one_job(near_shop):
    headers, _ = _signed_header(near_shop)
    r1 = httpx.post(near_shop.test_url + "/jobs/echo", json={"input": "hi"}, headers=headers, timeout=60)
    assert r1.status_code == 200
    r2 = httpx.post(near_shop.test_url + "/jobs/echo", json={"input": "hi"}, headers=headers, timeout=60)
    assert r2.status_code in (402, 409) and "output" not in r2.text
    assert len(near_shop.chain.included) == 1 and near_shop.chain.usdc("shop.testnet") == 50_000
    assert len(near_shop.stub.events) == 1, "the replay did no work"


def test_a_near_payment_that_expires_too_soon_does_no_work(near_shop):
    net = near_shop.chain

    def short(payload):  # valid for 20 blocks (~12 s): far less than the job's timeout plus settling
        d = near_x402.signed_delegate(payload).delegate_action
        return near.DelegateAction(d.sender_id, d.receiver_id, d.actions, d.nonce, net.height + 20, d.public_key).sign(BUYER)

    headers, _ = _signed_header(near_shop, short)
    r = httpx.post(near_shop.test_url + "/jobs/echo", json={"input": "hi"}, headers=headers, timeout=60)
    assert r.status_code == 402 and "expires too soon" in r.json()["error"] and r.json()["charged"] is False
    assert not near_shop.stub.events and net.included == []


def test_a_failed_near_settlement_withholds_the_result(near_shop, no_backoff):
    near_shop.chain.fail_transfers = 1
    with pytest.raises(buyer.HireError) as e:
        hire(near_shop, text="secret sauce")
    assert "secret sauce" not in str(e.value)
    assert near_shop.books.recent_jobs(1)[0]["status"] == books.UNPAID
    assert len(near_shop.chain.included) == 1, "a spent delegate is not retried"
    assert near_shop.chain.usdc("buyer.testnet") == 2_000_000


def test_a_near_settlement_reported_pending_but_landed_is_delivered(near_shop, monkeypatch, no_backoff):
    monkeypatch.setattr(near_x402.ExactNearFacilitator, "wait_seconds", 0.0)
    near_shop.chain.pending_polls = 1  # the facilitator gives up waiting; the shop's on-chain check finds it
    out = hire(near_shop)
    assert out["output"] == "ECHO: hello shop" and near.TX_HASH.match(out["receipt"]["tx"])
    assert len(near_shop.chain.included) == 1 and near_shop.chain.usdc("shop.testnet") == 50_000


def test_one_near_key_pays_for_one_job_at_a_time(near_shop):
    results, errors = [], []

    def go(i):
        try:
            results.append(hire(near_shop, text=f"job {i}"))
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert len(results) == 3 and near_shop.chain.usdc("shop.testnet") == 150_000
    nonces = [a[1]["nonce"] for tx in near_shop.chain.included for a in tx["actions"]]
    assert len(set(nonces)) == 3 and nonces == sorted(nonces)


def test_a_near_shop_needs_its_accounts_and_relayer_key(net, monkeypatch):
    import agent.secret_scope as ss
    from gateway.config import PlatformConfig
    from emporium.adapter import EmporiumAdapter
    services = [{"id": "echo", "title": "Echo", "price": "$0.05", "instructions": "x"}]
    bad = shop_mod.load_shop(as_settings({"pay_to": "Not A Shop", "network": NET, "services": services}))
    assert any("pay_to must be the NEAR account" in p for p in bad.problems)
    assert any("near_relayer_account" in p for p in bad.problems)
    monkeypatch.setattr(ss, "get_secret", lambda name, *a, **k: None)
    settings = {"pay_to": "shop.testnet", "network": NET, "near_relayer_account": "relay.testnet", "rpc_url": net.url,
                "services": services}
    good = shop_mod.load_shop(as_settings(settings))
    assert good.ready, good.problems
    adapter = EmporiumAdapter(PlatformConfig(enabled=True), as_settings(settings), "test")
    with pytest.raises(RuntimeError, match="EMPORIUM_NEAR_RELAYER_KEY"):
        adapter._build_x402(good)


# ---- the RPC client under load: rate limits and dead endpoints ---------------------------------
class _Endpoints:
    """Stands in for httpx.post: each URL answers from its own script of replies (the last one repeats)."""

    def __init__(self, **script):
        self.script = {f"https://{k.replace('_', '-')}.example": list(v) for k, v in script.items()}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url, json=None, timeout=None):
        self.calls.append((url, json["method"]))
        replies = self.script[url]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if reply == "down":
            raise httpx.ConnectError("refused")
        status, body = reply
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


_LIMITED = (200, {"jsonrpc": "2.0", "id": 1, "error": {"name": "RPC_ERROR", "message": "Rate limits exceeded. "
                                                         "For higher rate limits get a subscription"}})
_OK = (200, {"jsonrpc": "2.0", "id": 1, "result": {"header": {"height": 7, "hash": "h"}}})


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(near.time, "sleep", waits.append)
    return waits


def test_a_rate_limited_rpc_hands_the_call_to_the_next_endpoint(monkeypatch, no_sleep):
    ep = _Endpoints(first=[_LIMITED], second=[_OK])
    monkeypatch.setattr(near.httpx, "post", ep)
    rpc = near.Rpc("https://first.example, https://second.example")
    assert rpc.final_block()["height"] == 7
    assert [u for u, _ in ep.calls] == ["https://first.example", "https://second.example"] and not no_sleep


def test_http_429_and_a_dead_endpoint_also_move_on(monkeypatch, no_sleep):
    ep = _Endpoints(first=[(429, {})], second=["down"], third=[_OK])
    monkeypatch.setattr(near.httpx, "post", ep)
    assert near.Rpc("https://first.example,https://second.example,https://third.example").final_block()["height"] == 7


def test_when_every_endpoint_is_limited_it_backs_off_then_gives_up(monkeypatch, no_sleep):
    ep = _Endpoints(first=[_LIMITED], second=[_LIMITED])
    monkeypatch.setattr(near.httpx, "post", ep)
    with pytest.raises(near.RpcError) as e:
        near.Rpc("https://first.example,https://second.example", rounds=3).final_block()
    assert e.value.name == "RATE_LIMITED" and len(ep.calls) == 6 and no_sleep == [0.5, 1.0]


def test_a_limit_that_lifts_is_waited_out(monkeypatch, no_sleep):
    ep = _Endpoints(only=[_LIMITED, _LIMITED, _OK])
    monkeypatch.setattr(near.httpx, "post", ep)
    assert near.Rpc("https://only.example").final_block()["height"] == 7 and no_sleep == [0.5, 1.0]


def test_a_real_refusal_is_not_retried(monkeypatch, no_sleep):
    gone = (200, {"jsonrpc": "2.0", "id": 1, "error": {"name": "HANDLER_ERROR", "cause": {"name": "UNKNOWN_ACCOUNT"}}})
    ep = _Endpoints(first=[gone], second=[_OK])
    monkeypatch.setattr(near.httpx, "post", ep)
    assert near.Rpc("https://first.example,https://second.example").account("nobody.testnet") is None
    assert len(ep.calls) == 1


def test_a_transaction_sent_to_a_dead_endpoint_is_not_sent_again(monkeypatch, no_sleep):
    """It may already be on its way; sending it elsewhere is the settle path's call (it looks it up first)."""
    ep = _Endpoints(first=["down"], second=[_OK])
    monkeypatch.setattr(near.httpx, "post", ep)
    with pytest.raises(near.RpcError) as e:
        near.Rpc("https://first.example,https://second.example").send_tx("AAAA")
    assert e.value.name == "RPC_UNREACHABLE" and [m for _, m in ep.calls] == ["send_tx"]


def test_the_egress_sweep_redacts_near_secret_keys_but_not_public_ones():
    from emporium import guard
    secret = BUYER.secret()  # ed25519:<64 bytes in base58>
    seed = "ed25519:" + near.b58encode(hashlib.sha256(b"seed-form").digest())  # the 32-byte form
    pub = str(BUYER.public_key)
    out = guard.scrub_output(f"key {secret}\nEMPORIUM_NEAR_RELAYER_KEY={seed}\nprivate key: {seed}\n"
                             f"public_key: {pub}\nPUBLIC_KEY={pub}")
    assert secret[8:] not in out and seed[8:] not in out and out.count("[redacted private key]") == 3
    assert out.count(pub) == 2


def test_a_providers_plan_limit_moves_a_read_on_but_never_resends_a_transaction(monkeypatch, no_sleep):
    plan = (200, {"jsonrpc": "2.0", "id": 1, "error": {"name": "RPC_ERROR",
                                                       "message": "Request timeout on the free plan, please upgrade to paid plan"}})
    ep = _Endpoints(first=[plan], second=[_OK])
    monkeypatch.setattr(near.httpx, "post", ep)
    assert near.Rpc("https://first.example,https://second.example").final_block()["height"] == 7
    ep = _Endpoints(first=[plan], second=[_OK])
    monkeypatch.setattr(near.httpx, "post", ep)
    with pytest.raises(near.RpcError) as e:
        near.Rpc("https://first.example,https://second.example").send_tx("AAAA")
    assert e.value.name == "PROVIDER_LIMIT" and len(ep.calls) == 1
