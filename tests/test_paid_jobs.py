"""End to end over real HTTP: the shop adapter, the official x402 SDK on both sides and the buyer
code, against a fake facilitator. The agent is stubbed; everything else is the shipped code."""

import json
from decimal import Decimal

import httpx
import pytest

from conftest import BUYER_KEY, SHOP_ADDR, as_settings

from emporium import buyer, ledger as books

POLICY = buyer.Policy(as_settings({"buyer_max_per_job": "$1.00", "buyer_daily_budget": "$2.00"}))


def hire(shop, service="echo", text="hello shop", **kw):
    return buyer.hire(shop.test_url, service, text, policy=kw.pop("policy", POLICY), key=kw.pop("key", BUYER_KEY),
                      ledger=kw.pop("ledger", None), **kw)


def test_storefront_catalog_and_402(shop):
    page = httpx.get(shop.test_url + "/")
    assert page.status_code == 200 and "Test Emporium" in page.text
    cat = httpx.get(shop.test_url + "/.well-known/emporium.json").json()
    assert [s["id"] for s in cat["services"]] == ["echo", "pricey", "search"]
    assert "instructions" not in json.dumps(cat)
    r = httpx.post(shop.test_url + "/jobs/echo", json={"input": "hi"})
    assert r.status_code == 402 and r.headers.get("PAYMENT-REQUIRED")
    assert not shop.stub.events, "no work before payment"


def test_paid_job_is_delivered_and_settled(shop, tmp_path):
    led = books.Ledger(tmp_path / "buyer.db")
    out = hire(shop, text="ping 123", ledger=led)
    assert out["output"] == "ECHO: ping 123"
    r = out["receipt"]
    assert r["amount"] == "$0.05" and r["pay_to"] == SHOP_ADDR and r["tx"].startswith("0x") and len(r["tx"]) == 66
    assert len(shop.facilitator.verified) == 1 and len(shop.facilitator.settled) == 1
    job = shop.books.recent_jobs(1)[0]
    assert job["status"] == books.SETTLED and job["tx"] == r["tx"]
    assert led.recent_purchases(1)[0]["status"] == books.BOUGHT
    # the agent saw the job framed, in its own chat
    ev = shop.stub.events[0]
    assert ev.source.chat_id == "job-" + job["id"] and "<<<BUYER INPUT\nping 123\nBUYER INPUT>>>" in ev.text


def test_failed_job_is_never_charged(shop):
    shop.stub.fail = True
    with pytest.raises(buyer.HireError, match="not charged"):
        hire(shop)
    assert shop.facilitator.settled == []
    assert shop.books.recent_jobs(1)[0]["status"] == books.FAILED


def test_timed_out_job_is_never_charged(shop):
    shop.stub.silent = True
    svc = shop.shop.service("echo")
    svc.timeout_seconds = 2
    with pytest.raises(buyer.HireError, match="not charged"):
        hire(shop)
    assert shop.facilitator.settled == []
    assert shop.books.recent_jobs(1)[0]["status"] == books.FAILED


@pytest.fixture
def no_backoff(monkeypatch):
    from emporium import adapter
    monkeypatch.setattr(adapter.time, "sleep", lambda s: None)
    return adapter


def test_settlement_failure_withholds_the_result(shop, no_backoff):
    shop.facilitator.fail_settle = True
    with pytest.raises(buyer.HireError) as e:
        hire(shop, text="secret sauce")
    assert "secret sauce" not in str(e.value)
    assert shop.books.recent_jobs(1)[0]["status"] == books.UNPAID
    assert len(shop.facilitator.settled) == no_backoff.SETTLE_ATTEMPTS


def test_flaky_settlement_is_retried_and_delivered(shop, no_backoff):
    shop.facilitator.fail_settle = 1  # the public facilitator sometimes fails to land the transfer once
    r = hire(shop, text="hello again")
    assert r["receipt"]["tx"].startswith("0x")
    assert len(shop.facilitator.settled) == 2
    assert shop.books.recent_jobs(1)[0]["status"] == books.SETTLED


def test_a_bad_authorization_is_not_retried(shop, no_backoff):
    shop.facilitator.fail_settle = True
    shop.facilitator.settle_reason = "invalid_exact_evm_insufficient_balance"
    with pytest.raises(buyer.HireError):
        hire(shop)
    assert len(shop.facilitator.settled) == 1


def test_invalid_payment_does_no_work(shop):
    shop.facilitator.invalid = True
    with pytest.raises(buyer.HireError):
        hire(shop)
    assert not shop.stub.events


def test_bad_input_and_closed_shop_refuse_before_payment(shop):
    r = httpx.post(shop.test_url + "/jobs/echo", json={"nope": 1})
    assert r.status_code == 400 and r.json()["charged"] is False
    assert httpx.post(shop.test_url + "/jobs/ghost", json={"input": "x"}).status_code == 404
    shop.books.set_open(False)
    r = httpx.post(shop.test_url + "/jobs/echo", json={"input": "x"})
    assert r.status_code == 503
    shop.books.set_open(True)


def test_output_is_scrubbed(shop):
    shop.stub.reply = lambda _t: "here you go. private key: 0x" + "7e" * 32
    out = hire(shop)
    assert "7e7e7e" not in out["output"]


def test_buyer_limits(shop, tmp_path):
    with pytest.raises(buyer.HireError, match="per-job limit"):
        hire(shop, service="pricey")
    with pytest.raises(buyer.HireError, match="per-job limit"):
        hire(shop, max_price="0.01")
    tight = buyer.Policy(as_settings({"buyer_daily_budget": "$0.08"}))
    led = books.Ledger(tmp_path / "b.db")
    hire(shop, policy=tight, ledger=led)
    with pytest.raises(buyer.HireError, match="budget"):
        hire(shop, policy=tight, ledger=led)
    mainnet_only = buyer.Policy(as_settings({"buyer_networks": ["eip155:8453"]}))
    with pytest.raises(buyer.HireError, match="only pays on"):
        hire(shop, policy=mainnet_only)
    with pytest.raises(buyer.HireError, match="EMPORIUM_BUYER_KEY"):
        hire(shop, key=None)
    assert len(shop.facilitator.settled) == 1


def test_buyer_refuses_a_shop_that_overcharges(shop):
    # The catalog says $0.05, but the shop's 402 asks for more: the buyer must not sign.
    shop.shop.service("echo").price = Decimal("0.05")
    buyer._catalog_cache.clear()
    buyer.catalog(shop.test_url)  # cache the honest catalog
    shop.shop.services[0].price = Decimal("0.90")
    shop._x402 = shop._build_x402(shop.shop)
    with pytest.raises(buyer.HireError, match="doesn't match"):
        hire(shop)
    assert shop.facilitator.verified == []
    buyer._catalog_cache.clear()


def test_rate_limit_per_wallet(shop):
    shop.shop.jobs_per_payer_per_hour = 1
    hire(shop)
    with pytest.raises(buyer.HireError, match="too many"):
        hire(shop)
    assert len(shop.facilitator.settled) == 1
