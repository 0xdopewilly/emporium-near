**Measured on Base Sepolia, with real USDC payments from agents we ran** (`bench/`, reports in `bench/results/`):

| | |
|---|---|
| Paid jobs from 20 buyer wallets | **228**, 219 delivered (96.0%) |
| Receipts verified on-chain | **219 / 219**, 0 duplicates |
| Money reconciled | buyers' wallets paid **$24.66**, exactly what the delivered jobs cost |
| Delivery time | median **6.1 s**, p95 24.8 s |
| Robbery attempts (paid attack jobs) | **38**: the model took the bait 4 times, **0 tools escaped, 0 secrets leaked** |
