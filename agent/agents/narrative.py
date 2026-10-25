"""Narrative sections (F4, F9) through the Meterless Markovian engine."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from agent.agents.base import Agent, AgentResult, RunContext
from agent.agents.planner import FORENSIC, STANDARD
from agent.narrative import insider_discount, objects_summary, read_section
from agent.specialists import f4_insider_discount, f9_use_of_money


class NarrativeReader(Agent):
    name = "NarrativeReader"
    role = ("Reads the share capital history and the objects of the issue page by page with "
            "the Meterless Markovian engine (Nemotron), then computes F4 and F9 in code")
    uses_llm = True

    def run(self, ctx: RunContext) -> AgentResult:
        pages = ctx.scratch.get("pages") or []
        depth = ctx.case.meta.get("depth")
        if ctx.gateway is None or not pages or depth not in (STANDARD, FORENSIC):
            why = ("no model configured" if ctx.gateway is None else "no RHP text" if not pages
                   else f"{depth} depth reads the core sections only")
            for item in ("F4 insider discount", "F9 use of money"):
                ctx.case.add_not_checked(item, why, agent=self.name)
            return AgentResult(f"skipped: {why}")
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
                       for n in ("capital", "objects")}
            results = {n: f.result() for n, f in futures.items()}
        done = []
        cap = results["capital"]
        if cap.data.get("rows"):
            summary = insider_discount(cap.data["rows"], ctx.meta.get("issue_price") or 0.0,
                                       ctx.as_of)
            self._record(ctx, f4_insider_discount(summary), cap, "F4 insider discount", done)
        else:
            ctx.case.add_not_checked("F4 insider discount", cap.error or "no allotment rows read",
                                     agent=self.name)
        obj = results["objects"]
        if obj.data.get("rows"):
            self._record(ctx, f9_use_of_money(objects_summary(obj.data), obj.pages), obj,
                         "F9 use of money", done)
        else:
            ctx.case.add_not_checked("F9 use of money", obj.error or "no objects table read",
                                     agent=self.name)
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
            ctx.case.add_not_checked(item, "the numbers needed were not in the table read",
                                     agent=self.name)
            return
        ctx.case.add_flag(finding.flag, finding.severity, value=finding.value,
                          evidence=[f"rhp:p{p}" for p in (finding.pages or section.pages[:2])],
                          confidence=0.55, agent=self.name, text=finding.text)
        done.append(f"{finding.flag}={finding.severity}")
