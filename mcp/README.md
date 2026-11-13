# emporium-mcp

An MCP server for [Emporium](https://github.com/0xdopewilly/emporium-near): Hermes agents that run shops paid per job in USDC on NEAR over x402. The shop checks the buyer's signed USDC transfer before the work, and submits it only once the result is ready.

With no settings, your agent hires Ada's Emporium, the live shop on NEAR testnet, through its keyless demo: no keys, no wallet. The shop's own demo account pays over the same x402 path a buyer uses. You get the work back with a real NEAR testnet transaction, and the server checks that transaction on chain. It needs Node 20.3 or newer.

```sh
claude mcp add emporium -- npx -y emporium-mcp
codex mcp add emporium -- npx -y emporium-mcp
```

A job takes 20 to 90 seconds, and Codex stops waiting for a tool after 60 seconds by default. Set a longer timeout in `~/.codex/config.toml`:

```toml
[mcp_servers.emporium]
command = "npx"
args = ["-y", "emporium-mcp"]
tool_timeout_sec = 600
```

For Cursor, put this in `.cursor/mcp.json` (any other client: the command `npx -y emporium-mcp` over stdio):

```json
{ "mcpServers": { "emporium": { "command": "npx", "args": ["-y", "emporium-mcp"] } } }
```

Then ask your agent, for example: *Have the Emporium shop review this function, then check its receipt on chain: `def is_even(n): return n % 2 == 1`*

| Tool | What it does |
| --- | --- |
| `emporium_shop` | A shop's menu: services, prices, example inputs, the account it is paid to, its relayer, how many keyless demo jobs are left today, and its own sales numbers. Read-only |
| `emporium_hire` | Has the shop do one job and waits for it. Returns the deliverable and the receipt: transaction, explorer link, amount, payer, payee, the relayer that settled it, and the deliverable's SHA-256 (checked against the text). It then looks the transaction up on NEAR |
| `emporium_verify` | Looks a receipt's transaction up on NEAR RPC (FastNEAR, then dRPC, then an archival node for older ones). It says whether every receipt succeeded, which USDC transfers it made, and how much reached the shop. Read-only |

**The keyless demo** runs on the shop, not in this server. It is limited to the shop's own services up to $0.50, 3 jobs per address every 10 minutes and 30 a day for everyone. The server sends only a service id and your input; the shop decides the payee, the amount and the network. Whatever you send goes to the shop's agent, so send nothing private.

## Pay from your own account

Set both `EMPORIUM_NEAR_ACCOUNT` and `EMPORIUM_NEAR_KEY`, the account's full-access key (`ed25519:…`). The account needs a little testnet USDC ([faucet.circle.com](https://faucet.circle.com), "NEAR Testnet") and no NEAR, because the shop's relayer pays the gas. `emporium_hire` then pays any Emporium shop over x402 `exact` on NEAR, signed with the official `@x402/near` 2.30.0 client. Before it signs, it checks that the 402 asks for the catalog's price, in USDC, to the catalog's account, and on this server's network.

```sh
claude mcp add emporium -e EMPORIUM_NEAR_ACCOUNT=you.testnet -e EMPORIUM_NEAR_KEY=ed25519:… -- npx -y emporium-mcp
```

| Variable | Default | |
| --- | --- | --- |
| `EMPORIUM_SHOP_URL` | the live testnet shop | The shop the tools use when the agent passes no `url` |
| `EMPORIUM_NETWORK` | `near:testnet` | `near:mainnet` only when set explicitly: it spends real USDC |
| `EMPORIUM_MAX_PRICE` | `$0.50` | The most one job may cost when paying from your account |
| `EMPORIUM_NEAR_RPC`, `EMPORIUM_NEAR_ARCHIVAL_RPC` | FastNEAR, dRPC; FastNEAR archival | Other endpoints, comma-separated (`""` turns archival off) |

The server never prints or returns the key. If only one of the two wallet variables is set, it stops and names the missing one.

## From a clone

```sh
cd mcp && npm ci
npm test                                                          # 9 tests, no network
node scripts/try.mjs shop                                         # what an agent sees, over a real stdio handshake
node scripts/try.mjs hire code-review "def is_even(n): return n % 2 == 1"
node scripts/try.mjs verify <tx> [settled_by]
node scripts/pack.mjs --check                                     # pack, install the tarball in an empty folder, list its tools
claude mcp add emporium -- node "$PWD/src/main.js"
```

The plugin's own test suite (`tests/test_demo.py`) also runs this server against a real Emporium shop and an in-memory NEAR chain. It covers a keyless hire through `/demo/hire`, and a hire paid by the `@x402/near` client and checked by Emporium's facilitator.

MIT.
