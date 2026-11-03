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

/** Test scan (capped) or full scan. Gmail allows ~300 metadata reads/minute, so a full scan of a big inbox takes hours. */
export function ScanChoice({ onStart, synthetic }: { onStart: (max: number | null) => void; synthetic: boolean }) {
  return (
    <div className="scan-choice">
      <button className="btn btn-ghost" onClick={() => onStart(500)}>
        Test scan<small>Newest 500 inbox + 500 sent messages · {synthetic ? "seconds" : "about 4 minutes"}</small>
      </button>
      <button className="btn btn-primary" onClick={() => onStart(null)}>
        Full read-only scan<small>{synthetic ? "~45,000 synthetic messages" : "Gmail limits reads to ~300/min; resumable"}</small>
      </button>
    </div>
  );
}

export default function ScanScreen({ go, synthetic }: { go: (r: string) => void; synthetic: boolean }) {
  const { data: scan, reload } = usePoll(() => api.get<Scan>("/scan"), 700);
  const [err, setErr] = useState<string | null>(null);

  const act = async (path: string, body?: unknown) => {
    setErr(null);
    try { await api.post(path, body); await reload(); } catch (e) { setErr((e as Error).message); }
  };
  const start = (max: number | null) => act("/scan", { max_messages: max });
  const [choosing, setChoosing] = useState(false);

  if (!scan) return <div className="loading">Loading…</div>;
  if (scan.status === "none") {
    return (
      <section className="empty-hero">
        <h1>No scan yet</h1>
        <ScanChoice onStart={start} synthetic={synthetic} />
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
          {!running && <button className="btn btn-ghost" onClick={() => setChoosing(!choosing)}>Start a fresh scan</button>}
          {scan.status === "completed" && <button className="btn btn-primary" onClick={() => go("overview")}>See overview</button>}
        </div>
      </div>

      {choosing && !running && <section className="card"><ScanChoice onStart={(m) => { setChoosing(false); start(m); }} synthetic={synthetic} /></section>}

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
          {scan.max_per_phase != null && <span className="tag" style={{ marginLeft: 8 }}>test scan · newest {n(scan.max_per_phase)} of each</span>}
          <span className="muted"> (archived mail isn't scanned)</span>
          {running && scan.eta_s != null && scan.rate_mps != null && (
            <div className="muted">About {dur(scan.eta_s)} left at the measured {scan.rate_mps.toLocaleString("en-GB", { maximumFractionDigits: 1 })} messages/second</div>
          )}
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
