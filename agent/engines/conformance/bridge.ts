// Shared client for agent/engines/bridge.py: runs one Python op per call.
// Pure ops run synchronously (constructors must throw synchronously on a bad
// config); `stream` ops answer the engine's generator requests while it runs.

import { spawn, spawnSync } from "node:child_process";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
export const REPO = path.resolve(here, "..", "..", "..");
const PYTHON = process.env.REDFLAG_PYTHON ?? path.join(REPO, ".venv", "bin", "python");

interface Reply { op: string; ok: boolean; result?: any; error?: string; args?: any[] }

function args(engine: string, op: string): string[] {
  return ["-m", "agent.engines.bridge", engine, op];
}

export function callSync(engine: string, op: string, request: unknown): any {
  const out = spawnSync(PYTHON, args(engine, op), { cwd: REPO, input: JSON.stringify(request) + "\n", encoding: "utf8" });
  if (out.status !== 0) throw new Error(`bridge ${engine}.${op} exited ${out.status}: ${out.stderr}`);
  const reply: Reply = JSON.parse(out.stdout.trim().split("\n").pop() ?? "{}");
  if (!reply.ok) throw new Error(reply.error);
  return reply.result;
}

export async function callStreaming(
  engine: string,
  op: string,
  request: unknown,
  handlers: Record<string, (...a: any[]) => Promise<unknown>>
): Promise<any> {
  const child = spawn(PYTHON, args(engine, op), { cwd: REPO, stdio: ["pipe", "pipe", "inherit"] });
  child.stdin.write(JSON.stringify(request) + "\n");
  const lines = readline.createInterface({ input: child.stdout });
  for await (const line of lines) {
    const msg: Reply = JSON.parse(line);
    if (msg.op === "result") {
      child.stdin.end();
      if (!msg.ok) throw new Error(msg.error);
      return msg.result;
    }
    const handler = handlers[msg.op];
    try {
      if (!handler) throw new Error(`no handler for ${msg.op}`);
      child.stdin.write(JSON.stringify({ ok: true, result: await handler(...(msg.args ?? [])) }) + "\n");
    } catch (err) {
      child.stdin.write(JSON.stringify({ ok: false, error: (err as Error).message }) + "\n");
    }
  }
  throw new Error(`bridge ${engine}.${op} ended without a result`);
}
