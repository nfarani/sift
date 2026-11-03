import { useState } from "react";
import { api, Status } from "../api";

export default function Account({ status, onChange }: { status: Status | null; onChange: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (!status) return <div className="loading">Loading…</div>;

  const disconnect = async () => {
    setErr(null);
    try {
      const r = await api.post<{ revoked_at_google: boolean }>("/gmail/disconnect");
      setResult(r.revoked_at_google
        ? "Disconnected. Google confirmed the access was revoked, and the token and scanned metadata were deleted from this Mac."
        : "Disconnected locally: the token and scanned metadata were deleted. Google didn't confirm the revocation (it may already have expired). You can also remove Sift at myaccount.google.com → Security → Third-party access.");
      setConfirming(false);
      onChange();
    } catch (e) { setErr((e as Error).message); }
  };

  const connected = status.connection.state === "connected";
  return (
    <div className="stack-lg narrow">
      <div className="page-head"><div><h1>Account</h1><p className="muted">Connection, permissions and stored credentials.</p></div></div>
      <section className="card">
        <table className="table">
          <tbody>
            <tr><td>Mailbox</td><td>{status.synthetic ? "Synthetic mock mailbox" : status.provider === "gmail" ? "Gmail" : status.provider}</td></tr>
            <tr><td>Account</td><td>{status.account ?? "not connected"}</td></tr>
            <tr><td>Connection</td><td><span className={`status status-${connected ? "completed" : status.connection.state === "error" || status.connection.state === "reauth_required" ? "failed" : "cancelled"}`}>{status.connection.state.replace("_", " ")}</span></td></tr>
            <tr><td>Access</td><td>{status.mode}</td></tr>
            {status.credential_store && <tr><td>Credentials stored in</td><td>{status.credential_store} (never in the project folder, logs or this page)</td></tr>}
            <tr><td>AI classification</td><td>{status.classifier.label}</td></tr>
          </tbody>
        </table>
        {status.connection.error && <div className="notice notice-error">{status.connection.error}</div>}
      </section>

      {status.provider === "gmail" && connected && (
        <section className="card">
          <h2>Disconnect Gmail</h2>
          <p className="muted">
            Revokes Sift's access at Google, deletes the token from your Keychain, and deletes the message metadata Sift scanned.
            Your mail itself is not touched. The audit log of past actions is kept.
          </p>
          {!confirming ? (
            <div className="row" style={{ marginTop: 14 }}><button className="btn btn-ghost" onClick={() => setConfirming(true)}>Disconnect…</button></div>
          ) : (
            <div className="row" style={{ marginTop: 14 }}>
              <button className="btn btn-danger" onClick={disconnect}>Yes, disconnect and delete local data</button>
              <button className="btn btn-link" onClick={() => setConfirming(false)}>Keep connected</button>
            </div>
          )}
        </section>
      )}
      {result && <div className="notice notice-cap">{result}</div>}
      {err && <div className="notice notice-error">{err}</div>}
    </div>
  );
}
