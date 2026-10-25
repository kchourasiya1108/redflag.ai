import { Brain, Database, Layers, Lock, Sparkles } from "lucide-react";
import { Card } from "../components/ui";
import { STAGES } from "./Run";
import { FLAGS } from "../lib";

const LLM = new Set(["Extractor", "NarrativeReader", "Skeptic", "Reporter"]);

export function HowPage() {
  return (
    <div className="container stack-lg">
      <div className="stack-sm">
        <div className="eyebrow">How it works</div>
        <h1>Thirteen agents, one case file, every claim cited.</h1>
        <p className="muted" style={{ maxWidth: 720 }}>
          A deterministic Orchestrator runs the cheap, high-signal lookups first, lets the Planner choose how deep to read,
          and sends coverage gaps back to the Planner at most twice. Numbers are always computed in code; Nemotron reads
          and argues, it never invents a figure.
        </p>
      </div>

      <div className="grid-3">
        <Card title={<span className="row"><Database size={16} /> Zetaris Lightning</span>}>
          <p className="small muted">The Data Port. Every lookup is a named SQL template over the archive, live and price sources, run through Zetaris's REST API on the Linux PC. The Leakage Guard keeps only rows dated before the IPO's bid date.</p>
        </Card>
        <Card title={<span className="row"><Brain size={16} /> Meterless Nightshift</span>}>
          <p className="small muted">The Model Gateway, on this Mac. Nemotron 3.5 Lightning maps table rows, reads narrative sections and drafts the summary; Nemotron 3 Ultra argues the Skeptic's case.</p>
        </Card>
        <Card title={<span className="row"><Layers size={16} /> Meterless engines</span>}>
          <p className="small muted">Markovian reads long RHP sections chunk by chunk with a bounded state. The World Model is the shared Case File with a replayable write log. H-MEM remembers RHP layouts across runs, as of each run's date.</p>
        </Card>
      </div>

      <Card title="The pipeline" hint="Grey steps are code; Nemotron steps are marked" pad={false}>
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

      <Card title="The checks" pad={false}>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Flag</th><th>Check</th><th>What it looks for</th></tr></thead>
            <tbody>
              {Object.entries(FLAGS).map(([id, f]) => (
                <tr key={id}>
                  <td className="mono small">{id}</td>
                  <td><b>{f.title}</b>{["F12", "F13", "F14"].includes(id) && <div className="tiny faint">not checked yet: the data is not collected</div>}</td>
                  <td className="small muted">{f.what}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title={<span className="row"><Lock size={16} /> Guard rails</span>}>
        <ul className="small muted stack-sm" style={{ margin: 0, paddingLeft: 18 }}>
          <li>The Skeptic must cite an RHP page to dismiss a flag; a ruling without evidence counts as upheld.</li>
          <li>A HIGH flag stays HIGH only when a second, independent signal supports it; otherwise it is held at ELEVATED.</li>
          <li>Unverified financial periods are shown but never scored.</li>
          <li>The written summary is published only if every number in it is in the case file.</li>
          <li>Runs aim for three minutes; at ten, the Orchestrator scores with what it has and marks the run partial.</li>
          <li>RedFlag never says apply, avoid, buy or sell; being named in a SEBI order is not a finding against the company.</li>
        </ul>
      </Card>
    </div>
  );
}
