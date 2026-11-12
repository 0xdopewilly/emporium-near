# Emporium against @x402/near, the official x402 `exact`-on-NEAR

Emporium's NEAR payments are pure Python (`near.py`, `near_x402.py`: no NEAR SDK). This folder checks them,
both ways, against the x402 Foundation's TypeScript implementation **`@x402/near` 2.30.0** (with
`@x402/core` 2.30.0), on real NEAR testnet state, with one command.

## Run it

```bash
cd interop && npm ci
PY=/path/to/hermes-agent/.venv/bin/python \
  node run.mjs --n 120 --m 120 --settle --settles 2 --chain-truth
```

It reads keys from `../.dev/near-wallets.json` (`--wallets` to point elsewhere; the funded buyers come from
`scripts/near_accounts.py setup`) and settles from its own relayer, `interop-relay.emporium.testnet` (made
with `near_accounts.ensure_sub(…, "interop-relay.emporium.testnet", "0.5", …)`; it refuses the live shops'
`ada-relay`/`tunde-relay`). Testnet only: `near:mainnet` RPC is pointed at `127.0.0.1:9` on both sides.
It exits 0 only if every check below holds, and writes `../bench/results/interop-official.json`.

Expected output (this run: 2026-11-12 22:09 UTC):

```
NEAR testnet: 10 payers, 4 shops; relayer interop-relay.emporium.testnet
direction 1: 120/120 Emporium-signed payloads accepted by @x402/near 2.30.0 (Emporium's own facilitator: 120/120)
direction 2: 120/120 @x402/near-signed payloads accepted by Emporium (the official facilitator: 120/120)
tamper matrix: 71 tampered payloads in 17 classes, both reject 71/71; 7 controls, both accept 7/7; agreement 78/78
  edge E01 amount as a JSON number, not a string: @x402/near rejects, Emporium rejects; on chain it does not pay
  edge E02 duplicate receiver_id key (another account first, payTo last): @x402/near accepts, Emporium rejects; on chain it does not pay
  edge E03 duplicate amount key ("1" first, the amount last): @x402/near accepts, Emporium rejects; on chain it does not pay
  edge E04 memo is a number: @x402/near accepts, Emporium rejects; on chain it does not pay
  edge E05 an extra unknown argument (msg): @x402/near accepts, Emporium rejects; on chain it pays as asked
  edge E06 gas 0: @x402/near accepts, Emporium rejects; on chain it does not pay
  edge E07 gas 1 TGas: @x402/near accepts, Emporium rejects; on chain it does not pay
  edge E08 a trailing byte after the signature: @x402/near accepts, Emporium rejects; on chain it pays as asked
  edge E09 requirements amount "0" (and a transfer of 0): @x402/near accepts, Emporium rejects; on chain it does not pay
real settles: 4/4 confirmed on NEAR testnet
wrote bench/results/interop-official.json (379 s, 3208 RPC calls, 3120 rate-limited)
```

Without a network or a wallet, `node run.mjs --offline --n 40 --m 40 --settle` runs the same checks
against `tests/fake_near.py`'s in-memory chain (and `tests/test_interop.py` runs that under pytest when
`node` and `interop/node_modules` are present; it is skipped otherwise).

The Borsh fuzz (near.py's bytes against near-api-js 7.4.0, which `npm ci` installs here too):

```bash
python scripts/check_borsh.py interop --fuzz 10000 --seed 1 --json bench/results/borsh-fuzz.json
# 7 of 7 identical            (the fixed vectors, as before)
# 10000 of 10000 identical    (delegate, NEP-461 hash, signed delegate, relayer tx, its hash, signed tx,
#                              and near.py reading near-api-js's signed delegate back to the same bytes)
```

## What it checks

Everything crosses as x402 v2 wire JSON (`PaymentRequired`, `PaymentPayload`, `PaymentRequirements`) and
goes through each side's x402 SDK the way a shop runs it: `x402Client` / `x402Facilitator` from
`@x402/core` with `@x402/near`'s `ExactNearScheme` registered, and `x402ClientSync` / `x402FacilitatorSync`
from the x402 Python SDK with Emporium's `ExactNearClient` / `ExactNearFacilitator`
(`emporium_bridge.py`, driven over JSON lines).

1. **Emporium → official.** Emporium's client signs a payment for random terms (10 payers, 14 payees,
   amounts 1 to 150000 units, `maxTimeoutSeconds` 60 to 86400); `@x402/near`'s facilitator verifies it
   against the chain. Emporium's facilitator verifies the same payload.
2. **Official → Emporium.** `@x402/near`'s client signs; Emporium's facilitator verifies; so does the
   official one.
3. **Tamper matrix.** near-api-js 7.4.0 (NEAR's own JS library, neither implementation under test) builds
   each payload once, wrong in exactly one way; both facilitators verify the same bytes. Controls (the
   same construction, untampered or harmlessly varied) must pass both.
4. **Real settles.** An Emporium-signed payment settled by `@x402/near`'s facilitator, and an
   `@x402/near`-signed payment settled by Emporium's, each confirmed on chain from the token contract's own
   NEP-141 event; then each settled payment is replayed at both facilitators (verify and settle) and must
   not pay again.
5. **The chain's verdict.** For the cases the spec leaves open, the payload is submitted to testnet from
   the relayer *without* verification, so the token contract (Circle's USDC) says what really happens.

Both sides read the chain through a local forwarder (`lib/rpc-proxy.mjs`) that passes each request and
answer through unchanged; it keeps TLS connections open and moves a request from FastNEAR to dRPC when
FastNEAR's free tier refuses it for its rate limit (a refused transaction never reached the chain; one an
upstream took, or lost mid-way, is never sent again).

## Results (2026-11-12 22:09 UTC, `bench/results/interop-official.json`)

| | |
|---|---|
| Emporium → @x402/near | **120/120** Emporium-signed payloads accepted by @x402/near's facilitator (120/120 by Emporium's) |
| @x402/near → Emporium | **120/120** @x402/near-signed payloads accepted by Emporium's facilitator (120/120 by the official) |
| Tamper matrix | **71** tampered payloads in 17 classes: both reject **71/71**; 7 controls: both accept 7/7; same verdict on **78/78** (100%) |
| Real settles on testnet | **4/4** confirmed from the token's own NEP-141 event, both ways; no replay paid twice |
| Spec-silent edge cases | 9, each sent to the chain unverified: Emporium's verdict matches the chain's on 7, @x402/near's on 3; **every payload the chain refused to pay, Emporium rejects; @x402/near accepts 6 of them** |
| Borsh | 10000 of 10000 random delegates/relayer transactions byte-identical to near-api-js 7.4.0 (`bench/results/borsh-fuzz.json`) |

14 different payees and 10 payers, amounts 1 to 150000 units, windows 60 s to a day. Median verify time
through the same local forwarder: @x402/near 2244 ms, Emporium 399 ms (Emporium makes its reads at once
and remembers a token and payee it has already confirmed; @x402/near makes its six reads one after
another). The run made 3208 RPC calls in 379 s.

### Tamper matrix

| class | cases | result (same bytes at both) |
|---|---:|---|
| control | 7 | both accept 7/7 |
| wrong amount | 6 | both reject 6/6 |
| wrong payTo | 4 | both reject 4/4 |
| wrong asset | 4 | both reject 4/4 |
| wrong method | 3 | both reject 3/3 |
| extra action | 5 | both reject 5/5 |
| deposit != 1 yocto | 3 | both reject 3/3 |
| excess gas | 3 | both reject 3/3 |
| expired max_block_height | 2 | both reject 2/2 |
| window > maxTimeoutSeconds | 2 | both reject 2/2 |
| bad signature | 4 | both reject 4/4 |
| signer != key owner | 5 | both reject 5/5 |
| stale nonce | 7 | both reject 7/7 |
| wrong network | 4 | both reject 4/4 |
| wrong version/scheme | 2 | both reject 2/2 |
| bad requirements | 2 | both reject 2/2 |
| chain state | 2 | both reject 2/2 |
| malformed payload | 13 | both reject 13/13 |

Every tampered payload is refused by both for the reason it was built to trip (per-case reasons in the
JSON), e.g. a transfer 1 unit short is `invalid_exact_near_payload_amount_mismatch` there and
`amount_mismatch` here. The replays (R*) are the settled payments sent again.

### The chain's verdict on what the spec leaves open

| | payload | @x402/near 2.30.0 | Emporium | on chain (unverified submit) | |
|---|---|---|---|---|---|
| E01 | amount as a JSON number, not a string | rejects | rejects | does not pay ([9rYXK8JX…](https://testnet.nearblocks.io/txns/9rYXK8JX8jys7VsnEw8CryJxzh4pPtp6TZUimtHkUA4k)) | USDC: "invalid type: integer 10000, expected a string"; Emporium accepted this before this branch (fixed) |
| E02 | duplicate receiver_id key (another account first, payTo last) | accepts | rejects | does not pay ([DSzHGewf…](https://testnet.nearblocks.io/txns/DSzHGewfvpNmjHPBdGAmqb4B3XPukHN2QW2DgLwCvtfi)) | USDC: "duplicate field receiver_id"; both accepted it before this branch; Emporium fixed |
| E03 | duplicate amount key ("1" first, the amount last) | accepts | rejects | does not pay ([CkaRkDpi…](https://testnet.nearblocks.io/txns/CkaRkDpiUgs7YuxL4f2cHqAy9jMyxo7bSpiVGe3g9ydc)) | USDC: "duplicate field amount"; both accepted it before this branch; Emporium fixed |
| E04 | memo is a number | accepts | rejects | does not pay ([AjwrBGK4…](https://testnet.nearblocks.io/txns/AjwrBGK4jTFpuW4go21aip8xBpVfc97WoHn3qKdnHJbc)) | USDC: "invalid type: integer 42, expected a string" |
| E05 | an extra unknown argument (msg) | accepts | rejects | pays ([68D7LakH…](https://testnet.nearblocks.io/txns/68D7LakHHjCpDvLCzzTSo3UBNixt4cJj51be4BJot54u)) | Emporium is stricter than the chain: only NEP-141's own arguments |
| E06 | gas 0 | accepts | rejects | refused: `FunctionCallZeroAttachedGas` | nearcore refuses the relayer's transaction before it runs |
| E07 | gas 1 TGas | accepts | rejects | does not pay ([FU82CRne…](https://testnet.nearblocks.io/txns/FU82CRneWreb6CGLdmH98mNiRBShPhcB5Dc2dtu4fNzk)) | "Exceeded the prepaid gas"; both accepted it before this branch; Emporium fixed (5 TGas floor) |
| E08 | a trailing byte after the signature | accepts | rejects | pays ([WXHZPohp…](https://testnet.nearblocks.io/txns/WXHZPohpXwixCuF6ntDMVsWYDYuxm9KdZSCE8oJcW5f)) | Emporium is stricter: Borsh with bytes left over is not a SignedDelegateAction |
| E09 | requirements amount "0" (and a transfer of 0) | accepts | rejects | does not pay ([5YWTLerp…](https://testnet.nearblocks.io/txns/5YWTLerpdRihTzhcWimYF9BGgowyCmvwP1dJy4mSMiex)) | USDC: "The amount should be a positive number" |

So where the two disagree, the chain sided with Emporium on every case it refused to pay (E02, E03, E04, E06,
E07, E09), and on the two it paid (E05, E08) Emporium is the stricter of the two: it refuses a payment that
would have gone through, and never accepts one that won't. For a shop that does the work between verify and
settle, the six @x402/near accepts are jobs run for nothing: settle then fails.

What this branch changed in Emporium, found by this comparison (`near_x402.py`, with tests):
- a numeric `amount` (E01) is refused: Emporium compared `str(amount)` and accepted it; USDC panics;
- repeated JSON keys (E02, E03) are refused: both parsers kept the last value; USDC refuses `duplicate field`;
- gas under 5 TGas (E07) is refused: USDC's `ft_transfer` burns 2.3 TGas, and 1 TGas fails on chain;
- a payer is named only once the delegate's signature verifies (as the spec asks, and as @x402/near does);
  before, a forged payload's claimed `sender_id` came back as `payer`;
- the delegate's window against RPC lag: FastNEAR's and dRPC's final heads differ by up to 2 blocks, and
  the spec's window rule has no slack, so a delegate signed for `head + timeoutBlocks` and checked against a
  node a block behind reads as too long (an earlier run lost 1 of 240 valid payloads that way). Emporium's
  client now signs 3 blocks inside the window, and its facilitator looks at the head again (up to 3 times,
  0.7 s apart) before rejecting a window that is at most 10 blocks too long. @x402/near's client signs the
  full window and its facilitator reads the head once, so it has the same exposure;
- `near.py` reads a gateway's own error (`{"error": "text"}`, not NEAR's `{name, cause}`) as an RPC error
  and moves on to the next endpoint, where it raised `AttributeError` before.

### Real settles

| way | payer | tx (0.01 test USDC to emporium.testnet) | settle | then replayed |
|---|---|---|---:|---|
| emporium-signed -> official settles | buyer-3 | [ERKjoaAB3YK53pmr8uMFSrAkNoUHYroenQdtv8EsvFNJ](https://testnet.nearblocks.io/txns/ERKjoaAB3YK53pmr8uMFSrAkNoUHYroenQdtv8EsvFNJ) | 7.2 s | both verifiers reject; emporium settle: `delegate_action_nonce_already_used` |
| official-signed -> emporium settles | buyer-4 | [6x3kp4C3xZFMeJrjy5qoydnDVc4FsUprgo16iqfZ1frA](https://testnet.nearblocks.io/txns/6x3kp4C3xZFMeJrjy5qoydnDVc4FsUprgo16iqfZ1frA) | 5.3 s | both verifiers reject; official settle: `invalid_exact_near_payload_delegate_action_nonce_already_used` |
| emporium-signed -> official settles | buyer-5 | [G69d6z5rDqv6RYUaQ6XbKAxoSxgqhfhP4iQeDjcitxRp](https://testnet.nearblocks.io/txns/G69d6z5rDqv6RYUaQ6XbKAxoSxgqhfhP4iQeDjcitxRp) | 7.5 s | both verifiers reject; emporium settle: `delegate_action_nonce_already_used` |
| official-signed -> emporium settles | buyer-6 | [8BnDFP7voHCyqaNwqDFwBou7rS7SsXYg4H8B5ztkdSSX](https://testnet.nearblocks.io/txns/8BnDFP7voHCyqaNwqDFwBou7rS7SsXYg4H8B5ztkdSSX) | 5.5 s | both verifiers reject; official settle: `invalid_exact_near_payload_delegate_action_nonce_already_used` |

Each settle is the full x402 path: the payer's USDC balance drops by exactly 10000 units, and the token's
`ft_transfer` event names payer, payee and amount (`near.paid`). Settling the same payload again at the
other facilitator is refused (nonce already used) and the balance doesn't move.

