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

export const FLAGS: Record<string, { title: string; what: string; why: string; ask?: string }> = {
  F1: {
    title: "Profits not backed by cash", what: "Profit that does not turn into operating cash",
    why: "Profit that never arrives as cash can be an accounting choice rather than real earnings.",
    ask: "Why is operating cash flow so far below reported profit, and when will receivables be collected?",
  },
  F2: {
    title: "Profit jump just before the IPO", what: "Last year's profit far above the two before",
    why: "A sudden rise in profit right before listing can be timed to justify a higher issue price.",
    ask: "What drove the jump in profit in the last year, and is it likely to repeat?",
  },
  F3: {
    title: "Profit just above the listing minimum", what: "Operating profit parked just above the ₹1 crore SME floor",
    why: "SME listing needs ₹1 crore of operating profit; figures sitting just above it may have been managed to qualify.",
    ask: "How much of operating profit comes from one-off items?",
  },
  F4: {
    title: "Cheap shares for insiders", what: "Shares issued far below the IPO price, or a bonus issue, in the 18 months before",
    why: "Insiders who got shares cheaply can profit by selling soon after listing, even if the price falls.",
    ask: "At what price did insiders get shares in the last 18 months, and when does their lock-in end?",
  },
  F5: {
    title: "Depends on a few customers", what: "Revenue depends on a handful of customers",
    why: "Losing one large customer could cut revenue sharply.",
    ask: "Who are the largest customers, and are there long-term contracts with them?",
  },
  F6: {
    title: "Business with related parties", what: "Sales and purchases with promoters' own firms as a share of revenue",
    why: "Trade with the promoters' own companies can move money out of the listed company.",
    ask: "Are related-party transactions priced at market rates, and will they continue after listing?",
  },
  F7: {
    title: "Auditor's record", what: "The auditor's past SME IPOs and recent changes",
    why: "An auditor whose earlier SME clients did badly gives less comfort that the numbers are right.",
    ask: "Why was this auditor chosen, and has the auditor changed recently?",
  },
  F8: {
    title: "Priced above listed peers", what: "Issue P/E against the listed peers the prospectus names",
    why: "Paying more than similar listed companies leaves less room for gains.",
    ask: "Why does the issue price justify a higher valuation than listed peers?",
  },
  F9: {
    title: "Vague use of the money raised", what: "How much goes to 'general corporate purposes'",
    why: "Money raised for 'general corporate purposes' has no stated use.",
    ask: "What exactly will the general corporate purposes money be spent on?",
  },
  F10: {
    title: "Lead manager's track record", what: "How the lead manager's earlier SME IPOs did",
    why: "Lead managers whose earlier SME IPOs often fell sharply are a warning sign for the next one.",
    ask: "How have the lead manager's earlier SME IPOs performed after listing?",
  },
  F11: {
    title: "Linked to a SEBI order", what: "Someone connected to the issue appears in a SEBI order dated before the bid",
    why: "A connected party has been named by the regulator before. Worth knowing; it is not proof of wrongdoing by this company.",
    ask: "What was the SEBI order about, and is the named party still involved?",
  },
  F12: { title: "Changes between draft and final prospectus", what: "What changed between the DRHP and the RHP", why: "Late changes can hide problems found during review." },
  F13: { title: "Same anchor investors across issues", what: "The same anchor investors or market maker across issues", why: "A repeat group of anchors can signal a coordinated listing." },
  F14: { title: "Credit rating downgrades", what: "Rating downgrades before the issue", why: "A recent downgrade means lenders see more risk." },
};

/** Investor wording for the overall rating. */
export const RATING_TEXT: Record<string, { label: string; headline: string }> = {
  HIGH: { label: "High risk", headline: "Several warning signs. Look very carefully before you bid." },
  ELEVATED: { label: "Elevated risk", headline: "Some warning signs worth checking before you bid." },
  LOW: { label: "Low risk", headline: "No major warning signs in what we could check." },
  "INSUFFICIENT DATA": { label: "Not enough data", headline: "There was not enough information to rate this IPO." },
};

/** Severity of a single concern, in plain words. */
export const CONCERN_TEXT: Record<string, string> = { HIGH: "Serious", ELEVATED: "Moderate", LOW: "Minor" };

export const DEPTH_TEXT: Record<string, string> = {
  LIGHT: "Quick screen: parties, SEBI orders and the lead manager's record",
  STANDARD: "Standard read of the prospectus",
  FORENSIC: "Deep read of the prospectus",
};

/** 0.456 -> "46 in 100"; tiny odds stay honest rather than rounding to zero. */
export function odds(p: number | null | undefined): string {
  if (p === null || p === undefined) return "–";
  if (p < 0.01) return "under 1 in 100";
  return `${Math.round(p * 100)} in 100`;
}

/** A plain title and reason for a "not checked" line such as "F1: needed numbers are missing". */
export function notCheckedText(item: string): { title: string; reason: string } {
  const at = item.indexOf(": ");
  const head = at > 0 ? item.slice(0, at) : item;
  let reason = at > 0 ? item.slice(at + 2) : "";
  const code = head.match(/^F\d{1,2}\b/)?.[0];
  const stmt = head.match(/^restated (PL|BS|CF) statement/i)?.[1]?.toUpperCase();
  const title = code && FLAGS[code] ? FLAGS[code].title
    : stmt ? { PL: "Profit & loss statement", BS: "Balance sheet", CF: "Cash flow statement" }[stmt]!
    : head.charAt(0).toUpperCase() + head.slice(1);
  if (/needed numbers are missing or unverified/.test(reason)) reason = "The prospectus figures this needs could not be confirmed.";
  else if (/could not be read or did not pass/.test(reason)) reason = "Could not be read reliably from the prospectus, so it was not used.";
  else if (/LIGHT depth/.test(reason)) reason = "This was a quick screen, so that part of the prospectus was not read.";
  else if (reason) reason = sentence(reason);
  return { title, reason };
}

/** Drop internal check codes such as "(F1, F10, F11)" from prose. */
export const plainText = (t: string) => t.replace(/\s*\((?:F\d{1,2}(?:,\s*)?)+\)/g, "");

/** Capitalise and end with a full stop. */
export function sentence(t: string): string {
  const s = plainText(t).trim();
  if (!s) return s;
  return s.charAt(0).toUpperCase() + s.slice(1) + (/[.!?)]$/.test(s) ? "" : ".");
}

export const FEATURE_NAMES: Record<string, string> = {
  f_banker_crash: "How often the lead manager's past IPOs fell sharply",
  f_banker_low_history: "Lead manager has few past IPOs",
  f_banker_median_ret: "Typical return of the lead manager's past IPOs",
  f_watchlist_hit: "A party is named in a SEBI order",
  f_watchlist_hop1: "A party is linked to someone in a SEBI order",
  f_cfo_pat: "Cash earned compared with profit",
  f_cfo_neg_pat_pos: "Profit on paper, but cash going out",
  f_recv_rev_gap: "Unpaid bills growing faster than sales",
  f_profit_spike: "Jump in profit before the IPO",
  f_ebitda_headroom_min: "Margin above the ₹1 crore listing minimum",
  f_other_income_share: "Share of profit from non-core income",
  f_rev_growth: "Sales growth",
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
