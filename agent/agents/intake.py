"""Intake: identifies the IPO, fixes as_of to its bid opening date, opens the Case File."""
from __future__ import annotations

from agent.agents.base import Agent, AgentResult, RunContext
from agent.case_file import CaseFile
from agent.tools_zetaris import DataPort


class UnknownIPO(LookupError):
    pass


class Intake(Agent):
    name = "Intake"
    role = "Identifies the IPO and fixes as_of to its bid opening date"

    def run(self, ctx: RunContext) -> AgentResult:
        rows = DataPort(ctx.client, None, on_query=ctx.on_query).run(
            "ipo_meta", ipo_id=ctx.ipo_id).rows
        if not rows:
            raise UnknownIPO(f"{ctx.ipo_id} is not in the archive")
        ctx.meta = rows[0]
        ctx.as_of = ctx.meta["open_date"]
        ctx.port = DataPort(ctx.client, ctx.as_of, on_query=ctx.on_query)
        ctx.case = CaseFile(ctx.run_id, ctx.ipo_id, ctx.as_of, log_dir=ctx.cfg.logs / ctx.run_id,
                            meta={"company": ctx.meta["company"], "exchange": ctx.meta["exchange"],
                                  "issue_price": ctx.meta["issue_price"],
                                  "sample_mode": ctx.sample_mode, "data_source": ctx.source})
        return AgentResult(f"{ctx.meta['company']} ({ctx.meta['exchange']}); as_of fixed to "
                           f"{ctx.as_of}", target="Names")
