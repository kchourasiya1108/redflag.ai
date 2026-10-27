# Prompts

The prompt files RedFlag sends to models live here, one per agent or per RHP
section (for example `extractor/<section>.md`, `skeptic.md`, `reporter.md`).
Agents load them by name, so a prompt can change without a code change.

`meterless/` keeps the prompts from meterless.ai/engines.html that were used
to build RedFlag. Each file records its source, the date it was copied, and
the code that implements it.
