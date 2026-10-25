import { useState } from "react";
import { api } from "../api";
import { RunList } from "./Home";
import { ErrorBox } from "../components/ui";
import { Link, useAsync, useInterval } from "../lib";

export function RunsPage() {
  const { data, error, loading, reload } = useAsync(() => api.runs(), []);
  const [filter, setFilter] = useState<"all" | "live" | "sample">("all");
  useInterval(reload, data?.active.some((r) => r.status === "running") ? 4000 : null);
  const runs = (data?.runs ?? []).filter((r) => filter === "all" || (filter === "sample") === r.sample_mode);
  return (
    <div className="container stack-lg">
      <div className="between" style={{ flexWrap: "wrap" }}>
        <div className="stack-sm">
          <div className="eyebrow">History</div>
          <h1>Runs</h1>
        </div>
        <div className="segmented" role="group" aria-label="Filter runs">
          {(["all", "live", "sample"] as const).map((f) => (
            <button key={f} aria-pressed={filter === f} onClick={() => setFilter(f)}>{f[0].toUpperCase() + f.slice(1)}</button>
          ))}
        </div>
      </div>
      {error && <ErrorBox error={error} />}
      {data && data.active.length > 0 && (
        <section className="stack-sm">
          <h2>In progress or failed</h2>
          <div className="card table-wrap">
            <table>
              <tbody>
                {data.active.map((r) => (
                  <tr key={r.run_id}>
                    <td><Link to={`/run/${r.run_id}`}><b>{r.ipo_id}</b></Link><div className="tiny faint mono">{r.run_id}</div></td>
                    <td>{r.status === "running" ? <span className="pill"><span className="dot dot-live" /> Running</span> : <span className="badge sev-HIGH">Error</span>}</td>
                    <td className="small muted">{r.error ? `${r.error.type}: ${r.error.message}` : r.sample_mode ? "Sample" : "Live"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <RunList runs={runs} loading={loading} />
    </div>
  );
}
