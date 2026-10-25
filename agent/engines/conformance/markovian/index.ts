// Conformance adapter: exposes RedFlag's Python Markovian engine
// (agent/engines/markovian.py) with the reference interface, so the official
// suite tests the Python code itself:
//
//   MARKOVIAN_IMPL=$PWD/agent/engines/conformance/markovian/index.ts \
//     npx tsx <Meterless>/engines/markovian/conformance/runner.ts

import { callStreaming, callSync } from "../bridge.ts";

type Generator = (prompt: string, attachments: unknown[], systemPrompt?: string) => Promise<{ text: string; metadata?: unknown }>;

export class Markovian {
  readonly config: Record<string, number>;
  private generator?: Generator;

  constructor(opts: { chunkConfig?: Record<string, number>; generator?: Generator; clock?: () => number } = {}) {
    this.config = callSync("markovian", "validate", { chunkConfig: opts.chunkConfig ?? {} });
    this.generator = opts.generator;
  }

  async run(options: { goal?: string; prompt?: string; generator?: Generator; reflect?: boolean; mode?: string; memoryContext?: string; attachments?: unknown[] }): Promise<any> {
    const generator = options.generator ?? this.generator;
    if (!generator) throw new Error("Markovian: provide a generator");
    const { generator: _g, ...rest } = options;
    return callStreaming("markovian", "run", { chunkConfig: this.config, options: rest }, {
      generate: async (prompt: string, attachments: unknown[], systemPrompt?: string) => generator(prompt, attachments, systemPrompt ?? undefined),
    });
  }
}

export function modelTotals(n: number, constants?: Record<string, number>) {
  return callSync("markovian", "modelTotals", { n, constants });
}

export function runEfficiency(chunks: { tokens: number }[], config: Record<string, number>) {
  return callSync("markovian", "runEfficiency", { chunks, config });
}
