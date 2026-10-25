import { useCallback, useEffect, useRef, useState, type ReactNode, type MouseEvent } from "react";

// -- routing: history API, the server returns index.html for every path ------

const listeners = new Set<() => void>();

export function navigate(to: string) {
  if (to === location.pathname + location.search) return;
  history.pushState(null, "", to);
  listeners.forEach((l) => l());
  window.scrollTo({ top: 0 });
}

export function useLocation(): string {
  const [path, setPath] = useState(location.pathname);
  useEffect(() => {
    const update = () => setPath(location.pathname);
    listeners.add(update);
    window.addEventListener("popstate", update);
    return () => {
      listeners.delete(update);
      window.removeEventListener("popstate", update);
    };
  }, []);
  return path;
}

export function Link({ to, children, className, title }: {
  to: string; children: ReactNode; className?: string; title?: string;
}) {
  const onClick = (e: MouseEvent) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    navigate(to);
  };
  return <a href={to} onClick={onClick} className={className} title={title}>{children}</a>;
}

// -- data loading -------------------------------------------------------------

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [state, setState] = useState<{ data?: T; error?: Error; loading: boolean }>({ loading: true });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setState((s) => ({ data: s.data, loading: true }));
    fn().then(
      (data) => live && setState({ data, loading: false }),
      (error) => live && setState({ error, loading: false }),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}

export function useInterval(fn: () => void, ms: number | null) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (ms === null) return;
    const id = setInterval(() => saved.current(), ms);
    return () => clearInterval(id);
  }, [ms]);
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

// -- formatting ---------------------------------------------------------------

export const pct = (x: number | null | undefined, digits = 1) =>
  x === null || x === undefined ? "–" : `${(x * 100).toFixed(digits)}%`;

export const num = (x: number | null | undefined, digits = 2) =>
  x === null || x === undefined ? "–" : x.toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits });

export const rupees = (x: number | string | null | undefined) =>
  x === null || x === undefined || x === "" ? "–" : `₹${Number(x).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

export function date(d: string | null | undefined): string {
  if (!d) return "–";
  const t = new Date(d.length === 10 ? d + "T00:00:00" : d);
  return isNaN(+t) ? d : t.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

export function ago(epochSeconds: number | undefined): string {
  if (!epochSeconds) return "";
  const s = Date.now() / 1000 - epochSeconds;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export const seconds = (s: number | undefined) =>
  s === undefined ? "–" : s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;

export function cx(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

// -- domain vocabulary ---------------------------------------------------------

export const FLAGS: Record<string, { title: string; what: string }> = {
  F1: { title: "Paper profits", what: "Profit that does not turn into operating cash" },
  F2: { title: "Profit spike before the IPO", what: "Last year's profit far above the two before" },
  F3: { title: "Eligibility gaming", what: "EBITDA parked just above the ₹1 cr SME floor" },
  F4: { title: "Cheap insider shares", what: "Shares allotted far below the IPO price in the 18 months before" },
  F5: { title: "Customer concentration", what: "Revenue depends on a handful of customers" },
  F6: { title: "Related-party trade", what: "Sales and purchases with related parties as a share of revenue" },
  F7: { title: "Auditor risk", what: "Auditor's past SME IPOs and recent changes" },
  F8: { title: "Overvalued vs peers", what: "Issue P/E against the listed peers the RHP names" },
  F9: { title: "Vague use of money", what: "General corporate purposes against the cap" },
  F10: { title: "Banker track record", what: "How the lead manager's earlier SME IPOs did" },
  F11: { title: "Watchlist link", what: "A named party appears in a SEBI order dated before the bid" },
  F12: { title: "Drift between drafts", what: "Changes between DRHP and RHP" },
  F13: { title: "Repeat anchors / market maker", what: "The same anchor investors across issues" },
  F14: { title: "Credit rating actions", what: "Downgrades before the issue" },
};

export const FEATURE_NAMES: Record<string, string> = {
  f_banker_crash: "Banker's past crash rate",
  f_banker_low_history: "Banker has a thin history",
  f_banker_median_ret: "Banker's median 6-month return",
  f_watchlist_hit: "Named in a SEBI order",
  f_watchlist_hop1: "Linked to a SEBI order via one party",
  f_cfo_pat: "Operating cash / profit",
  f_cfo_neg_pat_pos: "Negative cash, positive profit",
  f_recv_rev_gap: "Receivables outgrowing revenue",
  f_profit_spike: "Profit spike",
  f_ebitda_headroom_min: "EBITDA headroom over ₹1 cr",
  f_other_income_share: "Other income share of PBT",
  f_rev_growth: "Revenue growth",
};

export const FIELD_NAMES: Record<string, string> = {
  "pl.revenue": "Revenue from operations", "pl.other_income": "Other income", "pl.total_income": "Total income",
  "pl.total_expenses": "Total expenses", "pl.employee": "Employee benefits", "pl.finance_cost": "Finance costs",
  "pl.depreciation": "Depreciation & amortisation", "pl.pbt": "Profit before tax", "pl.tax": "Tax expense",
  "pl.pat": "Profit after tax",
  "bs.share_capital": "Share capital", "bs.reserves": "Reserves & surplus", "bs.equity": "Total equity",
  "bs.lt_borrowings": "Long-term borrowings", "bs.st_borrowings": "Short-term borrowings",
  "bs.trade_payables": "Trade payables", "bs.inventories": "Inventories", "bs.receivables": "Trade receivables",
  "bs.cash": "Cash & bank", "bs.total_assets": "Total assets", "bs.total_eq_liab": "Total equity & liabilities",
  "bs.nc_liabilities": "Non-current liabilities", "bs.c_liabilities": "Current liabilities",
  "bs.total_liabilities": "Total liabilities",
  "cf.cfo": "Cash from operations", "cf.cfi": "Cash from investing", "cf.cff": "Cash from financing",
  "cf.net_change": "Net change in cash", "cf.opening_cash": "Opening cash", "cf.closing_cash": "Closing cash",
};

export const STATEMENTS: Record<string, string> = {
  pl: "Profit & loss", bs: "Assets & liabilities", cf: "Cash flows",
};
