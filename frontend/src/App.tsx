import { useEffect, useState } from "react";
import { api, Status } from "./api";
import { useHashRoute, usePoll } from "./hooks";
import { loadSelection, saveSelection, Selection } from "./selection";
import Overview from "./screens/Overview";
import ScanScreen from "./screens/ScanScreen";
import Recommendations from "./screens/Recommendations";
import Confirm from "./screens/Confirm";
import RunScreen from "./screens/RunScreen";
import History from "./screens/History";

const NAV: [string, string][] = [
  ["overview", "Overview"],
  ["scan", "Scan"],
  ["recommendations", "Recommendations"],
  ["confirm", "Confirm"],
  ["history", "Runs"],
];

export default function App() {
  const [route, go] = useHashRoute();
  const { data: status, reload: reloadStatus } = usePoll(() => api.get<Status>("/status"), 4000);
  const [selection, setSelection] = useState<Selection>(loadSelection);
  useEffect(() => saveSelection(selection), [selection]);

  const [page, arg] = route.split("/");
  const active = page === "run" ? "history" : page;

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand" onClick={() => go("overview")}>
          <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden>
            <rect width="32" height="32" rx="8" fill="var(--accent)" />
            <path d="M8 11h16M11 16h10M14 21h4" stroke="var(--paper)" strokeWidth="2.4" strokeLinecap="round" />
          </svg>
          <span>Sift</span>
        </div>
        <nav>
          {NAV.map(([k, label]) => (
            <a key={k} href={`#/${k}`} className={active === k ? "on" : ""}>{label}</a>
          ))}
        </nav>
        <div className="status-chips">
          {status?.synthetic && <span className="chip chip-synthetic" title="Synthetic mailbox for development. Nothing here is real mail.">Synthetic data</span>}
          {status && (
            <span className={`chip ${status.classifier.uses_ai ? "chip-ai" : "chip-rules"}`}
              title={status.classifier.uses_ai ? "Claude classifies ambiguous senders" : "No AI calls are made. Deterministic rules only."}>
              {status.classifier.label}
            </span>
          )}
          {status && <span className="account">{status.account}</span>}
        </div>
      </header>
      <main>
        {page === "overview" && <Overview go={go} />}
        {page === "scan" && <ScanScreen go={go} />}
        {page === "recommendations" && <Recommendations selection={selection} setSelection={setSelection} go={go} />}
        {page === "confirm" && <Confirm selection={selection} status={status} go={go} />}
        {page === "run" && arg && <RunScreen runId={Number(arg)} go={go} onChange={reloadStatus} status={status} />}
        {page === "history" && <History go={go} />}
      </main>
      <footer className="foot">
        {status?.synthetic
          ? "Synthetic mailbox · these results are not from a real account"
          : status?.mode}
        {" · "}No permanent deletes, ever. Every change is labelled and logged.
      </footer>
    </div>
  );
}
