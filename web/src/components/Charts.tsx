import { useState, type MouseEvent, type ReactNode } from "react";
import type { CaseFile, Explain, TraceEvent } from "../types";
import { FEATURE_NAMES, num, odds, pct } from "../lib";

// -- hover tooltip ------------------------------------------------------------

export function useTooltip() {
  const [tip, setTip] = useState<{ x: number; y: number; body: ReactNode } | null>(null);
  const bind = (body: ReactNode) => ({
    onMouseMove: (e: MouseEvent) => setTip({ x: e.clientX + 14, y: e.clientY + 14, body }),
    onMouseLeave: () => setTip(null),
  });
  const node = tip && (
    <div className="tooltip" style={{ left: Math.min(tip.x, window.innerWidth - 300), top: tip.y }}>{tip.body}</div>
  );
  return { bind, node };
}

// -- probability on the model's bands -----------------------------------------

export function ProbabilityScale({ p, bands }: { p: number | null; bands: { low_below: number; high_at: number } }) {
  const max = Math.max(0.6, Math.ceil(((p ?? 0) + 0.05) * 10) / 10);
  const at = (x: number) => `${(Math.min(x, max) / max) * 100}%`;
  const ticks = [...Array(Math.round(max * 10) + 1)].map((_, i) => i / 10);
  return (
    <div className="scale" aria-label={`Probability of an adverse outcome ${pct(p)}`}>
      {p !== null && (
        <div className="marker" style={{ left: at(p) }}>
          <span className="lbl">{pct(p)}</span>
          <span className="stem" />
        </div>
      )}
      <div className="track">
        <div className="seg low" style={{ width: at(bands.low_below) }} title={`LOW below ${pct(bands.low_below)}`} />
        <div className="seg elev" style={{ width: `calc(${at(bands.high_at)} - ${at(bands.low_below)})` }} title="ELEVATED" />
        <div className="seg high" style={{ flex: 1 }} title={`HIGH from ${pct(bands.high_at)}`} />
      </div>
      <div className="ticks">
        {ticks.map((t) => <span key={t} style={{ left: at(t) }}>{Math.round(t * 100)}%</span>)}
      </div>
      <div className="bands">
        <span>LOW &lt; {pct(bands.low_below)}</span>
        <span>ELEVATED</span>
        <span>HIGH ≥ {pct(bands.high_at)}</span>
      </div>
    </div>
  );
}

// -- why this probability: each feature's push on the log-odds ----------------

export function Contributions({ explain, baseRate }: { explain: Explain; baseRate: number }) {
  const { bind, node } = useTooltip();
  const rows = [...explain.features].sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));
  const extent = Math.max(0.25, ...rows.map((r) => Math.abs(r.contribution)));
  const known = rows.filter((r) => !r.missing && Math.abs(r.contribution) > 0.0005);
  const top = known[0];
  return (
    <div className="stack">
      <p className="small muted">
        We start from a typical SME IPO ({odds(baseRate)} went badly) and each factor below pushes this IPO's chance up or down,
        ending at <b>{odds(explain.p_adverse)}</b>.
        {top && <> The biggest single factor: <b>{FEATURE_NAMES[top.feature] ?? top.feature}</b>, which {top.contribution > 0 ? "raised" : "lowered"} the risk.</>}
      </p>
      <div className="legend">
        <span><i className="sw" style={{ background: "var(--raise)" }} />Raised the risk</span>
        <span><i className="sw" style={{ background: "var(--lower)" }} />Lowered the risk</span>
        <span className="faint">Grey name: not known for this IPO, so a typical value was used</span>
      </div>
      <div className="bars" role="table" aria-label="What moved the rating">
        {rows.map((r) => {
          const w = (Math.abs(r.contribution) / extent) * 50;
          const up = r.contribution >= 0;
          const moved = Math.abs(r.contribution) > 0.0005;
          return (
            <div className="bar-row" key={r.feature} role="row"
              {...bind(<>
                <b>{FEATURE_NAMES[r.feature] ?? r.feature}</b><br />
                {r.missing ? "Not known for this IPO" : `Value for this IPO: ${num(r.value, 2)}`}<br />
                {moved ? (up ? "Raised the risk" : "Lowered the risk") : "Little effect"}
              </>)}>
              <span className="lab" role="cell" style={r.missing ? { color: "var(--text-3)" } : undefined}>
                {FEATURE_NAMES[r.feature] ?? r.feature}
              </span>
              <div className="bar-track" role="cell">
                <div className="axis" />
                {moved && (
                  <div className={`fill ${up ? "up" : "down"}`}
                    style={up ? { left: "50%", width: `${w}%` } : { right: "50%", width: `${w}%` }} />
                )}
              </div>
              <span className="val small" role="cell" style={{ fontFamily: "var(--font)", color: "var(--text-2)" }}>{moved ? (up ? "raised" : "lowered") : "–"}</span>
            </div>
          );
        })}
      </div>
      {node}
    </div>
  );
}

// -- the rating as odds out of 100 --------------------------------------------

/** 100 dots, one per IPO "like this one"; the filled ones went badly. Colour follows the rating and is never alone. */
export function OddsGrid({ p, rating }: { p: number | null; rating: string }) {
  const filled = p === null ? 0 : Math.round(p * 100);
  const tone = rating === "HIGH" ? "var(--high)" : rating === "ELEVATED" ? "var(--elev)" : rating === "LOW" ? "var(--low)" : "var(--text-3)";
  return (
    <div className="odds-grid" role="img" aria-label={`${odds(p)} IPOs like this one went badly`}>
      {[...Array(100)].map((_, i) => (
        <i key={i} style={i < filled ? { background: tone, borderColor: tone } : undefined} />
      ))}
    </div>
  );
}

// -- profit against the cash it brought in ------------------------------------

export function CashProfitChart({ years }: { years: { label: string; pat: number | null; cfo: number | null }[] }) {
  const { bind, node } = useTooltip();
  const vals = years.flatMap((y) => [y.pat, y.cfo]).filter((v): v is number => v !== null);
  if (!vals.length) return null;
  const max = Math.max(0, ...vals), min = Math.min(0, ...vals);
  const span = max - min || 1;
  const H = 180;
  const y0 = (max / span) * H; // the zero line, from the top
  const bar = (v: number | null) => (v === null ? null : { top: v >= 0 ? y0 - (v / span) * H : y0, h: Math.max(2, (Math.abs(v) / span) * H) });
  return (
    <div className="stack-sm">
      <div className="legend">
        <span><i className="sw" style={{ background: "var(--series-1)" }} />Profit after tax</span>
        <span><i className="sw" style={{ background: "var(--series-2)" }} />Cash from operations</span>
      </div>
      <div className="cp-chart" style={{ height: H + 26 }}>
        <div className="zero" style={{ top: y0 }} />
        {years.map((y) => {
          const a = bar(y.pat), b = bar(y.cfo);
          return (
            <div className="grp" key={y.label} {...bind(<><b>{y.label}</b><br />Profit after tax: {y.pat === null ? "–" : `₹${num(y.pat)} cr`}<br />Cash from operations: {y.cfo === null ? "–" : `₹${num(y.cfo)} cr`}</>)}>
              <div className="pair" style={{ height: H }}>
                {a && <div className={`b s1${y.pat! < 0 ? " neg" : ""}`} style={{ top: a.top, height: a.h }} />}
                {b && <div className={`b s2${y.cfo! < 0 ? " neg" : ""}`} style={{ top: b.top, height: b.h }} />}
              </div>
              <div className="x">{y.label}</div>
            </div>
          );
        })}
      </div>
      {node}
    </div>
  );
}

// -- the parties and the orders they are named in ------------------------------

const ROLE: Record<string, string> = {
  banker: "Lead manager", auditor: "Auditor", registrar: "Registrar", market_maker: "Market maker",
  promoter: "Promoter", director: "Director", sebi_order: "SEBI order",
};

export function PartyGraph({ cf, company }: { cf: CaseFile; company: string }) {
  const { bind, node } = useTooltip();
  const parties = cf.entities.filter((e) => e.type !== "sebi_order");
  const orders = cf.entities.filter((e) => e.type === "sebi_order");
  const W = 760, rowH = 64;
  const H = Math.max(parties.length, orders.length, 3) * rowH + 40;
  const pos: Record<string, [number, number]> = {};
  const col = (items: typeof parties, x: number) =>
    items.forEach((e, i) => (pos[e.id] = [x, 20 + (H - 40) * ((i + 0.5) / items.length)]));
  pos["company"] = [110, H / 2];
  col(parties, 380);
  col(orders, 640);
  const short = (s: string, n = 34) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  return (
    <div className="graph">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Parties to the issue and SEBI orders they are named in">
        {cf.edges.map((e, i) => {
          const a = pos[e.src];
          const b = e.dst.startsWith("company:") ? pos["company"] : pos[e.dst];
          if (!a || !b) return null;
          const alert = e.type === "named_in_order";
          const [x1, y1, x2, y2] = alert ? [a[0] + 8, a[1], b[0] - 8, b[1]] : [b[0] + 8, b[1], a[0] - 8, a[1]];
          const mx = (x1 + x2) / 2;
          return (
            <g key={i}>
              <path className={`edge${alert ? " alert" : ""}`} fill="none" d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} />
            </g>
          );
        })}
        <g>
          <circle cx={pos.company[0]} cy={pos.company[1]} r={10} fill="var(--ink)" />
          <text className="node-label" x={pos.company[0]} y={pos.company[1] + 28} textAnchor="middle" style={{ fontWeight: 650 }}>{short(company, 26)}</text>
          <text className="node-sub" x={pos.company[0]} y={pos.company[1] + 43} textAnchor="middle">Issuer</text>
        </g>
        {parties.map((e) => {
          const [x, y] = pos[e.id];
          const linked = cf.edges.some((d) => d.src === e.id && d.type === "named_in_order");
          return (
            <g key={e.id} {...bind(<><b>{e.name}</b><br />{ROLE[e.type] ?? e.type} · from {e.source}</>)} style={{ cursor: "default" }}>
              <rect x={x - 22} y={y - 22} width={44} height={44} fill="transparent" />
              <circle cx={x} cy={y} r={8} fill={linked ? "var(--high)" : "var(--surface)"} stroke={linked ? "var(--surface)" : "var(--text-2)"} strokeWidth={2} />
              <text className="node-label" x={x + 16} y={y - 2}>{short(e.name)}</text>
              <text className="node-sub" x={x + 16} y={y + 13}>{ROLE[e.type] ?? e.type}</text>
            </g>
          );
        })}
        {orders.map((e) => {
          const [x, y] = pos[e.id];
          return (
            <g key={e.id} {...bind(<><b>{e.name}</b><br />{e.source}</>)}>
              <rect x={x - 7} y={y - 7} width={14} height={14} rx={3} fill="var(--high)" />
              <text className="node-label" x={x + 14} y={y - 2}>{short(e.source.replace("sebi:", ""), 16)}</text>
              <text className="node-sub" x={x + 14} y={y + 13}>SEBI order</text>
            </g>
          );
        })}
      </svg>
      {node}
    </div>
  );
}

// -- where the run's time went --------------------------------------------------

export function AgentTimeline({ trace }: { trace: TraceEvent[] }) {
  const { bind, node } = useTooltip();
  if (trace.length < 2) return null;
  const t = (e: TraceEvent) => new Date(e.timestamp).getTime() / 1000;
  const t0 = t(trace[0]);
  const total = Math.max(1, t(trace[trace.length - 1]) - t0);
  // Each agent's step ends at its own trace event and lasts elapsed_ms.
  const steps = trace
    .filter((e) => typeof e.detail?.elapsed_ms === "number" && !["Orchestrator", "DataPort", "ModelGateway", "HMEM"].includes(e.agent_name))
    .map((e) => {
      const dur = (e.detail!.elapsed_ms as number) / 1000;
      const end = t(e) - t0;
      return { agent: e.agent_name, action: e.action, start: Math.max(0, end - dur), dur, summary: e.output_summary };
    });
  return (
    <div className="stack-sm">
      {steps.map((s, i) => (
        <div className="timeline-row" key={i} {...bind(<><b>{s.agent}</b> · {s.action}<br />{s.summary}</>)}>
          <span className="small" style={{ fontWeight: 550 }}>{s.agent}</span>
          <div className="lane">
            <div className="seg" style={{ left: `${(s.start / total) * 100}%`, width: `${Math.max(0.4, (s.dur / total) * 100)}%` }} />
          </div>
          <span className="small mono faint" style={{ textAlign: "right" }}>{s.dur < 1 ? `${Math.round(s.dur * 1000)} ms` : `${s.dur.toFixed(1)} s`}</span>
        </div>
      ))}
      <p className="small faint">Trace timestamps are to the second, so very short steps show as a sliver at their end time.</p>
      {node}
    </div>
  );
}

// -- simple horizontal metric bars (backtest) ----------------------------------

export function MetricBars({ rows, max = 1, format = (v: number) => v.toFixed(3), reference }: {
  rows: { label: string; value: number; highlight?: boolean; note?: string }[];
  max?: number; format?: (v: number) => string; reference?: { value: number; label: string };
}) {
  const { bind, node } = useTooltip();
  return (
    <div className="bars">
      {rows.map((r) => (
        <div className="bar-row" key={r.label} {...bind(<><b>{r.label}</b><br />{format(r.value)}{r.note ? <><br />{r.note}</> : null}</>)}>
          <span className="lab" style={r.highlight ? { color: "var(--text)", fontWeight: 600 } : undefined}>{r.label}</span>
          <div className="bar-track">
            {reference && (
              <div className="axis" style={{ left: `${(reference.value / max) * 100}%`, background: "var(--text-3)" }} title={reference.label} />
            )}
            <div className={`fill ${r.highlight ? "plain" : "muted"}`} style={{ width: `${Math.max(0.5, (r.value / max) * 100)}%` }} />
          </div>
          <span className="val">{format(r.value)}</span>
        </div>
      ))}
      {reference && <div className="small faint">Vertical line: {reference.label}</div>}
      {node}
    </div>
  );
}
