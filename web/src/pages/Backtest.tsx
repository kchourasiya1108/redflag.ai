import { Info } from "lucide-react";
import { api } from "../api";
import { MetricBars } from "../components/Charts";
import { Card, Empty, ErrorBox, Skeleton, Stat } from "../components/ui";
import type { Metrics } from "../types";
import { FEATURE_NAMES, date, pct, useAsync } from "../lib";
import { hitRates } from "./Home";

export function BacktestPage() {
  const { data, error, loading } = useAsync(() => api.backtest(), []);
  if (loading) return <div className="container stack"><Skeleton h={34} w={300} /><Skeleton h={300} /></div>;
  if (error) return <div className="container"><ErrorBox error={error} /></div>;
  const r = data?.results;
  if (!r) return <div className="container"><Empty title="No track record yet">The ratings have not been tested yet.</Empty></div>;
  const llm = data?.llm_baseline as { metrics?: Metrics | null; scorer_on_same_ipos?: Metrics | null; rated?: number; test_ipos?: number; caveat?: string } | null;
  const m = r.model;
  const rates = hitRates(m);
  const caught = Math.round(m.recall_at_high * m.adverse);
  const fine = m.n - m.adverse;
  const llmRates = llm?.metrics && hitRates(llm.metrics);

  const rows: { label: string; m: Metrics; highlight?: boolean; note?: string }[] = [
    { label: "RedFlag", m: r.model, highlight: true, note: r.version },
    { label: "Lead manager's record alone", m: r.baseline_banker_model_v0 },
    { label: "Simple lead-manager rule", m: r.baseline_banker_rule, note: "a fixed cut-off on the banker's crash rate" },
  ];
  if (llm?.metrics) rows.push({ label: "Asking an AI model directly", m: llm.metrics, note: `${llm.rated}/${llm.test_ipos} test IPOs rated` });

  return (
    <div className="container stack-xl">
      <div className="stack-sm">
        <div className="eyebrow">Track record</div>
        <h1>How reliable are the ratings?</h1>
        <p className="muted" style={{ maxWidth: 720 }}>
          We built the rating on SME IPOs from {date(r.split.train_from)} to {date(r.split.train_to)}, then tested it on {r.split.test} later
          IPOs ({date(r.split.test_from)} to {date(r.split.test_to)}) that it had never seen, using only what was known on each bid date.
          Here is how it did.
        </p>
      </div>

      <div className="grid-3">
        <Stat label="When we said High risk" value={`${pct(rates.highHit, 0)} went badly`}
          sub={`${caught} of the ${rates.flagged} IPOs rated High risk. Across all test IPOs, ${pct(rates.base, 0)} went badly.`} />
        <Stat label="Bad IPOs we warned about" value={`${caught} of ${m.adverse}`}
          sub={`${pct(m.recall_at_high, 0)} of the IPOs that went badly were rated High risk in advance`} />
        <Stat label="False alarms" value={pct(m.false_alarm_rate, 0)}
          sub={`of the ${fine} IPOs that did fine were rated High risk`} />
      </div>

      <Card title="Out of 100 IPOs rated High risk, how many went badly?" hint="Test set: IPOs the model never saw">
        <MetricBars max={1} format={(v) => `${Math.round(v * 100)} in 100`}
          rows={[
            { label: "Rated High risk by RedFlag", value: rates.highHit, highlight: true },
            ...(llmRates ? [{ label: "Rated high risk by AI asked directly", value: llmRates.highHit }] : []),
            { label: "Any SME IPO (no rating)", value: rates.base },
          ]} />
      </Card>

      <div className="callout">
        <Info size={18} />
        <div className="small">
          <b>What this means for you.</b> A High risk rating means roughly {rates.base ? (rates.highHit / rates.base).toFixed(1) : "–"}× the usual chance of a bad outcome, not a certainty:
          some High risk IPOs did fine, and some IPOs we rated lower went badly. Use the rating to decide where to look harder,
          and read the concerns and their sources.
        </div>
      </div>

      {llm?.metrics && (
        <Card title="Why not just ask an AI chatbot?">
          <p className="small muted">
            We asked NVIDIA's Nemotron model to rate the same {llm.rated} test IPOs from the same facts in a single prompt. It rated
            {" "}{pct(llm.metrics.false_alarm_rate, 0)} of the IPOs that did fine as high risk, and overall it separated good IPOs from bad
            ones no better than a coin flip. RedFlag uses AI to read the prospectus and argue each concern, but the rating itself comes from
            a model tested on past outcomes. {llm.caveat}
          </p>
        </Card>
      )}

      <details className="disclosure">
        <summary>For analysts: full metrics</summary>
        <div className="stack" style={{ marginTop: 14 }}>
          <div className="grid-2">
            <Card title="AUC on the test set" hint="Chance a random IPO that went badly is rated riskier than a random one that did fine; 0.5 is a coin flip">
              <MetricBars rows={rows.map((x) => ({ label: x.label, value: x.m.auc, highlight: x.highlight, note: x.note }))}
                reference={{ value: 0.5, label: "0.5, a coin flip" }} />
            </Card>
            <Card title="Caught at HIGH vs false alarms">
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Model</th><th className="num">Caught</th><th className="num">False alarms</th><th className="num">Brier</th></tr></thead>
                  <tbody>
                    {rows.map((x) => (
                      <tr key={x.label}>
                        <td style={x.highlight ? { fontWeight: 600 } : undefined}>{x.label}<div className="tiny faint">{x.note}</div></td>
                        <td className="num mono">{pct(x.m.recall_at_high, 0)}</td>
                        <td className="num mono">{pct(x.m.false_alarm_rate, 0)}</td>
                        <td className="num mono">{x.m.brier.toFixed(3)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          </div>
          <div className="grid-2">
            <Card title="Model selection" hint={`Validation AUC on ${r.selection.validation.n} IPOs from ${date(r.selection.validation.from)}; the best feature set was kept`}>
              <MetricBars rows={Object.entries(r.selection.validation_auc).map(([k, v]) => ({ label: k, value: v, highlight: k === r.version }))}
                reference={{ value: 0.5, label: "0.5, a coin flip" }} />
            </Card>
            <Card title="How often each factor was known" hint="Share of test IPOs where the factor could be computed from cross-checked figures">
              <MetricBars format={(v) => pct(v, 0)}
                rows={Object.entries(r.coverage_test).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ label: FEATURE_NAMES[k] ?? k, value: v, highlight: true }))} />
            </Card>
          </div>
          <Card title="Rating bands">
            <div className="small muted">
              Low risk below {pct(data?.scorer?.bands.low_below)} (the share of training IPOs that went badly), High risk from {pct(data?.scorer?.bands.high_at)} (twice that),
              Elevated in between. Brier score on the test set: {m.brier.toFixed(3)} (lower is better calibrated).
            </div>
          </Card>
        </div>
      </details>
    </div>
  );
}
