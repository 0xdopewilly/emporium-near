#!/usr/bin/env node
// emporium-mcp over stdio.   npx -y emporium-mcp    (or: node mcp/src/main.js from a clone)
//
// No settings: hires go through the live testnet shop's keyless demo. EMPORIUM_NEAR_ACCOUNT and
// EMPORIUM_NEAR_KEY pay from your own NEAR account instead (testnet unless EMPORIUM_NETWORK=near:mainnet).
// stdout carries the protocol, so anything for a person goes to stderr.
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { configFromEnv } from "./config.js";
import { createServer } from "./server.js";

let cfg;
try {
  cfg = configFromEnv(process.env);
} catch (e) {
  const key = process.env.EMPORIUM_NEAR_KEY?.trim();
  const message = String(e?.message ?? e);
  console.error(`emporium-mcp: ${key ? message.split(key).join("[redacted]") : message}`);
  process.exit(1);
}
console.error(
  cfg.wallet
    ? `emporium-mcp: paying from ${cfg.wallet.account} on ${cfg.network} (at most $${(Number(cfg.maxPrice) / 1e6).toFixed(2)} a job); default shop ${cfg.shopUrl}`
    : `emporium-mcp: no keys; hires go through the keyless demo of ${cfg.shopUrl} (NEAR testnet)`,
);
await createServer(cfg).connect(new StdioServerTransport());
