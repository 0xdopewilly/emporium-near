"""Cross-chain money through NEAR Intents (intents.py), offline.

1. Quote signatures, pinned to real 1Click quotes and 1Click's real key (fixtures/oneclick_quotes.json).
2. Every way a quote can differ from what was asked is refused, before anything is paid.
3. The 1Click client: tokens (cached), retries with backoff, errors, minimums, the optional JWT.
4. Status: PENDING_DEPOSIT -> … -> SUCCESS / REFUNDED / FAILED, or still going when the wait ends.
5. Fund (any chain -> the buyer's own NEAR account) and cash out (the shop's NEAR account -> any
   chain) end to end against tests/fake_oneclick.py and the in-memory NEAR chain tests/fake_near.py:
   the relayer registers the deposit address and pays the gas, the shop pays none, and no retry, timeout
   or lost transaction ever pays twice.
"""

import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from fake_near import FakeNear
from fake_oneclick import FakeOneClick

from emporium import intents, ledger as books, near, near_x402
from emporium import shop as shop_mod

QUOTES = json.loads((Path(__file__).parent / "fixtures" / "oneclick_quotes.json").read_text())
TOKEN_LIST = [intents.Token.from_api(t) for t in json.loads((Path(__file__).parent / "fixtures" / "oneclick_tokens.json").read_text())]
MAINNET_USDC = shop_mod.USDC["near:mainnet"]
NET = "near:mainnet"
BASE_USDC = "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"
PAYEE = "0xa11ce57b2f0c88de41a9b3c07e6d1f45a3c0d9f2"


def seeded(text: str) -> near.KeyPair:
    return near.KeyPair("ed25519:" + near.b58encode(hashlib.sha256(text.encode()).digest()))


# Throwaway keys, used only against the fakes.
ONECLICK_KEY, SHOP, RELAYER = seeded("test-1click-manager"), seeded("test-intents-shop"), seeded("test-intents-relayer")


def tok(spec: str) -> intents.Token:
    return intents.resolve(TOKEN_LIST, spec)


# ---- 1. signatures over real 1Click quotes ----------------------------------------------------------
@pytest.mark.parametrize("name", ["veto_dry", "fresh_dry", "live_cashout", "live_fund"])
def test_real_1click_quotes_verify_with_1clicks_key(name):
    resp = QUOTES[name]
    assert intents.verify_quote(resp), "1Click's own signature over its own quote"
    assert not intents.verify_quote(resp, str(ONECLICK_KEY.public_key)), "and only under 1Click's key"


def test_signed_json_is_byte_for_byte_what_veto_verified_on_chain():
    veto = json.loads((Path(__file__).parent / "fixtures" / "oneclick_quotes.json").read_text())["veto_live_signed_json"]
    msg = intents.quote_message(veto["json"])
    sig = near.b58decode(veto["signature"].removeprefix("ed25519:"))
    assert near.PublicKey.parse(intents.ONE_CLICK_MANAGER_KEY).verify(sig, msg)
    # the dry fixture Veto pinned: our reconstruction of the signed JSON is the same string
    assert intents.signed_quote_json(QUOTES["veto_dry"]).startswith('{"amount":"300000000","amountIn":"301567849"')
    assert '"timestamp":"2026-10-30T21:56:50.842Z"}' in intents.signed_quote_json(QUOTES["veto_dry"])


def test_a_live_quote_signs_its_deposit_address_deadline_and_fees():
    signed = json.loads(intents.signed_quote_json(QUOTES["live_cashout"]))
    q = QUOTES["live_cashout"]["quote"]
    assert signed["depositAddress"] == q["depositAddress"] and signed["deadline"] == q["deadline"]
    assert signed["deadline"] != QUOTES["live_cashout"]["quoteRequest"]["deadline"], "the quote's own deadline replaces the request's"
    assert signed["withdrawFee"] == "2400" and signed["refundFee"] == "0" and signed["referral"] == "emporium"
    assert "quoteWaitingTimeMs" not in signed and "appFees" not in signed and "depositMode" not in signed
    dry = json.loads(intents.signed_quote_json(QUOTES["fresh_dry"]))
    assert "timeEstimate" not in dry and "refundFee" not in dry, "a dry quote signs fewer fields"


@pytest.mark.parametrize("path,value", [
    (("quoteRequest", "recipient"), "0x000000000000000000000000000000000000dead"),
    (("quoteRequest", "refundTo"), "thief.near"),
    (("quoteRequest", "amount"), "2000001"),
    (("quoteRequest", "originAsset"), BASE_USDC),
    (("quoteRequest", "slippageTolerance"), 5000),
    (("quoteRequest", "dry"), True),
    (("quote", "amountOut"), "9997396"),
    (("quote", "minAmountOut"), "1"),
    (("quote", "depositAddress"), "f" * 64),
    (("quote", "deadline"), "2026-11-12T21:55:00.000Z"),
    (("quote", "withdrawFee"), "0"),
    (("timestamp",), "2026-11-12T21:24:55.000Z"),
])
def test_any_signed_field_changed_breaks_the_signature(path, value):
    resp = copy.deepcopy(QUOTES["live_cashout"])
    target = resp
    for k in path[:-1]:
        target = target[k]
    target[path[-1]] = value
    assert not intents.verify_quote(resp)


def test_a_live_quote_signs_its_own_deadline_and_a_dry_one_the_requested_one():
    """1Click's live signature covers the deposit address's deadline, not the request's echoed one (which
    is why check_quote also checks the signed deadline doesn't close before the requested one)."""
    live = copy.deepcopy(QUOTES["live_cashout"])
    live["quoteRequest"]["deadline"] = "2030-01-01T00:00:00.000Z"
    assert intents.verify_quote(live)
    dry = copy.deepcopy(QUOTES["fresh_dry"])
    dry["quoteRequest"]["deadline"] = "2030-01-01T00:00:00.000Z"
    assert not intents.verify_quote(dry)


def test_unsigned_extras_dont_matter_and_garbage_signatures_fail():
    resp = copy.deepcopy(QUOTES["live_cashout"])
    resp["correlationId"] = "x"
    resp["quoteRequest"]["appFees"] = []
    resp["quoteRequest"]["insured"] = True
    assert intents.verify_quote(resp), "1Click doesn't sign these, so they can't be relied on (and aren't)"
    for bad in ("", "ed25519:", "ed25519:0OIl", "ed25519:" + near.b58encode(bytes(64)), None):
        resp["signature"] = bad
        assert not intents.verify_quote(resp)


# ---- 2. checking a quote against what was asked ------------------------------------------------------
def _sent(resp: dict) -> dict:
    return {k: v for k, v in resp["quoteRequest"].items()
            if k in intents._REQ_ALWAYS or k == "referral"}


def _now(resp: dict) -> float:
    return intents.parse_iso(resp["timestamp"])


def test_a_real_live_cashout_quote_passes_every_check():
    resp = QUOTES["live_cashout"]
    q = intents.check_quote(resp, _sent(resp), tok(intents.NEAR_USDC), tok("base:usdc"), now=_now(resp))
    assert q.kind == "cashout" and q.deposit_address == resp["quote"]["depositAddress"] and q.amount_in == 2_000_000
    expected, worst = q.fee_bps()
    assert Decimal(0) < expected < Decimal(20) and expected < worst < Decimal(150)
    s = q.summary()
    assert s["amount_in"] == "2" and s["amount_out"] == "1.997396" and s["time_estimate_s"] == 22


def test_a_real_live_fund_quote_passes_every_check():
    resp = QUOTES["live_fund"]
    q = intents.check_quote(resp, _sent(resp), tok("base:usdc"), tok(intents.NEAR_USDC), now=_now(resp))
    assert q.kind == "fund" and q.amount_out == 1_000_000 and q.deposit_address.startswith("0x")
    assert q.min_amount_in <= q.amount_in


@pytest.mark.parametrize("field,value", [
    ("recipient", "0x000000000000000000000000000000000000dead"), ("refundTo", "shop.near"),
    ("originAsset", BASE_USDC), ("destinationAsset", "nep141:eth-0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.omft.near"),
    ("amount", "2000001"), ("deadline", "2026-11-12T21:54:55.691Z"), ("swapType", "EXACT_OUTPUT"),
    ("slippageTolerance", 50), ("refundType", "INTENTS"), ("recipientType", "INTENTS"), ("depositType", "INTENTS"),
    ("dry", True), ("referral", "someone-else"),
])
def test_a_signed_quote_for_other_terms_is_refused(field, value):
    """The signature is 1Click's, but the quote is not the one we asked for: refused on that field."""
    resp = QUOTES["live_cashout"]
    sent = {**_sent(resp), field: value}
    with pytest.raises(intents.QuoteRejected) as e:
        intents.check_quote(resp, sent, tok(intents.NEAR_USDC), tok("base:usdc"), now=_now(resp))
    assert e.value.field == field


def test_recipient_case_only_matters_off_evm():
    resp = QUOTES["live_cashout"]
    sent = {**_sent(resp), "recipient": PAYEE.upper().replace("0X", "0x")}
    intents.check_quote(resp, sent, tok(intents.NEAR_USDC), tok("base:usdc"), now=_now(resp))


def test_fee_floor_and_deadline_limits():
    resp = QUOTES["live_cashout"]
    args = (resp, _sent(resp), tok(intents.NEAR_USDC), tok("base:usdc"))
    with pytest.raises(intents.QuoteRejected) as e:
        intents.check_quote(*args, now=_now(resp), max_fee_bps=50)
    assert e.value.field == "fee"
    with pytest.raises(intents.QuoteRejected) as e:
        intents.check_quote(*args, now=_now(resp), min_out=1_990_000)
    assert e.value.field == "minAmountOut"
    with pytest.raises(intents.QuoteRejected) as e:
        intents.check_quote(*args, now=intents.parse_iso(resp["quoteRequest"]["deadline"]) + 1)
    assert e.value.field == "deadline"
    with pytest.raises(intents.QuoteRejected) as e:
        intents.check_quote(*args, now=_now(resp), manager_key=str(ONECLICK_KEY.public_key))
    assert e.value.field == "signature"


# ---- the fake 1Click, for everything that needs a server -------------------------------------------
@pytest.fixture
def oneclick(monkeypatch):
    f = FakeOneClick(ONECLICK_KEY).start()
    monkeypatch.setattr(intents, "ONE_CLICK_URL", f.url)
    monkeypatch.setattr(intents, "ONE_CLICK_MANAGER_KEY", str(ONECLICK_KEY.public_key))
    monkeypatch.setattr(intents.time, "sleep", lambda s: None)  # backoff and polling, instantly
    yield f
    f.stop()


def client(**kw) -> intents.OneClick:
    return intents.OneClick(retries=kw.pop("retries", 3), backoff=0.0, **kw)


@pytest.fixture
def mainnet(monkeypatch, oneclick):
    """A fresh in-memory NEAR "mainnet": the shop holds 10 USDC, its relayer 1 NEAR; a registered buyer."""
    c = FakeNear(usdc=MAINNET_USDC).start()
    c.add_account("shop.near", SHOP, near_amount=0.05, usdc=10_000_000)
    c.add_account("relay.near", RELAYER, near_amount=1)
    c.add_account("buyer.near", near_amount=0.01)
    c.add_account("newbuyer.near", register=False)
    monkeypatch.setitem(shop_mod.RPC_URLS, NET, c.url)
    monkeypatch.setattr(near_x402.ExactNearFacilitator, "poll_seconds", 0.01)
    oneclick.chain = c
    yield c
    c.stop()


@pytest.mark.parametrize("field,edit", [
    ("recipient", lambda r: r["quoteRequest"].update(recipient="0x000000000000000000000000000000000000dead")),
    ("refundTo", lambda r: r["quoteRequest"].update(refundTo="thief.near")),
    ("originAsset", lambda r: r["quoteRequest"].update(originAsset=BASE_USDC)),
    ("destinationAsset", lambda r: r["quoteRequest"].update(destinationAsset="nep141:sol.omft.near")),
    ("amount", lambda r: r["quoteRequest"].update(amount="5000001")),
    ("deadline", lambda r: r["quoteRequest"].update(deadline="2099-01-01T00:00:00.000Z")),
    ("swapType", lambda r: r["quoteRequest"].update(swapType="EXACT_OUTPUT")),
    ("dry", lambda r: r["quoteRequest"].update(dry=True)),
    ("amountIn", lambda r: r["quote"].update(amountIn="5000002")),
    ("amountOut", lambda r: r["quote"].update(minAmountOut=str(int(r["quote"]["amountOut"]) + 1))),
    ("fee", lambda r: r["quote"].update(amountOutUsd="3.5")),
    ("depositAddress", lambda r: r["quote"].pop("depositAddress")),
    ("depositAddress", lambda r: r["quote"].update(depositAddress="0x" + "ab" * 20)),
    ("deadline", lambda r: r["quote"].update(deadline=r["quoteRequest"]["deadline"][:-5] + "000Z")),
])
def test_forged_cashout_quotes_are_refused_even_when_signed(oneclick, mainnet, field, edit):
    """1Click (here, a fake signing with the trusted key) returns a quote for other terms: refused, unpaid."""
    def forge(resp):
        edit(resp)
        resp["signature"] = "ed25519:" + near.b58encode(ONECLICK_KEY.sign(intents.quote_message(intents.signed_quote_json(resp))))
        return resp
    oneclick.tamper = forge
    with pytest.raises(intents.QuoteRejected) as e:
        intents.cashout_quote(client(), usd="5", destination="base:usdc", recipient=PAYEE, shop="shop.near")
    assert e.value.field == field
    assert mainnet.included == [] and mainnet.usdc("shop.near") == 10_000_000


@pytest.mark.parametrize("kind", ["unsigned", "altered"])
def test_quotes_1click_didnt_sign_are_refused(oneclick, kind):
    if kind == "unsigned":
        oneclick.unsigned = True
    else:
        oneclick.tamper = lambda r: (r["quoteRequest"].update(recipient="0x" + "be" * 20), r)[1]
    with pytest.raises(intents.QuoteRejected) as e:
        intents.cashout_quote(client(), usd="5", destination="base:usdc", recipient=PAYEE, shop="shop.near", dry=True)
    assert e.value.field == "signature"


def test_fund_quotes_must_pay_out_exactly_what_was_asked(oneclick):
    def short(resp):
        resp["quote"].update(amountOut="999999", minAmountOut="999999")
        resp["signature"] = "ed25519:" + near.b58encode(ONECLICK_KEY.sign(intents.quote_message(intents.signed_quote_json(resp))))
        return resp
    oneclick.tamper = short
    with pytest.raises(intents.QuoteRejected) as e:
        intents.fund_quote(client(), usd="1", origin="base:usdc", recipient="buyer.near", dry=True)
    assert e.value.field == "amountOut"


# ---- 3. tokens, amounts, addresses and the client ---------------------------------------------------
@pytest.mark.parametrize("spec,asset", [
    ("base:usdc", BASE_USDC),
    ("BASE:USDC", BASE_USDC),
    ("arbitrum:usdc", "nep141:arb-0xaf88d065e77c8cc2239327c5edb3a432268e5831.omft.near"),
    ("arb:usdt", "nep141:arb-0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9.omft.near"),  # listed as USDT0
    ("sol:sol", "nep141:sol.omft.near"),
    ("solana:usdc", "nep141:sol-5ce3bf3a31af18be40ba30f721101b4341690186.omft.near"),
    ("eth:eth", "nep141:eth.omft.near"),
    ("btc:btc", "nep141:btc.omft.near"),
    ("near:near", "nep141:wrap.near"),
    ("near:usdc", intents.NEAR_USDC),
    ("bsc:usdc", "nep141:bnb-0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d.omdep.near"),  # not the DEPRECATED one
    (intents.NEAR_USDC, intents.NEAR_USDC),
])
def test_tokens_resolve_from_chain_and_symbol(spec, asset):
    assert tok(spec).asset_id == asset


def test_token_resolution_explains_what_it_cant_find():
    with pytest.raises(intents.IntentsError, match="doesn't support the chain 'dogechain'"):
        tok("dogechain:usdc")
    with pytest.raises(intents.IntentsError, match="no PEPE on Base .*USDC"):
        tok("base:pepe")
    with pytest.raises(intents.IntentsError, match="chain:symbol"):
        tok("usdc")
    with pytest.raises(intents.IntentsError, match="doesn't list"):
        tok("nep141:nope.near")
    assert tok("bsc:usdc").decimals == 18 and tok("btc:btc").decimals == 8
    hyper = [t for t in TOKEN_LIST if t.chain == "hypercore" and t.symbol == "USDC"]
    assert len(hyper) == 2 and tok("hypercore:usdc") in hyper


def test_token_list_is_cached(oneclick):
    c = client()
    c.tokens(), c.tokens(), c.token("base:usdc")
    assert sum(1 for r in oneclick.requests if r[1] == "/v0/tokens") == 1
    c.tokens(fresh=True)
    assert sum(1 for r in oneclick.requests if r[1] == "/v0/tokens") == 2


@pytest.mark.parametrize("text,atomic", [("5", 5_000_000), ("$5", 5_000_000), ("0.01", 10_000), ("1,000.50", 1_000_500_000),
                                         ("0.000001", 1), (Decimal("2.5"), 2_500_000), ("10000", 10_000_000_000)])
def test_usd_amounts(text, atomic):
    assert intents.usd_atomic(text) == atomic


@pytest.mark.parametrize("text", ["0", "-1", "abc", "", "0.0000001", "1.2345678", "10000.01", "NaN", "inf"])
def test_bad_usd_amounts(text):
    with pytest.raises(intents.IntentsError):
        intents.usd_atomic(text)


def test_formatting_amounts_in_any_decimals():
    assert intents.fmt_amount(1_010_208, 6) == "1.010208"
    assert intents.fmt_amount(2_000_000, 6) == "2"
    assert intents.fmt_amount("295869975164999019544721", 24) == "0.295869975164999019544721"
    assert intents.fmt_amount(5 * 10**18 + 1, 18) == "5.000000000000000001"
    assert intents.fmt_amount(12_448_732, 9) == "0.012448732"
    assert intents.fmt_amount("not a number", 6) == "?"


def test_addresses_per_chain():
    assert intents.check_address("base", "0xA11CE57B2F0C88DE41A9B3C07E6D1F45A3C0D9F2") == PAYEE
    assert intents.check_address("near", "shop.near") == "shop.near"
    assert intents.check_address("sol", "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM")
    assert intents.check_address("btc", "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh")
    for chain, bad in (("base", "0x123"), ("arb", "shop.near"), ("near", "Shop.Near"), ("sol", "0xabc"),
                       ("sol", "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWW0"), ("btc", "0xabc"), ("tron", "has space")):
        with pytest.raises(intents.IntentsError):
            intents.check_address(chain, bad)


@pytest.mark.parametrize("words,want", [
    ("5 from base:usdc", {"usd": "5", "direction": "from", "token": "base:usdc"}),
    ("5 from sol:usdc refund 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM to me.near send",
     {"refund": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "recipient": "me.near", "send": True}),
    ("12.5 to base:usdc 0xAbC0000000000000000000000000000000000001 min 12.3",
     {"direction": "to", "recipient": "0xAbC0000000000000000000000000000000000001", "min": "12.3"}),
    ("5 to sol:sol 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM slippage 50 max-fee 200",
     {"slippage": 50, "max_fee": 200}),
])
def test_parsing_what_people_type(words, want):
    got = intents.parse_words(words.split())
    assert {k: got[k] for k in want} == want


@pytest.mark.parametrize("words", ["", "five from base:usdc", "5 base:usdc", "5 from", "5 from base:usdc please",
                                   "5 to base:usdc 0xabc slippage 9999"])
def test_parsing_refuses_what_it_doesnt_understand(words):
    with pytest.raises(intents.IntentsError):
        intents.parse_words(words.split())


def test_the_jwt_is_sent_as_a_bearer_token_only_when_set(oneclick):
    client(jwt="test-jwt").tokens(fresh=True)
    client().quote(intents.quote_request(dry=True, swap_type="EXACT_OUTPUT", origin=tok("base:usdc"),
                                         destination=tok(intents.NEAR_USDC), amount=1_000_000, refund_to=PAYEE,
                                         recipient="buyer.near", window=600))
    auth = [r[3] for r in oneclick.requests]
    assert auth == ["Bearer test-jwt", None]


def test_busy_answers_are_retried_and_refusals_are_not(oneclick):
    oneclick.errors = [(429, {"message": "slow down"}), (502, {"message": "bad gateway"})]
    assert client().tokens(fresh=True)
    assert len(oneclick.requests) == 3
    oneclick.errors = [(503, {})] * 5
    with pytest.raises(intents.OneClickError, match="busy"):
        client(retries=2).tokens(fresh=True)
    n = len(oneclick.requests)
    oneclick.errors = [(400, {"message": "refundTo is not valid"})]
    with pytest.raises(intents.OneClickError, match="refundTo is not valid") as e:
        client().tokens(fresh=True)
    assert e.value.status == 400 and len(oneclick.requests) == n + 1, "a 4xx is an answer, not retried"


def test_unreachable_1click_is_an_error_after_retries(monkeypatch):
    monkeypatch.setattr(intents.time, "sleep", lambda s: None)
    with pytest.raises(intents.OneClickError, match="couldn't reach 1Click"):
        intents.OneClick(url="http://127.0.0.1:9/v0", retries=1, backoff=0, timeout=1).tokens(fresh=True)


def test_route_minimums_are_reported(oneclick):
    oneclick.min_amount = 5_926_164
    with pytest.raises(intents.OneClickError) as e:
        intents.fund_quote(client(), usd="1", origin="btc:btc", recipient="buyer.near", dry=True)
    assert e.value.min_amount == 5_926_164 and "try at least" in str(e.value)


# ---- 4. status -------------------------------------------------------------------------------------
def _live_fund(oneclick, mainnet, **kw):
    return intents.fund_quote(client(), usd=kw.pop("usd", "5"), origin=kw.pop("origin", "base:usdc"),
                              recipient=kw.pop("recipient", "buyer.near"), refund_to=kw.pop("refund_to", PAYEE), **kw)


@pytest.mark.parametrize("script,final", [
    (["PENDING_DEPOSIT", "KNOWN_DEPOSIT_TX", "PROCESSING", "SUCCESS"], "SUCCESS"),
    (["PENDING_DEPOSIT", "INCOMPLETE_DEPOSIT", "PROCESSING", "REFUNDED"], "REFUNDED"),
    (["PENDING_DEPOSIT", "PROCESSING", "FAILED"], "FAILED"),
])
def test_status_is_followed_to_the_end(oneclick, mainnet, ledger, script, final):
    q = _live_fund(oneclick, mainnet)
    oneclick.statuses[q.deposit_address] = list(script)
    seen = []
    b = intents.Books(ledger)
    intents.record(b, q)
    out = intents.track(client(), q.deposit_address, books=b, wait=60, poll=0.01, on_status=seen.append)
    assert out["status"] == final and not out["timed_out"] and out["quote_signed"] is True
    assert seen == list(dict.fromkeys(script))
    assert ledger.transfer(q.deposit_address)["status"] == final
    if final == "SUCCESS":
        assert out["destination_txs"][0]["hash"].startswith("0x")
        assert json.loads(ledger.transfer(q.deposit_address)["dest_txs"])[0]["hash"] == out["destination_txs"][0]["hash"]
    if final == "REFUNDED":
        assert out["refund_reason"] == "DEADLINE_EXCEEDED" and out["refunded"]


def test_a_status_still_going_when_the_wait_ends_says_so(oneclick, mainnet):
    q = _live_fund(oneclick, mainnet)
    out = intents.track(client(), q.deposit_address, wait=0.05, poll=0.01)
    assert out["status"] == "PENDING_DEPOSIT" and out["timed_out"]
    once = intents.track(client(), q.deposit_address)
    assert once["status"] == "PENDING_DEPOSIT" and not once["timed_out"]
    with pytest.raises(intents.OneClickError, match="not found"):
        intents.track(client(), "0" * 64)


# ---- 5a. fund ---------------------------------------------------------------------------------------
def test_fund_asks_for_exactly_the_usdc_and_refunds_to_the_payer(oneclick, mainnet):
    q = _live_fund(oneclick, mainnet, usd="5", origin="sol:usdc", refund_to="9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM")
    req = oneclick.requests[-1][2]
    assert (req["swapType"], req["destinationAsset"], req["amount"], req["recipient"], req["recipientType"],
            req["depositType"], req["refundType"]) == ("EXACT_OUTPUT", intents.NEAR_USDC, "5000000", "buyer.near",
                                                     "DESTINATION_CHAIN", "ORIGIN_CHAIN", "ORIGIN_CHAIN")
    assert req["refundTo"] == "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM" and req["dry"] is False
    assert req["slippageTolerance"] == intents.STABLE_SLIPPAGE_BPS, "stablecoin to stablecoin: a small buffer"
    volatile = _live_fund(oneclick, mainnet, origin="sol:sol", refund_to="9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM")
    assert volatile.request["slippageTolerance"] == intents.DEFAULT_SLIPPAGE_BPS
    assert q.amount_out == 5_000_000 and q.amount_in > 5_000_000 and q.deposit_address
    assert mainnet.included == [], "funding asks 1Click for an address; nothing on NEAR moves"


def test_fund_refuses_what_would_lose_money(oneclick, mainnet):
    with pytest.raises(intents.IntentsError, match="isn't registered with USDC"):
        _live_fund(oneclick, mainnet, recipient="newbuyer.near")
    with pytest.raises(intents.IntentsError, match="testnet"):
        _live_fund(oneclick, mainnet, recipient="buyer.testnet")
    with pytest.raises(intents.IntentsError, match="address you pay from"):
        _live_fund(oneclick, mainnet, refund_to="")
    with pytest.raises(intents.IntentsError, match="not a valid Base refund address"):
        _live_fund(oneclick, mainnet, refund_to="buyer.near")
    with pytest.raises(intents.IntentsError, match="already USDC on NEAR"):
        _live_fund(oneclick, mainnet, origin="near:usdc", refund_to="buyer.near")
    assert not [r for r in oneclick.requests if r[1] == "/v0/quote"], "refused before asking 1Click"


def test_a_dry_fund_quote_needs_no_refund_address(oneclick):
    q = intents.fund_quote(client(), usd="1", origin="eth:eth", recipient="buyer.near", dry=True)
    assert q.dry and not q.deposit_address and q.request["refundTo"] == intents.SAMPLE_ADDRESS["evm"]
    assert q.origin.decimals == 18 and Decimal(q.summary()["amount_in"]) > 0


# ---- 5b. cash out end to end --------------------------------------------------------------------------
@pytest.fixture
def ledger(tmp_path):
    return books.Ledger(tmp_path / "books.db")


def _cashout(oneclick, mainnet, ledger=None, usd="5", **kw):
    q = intents.cashout_quote(client(), usd=usd, destination="base:usdc", recipient=PAYEE, shop="shop.near")
    b = intents.Books(ledger)
    return q, b


def _send(q, b, **kw):
    return intents.cashout_send(client(), q, shop="shop.near", shop_key=SHOP, relayer="relay.near",
                                relayer_key=RELAYER, books=b, **kw)


def test_a_cashout_registers_the_deposit_address_and_the_shop_pays_no_gas(oneclick, mainnet, ledger):
    q, b = _cashout(oneclick, mainnet, ledger)
    deposit = q.deposit_address
    assert near.is_account_id(deposit) and len(deposit) == 64 and deposit not in mainnet.registered
    shop_near, relay_near = mainnet.near_balance("shop.near"), mainnet.near_balance("relay.near")
    out = _send(q, b, wait=60, poll=0.01)
    # 1. the relayer registered the deposit address with USDC, and paid for it
    storage, transfer = mainnet.included
    assert storage["signer_id"] == "relay.near" and storage["receiver_id"] == MAINNET_USDC
    call = storage["actions"][0][1]
    assert call.method_name == "storage_deposit" and call.deposit == intents.STORAGE_DEPOSIT
    assert call.json_args() == {"account_id": deposit, "registration_only": True}
    # 2. the shop's signed transfer went in the relayer's transaction
    assert transfer["signer_id"] == "relay.near" and transfer["receiver_id"] == "shop.near"
    d = transfer["actions"][0][1]
    assert d["sender_id"] == "shop.near" and d["receiver_id"] == MAINNET_USDC
    assert d["actions"][0][1].json_args() == {"receiver_id": deposit, "amount": "5000000"}
    assert mainnet.usdc("shop.near") == 5_000_000 and mainnet.usdc(deposit) == 5_000_000
    assert mainnet.near_balance("shop.near") == shop_near, "the shop spends no NEAR"
    assert relay_near - mainnet.near_balance("relay.near") >= intents.STORAGE_DEPOSIT, "the relayer pays storage and gas"
    # 3. 1Click was told, and followed to SUCCESS
    assert oneclick.submitted == [{"txHash": out["near_tx"], "depositAddress": deposit, "nearSenderAccount": "relay.near"}]
    assert out["status"] == "SUCCESS" and out["destination_txs"] and out["storage_tx"] == near.b58encode(storage["hash"])
    row = ledger.transfer(deposit)
    assert row["status"] == "SUCCESS" and row["kind"] == "cashout" and row["usd"] == "5" and row["deposit_tx"] == out["near_tx"]
    assert intents.verify_quote(json.loads(row["quote"]), str(ONECLICK_KEY.public_key)), "the signed quote is kept"


def test_a_cashout_run_again_pays_nothing_more(oneclick, mainnet, ledger):
    q, b = _cashout(oneclick, mainnet, ledger)
    first = _send(q, b)
    assert first["status"] == "PENDING_DEPOSIT" and len(mainnet.included) == 2
    for _ in range(3):
        again = _send(q, b)
        assert again["near_tx"] == first["near_tx"]
    assert len(mainnet.included) == 2 and mainnet.usdc("shop.near") == 5_000_000
    intents.track(client(), q.deposit_address, books=b, wait=60, poll=0.01)
    done = _send(q, b)
    assert done["status"] == "SUCCESS" and len(mainnet.included) == 2


def test_a_crash_after_sending_is_resumed_without_paying_twice(oneclick, mainnet, ledger):
    """The process died after the transfer went out but before it recorded the transaction: the kept
    signed transfer's nonce is spent, and the deposit shows the money, so nothing is sent again."""
    q, b = _cashout(oneclick, mainnet, ledger)
    _send(q, b)
    ledger.update_transfer(q.deposit_address, deposit_tx="", status="sending")
    out = _send(q, b)
    assert len(mainnet.included) == 2 and mainnet.usdc("shop.near") == 5_000_000
    assert out["status"] in ("PENDING_DEPOSIT", "PROCESSING")


def test_a_kept_signed_transfer_is_reused_not_signed_again(oneclick, mainnet, ledger):
    """Signed and kept, then the process died before sending: the same signed transfer is what goes out."""
    q, b = _cashout(oneclick, mainnet, ledger)
    intents.record(b, q)
    req = intents._requirements(MAINNET_USDC, q.deposit_address, q.amount_in)
    kept = near_x402.ExactNearClient("shop.near", SHOP).create_payment_payload(req)["signedDelegateAction"]
    ledger.update_transfer(q.deposit_address, signed=kept, status="signed")
    mainnet.advance(3)  # a block later, a fresh signature would differ
    _send(q, b)
    sent = mainnet.included[-1]["actions"][0][1]
    assert sent["signature"][1] == near.SignedDelegateAction.from_base64(kept).signature
    assert mainnet.usdc("shop.near") == 5_000_000


def test_an_expired_unsent_transfer_is_replaced_and_never_runs(oneclick, mainnet, ledger):
    q, b = _cashout(oneclick, mainnet, ledger)
    intents.record(b, q)
    req = intents._requirements(MAINNET_USDC, q.deposit_address, q.amount_in)
    stale = near_x402.ExactNearClient("shop.near", SHOP).create_payment_payload(req)["signedDelegateAction"]
    ledger.update_transfer(q.deposit_address, signed=stale, status="signed")
    mainnet.advance(intents.DELEGATE_SECONDS + 5)  # past its last block, never sent: it can never run
    _send(q, b)
    stale_sig = near.SignedDelegateAction.from_base64(stale).signature
    assert all(a[1]["signature"][1] != stale_sig for tx in mainnet.included for a in tx["actions"] if a[0] == "Delegate")
    assert mainnet.usdc("shop.near") == 5_000_000 and ledger.transfer(q.deposit_address)["signed"] != stale


def test_a_timed_out_send_that_landed_is_found_not_resent(oneclick, mainnet, ledger):
    q, b = _cashout(oneclick, mainnet, ledger)
    mainnet.registered.add(q.deposit_address)  # already registered: the next send is the transfer itself
    mainnet.timeout_next = 1
    out = _send(q, b)
    assert len(mainnet.included) == 1 and mainnet.usdc("shop.near") == 5_000_000 and out["near_tx"]
    assert out["storage_tx"] == "already"


def test_a_lost_transfer_goes_again_and_pays_once(oneclick, mainnet, ledger, monkeypatch):
    monkeypatch.setattr(near_x402.ExactNearFacilitator, "lost_after", 0.05)
    q, b = _cashout(oneclick, mainnet, ledger)
    mainnet.registered.add(q.deposit_address)
    mainnet.drop_next = 1  # the RPC answers a timeout and the transaction never reaches a block
    _send(q, b)
    assert len(mainnet.included) == 1 and mainnet.usdc("shop.near") == 5_000_000
    assert mainnet.usdc(q.deposit_address) == 5_000_000


def test_a_registration_that_timed_out_is_looked_up_not_paid_twice(oneclick, mainnet, ledger):
    q, b = _cashout(oneclick, mainnet, ledger)
    mainnet.timeout_next = 1  # the storage_deposit lands but the RPC gives up waiting
    _send(q, b)
    storage = [tx for tx in mainnet.included if tx["receiver_id"] == MAINNET_USDC]
    assert len(storage) == 1 and mainnet.usdc("shop.near") == 5_000_000


def test_a_token_that_refuses_the_transfer_moves_nothing_and_a_retry_works(oneclick, mainnet, ledger):
    q, b = _cashout(oneclick, mainnet, ledger)
    mainnet.fail_transfers = 1
    with pytest.raises(intents.IntentsError, match="failed on-chain and nothing moved"):
        _send(q, b)
    assert mainnet.usdc("shop.near") == 10_000_000 and ledger.transfer(q.deposit_address)["status"] == "failed"
    _send(q, b)
    assert mainnet.usdc("shop.near") == 5_000_000 and mainnet.usdc(q.deposit_address) == 5_000_000


def test_a_cashout_the_shop_cant_cover_sends_nothing(oneclick, mainnet, ledger):
    relay_near = mainnet.near_balance("relay.near")
    q, b = _cashout(oneclick, mainnet, ledger, usd="25")
    with pytest.raises(intents.IntentsError, match="holds 10 USDC, less than the 25"):
        _send(q, b)
    assert mainnet.included == [] and mainnet.near_balance("relay.near") == relay_near, "not even the registration"


def test_cashout_refuses_bad_setups(oneclick, mainnet, ledger):
    q, b = _cashout(oneclick, mainnet, ledger)
    with pytest.raises(intents.IntentsError, match="different account"):
        intents.cashout_send(client(), q, shop="shop.near", shop_key=SHOP, relayer="shop.near", relayer_key=SHOP, books=b)
    with pytest.raises(intents.IntentsError, match="refunds to another account"):
        intents.cashout_send(client(), q, shop="other.near", shop_key=SHOP, relayer="relay.near", relayer_key=RELAYER, books=b)
    with pytest.raises(intents.IntentsError, match="testnet"):
        intents.cashout_quote(client(), usd="5", destination="base:usdc", recipient=PAYEE, shop="shop.testnet")
    with pytest.raises(intents.IntentsError, match="not a valid Base recipient"):
        intents.cashout_quote(client(), usd="5", destination="base:usdc", recipient="shop.near", shop="shop.near")
    dry = intents.cashout_quote(client(), usd="5", destination="base:usdc", recipient=PAYEE, shop="shop.near", dry=True)
    with pytest.raises(intents.IntentsError, match="live cash-out"):
        _send(dry, b)
    assert mainnet.included == []


def test_a_kept_quote_is_checked_again_before_its_paid(oneclick, mainnet, ledger):
    q, b = _cashout(oneclick, mainnet, ledger)
    row = intents.record(b, q)
    again = intents.quote_from_record(row, client())
    assert again.deposit_address == q.deposit_address and again.amount_in == q.amount_in
    with pytest.raises(intents.QuoteRejected, match="deadline"):
        intents.quote_from_record(row, client(), now=intents.parse_iso(q.pay_by) + 1)
    forged = {**row, "recipient": "0x" + "be" * 20}
    with pytest.raises(intents.QuoteRejected, match="record"):
        intents.quote_from_record(forged, client())


def test_the_ledger_keeps_one_row_per_deposit(ledger):
    row = {"deposit": "ab" * 32, "kind": "cashout", "usd": "5", "origin": "o", "destination": "d", "recipient": PAYEE,
           "refund_to": "shop.near", "amount_in": "5000000", "amount_out": "4990000", "status": "quoted",
           "quote": "{}", "signature": "sig"}
    ledger.add_transfer(row)
    ledger.update_transfer(row["deposit"], status="sent", signed="kept", nonsense="ignored")
    ledger.add_transfer({**row, "status": "quoted"})  # a retry never resets it
    got = ledger.transfer(row["deposit"])
    assert got["status"] == "sent" and got["signed"] == "kept"
    assert ledger.find_transfer("abab") is None, "too short to be sure"
    assert ledger.find_transfer("ABABAB")["deposit"] == row["deposit"]
    assert ledger.find_transfer("abab%") is None
    assert [r["deposit"] for r in ledger.recent_transfers()] == [row["deposit"]]


# ---- 6. /shop quote | fund | cashout | track | transfers ---------------------------------------------
class Ctx:
    """Just enough of Hermes's plugin context: settings, and optionally a chat choice and live messages."""

    def __init__(self, settings: dict, choice=None):
        self.settings, self.profile_name, self.choice = settings, "shop", choice
        self.asked, self.posted = [], []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def ask_choice(self, question, choices):
        self.asked.append((question, choices))
        return self.choice

    def post_message(self, text):
        ctx = self

        class Handle:
            def edit(self, t):
                ctx.posted.append(t)
        self.posted.append(text)
        return Handle()


MAINNET_SHOP = {"network": "near:mainnet", "pay_to": "shop.near", "near_relayer_account": "relay.near",
                "buyer_near_account": "buyer.near", "services": []}


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("EMPORIUM_NEAR_PAYOUT_KEY", SHOP.secret())
    monkeypatch.setenv("EMPORIUM_NEAR_RELAYER_KEY", RELAYER.secret())
    monkeypatch.delenv("EMPORIUM_BUYER_KEY", raising=False)
    monkeypatch.delenv("EMPORIUM_ONECLICK_JWT", raising=False)


def slash(ctx, text):
    from emporium import commands
    return commands.make_slash(ctx)(text)


def test_slash_quote_both_ways(oneclick, keys):
    ctx = Ctx(MAINNET_SHOP)
    fund = slash(ctx, "quote 1 from base:usdc")
    assert "dry" in fund and "exactly 1 USDC on NEAR to buyer.near" in fund and "✓ signed by 1Click" in fund
    out = slash(ctx, "quote 5 to sol:sol")
    assert "exactly 5 USDC on NEAR from shop.near" in out and "SOL on Solana" in out
    assert not [r for r in oneclick.requests if r[1] == "/v0/quote" and r[2]["dry"] is not True], "dry quotes only"
    assert "Emporium:" in slash(ctx, "quote 5 from dogechain:usdc")


def test_slash_fund_prints_where_to_pay_and_keeps_the_quote(oneclick, mainnet, keys):
    ctx = Ctx(MAINNET_SHOP)
    out = slash(ctx, f"fund 5 from base:usdc refund {PAYEE}")
    row = books.Ledger.open().recent_transfers()[0]
    assert row["kind"] == "fund" and row["recipient"] == "buyer.near" and row["refund_to"] == PAYEE
    assert f"to {row['deposit']}" in out and "USDC on Base" in out and "Nothing is spent until a shop delivers" in out
    assert "Set buyer_near_account" in slash(Ctx({**MAINNET_SHOP, "buyer_near_account": ""}), "fund 5 from base:usdc refund 0x" + "1" * 40)
    assert "isn't set" in slash(ctx, f"fund 5 from base:usdc refund {PAYEE} send")
    assert mainnet.included == []


def test_slash_cashout_asks_then_sends(oneclick, mainnet, keys):
    ctx = Ctx(MAINNET_SHOP, choice="send")
    out = slash(ctx, f"cashout 5 to base:usdc {PAYEE}")
    assert "Send it?" in ctx.asked[0][0] and "✓ signed by 1Click" in ctx.asked[0][0]
    assert "Sent 5 USDC from shop.near" in out and "Status: SUCCESS" in out and "paid out on USDC on Base" in out
    assert mainnet.usdc("shop.near") == 5_000_000 and len(mainnet.included) == 2
    assert any("SUCCESS" in p for p in ctx.posted), "live status in the chat"
    assert "SUCCESS" in slash(ctx, "transfers")
    deposit = books.Ledger.open().recent_transfers()[0]["deposit"]
    assert "SUCCESS · cashout of $5" in slash(ctx, f"track {deposit[:10]}")


def test_slash_cashout_without_a_chat_choice_waits_for_confirm(oneclick, mainnet, keys):
    plain = Ctx(MAINNET_SHOP)
    plain.ask_choice = lambda q, c: None  # a surface that can't ask
    out = slash(plain, f"cashout 2.5 to arb:usdc {PAYEE}")
    assert "Nothing has moved" in out and "/shop cashout confirm" in out and mainnet.included == []
    prefix = out.split("/shop cashout confirm ")[1].split()[0]
    sent = slash(plain, f"cashout confirm {prefix}")
    assert "Sent 2.5 USDC" in sent and mainnet.usdc("shop.near") == 7_500_000
    again = slash(plain, f"cashout confirm {prefix}")
    assert "Status: SUCCESS" in again and mainnet.usdc("shop.near") == 7_500_000, "confirming twice pays once"


def test_slash_cashout_cancel_and_refusals(oneclick, mainnet, keys, monkeypatch):
    ctx = Ctx(MAINNET_SHOP, choice="cancel")
    assert "Cancelled" in slash(ctx, f"cashout 5 to base:usdc {PAYEE}")
    deposit = books.Ledger.open().recent_transfers()[0]["deposit"]
    assert "cancelled" in slash(ctx, f"cashout confirm {deposit}")
    assert "testnet" in slash(Ctx({**MAINNET_SHOP, "network": "near:testnet"}), f"cashout 5 to base:usdc {PAYEE}")
    monkeypatch.delenv("EMPORIUM_NEAR_PAYOUT_KEY")
    assert "EMPORIUM_NEAR_PAYOUT_KEY" in slash(ctx, f"cashout 5 to base:usdc {PAYEE}")
    assert "<your Base address>" in slash(ctx, "cashout 5 to base:usdc")
    assert mainnet.included == [] and mainnet.usdc("shop.near") == 10_000_000


def test_terminal_commands(oneclick, keys, capsys):
    from emporium import commands
    import argparse
    setup, handle = commands.make_cli(Ctx(MAINNET_SHOP))
    parser = argparse.ArgumentParser()
    setup(parser)
    args = parser.parse_args(["quote", "1", "from", "arb:usdc"])
    handle(args)
    assert "exactly 1 USDC on NEAR" in capsys.readouterr().out


# ---- 7. optional: the buyer's own EVM wallet pays a fund deposit -------------------------------------
BUYER_EVM_KEY = "0x" + "22" * 32  # throwaway, only against the fake EVM node below


class FakeEvm:
    """Just enough of an EVM node's JSON-RPC: balances, gas, nonce, raw transactions and receipts."""

    def __init__(self, usdc_balance: int = 50_000_000, eth_balance: int = 10**16):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.usdc_balance, self.eth_balance = usdc_balance, eth_balance
        self.raw: list[str] = []
        self.reverts = False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                result = outer.handle(body["method"], body.get("params") or [])
                b = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def handle(self, method, params):
        from eth_account import Account
        if method == "eth_call":
            return hex(self.usdc_balance)
        if method == "eth_getBalance":
            return hex(self.eth_balance)
        if method == "eth_estimateGas":
            return hex(52_000)
        if method == "eth_getBlockByNumber":
            return {"baseFeePerGas": hex(10**7)}
        if method == "eth_maxPriorityFeePerGas":
            return hex(10**6)
        if method == "eth_getTransactionCount":
            return hex(7)
        if method == "eth_sendRawTransaction":
            self.raw.append(params[0])
            return "0x" + Account.recover_transaction(params[0]).lower()[2:].rjust(64, "0")
        if method == "eth_getTransactionReceipt":
            return {"status": "0x0" if self.reverts else "0x1", "transactionHash": params[0]} if self.raw else None
        return None

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def evm():
    node = FakeEvm()
    yield node
    node.stop()


def test_the_buyer_wallet_pays_the_deposit_once(oneclick, mainnet, ledger, evm):
    from eth_account import Account
    from eth_account.typed_transactions import TypedTransaction
    from hexbytes import HexBytes
    wallet = intents.evm_address(BUYER_EVM_KEY)
    q = _live_fund(oneclick, mainnet, usd="5", refund_to=wallet)
    b = intents.Books(ledger)
    out = intents.evm_pay(client(), q, BUYER_EVM_KEY, books=b, rpc_url=evm.url, poll=0.01)
    assert out["confirmed"] and out["status"] == "PENDING_DEPOSIT"
    tx = TypedTransaction.from_bytes(HexBytes(evm.raw[0])).as_dict()
    assert Account.recover_transaction(evm.raw[0]).lower() == wallet and tx["chainId"] == 8453 and tx["nonce"] == 7
    assert "0x" + bytes(tx["to"]).hex() == q.origin.contract.lower() and tx["value"] == 0
    data = bytes(tx["data"]).hex()
    assert data[:8] == "a9059cbb" and data[32:72] == q.deposit_address.lower()[2:] and int(data[72:], 16) == q.amount_in
    assert oneclick.submitted[-1] == {"txHash": out["tx"], "depositAddress": q.deposit_address}
    again = intents.evm_pay(client(), q, BUYER_EVM_KEY, books=b, rpc_url=evm.url, poll=0.01)
    assert again["tx"] == out["tx"] and set(evm.raw) == {evm.raw[0]}, "a retry rebroadcasts the same transaction"
    assert ledger.transfer(q.deposit_address)["deposit_tx"] == out["tx"]


def test_the_buyer_wallet_refuses_what_it_cant_pay(oneclick, mainnet, ledger, evm):
    wallet = intents.evm_address(BUYER_EVM_KEY)
    q = _live_fund(oneclick, mainnet, refund_to=PAYEE)
    with pytest.raises(intents.IntentsError, match="refunds to another address"):
        intents.evm_pay(client(), q, BUYER_EVM_KEY, rpc_url=evm.url)
    evm.usdc_balance = 1
    q = _live_fund(oneclick, mainnet, refund_to=wallet)
    with pytest.raises(intents.IntentsError, match="less than the"):
        intents.evm_pay(client(), q, BUYER_EVM_KEY, books=intents.Books(ledger), rpc_url=evm.url)
    q = _live_fund(oneclick, mainnet, origin="sol:usdc", refund_to="9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM")
    with pytest.raises(intents.IntentsError, match="by hand"):
        intents.evm_pay(client(), q, BUYER_EVM_KEY, rpc_url=evm.url)
    with pytest.raises(intents.IntentsError, match="not a valid EVM private key"):
        intents.evm_address("0xnope")
    assert evm.raw == []


def test_a_reverted_deposit_is_reported(oneclick, mainnet, ledger, evm):
    evm.reverts = True
    q = _live_fund(oneclick, mainnet, refund_to=intents.evm_address(BUYER_EVM_KEY))
    with pytest.raises(intents.IntentsError, match="reverted"):
        intents.evm_pay(client(), q, BUYER_EVM_KEY, books=intents.Books(ledger), rpc_url=evm.url, poll=0.01)
    assert ledger.transfer(q.deposit_address)["status"] == "failed"


def test_slash_fund_send_pays_from_the_buyer_wallet(oneclick, mainnet, keys, evm, monkeypatch):
    monkeypatch.setenv("EMPORIUM_BUYER_KEY", BUYER_EVM_KEY)
    monkeypatch.setitem(intents.EVM_RPC, "base", evm.url)
    out = slash(Ctx(MAINNET_SHOP), "fund 5 from base:usdc send")
    assert f"Paid the deposit from {intents.evm_address(BUYER_EVM_KEY)} (confirmed)" in out and len(evm.raw) == 1


def test_minimums_in_dollars_are_read_too():
    e = intents.OneClickError("1Click refused: Temporary swap limits: minimum swap amount is $100", 400)
    assert e.min_usd == Decimal(100) and e.min_amount is None


def test_chains_that_need_a_memo_get_a_memo_deposit(oneclick, mainnet, ledger):
    """Stellar takes deposits only with a memo: the quote is asked again in MEMO mode, the memo is shown,
    signed, kept, and sent along when the status is looked up."""
    q = _live_fund(oneclick, mainnet, origin="stellar:usdc", refund_to="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")
    assert q.request["depositMode"] == "MEMO" and q.deposit_memo
    assert json.loads(q.signed_json)["depositMemo"] == q.deposit_memo
    b = intents.Books(ledger)
    intents.record(b, q)
    assert intents.track(client(), q.deposit_address, books=b)["status"] == "PENDING_DEPOSIT"
    with pytest.raises(intents.OneClickError, match="depositMemo"):
        intents.track(client(), q.deposit_address)
