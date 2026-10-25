"""Offline pipeline stages, in dependency order (docs/DESIGN.md, Offline pipeline)."""
from __future__ import annotations

import importlib

# name -> module; each module has run(ctx) -> dict and gate(conn, settings) -> Gate
STAGES = {
    "universe": "redflag.pipeline.universe",          # NSE Emerge IPO list
    "bse_docs": "redflag.pipeline.bse_docs",          # BSE SME list + offer documents
    "prices": "redflag.pipeline.prices",              # NSE + BSE daily prices
    "bse_universe": "redflag.pipeline.bse_universe",  # BSE IPOs into ipos/docs
    "sme_index": "redflag.pipeline.sme_index",        # NIFTY SME EMERGE history
    "labels": "redflag.pipeline.labels",
    "sebi": "redflag.pipeline.sebi",
    "rhp": "redflag.pipeline.rhp",                    # NSE RHP downloads
    "offer_facts": "redflag.pipeline.offer_facts",    # lot size, CIN/NIC, market maker, auditor
    "intermediaries": "redflag.pipeline.intermediaries",  # registrar + market maker masters
    "features": "redflag.pipeline.features",
    "train": "redflag.pipeline.train",
    "publish": "redflag.pipeline.publish",
}


def stage_module(name: str):
    if name not in STAGES:
        raise SystemExit(f"unknown stage {name!r}; choose from {', '.join(STAGES)}")
    return importlib.import_module(STAGES[name])
