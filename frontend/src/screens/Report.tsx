import { api, Report as ReportData, Status } from "../api";
import { usePoll } from "../hooks";
import { dur, n } from "../format";

/** Before/after report. Every number is a field of GET /api/report; nothing here is computed from other numbers. */
export default function Report({ go, status }: { go: (r: string) => void; status: Status | null }) {
  const { data: r, error } = usePoll(() => api.get<ReportData>("/report"), status?.busy ? 3000 : null, [status?.busy]);

  if (error) return <div className="notice notice-error" role="alert">Couldn't load the report: {error}</div>;
  if (!r) return <div className="loading">Loading…</div>;

  const t = r.totals;
  const u = r.unsubscribe;
  const executes = r.runs.filter((x) => x.kind === "execute");
  const skipReasons = Object.entries(r.skip_reasons);
  const anyUnsub = Object.values(u).some((v) => v > 0);

  return (
    <div className="stack-lg narrow">
      <div className="page-head">
        <div>
          <h1>Report</h1>
          <p className="muted">What Sift has changed so far, read from its run history and append-only audit log.</p>
        </div>
        <div className="row">
          <button className="btn btn-ghost" onClick={() => go("history")}>All runs</button>
        </div>
      </div>

      {status?.synthetic && <div className="notice notice-warn">Synthetic mailbox. These numbers come from the backend, but the mail is fictional.</div>}

      <section className="card">
        <div className="before-after">
          <div>
            <div className="stat-label">Inbox before the first run</div>
            <div className="huge">{r.inbox_before == null ? "—" : n(r.inbox_before)}</div>
          </div>
          <div className="arrow" aria-hidden>→</div>
          <div>
            <div className="stat-label">Inbox now</div>
            <div className="huge">{n(r.inbox_now)}</div>
          </div>
        </div>
        <p className="muted small">
          {r.inbox_before == null
            ? "No cleanup has run yet, so there is no before figure."
            : "Before is the Inbox count when the first plan was built. Now is Sift's record of your Inbox, updated by each confirmed change and each scan; mail that arrived since the last scan isn't in it."}
        </p>

        <div className="stats stats-4">
          <Mini label="Changes attempted" value={n(t.attempted)} hint={status?.synthetic ? "sent to the mailbox" : "sent to Gmail"} />
          <Mini label="Archived, confirmed" value={n(t.archived_confirmed)} tone="good" hint="re-read after the change" />
          <Mini label="Trashed, confirmed" value={n(t.trashed_confirmed)} hint="restorable for about 30 days" />
          <Mini label="Restored, confirmed" value={n(t.restored_confirmed)} hint="back in the Inbox" />
          <Mini label="Skipped at run time" value={n(t.skipped_at_run_time)} hint="re-checked and left alone" />
          <Mini label="Failed" value={n(t.failed)} tone={t.failed ? "bad" : undefined} hint={t.runs_with_errors ? `${n(t.runs_with_errors)} ${t.runs_with_errors === 1 ? "run" : "runs"} with errors` : "no run had errors"} />
          <Mini label="Time spent changing mail" value={dur(t.duration_s)} hint={`${n(executes.length)} cleanup ${executes.length === 1 ? "run" : "runs"}`} />
        </div>
        <p className="muted small" style={{ marginTop: 12 }}>
          Attempted counts changes Sift sent{status?.synthetic ? "" : " to Gmail"}. Confirmed counts only messages Sift re-read afterwards and found in the new state.
          Totals for skipped, failed and time cover cleanup runs; restores are listed below.
        </p>
      </section>

      <div className="grid-2">
        <section className="card">
          <h2>Skipped at run time <span className="count">{n(t.skipped_at_run_time)}</span></h2>
          {skipReasons.length === 0 ? <p className="muted">Nothing was skipped.</p> : (
            <>
              <p className="muted small" style={{ marginBottom: 8 }}>Right before each batch Sift re-checks every message and leaves out anything that changed. Top reasons, all runs:</p>
              <table className="table">
                <tbody>
                  {skipReasons.map(([reason, k]) => <tr key={reason}><td>{reason}</td><td className="num">{n(k)}</td></tr>)}
                </tbody>
              </table>
            </>
          )}
        </section>
        <section className="card">
          <h2>Failures <span className="count">{n(t.failed)}</span></h2>
          {r.errors.length === 0 ? <p className="muted">No message failed.</p> : (
            <>
              <p className="muted small" style={{ marginBottom: 8 }}>Most frequent errors. Failed messages were not changed, or the change couldn't be confirmed.</p>
              <table className="table">
                <thead><tr><th>Run</th><th>Action</th><th>Error</th><th className="num">Messages</th></tr></thead>
                <tbody>
                  {r.errors.map((e, i) => (
                    <tr key={i}>
                      <td><a href={`#/run/${e.run_id}`}>#{e.run_id}</a></td>
                      <td>{e.action}</td>
                      <td className="small">{e.detail}</td>
                      <td className="num">{n(e.n)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </section>
      </div>

      <section className="card">
        <h2>Runs <span className="count">{n(r.runs.length)}</span></h2>
        {r.runs.length === 0 ? <p className="muted">No runs yet. Approve a plan from <a href="#/recommendations">Recommendations</a> to start one.</p> : (
          <div className="table-scroll">
            <table className="table clickable">
              <thead>
                <tr>
                  <th>Run</th><th>Kind</th><th>Status</th><th>Label</th><th>Started (UTC)</th><th className="num">Duration</th>
                  <th className="num">Attempted</th><th className="num">Confirmed</th><th className="num">Failed</th><th className="num">Skipped</th>
                </tr>
              </thead>
              <tbody>
                {r.runs.slice().reverse().map((x) => (
                  <tr key={x.id} onClick={() => go(`run/${x.id}`)}>
                    <td><a href={`#/run/${x.id}`} onClick={(e) => e.stopPropagation()}>#{x.id}</a></td>
                    <td className="nowrap">{x.kind === "restore" ? `restore of #${x.source_run_id}` : `plan #${x.plan_id}`}</td>
                    <td><span className={`status status-${x.status}`}>{x.status}</span></td>
                    <td>{x.label_name ? <code>{x.label_name}</code> : <span className="muted">—</span>}</td>
                    <td className="nowrap">{x.started_at.replace("T", " ").slice(0, 16)}</td>
                    <td className="num">{x.duration_s == null ? (x.status === "running" ? "running" : "—") : dur(x.duration_s)}</td>
                    <td className="num">{n(x.attempted)}</td>
                    <td className="num"><strong>{n(x.confirmed)}</strong></td>
                    <td className={`num ${x.failed ? "bad" : ""}`}>{n(x.failed)}</td>
                    <td className="num">{n(x.skipped)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2>Unsubscribe</h2>
        <p className="muted small" style={{ marginBottom: 4 }}>
          Separate from cleanup. Attempted means Sift started a single RFC 8058 POST (logged before it is sent).
          Accepted means the sender's server answered HTTP 2xx; it can't prove the sender will stop emailing.
        </p>
        {!anyUnsub ? <p className="muted" style={{ marginTop: 10 }}>No unsubscribes yet. See <a href="#/unsubscribe">Unsubscribe</a>.</p> : (
          <div className="stats stats-4">
            <Mini label="Attempted" value={n(u.attempted)} hint="POST requests started" />
            <Mini label="Accepted (HTTP 2xx)" value={n(u.accepted)} tone="good" />
            <Mini label="Not confirmed" value={n(u.not_confirmed)} hint="e.g. a redirect, not followed" />
            <Mini label="Failed" value={n(u.failed)} tone={u.failed ? "bad" : undefined} hint="network error or non-2xx answer" />
            <Mini label="Refused by Sift" value={n(u.refused)} hint="unsafe link, never sent" />
            <Mini label="Manual, not sent" value={n(u.manual)} hint="mailto only: you do these" />
            <Mini label="Simulated" value={n(u.simulated)} hint="synthetic senders, nothing sent" />
          </div>
        )}
      </section>
    </div>
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
