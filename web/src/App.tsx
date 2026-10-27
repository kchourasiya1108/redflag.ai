import { Header, useStatus } from "./components/Header";
import { Empty } from "./components/ui";
import { Home } from "./pages/Home";
import { HowPage } from "./pages/How";
import { IpoPage } from "./pages/Ipo";
import { RunPage } from "./pages/Run";
import { RunsPage } from "./pages/Runs";
import { UploadPage } from "./pages/Upload";
import { Link, useLocation } from "./lib";

export function App() {
  const path = useLocation();
  const status = useStatus();
  const seg = path.split("/").filter(Boolean).map(decodeURIComponent);
  let page;
  if (seg.length === 0) page = <Home />;
  else if ((seg[0] === "upload" || seg[0] === "ipo") && seg[1]?.startsWith("UP-")) page = <UploadPage id={seg[1]} status={status} />;
  else if (seg[0] === "ipo" && seg[1]) page = <IpoPage id={seg[1]} status={status} />;
  else if (seg[0] === "run" && seg[1]) page = <RunPage id={seg[1]} />;
  else if (seg[0] === "runs" || seg[0] === "reports") page = <RunsPage />;
  else if (seg[0] === "backtest" || seg[0] === "track-record") page = <HowPage section="track-record" />;
  else if (seg[0] === "how") page = <HowPage />;
  else page = <div className="container"><Empty title="Page not found"><Link to="/">Back to the IPO search</Link></Empty></div>;
  return (
    <div className="app">
      <Header path={path} />
      <main key={seg.slice(0, 2).join("/")}>{page}</main>
      <footer className="footer">
        <div className="container between" style={{ flexWrap: "wrap" }}>
          <span>RedFlag rates risk and shows its evidence. It is not investment advice.</span>
          <span>Powered by Zetaris and NVIDIA Nemotron</span>
        </div>
      </footer>
    </div>
  );
}
