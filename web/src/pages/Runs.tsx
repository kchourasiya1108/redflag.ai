import { useState } from "react";
import { api } from "../api";
import { ReportList, latestPerIpo } from "./Home";
import { ErrorBox } from "../components/ui";
import { IpoSearch } from "../components/Search";
import { useAsync, useInterval } from "../lib";

export function RunsPage() {
  const { data, error, loading, reload } = useAsync(() => api.runs(), []);
  const [all, setAll] = useState(false);
  const running = data?.active.filter((r) => r.status === "running") ?? [];
  useInterval(reload, running.length ? 4000 : null);
  const runs = all ? data?.runs : latestPerIpo(data?.runs);
  return (
    <div className="container stack-lg">
      <div className="between" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
        <div className="stack-sm">
          <h1>Reports</h1>
          <p className="muted">Every IPO checked so far{all ? ", including earlier checks of the same IPO" : ", latest report for each"}.</p>
        </div>
        <div className="segmented" role="group" aria-label="Which reports">
          <button aria-pressed={!all} onClick={() => setAll(false)}>Latest per IPO</button>
          <button aria-pressed={all} onClick={() => setAll(true)}>Every check</button>
        </div>
      </div>
      {error && <ErrorBox error={error} />}
      {running.length > 0 && (
        <section className="stack-sm">
          <h2>Checking now</h2>
          <ReportList runs={running} />
        </section>
      )}
      <ReportList runs={runs} loading={loading} />
      <section className="stack-sm">
        <h2>Check another IPO</h2>
        <IpoSearch />
      </section>
    </div>
  );
}
