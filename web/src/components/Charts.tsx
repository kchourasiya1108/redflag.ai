import { useState, type MouseEvent, type ReactNode } from "react";
import type { CaseFile, Explain, TraceEvent } from "../types";
import { FEATURE_NAMES, crore, num, odds, pct } from "../lib";

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
        <span>Low risk</span>
        <span>Elevated</span>
        <span>High risk</span>
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
            <div className="grp" key={y.label} {...bind(<><b>{y.label}</b><br />Profit after tax: {y.pat === null ? "–" : crore(y.pat)}<br />Cash from operations: {y.cfo === null ? "–" : crore(y.cfo)}</>)}>
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

// -- one measure over the years (small multiples share this) ------------------

/** Columns from a zero line; only the latest year is labelled, the rest are on hover. */
export function ColumnBars({ rows, unit = "₹ cr", height = 120 }: {
  rows: { label: string; value: number | null }[]; unit?: string; height?: number;
}) {
  const { bind, node } = useTooltip();
  const vals = rows.map((r) => r.value).filter((v): v is number => v !== null);
  if (!vals.length) return null;
  const max = Math.max(0, ...vals), min = Math.min(0, ...vals), span = max - min || 1;
  const y0 = (max / span) * height;
  const last = rows.length - 1;
  const fmt = (v: number) => (unit === "₹ cr" ? crore(v) : `${num(v)}${unit}`);
  return (
    <div className="col-chart" style={{ height: height + 40 }}>
      <div className="zero" style={{ top: y0 + 18 }} />
      {rows.map((r, i) => {
        const h = r.value === null ? 0 : Math.max(2, (Math.abs(r.value) / span) * height);
        const top = r.value === null ? y0 : r.value >= 0 ? y0 - h : y0;
        return (
          <div className="col" key={r.label} {...bind(<><b>{r.label}</b><br />{r.value === null ? "Not read" : fmt(r.value)}</>)}>
            <div className="plot" style={{ height }}>
              {i === last && r.value !== null && <span className="v" style={{ top: r.value >= 0 ? top - 18 : top + h + 2 }}>{fmt(r.value)}</span>}
              {r.value !== null && <div className={`b${r.value < 0 ? " neg" : ""}`} style={{ top, height: h }} />}
            </div>
            <div className="x">{r.label}</div>
          </div>
        );
      })}
      {node}
    </div>
  );
}

// -- top customers and suppliers (F5) -------------------------------------------

export function ConcentrationChart({ value }: { value: Record<string, number | null> }) {
  const { bind, node } = useTooltip();
  const tiers = [1, 3, 5, 10].filter((n) => value[`top${n}_customers_pct`] != null || value[`top${n}_suppliers_pct`] != null);
  if (!tiers.length) return null;
  const hasS = tiers.some((n) => value[`top${n}_suppliers_pct`] != null);
  const bar = (v: number | null | undefined, cls: string) =>
    v == null ? <div className="pbar"><span className="faint tiny">not given</span></div>
      : <div className="pbar"><div className={`f ${cls}`} style={{ width: `${Math.min(100, v)}%` }} /><span className="v">{num(v, 0)}%</span></div>;
  return (
    <div className="stack-sm">
      <div className="legend">
        <span><i className="sw" style={{ background: "var(--series-1)" }} />Share of sales from top customers</span>
        {hasS && <span><i className="sw" style={{ background: "var(--series-2)" }} />Share of purchases from top suppliers</span>}
      </div>
      <div className="pairs">
        {tiers.map((n) => {
          const c = value[`top${n}_customers_pct`], s = value[`top${n}_suppliers_pct`];
          return (
            <div className="pair-row" key={n} {...bind(<><b>Top {n === 1 ? "customer / supplier" : `${n}`}</b><br />
              Customers: {c == null ? "not given" : `${num(c, 1)}% of sales`}<br />
              {hasS && <>Suppliers: {s == null ? "not given" : `${num(s, 1)}% of purchases`}</>}</>)}>
              <span className="lab">{n === 1 ? "Largest" : `Top ${n}`}</span>
              <div className="stack-xs">{bar(c, "s1")}{hasS && bar(s, "s2")}</div>
            </div>
          );
        })}
      </div>
      {node}
    </div>
  );
}

// -- issue price against listed peers (F8) -----------------------------------------

export function PeerPeChart({ value }: { value: { issue_pe?: number | null; peer_median_pe?: number | null; peers?: { name: string; pe: number | null }[] } }) {
  const { bind, node } = useTooltip();
  const peers = (value.peers ?? []).filter((p) => p.pe != null && p.pe > 0);
  if (value.issue_pe == null || !peers.length) return null;
  const rows = [{ name: "This IPO", pe: value.issue_pe, me: true }, ...peers.map((p) => ({ name: p.name, pe: p.pe as number, me: false }))];
  const max = Math.max(...rows.map((r) => r.pe)) * 1.1;
  const med = value.peer_median_pe;
  return (
    <div className="stack-sm">
      <div className="small muted">Price-to-earnings: how many years of current profit the price pays for. Higher means more expensive.</div>
      <div className="bars">
        {rows.map((r) => (
          <div className="bar-row compact" key={r.name} {...bind(<><b>{r.name}</b><br />P/E {num(r.pe, 1)}{med && !r.me ? "" : med ? <><br />{num(r.pe / med, 1)}× the peers' median</> : null}</>)}>
            <span className="lab" style={r.me ? { color: "var(--text)", fontWeight: 600 } : undefined}>{r.name}</span>
            <div className="bar-track">
              {med != null && <div className="axis ref" style={{ left: `${(med / max) * 100}%` }} />}
              <div className={`fill ${r.me ? "plain" : "muted"}`} style={{ width: `${(r.pe / max) * 100}%` }} />
            </div>
            <span className="val">{num(r.pe, 1)}</span>
          </div>
        ))}
      </div>
      {med != null && <div className="small faint">Dashed line: the peers' median P/E ({num(med, 1)}).</div>}
      {node}
    </div>
  );
}

// -- the lead manager's earlier IPOs (F10) ----------------------------------------

export function TrackRecordChart({ ipos }: { ipos: { company: string; listing_date: string; ret_6m: number | null; adverse: number }[] }) {
  const { bind, node } = useTooltip();
  const rows = ipos.filter((i) => i.ret_6m !== null);
  if (!rows.length) return null;
  const H = 150;
  const max = Math.max(0.1, ...rows.map((r) => r.ret_6m!)), min = Math.min(-0.1, ...rows.map((r) => r.ret_6m!));
  const span = max - min, y0 = (max / span) * H;
  return (
    <div className="stack-sm">
      <div className="legend">
        <span><i className="sw" style={{ background: "var(--raise)" }} />Went badly</span>
        <span><i className="sw" style={{ background: "var(--line-strong)" }} />Did not</span>
        <span className="faint">Each bar is one earlier IPO: its price change six months after listing</span>
      </div>
      <div className="tr-chart" style={{ height: H }}>
        <div className="zero" style={{ top: y0 }}><span>0%</span></div>
        {rows.map((r, i) => {
          const h = Math.max(2, (Math.abs(r.ret_6m!) / span) * H);
          return (
            <div className="slot" key={i} {...bind(<><b>{r.company}</b><br />Listed {r.listing_date}<br />
              {r.ret_6m! >= 0 ? "Up" : "Down"} {pct(Math.abs(r.ret_6m!), 0)} after six months{r.adverse ? <><br />Counted as going badly</> : null}</>)}>
              <div className={`b${r.ret_6m! < 0 ? " neg" : ""}${r.adverse ? " bad" : ""}`} style={{ top: r.ret_6m! >= 0 ? y0 - h : y0, height: h }} />
            </div>
          );
        })}
      </div>
      <div className="between tiny faint"><span>{rows[0].listing_date.slice(0, 7)}</span><span>{rows[rows.length - 1].listing_date.slice(0, 7)}</span></div>
      {node}
    </div>
  );
}

// -- did the odds hold up? (backtest) ---------------------------------------------

export function ReliabilityChart({ bins }: { bins: { n: number; p_mean: number; observed: number; p_from: number; p_to: number }[] }) {
  const { bind, node } = useTooltip();
  const H = 160, max = Math.max(0.5, ...bins.flatMap((b) => [b.p_mean, b.observed])) * 1.1;
  return (
    <div className="stack-sm">
      <div className="legend">
        <span><i className="sw" style={{ background: "var(--series-1)" }} />Chance we gave</span>
        <span><i className="sw" style={{ background: "var(--series-2)" }} />Share that actually went badly</span>
      </div>
      <div className="cp-chart" style={{ height: H + 40 }}>
        <div className="zero" style={{ top: H }} />
        {bins.map((b, i) => (
          <div className="grp" key={i} {...bind(<><b>{b.n} IPOs we gave {pct(b.p_from, 0)}–{pct(b.p_to, 0)}</b><br />
            Average chance we gave: {pct(b.p_mean, 0)}<br />Actually went badly: {pct(b.observed, 0)}</>)}>
            <div className="pair" style={{ height: H }}>
              <div className="b s1" style={{ top: H - (b.p_mean / max) * H, height: (b.p_mean / max) * H }} />
              <div className="b s2" style={{ top: H - (b.observed / max) * H, height: (b.observed / max) * H }} />
            </div>
            <div className="x">{pct(b.p_from, 0)}–{pct(b.p_to, 0)}</div>
          </div>
        ))}
      </div>
      {node}
    </div>
  );
}
