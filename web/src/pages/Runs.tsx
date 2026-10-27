import { api } from "../api";
import { ReportList, latestPerIpo } from "./Home";
import { ErrorBox } from "../components/ui";
import { UploadBox } from "./Upload";
import { useAsync, useInterval } from "../lib";

export function RunsPage() {
  const { data, error, loading, reload } = useAsync(() => api.runs(), []);
  const running = data?.active.filter((r) => r.status === "running") ?? [];
  useInterval(reload, running.length ? 4000 : null);
  return (
    <div className="container stack-lg narrow">
      <h1>My reports</h1>
      {error && <ErrorBox error={error} />}
      {running.length > 0 && <ReportList runs={running} />}
      <ReportList runs={latestPerIpo(data?.runs)} loading={loading} />
      <section className="stack-sm">
        <h2>Check another IPO</h2>
        <UploadBox compact />
      </section>
    </div>
  );
}
