import { useRef, useState, type DragEvent } from "react";
import { AlertTriangle, ArrowRight, CheckCircle2, ChevronLeft, FileUp, Loader2, Plus, RefreshCw, Search, X } from "lucide-react";
import { api, uploadRhp } from "../api";
import { Card, ErrorBox, Skeleton } from "../components/ui";
import type { Status, Upload, UploadFields } from "../types";
import { Link, ago, cx, date, navigate, useAsync, useInterval } from "../lib";
import { ReportList, titleCase } from "./Home";

/** Drop or pick an RHP PDF: we fill in the details and start the check straight away. */
export function UploadBox({ compact }: { compact?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [phase, setPhase] = useState<"idle" | "sending" | "reading" | "starting">("idle");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const send = async (file: File | undefined) => {
    if (!file) return;
    if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
      setError("Please choose the prospectus as a PDF file.");
      return;
    }
    setError(null);
    setPhase("sending");
    try {
      const up = await uploadRhp(file, (f) => {
        setProgress(f);
        if (f >= 1) setPhase("reading");
      });
      if (up.status !== "ok" || up.missing.length) {
        navigate(`/upload/${up.upload_id}`);       // a scanned PDF, or no company name found
        return;
      }
      setPhase("starting");
      try {
        navigate(`/run/${(await api.check(up.upload_id)).run_id}`);
      } catch {
        navigate(`/upload/${up.upload_id}`);       // e.g. another live check is running
      }
    } catch (e) {
      setError((e as Error).message);
      setPhase("idle");
    }
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    send(e.dataTransfer.files[0]);
  };

  return (
    <div className="stack-sm">
      <div
        className={cx("dropzone", over && "over", compact && "compact")}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
        onClick={() => phase === "idle" && input.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && phase === "idle" && input.current?.click()}
        aria-label="Upload the RHP PDF"
      >
        <input ref={input} type="file" accept="application/pdf,.pdf" hidden onChange={(e) => send(e.target.files?.[0])} />
        {phase === "idle" ? (
          <>
            <div className="ic"><FileUp size={22} /></div>
            <div className="t">Upload the RHP of an upcoming SME IPO</div>
            <div className="small muted">Drop the red herring prospectus PDF here, or <span className="link">choose a file</span>. That's all we need; we fill in the rest. Up to 80 MB.</div>
          </>
        ) : (
          <>
            <div className="ic"><Loader2 size={22} className="spin" /></div>
            <div className="t">{phase === "sending" ? `Uploading… ${Math.round(progress * 100)}%` : "Reading the prospectus…"}</div>
            <div className="progress" style={{ width: "min(320px, 80%)" }}><div style={{ width: `${phase === "sending" ? progress * 100 : 100}%` }} /></div>
            <div className="small muted">{phase === "reading" ? "Finding the company, bid date, price and the firms involved. This takes a few seconds." : " "}</div>
          </>
        )}
      </div>
      {error && <ErrorBox error={error} />}
    </div>
  );
}

const LABELS: Record<string, string> = {
  company: "Company name", open_date: "Bid opens on", issue_price: "Issue price (₹ per share)", exchange: "Exchange",
  lead_managers: "Lead manager(s)", registrar: "Registrar", market_maker: "Market maker", auditor: "Statutory auditor",
  lot_size: "Lot size (shares)",
};

/** Confirm what was read from the cover, then start the check. */
export function UploadPage({ id, status }: { id: string; status: Status | null }) {
  const { data, error, loading } = useAsync(() => api.ipo(id), [id]);
  if (loading && !data) return <div className="container stack"><Skeleton h={34} w={360} /><Skeleton h={320} /></div>;
  if (error) return <div className="container"><ErrorBox error={error} /></div>;
  if (!data?.upload) return <div className="container"><ErrorBox error="This upload was not found." /></div>;
  return <UploadDetails upload={data.upload} runs={data.runs} status={status} />;
}

function UploadDetails({ upload: initial, runs, status }: { upload: Upload; runs: import("../types").RunSummary[]; status: Status | null }) {
  const [up, setUp] = useState(initial);
  const [form, setForm] = useState<UploadFields>(initial.fields);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const liveReady = !!(status?.zetaris.ok && status?.meterless.ok);
  const [offline, setOffline] = useState(false);
  const useOffline = offline || (!!status && !liveReady);

  const set = <K extends keyof UploadFields>(k: K, v: UploadFields[K]) => setForm((f) => ({ ...f, [k]: v }));
  const gaps = up.required.filter((k) => {
    const v = form[k as keyof UploadFields];
    return v === undefined || v === null || v === "" || (typeof v === "number" && !(v > 0));
  });
  const managers = form.lead_managers?.length ? form.lead_managers : [""];
  // Where each value came from; a field the investor has changed here says so.
  const src = (k: keyof UploadFields) => JSON.stringify(form[k] ?? null) !== JSON.stringify(up.fields[k] ?? null)
    ? "changed by you" : up.field_sources?.[k] ? `from ${up.field_sources[k]}` : form[k] ? undefined : "not found";

  const check = async () => {
    setSaving(true);
    setError(null);
    try {
      const keys: (keyof UploadFields)[] = ["company", "open_date", "issue_price", "exchange", "lead_managers",
        "registrar", "market_maker", "auditor", "lot_size"];
      const changes = Object.fromEntries(keys.map((k) => [k, k === "lead_managers"
        ? (form.lead_managers ?? []).filter((m) => m.trim()) : form[k] ?? null])) as UploadFields;
      const saved = await api.editUpload(up.upload_id, changes);
      setUp(saved);
      if (saved.missing.length) throw new Error(`Please fill in: ${saved.missing.map((m) => LABELS[m] ?? m).join(", ")}`);
      const r = offline ? await api.start(up.upload_id, true) : await api.check(up.upload_id);
      navigate(`/run/${r.run_id}`);
    } catch (e) {
      const msg = (e as Error).message;
      setError(/already in progress|busy/i.test(msg) ? "Another live check is running. Please wait a minute and try again." : msg);
      setSaving(false);
    }
  };

  if (up.status === "scanned") {
    return (
      <div className="container stack-lg narrow">
        <Link to="/" className="small row"><ChevronLeft size={14} /> Home</Link>
        <Card>
          <div className="stack">
            <div className="row"><AlertTriangle size={20} color="var(--elev)" /><h1 style={{ fontSize: 22 }}>This PDF has no readable text</h1></div>
            <p className="muted">It looks like a scanned document ({up.text_pages} of {up.pages} pages have text). We can only check prospectuses with a text layer, like the ones the exchanges publish. Please download the RHP from the NSE or BSE website and upload that file.</p>
            <UploadBox compact />
          </div>
        </Card>
      </div>
    );
  }

  const upcoming = form.open_date ? new Date(form.open_date + "T00:00:00") > new Date() : false;
  return (
    <div className="container stack-lg narrow">
      <div className="stack-sm">
        <Link to="/" className="small row"><ChevronLeft size={14} /> Home</Link>
        <div className="small faint">
          {up.source ? <>RHP from {up.source.exchange} · {up.pages} pages{up.source.rhp_url && <> · <a href={up.source.rhp_url} target="_blank" rel="noreferrer">original file</a></>}</>
            : <>Uploaded {up.filename} · {up.pages} pages</>}
        </div>
        <h1>{titleCase(form.company) || "Your prospectus"}</h1>
        <p className="muted">These are the details we used, filled in from {up.source ? "the exchange and the RHP" : "the RHP, our archive and the exchange"}. You don't need to change anything; correct a value only if it is wrong, then check again.</p>
      </div>

      <Card title="Issue details" hint="Under each field: where the value came from">
        <div className="form-grid">
          <Field label={LABELS.company} source={src("company")} wide>
            <input value={form.company ?? ""} onChange={(e) => set("company", e.target.value)} placeholder="e.g. Acme Industries Limited" />
          </Field>
          <Field label={LABELS.open_date} source={src("open_date")} hint={form.open_date ? (upcoming ? "Upcoming: judged on what is known today" : "Judged on what was known that day") : "Not announced yet: judged on what is known today"}>
            <input type="date" value={form.open_date ?? ""} onChange={(e) => set("open_date", e.target.value)} />
          </Field>
          <Field label={LABELS.issue_price} source={src("issue_price")} hint={form.issue_price ? "Fixed price, or the top of the price band" : "Price band not announced yet: the cheap-insider-shares check waits for it"}>
            <input type="number" min="1" step="0.01" inputMode="decimal" value={form.issue_price ?? ""} onChange={(e) => set("issue_price", e.target.value === "" ? undefined : Number(e.target.value))} placeholder="e.g. 120" />
          </Field>
          <Field label={LABELS.exchange} source={src("exchange")}>
            <div className="segmented" role="radiogroup" aria-label="Exchange">
              {up.exchanges.map((x) => (
                <button key={x} type="button" role="radio" aria-pressed={form.exchange === x} aria-checked={form.exchange === x} onClick={() => set("exchange", x)}>{x}</button>
              ))}
            </div>
          </Field>
          <Field label={LABELS.lot_size} source={src("lot_size")}>
            <input type="number" min="1" step="1" value={form.lot_size ?? ""} onChange={(e) => set("lot_size", e.target.value === "" ? undefined : Number(e.target.value))} />
          </Field>
          <Field label={LABELS.lead_managers} source={src("lead_managers")} wide hint="We look up each lead manager's past SME IPOs">
            <div className="stack-sm">
              {managers.map((m, i) => (
                <div className="row" key={i}>
                  <input value={m} onChange={(e) => set("lead_managers", managers.map((x, j) => (j === i ? e.target.value : x)))} placeholder="e.g. Hem Securities Limited" />
                  {managers.length > 1 && <button type="button" className="icon-btn" aria-label="Remove" onClick={() => set("lead_managers", managers.filter((_, j) => j !== i))}><X size={15} /></button>}
                </div>
              ))}
              {managers.length < 4 && <button type="button" className="btn btn-sm btn-ghost" style={{ alignSelf: "flex-start" }} onClick={() => set("lead_managers", [...managers, ""])}><Plus size={14} /> Add another</button>}
            </div>
          </Field>
          <Field label={LABELS.auditor} source={src("auditor")}>
            <input value={form.auditor ?? ""} onChange={(e) => set("auditor", e.target.value)} />
          </Field>
          <Field label={LABELS.registrar} source={src("registrar")}>
            <input value={form.registrar ?? ""} onChange={(e) => set("registrar", e.target.value)} />
          </Field>
          <Field label={LABELS.market_maker} source={src("market_maker")}>
            <input value={form.market_maker ?? ""} onChange={(e) => set("market_maker", e.target.value)} />
          </Field>
        </div>
      </Card>

      <Card>
        <div className="stack">
          {error && <ErrorBox error={error} />}
          {gaps.length > 0 && <p className="small muted">We could not find the company name in this file; please type it.</p>}
          <button className="btn btn-primary btn-lg" onClick={check} disabled={saving || gaps.length > 0}>
            {saving ? <Loader2 size={17} className="spin" /> : <Search size={17} />}
            {saving ? "Starting…" : runs.length ? "Check again" : "Check this IPO"}
          </button>
          <p className="small faint" style={{ textAlign: "center", marginTop: -6 }}>
            {useOffline ? "Offline check against our archive, without the AI reading steps" : "Usually takes about 3 minutes"}
          </p>
          <details className="disclosure">
            <summary>Check options</summary>
            <label className="row small" style={{ marginTop: 10 }}>
              <input type="checkbox" checked={useOffline} disabled={!!status && !liveReady} onChange={(e) => setOffline(e.target.checked)} />
              Offline check: use the local archive and skip the AI reading steps
              {status && !liveReady && <span className="faint">(live data is offline right now)</span>}
            </label>
          </details>
        </div>
      </Card>

      {runs.length > 0 && (
        <section className="stack-sm">
          <h2>Earlier checks of this prospectus</h2>
          <ReportList runs={runs} />
        </section>
      )}
      <p className="tiny faint">{up.source ? "Downloaded from the exchange's offer documents." : "Your file is stored on this RedFlag server only, to show the pages behind each finding."} Checked as of {form.open_date ? date(upcoming ? new Date().toISOString().slice(0, 10) : form.open_date) : "the bid date"}.</p>
    </div>
  );
}

function Field({ label, source, hint, wide, children }: {
  label: string; source?: string; hint?: string; wide?: boolean; children: React.ReactNode;
}) {
  return (
    <label className={cx("field", wide && "wide")} onClick={(e) => (e.target as HTMLElement).tagName === "BUTTON" && e.preventDefault()}>
      <span className="lbl">{label}</span>
      {children}
      {source && (
        <span className={cx("tiny src", source === "not found" && "none")}>
          {source !== "not found" && source !== "changed by you" && <CheckCircle2 size={11} className="ok-ic" />} {source.charAt(0).toUpperCase() + source.slice(1)}
        </span>
      )}
      {hint && <span className="tiny faint">{hint}</span>}
    </label>
  );
}

/** Recently uploaded prospectuses, for the home page. */
export function RecentUploads() {
  const { data } = useAsync(() => api.uploads(), []);
  const items = data?.items.filter((u) => !u.source).slice(0, 6) ?? [];
  if (!items.length) return null;
  return (
    <div className="card report-list">
      {items.map((u) => (
        <Link key={u.upload_id} to={`/upload/${u.upload_id}`} className="report-row">
          <div style={{ minWidth: 0 }}>
            <div className="name">{titleCase(u.fields.company) || u.filename}</div>
            <div className="small faint">{u.fields.exchange ?? "Exchange not set"} · bid {u.fields.open_date ? date(u.fields.open_date) : "date not set"}{u.missing.length ? " · details needed" : ""}</div>
          </div>
          <span className="pill">Uploaded RHP</span>
          <span />
          <span />
        </Link>
      ))}
    </div>
  );
}

/** Open and upcoming SME IPOs whose RHPs we have already loaded from the exchanges. */
export function UpcomingIpos() {
  const { data, reload } = useAsync(() => api.upcoming(), []);
  const [asked, setAsked] = useState(false);
  const refreshing = asked || !!data?.refreshing;
  useInterval(() => {
    reload();
    if (asked && data && !data.refreshing) setAsked(false);
  }, refreshing ? 3000 : null);
  const refresh = async () => {
    setAsked(true);
    await api.refreshUpcoming().catch(() => setAsked(false));
  };
  const items = data?.items ?? [];
  const today = new Date().toISOString().slice(0, 10);
  return (
    <section>
      <div className="section-title">
        <h2>Open and upcoming SME IPOs</h2>
        <span className="row small faint" style={{ gap: 10 }}>
          {data?.last?.refreshed_at ? `Updated ${ago(data.last.refreshed_at)}` : "Not loaded yet"}
          <button className="btn btn-sm" onClick={refresh} disabled={refreshing}>
            <RefreshCw size={13} className={refreshing ? "spin" : undefined} /> {refreshing ? "Checking the exchanges…" : "Refresh"}
          </button>
        </span>
      </div>
      {items.length === 0 ? (
        <div className="card"><div className="empty small">
          {refreshing ? "Looking for new SME IPOs on NSE Emerge and BSE SME and downloading their RHPs…"
            : "No open or upcoming SME IPOs found yet. Press Refresh to check NSE Emerge and BSE SME, or upload an RHP above."}
        </div></div>
      ) : (
        <div className="card report-list">
          {items.map((u) => {
            const open = u.fields.open_date;
            const state = !open ? "Dates not announced" : open > today ? `Bid opens ${date(open)}` : u.source?.close_date && u.source.close_date >= today ? `Open now · closes ${date(u.source.close_date)}` : `Bid opened ${date(open)}`;
            return (
              <Link key={u.upload_id} to={`/upload/${u.upload_id}`} className="report-row">
                <div style={{ minWidth: 0 }}>
                  <div className="name">{titleCase(u.fields.company) || u.filename}</div>
                  <div className="small faint">{u.source?.exchange ?? u.fields.exchange} · {state}{u.source?.price_band ? ` · ${u.source.price_band.replace(/Rs\./g, "₹")}` : ""}</div>
                </div>
                {(() => {
                  const live = !!open && open <= today && !!u.source?.close_date && u.source.close_date >= today;
                  return <span className={cx("pill", live && "pill-live")}>{live ? "Open" : open && open <= today ? "Recent" : "Upcoming"}</span>;
                })()}
                <CheckNow id={u.upload_id} />
                <ArrowRight size={15} className="faint go" />
              </Link>
            );
          })}
        </div>
      )}
      {data?.error && <p className="small faint" style={{ marginTop: 8 }}>The last refresh failed: {data.error}</p>}
    </section>
  );
}

/** One click from a list row to a running check. */
function CheckNow({ id }: { id: string }) {
  const [busy, setBusy] = useState(false);
  return (
    <button className="btn btn-sm btn-primary" disabled={busy} onClick={async (e) => {
      e.preventDefault();
      e.stopPropagation();
      setBusy(true);
      try {
        navigate(`/run/${(await api.check(id)).run_id}`);
      } catch {
        navigate(`/upload/${id}`);
      }
    }}>
      {busy ? <Loader2 size={13} className="spin" /> : <Search size={13} />} Check
    </button>
  );
}
