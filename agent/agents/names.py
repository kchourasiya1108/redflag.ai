"""Names first: everyone involved in the IPO goes into the Case File as an entity.

From the archive: the issuer, lead managers, auditor, market maker and
registrar. The Extractor adds promoters and directors read from the RHP's
cover and general-information pages when a model is available.
"""
from __future__ import annotations

from agent.agents.base import Agent, AgentResult, RunContext
from agent.features import archive_parties

EDGE = {"banker": "lead_manager_of", "auditor": "auditor_of", "market_maker": "market_maker_of",
        "registrar": "registrar_of", "person": "director_of"}


class Names(Agent):
    name = "Names"
    role = "Records the issuer, lead managers, auditor, market maker and registrar"

    def run(self, ctx: RunContext) -> AgentResult:
        ctx.named = archive_parties(ctx.port, ctx.ipo_id, ctx.meta["company"])
        for n in ctx.named:
            if n.role == "company":
                continue
            eid = ctx.case.add_entity(n.role, n.name, source=n.source, agent=self.name,
                                      attrs={"archive_id": n.entity_id} if n.entity_id else None)
            ctx.case.link(eid, EDGE.get(n.role, "party_to"), ctx.case.company,
                          source=n.source, agent=self.name)
        roles = sorted({n.role for n in ctx.named if n.role != "company"})
        return AgentResult(f"{len(ctx.named) - 1} parties: {', '.join(roles)}",
                           target="BankerTrackRecord")
