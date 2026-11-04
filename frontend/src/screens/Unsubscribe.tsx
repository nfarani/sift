import { useEffect, useMemo, useState } from "react";
import { api, Recommendation, SenderRow, Status, UnsubBatch, UnsubLogRow } from "../api";
import { usePoll } from "../hooks";
import { n, plural } from "../format";

const MAX_SENDERS = 200; // backend: unsubscribe.MAX_BATCH_SENDERS

const METHOD_LABEL: Record<string, string> = { one_click: "One-click", manual: "Manual", none: "Not possible" };
const OUTCOME_LABEL: Record<string, string> = {
  attempted: "attempted", confirmed: "confirmed", not_confirmed: "not confirmed", failed: "failed",
  refused: "refused", simulated: "simulated (synthetic)", manual: "manual, not sent",
};

function Separate() {
  return (
    <div className="notice">
      <strong>Unsubscribing is separate from cleanup.</strong> Approving a cleanup plan never unsubscribes you, and approving
      unsubscribes never changes mail. For each one-click sender Sift sends a single RFC 8058 POST to the sender's own unsubscribe
      address. It opens no links, loads no pages and follows no redirects. Manual senders are never contacted.
    </div>
  );
}

function NeedsCleanup({ go }: { go: (r: string) => void }) {
  return (
    <div className="stack-lg narrow">
      <div className="page-head"><div><h1>Unsubscribe</h1></div></div>
      <div className="notice notice-warn">
        Unsubscribing needs the same explicit permission as changing mail. Enable cleanup first.
      </div>
      <div className="row"><button className="btn btn-primary" onClick={() => go("cleanup")}>Enable cleanup…</button></div>
    </div>
  );
}

interface RecsResponse { items: Recommendation[] }
type Candidate = SenderRow & { categories: string[] };

export default function Unsubscribe({ status, go, batchId }: { status: Status | null; go: (r: string) => void; batchId?: number }) {
  if (!status) return <div className="loading">Loading…</div>;
  if (status.cleanup.state !== "enabled") return <NeedsCleanup go={go} />;
  return batchId ? <BatchReview batchId={batchId} go={go} status={status} /> : <Picker go={go} />;
}

function Picker({ go }: { go: (r: string) => void }) {
  const { data, error } = usePoll(() => api.get<RecsResponse>("/recommendations"), null);
  const { data: log } = usePoll(() => api.get<UnsubLogRow[]>("/unsubscribe/log"), null);
  const [picked, setPicked] = useState<string[]>([]);
  const [show, setShow] = useState<"one_click" | "all">("one_click");
  const [q, setQ] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Senders with a List-Unsubscribe header, as seen in the scan. The batch re-reads each sender's newest message.
  const candidates = useMemo(() => {
    const by = new Map<string, Candidate>();
    for (const r of data?.items ?? []) {
      for (const s of r.senders) {
        if (!s.has_unsub && !s.one_click_unsub) continue;
        const prev = by.get(s.sender);
        if (prev) prev.categories.push(r.category_label);
        else by.set(s.sender, { ...s, categories: [r.category_label] });
      }
    }
    return [...by.values()].sort((a, b) => b.total - a.total);
  }, [data]);

  if (error) return <div className="notice notice-error">{error}</div>;
  if (!data) return <div className="loading">Loading…</div>;

  const query = q.trim().toLowerCase();
  const visible = candidates
    .filter((s) => show === "all" || s.one_click_unsub)
    .filter((s) => !query || s.sender.includes(query) || (s.display_name || "").toLowerCase().includes(query));
  const oneClick = candidates.filter((s) => s.one_click_unsub).length;
  const toggle = (s: string) => setPicked((p) => (p.includes(s) ? p.filter((x) => x !== s) : [...p, s]));

  const create = async () => {
    setErr(null); setBusy(true);
    try {
      const b = await api.post<UnsubBatch>("/unsubscribe/batches", { senders: picked });
      go(`unsubscribe/${b.id}`);
    } catch (e) { setErr((e as Error).message); setBusy(false); }
  };

  return (
    <div className="stack-lg with-footer">
      <div className="page-head">
        <div>
          <h1>Unsubscribe</h1>
          <p className="muted">
            {plural(candidates.length, "sender")} with an unsubscribe header in the last scan · {n(oneClick)} one-click · {n(candidates.length - oneClick)} manual
          </p>
        </div>
      </div>
      <Separate />

      <div className="filters">
        <label>Show
          <select value={show} onChange={(e) => setShow(e.target.value as "one_click" | "all")}>
            <option value="one_click">One-click only</option>
            <option value="all">One-click and manual</option>
          </select>
        </label>
        <label>Sender
          <input placeholder="Search senders" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        <button className="btn btn-ghost btn-sm" onClick={() => setPicked((p) => [...new Set([...p, ...visible.map((s) => s.sender)])].slice(0, MAX_SENDERS))}>
          Select all shown
        </button>
        {picked.length > 0 && <button className="btn btn-link small" onClick={() => setPicked([])}>Clear</button>}
      </div>

      <section className="card">
        {visible.length === 0 ? <p className="muted">No senders match.</p> : (
          <table className="table senders">
            <thead><tr><th>Pick</th><th>Sender</th><th>Method (from scan)</th><th>Category</th><th className="num">Messages</th><th className="num">Unread</th><th>Last seen</th></tr></thead>
            <tbody>
              {visible.map((s) => (
                <tr key={s.sender}>
                  <td><input type="checkbox" checked={picked.includes(s.sender)} onChange={() => toggle(s.sender)} aria-label={`Pick ${s.sender}`}
                    disabled={!picked.includes(s.sender) && picked.length >= MAX_SENDERS} /></td>
                  <td><div className="sender-name">{s.display_name || s.sender}</div><div className="sender-addr">{s.sender}</div></td>
                  <td>{s.one_click_unsub
                    ? <span className="tag tag-keep">one-click</span>
                    : <span className="tag tag-review" title="Needs an email or a web page. Sift won't do either; the address is shown on review.">manual</span>}</td>
                  <td className="muted small">{s.categories.join(", ")}</td>
                  <td className="num">{n(s.total)}</td>
                  <td className="num">{n(s.unread)}</td>
                  <td>{s.last_date}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="muted small" style={{ marginTop: 10 }}>
          The method here is from the scan. When you build a batch, Sift re-reads each sender's newest message header (read-only) and that decides it.
          Senders you have emailed, senders you protect, and senders with any protected mail (starred, your replies, receipts, security or bank mail) are never unsubscribed.
        </p>
      </section>

      <LogTable rows={log} title="Unsubscribe log" />

      <div className="footer-bar">
        <div>
          <strong>{plural(picked.length, "sender")}</strong> picked
          <span className="muted"> · nothing is sent until you approve and then run the batch</span>
          {err && <div className="bad small" role="alert">{err}</div>}
        </div>
        <button className="btn btn-primary" disabled={picked.length === 0 || busy} onClick={create}>
          {busy ? "Reading headers…" : "Review unsubscribe batch"}
        </button>
      </div>
    </div>
  );
}

function BatchReview({ batchId, go, status }: { batchId: number; go: (r: string) => void; status: Status }) {
  const [live, setLive] = useState(false); // poll only while the batch is running
  const { data: b, error, reload, setData } = usePoll(() => api.get<UnsubBatch>(`/unsubscribe/batches/${batchId}`), live ? 1000 : null, [batchId]);
  useEffect(() => setLive(b?.status === "running"), [b?.status]);
  const [ack, setAck] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (error && !b) return <div className="stack narrow"><div className="notice notice-error">{error}</div><button className="btn btn-ghost" onClick={() => go("unsubscribe")}>Back</button></div>;
  if (!b) return <div className="loading">Loading…</div>;

  const act = async (fn: () => Promise<UnsubBatch>) => {
    setErr(null); setBusy(true);
    try { setData(await fn()); } catch (e) { setErr((e as Error).message); reload(); }
    setBusy(false);
  };
  const approve = () => act(() => api.post<UnsubBatch>(`/unsubscribe/batches/${b.id}/approve`, { confirm_hash: b.items_hash }));
  const execute = () => act(() => api.post<UnsubBatch>(`/unsubscribe/batches/${b.id}/execute`));
  const cancel = () => act(() => api.post<UnsubBatch>(`/unsubscribe/batches/${b.id}/cancel`));

  // Outcomes from the batch's own log rows. "attempted" rows are written before each POST.
  const results = b.log.filter((r) => r.outcome !== "attempted");
  const attempted = b.log.filter((r) => r.outcome === "attempted").length;
  const byOutcome = results.reduce<Record<string, number>>((a, r) => ({ ...a, [r.outcome]: (a[r.outcome] ?? 0) + 1 }), {});
  const done = ["completed", "failed", "cancelled"].includes(b.status);

  return (
    <div className="stack-lg narrow">
      <div className="page-head">
        <div>
          <h1>Unsubscribe batch #{b.id} <span className={`status status-${b.status === "approved" || b.status === "draft" ? "running" : b.status}`}>{b.status}</span></h1>
          <p className="muted">Frozen at creation. Changing the senders means a new batch and a new approval.</p>
        </div>
        <button className="btn btn-ghost" onClick={() => go("unsubscribe")}>All senders</button>
      </div>
      <Separate />

      <section className="card">
        <div className="confirm-actions" style={{ marginBottom: 14 }}>
          <div className="pill">One-click POST: <strong>{n(b.counts.one_click)}</strong></div>
          <div className="pill pill-trash">Manual, not sent: <strong>{n(b.counts.manual)}</strong></div>
          <div className="pill pill-none">Not possible: <strong>{n(b.counts.none)}</strong></div>
        </div>
        <table className="table">
          <thead><tr><th>Sender</th><th>Method</th><th>Where</th><th>Why</th></tr></thead>
          <tbody>
            {b.items.map((it) => (
              <tr key={it.sender} className={it.method === "none" ? "excluded" : ""}>
                <td className="sender-addr" style={{ color: "var(--ink)" }}>{it.sender}</td>
                <td>{it.method === "one_click" ? <span className="tag tag-keep">one-click</span>
                  : it.method === "manual" ? <span className="tag tag-review">manual</span> : <span className="tag">{METHOD_LABEL.none}</span>}</td>
                <td>{it.method === "one_click" ? <>POST to <code>{it.host}</code></>
                  : it.method === "manual" ? (it.mailto ? <>email <code>{it.mailto}</code> yourself</> : it.host ? <>web page on <code>{it.host}</code> (not opened)</> : "—")
                  : "—"}</td>
                <td className="muted small">{it.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {b.status === "draft" && (
        <section className="card">
          <h2>Step 1 · Approve this batch</h2>
          <p className="muted">
            Approving allows exactly {plural(b.counts.one_click, "POST request", "POST requests")}, one per one-click sender above. It sends nothing yet.
          </p>
          <label className="ack" style={{ marginTop: 12 }}>
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            I want to unsubscribe from these {plural(b.counts.one_click, "sender")}.
          </label>
          <div className="row end" style={{ marginTop: 14 }}>
            <button className="btn btn-link" disabled={busy} onClick={cancel}>Discard batch</button>
            <button className="btn btn-primary" disabled={!ack || busy || b.counts.one_click === 0} onClick={approve}>Approve batch #{b.id}</button>
          </div>
          {b.counts.one_click === 0 && <p className="muted small">No sender in this batch supports one-click, so there is nothing to approve.</p>}
        </section>
      )}

      {b.status === "approved" && (
        <section className="card">
          <h2>Step 2 · Run it</h2>
          <p className="muted">Approved {b.approved_at?.slice(0, 19).replace("T", " ")} UTC. A batch runs once.
            {status.synthetic && " Synthetic mailbox: nothing is sent to any host; results are marked simulated."}</p>
          <div className="row end" style={{ marginTop: 14 }}>
            <button className="btn btn-link" disabled={busy} onClick={cancel}>Cancel batch</button>
            <button className="btn btn-primary" disabled={busy || status.busy} onClick={execute}>
              Send {plural(b.counts.one_click, "unsubscribe request")}
            </button>
          </div>
        </section>
      )}

      {err && <div className="notice notice-error" role="alert">{err}</div>}
      {b.error && <div className="notice notice-error">{b.error}</div>}

      {(b.status === "running" || done) && b.status !== "cancelled" && (
        <section className="card">
          <h2>Results</h2>
          <div className="stats stats-4" style={{ marginTop: 0 }}>
            <div className="stat stat-mini"><div className="stat-label">POSTs attempted</div><div className="stat-value">{n(attempted)}</div><div className="stat-hint">of {n(b.counts.one_click)} approved</div></div>
            <div className="stat stat-mini stat-good"><div className="stat-label">Accepted by sender (HTTP 2xx)</div><div className="stat-value">{n(byOutcome.confirmed)}</div></div>
            <div className="stat stat-mini"><div className="stat-label">Not confirmed / failed / refused</div><div className="stat-value">{n((byOutcome.not_confirmed ?? 0) + (byOutcome.failed ?? 0) + (byOutcome.refused ?? 0))}</div></div>
            <div className="stat stat-mini"><div className="stat-label">Manual, not sent</div><div className="stat-value">{n(byOutcome.manual)}</div></div>
          </div>
          {(byOutcome.simulated ?? 0) > 0 && <p className="muted small" style={{ marginTop: 10 }}>{n(byOutcome.simulated)} simulated: synthetic senders, nothing was sent.</p>}
          {b.status === "running" && <p className="muted" role="status" style={{ marginTop: 10 }}>Running…</p>}
          <LogTable rows={b.log} />
        </section>
      )}
    </div>
  );
}

function LogTable({ rows, title }: { rows: UnsubLogRow[] | null; title?: string }) {
  if (!rows) return null;
  const body = rows.length === 0 ? <p className="muted">Nothing logged yet.</p> : (
    <table className="table">
      <thead><tr><th>Time (UTC)</th>{title && <th>Batch</th>}<th>Sender</th><th>Method</th><th>Host</th><th className="num">HTTP</th><th>Outcome</th><th>Detail</th></tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={`${r.ts}-${r.sender}-${i}`}>
            <td className="small">{r.ts.slice(0, 19).replace("T", " ")}</td>
            {title && <td className="num">{r.batch_id ?? "—"}</td>}
            <td className="small">{r.sender}</td>
            <td className="small">{METHOD_LABEL[r.method] ?? r.method}</td>
            <td className="small">{r.url_host ? <code>{r.url_host}</code> : "—"}</td>
            <td className="num">{r.status_code ?? "—"}</td>
            <td className={`small ${r.outcome === "confirmed" ? "" : r.outcome === "failed" || r.outcome === "refused" ? "bad" : "muted"}`}>{OUTCOME_LABEL[r.outcome] ?? r.outcome}</td>
            <td className="muted small">{r.detail}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
  return title ? <section className="card"><h2>{title}</h2>{body}</section> : <div style={{ marginTop: 16 }}>{body}</div>;
}
