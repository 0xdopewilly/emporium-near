# Emporium, NEAR edition

![Emporium: Hermes opens a shop, paid in USDC on NEAR](docs/banner.png)

**A Hermes Agent plugin that turns an agent into a shop paid in USDC on NEAR.** [Hermes Agent](https://github.com/NousResearch/hermes-agent) is Nous Research's open-source agent. With this plugin, a Hermes profile sells its skills per job (code review, translation, research) to people, scripts or other agents, over the open [x402](https://x402.org) payment standard.

- **Paid only on delivery.** The buyer signs a USDC transfer up front. The shop checks it, does the work, and only then submits the transfer. A failed job is never submitted, so it costs nothing. (In x402's usual flow the seller settles before it sends the response; here the money moves once the result is ready.)
- **No gas for buyers.** The signed transfer is a NEP-366 delegate action (a meta-transaction). The shop's relayer account submits it and pays the gas, so a buyer needs USDC and no NEAR.
- **Agents hire agents.** A job can hire another shop for part of the work, and pays it the same way from the shop's own NEAR wallet.
- **Pay from any chain, cash out to any chain.** Through NEAR Intents (1Click), a buyer tops up its NEAR account from USDC, USDT or native tokens on 29 chains, and a shop cashes its earnings out to any of them. The money stays the buyer's own until a job is delivered. Every 1Click quote is checked before anything is paid: 1Click's signature, then that it is exactly the quote asked for.
- **x402 on NEAR, built from the spec and checked against the reference.** The plugin implements the x402 v2 [`exact` scheme for NEAR](https://github.com/x402-foundation/x402/blob/main/specs/schemes/exact/scheme_exact_near.md) for all three roles (buyer, shop, facilitator), plugged into the official x402 Python SDK. `near.py` encodes NEAR's Borsh by hand, with no NEAR SDK underneath. On NEAR testnet, the x402 Foundation's TypeScript implementation, `@x402/near` 2.30.0, accepts 120 of 120 Emporium payments and Emporium accepts 120 of 120 of its; on 71 tampered payloads both refuse all 71 ([interop/](interop/)). Its bytes match `near-api-js` on 10,000 of 10,000 random payments.

**Run on NEAR mainnet with real USDC** (12 Nov 2026; every transaction is in `bench/results/mainnet-run.json`, and `$PY scripts/verify_mainnet.py` checks them all from public data, with no keys):

| | |
|---|---|
| Topped up from another chain | 2 USDC sent on Base became exactly **2.00 USDC on NEAR**, through NEAR Intents with the plugin's `fund` command |
| Paid jobs | **68**, 68 delivered; median delivery **21.0 s**, p95 81.7 s |
| Shop-to-shop payments | **12**: each launch post hired Tunde's shop for the Yoruba half and paid it from Ada's wallet |
| Payments confirmed on NEAR mainnet | **80 / 80**, 0 duplicates; in the 67-job bench run, buyers' accounts paid **$1.06**, exactly what the delivered jobs cost |
| Gas | the shops' relayers paid about **0.00034 NEAR** per payment; buyers and shops spent **no NEAR** |
| Cashed out to another chain | 3 cash-outs through NEAR Intents put **1.992447 USDC** back on Base |
| The whole round trip | Base → NEAR → 80 payments → Base cost **$0.0102** in route fees |

**Live on NEAR testnet:** https://shop-production-8cc3cf57.up.railway.app is Ada's Emporium. Its launch-post service hires Tunde's Translations, a second shop in the same deployment, for the Yoruba half. To check its sales on-chain with no keys, run `$PY scripts/verify_shop.py https://shop-production-8cc3cf57.up.railway.app` (Hermes's Python, set up in "Try it from a fresh clone" below).

**Measured on NEAR testnet, with real USDC payments from buyer accounts we ran** (scripted buyers using the plugin's own hire client) (`bench/`, reports in `bench/results/`):

| | |
|---|---|
| Paid jobs from 10 buyer accounts | **70**, 70 delivered (100.0%) |
| Receipts verified on-chain | **70 / 70**, 0 duplicates |
| Money reconciled | buyers' accounts paid **$10.40**, exactly what the delivered jobs cost |
| Delivery time | median **27.3 s**, p95 94.7 s |
| Robbery attempts (paid attack jobs) | **38**: the model took the bait once, **0 tools escaped, 0 secrets leaked** |

**What a job costs to run** (median per settled job: AI cost from Hermes's own session pricing, plus what the job paid other shops):

| Service | Price | Jobs | AI cost | Paid to other shops | Margin |
|---|---|---|---|---|---|
| English → Yoruba | $0.05 | 111 | $0.0002 | — | 99.6% |
| Code review | $0.25 | 49 | $0.0005 | — | 99.8% |
| Launch post, English + Yoruba | $0.30 | 13 | $0.0013 | $0.05 | 82.9% |
| Web research brief | $0.40 | 9 | $0.0032 | — | 99.2% |

The shop's relayer paid about **0.00033 NEAR of gas per payment** (182 settled payments across both shops); the buyers spent no NEAR.

Two earlier runs of the same plan are kept in `bench/results/pre-fix/`:
- **Run 1:** FastNEAR's free RPC tier rate-limited the buyers and shops at this load. 11 of 70 jobs were delivered, and the other 59 were refused before any money moved. That is why the RPC client now falls back to dRPC and backs off.
- **Run 2:** Tunde's shop ran the fix and delivered 40 of 40. Ada's gateway hadn't actually restarted, so it was still on the old code and delivered 4 of 30.

In every run the books reconciled to the cent: no buyer paid for a job that wasn't delivered.

## How a NEAR payment works

```
buyer ── POST /jobs/launch-post ────────────────▶ 402: $0.30 in USDC (Circle's NEP-141) to ada.emporium.testnet
buyer signs a NEP-366 delegate: exactly one ft_transfer of 300000 to the shop. No NEAR, no gas.
buyer ── POST + {"signedDelegateAction": …} ────▶ the shop checks it against the 402 and the chain; nothing moves
                                                   ▼
            the job runs in its own sealed Hermes session (DeepSeek V4.1 Flash on NEAR AI Cloud)
                                                   ▼
            (optional) the job hires another shop and pays it the same way, from the shop's own wallet
                                                   ▼
buyer ◀── result + receipt (tx hash, SHA-256) ─── the shop's relayer submits the delegate: the USDC moves now
```

Before any work, the shop (`near_x402.py`, `ExactNearFacilitator`) refuses a payment unless:

- it is exactly one `ft_transfer` on the USDC contract the 402 named, to the shop's account, for the exact amount, with a 1 yoctoNEAR deposit and bounded gas;
- the delegate's ed25519 signature is valid, and it signs with a full-access key the payer's account holds;
- the delegate's nonce is above the key's current one, and it expires within the window the 402 allows, but not before the job can finish and settle;
- the payer holds enough USDC, and the shop is registered with the token;
- the same signed delegate hasn't already bought a job here (each nonce is claimed once).

After delivery, the relayer wraps the delegate in a transaction it signs and sends. Settling is idempotent: a retry looks up the transaction it already sent instead of sending a second one. The receipt names the relayer, because a NEAR transaction is found by its hash and its signer.

The model the shops run, DeepSeek V4.1 Flash, is hosted by NEAR AI Cloud in a trusted execution environment, so a buyer's job is processed inside attested hardware. On 2026-11-10, NEAR AI's attestation for that model passed all 7 checks of [Veto](https://github.com/0xdopewilly/veto)'s enclave verifier: a fresh nonce, an Intel TDX quote, key and nonce bound to the quote, the published software, 8 NVIDIA confidential GPUs, and NEAR AI's gateway.

## Pay from any chain, cash out to any chain

NEAR Intents has no testnet, so this part is mainnet only. `intents.py` talks to 1Click (NEAR Intents' swap API) and keeps pay-on-delivery intact: the buyer's money moves onto the buyer's own NEAR account first, and from there to a shop only after a job is delivered.

- **Top up** (`/shop fund 2 from base:usdc refund 0x…`, or `scripts/intents.py fund`): an exact-output quote, so the buyer's NEAR account receives exactly the USDC asked for. The plugin prints the deposit address, or pays it from the profile's Base wallet with `send`.
- **Cash out** (`/shop cashout 5 to base:usdc 0x…`, then `confirm`): the shop's key signs one NEP-366 `ft_transfer` to 1Click's deposit address, and the shop's relayer registers that address with USDC and pays the gas. The signed transfer is saved before it is sent, so a retry or a crash never pays twice.
- **Before paying anything**, each quote must carry 1Click's ed25519 signature over its terms and match the request field by field: assets, amount, recipient, refund address, deadline. A quote whose fee could pass 3% is refused unless the owner raises the cap.

Measured with 280 real dry quotes on 12 Nov 2026 ([bench/results/intents-quotes.md](bench/results/intents-quotes.md)): **213 routes on 29 chains** quoted and verified. Topping up costs a median **0.25%** (0.06% between stablecoins; 0.01% from USDC on Base, Arbitrum, Ethereum or Solana) with about 35 s once the deposit confirms. Cashing out costs a median **0.29%** (0.03% for $10 to Base) with about 24 s. Bitcoin is refused: 1Click's BTC quotes don't verify against its own published key.

## Checked against the reference implementation

[interop/](interop/) runs Emporium against the x402 Foundation's `@x402/near` 2.30.0 on NEAR testnet, both ways and on the same bytes:

- **120 / 120** Emporium payments accepted by `@x402/near`'s facilitator, and **120 / 120** `@x402/near` payments accepted by Emporium's.
- **71 tampered payloads in 17 classes** (wrong amount, payee, token, method, gas, deposit, signer, nonce, expiry, network, extra actions…): both refuse **71 / 71**, and both accept the 7 controls.
- **4 / 4** real settles across implementations confirmed on chain; replaying each at the other facilitator is refused.
- **9 edge cases the spec leaves open**, decided by sending them to Circle's USDC contract: `@x402/near` accepts 6 payments that USDC then refuses to pay, which would leave a shop that did the work unpaid. Emporium refuses all of them.

Writing the comparison found five bugs in Emporium, fixed with tests before this README: a numeric amount, duplicate JSON keys and too little gas were accepted although the chain refuses them; a payer was reported from a payload whose signature failed; and RPC nodes a block or two apart could make a valid payment look expired.

## Try it from a fresh clone

These steps need no keys and no NEAR account. Python 3.11+ and [uv](https://docs.astral.sh/uv/) are assumed, plus Node.js 22+ for the Borsh check (near-api-js 7.4.0 requires it). The plugin runs inside Hermes, so its tests run on Hermes's Python:

```bash
git clone --depth 1 --branch v0.23.0 https://github.com/NousResearch/hermes-agent
(cd hermes-agent && uv venv && uv pip install -e . "x402[evm]>=2.22,<3" pytest)
git clone https://github.com/0xdopewilly/emporium-near
cd emporium-near
PY=../hermes-agent/.venv/bin/python
```

| Do | See |
|---|---|
| `$PY -m pytest tests -q` | `263 passed, 2 skipped` (the 2 run once `interop/` has its Node packages: `265 passed`). The NEAR tests run real HTTP shops and buyers against `tests/fake_near.py`, an in-memory NEAR chain behind NEAR's JSON-RPC (accounts, access-key nonces, USDC, delegate actions); the NEAR Intents tests run against `tests/fake_oneclick.py` with real signed 1Click quotes. |
| `npm i --prefix /tmp/nearjs near-api-js@7.4.0`<br>`$PY scripts/check_borsh.py /tmp/nearjs` | `7 of 7 identical`: the buyer's public key, the delegate, its NEP-461 hash, the signed delegate and the relayer's transaction (unsigned, hash, signed), byte for byte against NEAR's own JS library. Add `--fuzz 10000 --seed 1`: `10000 of 10000 identical` on random delegates and relayer transactions. |
| `cd interop && npm ci`<br>`PY=$PWD/../$PY node run.mjs --offline --n 40 --m 40 --settle` | Emporium against the x402 Foundation's reference, `@x402/near` 2.30.0, on the in-memory chain: 40/40 accepted each way, `agreement 78/78` on tampered payloads, `real settles: 2/2`. Without `--offline` it runs on NEAR testnet (needs funded testnet accounts; [interop/README.md](interop/README.md)). |
| `$PY scripts/verify_mainnet.py` | The mainnet run, from public NEAR RPC, public Base RPC and 1Click's status API: `68/68 paid jobs and 12/12 shop-to-shop payments confirmed on NEAR mainnet`, the top-up and three cash-outs ✓, `All confirmed on NEAR mainnet and Base.` (about a minute). |
| `$PY scripts/intents.py quote 5 from base:usdc` | A real NEAR Intents quote (dry: nothing to pay): what topping up with 5 USDC from Base costs, and `✓ signed by 1Click, and it is the quote that was asked for`. |
| `$PY scripts/verify_shop.py https://shop-production-8cc3cf57.up.railway.app` | Each recent receipt of the live shop, looked up on NEAR testnet: `N of N receipts confirmed on NEAR`. |
| `$PY -m hermes_cli.main plugins validate .` | `Validation passed.` (Hermes's own plugin checks: manifest, isolated load, declared tools and hooks, security scan). |

### Hire the live shop yourself

This needs a NEAR testnet account holding a little testnet USDC. The account needs no NEAR beyond its minimum, because the shop pays the gas. Get USDC from [faucet.circle.com](https://faucet.circle.com) ("NEAR Testnet"). The account must also be registered with the USDC contract once. To make one that is: `$PY scripts/near_accounts.py setup --parent <a-new-name>.testnet --buyers 1` creates the parent from the testnet faucet and `buyer-1.<parent>` registered with USDC (keys go to the git-ignored `.dev/near-wallets.json`). Send the faucet's USDC to the parent, then run `fund buyer-1.<parent> 1`, and `env buyer-1.<parent> --var EMPORIUM_NEAR_BUYER_KEY` prints the key line for the export below.

```bash
export EMPORIUM_NEAR_BUYER_KEY=ed25519:…      # the account's full-access key, from the environment only
$PY scripts/hire.py https://shop-production-8cc3cf57.up.railway.app launch-post \
  "Mama Put Lagos: order jollof and suya from your phone, pay in USDC, pick up in 10 minutes." --account you.testnet
```

The reply has the English post, Tunde's Yoruba translation of it, and a receipt with a nearblocks link. Your account paid $0.30, and Ada's wallet paid Tunde $0.05 out of it.

### Open your own shop

```bash
hermes profile create shop                          # fresh: no memory, no skills, no .env (don't use --clone)
hermes -p shop model                                # pick the model the shop works with
hermes -p shop plugins install https://github.com/0xdopewilly/emporium-near
hermes -p shop plugins enable emporium
hermes -p shop emporium init --pay-to you.testnet --relayer you-relay.testnet --name "Ada's Emporium"
echo 'EMPORIUM_NEAR_RELAYER_KEY=ed25519:…' >> ~/.hermes/profiles/shop/.env   # the relayer's key: it only pays gas
hermes gateway run                                  # serves the shop profile at http://127.0.0.1:9402
```

`init` writes a starter menu (English → Yoruba $0.05, code review $0.25, web research brief $0.40) and turns off this profile's memory. The relayer needs a little NEAR for gas; the payout account needs a USDC registration. **The shop only opens on a sealed profile:** the gateway refuses to open it while any of these would reach a buyer's job:

- it's your main profile;
- built-in memory is on;
- a memory provider would record buyers' turns;
- its `SOUL.md` is your main persona;
- the working directory has project context files (`AGENTS.md`, `.hermes.md`, `CLAUDE.md`, `.cursorrules`).

`/shop`, `hermes emporium status` and the dashboard say which one applies.

To let your Hermes hire shops, put a dedicated low-balance NEAR account in the profile (`buyer_near_account` setting, `EMPORIUM_NEAR_BUYER_KEY` in its `.env`). Then just ask it, for example: *"There's an agent shop at https://…. Have it review this function."* Hermes calls `emporium_browse`, which also checks the shop's recent receipts on NEAR, and then `emporium_hire`. Above `buyer_approve_over` (default $0.10), Hermes asks you first.

## What you get

| Surface | What it does |
|---|---|
| **`emporium` gateway platform** | The paid front door. Each paid job becomes a message in its own fresh gateway session, and the agent's reply is the deliverable. |
| **x402 `exact` on NEAR** (`near_x402.py`, `near.py`) | Buyer, shop and facilitator roles, registered with the official x402 SDK the way its EVM and Solana mechanisms are. Borsh, NEP-366 delegates, NEP-461 hashes and relayer transactions, with no NEAR SDK. The RPC client falls back from FastNEAR to dRPC when one is rate-limited or down. |
| **The books** | Every job is metered: model calls, tokens, and inference cost from Hermes's own session pricing. `/shop` and the dashboard show sales, AI cost, payments to other shops, profit and margin. |
| **Agents hiring agents** | A service can subcontract part of a job to a shop the owner pins. The subcontract is paid out of the job's own price, and a job can never spend more than its subcontract budget. |
| **Storefront** at `/` | The menu, prices, median delivery time and a live feed of jobs served, with nearblocks links. Also `/.well-known/emporium.json` (a machine-readable catalog: network, USDC contract, relayer) and `/feed`. |
| **Sealed jobs** | A job's session loads only the toolsets its service sells. A `pre_tool_call` lock refuses anything else, and a per-job cap bounds tool calls. |
| **`emporium_browse` / `emporium_hire`** | Your Hermes can hire other shops, within a per-job ceiling, a daily budget and allowed networks. |
| **`hermes emporium`** | `init`, `status`, `jobs`, `buys`, `open`, `close`. |
| **`bench/`** | A stress bench that reconciles every cent on NEAR, and a robbery bench that pays your own shop to attack itself, then reads Hermes's transcripts to see what got out. |

## Why a paying stranger can't rob you

1. **Only what's sold is loaded.** A job's session gets only the toolsets its service sells. Terminal, files, browser, memory, skills, delegation, cron and MCP servers are never in the model's context.
2. **Hard tool lock.** `pre_tool_call` refuses every tool the service doesn't sell, every hire that isn't a pinned subcontract, and anything past the job's tool-call cap. A shop session not bound to a known job gets no tools at all. Your own sessions (CLI, Telegram…) are never affected.
3. **Fenced input.** The buyer's text reaches the agent between markers with a fresh random tag per job, and fence-like text in it is defused.
4. **Egress sweep.** Replies are swept for private keys (including NEAR `ed25519:` keys), seed phrases, tokens and JWTs. Any verbatim run of the service's private instructions becomes `[private]`.
5. **Money last.** The payment is checked before the work and submitted after it. One signed delegate buys one job. A failed turn, a timeout, or a buyer who hangs up is never submitted. A payment that would expire before the job could settle is refused before any work.
6. **A sealed profile** (see "Open your own shop").
7. **The buyer checks the shop too.** `emporium_hire` won't sign unless the 402 asks for the catalog's price, in USDC (checked by contract id), to the shop's published account, on a network you allowed.

## Disclosures

- **Network:**
  - The shop listens on `host:port` (localhost by default).
  - It calls NEAR JSON-RPC (default FastNEAR, then dRPC; `rpc_url` overrides) to check payments and submit them.
  - The buyer tools call the shop URLs the agent is given, and NEAR RPC to check receipts. A model-supplied shop URL must resolve to a public address unless the owner lists it in `buyer_allow_hosts`.
  - Only when the owner runs `/shop quote`, `fund` or `cashout`: 1Click's API (`1click.chaindefuser.com`), and for a top-up paid with `send`, public Base RPC.
- **Credentials:**
  - `EMPORIUM_NEAR_RELAYER_KEY` (the shop) signs the transactions that submit buyers' delegates. Use an account that holds only gas money.
  - `EMPORIUM_NEAR_BUYER_KEY` (optional, for hiring and subcontracts) signs USDC delegates in-process. Use a dedicated low-balance account.
  - `EMPORIUM_NEAR_PAYOUT_KEY` (optional, only to cash out) is the shop account's own key. Running `scripts/intents.py cashout` from your own computer keeps it off the server.
  - `EMPORIUM_ONECLICK_JWT` (optional) is a 1Click partner key; without it 1Click's app fee applies (1 bps between stablecoins), already included in every quote shown.
  - All are read through Hermes's secret scope and never logged.
- **Data:** A SQLite ledger in `plugin-data/emporium/` holds job metadata: service, price, status, payer account, tx hash, sizes, token counts, cost, and the output's SHA-256. It never stores inputs or outputs. Top-ups and cash-outs are kept there too, with each 1Click quote and its signature. Each job is also a session in the shop profile's own Hermes history, like any conversation.
- **Config writes:** `hermes emporium init` writes the plugin's own settings, and on the shop's profile only (never the main one) turns off its memory.
- **Background:** one HTTP server thread while the gateway runs, and a short timer after each job to read its cost. No shell commands, no telemetry, no self-updates.

## Bench

Both benches make real paid jobs on NEAR testnet against running shops, from a fleet of throwaway buyer accounts (the mainnet run above is `stress.py` with `--network near:mainnet`, which spends real USDC):

```bash
python scripts/near_accounts.py setup --parent <yours>.testnet --buyers 10   # testnet accounts under one faucet-funded parent
python scripts/near_accounts.py fleet .dev/fleet-near.json  # the buyers' keys, as a bench wallet file (git-ignored)

# many buyers at once, then every receipt and every account reconciled on NEAR
python bench/stress.py --wallets .dev/fleet-near.json --concurrency 7 \
  --plan http://127.0.0.1:9503/translate-yoruba=40 --plan http://127.0.0.1:9502/code-review=20 \
  --plan http://127.0.0.1:9502/launch-post=6 --plan http://127.0.0.1:9502/web-brief=4

# pay your own shop to attack itself; Hermes's transcripts decide what got out
python bench/redteam.py --wallets .dev/fleet-near.json --shop http://127.0.0.1:9502=<ada's profile dir> \
  --shop http://127.0.0.1:9503=<tunde's profile dir> --secrets <ada's .env> --secrets <tunde's .env>

python bench/summarize.py --ledger <each shop's plugin-data/emporium/data.db>
```

Reports are in `bench/results/`.

## Tests

`$PY -m pytest tests` runs 265 tests over real HTTP (2 of them need `interop/`'s Node packages and are skipped without them): the official x402 SDK on both sides, the plugin's buyer code, and stand-ins for the chain (`tests/fake_near.py`) and for Base's facilitator. On NEAR they cover:

- the encoding, pinned to near-api-js's bytes;
- every verify refusal above;
- settlement, including a dropped relayer transaction sent again, and a retry that finds the one already sent;
- paid jobs end to end, with the buyer's account, the shop's and the relayer's balances checked;
- a failed job never charged, and the same buyer paying again after it;
- the RPC client under rate limits and dead endpoints;
- the NEAR Intents client against a fake 1Click with real signed quotes: forged and mismatched quotes refused with nothing paid, every status ending, and cash-outs that crash or retry without paying twice;
- the reference check, offline: Emporium and `@x402/near` on the same payloads, plus a 300-case Borsh fuzz.

The rest cover the shop itself: the books, the tool lock, sealed profiles, subcontract budgets, the fence and the secret sweep.

## Built for NEAR HACKATHON

- **My other NEAR HACKATHON project is [Veto](https://github.com/0xdopewilly/veto).** It is a NEAR vault in which an AI agent can only propose payments, and a model in NEAR AI Cloud's TEE signs a verdict the contract checks.
- **Emporium also has a Nous edition.** Emporium started as an entry to Nous Research's Hermes plugin hackathon ([hermes-emporium](https://github.com/0xdopewilly/hermes-emporium)), which pays on Base through an outside facilitator. This repo forked from it at tag `nous-edition` (7b43458). Everything NEAR was built after that fork, for this hackathon: `near.py`, `near_x402.py`, the shop as its own NEAR facilitator and relayer, the NEAR buyer, `tests/fake_near.py` and the NEAR tests, the RPC fallback, the NEAR benches and the NEAR deployment, NEAR Intents (`intents.py`), the reference check (`interop/`) and the mainnet run. The two editions have separate repos, deployments, wallets and numbers.
- **Built with:**
  - Hermes Agent v0.23.0
  - the x402 Python SDK (`x402`)
  - `httpx`
  - `cryptography` (Ed25519)
  - `near-api-js` 7.4.0 (test oracle only)
  - Circle's USDC on NEAR testnet and mainnet
  - NEAR Intents (1Click API)
  - `@x402/near` 2.30.0 (reference check only)
  - NEAR AI Cloud (DeepSeek V4.1 Flash)
  - FastNEAR and dRPC (RPC)
  - nearblocks (explorer links)

## License

MIT
