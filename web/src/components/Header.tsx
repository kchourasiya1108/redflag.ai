import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";
import { api } from "../api";
import type { Status } from "../types";
import { Link, cx, useInterval } from "../lib";

function Logo() {
  return (
    <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden>
      <rect width="32" height="32" rx="8" fill="#d03b3b" />
      <path d="M11 8v17" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" />
      <path d="M11 9h11l-3 4.5 3 4.5H11z" fill="#fff" />
    </svg>
  );
}

export function useStatus() {
  const [status, setStatus] = useState<Status | null>(null);
  const load = () => api.status().then(setStatus, () => setStatus(null));
  useEffect(() => {
    load();
  }, []);
  useInterval(load, 30_000);
  return status;
}

function Indicator({ ok, configured }: { ok?: boolean; configured: boolean }) {
  return <span className={cx("dot", !configured ? "dot-off" : ok ? "dot-ok" : "dot-bad")} />;
}

/** Live service health, for the technical section of "How we rate". */
export function ServiceDetails({ status }: { status: Status | null }) {
  if (!status) return <p className="small muted">Checking the live services…</p>;
  const z = status.zetaris, m = status.meterless;
  return (
    <div className="stack">
      <div className="stack-sm">
        <div className="between">
          <div className="row"><Indicator configured={z.configured} ok={z.ok} /><b>Zetaris Lightning</b></div>
          <span className="small faint">{z.ok ? `${z.latency_ms} ms` : z.configured ? "unreachable" : "not configured"}</span>
        </div>
        <p className="small muted">The data layer: the IPO archive, filings, SEBI orders and prices over SQL.</p>
        {z.error && <p className="tiny mono" style={{ color: "var(--high-text)" }}>{z.error}</p>}
      </div>
      <div className="divider" />
      <div className="stack-sm">
        <div className="between">
          <div className="row"><Indicator configured={m.configured} ok={m.ok} /><b>Meterless Nightshift</b></div>
          <span className="small faint">{m.ok ? `${m.models} models` : m.configured ? "unreachable" : "not configured"}</span>
        </div>
        <p className="small muted">The model gateway that serves NVIDIA Nemotron.</p>
        {m.roles && Object.entries(m.roles).map(([role, model]) => (
          <div key={role} className="between tiny"><span className="faint">{role}</span><span className="mono">{model.replace("nvidia/", "")}</span></div>
        ))}
        {m.error && <p className="tiny mono" style={{ color: "var(--high-text)" }}>{m.error}</p>}
      </div>
    </div>
  );
}

/** One quiet line for the footer: can a fresh check run right now? */
export function LiveIndicator({ status }: { status: Status | null }) {
  const ok = !!(status?.zetaris.ok && status?.meterless.ok);
  return (
    <Link to="/how#technology" className="row small" title="Service details">
      <span className={cx("dot", !status ? "dot-off" : ok ? "dot-ok" : "dot-bad")} />
      <span className="faint">{!status ? "Checking live data…" : ok ? "Live data connected" : "Live data offline: saved reports still work"}</span>
    </Link>
  );
}

function ThemeToggle() {
  const [theme, setTheme] = useState<string | null>(() => {
    try { return localStorage.getItem("rf-theme"); } catch { return null; }
  });
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    try { if (theme) localStorage.setItem("rf-theme", theme); } catch { /* private mode */ }
  }, [theme]);
  const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  return (
    <button className="icon-btn" onClick={() => setTheme(dark ? "light" : "dark")} aria-label={dark ? "Light theme" : "Dark theme"} title={dark ? "Light theme" : "Dark theme"}>
      {dark ? <Sun size={17} /> : <Moon size={17} />}
    </button>
  );
}

export function Header({ path }: { path: string }) {
  const nav = [
    { to: "/", label: "Check an IPO", match: (p: string) => p === "/" || p.startsWith("/ipo") },
    { to: "/reports", label: "Reports", match: (p: string) => p.startsWith("/run") || p.startsWith("/report") },
    { to: "/track-record", label: "Track record", match: (p: string) => p.startsWith("/track-record") || p.startsWith("/backtest") },
    { to: "/how", label: "How we rate", match: (p: string) => p.startsWith("/how") },
  ];
  return (
    <header className="topbar">
      <div className="container">
        <Link to="/" className="brand"><Logo /> RedFlag <span className="tag">SME IPO checker</span></Link>
        <nav className="nav" aria-label="Main">
          {nav.map((n) => (
            <Link key={n.to} to={n.to} className={n.match(path) ? "active" : undefined}>{n.label}</Link>
          ))}
        </nav>
        <span className="spacer" />
        <ThemeToggle />
      </div>
    </header>
  );
}
