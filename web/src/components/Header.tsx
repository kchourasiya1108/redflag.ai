import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";
import { api } from "../api";
import type { Status } from "../types";
import { Link, useInterval } from "../lib";

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
    { to: "/", label: "Check an IPO", match: (p: string) => p === "/" || p.startsWith("/ipo") || p.startsWith("/upload") },
    { to: "/reports", label: "My reports", match: (p: string) => p.startsWith("/run") || p.startsWith("/report") },
    { to: "/how", label: "How it works", match: (p: string) => p.startsWith("/how") || p.startsWith("/track-record") || p.startsWith("/backtest") },
  ];
  return (
    <header className="topbar">
      <div className="container">
        <Link to="/" className="brand"><Logo /> RedFlag </Link>
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
