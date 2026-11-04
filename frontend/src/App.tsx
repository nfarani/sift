import { useCallback, useEffect, useState } from "react";
import { api, Status } from "./api";
import { useHashRoute, usePoll } from "./hooks";
import { loadSelection, saveSelection, Selection } from "./selection";
import Overview from "./screens/Overview";
import ScanScreen from "./screens/ScanScreen";
import Recommendations from "./screens/Recommendations";
import Confirm from "./screens/Confirm";
import RunScreen from "./screens/RunScreen";
import History from "./screens/History";
import Account from "./screens/Account";
import Connect from "./screens/Connect";
import Claude from "./screens/Claude";
import Cleanup from "./screens/Cleanup";
import Unsubscribe from "./screens/Unsubscribe";
import Report from "./screens/Report";

const NAV: [string, string][] = [
  ["overview", "Overview"],
  ["scan", "Scan"],
  ["claude", "Claude"],
  ["recommendations", "Recommendations"],
  ["confirm", "Confirm"],
  ["history", "Runs"],
  ["report", "Report"],
  ["unsubscribe", "Unsubscribe"],
  ["account", "Account"],
];

export default function App() {
  const [route, go] = useHashRoute();
  const { data: status, error: statusError, reload: reloadStatus } = usePoll(() => api.get<Status>("/status"), 2500);
  const demo = status?.demo_mode;

  // Selection is tagged with the mode it was made in. Normal mode persists to localStorage; demo
  // mode starts empty and stays in memory, so nothing chosen with real addresses shows up masked
  // (or the other way round) and nothing masked is ever written to storage.
  const [sel, setSel] = useState<{ demo: boolean | null; s: Selection }>({ demo: null, s: {} });
  useEffect(() => {
    if (demo === undefined) return;
    setSel({ demo, s: demo ? {} : loadSelection() });
  }, [demo]);
  useEffect(() => { if (sel.demo === false) saveSelection(sel.s); }, [sel]);
  const selection = sel.s;
  const setSelection = useCallback((f: (s: Selection) => Selection) => setSel((x) => ({ ...x, s: f(x.s) })), []);

  // While demo mode switches, show nothing fetched under the old mode. Every screen remounts
  // (key below) and refetches once the backend reports the new mode.
  const [switching, setSwitching] = useState<boolean | null>(null);
  const [demoErr, setDemoErr] = useState<string | null>(null);
  useEffect(() => { if (switching !== null && demo === switching) setSwitching(null); }, [demo, switching]);
  const toggleDemo = async () => {
    if (!status || switching !== null) return;
    const target = !status.demo_mode;
    setDemoErr(null);
    setSwitching(target);
    try {
      await api.post("/demo", { enabled: target });
      await reloadStatus();
    } catch (e) {
      setDemoErr(`Couldn't switch demo mode: ${(e as Error).message}`);
      setSwitching(null);
    }
  };
  const ready = !!status && switching === null && sel.demo === demo;

  const [page, arg] = route.split("/");
  const active = page === "run" ? "history" : page === "cleanup" ? "account" : page;
  const demoOn = switching ?? !!demo;

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="#/overview" aria-label="Sift, overview">
          <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden>
            <rect width="32" height="32" rx="8" fill="var(--accent)" />
            <path d="M8 11h16M11 16h10M14 21h4" stroke="var(--paper)" strokeWidth="2.4" strokeLinecap="round" />
          </svg>
          <span>Sift</span>
        </a>
        <nav>
          {NAV.map(([k, label]) => (
            <a key={k} href={`#/${k}`} className={active === k ? "on" : ""}>{label}</a>
          ))}
        </nav>
        <div className="status-chips">
          {status?.synthetic && <span className="chip chip-synthetic" title="Synthetic mailbox for development. Nothing here is real mail.">Synthetic data</span>}
          {status && !status.synthetic && status.connection.state === "connected" && (
            <a href="#/cleanup" className="chip chip-live" title={status.mode}>
              Gmail · {status.cleanup.state === "enabled" ? "cleanup enabled" : status.cleanup.state === "reauth_required" ? "reconnect cleanup" : "read-only"}
            </a>
          )}
          {status && (
            <span className={`chip ${status.classifier.uses_ai ? "chip-ai" : "chip-rules"}`}
              title={status.classifier.uses_ai ? "Claude classifies ambiguous senders" : "No AI calls are made. Deterministic rules only."}>
              {status.classifier.label}
            </span>
          )}
          {ready && status?.account && <span className="account">{status.account}</span>}
          {status && (
            <button type="button" role="switch" aria-checked={demoOn} className={`switch ${demoOn ? "on" : ""}`}
              onClick={toggleDemo} disabled={switching !== null}
              title="Masks sender names, addresses, subjects and your own name on every screen, for screen recordings">
              <span className="switch-track" aria-hidden><span className="switch-knob" /></span>
              Demo mode
            </button>
          )}
        </div>
      </header>
      {demoOn && (
        <div className="demo-strip" role="status">
          <span className="demo-dot" aria-hidden />
          {switching === true ? "Turning on demo mode…" : "Demo mode: personal data masked"}
          <span className="demo-strip-sub">Sender names, addresses and subjects are masked by Sift's backend before they reach this page.</span>
        </div>
      )}
      {demoErr && <div className="notice notice-error demo-err" role="alert">{demoErr}</div>}
      <main key={ready ? `demo-${demo}` : "pending"}>
        {!ready || !status ? (
          statusError && !status
            ? <div className="notice notice-error">Couldn't reach the Sift backend: {statusError}</div>
            : <div className="loading">{switching !== null ? (switching ? "Turning on demo mode…" : "Turning off demo mode…") : "Loading…"}</div>
        ) : status.connection.state !== "connected" && page !== "account" ? <Connect status={status} onChange={reloadStatus} /> : <>
        {page === "overview" && <Overview go={go} status={status} />}
        {page === "scan" && <ScanScreen go={go} synthetic={!!status?.synthetic} />}
        {page === "recommendations" && <Recommendations selection={selection} setSelection={setSelection} go={go} ai={status?.ai ?? { available: false, reason: null }} status={status} />}
        {page === "confirm" && <Confirm selection={selection} status={status} go={go} />}
        {page === "run" && arg && <RunScreen key={arg} runId={Number(arg)} go={go} onChange={reloadStatus} status={status} />}
        {page === "history" && <History go={go} />}
        {page === "report" && <Report go={go} status={status} />}
        {page === "claude" && <Claude go={go} />}
        {page === "account" && <Account status={status} onChange={reloadStatus} go={go} />}
        {page === "cleanup" && <Cleanup status={status} onChange={reloadStatus} go={go} />}
        {page === "unsubscribe" && <Unsubscribe key={arg ?? "list"} status={status} go={go} batchId={arg ? Number(arg) : undefined} />}
        </>}
      </main>
      <footer className="foot">
        {!ready ? "" : status?.synthetic
          ? "Synthetic mailbox · these results are not from a real account"
          : status?.mode}
        {" · "}No permanent deletes, ever. Every change is labelled and logged.
      </footer>
    </div>
  );
}
