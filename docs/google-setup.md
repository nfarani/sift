# Connecting Gmail (read-only), then enabling cleanup

This takes about 10 minutes, once. You create a private Google Cloud app that only you can use. Sift then asks it for **read-only** Gmail access (`gmail.readonly`). With that scope Sift reads headers, labels and Gmail's short preview snippet (about 200 characters); never full bodies or attachments. It cannot change, delete or send anything.

Changing mail needs a second, separate permission (`gmail.modify`). You grant it only when you click **Enable cleanup** (section 7).

Console wording was checked against Google's docs on 2026-11-03. The scope and OAuth details were re-checked on 2026-11-04. Google renames these pages from time to time, so if a label differs, look for the closest match.

## 1. Create a Google Cloud project
1. Open https://console.cloud.google.com/ and sign in with the Gmail account you want to clean.
2. Use the project picker (top left) and choose **New project**.
3. Name it `Sift`, then click **Create** and select the project.

## 2. Enable the Gmail API
1. Go to **APIs & Services → Library**.
2. Search for **Gmail API**, open it and click **Enable**.

## 3. Configure the consent screen (External, Testing)
1. Go to **Menu → Google Auth platform → Branding**. If you see "Google Auth platform not configured yet", click **Get Started**.
2. Fill in the app details:
   - **App information:** name it `Sift` and pick your address as **User support email**.
   - **Audience:** choose **External**.
   - **Contact information:** enter your address.
   - Accept the Google API Services User Data Policy, then click **Create**.
3. Leave the publishing status as **Testing**. Do not publish the app.
4. Go to **Audience → Test users → Add users**, add your own Gmail address and save. In Testing mode only listed test users can sign in.
5. Optional: under **Data Access → Add or Remove Scopes**, add `.../auth/gmail.readonly` and `.../auth/gmail.modify`. Sift requests them itself when needed. It never needs any other Gmail scope.

## 4. Create a Desktop OAuth client
1. Go to **Menu → Google Auth platform → Clients → Create Client**.
2. Set **Application type** to **Desktop app** and **Name** to `Sift desktop`. Click **Create**.
3. In the dialog that appears, click **Download JSON**. Google may only show the client secret at creation time. If you miss it, open the client and add a new secret.

## 5. Put the file where Sift looks for it
Sift reads the file from `~/.sift/client_secret.json`, outside the project folder. That path is the default for `GOOGLE_CLIENT_SECRETS` in `.env`.

```bash
mkdir -p ~/.sift && chmod 700 ~/.sift
mv ~/Downloads/client_secret_*.json ~/.sift/client_secret.json
chmod 600 ~/.sift/client_secret.json
```

Never put this file inside the repo. `.gitignore` blocks `*client_secret*.json` anyway.

## 6. Connect
```bash
./start.sh --gmail          # or set SIFT_PROVIDER=gmail in .env
```
1. In the browser, click **Connect Gmail (read-only)**. A Google sign-in tab opens.
2. Google will warn **"Google hasn't verified this app"**. That's expected for a private Testing app. Click **Continue**.
3. Approve **"View your email messages and settings"**. This is the read-only scope.
4. The tab says Sift is connected. Back in Sift, start with a **Test scan**.

Where things are stored:
- **Token:** the macOS Keychain, under the service name `sift-gmail`. It is never stored in the repo, the logs or the browser.
- **Scanned metadata:** `data/sift-gmail.sqlite`. It is kept separate from the synthetic mailbox's `data/sift.sqlite`.

## 7. Enable cleanup (only when you're ready to change mail)
Cleanup is off until you turn it on. To turn it on:
1. In Sift, click **Enable cleanup**. Read the explanation screen, then continue. A Google sign-in tab opens.
2. Sign in with **the same account** that's connected. If you pick a different account, Sift refuses the permission, revokes it and saves nothing.
3. Google lists the permission as **"Read, compose, and send emails from your Gmail account"**. That is Google's wording for `gmail.modify` ([Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)). Sift uses it only to add or remove labels, archive (remove the Inbox label) and move messages to Trash or back. The scope can't permanently delete mail. Sift has no code that sends or drafts mail, and its guardrail hook blocks anyone adding it.
4. Sift stores this permission as a **separate** Keychain item, `sift-gmail-writer`. The read-only item `sift-gmail` stays read-only.

Google doesn't support adding scopes to an existing grant for desktop apps ([Google OAuth for desktop apps](https://developers.google.com/identity/protocols/oauth2/native-app)), so this is a full second consent.

What enabling cleanup does **not** do:
- It changes no mail by itself. Every change needs a plan you approve. Plans approved before you enabled cleanup must be approved again.
- It doesn't unsubscribe you from anything. Unsubscribing needs its own approval for each batch. Sift only uses RFC 8058 one-click links, and shows `mailto:` unsubscribes as manual steps for you.

**Disable cleanup** (Account page) revokes the cleanup permission at Google and deletes `sift-gmail-writer`. Google links both Sift tokens to one grant, so revoking can end the read-only sign-in as well. If it does, Sift shows **Reconnect Gmail**.

## Good to know
- **Sign-in lasts 7 days in Testing mode.** Google issues refresh tokens that expire after 7 days to External apps in Testing status ([refresh token expiration](https://developers.google.com/identity/protocols/oauth2)). This applies to both tokens. When the read-only one expires, Sift shows **Reconnect Gmail**. One click fixes it, and your scanned data is kept. When the cleanup one expires, Sift shows **Reconnect cleanup**. Any run in progress stops, and its run page shows what was confirmed before it stopped.
- **Unreadable Keychain item.** If a stored token can't be read, Sift deletes it, shows a message and continues as if you weren't connected.
- **Scans are slow on purpose.** Gmail allows about 6,000 quota units per user per minute, and reading one message's headers costs 20 units. That's about 300 messages a minute. Sift stays just under the limit, so a 40,000-message inbox takes about 2–2½ hours. Scans checkpoint as they go: you can cancel, close the app and resume later.
- **Token limit.** Google keeps at most 100 refresh tokens per account per client. Reconnecting many times silently invalidates the oldest ones, which is harmless here.

## Disconnect and wipe
In Sift, go to **Account → Disconnect…**. This does four things:
1. Revokes Sift's access at Google (`oauth2.googleapis.com/revoke`), for both the read-only and the cleanup token.
2. Deletes both tokens from your Keychain (`sift-gmail` and `sift-gmail-writer`).
3. Deletes the scanned message metadata and any unsubscribe batches from `data/sift-gmail.sqlite`. The audit log and the unsubscribe log are kept. The unsubscribe log stores sender, URL host and status only.
4. Reports any Keychain item it couldn't read. Unreadable items are still deleted.

To double-check or do it by hand:
- **Revoke at Google:** go to https://myaccount.google.com/connections, find **Sift** and choose **Delete all connections**.
- **Remove the Keychain items:** open **Keychain Access**, search `sift-gmail` and delete both `sift-gmail` and `sift-gmail-writer`.
- **Remove all local data:** stop Sift and delete `data/sift-gmail.sqlite*`.
- **Remove the OAuth client:** delete `~/.sift/client_secret.json`. To remove the app entirely, delete the Cloud project.
