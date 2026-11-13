#!/usr/bin/env node
// Drives emporium-mcp the way an agent does: starts the server over stdio, does the MCP handshake and
// calls one tool, then prints what the agent would read. No keys needed.
//
//   node scripts/try.mjs shop [url]
//   node scripts/try.mjs hire <service> <input> [url]
//   node scripts/try.mjs verify <tx> [account] [url]
//
// The server gets this process's environment (EMPORIUM_SHOP_URL, EMPORIUM_NEAR_ACCOUNT, …).
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { fileURLToPath } from "node:url";

const [cmd, ...rest] = process.argv.slice(2);
const calls = {
  shop: ([url]) => ["emporium_shop", { ...(url && { url }) }],
  hire: ([service, input, url]) => ["emporium_hire", { service, input, ...(url && { url }) }],
  verify: ([tx, account, url]) => ["emporium_verify", { tx, ...(account && { account }), ...(url && { url }) }],
};
if (!calls[cmd] || (cmd === "hire" && rest.length < 2) || (cmd === "verify" && !rest[0])) {
  console.error("usage: node scripts/try.mjs shop [url] | hire <service> <input> [url] | verify <tx> [account] [url]");
  process.exit(2);
}
const [tool, args] = calls[cmd](rest);
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [fileURLToPath(new URL("../src/main.js", import.meta.url))],
  env: Object.fromEntries(Object.entries(process.env).filter(([, v]) => v !== undefined)),
  stderr: "inherit",
});
const client = new Client({ name: "emporium-try", version: "0" });
await client.connect(transport);
const { tools } = await client.listTools();
console.error(`[mcp] connected to ${client.getServerVersion()?.name} ${client.getServerVersion()?.version}; tools: ${tools.map((t) => t.name).join(", ")}`);
console.error(`[mcp] calling ${tool} ${JSON.stringify(args).slice(0, 160)}`);
const started = Date.now();
const res = await client.callTool({ name: tool, arguments: args }, undefined, { timeout: 900_000, resetTimeoutOnProgress: true, onprogress: (p) => console.error(`[mcp] ${p.message ?? ""}`) });
console.error(`[mcp] ${tool} answered in ${((Date.now() - started) / 1000).toFixed(1)} s${res.isError ? " with an error" : ""}\n`);
console.log(res.content.map((c) => c.text).join("\n"));
await client.close();
process.exit(res.isError ? 1 : 0);
