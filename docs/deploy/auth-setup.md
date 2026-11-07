# Setting up sign-in for lore.jrvn.site

This takes about 30–45 minutes. Each sign-in method switches on only when its credentials are set, so you can start with any of them. **Recommended for launch: Google plus email links.** GitHub can come later.

Everything ends up in one `.env` file on the server (never committed). The full list is at the bottom.

You'll register each provider twice:
- once for **local testing**, with `http://localhost:8767` callbacks (the local multi-user trial);
- once for **production**, with `https://lore.jrvn.site` callbacks.

Google takes several redirect URIs in one client. GitHub needs a separate app for each URL.

**Where the credentials go:** never paste them into a chat. Put them in two private files on your machine:
- local trial: `~/.config/lore/multi-dev.env`
- production: `~/.config/lore/deploy/secrets.env`

Create and open them with:
```
mkdir -p ~/.config/lore/deploy && touch ~/.config/lore/deploy/secrets.env && chmod 600 ~/.config/lore/deploy/secrets.env ~/.config/lore/multi-dev.env
notepad.exe "$(wslpath -w ~/.config/lore/deploy/secrets.env)"
```

---

## 0. Prove to Google that you own jrvn.site (about 10 minutes, plus DNS time)

Google won't let the sign-in app be published for `jrvn.site` until the same Google account has verified the domain. Do this first. It works with your DNS at Namecheap today.

1. Go to <https://search.google.com/search-console> with the **same personal Google account** you'll use for Google Cloud.
2. **Add property → Domain** (the left box, not "URL prefix"), enter `jrvn.site`, then **Continue**.
3. Google shows a **TXT record**, `google-site-verification=…`. Click **Copy**.
4. At **Namecheap**, go to **Domain List → jrvn.site → Manage → Advanced DNS → Add new record**:
   - Type **TXT Record**, Host `@`, Value: paste the whole `google-site-verification=…` string, TTL **Automatic**.
   - Click the ✓ to save.
5. Back in Search Console, click **Verify**. If it says "not found", wait 10–30 minutes and click **Verify** again. DNS takes time.
6. **Leave the TXT record in place for good.** Google re-checks it now and then.

**Later, when DNS moves to Cloudflare:** Cloudflare imports your existing records when you add the site. Check that this TXT record is in Cloudflare's DNS list before changing nameservers, or add it there by hand.

## 1. Google sign-in (free)

1. Go to <https://console.cloud.google.com/> and sign in with your **personal** Google account.
2. Use the project picker at the top: **New project**, name it `LORE`, then **Create**, and select it.
3. Open ☰ → **APIs & Services → OAuth consent screen**. The current console calls this **Google Auth Platform**. Click **Get started** and fill in **App information** (name `LORE`, your support email), **Audience: External**, and **Contact information** (your email). Accept the policy and click **Create**. Then, under **Branding**:
   - **User type:** External.
   - **App name:** `LORE`.
   - **User support email:** your email.
   - **App logo:** optional. A logo needs Google to verify your brand; you can skip it for the beta.
   - **App domain:**
     - Home page: `https://lore.jrvn.site`
     - Privacy policy: `https://lore.jrvn.site/privacy`
     - Terms of service: `https://lore.jrvn.site/terms`
   - **Authorised domains:** `jrvn.site`. This must be verified in step 0, with the same Google account.
   - **Developer contact:** your email.
4. **Data access → Add or remove scopes:** tick only `openid`, `.../auth/userinfo.email` and `.../auth/userinfo.profile`. These are "non-sensitive", so Google doesn't need to review the app.
5. **Audience → Test users → Add users:** while the app is in **Testing**, only the emails listed here can sign in. Add yours for local tests. When you launch, click **Publish app** (Testing → In production). With only these scopes there is no review; at most Google asks you to confirm the domain.
6. **Clients → Create client** (older consoles: APIs & Services → Credentials → Create credentials → OAuth client ID).
   - Application type: **Web application**, name `LORE web`.
   - **Authorised redirect URIs**, add both:
     - `https://lore.jrvn.site/auth/google/callback`
     - `http://localhost:8767/auth/google/callback` (for the local multi-user trial)
   - Leave "Authorised JavaScript origins" empty; LORE does the exchange on the server.
7. Click **Create**. Copy the **Client ID** and **Client secret** straight into both files, or click **Download JSON** and keep it private. Newer consoles show the secret only once.
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
   - Homepage URL: `http://localhost:8767`
   - Callback URL: `http://localhost:8767/auth/github/callback`
4. Copy the production app's **Client ID** and **Client secret** into `deploy/secrets.env`, and the local app's into `multi-dev.env`:
   ```
   LORE_GITHUB_CLIENT_ID=…
   LORE_GITHUB_CLIENT_SECRET=…
   ```

LORE asks GitHub for `read:user user:email` and uses the account's **primary verified** email.

## 3. Email sign-in links, with Resend (free for 3,000 emails a month)

1. Sign up at <https://resend.com> with your personal email.
2. **Domains → Add domain:** `jrvn.site`, with a region close to you (e.g. Tokyo or Singapore, for India). Resend shows 3–4 DNS records: an SPF `TXT`, a DKIM `TXT` (or `CNAME`) and an `MX` for bounces, usually on a `send` subdomain.
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

The trial on :8767 is already set up this way (`~/.config/lore/multi-dev.env`). Add the Google and GitHub local-client lines to that file, and ask Claude to restart the trial:
```
LORE_MULTI_USER=1
LORE_BASE_URL=http://localhost:8767
LORE_COOKIE_SECURE=0           # local http only, never in production
LORE_DATABASE_URL=sqlite:////home/jatin/lore-multi/central.db
LORE_DATA_DIR=/home/jatin/lore-multi        # a separate, empty data root, not your real library
LORE_SESSION_SECRET=…  LORE_CSRF_SECRET=…  LORE_KEY_ENCRYPTION_KEY=…
LORE_GOOGLE_CLIENT_ID=…  LORE_GOOGLE_CLIENT_SECRET=…
LORE_MAIL_CONSOLE=1            # no Resend needed: sign-in emails go to <data root>/dev-mailbox.txt (localhost only)
```
Then:
1. Run `lore migrate` (creates the central database), then `lore admin grant you@gmail.com`.
2. Start `lore ui --port 8767` and open <http://localhost:8767>.
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
LORE_TRUST_PROXY=1                      # behind the Cloudflare Tunnel
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

## 7. Connecting Claude to LORE (MCP)

Readers connect claude.ai, Claude Desktop, Claude Code or Cursor to their own library at `https://lore.jrvn.site/mcp`: claude.ai and Claude Desktop through a custom connector (OAuth, using the sign-in set up above), the others with a personal access token from Settings → MCP connector. Nothing extra to configure; the steps a reader follows are in [mcp.md](mcp.md).

## Checklist

- [ ] jrvn.site verified in Google Search Console (TXT record at Namecheap, kept forever)
- [ ] Google project, consent screen with the privacy and terms links, the 3 scopes, and a web client with both redirect URIs
- [ ] (optional) Two GitHub OAuth apps: production and local
- [ ] Resend domain verified at Namecheap (SPF and DKIM), plus an API key
- [ ] DMARC record (recommended)
- [ ] The local multi-user run works from start to finish
- [ ] At launch: Google app published (Testing → In production)
