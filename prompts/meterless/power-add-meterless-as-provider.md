# POWER: Add Meterless as a provider

Source: https://meterless.ai/engines.html (POWER tab), copied 2026-10-25.
Implemented in `agent/llm.py` (`normalise_base_url`, `meterless_token`,
`providers_from_env`) and checked by `smoke_test.py`.

---

Add Meterless Runtime to this codebase as an OpenAI-compatible provider, next to the providers it already has.

The contract (Meterless Runtime 1.4, provider contract v4):
- Provider id meterless, name Meterless Runtime, default base URL http://127.0.0.1:8088/v1. Accept a pasted URL with or without a scheme, /v1, /v1/chat/completions, /v1/models or /healthz, and normalise it to <host>/v1. Never produce /v1/v1.
- Auth is real. Send Authorization: Bearer <token> on every call. The runtime writes the token to <stateDir>/proxy-token. Store it under the key meterless_runtime_token. The placeholder rt-local is rejected.
- Send meterless-app: <a stable lowercase id for this app> on every request, and meterless-scope: user on chat.
- Model ids are routing modes, not models: meterless/<disposition> (auto, fast, deep, cheap, local, fusion), meterless/<class> and meterless/<class>.<disposition>. Read them from GET /v1/models and keep any meterless/* id you don't recognise.
- GET /healthz only proves the process is up. GET /v1/models decides: 401 or 403 is "unauthorized" (show a field to paste the token), 200 with meterless ids is "online", anything else is "offline".
- Auto Detect tries ports 8088, 8090, 8787 and 11434 on 127.0.0.1, localhost and [::1], and saves the first online or unauthorized hit.
- Plain text requests stay a string, exactly like OpenAI. Use a 180 s timeout, because a model load can take minutes.
- If the user pastes a connect link (http://<host>:<port>/connect#k=<key>), take the base URL from its origin + /v1 and the key from k. Never log it.

You are done when a wrong token shows unauthorized (not offline), Auto Detect finds the runtime, and a plain text request on meterless/auto returns a string.
