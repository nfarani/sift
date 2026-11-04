import { useEffect, useState } from "react";
import { api, CleanupDisableResult, Status } from "../api";
import { n } from "../format";

/** "Enable cleanup": the separate gmail.modify consent, explained before the button. */
export default function Cleanup({ status, onChange, go }: { status: Status | null; onChange: () => void; go: (r: string) => void }) {
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDisable, setConfirmDisable] = useState(false);
  const [disabled, setDisabled] = useState<CleanupDisableResult | null>(null);

  const state = status?.cleanup.state;
  // While Google's consent tab is open, poll status faster than the app-wide poll.
  useEffect(() => {
    if (state !== "enabling") return;
    const t = setInterval(onChange, 1000);
    return () => clearInterval(t);
  }, [state, onChange]);

  if (!status) return <div className="loading">Loading…</div>;

  if (status.synthetic) {
    return (
      <div className="stack-lg narrow">
        <div className="page-head"><div><h1>Cleanup</h1><p className="muted">Synthetic mailbox</p></div></div>
        <div className="notice notice-cap">
          Cleanup is enabled for the synthetic mailbox. No Google permission is involved: approved plans change synthetic data only.
          The first-run cap and the restore check still apply, so the flow matches Gmail.
        </div>
        <div className="row"><button className="btn btn-primary" onClick={() => go("recommendations")}>Go to recommendations</button></div>
      </div>
    );
  }

  const enable = async () => {
    setErr(null); setBusy(true); setDisabled(null);
    try { await api.post("/cleanup/enable"); onChange(); } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };
  const disable = async () => {
    setErr(null); setBusy(true);
    try {
      const r = await api.post<CleanupDisableResult>("/cleanup/disable");
      setDisabled(r); setConfirmDisable(false); onChange();
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };

  const cap = status.first_run_cap;
  return (
    <div className="stack-lg narrow">
      <div className="page-head">
        <div>
          <h1>{state === "enabled" ? "Cleanup is enabled" : state === "reauth_required" ? "Reconnect cleanup" : "Enable cleanup"}</h1>
          <p className="muted">
            {status.account} · currently <span className={`status status-${state === "enabled" ? "completed" : state === "error" || state === "reauth_required" ? "failed" : state === "enabling" ? "running" : "cancelled"}`}>
              {state === "enabled" ? "cleanup enabled (gmail.modify)" : state === "enabling" ? "waiting for Google" : state === "reauth_required" ? "cleanup permission expired" : state === "error" ? "enable failed" : "read-only (gmail.readonly)"}
            </span>
          </p>
        </div>
      </div>

      {state !== "enabled" && (
        <section className="card explain">
          <h2>What this changes</h2>
          <p>
            Right now Sift is read-only. To archive or trash anything, Sift asks Google for a second permission, <code>gmail.modify</code>.
            It is stored separately from the read-only sign-in, in the macOS Keychain.
          </p>

          <div className="notice notice-warn" style={{ marginTop: 14 }}>
            <strong>Google's consent screen will say Sift can "read, compose, and send emails".</strong>{" "}
            That is how Google words <code>gmail.modify</code>; it is the narrowest permission that allows archiving and labelling.
            Sift has no code that sends, replies or forwards, and its guard blocks anyone adding it.
          </div>

          <h4>What Sift will do with it</h4>
          <ul className="explain-list">
            <li><strong>Archive and label.</strong> This is the default. Each changed message gets a label like <code>Sift/2026-11-04-run-12</code>.
              It is fully reversible: restore puts the message back in the Inbox and removes the label.</li>
            <li><strong>Move to Trash, only if you choose it</strong> for a recommendation. Gmail empties Trash after about 30 days,
              and restore is only guaranteed within that window.</li>
            <li><strong>Never delete permanently</strong>, never empty Trash, never send, reply or forward.</li>
            <li><strong>Nothing changes without an approved plan.</strong> You approve an exact, frozen list of messages first.</li>
          </ul>

          <h4>The first run is a test</h4>
          <ol className="explain-list">
            <li>Your first run is capped at {n(cap)} messages from one recommendation you pick.</li>
            <li>You check the result in Gmail, then restore those messages to prove recovery works.</li>
            <li>Only after a successful restore can you unlock full runs.</li>
          </ol>

          <p className="muted" style={{ marginTop: 14 }}>
            Any plan you approved while Sift was read-only has to be approved again after you enable cleanup.
          </p>

          {state === "error" && status.cleanup.error && <div className="notice notice-error" style={{ marginTop: 14 }}>{status.cleanup.error}</div>}
          {state === "reauth_required" && (
            <div className="notice notice-warn" style={{ marginTop: 14 }}>
              {status.cleanup.error ?? "Google ended the cleanup permission."} While the Google Cloud app is in Testing mode, permissions expire after about 7 days.
              Read-only scanning still works.
            </div>
          )}

          <div className="row" style={{ marginTop: 18 }}>
            {state === "enabling" ? (
              <div className="notice" role="status">Finish in the Google tab that just opened. This page updates by itself once Google responds.</div>
            ) : (
              <button className="btn btn-primary btn-lg" disabled={busy || status.busy || status.connection.state !== "connected"} onClick={enable}>
                {state === "reauth_required" ? "Reconnect cleanup" : state === "error" ? "Try again" : "Enable cleanup (gmail.modify)"}
              </button>
            )}
            {status.busy && state !== "enabling" && <span className="muted small">Wait for the current job to finish first.</span>}
          </div>
        </section>
      )}

      {state === "enabled" && (
        <section className="card">
          <p>
            Sift holds <code>gmail.modify</code> for {status.account}. Changes still need an approved plan, and every change is labelled and logged.
          </p>
          <p className="muted" style={{ marginTop: 8 }}>
            {status.full_runs_unlocked
              ? "Full runs are unlocked."
              : `Full runs are locked: the first run is ${n(cap)} messages from one recommendation, then you restore it.`}
          </p>
          <div className="row" style={{ marginTop: 16 }}>
            <button className="btn btn-primary" onClick={() => go("recommendations")}>Choose a recommendation</button>
            <button className="btn btn-ghost" onClick={() => go("unsubscribe")}>Unsubscribe</button>
          </div>
          <h4>Disable cleanup</h4>
          <p className="muted">
            Revokes the cleanup permission at Google and deletes its token from the Keychain. The read-only connection stays, though Google may end it too.
          </p>
          {!confirmDisable ? (
            <div className="row" style={{ marginTop: 12 }}><button className="btn btn-ghost" onClick={() => setConfirmDisable(true)}>Disable cleanup…</button></div>
          ) : (
            <div className="row" style={{ marginTop: 12 }}>
              <button className="btn btn-danger" disabled={busy} onClick={disable}>Yes, disable cleanup</button>
              <button className="btn btn-link" onClick={() => setConfirmDisable(false)}>Keep it enabled</button>
            </div>
          )}
        </section>
      )}

      {disabled && (
        <div className="notice notice-cap" role="status">
          Cleanup disabled.{" "}
          {disabled.revoked_at_google ? "Google confirmed the revocation." : "Google didn't confirm the revocation."}{" "}
          {disabled.local_writer_token_deleted ? "The cleanup token was deleted from this Mac." : <span className="bad">The cleanup token could not be deleted from this Mac. Remove the "sift-gmail-writer" item in Keychain Access.</span>}
          <div style={{ marginTop: 6 }}>{disabled.note}</div>
          {disabled.problems.map((p) => <div key={p} className="bad" style={{ marginTop: 4 }}>{p}</div>)}
        </div>
      )}
      {err && <div className="notice notice-error" role="alert">{err}</div>}
    </div>
  );
}
