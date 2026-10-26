"""Intake: identifies the IPO, fixes as_of to its bid opening date, opens the Case File."""
from __future__ import annotations

from agent.agents.base import Agent, AgentResult, RunContext
from agent import uploads
from agent.case_file import CaseFile
from agent.tools_zetaris import DataPort


class UnknownIPO(LookupError):
    pass


class Intake(Agent):
    name = "Intake"
    role = "Identifies the IPO and fixes as_of to its bid opening date"

    def run(self, ctx: RunContext) -> AgentResult:
        if uploads.is_upload(ctx.ipo_id):
            return self.from_upload(ctx)
        rows = DataPort(ctx.client, None, on_query=ctx.on_query).run(
            "ipo_meta", ipo_id=ctx.ipo_id).rows
        if not rows:
            raise UnknownIPO(f"{ctx.ipo_id} is not in the archive")
        ctx.meta = rows[0]
        ctx.as_of = ctx.meta["open_date"]
        ctx.port = DataPort(ctx.client, ctx.as_of, on_query=ctx.on_query)
        return self.open_case(ctx)

    def from_upload(self, ctx: RunContext) -> AgentResult:
        """An upcoming IPO from an uploaded RHP: judged as of its bid date, or today if later."""
        record = uploads.load(ctx.cfg, ctx.ipo_id)
        if record is None:
            raise UnknownIPO(f"{ctx.ipo_id} is not an uploaded RHP")
        if gaps := uploads.missing(record):
            raise UnknownIPO(f"{ctx.ipo_id} is missing {', '.join(gaps)}; confirm the details first")
        ctx.meta = uploads.meta(record)
        ctx.as_of = uploads.as_of(record)
        ctx.port = DataPort(ctx.client, ctx.as_of, on_query=ctx.on_query)
        ctx.port.provide(uploads.local_rows(record, ctx.port))
        return self.open_case(ctx)

    def open_case(self, ctx: RunContext) -> AgentResult:
        ctx.case = CaseFile(ctx.run_id, ctx.ipo_id, ctx.as_of, log_dir=ctx.cfg.logs / ctx.run_id,
                            meta={"company": ctx.meta["company"], "exchange": ctx.meta["exchange"],
                                  "issue_price": ctx.meta["issue_price"],
                                  "sample_mode": ctx.sample_mode, "data_source": ctx.source})
        return AgentResult(f"{ctx.meta['company']} ({ctx.meta['exchange']}); as_of fixed to "
                           f"{ctx.as_of}", target="Names")
