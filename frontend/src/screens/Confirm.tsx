import { useEffect, useRef, useState } from "react";
import { api, Plan, Recommendation, Run, Status } from "../api";
import { usePoll } from "../hooks";
import { ageLabel, day, n, plural } from "../format";
import { Selection } from "../selection";

const ACTION_LABEL: Record<string, string> = { archive: "Archive + label", trash: "Move to Trash" };

export default function Confirm({ selection, status, go }: { selection: Selection; status: Status | null; go: (r: string) => void }) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [buildErr, setBuildErr] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null); // shown verbatim next to the button
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [rebuild, setRebuild] = useState(0);
  // Titles for the per-recommendation table (the plan itself only carries ids).
  const { data: recs } = usePoll(() => api.get<{ items: Recommendation[] }>("/recommendations"), null);
  // Category label, not the title: titles say "Archive …" even when you switched the action to Trash.
  const titleOf = (id: string) => {
    const r = recs?.items.find((x) => x.id === id);
    return r ? (r.source === "query" ? r.title : r.category_label) : id.replace("_", " ");
  };

  const items = Object.entries(selection)
    .filter(([, c]) => c.selected)
    .map(([rec_id, c]) => ({ rec_id, action: c.action, older_than_days: c.older_than_days, excluded_senders: c.excluded_senders }));
  const key = JSON.stringify([items, rebuild]);

  // One draft per distinct selection (StrictMode re-runs effects; don't create duplicate drafts).
  const built = useRef<string | null>(null);
  useEffect(() => {
    if (items.length === 0 || built.current === key) return;
    built.current = key;
    setPlan(null); setBuildErr(null); setActionErr(null); setAck(false);
    api.post<Plan>("/plans", { items }).then(setPlan, (e) => setBuildErr(e.message));
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
  if (buildErr) return <div className="stack narrow"><div className="notice notice-error" role="alert">{buildErr}</div><button className="btn btn-ghost" onClick={() => go("recommendations")}>Back</button></div>;
  if (!plan || !status) return <div className="loading">Building the exact plan…</div>;

  const locked = !status.full_runs_unlocked;
  const tooMany = locked && plan.summary.items.length > 1;
  const capped = locked && plan.total > status.first_run_cap;
  const thisRun = capped ? status.first_run_cap : plan.total;
  const hasTrash = (plan.summary.by_action.trash ?? 0) > 0;
  const cleanupOn = status.cleanup.state === "enabled" && status.can_modify;

  const approveAndRun = async () => {
    setBusy(true); setActionErr(null);
    try {
      // A plan is approved once. If approval succeeded but execution was refused, don't approve again.
      if (plan.status === "draft") {
        const approved = await api.post<Plan>(`/plans/${plan.id}/approve`, { confirm_hash: plan.ids_hash });
        setPlan(approved);
      }
      const run = await api.post<Run>(`/plans/${plan.id}/execute`);
      go(`run/${run.id}`);
    } catch (e) {
      setActionErr((e as Error).message); setBusy(false);
      api.get<Plan>(`/plans/${plan.id}`).then(setPlan, () => undefined);
    }
  };
  const newPlan = () => setRebuild((x) => x + 1);

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
                <td>{titleOf(it.rec_id)}</td>
                <td>{it.action === "trash" ? <span className="tag tag-warn">{ACTION_LABEL.trash}</span> : ACTION_LABEL[it.action] ?? it.action}</td>
                <td>{ageLabel(it.older_than_days)}</td>
                <td className="num"><strong>{n(it.count)}</strong></td>
                <td className="num keep">{n(it.protected_excluded)}</td>
                <td className="num">{n(it.user_excluded)}{it.excluded_senders.length > 0 && <span className="muted"> ({plural(it.excluded_senders.length, "sender")})</span>}</td>
                <td className="num">{n(it.too_recent)}</td>
              </tr>
            ))}
          </tbody>
          {plan.summary.items.length > 1 && (
            <tfoot>
              <tr>
                <td colSpan={3}>All {n(plan.summary.items.length)} recommendations</td>
                <td className="num"><strong>{n(plan.total)}</strong></td>
                <td className="num keep">{n(plan.summary.protected_excluded)}</td>
                <td className="num">{n(plan.summary.user_excluded)}</td>
                <td className="num">{n(plan.summary.too_recent)}</td>
              </tr>
            </tfoot>
          )}
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

      {tooMany && (
        <div className="notice notice-error" role="alert">
          <strong>First run: {n(status.first_run_cap)} messages from one recommendation.</strong> This plan has {plural(plan.summary.items.length, "recommendation")}.
          Go back and keep only one; the backend refuses anything else until full runs are unlocked.
        </div>
      )}
      {locked && !tooMany && (
        <div className="notice notice-cap">
          <strong>First run: up to {n(status.first_run_cap)} messages from one recommendation.</strong>{" "}
          {capped ? <>This run will process {n(thisRun)} of the {n(plan.total)} in this plan.</> : <>This plan has {n(plan.total)}, so all of them run.</>}{" "}
          Check the result in Gmail, then restore it to prove recovery works. After a successful restore you can unlock full runs.
        </div>
      )}
      {hasTrash && (
        <div className="notice notice-warn">
          <strong>Trash is not forever.</strong> {n(plan.summary.by_action.trash ?? 0)} {(plan.summary.by_action.trash ?? 0) === 1 ? "message goes" : "messages go"} to Trash.
          Gmail empties Trash automatically after about 30 days. Restore is only possible within that window.
          Sift never empties Trash and never deletes permanently.
        </div>
      )}
      {!locked && plan.total > 500 && (
        <div className="notice">
          <strong>Large run.</strong> Sift works through the {n(plan.total)} messages in batches of up to 500, re-checking each batch first.
          You can watch progress and cancel at any time: messages not reached stay untouched, and changed ones keep their label and can be restored.
        </div>
      )}
      <div className="notice">
        Every changed message gets a label like <code>Sift/YYYY-MM-DD-run-N</code>. Archive is fully reversible: restore re-adds the Inbox label and removes Sift's label.
        Right before each batch, Sift re-checks every message: anything newly starred, replied to, or deleted is skipped. Nothing can be added.
      </div>

      {!cleanupOn && (
        <div className="notice notice-warn notice-row">
          <span><strong>Read-only.</strong> Sift is connected with <code>gmail.readonly</code> and cannot change your mail.
            You can review this plan; to run it, enable cleanup. Plans approved while read-only must be approved again afterwards.</span>
          <button className="btn btn-ghost btn-sm" onClick={() => go("cleanup")}>{status.cleanup.state === "reauth_required" ? "Reconnect cleanup" : "Enable cleanup…"}</button>
        </div>
      )}
      <label className="ack">
        <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        I've reviewed this plan. {status.synthetic ? "Apply it to the synthetic mailbox." : "Apply it to my mailbox."}
      </label>
      {actionErr && (
        <div className="notice notice-error notice-row" role="alert">
          <span><strong>Sift refused to run this plan:</strong> {actionErr}</span>
          <button className="btn btn-ghost btn-sm" onClick={newPlan}>Build a new plan</button>
        </div>
      )}
      <div className="row end">
        <button className="btn btn-ghost" onClick={() => go("recommendations")}>Edit selection</button>
        <button className="btn btn-primary btn-lg" disabled={!ack || busy || !cleanupOn || tooMany} onClick={approveAndRun}>
          {busy ? "Starting…" : plan.status === "draft"
            ? `Approve and run ${n(thisRun)} ${thisRun === 1 ? "change" : "changes"}`
            : `Run ${n(thisRun)} ${thisRun === 1 ? "change" : "changes"} (plan ${plan.status})`}
        </button>
      </div>
    </div>
  );
}
