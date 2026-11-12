// Talks to emporium_bridge.py (Emporium's Python x402 client and facilitator) over JSON lines.
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));

export function startBridge(python, { logStderr = false } = {}) {
  const script = path.join(HERE, "..", "emporium_bridge.py");
  const child = spawn(python, [script], { stdio: ["pipe", "pipe", "pipe"], env: { ...process.env, PYTHONUNBUFFERED: "1" } });
  const pending = new Map();
  let nextId = 1;
  let stderr = "";
  child.stderr.on("data", (d) => {
    stderr = (stderr + d.toString()).slice(-20_000);
    if (logStderr) process.stderr.write(d);
  });
  const lines = createInterface({ input: child.stdout });
  lines.on("line", (line) => {
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      return; // not ours
    }
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    if (msg.ok) p.resolve(msg.result);
    else p.reject(new Error(`emporium bridge: ${msg.error}`));
  });
  const died = new Promise((_, reject) => {
    child.on("exit", (code) => {
      const err = new Error(`emporium bridge exited (${code}): ${stderr.slice(-2000)}`);
      for (const p of pending.values()) p.reject(err);
      pending.clear();
      reject(err);
    });
  });
  died.catch(() => {});

  function call(op, args = {}) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      child.stdin.write(JSON.stringify({ id, op, args }) + "\n");
    });
  }

  return {
    call,
    close: () => new Promise((r) => {
      child.on("exit", r);
      child.stdin.end();
      setTimeout(() => child.kill(), 5000).unref();
    }),
  };
}
