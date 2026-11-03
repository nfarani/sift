import { useState } from "react";
import { api, Scan } from "../api";
import { usePoll } from "../hooks";
import { dur, n } from "../format";

const PHASES: [Scan["phase"], string][] = [
  ["sent", "Reading your sent mail"],
  ["inbox", "Reading inbox headers"],
  ["analyse", "Grouping, protecting, classifying"],
  ["done", "Done"],
];

export default function ScanScreen({ go }: { go: (r: string) => void }) {
  const { data: scan, reload } = usePoll(() => api.get<Scan>("/scan"), 700);
  const [err, setErr] = useState<string | null>(null);

  const act = async (path: string) => {
    setErr(null);
    try { await api.post(path); await reload(); } catch (e) { setErr((e as Error).message); }
  };

  if (!scan) return <div className="loading">Loading…</div>;
  if (scan.status === "none") {
    return (
      <section className="empty-hero">
        <h1>No scan yet</h1>
        <button className="btn btn-primary btn-lg" onClick={() => act("/scan")}>Start read-only scan</button>
      </section>
    );
  }

  const running = scan.status === "running";
  const phaseIdx = PHASES.findIndex(([p]) => p === scan.phase);
  const frac = scan.total_estimate ? Math.min(1, scan.messages_scanned / scan.total_estimate) : 0;

  return (
    <div className="stack-lg narrow">
      <div className="page-head">
        <div>
          <h1>{running ? "Scanning…" : scan.status === "completed" ? "Scan complete" : `Scan ${scan.status}`}</h1>
          <p className="muted">Read-only. Headers and labels only; nothing is marked as read.</p>
        </div>
        <div className="row">
          {running && <button className="btn btn-ghost" onClick={() => act("/scan/cancel")}>Cancel</button>}
          {scan.resumable && <button className="btn btn-primary" onClick={() => act("/scan/resume")}>Resume from checkpoint</button>}
          {!running && <button className="btn btn-ghost" onClick={() => act("/scan")}>Start a fresh scan</button>}
          {scan.status === "completed" && <button className="btn btn-primary" onClick={() => go("overview")}>See overview</button>}
        </div>
      </div>

      <section className="card">
        <ol className="phases">
          {PHASES.map(([p, label], i) => (
            <li key={p} className={i < phaseIdx || scan.phase === "done" ? "done" : i === phaseIdx ? (running ? "now" : "paused") : ""}>
              <span className="dot" />{label}
            </li>
          ))}
        </ol>
        <div className="progress"><div className={`progress-fill ${running ? "live" : ""}`} style={{ width: `${(scan.phase === "done" ? 1 : frac) * 100}%` }} /></div>
        <div className="progress-caption">
          {n(scan.messages_scanned)} of {n(scan.total_estimate)} messages in Inbox and Sent
          <span className="muted"> (archived mail isn't scanned)</span>
        </div>
        <div className="stats stats-4">
          <Mini label="Messages scanned" value={n(scan.messages_scanned)} />
          <Mini label="Senders discovered" value={n(scan.senders_discovered)} />
          <Mini label="Categories" value={scan.categories_detected ? n(scan.categories_detected) : "—"} />
          <Mini label="Elapsed" value={dur(scan.elapsed_s)} />
        </div>
      </section>

      {(scan.errors > 0 || err) && (
        <section className="card">
          <h2>Errors and retries <span className="count">{n(scan.errors)}</span></h2>
          {err && <div className="notice notice-error">{err}</div>}
          <ul className="log">
            {scan.error_log.slice().reverse().map((e, i) => (
              <li key={i}><time>{e.at.slice(11, 19)}</time> {e.error}</li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function Mini({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat stat-mini">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}
