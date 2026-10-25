"""Offline pipeline stages, in dependency order (docs/DESIGN.md, Offline pipeline)."""
from __future__ import annotations

import importlib

# name -> module; each module has run(ctx) -> dict and gate(conn, settings) -> Gate
STAGES = {
    "universe": "pipeline.universe",          # NSE Emerge IPO list
    "bse_docs": "pipeline.bse_docs",          # BSE SME list + offer documents
    "prices": "pipeline.prices",              # NSE + BSE daily prices
    "bse_universe": "pipeline.bse_universe",  # BSE IPOs into ipos/docs
    "sme_index": "pipeline.sme_index",        # NIFTY SME EMERGE history
    "labels": "pipeline.labels",
    "sebi": "pipeline.sebi",
    "rhp": "pipeline.rhp",                    # NSE RHP downloads
    "offer_facts": "pipeline.offer_facts",    # lot size, CIN/NIC, market maker, auditor
    "intermediaries": "pipeline.intermediaries",  # registrar + market maker masters
    "features": "pipeline.features",
    "train": "pipeline.train",
    "publish": "pipeline.publish",
}


def stage_module(name: str):
    if name not in STAGES:
        raise SystemExit(f"unknown stage {name!r}; choose from {', '.join(STAGES)}")
    return importlib.import_module(STAGES[name])
