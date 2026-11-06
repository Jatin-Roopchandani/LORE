# Setting up sign-in for lore.jrvn.site

This takes about 30–45 minutes. Each sign-in method switches on only when its credentials are set, so you can start with any of them. **Recommended for launch: Google plus email links.** GitHub can come later.

Everything ends up in one `.env` file on the server (never committed). The full list is at the bottom.

You'll register each provider twice:
- once for **local testing**, with `http://localhost:8766` callbacks;
- once for **production**, with `https://lore.jrvn.site` callbacks.

Google and GitHub accept both kinds in one app: Google takes several redirect URIs per client, and GitHub needs a separate app for each URL.

---

## 1. Google sign-in (free)

1. Go to <https://console.cloud.google.com/> and sign in with your **personal** Google account.
2. Use the project picker at the top: **New project**, name it `LORE`, then **Create**, and select it.
3. Open **APIs & Services → OAuth consent screen**. In newer consoles this is under **Google Auth Platform → Branding**.
   - **User type:** External.
   - **App name:** `LORE`.
   - **User support email:** your email.
   - **App logo:** optional. A logo needs Google to verify your brand; you can skip it for the beta.
   - **App domain:**
     - Home page: `https://lore.jrvn.site`
     - Privacy policy: `https://lore.jrvn.site/privacy`
     - Terms of service: `https://lore.jrvn.site/terms`
   - **Authorised domains:** `jrvn.site`
   - **Developer contact:** your email.
4. **Scopes** (Data access): add only `openid`, `.../auth/userinfo.email` and `.../auth/userinfo.profile`. These are "non-sensitive", so Google doesn't need to review the app.
5. **Audience / Test users:** while the app is in **Testing**, only the emails listed here can sign in. Add yours for local tests. When you launch, click **Publish app** (Testing → In production). With only these scopes there is no review; at most Google asks you to confirm the domain.
6. **APIs & Services → Credentials → Create credentials → OAuth client ID.**
   - Application type: **Web application**, name `LORE web`.
   - **Authorised redirect URIs**, add both:
     - `https://lore.jrvn.site/auth/google/callback`
     - `http://localhost:8766/auth/google/callback` (for local testing)
   - Leave "Authorised JavaScript origins" empty; LORE does the exchange on the server.
7. Copy the **Client ID** and **Client secret**:
   ```
   LORE_GOOGLE_CLIENT_ID=…apps.googleusercontent.com
   LORE_GOOGLE_CLIENT_SECRET=…
   ```

## 2. GitHub sign-in (free, optional for launch)

GitHub OAuth apps allow one callback URL each, so make two apps.

1. Go to <https://github.com/settings/developers> as **Jatin-Roopchandani** (your personal account), then **OAuth Apps → New OAuth App**.
2. **Production app:**
   - Application name: `LORE`
   - Homepage URL: `https://lore.jrvn.site`
   - Authorization callback URL: `https://lore.jrvn.site/auth/github/callback`
   - Leave Device Flow unticked.
   - **Register application**, then **Generate a new client secret**.
3. **Local app:** the same, but named `LORE (local)` with:
   - Homepage URL: `http://localhost:8766`
   - Callback URL: `http://localhost:8766/auth/github/callback`
4. Copy each app's **Client ID** and **Client secret** into that environment's `.env`:
   ```
   LORE_GITHUB_CLIENT_ID=…
   LORE_GITHUB_CLIENT_SECRET=…
   ```

LORE asks GitHub for `read:user user:email` and uses the account's **primary verified** email.

## 3. Email sign-in links, with Resend (free for 3,000 emails a month)

1. Sign up at <https://resend.com> with your personal email.
2. **Domains → Add domain:** `jrvn.site`. Resend shows 3–4 DNS records: an SPF `TXT`, a DKIM `TXT` (or `CNAME`) and an optional `MX` for bounces.
3. At **Namecheap**, go to **Domain List → jrvn.site → Manage → Advanced DNS** and add each record exactly as Resend shows it.
   - For the **Host** field, Namecheap wants only the part before `.jrvn.site`. For example, `resend._domainkey`, not `resend._domainkey.jrvn.site`.
   - Click **Verify** in Resend. It can take from a few minutes up to an hour.
4. **API Keys → Create API key**, with permission "Sending access" and domain `jrvn.site`:
   ```
   LORE_RESEND_API_KEY=re_…
   LORE_MAIL_FROM=LORE <login@jrvn.site>
   ```
   The sender address doesn't need a real inbox. If you want replies, use an address you read.
5. **Optional but recommended:** add a DMARC record at Namecheap, a `TXT` with host `_dmarc` and value `v=DMARC1; p=none; rua=mailto:you@example.com`. Gmail and Outlook trust mail more when one exists.

## 4. Secrets made on the server

Don't make these by hand. On the server, run:
```
lore admin generate-master-key          # prints LORE_KEY_ENCRYPTION_KEY
python -c "import secrets;print(secrets.token_urlsafe(48))"   # run twice: session and CSRF secrets
```
Each secret must be at least 32 characters, and the session and CSRF secrets must be different. LORE refuses to start otherwise.

## 5. Trying it all locally first

With the local Google client or the local GitHub app, run this on your machine:
```
LORE_MULTI_USER=1
LORE_BASE_URL=http://localhost:8766
LORE_COOKIE_SECURE=0           # local http only, never in production
LORE_DATABASE_URL=sqlite:////home/jatin/lore-multi/central.db
LORE_DATA_DIR=/home/jatin/lore-multi        # a separate, empty data root, not your real library
LORE_SESSION_SECRET=…  LORE_CSRF_SECRET=…  LORE_KEY_ENCRYPTION_KEY=…
LORE_GOOGLE_CLIENT_ID=…  LORE_GOOGLE_CLIENT_SECRET=…
LORE_MAIL_CONSOLE=1            # no Resend needed: sign-in emails go to <data root>/dev-mailbox.txt (localhost only)
```
Then:
1. Run `lore migrate` (creates the central database), then `lore admin grant you@gmail.com`.
2. Start `lore ui` and open <http://localhost:8766>.
3. Go through the whole flow:
   1. the homepage;
   2. Sign in with Google;
   3. you're the admin, so you need no invite;
   4. add your AI key;
   5. upload a book;
   6. ask a question.
4. Then test a new user: from `/admin`, approve a waitlist entry (or make an invite), sign in as that second account, and check that you can't see each other's libraries.

## 6. The full production `.env`

```
LORE_MULTI_USER=1
LORE_BASE_URL=https://lore.jrvn.site
LORE_TRUST_PROXY=1                      # behind Caddy
LORE_DATABASE_URL=postgresql+psycopg://lore:…@db:5432/lore
LORE_SESSION_SECRET=…
LORE_CSRF_SECRET=…
LORE_KEY_ENCRYPTION_KEY=…
LORE_GOOGLE_CLIENT_ID=…
LORE_GOOGLE_CLIENT_SECRET=…
LORE_GITHUB_CLIENT_ID=…                 # optional
LORE_GITHUB_CLIENT_SECRET=…             # optional
LORE_RESEND_API_KEY=…
LORE_MAIL_FROM=LORE <login@jrvn.site>
# limits (defaults shown): LORE_UPLOADS_PER_DAY=10  LORE_UPLOADS_PER_HOUR=30  LORE_MAX_BOOKS=30
```
`LORE_INVITE_ONLY` defaults to on in multi mode. Keep it on for the beta. Step 6 adds rate-limit variables; its report lists them.

## Checklist

- [ ] Google project, consent screen with the privacy and terms links, the 3 scopes, and a web client with both redirect URIs
- [ ] (optional) Two GitHub OAuth apps: production and local
- [ ] Resend domain verified at Namecheap (SPF and DKIM), plus an API key
- [ ] DMARC record (recommended)
- [ ] The local multi-user run works from start to finish
- [ ] At launch: Google app published (Testing → In production)
