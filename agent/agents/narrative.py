"""Narrative sections (F4, F9) through the Meterless Markovian engine."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from agent.agents.base import Agent, AgentResult, RunContext
from agent.agents.planner import FORENSIC, LIGHT, STANDARD
from agent.narrative import (reprice_peers, insider_discount, objects_summary, peer_table, peer_valuation,
                             printed_gross, read_section, rpt_share)
from agent.specialists import (f4_insider_discount, f6_related_parties, f8_overvalued,
                               f9_use_of_money)

# Sections per depth (design: LIGHT reads the core sections; STANDARD adds
# the objects and related parties). Two at a time: Nightshift fails more
# often under heavier concurrency (measured 25 Oct).
SECTIONS_BY_DEPTH = {LIGHT: ("capital", "basis"),
                     STANDARD: ("capital", "basis", "objects", "rpt"),
                     FORENSIC: ("capital", "basis", "objects", "rpt")}


class NarrativeReader(Agent):
    name = "NarrativeReader"
    role = ("Reads the share capital history, peer comparison, objects of the issue and "
            "related-party transactions page by page with the Meterless Markovian engine "
            "(Nemotron), then computes F4, F8, F9 and F6 in code")
    uses_llm = True

    def run(self, ctx: RunContext) -> AgentResult:
        pages = ctx.scratch.get("pages") or []
        depth = ctx.case.meta.get("depth")
        if (ctx.gateway is None or not pages or depth not in SECTIONS_BY_DEPTH
                or ctx.seconds_left() < 180):
            why = ("no model configured" if ctx.gateway is None else "no RHP text" if not pages
                   else f"no sections are read at depth {depth}" if depth not in SECTIONS_BY_DEPTH
                   else "the run's time limit is near")
            for item in ("F4 insider discount", "F6 related parties", "F8 valuation vs peers",
                         "F9 use of money"):
                ctx.case.add_not_checked(item, why, agent=self.name)
            return AgentResult(f"skipped: {why}")
        if depth == LIGHT:
            for item in ("F6 related parties", "F9 use of money"):
                ctx.case.add_not_checked(item, "a quick screen does not read this part of the prospectus",
                                         agent=self.name)
        history = ctx.cfg.logs / ctx.run_id / "markovian.jsonl"
        memory = ""
        if ctx.memory is not None:      # H-MEM: how this banker's RHPs were laid out before
            bankers = [n.entity_id for n in ctx.named if n.role == "banker" and n.entity_id]
            memory = ctx.memory.context_block(ctx.memory.retrieve(
                "RHP layout share capital history objects of the issue pages",
                as_of=ctx.as_of, tags=["rhp_layout"], entities=bankers))

        def on_event(e):
            if e["phase"] in ("complete", "error"):
                ctx.tracer.event(self.name, "markovian", f"{e['markovian_id']} {e['phase']}: "
                                 f"{e['chunks']} chunk(s)", status=e["status"], engine="markovian")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {n: pool.submit(read_section, ctx.gateway, pages, n, history=history,
                                      run_id=ctx.run_id, on_event=on_event,
                                      memory_context=memory)
                       for n in SECTIONS_BY_DEPTH[depth]}
            results = {n: f.result() for n, f in futures.items()}
        done = []
        cap = results.get("capital")
        if cap is None:
            pass
        elif cap.data.get("rows"):
            summary = insider_discount(cap.data["rows"], ctx.meta.get("issue_price") or 0.0,
                                       ctx.as_of)
            finding = f4_insider_discount(summary)
            if finding is None and not ctx.meta.get("issue_price"):
                ctx.case.add_not_checked("F4 insider discount", "the issue price (price band) "
                                         "is not announced yet", agent=self.name)
            else:
                self._record(ctx, finding, cap, "F4 insider discount", done)
        else:
            ctx.case.add_not_checked("F4 insider discount", cap.error or "the share capital history could not be read",
                                     agent=self.name)
        obj = results.get("objects")
        if obj is None:
            pass
        elif obj.data.get("rows"):
            gross = printed_gross(pages, obj.pages)
            self._record(ctx, f9_use_of_money(objects_summary(obj.data, ctx.as_of, gross),
                                              obj.pages), obj,
                         "F9 use of money", done)
        else:
            ctx.case.add_not_checked("F9 use of money", obj.error or "the table of how the money will be used could not be read",
                                     agent=self.name)
        basis = results.get("basis")
        if basis:
            # The peer table read in code wins over the model's; the model's answer fills
            # what the code did not find (the issuer's own P/E or EPS).
            code = peer_table(pages, basis.pages, ctx.meta.get("company", ""))
            if code.get("peers"):
                basis.data = {**basis.data, "peers": code["peers"],
                              "issue_eps": basis.data.get("issue_eps") or code.get("issue_eps")}
        if basis and basis.data.get("peers"):
            self._record(ctx, f8_overvalued(reprice_peers(peer_valuation(basis.data, ctx.meta.get("company", ""),
                                                                         ctx.meta.get("issue_price")),
                                                          ctx.port, ctx.as_of)),
                         basis, "F8 valuation vs peers", done)
        elif basis:
            ctx.case.add_not_checked("F8 valuation vs peers", basis.error or "the comparison with listed peers could not be read",
                                     agent=self.name)
        rpt = results.get("rpt")
        facts = ctx.scratch.get("facts")
        if rpt and rpt.data.get("rows") and facts is not None:
            revenue = {fy: facts.get("pl.revenue", fy) for fy in facts.years()}
            self._record(ctx, f6_related_parties(rpt_share(
                rpt.data, {k: v for k, v in revenue.items() if v})), rpt, "F6 related parties", done)
        elif rpt:
            ctx.case.add_not_checked("F6 related parties", rpt.error or
                                     "no RPT table or verified revenue", agent=self.name)
        ctx.scratch.setdefault("layout_notes", []).extend(
            f"{ctx.meta.get('banker_raw')} RHP: {r.name} section read from pages "
            f"{r.pages[0]}-{r.pages[-1]}" for r in results.values() if r.pages and r.data.get("rows"))
        runs = [r.run for r in results.values() if r.run]
        saved = sum(r.total_tokens_saved for r in runs)
        return AgentResult(f"{', '.join(done) or 'no narrative flags'}; Markovian "
                           f"{sum(len(r.chunks) for r in runs)} chunks, {saved} tokens saved",
                           data={"markovian": [r.to_wire() | {"chunks": len(r.chunks)}
                                               for r in runs]})

    def _record(self, ctx, finding, section, item, done):
        if finding is None:
            ctx.case.add_not_checked(item, "the figures this needs were not in the table we read",
                                     agent=self.name)
            return
        ctx.case.add_flag(finding.flag, finding.severity, value=finding.value,
                          evidence=[f"rhp:p{p}" for p in (finding.pages or section.pages[:2])],
                          confidence=0.55, agent=self.name, text=finding.text)
        done.append(f"{finding.flag}={finding.severity}")
