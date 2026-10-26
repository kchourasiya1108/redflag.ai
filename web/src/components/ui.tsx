import { AlertOctagon, AlertTriangle, CheckCircle2, HelpCircle } from "lucide-react";
import type { ReactNode } from "react";
import type { Rating } from "../types";
import { CONCERN_TEXT, RATING_TEXT, cx } from "../lib";

const SEV_ICON = { HIGH: AlertOctagon, ELEVATED: AlertTriangle, LOW: CheckCircle2 } as const;

/** Severity always carries an icon and a word, never colour alone. "concern" words a single finding. */
export function SeverityBadge({ level, size, kind = "rating" }: {
  level: Rating | string | null | undefined; size?: "lg" | "xl"; kind?: "rating" | "concern";
}) {
  const key = level === "HIGH" || level === "ELEVATED" || level === "LOW" ? level : "NONE";
  const Icon = key === "NONE" ? HelpCircle : SEV_ICON[key];
  const iconSize = size === "xl" ? 20 : size === "lg" ? 16 : 13;
  return (
    <span className={cx("badge", `sev-${key}`, size && `badge-${size}`)}>
      <Icon size={iconSize} strokeWidth={2.4} aria-hidden />
      {label(level, kind)}
    </span>
  );
}

export function Card({ title, hint, action, children, pad = true, className }: {
  title?: ReactNode; hint?: ReactNode; action?: ReactNode; children: ReactNode; pad?: boolean; className?: string;
}) {
  return (
    <section className={cx("card", className)}>
      {(title || action) && (
        <div className="card-head">
          <div className="stack-sm" style={{ gap: 2 }}>
            {title && <h2 style={{ fontSize: 15.5 }}>{title}</h2>}
            {hint && <div className="small faint">{hint}</div>}
          </div>
          {action}
        </div>
      )}
      <div className={pad ? "card-body" : undefined}>{children}</div>
    </section>
  );
}

export function Skeleton({ h = 16, w = "100%" }: { h?: number; w?: number | string }) {
  return <div className="skeleton" style={{ height: h, width: w }} />;
}

export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      {icon}
      <h3>{title}</h3>
      {children && <div className="small muted" style={{ maxWidth: 460 }}>{children}</div>}
    </div>
  );
}

export function ErrorBox({ error }: { error: Error | string }) {
  return (
    <div className="callout err" role="alert">
      <AlertOctagon size={18} aria-hidden />
      <div>{typeof error === "string" ? error : error.message}</div>
    </div>
  );
}

export function Stat({ label, value, sub, icon }: { label: ReactNode; value: ReactNode; sub?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="card stat">
      <div className="k">{icon}{label}</div>
      <div className="v">{value}</div>
      {sub && <div className="s">{sub}</div>}
    </div>
  );
}

function label(level: string | null | undefined, kind: "rating" | "concern"): string {
  if (!level) return "–";
  const text = kind === "concern" ? CONCERN_TEXT[level] : RATING_TEXT[level]?.label;
  if (text) return text;
  return { error: "Could not finish", running: "Checking…" }[level] ?? level;
}
