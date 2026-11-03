import { useEffect, useRef, useState } from "react";
import { api, Plan, Run, Status } from "../api";
import { ageLabel, day, n, plural } from "../format";
import { Selection } from "../selection";

const ACTION_LABEL: Record<string, string> = { archive: "Archive + label", trash: "Move to Trash" };

export default function Confirm({ selection, status, go }: { selection: Selection; status: Status | null; go: (r: string) => void }) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);

  const items = Object.entries(selection)
    .filter(([, c]) => c.selected)
    .map(([rec_id, c]) => ({ rec_id, action: c.action, older_than_days: c.older_than_days, excluded_senders: c.excluded_senders }));
  const key = JSON.stringify(items);

  // One draft per distinct selection (StrictMode re-runs effects; don't create duplicate drafts).
  const built = useRef<string | null>(null);
  useEffect(() => {
    if (items.length === 0 || built.current === key) return;
    built.current = key;
    setPlan(null); setErr(null); setAck(false);
    api.post<Plan>("/plans", { items }).then(setPlan, (e) => setErr(e.message));
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  if (items.length === 0) {
    return (
      <section className="empty-hero">
        <h1>Nothing selected</h1>
        <p>Choose recommendations first.</p>
        <button className="btn btn-primary" onClick={() => go("recommendations")}>Back to recommendations</button>
      </section>
    );
  }
  if (err) return <div className="stack narrow"><div className="notice notice-error">{err}</div><button className="btn btn-ghost" onClick={() => go("recommendations")}>Back</button></div>;
  if (!plan || !status) return <div className="loading">Building the exact plan…</div>;

  const capped = !status.full_runs_unlocked && plan.total > status.first_run_cap;
  const thisRun = capped ? status.first_run_cap : plan.total;
  const hasTrash = (plan.summary.by_action.trash ?? 0) > 0;

  const approveAndRun = async () => {
    setBusy(true); setErr(null);
    try {
      await api.post(`/plans/${plan.id}/approve`, { confirm_hash: plan.ids_hash });
      const run = await api.post<Run>(`/plans/${plan.id}/execute`);
      go(`run/${run.id}`);
    } catch (e) {
      setErr((e as Error).message); setBusy(false);
    }
  };

  return (
    <div className="stack-lg narrow">
      <div className="page-head">
        <div>
          <h1>Confirm plan #{plan.id}</h1>
          <p className="muted">This is the exact, frozen list of messages. Changing anything creates a new plan.</p>
        </div>
      </div>

      <section className="card confirm">
        <div className="confirm-total">
          <div>
            <div className="stat-label">Messages in this plan</div>
            <div className="huge">{n(plan.total)}</div>
          </div>
          <div className="confirm-actions">
            {Object.entries(plan.summary.by_action).map(([a, k]) => (
              <div key={a} className={`pill pill-${a}`}>{ACTION_LABEL[a] ?? a}: <strong>{n(k)}</strong></div>
            ))}
          </div>
        </div>

        <table className="table">
          <thead><tr><th>Recommendation</th><th>Action</th><th>Age filter</th><th className="num">Messages</th><th className="num">Protected, excluded</th><th className="num">You excluded</th><th className="num">Too recent</th></tr></thead>
          <tbody>
            {plan.summary.items.map((it) => (
              <tr key={it.rec_id}>
                <td>{it.rec_id.replace("_", " ")}</td>
                <td>{ACTION_LABEL[it.action]}</td>
                <td>{ageLabel(it.older_than_days)}</td>
                <td className="num"><strong>{n(it.count)}</strong></td>
                <td className="num keep">{n(it.protected_excluded)}</td>
                <td className="num">{n(it.user_excluded)}{it.excluded_senders.length > 0 && <span className="muted"> ({plural(it.excluded_senders.length, "sender")})</span>}</td>
                <td className="num">{n(it.too_recent)}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="protection-summary">
          <h4>Protection exclusions ({n(plan.summary.protected_excluded)} messages left untouched)</h4>
          <ul className="inline-list">
            {Object.entries(plan.summary.items.reduce<Record<string, number>>((acc, it) => {
              for (const [r, k] of Object.entries(it.protected_by_reason)) acc[r] = (acc[r] ?? 0) + k;
              return acc;
            }, {})).sort((a, b) => b[1] - a[1]).map(([r, k]) => <li key={r}><strong>{n(k)}</strong> {r}</li>)}
          </ul>
        </div>

        <h4>Most recent messages in the plan</h4>
        <ul className="samples">
          {plan.samples.map((m) => (
            <li key={m.id}><span className="s-subj">{m.subject}</span><span className="s-from">{m.from_name} · {day(m.internal_date)} · {ACTION_LABEL[m.action]}</span></li>
          ))}
        </ul>
      </section>

      {capped && (
        <div className="notice notice-cap">
          <strong>First run is capped at {n(status.first_run_cap)} messages.</strong> This run will process {n(thisRun)} of {n(plan.total)}.
          Check the result and restore it once to prove recovery works; then full runs unlock and the rest of this plan can run.
        </div>
      )}
      {hasTrash && (
        <div className="notice notice-warn">
          <strong>Trash is not forever.</strong> Gmail empties Trash automatically after about 30 days. Restore is only possible within that window.
          Sift never empties Trash and never deletes permanently.
        </div>
      )}
      <div className="notice">
        Every changed message gets a label like <code>Sift/YYYY-MM-DD-run-N</code>. Archive is fully reversible: restore re-adds the Inbox label and removes Sift's label.
        Right before each batch, Sift re-checks every message: anything newly starred, replied to, or deleted is skipped. Nothing can be added.
      </div>

      <label className="ack">
        <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        I've reviewed this plan. {status.synthetic ? "Apply it to the synthetic mailbox." : "Apply it to my mailbox."}
      </label>
      <div className="row end">
        <button className="btn btn-ghost" onClick={() => go("recommendations")}>Edit selection</button>
        <button className="btn btn-primary btn-lg" disabled={!ack || busy} onClick={approveAndRun}>
          {busy ? "Starting…" : `Approve and run ${n(thisRun)} ${thisRun === 1 ? "change" : "changes"}`}
        </button>
      </div>
    </div>
  );
}
