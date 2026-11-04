# Connecting Gmail (read-only)

This takes about 10 minutes, once. You create a private Google Cloud app that only you can use. Sift then asks it for **read-only** Gmail access (`gmail.readonly`). With that scope Sift reads headers, labels and Gmail's short preview snippet (about 200 characters); never full bodies or attachments. It cannot change, delete or send anything.

Console wording was checked against Google's docs on 2026-11-03. Google renames these pages from time to time, so if a label differs, look for the closest match.

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
5. Optional: under **Data Access → Add or Remove Scopes**, add `.../auth/gmail.readonly`. Sift requests this scope anyway, and doesn't need any other scope in this phase.

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

## Good to know
- **Sign-in lasts 7 days in Testing mode.** Google issues refresh tokens that expire after 7 days to External apps in Testing status. After that, Sift shows **Reconnect Gmail**. One click fixes it, and your scanned data is kept.
- **Scans are slow on purpose.** Gmail allows about 6,000 quota units per user per minute, and reading one message's headers costs 20 units. That's about 300 messages a minute. Sift stays just under the limit, so a 40,000-message inbox takes about 2–2½ hours. Scans checkpoint as they go: you can cancel, close the app and resume later.
- **Token limit.** Google keeps at most 100 refresh tokens per account per client. Reconnecting many times silently invalidates the oldest ones, which is harmless here.

## Disconnect and wipe
In Sift, go to **Account → Disconnect…**. This does three things:
1. Revokes Sift's access at Google (`oauth2.googleapis.com/revoke`).
2. Deletes the token from your Keychain.
3. Deletes the scanned message metadata from `data/sift-gmail.sqlite`. The audit log of past actions is kept.

To double-check or do it by hand:
- **Revoke at Google:** go to https://myaccount.google.com/connections, find **Sift** and choose **Delete all connections**.
- **Remove the Keychain item:** open **Keychain Access**, search `sift-gmail` and delete it.
- **Remove all local data:** stop Sift and delete `data/sift-gmail.sqlite*`.
- **Remove the OAuth client:** delete `~/.sift/client_secret.json`. To remove the app entirely, delete the Cloud project.
