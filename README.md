# Hermes Emporium

![Emporium: Hermes opens a shop](docs/banner.png)

**Hermes opens a shop.** Sell your agent's skills to anyone (people, scripts, other agents) for USDC per job, over the open [x402](https://x402.org) standard. Buyers pay **only when the work is delivered**.

Every payment plugin in the catalog teaches Hermes to *spend*. Emporium teaches it to **earn**.

```
buyer ── POST /jobs/code-review ──────────────▶ 402 Payment Required: $0.25 USDC
buyer ── POST + signed USDC authorization ────▶ facilitator verifies it (nothing moves yet)
                                                 ▼
                     the job runs in its own sealed, tool-locked Hermes session
                                                 ▼
buyer ◀── the review + an on-chain receipt ──── payment settles on Base, only now
```

A failed or timed-out job is never settled, so it costs the buyer nothing. A job whose settlement fails gets no deliverable.

## What you get

| Surface | What it does |
|---|---|
| **`emporium` gateway platform** | The paid front door. Each paid job becomes a message in its own fresh gateway session, and the agent's reply becomes the deliverable. |
| **Storefront** at `/` | A public shop page: the menu, prices, how hiring works and a live "recently served" feed with on-chain receipts. Also serves `/.well-known/emporium.json` (a machine-readable catalog) and `/feed`. |
| **Tool lock** (`pre_llm_call` + `pre_tool_call`) | A job can call only the tools its service sells (none by default). Every other call in a shop session is refused before it runs. |
| **`emporium_browse` / `emporium_hire`** | Your Hermes can hire other shops, with a per-job ceiling, a daily budget, allowed networks and Hermes's own approval prompt above a threshold. |
| **Dashboard tab** | Live earnings with a count-up, jobs as they land, the menu with sales, your hiring budget, and refused tool calls. |
| **`/shop`** in any chat | Status, earnings, recent jobs and purchases. `/shop open` and `/shop close` start or stop taking jobs. |
| **`hermes emporium`** | `init`, `status`, `jobs`, `buys`, `open`, `close`. |

## Quick start (testnet, free)

Give the shop a profile of its own, so buyers' jobs never load your memory or persona:

```bash
hermes profile create shop --clone                 # its own memory, skills, sessions and .env
hermes -p shop plugins install emporium            # or clone into ~/.hermes/profiles/shop/plugins/emporium
hermes -p shop plugins enable emporium             # installs x402[evm]
hermes -p shop emporium init --pay-to 0xYourWallet --name "Ada's Emporium"
hermes gateway run                                 # the host gateway serves the shop profile: http://127.0.0.1:9402
```

`init` writes a starter menu: English → Yoruba translation ($0.05), code review ($0.25) and a web research brief ($0.40, uses `web_search`). Payments default to **USDC on Base Sepolia** through the public `x402.org` facilitator, so you can try the whole flow with faucet USDC (faucet.circle.com).

A shop on your main profile still works and its tools stay locked, but `/shop`, the dashboard and the gateway log will tell you to move it.

### Hire a shop from another Hermes

Put a dedicated wallet's key in that profile's `.env` as `EMPORIUM_BUYER_KEY`, then just ask:

> There's an agent shop at http://127.0.0.1:9402. Have it review this function and tell me what it found.

Hermes calls `emporium_browse`, picks the service, and calls `emporium_hire`. Above `buyer_approve_over` (default $0.10), Hermes asks you first.

## Your menu

Services live in `plugins.entries.emporium.settings.services` (config.yaml, or the Desktop settings form):

```yaml
plugins:
  entries:
    emporium:
      settings:
        pay_to: "0xYourWallet"
        network: "eip155:84532"            # or eip155:8453 (Base) with a mainnet facilitator
        services:
          - id: code-review
            title: Code review
            price: "$0.25"
            description: A senior reviewer's pass over a diff or file.
            instructions: >-                # private: buyers never see this
              Review the buyer's code like a senior engineer. Bugs first, then risks, then at most three nits.
            tools: []                       # sellable: web_search, web_extract, vision_analyze, image_generate
            timeout_seconds: 180
```

| Setting | Default | Meaning |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `9402` | Where the shop listens. Keep it local and put a tunnel or reverse proxy in front to sell publicly. |
| `public_url` | — | The URL buyers use when you're behind a tunnel. |
| `facilitator_url` | `https://x402.org/facilitator` | Any x402 v2 facilitator. For Base mainnet, use one that settles mainnet. |
| `max_concurrent_jobs` | 3 | More arrivals get a 503 and are not charged. |
| `jobs_per_payer_per_hour` | 30 | Per-wallet rate limit. |
| `buyer_max_per_job` / `buyer_daily_budget` / `buyer_approve_over` | $0.50 / $2.00 / $0.10 | Limits for hiring other shops. |
| `buyer_networks` | `[eip155:84532]` | Networks your Hermes is allowed to pay on. |

## Why a paying stranger can't rob you

1. **Fenced input.** The buyer's text reaches the agent inside a job brief, marked as material to work on rather than instructions. Fence markers in the input are neutralised so it can't close the fence early.
2. **Its own profile.** Run on a `shop` profile, the agent that works jobs has a separate memory, persona, skills, history and `.env`. On your main profile, Emporium warns you.
3. **Hard tool lock.** `pre_llm_call` binds each shop session to its job, and `pre_tool_call` refuses every tool the service doesn't sell. A shop session that isn't bound to a known job gets no tools at all. Your own sessions (CLI, Telegram…) are never affected. `emporium_hire` itself is never sellable, so a buyer can't make your shop spend your money.
4. **Egress scrub.** Replies are swept for private keys, vendor tokens, bearer tokens and JWTs before they leave the machine.
5. **Money last.** Payments are verified before work and settled after it. A transient facilitator failure is retried (the authorization's one-time nonce means a retry can never charge twice); if it still doesn't settle, the result is withheld. Bad input, a closed shop, a busy shop or a rate-limited wallet are refused before any payment is taken.
6. **The buyer checks the shop too.** `emporium_hire` won't sign unless the 402 asks for the catalog's price, in USDC, to the shop's published wallet, on a network you allowed.

## Disclosures

- **Network:** the shop listens on `host:port` (localhost by default). It calls the configured x402 facilitator (default `x402.org`) to verify and settle payments. The buyer tools call the shop URLs the agent is given, and that shop's facilitator settles the payment.
- **Credentials:** `EMPORIUM_BUYER_KEY` (optional, only for hiring) is read through Hermes's secret scope and used only to sign USDC transfer authorizations in-process. Use a dedicated low-balance wallet. The shop itself needs no key, only your payout address.
- **Data:** a SQLite ledger in `plugin-data/emporium/` holds job and purchase metadata (service, price, status, payer address, tx hash, sizes). It never stores job inputs or outputs.
- **Background:** one HTTP server thread while the gateway runs. No shell commands, no telemetry, no self-updates.

## Tests

```bash
/path/to/hermes-agent/.venv/bin/python -m pytest tests
```

There are 25 tests. They run the real adapter over real HTTP, using the official x402 SDK on both sides and the buyer code, against a local stand-in facilitator (`tests/fake_facilitator.py`). They cover:

- delivery and settlement;
- failed and timed-out jobs (never charged);
- settlement failure (deliverable withheld), a flaky facilitator (retried) and a bad authorization (not retried);
- refusals before payment;
- per-job, daily-budget and network limits;
- a shop whose 402 asks for more than its catalog price;
- the per-wallet rate limit;
- the secret sweep;
- the tool lock and the hire gate.

## License

MIT
