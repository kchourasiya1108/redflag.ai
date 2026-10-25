"""Reporter: the report card and case file, built only from the Case File.

Every number in the report comes from the Case File; when a model is
available the Reporter only rewrites the reasons in plain language and the
Citation verifier then re-checks every cited value (phase 16).
"""
from __future__ import annotations

import json

from agent.agents.base import Agent, AgentResult, RunContext

DISCLAIMER = ("RedFlag gives a risk rating with evidence, not investment advice. It never "
              "says apply, avoid, buy or sell. Links to regulatory orders mean an entity "
              "was named in that order, not that this company did anything wrong.")
TITLES = {"F1": "Paper profits", "F2": "Profit spike before the IPO", "F3": "Eligibility gaming",
          "F4": "Cheap insider shares", "F5": "Customer/supplier concentration",
          "F6": "Related-party dependence", "F7": "Auditor risk", "F8": "Overvalued vs peers",
          "F9": "Vague use of money", "F10": "Lead manager track record",
          "F11": "Watchlist link", "F12": "Drift between drafts",
          "F13": "Repeat anchors / market maker", "F14": "Credit rating red flag"}
ORDER = {"HIGH": 0, "ELEVATED": 1, "LOW": 2}


def build_output(ctx: RunContext) -> dict:
    doc = ctx.case.case_file()
    score = doc["score"] or {}
    flags = sorted(doc["flags"], key=lambda f: (ORDER[f["severity"]], f["id"]))
    reasons = [{"flag": f["id"], "title": TITLES.get(f["id"], f["id"]), "severity": f["severity"],
                "text": f["text"], "evidence": f["evidence"], "confidence": f["confidence"]}
               for f in flags if f["severity"] != "LOW"]
    cleared = [{"flag": f["id"], "title": TITLES.get(f["id"], f["id"]), "text": f["text"],
                "evidence": f["evidence"]} for f in flags if f["severity"] == "LOW"]
    skeptic = [{"flag": f["id"], **f["skeptic"]} for f in flags if f.get("skeptic")]
    return {"ipo": {k: str(v) for k, v in ctx.meta.items()}, "as_of": ctx.as_of.isoformat(),
            "rating": score.get("band"), "p_adverse": score.get("p_adverse"),
            "model_version": score.get("version"), "depth": doc["meta"]["depth"],
            "reasons": reasons, "skeptic_notes": skeptic, "cleared_checks": cleared,
            "questions": doc["open_questions"],
            "not_checked": [f"{n['item']}: {n['reason']}" for n in doc["not_checked"]],
            "data_source": ctx.source, "disclaimer": DISCLAIMER}


def render_markdown(out: dict) -> str:
    ipo = out["ipo"]
    lines = [f"# RedFlag report: {ipo.get('company')} ({ipo.get('exchange')})", "",
             f"**Rating: {out['rating']}**"
             + (f" (probability of an adverse outcome {out['p_adverse']:.0%})"
                if out["p_adverse"] is not None else ""),
             f"As of the bid opening date {out['as_of']}. Depth: {out['depth']}. "
             f"Model {out['model_version']}. Data: {out['data_source']}.", ""]
    sections = [("Reasons", [f"**{r['flag']} {r['title']} ({r['severity']}).** {r['text']} "
                             f"_Evidence: {', '.join(r['evidence'][:4])}_" for r in out["reasons"]]),
                ("Skeptic's notes", [f"**{s['flag']}: {s['ruling']}.** {s['reason']} "
                                     f"_Evidence: {', '.join(s.get('evidence', []))}_"
                                     for s in out["skeptic_notes"]]),
                ("Checks that passed", [f"**{c['flag']} {c['title']}.** {c['text']}"
                                        for c in out["cleared_checks"]]),
                ("Questions to ask before applying", out["questions"]),
                ("Not checked", out["not_checked"])]
    for title, items in sections:
        lines += [f"## {title}", ""] + ([f"- {i}" for i in items] or ["- None."]) + [""]
    lines += ["---", "", out["disclaimer"], ""]
    return "\n".join(lines)


class Reporter(Agent):
    name = "Reporter"
    role = "Writes the report card and case_file.json from the Case File"

    def run(self, ctx: RunContext) -> AgentResult:
        out = build_output(ctx)
        folder = ctx.cfg.data.parent / "output" / ctx.run_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "report.md").write_text(render_markdown(out))
        (folder / "case_file.json").write_text(json.dumps(
            {**ctx.case.case_file(), "trace_ref": str(ctx.tracer.path)}, indent=2, default=str))
        ctx.scratch["output"], ctx.scratch["output_dir"] = out, folder
        return AgentResult(f"rating {out['rating']} with {len(out['reasons'])} reasons, "
                           f"{len(out['questions'])} questions", data={"dir": str(folder)})
