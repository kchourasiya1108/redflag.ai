# Meterless Nightshift setup (this Mac)

Topology: Zetaris runs in Docker on the Linux PC; Meterless Nightshift and the
RedFlag app run on this Mac (Apple M4, macOS 15). Nightshift is a desktop app,
so nothing in this folder installs it. This folder holds the walkthrough and
`check.py`, which checks the connection against provider contract v4.

## 1. Install Nightshift (M1+ edition)

1. Open <https://meterless.ai/Nightshift/M1+/>. This is the free edition for
   M1 to M4 Macs. The M5+ edition and the paid Mac edition do not run on an M4.
2. Enter your email at the access gate and click the verification link in the
   email they send.
3. Download the app, install it and launch it. Finish the first-run prompts and
   leave Nightshift running.

## 2. Add NVIDIA as a provider (inside Nightshift)

1. Sign in at <https://build.nvidia.com/models>, pick a Nemotron model that has
   an API endpoint, and copy your `nvapi-...` key.
2. In Nightshift, go to **Setup -> Providers -> NVIDIA**, paste the key, save it
   and confirm NVIDIA shows as connected.

The NVIDIA key stays in Nightshift. Do not put it in `.env` or in Claude Code.

## 3. Configure lanes

Go to **NIGHT -> LANES** and set up these lanes:

RedFlag does not depend on lanes. It pins a Nemotron model for each role in
`.env`, so the model that serves every request is explicit:

```
MODEL_ROUTE_EXTRACT=nvidia/nemotron-3.5-lightning-30b-a3b
MODEL_ROUTE_REASON=nvidia/nemotron-3-ultra-550b-a55b
```

These ids must appear in `GET /v1/models`, and `check.py` fails if they don't.
If you remove the two lines, the roles fall back to `meterless/fast` and
`meterless/deep`, and the lanes then decide which model serves each one.
`super-vl-120b` is not in this runtime's catalog.

## 4. Connect RedFlag (Console)

In Nightshift, open **Console** (top right) and copy the Runtime URL and the API
key into `.env` at the repo root:

```
METERLESS_BASE_URL=http://127.0.0.1:8088/v1
METERLESS_RUNTIME_TOKEN=<token from the Console>
```

You can also paste a connect link of the form `http://host:port/connect#k=...`.
Use the link's origin plus `/v1` as the URL and the value after `k=` as the token.

## 5. Verify

```
.venv/bin/python meterless/check.py --detect
.venv/bin/python meterless/check.py --model <a Nemotron-pinned meterless/* id from the models line>
```

The setup is done when every line says PASS. A wrong token must show
`unauthorized`, not `offline`. For the NVIDIA award, the `chat` line for the
pinned route must name a Nemotron model.
