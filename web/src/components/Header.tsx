import { useEffect, useRef, useState } from "react";
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

function StatusMenu({ status }: { status: Status | null }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  const z = status?.zetaris, m = status?.meterless;
  const allOk = z?.ok && m?.ok;
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button className="status-pill" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {status ? (
          <>
            <Indicator configured={!!z?.configured} ok={z?.ok} />
            <Indicator configured={!!m?.configured} ok={m?.ok} />
            <span className="label">{allOk ? "Live services up" : "Service status"}</span>
          </>
        ) : (
          <><span className="dot dot-off" /><span className="label">Checking…</span></>
        )}
      </button>
      {open && status && (
        <div className="popover stack">
          <div className="stack-sm">
            <div className="between">
              <div className="row"><Indicator configured={z!.configured} ok={z!.ok} /><b>Zetaris Lightning</b></div>
              <span className="small faint">{z!.ok ? `${z!.latency_ms} ms` : z!.configured ? "unreachable" : "not configured"}</span>
            </div>
            <p className="small muted">Data Port over SQL REST: the archive, live and price sources.</p>
            {z!.error && <p className="tiny mono" style={{ color: "var(--high-text)" }}>{z!.error}</p>}
          </div>
          <div className="divider" />
          <div className="stack-sm">
            <div className="between">
              <div className="row"><Indicator configured={m!.configured} ok={m!.ok} /><b>Meterless Nightshift</b></div>
              <span className="small faint">{m!.ok ? `${m!.models} models` : m!.configured ? "unreachable" : "not configured"}</span>
            </div>
            <p className="small muted">Model Gateway for Nemotron, on this Mac.</p>
            {m!.roles && Object.entries(m!.roles).map(([role, model]) => (
              <div key={role} className="between tiny"><span className="faint">{role}</span><span className="mono">{model.replace("nvidia/", "")}</span></div>
            ))}
            {m!.error && <p className="tiny mono" style={{ color: "var(--high-text)" }}>{m!.error}</p>}
          </div>
          <div className="divider" />
          <p className="small muted">Sample mode needs neither: it replays recorded Nemotron replies over a bundled archive, offline.</p>
        </div>
      )}
    </div>
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

export function Header({ path, status }: { path: string; status: Status | null }) {
  const nav = [
    { to: "/", label: "Investigate", match: (p: string) => p === "/" || p.startsWith("/ipo") },
    { to: "/runs", label: "Runs", match: (p: string) => p.startsWith("/run") },
    { to: "/backtest", label: "Backtest", match: (p: string) => p.startsWith("/backtest") },
    { to: "/how", label: "How it works", match: (p: string) => p.startsWith("/how") },
  ];
  return (
    <header className="topbar">
      <div className="container">
        <Link to="/" className="brand"><Logo /> RedFlag <span className="tag">SME IPO risk</span></Link>
        <nav className="nav" aria-label="Main">
          {nav.map((n) => (
            <Link key={n.to} to={n.to} className={n.match(path) ? "active" : undefined}>{n.label}</Link>
          ))}
        </nav>
        <span className="spacer" />
        <StatusMenu status={status} />
        <ThemeToggle />
      </div>
    </header>
  );
}
