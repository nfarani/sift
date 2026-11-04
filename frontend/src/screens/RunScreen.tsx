import { useEffect, useState } from "react";
import { api, Run, Status } from "../api";
import { usePoll } from "../hooks";
import { dur, n, seconds } from "../format";

/** Re-renders every second while `on`, so elapsed time is a real clock. It never moves a count. */
function useClock(on: boolean) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!on) return;
    const t = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, [on]);
}

export default function RunScreen({ runId, go, onChange, status }: { runId: number; go: (r: string) => void; onChange: () => void; status: Status | null }) {
  // Poll once a second while the run is live, then stop: the finished run doesn't change.
  const [live, setLive] = useState(true);
  const { data: run, reload } = usePoll(() => api.get<Run>(`/runs/${runId}`), live ? 1000 : null, [runId]);
  const [err, setErr] = useState<string | null>(null);
  const [askCancel, setAskCancel] = useState(false);
  const running = run?.status === "running";
  useEffect(() => { if (run && run.status !== "running") setLive(false); }, [run]);
  useClock(running);

  if (!run) return <div className="loading">Loading…</div>;
  const processed = run.confirmed + run.failed + run.skipped + run.already_done;
  const remaining = Math.max(0, run.total - processed);
  const isRestore = run.kind === "restore";
  const stopping = running && !!run.cancel_requested;
  const w = (k: number) => (run.total ? `${Math.min(100, (100 * k) / run.total)}%` : "0%");

  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try { await fn(); onChange(); } catch (e) { setErr((e as Error).message); }
  };
  const cancel = () => act(async () => { await api.post(`/runs/${run.id}/cancel`); setAskCancel(false); await reload(); });

  return (
    <div className="stack-lg narrow">
      <div className="page-head">
        <div>
          <h1>{isRestore ? `Restore of run #${run.source_run_id}` : `Run #${run.id}`}{" "}
            <span className={`status status-${stopping ? "cancelled" : run.status}`}>{stopping ? "stopping" : run.status}</span></h1>
          <p className="muted">
            {run.label_name && <>Label <code>{run.label_name}</code> · </>}
            started {run.started_at.slice(11, 19)} UTC
            {run.cap != null && !isRestore && <> · capped at {n(run.cap)}</>}
          </p>
        </div>
        <div className="row">
          {running && !askCancel && !stopping && <button className="btn btn-ghost" onClick={() => setAskCancel(true)}>Cancel run…</button>}
        </div>
      </div>

      {askCancel && running && !stopping && (
        <section className="card cancel-panel" aria-labelledby="cancel-h">
          <h2 id="cancel-h">Cancel this {isRestore ? "restore" : "run"}?</h2>
          <ul className="explain-list">
            <li>Sift finishes the batch it is working on (up to 500 messages), then stops. Nothing after that batch is sent{status?.synthetic ? "" : " to Gmail"}.</li>
            {isRestore ? (
              <li>Messages already restored stay in your Inbox. The rest stay where they are, with their Sift label; you can start the restore again from run #{run.source_run_id}.</li>
            ) : (
              <>
                <li><strong>Messages not reached yet stay untouched</strong> in your Inbox.</li>
                <li>Messages already changed keep their label{run.label_name && <> <code>{run.label_name}</code></>} and can be restored from this page.</li>
                <li>The plan's approval is withdrawn. To do the rest later, build and approve a new plan.</li>
              </>
            )}
          </ul>
          <div className="row" style={{ marginTop: 14 }}>
            <button className="btn btn-danger" onClick={cancel}>Stop after this batch</button>
            <button className="btn btn-ghost" onClick={() => setAskCancel(false)} autoFocus>Keep running</button>
          </div>
        </section>
      )}

      <section className="card">
        <div className="run-op" aria-live="polite">
          <span className="current-op">
            {stopping ? "Stopping after the current batch…" : running ? run.current_op
              : run.status === "cancelled"
                ? (isRestore ? "Cancelled. Messages already restored stay in your Inbox." : "Cancelled by you. Messages not reached were left untouched; the approval is withdrawn, so build a new plan to continue.")
                : "Finished"}
          </span>
          <span className="muted small">{running ? "Elapsed" : "Took"} {dur(seconds(run.started_at, run.finished_at))}</span>
        </div>
        <div className="progress progress-stack" role="progressbar" aria-label="Messages processed"
          aria-valuemin={0} aria-valuemax={run.total} aria-valuenow={processed}>
          <div className="seg-attempted" style={{ width: w(run.attempted) }} />
          <div className="seg seg-confirmed" style={{ width: w(run.confirmed) }} />
          <div className="seg seg-skipped" style={{ width: w(run.skipped + run.already_done) }} />
          <div className="seg seg-failed" style={{ width: w(run.failed) }} />
        </div>
        <div className="progress-caption row">
          <span>{n(processed)} of {n(run.total)} processed</span>
          <span className="legend"><i className="seg-confirmed" />confirmed</span>
          {running && <span className="legend"><i className="seg-attempted" />sent, awaiting confirmation</span>}
          <span className="legend"><i className="seg-skipped" />skipped or already done</span>
          {run.failed > 0 && <span className="legend"><i className="seg-failed" />failed</span>}
        </div>
        <div className="stats stats-5">
          <Mini label="Attempted" value={n(run.attempted)} hint={status?.synthetic ? "sent to the mailbox" : "sent to Gmail"} />
          <Mini label="Confirmed" value={n(run.confirmed)} tone="good" hint="re-read in the new state" />
          <Mini label="Failed" value={n(run.failed)} tone={run.failed ? "bad" : undefined} />
          <Mini label="Skipped" value={n(run.skipped + run.already_done)} hint="re-checked and left alone" />
          <Mini label={running ? "Remaining" : "Not reached"} value={n(remaining)} hint={running ? undefined : remaining ? "left untouched" : undefined} />
        </div>
        {run.error && <div className="notice notice-error">{run.error}</div>}
        {running && err && <div className="notice notice-error" role="alert">{err}</div>}
      </section>

      {!running && <Results run={run} status={status} err={err} onRestore={() => act(async () => { const r = await api.post<Run>(`/runs/${run.id}/restore`); go(`run/${r.id}`); })}
        onUnlock={() => act(() => api.post("/cleanup/unlock"))} onContinue={() => act(async () => {
          const r = await api.post<Run>(`/plans/${run.plan_id}/execute`); go(`run/${r.id}`);
        })} go={go} />}
    </div>
  );
}

function Results({ run, status, err, onRestore, onUnlock, onContinue, go }: {
  run: Run; status: Status | null; err: string | null; onRestore: () => void; onUnlock: () => void; onContinue: () => void; go: (r: string) => void;
}) {
  const archived = run.audit_counts["archive:confirmed"] ?? 0;
  const trashed = run.audit_counts["trash:confirmed"] ?? 0;
  const isRestore = run.kind === "restore";
  const restored = run.restores.some((r) => r.status === "completed" && r.failed === 0);
  const locked = !!status && !status.full_runs_unlocked;
  const canUnlock = isRestore && run.status === "completed" && run.failed === 0 && locked;
  const restoreIncomplete = isRestore && locked && !canUnlock;
  const awaitingRestore = !isRestore && locked && run.restorable > 0 && !restored;

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
              <tr><td>Changes attempted (sent to Gmail)</td><td className="num">{n(run.attempted)}</td></tr>
              <tr><td>Confirmed by Gmail</td><td className="num"><strong>{n(run.confirmed)}</strong></td></tr>
              <tr className="sub"><td>Archived + labelled</td><td className="num">{n(archived)}</td></tr>
              <tr className="sub"><td>Moved to Trash</td><td className="num">{n(trashed)}</td></tr>
              <tr><td>Already in the target state</td><td className="num">{n(run.already_done)}</td></tr>
              <tr><td>Subscriptions cancelled</td><td className="num muted">separate: <a href="#/unsubscribe">Unsubscribe</a></td></tr>
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
      {awaitingRestore && (
        <div className="notice notice-cap" style={{ marginTop: 16 }}>
          <strong>Next: prove recovery.</strong> Check these messages in Gmail{run.label_name && <> under the label <code>{run.label_name}</code></>}.
          Then restore all {n(run.restorable)} of them. Full runs unlock only after a restore that completes with no errors.
        </div>
      )}
      {canUnlock && (
        <div className="notice notice-cap" style={{ marginTop: 16 }}>
          <strong>Restore verified.</strong> All {n(run.confirmed + run.already_done)} messages from run #{run.source_run_id} are back in the Inbox, with no errors.
          Check them in Gmail if you like. Unlocking lifts the {status ? n(status.first_run_cap) : ""}-message cap and the one-recommendation rule.
          Every run still needs an approved plan, and every change stays labelled and reversible.
        </div>
      )}
      {restoreIncomplete && (
        <div className="notice notice-warn" style={{ marginTop: 16 }}>
          This restore {run.status === "completed" ? `had ${n(run.failed)} ${run.failed === 1 ? "error" : "errors"}` : `is ${run.status}`}, so full runs stay locked.
          Restore run #{run.source_run_id} again from its page.
        </div>
      )}
      {isRestore && status?.full_runs_unlocked && (
        <div className="notice notice-cap" style={{ marginTop: 16 }} role="status">Full runs are unlocked.</div>
      )}
      {err && <div className="notice notice-error" role="alert" style={{ marginTop: 16 }}>{err}</div>}
      <div className="row end" style={{ marginTop: 16 }}>
        {restoreIncomplete && run.source_run_id != null && <button className="btn btn-ghost" onClick={() => go(`run/${run.source_run_id}`)}>Open run #{run.source_run_id}</button>}
        {!isRestore && run.restorable > 0 && !restored && <button className="btn btn-ghost" onClick={onRestore}>Restore these {n(run.restorable)} messages</button>}
        {canUnlock && <button className="btn btn-primary" onClick={onUnlock}>Unlock full runs</button>}
        {status?.full_runs_unlocked && run.plan?.status === "partial" && (run.plan_remaining ?? 0) > 0 && <button className="btn btn-primary" onClick={onContinue}>Run the remaining {n(run.plan_remaining)} in plan #{run.plan_id}</button>}
        {status?.full_runs_unlocked && isRestore && !(run.plan?.status === "partial" && (run.plan_remaining ?? 0) > 0) && <button className="btn btn-ghost" onClick={() => go("recommendations")}>Back to recommendations</button>}
        <a className="btn btn-link" href={`/api/runs/${run.id}/audit`} target="_blank" rel="noreferrer">Audit log (JSON)</a>
        <button className="btn btn-ghost" onClick={() => go("report")}>See the full report</button>
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
