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
