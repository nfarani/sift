import { api, Dashboard, Scan, Status } from "../api";
import { usePoll } from "../hooks";
import { n, pct } from "../format";
import { ScanChoice } from "./ScanScreen";

export default function Overview({ go, status }: { go: (r: string) => void; status: Status | null }) {
  const { data: d, error } = usePoll(() => api.get<Dashboard>("/dashboard"), null);
  const { data: scan } = usePoll(() => api.get<Scan>("/scan"), null);

  if (error) return <div className="notice notice-error">Couldn't reach the Sift backend: {error}</div>;
  if (!d || !scan) return <div className="loading">Loading…</div>;

  if (scan.status === "none" || d.scanned === 0) {
    return (
      <section className="empty-hero">
        <h1>Let's see what's in your inbox.</h1>
        <p>
          Sift reads message headers (sender, subject, labels, dates) and Gmail's short preview snippet. It never downloads full bodies or attachments,
          never marks anything as read, and changes nothing until you approve an exact plan.
        </p>
        {d.synthetic
          ? <p className="muted">You're connected to a synthetic mailbox of about 45,000 fictional messages.</p>
          : <p className="muted">Start with a test scan to check everything works on {d.account}.</p>}
        <ScanChoice synthetic={d.synthetic} onStart={async (max) => { await api.post("/scan", { max_messages: max }); go("scan"); }} />
        {scan.status !== "none" && scan.resumable && (
          <button className="btn btn-ghost" onClick={() => go("scan")}>Resume the interrupted scan</button>
        )}
      </section>
    );
  }

  const maxCat = Math.max(...d.categories.map((c) => c.inbox), 1);
  return (
    <div className="stack-lg">
      <div className="page-head">
        <div>
          <h1>Inbox overview</h1>
          <p className="muted">
            {d.account} · {n(d.scanned)} messages analysed{d.last_scan?.status !== "completed" && " (scan incomplete)"}
          </p>
        </div>
        <div className="row">
          <button className="btn btn-ghost" onClick={() => go("scan")}>Rescan</button>
          <button className="btn btn-primary" onClick={() => go("recommendations")}>Review recommendations</button>
        </div>
      </div>

      {status && <CleanupBanner status={status} go={go} />}

      <div className="stats">
        <Stat label="In your inbox" value={n(d.inbox)} />
        <Stat label="Unread in inbox" value={n(d.unread)} />
        <Stat label="Promotional" value={n(d.promotional)} hint="Inbox mail in Gmail's Promotions tab" />
        <Stat label="Mailing lists" value={n(d.subscriptions)} hint="Senders with list headers" />
        <Stat label="Protected" value={n(d.protected)} tone="keep" hint="Never included in a plan" />
        <Stat label="Suggested cleanup" value={n(d.recommended)} tone="accent" hint={`${pct(d.impact_pct)} of your inbox`} />
      </div>

      <div className="grid-2">
        <section className="card">
          <h2>What your inbox is made of</h2>
          <ul className="bars">
            {d.categories.map((c) => (
              <li key={c.category}>
                <span className="bar-label">
                  {c.label}
                  {c.keep && <span className="tag tag-keep">keep</span>}
                </span>
                <span className="bar-track">
                  <span className={`bar-fill ${c.keep ? "keep" : c.category === "uncertain" ? "review" : ""}`}
                    style={{ width: `${(100 * c.inbox) / maxCat}%` }} />
                </span>
                <span className="bar-num">{n(c.inbox)}</span>
              </li>
            ))}
          </ul>
        </section>
        <section className="card">
          <h2>Most unread, by sender</h2>
          <table className="table">
            <tbody>
              {d.top_unread_senders.map((s) => (
                <tr key={s.sender}>
                  <td>
                    <div className="sender-name">{s.display_name || s.sender}</div>
                    <div className="sender-addr">{s.sender}</div>
                  </td>
                  <td className="num">{n(s.unread)}<span className="muted"> / {n(s.total)}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  );
}

/** Gmail only: synthetic mode reports cleanup as enabled, so this never shows there. */
export function CleanupBanner({ status, go }: { status: Status; go: (r: string) => void }) {
  const st = status.cleanup.state;
  if (status.synthetic || st === "enabled") return null;
  const text = st === "reauth_required" ? "The cleanup permission expired. Scanning still works; reconnect cleanup to change mail."
    : st === "enabling" ? "Waiting for you to finish Google's cleanup consent in the other tab."
    : st === "error" ? `Enabling cleanup failed: ${status.cleanup.error ?? "unknown error"}`
    : "Sift is read-only. You can review recommendations and plans, but nothing can change until you enable cleanup.";
  return (
    <div className={`notice ${st === "error" || st === "reauth_required" ? "notice-warn" : ""} notice-row`}>
      <span>{text}</span>
      {st !== "enabling" && (
        <button className="btn btn-ghost btn-sm" onClick={() => go("cleanup")}>
          {st === "reauth_required" ? "Reconnect cleanup" : "Enable cleanup…"}
        </button>
      )}
    </div>
  );
}

function Stat({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "keep" | "accent" }) {
  return (
    <div className={`stat ${tone ? `stat-${tone}` : ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}
