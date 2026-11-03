import { useState } from "react";
import { api, Run, Status } from "../api";
import { usePoll } from "../hooks";
import { dur, n, seconds } from "../format";

export default function RunScreen({ runId, go, onChange, status }: { runId: number; go: (r: string) => void; onChange: () => void; status: Status | null }) {
  const { data: run, reload } = usePoll(() => api.get<Run>(`/runs/${runId}`), 600, [runId]);
  const [err, setErr] = useState<string | null>(null);

  if (!run) return <div className="loading">Loading…</div>;
  const running = run.status === "running";
  const processed = run.confirmed + run.failed + run.skipped + run.already_done;
  const remaining = Math.max(0, run.total - processed);
  const isRestore = run.kind === "restore";

  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try { await fn(); onChange(); } catch (e) { setErr((e as Error).message); }
  };

  return (
    <div className="stack-lg narrow">
      <div className="page-head">
        <div>
          <h1>{isRestore ? `Restore of run #${run.source_run_id}` : `Run #${run.id}`}{" "}
            <span className={`status status-${run.status}`}>{run.status}</span></h1>
          <p className="muted">
            {run.label_name && <>Label <code>{run.label_name}</code> · </>}
            started {run.started_at.slice(11, 19)} UTC · {dur(seconds(run.started_at, run.finished_at))}
            {run.cap != null && !isRestore && <> · capped at {n(run.cap)}</>}
          </p>
        </div>
        <div className="row">
          {running && <button className="btn btn-ghost" onClick={() => act(() => api.post(`/runs/${run.id}/cancel`).then(reload))}>Cancel</button>}
        </div>
      </div>

      <section className="card">
        <div className="current-op">{running ? run.current_op : run.status === "cancelled" ? "Cancelled. Unprocessed messages stay pending in the plan." : "Finished"}</div>
        <div className="progress"><div className={`progress-fill ${running ? "live" : ""}`} style={{ width: `${run.total ? (100 * processed) / run.total : 100}%` }} /></div>
        <div className="stats stats-5">
          <Mini label="Processed" value={n(processed)} hint={`of ${n(run.total)}`} />
          <Mini label="Confirmed" value={n(run.confirmed)} tone="good" hint={`of ${n(run.attempted)} attempted`} />
          <Mini label="Failed" value={n(run.failed)} tone={run.failed ? "bad" : undefined} />
          <Mini label="Skipped" value={n(run.skipped + run.already_done)} hint="re-checked and left alone" />
          <Mini label="Remaining" value={n(remaining)} />
        </div>
        {run.error && <div className="notice notice-error">{run.error}</div>}
        {err && <div className="notice notice-error">{err}</div>}
      </section>

      {!running && <Results run={run} status={status} onRestore={() => act(async () => { const r = await api.post<Run>(`/runs/${run.id}/restore`); go(`run/${r.id}`); })}
        onUnlock={() => act(() => api.post("/cleanup/unlock"))} onContinue={() => act(async () => {
          const r = await api.post<Run>(`/plans/${run.plan_id}/execute`); go(`run/${r.id}`);
        })} go={go} />}
    </div>
  );
}

function Results({ run, status, onRestore, onUnlock, onContinue, go }: {
  run: Run; status: Status | null; onRestore: () => void; onUnlock: () => void; onContinue: () => void; go: (r: string) => void;
}) {
  const archived = run.audit_counts["archive:confirmed"] ?? 0;
  const trashed = run.audit_counts["trash:confirmed"] ?? 0;
  const isRestore = run.kind === "restore";
  const restored = run.restores.some((r) => r.status === "completed");
  const canUnlock = isRestore && run.status === "completed" && run.failed === 0 && status && !status.full_runs_unlocked;

  return (
    <section className="card">
      <h2>Results</h2>
      {status?.synthetic && <p className="muted small">Synthetic mailbox. These numbers come from the backend, but the mail is fictional.</p>}
      <div className="before-after">
        <div><div className="stat-label">Inbox before plan</div><div className="huge">{n(run.plan?.inbox_before)}</div></div>
        <div className="arrow">→</div>
        <div><div className="stat-label">Inbox now</div><div className="huge">{n(run.inbox_now)}</div></div>
      </div>
      <table className="table">
        <tbody>
          {isRestore ? (
            <>
              <tr><td>Restore attempted</td><td className="num">{n(run.attempted)}</td></tr>
              <tr><td>Restored and confirmed back in Inbox</td><td className="num"><strong>{n(run.confirmed)}</strong></td></tr>
              <tr><td>Already back in Inbox</td><td className="num">{n(run.already_done)}</td></tr>
            </>
          ) : (
            <>
              <tr><td>Changes attempted</td><td className="num">{n(run.attempted)}</td></tr>
              <tr><td>Archived (confirmed)</td><td className="num"><strong>{n(archived)}</strong></td></tr>
              <tr><td>Moved to Trash (confirmed)</td><td className="num"><strong>{n(trashed)}</strong></td></tr>
              <tr><td>Already in the target state</td><td className="num">{n(run.already_done)}</td></tr>
              <tr><td>Subscriptions cancelled</td><td className="num muted">not available yet</td></tr>
            </>
          )}
          <tr><td>Skipped at re-check</td><td className="num">{n(run.skipped)}</td></tr>
          {Object.entries(run.skip_reasons).map(([r, k]) => (
            <tr key={r} className="sub"><td>{r}</td><td className="num">{n(k)}</td></tr>
          ))}
          <tr><td>Errors</td><td className={`num ${run.failed ? "bad" : ""}`}>{n(run.failed)}</td></tr>
          {!isRestore && run.plan_remaining != null && <tr><td>Still pending in plan #{run.plan_id}</td><td className="num">{n(run.plan_remaining)}</td></tr>}
          <tr><td>Duration</td><td className="num">{dur(seconds(run.started_at, run.finished_at))}</td></tr>
        </tbody>
      </table>
      {run.failures.length > 0 && (
        <ul className="log">{run.failures.map((f) => <li key={f.message_id}>{f.message_id}: {f.detail}</li>)}</ul>
      )}
      <div className="row end">
        {!isRestore && run.confirmed > 0 && !restored && <button className="btn btn-ghost" onClick={onRestore}>Restore these {n(run.confirmed)} messages</button>}
        {canUnlock && <button className="btn btn-primary" onClick={onUnlock}>Restore verified · unlock full runs</button>}
        {status?.full_runs_unlocked && (run.plan_remaining ?? 0) > 0 && <button className="btn btn-primary" onClick={onContinue}>Run the remaining {n(run.plan_remaining)} in plan #{run.plan_id}</button>}
        {status?.full_runs_unlocked && isRestore && !(run.plan_remaining ?? 0) && <button className="btn btn-ghost" onClick={() => go("recommendations")}>Back to recommendations</button>}
        <a className="btn btn-link" href={`/api/runs/${run.id}/audit`} target="_blank" rel="noreferrer">Audit log (JSON)</a>
      </div>
    </section>
  );
}

function Mini({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "good" | "bad" }) {
  return (
    <div className={`stat stat-mini ${tone ? `stat-${tone}` : ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}
