// Conformance adapter: exposes RedFlag's Python World Model
// (agent/engines/world.py) with the reference interface. The runner calls
// some methods synchronously (stateShapeCanonical, views, audit), so every
// call replays this instance's write log in one blocking Python process. The
// engine is deterministic (content-addressed ids, observation-driven time),
// so a replay reproduces the same state.
//
//   WORLD_MODEL_IMPL=$PWD/agent/engines/conformance/world_model/index.ts \
//     npx tsx <Meterless>/engines/world-model/conformance/runner.ts

import { callSync } from "../bridge.ts";

type Op = [string, unknown[]];

export const entityIdFromName = (type: string, name: string): string => callSync("world-model", "entityIdFromName", { args: [type, name] });
export const normalizeName = (name: string): string => callSync("world-model", "normalizeName", { args: [name] });
export const decide = (similarity: number): string => callSync("world-model", "decide", { args: [similarity] });

export class WorldModel {
  private ops: Op[] = [];
  private options: Record<string, unknown>;

  constructor(opts: { namespace: string; storage?: string; clock?: () => Date; conflictPolicy?: string; sourcePriority?: string[] }) {
    if (!opts.namespace) throw new Error("WorldModel: namespace is required");
    this.options = {
      namespace: opts.namespace,
      clockAt: opts.clock ? opts.clock().toISOString() : undefined,
      conflictPolicy: opts.conflictPolicy,
      sourcePriority: opts.sourcePriority,
    };
  }

  private read(method: string, ...args: unknown[]): any {
    return callSync("world-model", "replay", { options: this.options, ops: this.ops, call: [method, args] });
  }

  private write(method: string, ...args: unknown[]): any {
    const result = this.read(method, ...args); // throws on rejection: nothing is logged
    this.ops.push([method, args]);
    return result;
  }

  async upsertEntity(input: unknown) { return this.write("upsertEntity", input); }
  async upsertContext(input: unknown) { return this.write("upsertContext", input); }
  async relate(input: unknown) { return this.write("relate", input); }
  async assertFact(input: unknown) { return this.write("assertFact", input); }
  async ingest(observations: unknown[]) { return this.write("ingest", observations); }
  async approveMerge(id: string, by: string) { return this.write("approveMerge", id, by); }
  async rejectMerge(id: string, by: string) { return this.write("rejectMerge", id, by); }

  stateShapeCanonical(): string { return this.read("stateShapeCanonical"); }
  stateShape(): unknown { return this.read("stateShape"); }
  audit(): any[] { return this.read("audit"); }
  events(): any[] { return this.read("events"); }
  mergeQueue(): any[] { return this.read("mergeQueue"); }

  view(name: string): any {
    const self = this;
    if (name === "graph") {
      return {
        entity: (id: string) => self.read("graph.entity", id),
        neighborsOf: (id: string, opts?: unknown) => self.read("graph.neighborsOf", id, opts ?? {}),
        incoming: (id: string, opts?: unknown) => self.read("graph.incoming", id, opts ?? {}),
        query: (opts?: { type?: string }) => self.read("graph.query", opts ?? {}),
        traverse: (id: string, opts: unknown) => self.read("graph.traverse", id, opts),
      };
    }
    if (name === "facts") return { about: (id: string) => self.read("facts.about", id) };
    if (name === "snapshot") return { entityAt: (id: string, at: string) => self.read("snapshot.entityAt", id, at) };
    throw new Error(`World Model: unknown view "${name}"`);
  }
}
