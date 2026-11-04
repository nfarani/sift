import { useState } from "react";
import { AiEstimate, AiRun, AiStatus, api } from "../api";
import { usePoll } from "../hooks";
import { n } from "../format";

const usd = (x: number) => `$${x.toFixed(x < 1 ? 3 : 2)}`;

export default function Claude({ go }: { go: (r: string) => void }) {
  const { data: st, reload } = usePoll(() => api.get<AiStatus>("/ai"), 1500);
  const [est, setEst] = useState<AiEstimate | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!st) return <div className="loading">Loading…</div>;
  const run: AiRun | null = st.last_run;
  const running = run?.status === "running";

  const estimate = async () => {
    setErr(null); setBusy(true);
    try { setEst(await api.post<AiEstimate>("/ai/estimate")); } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };
  const start = async (withinCap: boolean) => {
    if (!est) return;
    setErr(null);
    try {
      await api.post("/ai/run", { within_cap: withinCap, approved_estimate_usd: withinCap ? Math.min(est.usd, est.cap_usd) : est.usd });
      setEst(null); reload();
    } catch (e) { setErr((e as Error).message); }
  };

  return (
    <div className="stack-lg narrow">
      <div className="page-head">
        <div>
          <h1>Claude classification</h1>
          <p className="muted">Claude re-checks every sender the rules didn't firmly settle, including ones they may have misfiled. Senders you've corresponded with and mostly-protected bank/security senders are never sent. Protection rules always run first and always win, and Claude can't downgrade mail the rules wanted kept without asking you.</p>
        </div>
      </div>

      {!st.available && (
        <div className="notice notice-warn">
          <strong>Claude isn't available:</strong> {st.reason}. Sift keeps working with rules only and never shows guessed results as AI.
        </div>
      )}

      {running && run && (
        <section className="card">
          <h2>Classifying…</h2>
          <div className="current-op">{run.current_op}</div>
          <div className="progress"><div className="progress-fill live" style={{ width: `${run.groups_total ? (100 * run.groups_done) / run.groups_total : 0}%` }} /></div>
          <div className="stats stats-4">
            <Mini label="Senders labelled" value={`${n(run.groups_done)} / ${n(run.groups_total)}`} />
            <Mini label="Spent so far" value={usd(run.cost_usd)} hint={`cap ${usd(run.cap_usd)}`} />
            <Mini label="Senders in rejected batches" value={n(run.groups_failed)} hint="kept their rules label" />
            <Mini label="Second opinions" value={n(run.groups_reviewed)} />
          </div>
          <div className="row end"><button className="btn btn-ghost" onClick={() => api.post("/ai/run/cancel").then(reload)}>Cancel</button></div>
        </section>
      )}

      {!running && run && (
        <section className="card">
          <h2>Last run <span className={`status status-${run.status === "completed" ? "completed" : run.status === "failed" ? "failed" : "partial"}`}>{run.status.replace(/_/g, " ")}</span></h2>
          <table className="table"><tbody>
            <tr><td>Senders labelled by Claude</td><td className="num">{n(run.groups_done)} of {n(run.groups_total)}</td></tr>
            <tr><td>Got a second opinion from the stronger model</td><td className="num">{n(run.groups_reviewed)}</td></tr>
            <tr><td>Senders in rejected or failed first-pass batches (kept rules label)</td><td className="num">{n(run.groups_failed)}</td></tr>
            <tr><td>Cost</td><td className="num"><strong>{usd(run.cost_usd)}</strong> <span className="muted">(cap {usd(run.cap_usd)}, estimated {usd(run.estimate.usd)})</span></td></tr>
          </tbody></table>
          {run.status === "stopped_at_cap" && <div className="notice notice-cap">Stopped at your spend cap. Unlabelled senders keep their rules label; run again later to continue (labels so far are kept and not paid for twice).</div>}
          {run.error && <div className="notice notice-error">{run.error}</div>}
          <div className="row end"><button className="btn btn-primary" onClick={() => go("recommendations")}>See recommendations</button></div>
        </section>
      )}

      {st.available && !running && (
        <section className="card">
          <h2>Run Claude</h2>
          {!est ? (
            <>
              <p className="muted">First, Sift estimates the cost on this Mac. Nothing is sent to Anthropic until you approve.</p>
              <div className="row" style={{ marginTop: 12 }}><button className="btn btn-primary" disabled={busy} onClick={estimate}>Estimate cost</button></div>
            </>
          ) : est.groups === 0 ? (
            <p>Nothing to classify: every sender is settled by rules or already labelled by Claude.</p>
          ) : (
            <>
              <table className="table"><tbody>
                <tr><td>Senders to classify</td><td className="num">{n(est.groups)} <span className="muted">({n(est.messages_in_candidates)} messages)</span></td></tr>
                <tr><td>First pass · <code>{est.bulk.model}</code></td><td className="num">{n(est.bulk.requests)} requests · {n(est.bulk.input_tokens)} in / {n(est.bulk.output_tokens)} out · {usd(est.bulk.usd)}</td></tr>
                <tr><td>Second opinion · <code>{est.review.model}</code> <span className="muted">(assumes {Math.round(est.review_share_assumed * 100)}% need it)</span></td><td className="num">{n(est.review.requests)} requests · {n(est.review.input_tokens)} in / {n(est.review.output_tokens)} out · {usd(est.review.usd)}</td></tr>
                <tr><td><strong>Estimated total</strong></td><td className="num"><strong>{usd(est.usd)}</strong> <span className="muted">· your cap {usd(est.cap_usd)}</span></td></tr>
              </tbody></table>
              <p className="muted small" style={{ marginTop: 10 }}>Sent per sender: {est.sends}. Subjects and snippets come only from unprotected messages. Estimates are conservative; the run stops before any request that could cross the cap.</p>
              {est.usd > est.cap_usd && (
                <div className="notice notice-warn" style={{ marginTop: 12 }}>
                  The full run is over your cap. Running within the cap covers the biggest <strong>{n(est.within_cap.groups)}</strong> senders
                  ({n(est.within_cap.messages)} messages); the rest keep their rules label. Or raise <code>SIFT_AI_SPEND_CAP_USD</code> in <code>.env</code> and restart.
                </div>
              )}
              <div className="row end" style={{ marginTop: 14 }}>
                <button className="btn btn-link" onClick={() => setEst(null)}>Cancel</button>
                {est.usd > est.cap_usd
                  ? <button className="btn btn-primary" onClick={() => start(true)}>Approve: spend up to {usd(est.cap_usd)}</button>
                  : <button className="btn btn-primary" onClick={() => start(false)}>Approve: about {usd(est.usd)}</button>}
              </div>
            </>
          )}
        </section>
      )}
      {err && <div className="notice notice-error">{err}</div>}

      <section className="card">
        <h2>Usage to date <span className="count">{usd(st.usage.total_usd)}</span></h2>
        {st.usage.by.length === 0 ? <p className="muted">No Claude calls yet.</p> : (
          <table className="table">
            <thead><tr><th>Purpose</th><th>Model</th><th className="num">Calls</th><th className="num">Input tokens</th><th className="num">Output tokens</th><th className="num">Rejected</th><th className="num">Cost</th></tr></thead>
            <tbody>{st.usage.by.map((u) => (
              <tr key={u.purpose + u.model}><td>{u.purpose.replace("_", " ")}</td><td><code>{u.model}</code></td><td className="num">{n(u.calls)}</td><td className="num">{n(u.i)}</td><td className="num">{n(u.o)}</td><td className="num">{n(u.rejected)}</td><td className="num">{usd(u.usd)}</td></tr>
            ))}</tbody>
          </table>
        )}
        <p className="muted small" style={{ marginTop: 8 }}>{n(st.labelled_senders)} senders have a saved Claude label; rescans reuse them at no cost.</p>
      </section>
    </div>
  );
}

function Mini({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return <div className="stat stat-mini"><div className="stat-label">{label}</div><div className="stat-value">{value}</div>{hint && <div className="stat-hint">{hint}</div>}</div>;
}
