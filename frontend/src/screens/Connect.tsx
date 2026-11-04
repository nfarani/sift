import { useState } from "react";
import { api, Status } from "../api";

export default function Connect({ status, onChange }: { status: Status; onChange: () => void }) {
  const [err, setErr] = useState<string | null>(null);
  const st = status.connection.state;

  const connect = async () => {
    setErr(null);
    try { await api.post("/gmail/connect"); onChange(); } catch (e) { setErr((e as Error).message); }
  };

  return (
    <section className="empty-hero">
      <h1>{st === "reauth_required" ? "Reconnect Gmail" : "Connect your Gmail"}</h1>
      {st === "reauth_required" ? (
        <p>Google ended Sift's sign-in. While the Google Cloud app is in Testing mode, sign-ins expire after 7 days. Reconnecting takes a few seconds.</p>
      ) : (
        <p>Sift asks Google for <strong>read-only</strong> access (<code>gmail.readonly</code>). It reads headers, labels and Gmail's short preview snippet to analyse your inbox, and it cannot change, delete or send anything.</p>
      )}
      <ul className="connect-points">
        <li>Full message bodies and attachments are never downloaded; only Gmail’s ~200-character preview snippet.</li>
        <li>Nothing is marked as read.</li>
        <li>Your sign-in token is kept in the macOS Keychain.</li>
        <li>Disconnect anytime from Account.</li>
      </ul>
      {status.client_secrets_present === false && (
        <div className="notice notice-warn">The Google OAuth client file isn't in place yet. Follow <code>docs/google-setup.md</code>, then come back.</div>
      )}
      {st === "connecting" ? (
        <div className="notice">Finish signing in in the browser tab that just opened. This page updates by itself.</div>
      ) : (
        <button className="btn btn-primary btn-lg" disabled={status.client_secrets_present === false} onClick={connect}>
          {st === "reauth_required" ? "Reconnect Gmail (read-only)" : "Connect Gmail (read-only)"}
        </button>
      )}
      {(err || status.connection.error) && <div className="notice notice-error" style={{ marginTop: 16 }}>{err || status.connection.error}</div>}
    </section>
  );
}
