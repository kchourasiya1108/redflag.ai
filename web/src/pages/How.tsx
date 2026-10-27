import { useEffect } from "react";
import { FileSearch, Gavel, Scale, Target } from "lucide-react";
import { api } from "../api";
import { ReliabilityChart } from "../components/Charts";
import { Card, Stat } from "../components/ui";
import type { Metrics } from "../types";
import { FLAGS, pct, useAsync } from "../lib";
import { hitRates } from "./Home";

const NOT_YET = new Set<string>();

const STEPS = [
  { icon: <Gavel size={18} />, t: "Check the people", d: "Everyone named in the issue is checked against SEBI orders, and we look at how the lead manager's earlier SME IPOs did." },
  { icon: <FileSearch size={18} />, t: "Read the prospectus", d: "We read the financial statements, the share history, the use of the money raised and dealings with related parties." },
  { icon: <Scale size={18} />, t: "Argue against ourselves", d: "Every concern is challenged: if the prospectus gives an ordinary explanation, the concern is dropped or softened." },
  { icon: <Target size={18} />, t: "Rate the risk", d: "A model trained on how past SME IPOs turned out gives the chance of a bad outcome." },
];

export function HowPage({ section }: { section?: string }) {
  useEffect(() => {
    const id = section ?? location.hash.slice(1);
    if (id) document.getElementById(id)?.scrollIntoView();
  }, [section]);
  return (
    <div className="container stack-xl narrow">
      <div className="stack-sm">
        <h1>How RedFlag checks an IPO</h1>
        <p className="muted">
          A prospectus runs to hundreds of pages. We do the slow reading, judge the IPO only on what is known before its bid
          opens, and show you the page behind every finding.
        </p>
      </div>

      <div className="steps">
        {STEPS.map((s, i) => (
          <div className="step-row" key={s.t}>
            <div className="ic">{s.icon}</div>
            <div><h3>{i + 1}. {s.t}</h3><p className="small muted">{s.d}</p></div>
          </div>
        ))}
      </div>

      <section className="stack">
        <h2>What we look for</h2>
        <div className="look-for">
          {Object.entries(FLAGS).filter(([id]) => !NOT_YET.has(id)).map(([id, f]) => (
            <div key={id}><b>{f.title}</b><p className="small muted">{f.why}</p></div>
          ))}
        </div>
      </section>

      <TrackRecord />

      <section className="stack">
        <h2>Our rules</h2>
        <ul className="ticks small">
          <li>We only use information from before the bid opens.</li>
          <li>A concern is only explained away if the prospectus itself explains it.</li>
          <li>Figures that don't add up are never used in the rating.</li>
          <li>Anything we couldn't check is listed in the report.</li>
          <li>We never say buy, sell or apply. Being named in a SEBI order is not proof of wrongdoing.</li>
        </ul>
      </section>
    </div>
  );
}

function TrackRecord() {
  const { data } = useAsync(() => api.backtest(), []);
  const r = data?.results;
  if (!r) return <section id="track-record" />;
  const m = r.model, rates = hitRates(m);
  const llm = data?.llm_baseline as { metrics?: Metrics | null } | null;
  return (
    <section id="track-record" className="stack">
      <h2>How accurate is it?</h2>
      <p className="small muted">We tested the ratings on {r.split.test} past SME IPOs that the model had never seen, using only what was known on each bid date.</p>
      <div className="grid-3">
        <Stat label="When we said High risk" value={`${pct(rates.highHit, 0)} went badly`} sub={`against ${pct(rates.base, 0)} of all SME IPOs`} />
        <Stat label="Bad IPOs flagged in advance" value={pct(m.recall_at_high, 0)} sub={`${Math.round(m.recall_at_high * m.adverse)} of ${m.adverse}`} />
        <Stat label="False alarms" value={pct(m.false_alarm_rate, 0)} sub="of IPOs that did fine were rated High risk" />
      </div>
      {m.reliability && m.reliability.length > 1 && (
        <Card title="Did the chances hold up?" hint="Test IPOs in five equal groups, from the lowest chance we gave to the highest">
          <div className="stack-sm">
            <ReliabilityChart bins={m.reliability} />
            <p className="small muted">When the two bars in a group are close, the chance we gave was about right. Riskier groups did go badly more often.</p>
          </div>
        </Card>
      )}
      <Card>
        <p className="small muted">
          A High risk rating means roughly {rates.base ? (rates.highHit / rates.base).toFixed(1) : "–"}× the usual chance of a bad outcome, not a certainty.
          Use it to decide where to look harder.
          {llm?.metrics && <> For comparison, asking an AI chatbot directly did no better than a coin flip on the same IPOs.</>}
        </p>
      </Card>
    </section>
  );
}
