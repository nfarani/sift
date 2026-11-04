import { useEffect, useMemo, useRef, useState } from "react";
import { api, Recommendation, Status } from "../api";
import { usePoll } from "../hooks";
import { AGE_OPTIONS, ageLabel, day, n, pct, plural } from "../format";
import { RecChoice, Selection } from "../selection";
import Ask from "./Ask";

interface RecsResponse { classifier: { label: string; uses_ai: boolean; ai_groups?: number }; items: Recommendation[] }
interface Props {
  selection: Selection; setSelection: (f: (s: Selection) => Selection) => void; go: (r: string) => void;
  ai: { available: boolean; reason: string | null };
  status: Status | null;
}

export default function Recommendations({ selection, setSelection, go, ai, status }: Props) {
  // Until full runs unlock, the backend runs exactly one recommendation, capped. The picker behaves like a radio.
  const locked = !!status && !status.full_runs_unlocked;
  const cap = status?.first_run_cap ?? 0;
  const { data, error, reload } = usePoll(() => api.get<RecsResponse>("/recommendations"), null);
  const [previews, setPreviews] = useState<Record<string, Recommendation>>({});
  const [catFilter, setCatFilter] = useState("all");
  const [actionFilter, setActionFilter] = useState("all");
  const [senderQ, setSenderQ] = useState("");
  const [globalAge, setGlobalAge] = useState<number | "">("");

  const choiceFor = (r: Recommendation): RecChoice =>
    selection[r.id] ?? { selected: false, action: r.action === "trash" ? "trash" : "archive", older_than_days: r.older_than_days, excluded_senders: [] };

  const update = (r: Recommendation, patch: Partial<RecChoice>) =>
    setSelection((s) => {
      const next = { ...s, [r.id]: { ...choiceFor(r), ...patch } };
      if (locked && patch.selected) {
        for (const k of Object.keys(next)) if (k !== r.id && next[k].selected) next[k] = { ...next[k], selected: false };
      }
      return next;
    });

  // Re-count on the server whenever age or exclusions differ from the defaults.
  const timers = useRef<Record<string, number>>({});
  useEffect(() => {
    if (!data) return;
    for (const r of data.items) {
      const c = selection[r.id];
      if (!c || r.action === "review") continue;
      const key = JSON.stringify([c.older_than_days, c.excluded_senders]);
      if (previews[r.id] && (previews[r.id] as Recommendation & { _key?: string })._key === key) continue;
      clearTimeout(timers.current[r.id]);
      timers.current[r.id] = window.setTimeout(async () => {
        const p = await api.post<Recommendation>(`/recommendations/${r.id}/preview`, {
          older_than_days: c.older_than_days, excluded_senders: c.excluded_senders,
        });
        setPreviews((ps) => ({ ...ps, [r.id]: Object.assign(p, { _key: key }) }));
      }, 250);
    }
  }, [data, selection]); // eslint-disable-line react-hooks/exhaustive-deps

  const items = useMemo(() => {
    if (!data) return [];
    const q = senderQ.trim().toLowerCase();
    return data.items
      .map((r) => previews[r.id] ?? r)
      .filter((r) => catFilter === "all" || r.id === catFilter)
      .filter((r) => actionFilter === "all" || (r.action === "review" ? "review" : choiceFor(r).action) === actionFilter)
      .filter((r) => !q || r.senders.some((s) => s.sender.includes(q) || (s.display_name || "").toLowerCase().includes(q)));
  }, [data, previews, catFilter, actionFilter, senderQ, selection]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <div className="notice notice-error">{error}</div>;
  if (!data) return <div className="loading">Loading…</div>;
  if (data.items.length === 0) {
    return (
      <section className="empty-hero">
        <h1>No recommendations yet</h1>
        <p>Run a scan first.</p>
        <button className="btn btn-primary" onClick={() => go("scan")}>Go to scan</button>
      </section>
    );
  }

  const selected = data.items.filter((r) => r.action !== "review" && selection[r.id]?.selected);
  const selectedCount = selected.reduce((a, r) => a + (previews[r.id] ?? r).count, 0);

  const protectSender = async (sender: string) => {
    await api.post("/protection-rules", { kind: "sender", value: sender });
    setPreviews({});
    await reload();
  };

  return (
    <div className="stack-lg with-footer">
      <div className="page-head">
        <div>
          <h1>Recommendations</h1>
          <p className="muted">
            {data.classifier.uses_ai ? `Rules first; Claude labelled ${n(data.classifier.ai_groups ?? 0)} senders the rules weren't sure about. Protection always wins.` :
              "Classified by deterministic rules only. No AI was used; uncertain senders go to review."}
            {!data.classifier.uses_ai && ai.available && <> <a href="#/claude">Run Claude on them →</a></>}
          </p>
        </div>
      </div>

      <Ask available={ai.available} reason={ai.reason} onSaved={() => { setPreviews({}); reload(); }} />

      {locked && (
        <div className="notice notice-cap" role="note">
          <strong>First run: {n(cap)} messages from one recommendation.</strong> Pick one. Sift changes at most {n(cap)} of its messages,
          then you check them in Gmail and restore them to prove recovery works. Full runs, with several recommendations at once, unlock after that.
        </div>
      )}
      {status && !status.synthetic && status.cleanup.state !== "enabled" && (
        <div className="notice notice-row">
          <span>Read-only: you can build and review a plan, but running it needs cleanup enabled.</span>
          <button className="btn btn-ghost btn-sm" onClick={() => go("cleanup")}>{status.cleanup.state === "reauth_required" ? "Reconnect cleanup" : "Enable cleanup…"}</button>
        </div>
      )}

      <div className="filters">
        <label>Category
          <select value={catFilter} onChange={(e) => setCatFilter(e.target.value)}>
            <option value="all">All</option>
            {data.items.map((r) => <option key={r.id} value={r.id}>{r.category_label}</option>)}
          </select>
        </label>
        <label>Action
          <select value={actionFilter} onChange={(e) => setActionFilter(e.target.value)}>
            <option value="all">All</option>
            <option value="archive">Archive</option>
            <option value="trash">Trash</option>
            <option value="review">Review</option>
          </select>
        </label>
        <label>Sender
          <input placeholder="Search senders" value={senderQ} onChange={(e) => setSenderQ(e.target.value)} />
        </label>
        <label>Date range
          <span className="row tight">
            <select value={globalAge} onChange={(e) => setGlobalAge(e.target.value === "" ? "" : Number(e.target.value))}>
              <option value="">Per recommendation</option>
              {AGE_OPTIONS.map((d) => <option key={d} value={d}>{ageLabel(d)}</option>)}
            </select>
            <button className="btn btn-ghost btn-sm" disabled={globalAge === ""}
              onClick={() => data.items.forEach((r) => r.action !== "review" && update(r, { older_than_days: Number(globalAge) }))}>
              Apply to all
            </button>
          </span>
        </label>
      </div>

      {items.length === 0 && <div className="notice">No recommendations match these filters.</div>}
      {items.map((r) =>
        r.action === "review"
          ? <ReviewCard key={r.id} rec={r} onProtect={protectSender} />
          : <RecCard key={r.id} rec={r} choice={choiceFor(r)} single={locked} onChange={(p) => update(r, p)} onProtect={protectSender} senderQ={senderQ}
              onRemove={r.source === "query" ? async () => { await api.del(`/recommendations/${r.id}`); setSelection((s) => { const x = { ...s }; delete x[r.id]; return x; }); reload(); } : undefined} />,
      )}

      <div className="footer-bar">
        <div>
          <strong>{plural(selectedCount, "message")}</strong> selected across {plural(selected.length, "recommendation")}
          <span className="muted"> · protected mail is always excluded</span>
          {locked && selected.length === 1 && selectedCount > cap && <span className="muted"> · first run changes {n(cap)} of them</span>}
          {locked && selected.length > 1 && <div className="bad small">First run takes exactly one recommendation. Unselect {plural(selected.length - 1, "recommendation")}.</div>}
        </div>
        <button className="btn btn-primary" disabled={selected.length === 0 || selectedCount === 0 || (locked && selected.length > 1)} onClick={() => go("confirm")}>
          Review plan
        </button>
      </div>
    </div>
  );
}

function RecCard({ rec, choice, single, onChange, onProtect, senderQ, onRemove }: {
  rec: Recommendation; choice: RecChoice; single: boolean; onChange: (p: Partial<RecChoice>) => void;
  onProtect: (s: string) => void; senderQ: string; onRemove?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const toggleSender = (s: string) =>
    onChange({ excluded_senders: choice.excluded_senders.includes(s) ? choice.excluded_senders.filter((x) => x !== s) : [...choice.excluded_senders, s] });
  const q = senderQ.trim().toLowerCase();
  const senders = rec.senders.filter((s) => !q || s.sender.includes(q) || (s.display_name || "").toLowerCase().includes(q));

  return (
    <article className={`rec ${choice.selected ? "rec-on" : ""}`}>
      <div className="rec-head">
        <label className="check">
          <input type="checkbox" checked={choice.selected} onChange={(e) => onChange({ selected: e.target.checked })}
            aria-label={single ? `Use ${rec.title} for the first run` : `Select ${rec.title}`} />
          <span />
        </label>
        <div className="rec-title">
          <h3>{rec.title}</h3>
          <div className="rec-meta">
            <span className={`tag ${rec.source === "query" ? "tag-keep" : ""}`}>{rec.category_label}</span>
            {rec.source === "query" && onRemove && <button className="btn btn-link small" onClick={onRemove}>Remove</button>}
            <span>{plural(rec.sender_count, "sender")}</span>
            {rec.date_range[0] && <span>{rec.date_range[0]} → {rec.date_range[1]}</span>}
            <Confidence value={rec.confidence} />
          </div>
        </div>
        <div className="rec-count">
          <div className="big">{n(rec.count)}</div>
          <div className="muted small">{pct(rec.impact_pct)} of inbox</div>
        </div>
      </div>

      <div className="rec-controls">
        <label>Action
          <select value={choice.action} onChange={(e) => onChange({ action: e.target.value as RecChoice["action"] })}>
            <option value="archive">Archive + label (reversible)</option>
            <option value="trash">Move to Trash (30-day window)</option>
          </select>
        </label>
        <label>Age
          <select value={choice.older_than_days} onChange={(e) => onChange({ older_than_days: Number(e.target.value) })}>
            {AGE_OPTIONS.map((d) => <option key={d} value={d}>{ageLabel(d)}</option>)}
          </select>
        </label>
        {choice.excluded_senders.length > 0 && <span className="tag tag-warn">{plural(choice.excluded_senders.length, "sender")} excluded</span>}
        <button className="btn btn-link" onClick={() => setOpen(!open)}>{open ? "Hide details" : "Details, examples & senders"}</button>
      </div>

      <p className="rec-reason">{rec.reason}</p>
      {rec.protected_count > 0 && (
        <p className="rec-exceptions">
          <strong>{n(rec.protected_count)} protected</strong> from these senders and left alone:{" "}
          {rec.exceptions.map((e) => `${n(e.count)} ${e.reason}`).join(" · ")}
        </p>
      )}

      {open && (
        <div className="rec-details">
          <div className="grid-2">
            <div>
              <h4>Representative messages</h4>
              <ul className="samples">
                {rec.examples.map((m) => (
                  <li key={m.id}><span className="s-subj">{m.subject}</span><span className="s-from">{m.from_name} · {day(m.internal_date)}</span></li>
                ))}
              </ul>
            </div>
            <div>
              <h4>Protected exceptions</h4>
              {rec.protected_examples.length === 0 ? <p className="muted">None in this group.</p> : (
                <ul className="samples">
                  {rec.protected_examples.map((m) => (
                    <li key={m.id}><span className="s-subj">{m.subject}</span><span className="s-from keep">{m.protect_reason}</span></li>
                  ))}
                </ul>
              )}
              {(rec.unsubscribe_one_click > 0 || rec.unsubscribe_manual > 0) && (
                <p className="muted small">
                  Unsubscribe: {n(rec.unsubscribe_one_click)} senders support one-click, {n(rec.unsubscribe_manual)} are manual only.
                  Unsubscribing is separate: use <a href="#/unsubscribe">Unsubscribe</a>, which has its own approval.
                </p>
              )}
            </div>
          </div>
          <h4>Senders</h4>
          <table className="table senders">
            <thead><tr><th>Include</th><th>Sender</th><th className="num">Will change</th><th className="num">Protected</th><th className="num">Unread</th><th>Last seen</th><th /></tr></thead>
            <tbody>
              {senders.map((s) => {
                const excluded = choice.excluded_senders.includes(s.sender);
                return (
                  <tr key={s.sender} className={excluded ? "excluded" : ""}>
                    <td><input type="checkbox" checked={!excluded} onChange={() => toggleSender(s.sender)} aria-label={`Include ${s.sender}`} /></td>
                    <td><div className="sender-name">{s.display_name}</div><div className="sender-addr">{s.sender}</div>
                      {s.classifier && <div className={`src ${s.classifier.startsWith("claude") ? "src-claude" : ""}`} title={s.reason ?? ""}>
                        {s.classifier.startsWith("claude") ? "Claude" : "Rules"}{s.reason ? ` · ${s.reason}` : ""}</div>}</td>
                    <td className="num">{excluded ? "—" : n(s.eligible)}</td>
                    <td className="num">{s.protected_in_inbox == null ? "—" : n(s.protected_in_inbox)}</td>
                    <td className="num">{n(s.unread)}</td>
                    <td>{s.last_date}</td>
                    <td><button className="btn btn-link small" onClick={() => onProtect(s.sender)}>Always protect</button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </article>
  );
}

function ReviewCard({ rec, onProtect }: { rec: Recommendation; onProtect: (s: string) => void }) {
  return (
    <article className="rec rec-review">
      <div className="rec-head">
        <div className="rec-title">
          <h3>{rec.title}</h3>
          <div className="rec-meta"><span className="tag tag-review">Needs your review</span><span>{plural(rec.sender_count, "sender")}</span></div>
        </div>
        <div className="rec-count"><div className="big">{n(rec.count)}</div><div className="muted small">no action suggested</div></div>
      </div>
      <p className="rec-reason">{rec.reason}</p>
      <table className="table senders">
        <thead><tr><th>Sender</th><th className="num">In inbox</th><th className="num">Unread</th><th>Last seen</th><th /></tr></thead>
        <tbody>
          {rec.senders.map((s) => (
            <tr key={s.sender}>
              <td><div className="sender-name">{s.display_name}</div><div className="sender-addr">{s.sender}</div></td>
              <td className="num">{n(s.in_inbox)}</td>
              <td className="num">{n(s.unread)}</td>
              <td>{s.last_date}</td>
              <td><button className="btn btn-link small" onClick={() => onProtect(s.sender)}>Always protect</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </article>
  );
}

function Confidence({ value }: { value: number }) {
  const level = value >= 0.8 ? "high" : value >= 0.6 ? "medium" : "low";
  return <span className={`conf conf-${level}`} title={`Confidence ${Math.round(value * 100)}%`}>{level} confidence</span>;
}
