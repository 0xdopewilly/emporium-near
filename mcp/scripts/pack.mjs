// Packs emporium-mcp the way `npm publish` would, and checks the package works as `npx -y emporium-mcp` does.
// The package is plain ESM JavaScript for Node 20+, so there is no build step: the tarball is src/ and README.md.
//   node scripts/pack.mjs              pack into a temporary folder and list what's in the tarball
//   node scripts/pack.mjs --out DIR    keep the tarball in DIR
//   node scripts/pack.mjs --check      also install the tarball into an empty folder, outside this repo, start
//                                      its emporium-mcp bin and list its tools over a real stdio handshake
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL("..", import.meta.url));
const pkg = JSON.parse(readFileSync(join(here, "package.json"), "utf8"));
const outIdx = process.argv.indexOf("--out");
const dest = outIdx > 0 ? resolve(process.argv[outIdx + 1]) : mkdtempSync(join(tmpdir(), "emporium-mcp-pack-"));

const [info] = JSON.parse(execFileSync("npm", ["pack", "--json", "--pack-destination", dest], { cwd: here, encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] }));
const tarball = join(dest, info.filename);
console.log(`${pkg.name}@${pkg.version}: ${info.filename}, ${info.entryCount} files, ${(info.size / 1024).toFixed(1)} kB`);
for (const f of info.files) console.log(`  ${f.path}`);
console.log(`dependencies: ${Object.entries(pkg.dependencies).map(([d, v]) => `${d}@${v}`).join(", ")}`);
const leaked = info.files.filter((f) => /(^|\/)(test|node_modules|\.env)|\.tgz$/.test(f.path));
if (leaked.length) throw new Error(`the tarball holds files it shouldn't: ${leaked.map((f) => f.path).join(", ")}`);

if (process.argv.includes("--check")) {
  const tmp = mkdtempSync(join(tmpdir(), "emporium-mcp-check-"));
  execFileSync("npm", ["install", "--silent", "--no-audit", "--no-fund", "--prefix", tmp, tarball], { stdio: ["ignore", "ignore", "inherit"] });
  const bin = join(tmp, "node_modules", ".bin", "emporium-mcp");
  // No keys, and a shop URL nothing answers on: listing tools must not need the network.
  const env = { ...process.env, EMPORIUM_NEAR_ACCOUNT: "", EMPORIUM_NEAR_KEY: "", EMPORIUM_SHOP_URL: "http://127.0.0.1:9" };
  const child = spawn(bin, [], { stdio: ["pipe", "pipe", "inherit"], env });
  const send = (m) => child.stdin.write(JSON.stringify({ jsonrpc: "2.0", ...m }) + "\n");
  let buf = "";
  const tools = await new Promise((ok, fail) => {
    const timer = setTimeout(() => fail(new Error("no tools/list answer in 20 s")), 20_000);
    child.stdout.on("data", (d) => {
      buf += d;
      for (const line of buf.split("\n").slice(0, -1)) {
        const msg = JSON.parse(line);
        if (msg.id === 1) {
          send({ method: "notifications/initialized" });
          send({ id: 2, method: "tools/list" });
        }
        if (msg.id === 2) {
          clearTimeout(timer);
          ok(msg.result.tools.map((t) => t.name));
        }
      }
      buf = buf.slice(buf.lastIndexOf("\n") + 1);
    });
    send({ id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "pack-check", version: "0" } } });
  });
  child.kill();
  rmSync(tmp, { recursive: true, force: true });
  console.log(`installed from ${info.filename} into an empty folder; tools: ${tools.join(", ")}`);
}
if (outIdx < 0) rmSync(dest, { recursive: true, force: true });
