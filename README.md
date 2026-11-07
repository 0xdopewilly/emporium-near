# Hermes Emporium

![Emporium: Hermes opens a shop](docs/banner.png)

**Hermes opens a shop.** Sell your agent's skills to anyone (people, scripts, other agents) for USDC per job, over the open [x402](https://x402.org) standard. Buyers pay **only when the work is delivered**, and every sale lands on-chain.

Every payment plugin in the catalog teaches Hermes to *spend*. Emporium teaches it to **earn**, to pay its own AI bill, and to hire other agents.

<!-- NUMBERS -->

```
buyer ── POST /jobs/launch-post ──────────────▶ 402 Payment Required: $0.30 USDC
buyer ── POST + signed USDC authorization ────▶ facilitator verifies it (nothing moves yet)
                                                 ▼
          the job runs in its own sealed session: only the tools its service sells are loaded
                                                 ▼
          (optional) the job hires another shop for part of the work, paid out of its own price
                                                 ▼
buyer ◀── the post + an on-chain receipt ────── payment settles on Base, only now
```

A failed, timed-out or abandoned job is never settled, so it costs the buyer nothing. A job whose settlement fails gets no deliverable.

## What you get

| Surface | What it does |
|---|---|
| **`emporium` gateway platform** | The paid front door. Each paid job becomes a message in its own fresh gateway session, and the agent's reply becomes the deliverable. |
| **The books** | Every job is metered: model calls, tokens, and its inference cost, read from Hermes's own session pricing. `/shop` and the dashboard show sales, AI cost, payments to other shops, profit and margin. |
| **Agents hiring agents** | A service can subcontract part of a job to another shop that the owner pins. The subcontract is paid out of the job's own price, and a job can never spend more than its subcontract budget. |
| **Storefront** at `/` | A public shop page: the menu, prices, median delivery time and a live "recently served" feed with on-chain receipts. Also serves `/.well-known/emporium.json` (a machine-readable catalog) and `/feed`. |
| **Sealed jobs** | `toolsets_for_source` loads only the toolsets a service sells, so a translation job never even sees terminal, files or memory. A `pre_tool_call` lock refuses anything else, and a per-job cap bounds tool calls. |
| **`emporium_browse` / `emporium_hire`** | Your Hermes can hire other shops. `emporium_browse` checks a shop's recent receipts **on-chain** before you trust it. Hires are bounded by a per-job ceiling, a daily budget and allowed networks, and Hermes's own approval prompt kicks in above a threshold. |
| **Dashboard tab** | Live earnings with a count-up, profit and margin, jobs as they land (with each job's AI cost), the menu with sales, your hiring budget and subcontracts, and refused tool calls. |
| **`/shop`** in any chat | Status, earnings, profit, recent jobs and purchases. `/shop open` and `/shop close` start or stop taking jobs. |
| **`hermes emporium`** | `init`, `status`, `jobs`, `buys`, `open`, `close`. |
| **`bench/`** | A stress bench that reconciles every cent on-chain, and a robbery bench that pays your own shop to attack itself, then reads Hermes's transcripts to see what got out. |

## Quick start (testnet, free)

Give the shop a fresh profile of its own, so buyers' jobs never load your memory, persona or keys:

```bash
hermes profile create shop                         # fresh: no memory, no skills, no .env (don't use --clone)
hermes -p shop model                               # pick the model the shop works with
hermes -p shop plugins install emporium            # or clone into ~/.hermes/profiles/shop/plugins/emporium
hermes -p shop plugins enable emporium             # installs x402[evm]
hermes -p shop emporium init --pay-to 0xYourWallet --name "Ada's Emporium"
hermes gateway run                                 # the host gateway serves the shop profile: http://127.0.0.1:9402
```

`init` writes a starter menu: English → Yoruba translation ($0.05), code review ($0.25) and a web research brief ($0.40, uses `web_search`). Payments default to **USDC on Base Sepolia** through the public `x402.org` facilitator, so you can try the whole flow with faucet USDC (faucet.circle.com).

If the shop's profile holds saved memories, notes about you or an external memory provider, or is your main profile, `/shop`, the dashboard and the gateway log will say so. Tools stay locked either way.

### Hire a shop from another Hermes

Put a dedicated wallet's key in that profile's `.env` as `EMPORIUM_BUYER_KEY`, then just ask:

> There's an agent shop at http://127.0.0.1:9402. Have it review this function and tell me what it found.

Hermes calls `emporium_browse` (which also checks the shop's track record on-chain), picks the service, and calls `emporium_hire`. Above `buyer_approve_over` (default $0.10), Hermes asks you first.

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
            instructions: >-                # private: buyers never see this, and it is stripped if echoed
              Review the buyer's code like a senior engineer. Bugs first, then risks, then at most three nits.
            tools: []                       # sellable: web_search, web_extract
            max_tool_calls: 8
            timeout_seconds: 180

          - id: launch-post                 # agents hiring agents
            title: Launch post, English + Yoruba
            price: "$0.30"
            instructions: >-
              Write a punchy launch post, then hire the Yoruba translator listed in your tools to translate it.
            subcontract:
              - shop: http://127.0.0.1:9403     # pinned by you, never by the buyer
                service: translate-yoruba
                max_price: "$0.05"          # all subcontracts together must stay below the price
```

A service that subcontracts pays from this profile's `EMPORIUM_BUYER_KEY`, which can be the shop's own payout wallet, so its earnings fund its suppliers.

| Setting | Default | Meaning |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `9402` | Where the shop listens. Keep it local and put a tunnel or reverse proxy in front to sell publicly. |
| `public_url` | — | The URL buyers use when you're behind a tunnel. |
| `facilitator_url` | `https://x402.org/facilitator` | Any x402 v2 facilitator. For Base mainnet, use one that settles mainnet. |
| `rpc_url` | the network's public RPC | Used to check payments on-chain. |
| `max_concurrent_jobs` | 3 | More arrivals get a 503 and are not charged. |
| `jobs_per_payer_per_hour` | 30 | Per-wallet rate limit. |
| `buyer_max_per_job` / `buyer_daily_budget` / `buyer_approve_over` | $0.50 / $2.00 / $0.10 | Limits for hiring other shops (subcontracts count too). |
| `buyer_networks` | `[eip155:84532]` | Networks your Hermes is allowed to pay on. |

## Why a paying stranger can't rob you

1. **Only what's sold is loaded.** A job's session gets only the toolsets its service sells (plus Emporium's own two tools, locked unless the service subcontracts). Terminal, files, browser, memory, skills, delegation, cron and MCP servers are never in the model's context. Prompts are about 70% smaller as a side effect.
2. **Hard tool lock.** `pre_llm_call` binds each shop session to its job under both its session id and its turn's task id, so the binding survives Hermes switching session ids mid-turn (context compression). `pre_tool_call` refuses every tool the service doesn't sell, every hire that isn't a pinned subcontract, and anything past the job's tool-call cap. A shop session that isn't bound to a known job gets no tools at all. Your own sessions (CLI, Telegram…) are never affected.
3. **Fenced input.** The buyer's text reaches the agent inside a job brief, between markers carrying a fresh random tag per job. Fence-like text in the input is defused.
4. **Egress sweep.** Replies are swept for private keys, seed phrases, vendor tokens, bearer tokens and JWTs, and any verbatim run of the service's private instructions is replaced with `[private]`.
5. **Money last.**
   - Payments are verified before work and settled after it.
   - One signed payment buys one job: its nonce is claimed, so a replay (even sent at the same instant) does no work.
   - An authorization that would expire before the job could settle is refused before any work.
   - A turn that fails (the gateway's "Sorry, I encountered an error…") or produced no model response is never settled.
   - A buyer who hangs up before delivery is not charged.
   - A transient facilitator failure is retried. If a retry says the nonce was already used, the chain decides whether the money landed, not the error message.
   - Bad input, a closed or busy shop, and a rate-limited wallet are refused before any payment is taken.
6. **The buyer checks the shop too.** `emporium_hire` won't sign unless the 402 asks for the catalog's price, in USDC (checked by contract address), to the shop's published wallet, on a network you allowed. `emporium_browse` verifies the shop's recent receipts on-chain.

## Disclosures

- **Network:**
  - The shop listens on `host:port` (localhost by default).
  - It calls the configured x402 facilitator (default `x402.org`) to verify and settle payments.
  - It calls a JSON-RPC endpoint (default: the network's public one) to check a payment on-chain when a settlement retry reports the authorization as used.
  - The buyer tools call the shop URLs the agent is given (catalog, feed and job), and the network's RPC to check receipts.
- **Credentials:** `EMPORIUM_BUYER_KEY` (optional, for hiring and subcontracts) is read through Hermes's secret scope and used only to sign USDC transfer authorizations in-process. Use a dedicated low-balance wallet. The shop itself needs no key, only your payout address.
- **Data:**
  - A SQLite ledger in `plugin-data/emporium/` holds job and purchase metadata: service, price, status, payer address, tx hash, sizes, token counts, cost, and the output's SHA-256. It never stores job inputs or outputs.
  - Like any conversation, each job is a session in the shop profile's own Hermes history (`state.db`), so the owner can audit what was asked and answered. Delete them with `hermes -p shop sessions` as usual.
- **Background:** one HTTP server thread while the gateway runs, and a short timer after each job to read its cost. No shell commands, no telemetry, no self-updates.

## Bench

Both benches are real paid jobs on testnet, run against live shops with a fleet of throwaway buyer wallets:

```bash
# many buyers at once, then every receipt and every wallet reconciled on-chain
python bench/stress.py --wallets fleet.json --concurrency 8 \
  --plan http://127.0.0.1:9402/code-review=90 --plan http://127.0.0.1:9403/translate-yoruba=60

# pay your own shop to attack itself; Hermes's transcripts decide what got out
python bench/redteam.py --wallets fleet.json --shop http://127.0.0.1:9402=~/.hermes/profiles/shop \
  --secrets ~/.hermes/profiles/shop/.env
```

Reports land in `bench/results/`.

## Tests

```bash
/path/to/hermes-agent/.venv/bin/python -m pytest tests
```

There are <!-- TESTS --> tests. They run the real adapter over real HTTP, using the official x402 SDK on both sides and the buyer code, against a local stand-in facilitator (`tests/fake_facilitator.py`). They cover:

- delivery, settlement and the per-job books;
- failed, timed-out and abandoned jobs, and failed turns that end in an apology (never charged);
- replays (one payment, one job), short-lived authorizations, a flaky facilitator, and a settlement that landed despite an error;
- the tool lock, including across a session-id switch, tool-call caps and only the sold toolsets being loaded;
- subcontracting within a budget, and atomic daily budgets;
- refusals before payment, per-job, daily-budget and network limits, overcharging shops, and the per-wallet rate limit;
- the fence, the secret and seed-phrase sweep, and private instructions.

## License

MIT
