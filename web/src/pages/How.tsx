import { useEffect } from "react";
import { Brain, Database, FileSearch, Gavel, Layers, Lock, Scale, Sparkles, Target } from "lucide-react";
import { Card } from "../components/ui";
import { ServiceDetails } from "../components/Header";
import type { Status } from "../types";
import { STAGES } from "./Run";
import { FLAGS, Link } from "../lib";

const LLM = new Set(["Extractor", "NarrativeReader", "Skeptic", "Reporter"]);
const NOT_YET = new Set(["F12", "F13", "F14"]);

const STEPS = [
  { icon: <Gavel size={18} />, t: "Screen the people", d: "Everyone named in the issue (promoters, the lead manager, auditor, registrar, market maker) is checked against SEBI orders dated before the bid, and the lead manager's past SME IPOs are looked up." },
  { icon: <FileSearch size={18} />, t: "Read the prospectus", d: "The restated financial statements are read and cross-checked: a year only counts if its figures add up. AI reads the share history, peer comparison, use of money and related-party sections." },
  { icon: <Scale size={18} />, t: "Argue against ourselves", d: "Each concern is challenged: we look for an ordinary explanation in the prospectus, and drop or soften the concern if we find one." },
  { icon: <Target size={18} />, t: "Rate the risk", d: "A rating model trained on how past SME IPOs turned out gives the chance of a bad outcome, and the rating band." },
];

export function HowPage({ status }: { status: Status | null }) {
  useEffect(() => {
    if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  }, []);
  return (
    <div className="container stack-xl">
      <div className="stack-sm">
        <div className="eyebrow">How we rate</div>
        <h1>A second pair of eyes on the prospectus.</h1>
        <p className="muted" style={{ maxWidth: 720 }}>
          SME IPOs have lighter rules than main-board issues, and a prospectus runs to hundreds of pages. RedFlag does the slow
          reading for you, judges each IPO only on what was known the day its bid opened, and shows where every finding comes from.
        </p>
      </div>

      <div className="checks-grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))" }}>
        {STEPS.map((s, i) => (
          <div className="check-tile" key={s.t}>
            <div className="row"><div className="ic">{s.icon}</div><span className="faint small">Step {i + 1}</span></div>
            <h3>{s.t}</h3>
            <p className="small muted">{s.d}</p>
          </div>
        ))}
      </div>

      <Card title="What we check, and why it matters" pad={false}>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Check</th><th>Why it matters</th></tr></thead>
            <tbody>
              {Object.entries(FLAGS).map(([id, f]) => (
                <tr key={id}>
                  <td style={{ minWidth: 200 }}><b>{f.title}</b><div className="tiny faint">{f.what}</div>{NOT_YET.has(id) && <div className="tiny faint">Coming later: the data is not collected yet</div>}</td>
                  <td className="small muted">{f.why}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title={<span className="row"><Lock size={16} /> Rules we hold ourselves to</span>}>
        <ul className="ticks small">
          <li>Only information dated before the bid opened is used, so past IPOs are judged fairly.</li>
          <li>To explain a concern away, we must point to a page of the prospectus; otherwise the concern stands.</li>
          <li>A concern is only rated serious when a second, independent signal backs it up.</li>
          <li>Financial figures that don't add up are shown but never used in the rating.</li>
          <li>Any AI-written summary is published only if every number in it matches the prospectus data.</li>
          <li>What we could not check is listed in every report, never silently skipped.</li>
          <li>We never say buy, sell, apply or avoid. Being named in a SEBI order is not proof of wrongdoing.</li>
        </ul>
      </Card>

      <Card title="What counts as a bad outcome">
        <p className="small muted">
          An IPO "went badly" if, within six months of listing, its share price fell sharply, trading was suspended, or the company
          faced SEBI action. <Link to="/track-record">See how often our ratings got this right.</Link>
        </p>
      </Card>

      <section id="technology" className="stack">
        <h2>Under the hood</h2>
        <div className="grid-3">
          <Card title={<span className="row"><Database size={16} /> Zetaris Lightning</span>}>
            <p className="small muted">The data layer. Every lookup is a named SQL query over the IPO archive, filings, SEBI orders and prices, and a leakage guard drops anything dated after the bid.</p>
          </Card>
          <Card title={<span className="row"><Brain size={16} /> NVIDIA Nemotron</span>}>
            <p className="small muted">The AI models, served through Meterless Nightshift. They read table rows and narrative sections, argue each concern, and draft the summary. They never supply a number.</p>
          </Card>
          <Card title={<span className="row"><Layers size={16} /> Meterless engines</span>}>
            <p className="small muted">Markovian reads long sections chunk by chunk; the World Model keeps one shared case file with a replayable log; H-MEM remembers prospectus layouts across checks.</p>
          </Card>
        </div>
        <div className="grid-2">
          <Card title="The thirteen agents" hint="Grey steps are plain code; AI steps are marked" pad={false}>
            <ol className="pipeline" style={{ margin: 0, padding: 12, listStyle: "none" }}>
              {STAGES.map((s, i) => (
                <li key={s.agent} className="stage">
                  <span className="icon"><span className="tiny">{i + 1}</span></span>
                  <div>
                    <div className="name row" style={{ gap: 8 }}>{s.name}{LLM.has(s.agent) && <span className="pill" style={{ height: 20, fontSize: 11 }}><Sparkles size={11} /> Nemotron</span>}</div>
                    <div className="what">{s.what}</div>
                  </div>
                  <span />
                </li>
              ))}
            </ol>
          </Card>
          <Card title="Live services" hint="Needed for a fresh check; saved reports and replays work without them">
            <ServiceDetails status={status} />
          </Card>
        </div>
      </section>
    </div>
  );
}
