**NEAR Intents (1Click), measured with real dry quotes** (2026-11-12 21:59 UTC; `bench/intents_quotes.py`, every quote checked by the plugin's own `intents.check_quote`: 1Click's signature, then the terms)

| | |
|---|---|
| Routes quoted | **213 / 280** ($1, $10; both directions) |
| 1Click signatures verified | **213 / 215** quotes (failing, so refused: cashout btc:BTC, fund btc:BTC) |
| Chains with a working route | **29** of 39 tried: adi, aptos, arb, avax, base, bch, bera, bsc, dash, doge, eth, fogo, gnosis, hood, hypercore, ltc, monad, near, op, plasma, pol, scroll, sol, stellar, sui, ton, tron, xlayer, zec |
| Fund (any chain → USDC on NEAR, EXACT_OUTPUT) | 105/140 OK · median fee **0.25%** (stablecoins 0.06%) · median ETA **35 s** |
| Cash out (USDC on NEAR → any chain, EXACT_INPUT) | 108/140 OK · median fee **0.29%** (stablecoins 0.19%) · median ETA **24.0 s** |

Fee = 1Click's USD value in vs out at quote time, before the slippage buffer (on fund, the unused buffer is refunded). ETA = 1Click's estimate once the deposit confirms. The bench quotes with stand-in addresses (a dry quote makes no deposit address); 1Click answers some of them on aptos, starknet, ton, tron and xrp with "Internal server error", as it does for a NEAR recipient that doesn't exist, so those chains are under-counted here.

| Direction | Amount | Routes OK | Median fee | Inside the default 3% cap |
|---|---|---|---|---|
| fund | $1 | 51 | 0.25% | 51 |
| fund | $10 | 54 | 0.24% | 54 |
| cashout | $1 | 53 | 0.61% | 44 |
| cashout | $10 | 55 | 0.19% | 50 |

Route minimums 1Click reported (USD of USDC on NEAR): fund zec:ZEC $1.24, cashout zec:ZEC $1.27, fund btc:BTC $5.92, cashout btc:BTC $6.02, fund tron:USDT $100.00, fund tron:TRX $100.00

| Route ($10) | In | Out | Fee | ETA |
|---|---|---|---|---|
| near:USDC → gnosis:xDAI | 10 | 9.997943206029396039 | 0.00% | 24 s |
| near:USDC → near:USDT | 10 | 10.007666 | 0.00% | 20 s |
| near:USDC → near:wNEAR | 10 | 2.930969883136496950650708 | 0.00% | 22 s |
| near:USDC → op:USDT | 10 | 10.006132 | 0.00% | 23 s |
| near:USDC → gnosis:USDC | 10 | 9.998943 | 0.01% | 24 s |
| near:USDC → op:USDC | 10 | 9.998448 | 0.02% | 23 s |
| near:USDC → arb:USDT0 | 10 | 10.002815 | 0.03% | 19 s |
| near:USDC → base:USDC | 10 | 9.99658 | 0.03% | 22 s |
| near:USDC → arb:USDC | 10 | 9.99368 | 0.06% | 19 s |
| near:USDC → sol:USDT | 10 | 9.997795 | 0.07% | 17 s |
| near:USDC → bsc:USDT | 10 | 9.997214002776 | 0.07% | 25 s |
| near:USDC → pol:USDT | 10 | 9.997214 | 0.07% | 26 s |
| near:USDC → bsc:USDC | 10 | 9.988945011045 | 0.11% | 23 s |
| near:USDC → pol:USDC | 10 | 9.988945 | 0.11% | 24 s |
| near:USDC → sol:USDC | 10 | 9.988945 | 0.11% | 17 s |
| near:USDC → aptos:USDT | 10 | 9.993221 | 0.11% | 35 s |
| near:USDC → sui:SUI | 10 | 12.405997859 | 0.13% | 35 s |
| near:USDC → avax:USDT | 10 | 9.990946 | 0.14% | 23 s |
| near:USDC → base:ETH | 10 | 0.004949148834785968 | 0.14% | 22 s |
| near:USDC → arb:ETH | 10 | 0.004948676371907198 | 0.15% | 19 s |
| near:USDC → scroll:ETH | 10 | 0.004948417644814094 | 0.16% | 24 s |
| near:USDC → gnosis:USDT | 10 | 9.98865 | 0.16% | 24 s |
| near:USDC → scroll:USDT | 10 | 9.988529 | 0.16% | 22 s |
| near:USDC → monad:MON | 10 | 565.694412888824702211 | 0.17% | 24 s |
| near:USDC → avax:USDC | 10 | 9.982273 | 0.18% | 23 s |
| near:USDC → plasma:USDT0 | 10 | 9.988237 | 0.18% | 24 s |
| near:USDC → xlayer:USDT0 | 10 | 9.987065 | 0.19% | 22 s |
| near:USDC → monad:USDT0 | 10 | 9.986769 | 0.19% | 24 s |
| near:USDC → stellar:XLM | 10 | 67.1684885 | 0.20% | 35 s |
| near:USDC → bera:USDT0 | 10 | 9.985242 | 0.21% | 25 s |
| near:USDC → sui:USDC | 10 | 9.97898 | 0.21% | 35 s |
| near:USDC → plasma:XPL | 10 | 199.51190506677120424 | 0.21% | 24 s |
| near:USDC → monad:USDC | 10 | 9.978494 | 0.21% | 22 s |
| near:USDC → xlayer:OKB | 10 | 0.102656664507158614 | 0.22% | 24 s |
| near:USDC → avax:AVAX | 10 | 1.19712468799013891 | 0.23% | 23 s |
| near:USDC → sol:SOL | 10 | 0.122473313 | 0.26% | 17 s |
| near:USDC → ltc:LTC | 10 | 0.19894292 | 0.28% | 162 s |
| near:USDC → bch:BCH | 10 | 0.04426333 | 0.29% | 137 s |
| near:USDC → bera:BERA | 10 | 99.039320908633346573 | 0.29% | 25 s |
| near:USDC → op:ETH | 10 | 0.004941855418144886 | 0.29% | 25 s |
| near:USDC → bsc:BNB | 10 | 0.015230551916639906 | 0.30% | 23 s |
| near:USDC → hood:ETH | 10 | 0.004939868144690637 | 0.33% | 19 s |
| near:USDC → pol:POL | 10 | 113.259452607153167646 | 0.36% | 24 s |
| near:USDC → aptos:APT | 10 | 17.95752563 | 0.38% | 37 s |
| near:USDC → doge:DOGE | 10 | 149.97980255 | 0.52% | 160 s |
| near:USDC → fogo:FOGO | 10 | 5158.020791455 | 0.55% | 19 s |
| near:USDC → dash:DASH | 10 | 0.24042481 | 0.63% | 162 s |
| near:USDC → eth:ETH | 10 | 0.004915236698343142 | 0.83% | 27 s |
| near:USDC → tron:TRX | 10 | 31.355118 | 1.08% | 75 s |
| near:USDC → ton:GRAM | 10 | 8.669978766 | 1.11% | 75 s |
| near:USDC → eth:USDT | 10 | 9.707713 | 2.97% | 27 s |
| near:USDC → eth:USDC | 10 | 9.69898 | 3.01% | 27 s |
| near:USDC → hypercore:USDC | 10 | 9.68368 | 3.16% | 30 s |
| near:USDC → zec:ZEC | 10 | 0.01021974 | 3.17% | 135 s |
| near:USDC → adi:ADI | 10 | 1.240080173498694043 | 3.47% | 24 s |
| arb:USDC → near:USDC | 10.05128 | 10 | 0.01% | 25 s |
| avax:USDC → near:USDC | 10.05128 | 10 | 0.01% | 35 s |
| base:USDC → near:USDC | 10.05128 | 10 | 0.01% | 35 s |
| bsc:USDC → near:USDC | 10.051277489579108688 | 10 | 0.01% | 35 s |
| eth:USDC → near:USDC | 10.05128 | 10 | 0.01% | 45 s |
| gnosis:USDC → near:USDC | 10.05128 | 10 | 0.01% | 35 s |
| op:USDC → near:USDC | 10.05128 | 10 | 0.01% | 35 s |
| pol:USDC → near:USDC | 10.05128 | 10 | 0.01% | 35 s |
| sol:USDC → near:USDC | 10.05128 | 10 | 0.01% | 20 s |
| sui:USDC → near:USDC | 10.05128 | 10 | 0.01% | 25 s |
| arb:USDT0 → near:USDC | 10.060453 | 10 | 0.04% | 25 s |
| avax:USDT → near:USDC | 10.06048 | 10 | 0.06% | 35 s |
| bsc:USDT → near:USDC | 10.060477413745403863 | 10 | 0.06% | 35 s |
| eth:USDT → near:USDC | 10.060481 | 10 | 0.06% | 45 s |
| near:USDT → near:USDC | 10.060427 | 10 | 0.06% | 20 s |
| pol:USDT → near:USDC | 10.060434 | 10 | 0.06% | 35 s |
| sol:USDT → near:USDC | 10.060434 | 10 | 0.06% | 20 s |
| aptos:USDT → near:USDC | 10.06048 | 10 | 0.06% | 25 s |
| op:USDT → near:USDC | 10.061437 | 10 | 0.07% | 35 s |
| monad:USDC → near:USDC | 10.071434 | 10 | 0.21% | 20 s |
| xlayer:USDC → near:USDC | 10.071434 | 10 | 0.21% | 20 s |
| gnosis:xDAI → near:USDC | 10.121054609535841695 | 10 | 0.23% | 35 s |
| plasma:USDT0 → near:USDC | 10.079587 | 10 | 0.23% | 25 s |
| bera:USDT0 → near:USDC | 10.080652 | 10 | 0.24% | 35 s |
| monad:USDT0 → near:USDC | 10.080596 | 10 | 0.24% | 20 s |
| xlayer:USDT0 → near:USDC | 10.080626 | 10 | 0.24% | 20 s |
| gnosis:USDT → near:USDC | 10.079586 | 10 | 0.24% | 35 s |
| scroll:USDT → near:USDC | 10.079597 | 10 | 0.24% | 25 s |
| bsc:BNB → near:USDC | 0.015469200195439725 | 10 | 0.25% | 35 s |
| sol:SOL → near:USDC | 0.124366788 | 10 | 0.27% | 20 s |
| base:ETH → near:USDC | 0.005020689119212908 | 10 | 0.29% | 35 s |
| op:ETH → near:USDC | 0.00502066663841512 | 10 | 0.29% | 35 s |
| arb:ETH → near:USDC | 0.005020941193211969 | 10 | 0.29% | 25 s |
| pol:POL → near:USDC | 115.15820865215254458 | 10 | 0.29% | 35 s |
| eth:ETH → near:USDC | 0.005021441131971926 | 10 | 0.30% | 45 s |
| ton:GRAM → near:USDC | 8.886118904 | 10 | 0.34% | 67 s |
| hood:ETH → near:USDC | 0.005023354316963717 | 10 | 0.34% | 25 s |
| fogo:FOGO → near:USDC | 5256.874781763 | 10 | 0.34% | 22 s |
| sui:SUI → near:USDC | 12.593522035 | 10 | 0.36% | 25 s |
| scroll:ETH → near:USDC | 0.005025968866126028 | 10 | 0.39% | 25 s |
| avax:AVAX → near:USDC | 1.217147389450372345 | 10 | 0.42% | 35 s |
| ltc:LTC → near:USDC | 0.20242842 | 10 | 0.45% | 127 s |
| doge:DOGE → near:USDC | 153.00510225 | 10 | 0.47% | 127 s |
| bch:BCH → near:USDC | 0.04505108 | 10 | 0.47% | 107 s |
| aptos:APT → near:USDC | 18.29714575 | 10 | 0.49% | 27 s |
| xlayer:OKB → near:USDC | 0.104455276447973818 | 10 | 0.51% | 22 s |
| plasma:XPL → near:USDC | 203.003861512279539741 | 10 | 0.52% | 27 s |
| zec:ZEC → near:USDC | 0.01070342 | 10 | 0.54% | 455 s |
| dash:DASH → near:USDC | 0.24577929 | 10 | 0.57% | 127 s |
| bera:BERA → near:USDC | 100.954248054418544654 | 10 | 0.62% | 37 s |
| adi:ADI → near:USDC | 1.305842406454235498 | 10 | 0.63% | 27 s |
| monad:MON → near:USDC | 576.133540586586428988 | 10 | 0.65% | 22 s |
| near:wNEAR → near:USDC | 2.974149637548276867002092 | 10 | 0.75% | 20 s |
| hypercore:USDC → near:USDC | 10.25128 | 10 | 2.01% | 35 s |
