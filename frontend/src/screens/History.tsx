import { api, Run } from "../api";
import { usePoll } from "../hooks";
import { dur, n, seconds } from "../format";

export default function History({ go }: { go: (r: string) => void }) {
  const { data: runs } = usePoll(() => api.get<Run[]>("/runs"), 3000);
  if (!runs) return <div className="loading">Loading…</div>;
  return (
    <div className="stack-lg narrow">
      <div className="page-head"><div><h1>Runs</h1><p className="muted">Every execution and restore, newest first. Restore is available from each run.</p></div></div>
      {runs.length === 0 ? <div className="notice">No runs yet.</div> : (
        <section className="card">
          <table className="table clickable">
            <thead><tr><th>Run</th><th>Kind</th><th>Status</th><th className="num">Attempted</th><th className="num">Confirmed</th><th className="num">Failed</th><th className="num">Skipped</th><th>Started</th><th>Duration</th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} onClick={() => go(`run/${r.id}`)}>
                  <td>#{r.id}</td>
                  <td>{r.kind === "restore" ? `restore of #${r.source_run_id}` : `plan #${r.plan_id}`}</td>
                  <td><span className={`status status-${r.status}`}>{r.status}</span></td>
                  <td className="num">{n(r.attempted)}</td>
                  <td className="num">{n(r.confirmed)}</td>
                  <td className="num">{n(r.failed)}</td>
                  <td className="num">{n(r.skipped + r.already_done)}</td>
                  <td>{r.started_at.replace("T", " ").slice(0, 16)}</td>
                  <td>{dur(seconds(r.started_at, r.finished_at))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
