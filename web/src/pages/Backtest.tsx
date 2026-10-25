import { api } from "../api";
import { MetricBars } from "../components/Charts";
import { Card, Empty, ErrorBox, Skeleton, Stat } from "../components/ui";
import type { Metrics } from "../types";
import { FEATURE_NAMES, date, pct, useAsync } from "../lib";

export function BacktestPage() {
  const { data, error, loading } = useAsync(() => api.backtest(), []);
  if (loading) return <div className="container stack"><Skeleton h={34} w={300} /><Skeleton h={300} /></div>;
  if (error) return <div className="container"><ErrorBox error={error} /></div>;
  const r = data?.results;
  if (!r) return <div className="container"><Empty title="No backtest yet">Run: python run.py features train</Empty></div>;
  const llm = data?.llm_baseline as { metrics?: Metrics | null; scorer_on_same_ipos?: Metrics | null; rated?: number; test_ipos?: number; caveat?: string } | null;

  const rows: { label: string; m: Metrics; highlight?: boolean; note?: string }[] = [
    { label: "RedFlag model", m: r.model, highlight: true, note: r.version },
    { label: "Banker-only model", m: r.baseline_banker_model_v0, note: "the lead manager's record alone" },
    { label: "Banker rule", m: r.baseline_banker_rule, note: "a fixed threshold on the banker's crash rate" },
  ];
  if (llm?.metrics) rows.push({ label: "Single-prompt Nemotron", m: llm.metrics, note: `${llm.rated}/${llm.test_ipos} test IPOs rated` });

  return (
    <div className="container stack-lg">
      <div className="stack-sm">
        <div className="eyebrow">Backtest</div>
        <h1>How well does the rating separate bad IPOs from the rest?</h1>
        <p className="muted" style={{ maxWidth: 720 }}>
          Trained on SME IPOs that bid from {date(r.split.train_from)} to {date(r.split.train_to)}, then tested on {r.split.test} later
          IPOs ({date(r.split.test_from)} to {date(r.split.test_to)}) it never saw. Every feature is computed as of each IPO's bid date,
          through the same Data Port and Leakage Guard as a live run.
        </p>
      </div>

      <div className="grid-4">
        <Stat label="Test AUC" value={r.model.auc.toFixed(3)} sub={`banker-only ${r.baseline_banker_model_v0.auc.toFixed(3)} · 0.5 is chance`} />
        <Stat label="Caught at HIGH" value={pct(r.model.recall_at_high, 0)} sub={`of ${r.model.adverse} adverse IPOs in the test set`} />
        <Stat label="False alarms at HIGH" value={pct(r.model.false_alarm_rate, 0)} sub={`of ${r.model.n - r.model.adverse} IPOs that did fine`} />
        <Stat label="Brier score" value={r.model.brier.toFixed(3)} sub="lower is better calibrated" />
      </div>

      <div className="grid-2">
        <Card title="AUC on the test set" hint="Probability a random adverse IPO is rated riskier than a random fine one">
          <MetricBars rows={rows.map((x) => ({ label: x.label, value: x.m.auc, highlight: x.highlight, note: x.note }))}
            reference={{ value: 0.5, label: "0.5, a coin flip" }} />
        </Card>
        <Card title="Caught at HIGH vs false alarms" hint="Share of adverse IPOs rated HIGH, and share of fine ones rated HIGH">
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
          {!llm?.metrics && <p className="small faint" style={{ marginTop: 10 }}>The single-prompt Nemotron baseline appears here once <span className="mono">run.py llm-baseline</span> finishes.</p>}
          {llm?.caveat && <p className="small faint" style={{ marginTop: 10 }}>{llm.caveat}</p>}
        </Card>
      </div>

      <div className="grid-2">
        <Card title="Model selection" hint={`Validation AUC on ${r.selection.validation.n} IPOs from ${date(r.selection.validation.from)}; the best feature set was kept`}>
          <MetricBars rows={Object.entries(r.selection.validation_auc).map(([k, v]) => ({ label: k, value: v, highlight: k === r.version }))}
            reference={{ value: 0.5, label: "0.5, a coin flip" }} />
        </Card>
        <Card title="Feature coverage on the test set" hint="Share of test IPOs where the feature could be computed from verified facts">
          <MetricBars format={(v) => pct(v, 0)}
            rows={Object.entries(r.coverage_test).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ label: FEATURE_NAMES[k] ?? k, value: v, highlight: true }))} />
        </Card>
      </div>

      <Card title="Bands">
        <div className="small muted">
          LOW below {pct(data?.scorer?.bands.low_below)} (the training base rate of adverse outcomes), HIGH from {pct(data?.scorer?.bands.high_at)} (twice the base rate),
          ELEVATED in between. Adverse means a crash in the first six months, a suspension, or SEBI action. The financial features come
          from deterministic extraction for every RHP in the archive; Nemotron's narrative reading runs only in live investigations.
        </div>
      </Card>
    </div>
  );
}
