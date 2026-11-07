**Measured on Base Sepolia, with real USDC payments from buyer wallets we ran** (scripted buyers using the plugin's own hire client) (`bench/`, reports in `bench/results/`):

| | |
|---|---|
| Paid jobs from 20 buyer wallets | **376**, 367 delivered (97.6%) |
| Receipts verified on-chain | **367 / 367**, 0 duplicates |
| Money reconciled | buyers' wallets paid **$37.46**, exactly what the delivered jobs cost |
| Delivery time | median **6.2 s**, p95 23.1 s |
| Robbery attempts (paid attack jobs) | **38**: the model took the bait 4 times, **0 tools escaped, 0 secrets leaked** |
