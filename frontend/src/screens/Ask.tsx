import { useState } from "react";
import { api, QueryPreview, Spec } from "../api";
import { day, n } from "../format";

const CATS: [string, string][] = [
  ["job_alerts", "Job alerts"], ["promotions", "Promotions"], ["newsletters", "Newsletters"], ["social", "Social"],
  ["notifications", "Notifications"], ["advertising", "Advertising"], ["spam", "Spam"], ["uncertain", "Needs review"],
  ["employment", "Employment"], ["personal", "Personal"], ["financial", "Financial"], ["receipts", "Receipts"],
  ["travel", "Travel"], ["security", "Security"], ["important", "Important"],
];
const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
const EXAMPLES = [
  "Clean old shopping promotions but keep receipts",
  "Job alerts older than a month",
  "Newsletters I haven't opened in a year",
  "Which senders send me the most unread mail?",
];

/** Sentence -> visible, editable filter -> local count and samples -> optional saved recommendation. */
export default function Ask({ available, reason, onSaved }: { available: boolean; reason: string | null; onSaved: () => void }) {
  const [text, setText] = useState("");
  const [spec, setSpec] = useState<Spec | null>(null);
  const [pv, setPv] = useState<QueryPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const call = async (fn: () => Promise<void>) => {
    setErr(null); setBusy(true);
    try { await fn(); } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };
  const plan = () => call(async () => {
    const r = await api.post<{ spec: Spec; preview: QueryPreview }>("/query/plan", { text });
    setSpec(r.spec); setPv(r.preview); setSaved(null);
  });
  const refresh = (s: Spec) => call(async () => {
    const r = await api.post<{ spec: Spec; preview: QueryPreview }>("/query/preview", { spec: s });
    setSpec(r.spec); setPv(r.preview);
  });
  const save = () => spec && call(async () => {
    await api.post("/query/save", { spec });
    setSaved(spec.title || "Your request"); setSpec(null); setPv(null); setText(""); onSaved();
  });
  const edit = (patch: Partial<Spec>) => spec && setSpec({ ...spec, ...patch });

  return (
    <section className="card ask">
      <h2>Ask in plain English</h2>
      {!available ? (
        <p className="muted">Needs Claude ({reason}).</p>
      ) : (
        <>
          <div className="ask-row">
            <input value={text} placeholder="e.g. Clean old shopping promotions but keep receipts" onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && text.trim() && plan()} />
            <button className="btn btn-primary" disabled={busy || text.trim().length < 3} onClick={plan}>{busy && !spec ? "Thinking…" : "Turn into a filter"}</button>
          </div>
          {!spec && <div className="ask-examples">{EXAMPLES.map((x) => <button key={x} className="chip-btn" onClick={() => setText(x)}>{x}</button>)}</div>}
          <p className="muted small">Only your sentence and today's date are sent to Claude, never your mail; each request costs about one cent and shows in Claude → Usage. Claude can't act on anything: you'll see the filter and the exact count first.</p>
        </>
      )}
      {saved && <div className="notice notice-cap">Saved “{saved}” as a recommendation below. Select it like any other to build a plan.</div>}
      {err && <div className="notice notice-error">{err}</div>}

      {spec && pv && (
        <div className="spec">
          <p className="spec-explain">{spec.explanation}</p>
          {spec.not_supported.length > 0 && <div className="notice notice-warn">Couldn't express: {spec.not_supported.join("; ")}</div>}
          <div className="spec-grid">
            <label>Name<input value={spec.title} onChange={(e) => edit({ title: e.target.value })} /></label>
            <label>Senders containing<input value={spec.sender_contains.join(", ")} onChange={(e) => edit({ sender_contains: list(e.target.value) })} placeholder="any" /></label>
            <label>Subject contains any of<input value={spec.subject_contains_any.join(", ")} onChange={(e) => edit({ subject_contains_any: list(e.target.value) })} placeholder="any" /></label>
            <label>Skip subjects containing<input value={spec.subject_excludes_any.join(", ")} onChange={(e) => edit({ subject_excludes_any: list(e.target.value) })} placeholder="none" /></label>
            <label>Older than (days)<input type="number" min={0} value={spec.older_than_days ?? ""} onChange={(e) => edit({ older_than_days: e.target.value === "" ? null : Number(e.target.value) })} /></label>
            <label>Newer than (days)<input type="number" min={0} value={spec.newer_than_days ?? ""} onChange={(e) => edit({ newer_than_days: e.target.value === "" ? null : Number(e.target.value) })} /></label>
            <label className="inline"><input type="checkbox" checked={spec.unread_only} onChange={(e) => edit({ unread_only: e.target.checked })} /> Unread only</label>
            <label>Suggested action
              <select value={spec.suggested_action} onChange={(e) => edit({ suggested_action: e.target.value as Spec["suggested_action"] })}>
                <option value="archive">Archive</option><option value="trash">Trash</option><option value="review">Just review</option>
              </select>
            </label>
          </div>
          <div className="cat-chips">
            {CATS.map(([k, label]) => (
              <button key={k} className={`chip-btn ${spec.categories.includes(k) ? "on" : ""}`}
                onClick={() => edit({ categories: spec.categories.includes(k) ? spec.categories.filter((c) => c !== k) : [...spec.categories, k] })}>{label}</button>
            ))}
          </div>

          <div className="spec-result">
            <div><div className="stat-label">{spec.mode === "top_senders" ? "Matching messages" : "Would match"}</div><div className="big">{n(pv.count)}</div>
              <div className="muted small">{n(pv.protected_excluded)} protected messages excluded{pv.date_range[0] ? ` · ${day(pv.date_range[0])} → ${day(pv.date_range[1]!)}` : ""}</div></div>
            <div className="row">
              <button className="btn btn-ghost" disabled={busy} onClick={() => refresh(spec)}>Update count</button>
              {spec.mode === "messages" && <button className="btn btn-primary" disabled={busy || pv.count === 0} onClick={save}>Save as recommendation</button>}
              <button className="btn btn-link" onClick={() => { setSpec(null); setPv(null); }}>Discard</button>
            </div>
          </div>

          <div className="grid-2">
            <div>
              <h4>{spec.mode === "top_senders" ? "Top senders" : "Senders"}</h4>
              <table className="table"><tbody>{pv.senders.slice(0, spec.mode === "top_senders" ? 25 : 8).map((s) => (
                <tr key={s.sender}><td><div className="sender-name">{s.display_name}</div><div className="sender-addr">{s.sender}</div></td><td className="num">{n(s.n)}<span className="muted"> · {n(s.unread)} unread</span></td></tr>
              ))}</tbody></table>
            </div>
            <div>
              <h4>Most recent matches</h4>
              <ul className="samples">{pv.samples.map((m) => <li key={m.id}><span className="s-subj">{m.subject}</span><span className="s-from">{m.from_name} · {day(m.internal_date)}</span></li>)}</ul>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
