# The production server: first-time setup

LORE runs on one Oracle Cloud A1 VM (arm64, 1 OCPU, 6 GB RAM + 4 GB swap, Ubuntu 24.04, Docker 29 with Compose v5). SSH is the only open port. The public reaches `https://lore.jrvn.site` only through a Cloudflare Tunnel: `cloudflared` runs in the stack and connects out to Cloudflare, which sends requests to `http://web:8000` inside the compose network.

| Piece | Where |
|---|---|
| Git checkout (read-only deploy key) | `/srv/lore/app` |
| Environment (secrets), mode 600 | `/srv/lore/.env` |
| Deploy and backup logs | `/srv/lore/logs/deploy.log`, `/srv/lore/logs/backup.log` |
| Deploy state (current and previous image tag, lock) | `/srv/lore/state/` |
| Backups (7 daily, 4 weekly; pre-deploy dumps) | `/srv/lore/backups/` |
| Compose project | `lore` (`deploy/compose.yaml`): `web`, `worker`, `db`, `ollama`, `cloudflared`, one-shot `migrate` |
| Volumes | `lore_lore-data` (every library), `lore_pg-data` (Postgres), `ollama` (the model; external) |

Every command below runs on the server as the login user (`ubuntu` here; use yours), who must be in the `docker` group (`groups` lists it). A shortcut used throughout:

```
lore-dc() { docker compose -p lore -f /srv/lore/app/deploy/compose.yaml --env-file /srv/lore/.env "$@"; }
```
Add that line to `~/.bashrc` so it is there in every session.

## 1. Directories and a read-only deploy key

```
sudo mkdir -p /srv/lore/{logs,state,backups}
sudo chown -R "$USER:$USER" /srv/lore
ssh-keygen -t ed25519 -N "" -C "lore-server deploy key" -f ~/.ssh/lore_deploy
cat ~/.ssh/lore_deploy.pub
```
On GitHub, open the repository's **Settings → Deploy keys → Add deploy key**, paste the public key, title it `lore-server`, and leave **Allow write access** unticked. Then:

```
cat >> ~/.ssh/config <<'EOF'
Host github-lore
  HostName github.com
  User git
  IdentityFile ~/.ssh/lore_deploy
  IdentitiesOnly yes
EOF
ssh -T github-lore            # "Hi Jatin-Roopchandani/LORE! You've successfully authenticated..."
git clone github-lore:Jatin-Roopchandani/LORE.git /srv/lore/app
```
(Use the repository's real name if it differs.) `update.sh` checks out `origin/release` by default.

## 2. `/srv/lore/.env`

```
install -m 600 /srv/lore/app/deploy/.env.example /srv/lore/.env
```
Then fill it in with `nano /srv/lore/.env`. Every variable is explained in the file. The secrets made on the server:

- **`POSTGRES_PASSWORD`**: `openssl rand -hex 24`. It goes into a connection URL, so it must stay URL-safe (hex is).
- **`LORE_SESSION_SECRET`** and **`LORE_CSRF_SECRET`**: run `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` twice and use a different value for each. LORE refuses to start if one is shorter than 32 characters or the two are the same.
- **`LORE_KEY_ENCRYPTION_KEY`**: the master key that seals every user's stored AI key. Make it with LORE itself; this needs the image, so do it after the first build (step 4), or build it now with `docker build -f /srv/lore/app/deploy/Dockerfile -t lore:latest /srv/lore/app` (5–15 minutes) and then run:
  ```
  docker run --rm lore:latest lore admin generate-master-key
  ```
  Paste the printed key into `.env`. Also keep a copy **off the server** (a password manager): the backups do not contain it, and without it every stored AI key is lost (users would have to enter theirs again).

Don't paste a value into a terminal command that ends up in shell history. Edit the file instead. The sign-in credentials (Google, GitHub, Resend) come from [auth-setup.md](auth-setup.md).

Check the mode: `stat -c %a /srv/lore/.env` must print `600`.

## 3. The Cloudflare Tunnel token

DNS for `jrvn.site` must be on Cloudflare.

1. Open <https://one.dash.cloudflare.com/> → **Networks → Tunnels → Create a tunnel** → **Cloudflared**. Name it `lore`.
2. On the "Install and run a connector" page, choose **Docker**. The command shown ends with `--token eyJ…`. Copy only that long token into `/srv/lore/.env` as `CLOUDFLARE_TUNNEL_TOKEN=eyJ…`. Do not run the command it shows: compose runs the connector.
3. **Next → Public hostname:** subdomain `lore`, domain `jrvn.site`, path empty. Service: type **HTTP**, URL **`web:8000`**. Save.
   - Leave **HTTP Host Header** empty, so cloudflared passes `lore.jrvn.site` through. LORE answers only the Host in `LORE_BASE_URL` and refuses any other.
4. Keep `COMPOSE_PROFILES=tunnel` in `.env`, which starts `cloudflared` with the stack.

## 4. Adopt the running Ollama

The container `ollama` (volume `ollama`, `nomic-embed-text` pulled, `OLLAMA_KEEP_ALIVE=-1`) becomes the compose service `ollama`, with the same container name and volume, so the model is not downloaded again:

```
docker inspect ollama --format '{{.Config.Image}}'     # e.g. ollama/ollama:latest -> OLLAMA_IMAGE in .env
docker volume inspect ollama >/dev/null && echo "volume ok"
docker rm -f ollama                                      # the container only; the volume stays
```
The first `update.sh` starts it again under compose. It is not published on the host any more; only `web` and `worker` reach it, at `http://ollama:11434`. If the model is ever missing, `update.sh` pulls it.

## 5. First deploy

```
/srv/lore/app/deploy/update.sh            # or: update.sh origin/release, update.sh <commit>
```
The first run builds the image natively on arm64, which takes 5–15 minutes on 1 OCPU. It then:
1. starts `db` and `ollama`;
2. dumps the (empty) database;
3. runs the migrations;
4. starts `worker`, `web` and `cloudflared`;
5. waits for `/healthz`.

Follow it in `/srv/lore/logs/deploy.log`. A failure leaves the previous image running (on the first deploy there is none, so `web` and `worker` stay stopped) and says why.

## 6. The first admin

```
lore-dc run --rm --no-deps web lore admin grant you@gmail.com
```
Use the email you'll sign in with. Sign in at <https://lore.jrvn.site>: you are the admin and need no invite. `/admin` hands out invites.

## 7. Checking health

```
lore-dc ps                                   # web "healthy"; db and ollama healthy; worker and cloudflared running
lore-dc exec web python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/healthz').read())"
curl -sS https://lore.jrvn.site/healthz      # {"ok":true} through the tunnel
lore-dc logs --tail 50 web worker cloudflared
docker stats --no-stream                     # memory per container against its limit
```
`/healthz` answers `{"ok":true}` while the central database answers, and `503 {"ok":false}` otherwise, with no detail; the details are in `lore-dc logs web`.

## 8. Backups

```
sudo cp /srv/lore/app/deploy/systemd/lore-backup.service /srv/lore/app/deploy/systemd/lore-backup.timer /etc/systemd/system/
sudo sed -i "s/^User=.*/User=$USER/; s/^Group=.*/Group=$USER/" /etc/systemd/system/lore-backup.service
sudo systemctl daemon-reload
sudo systemctl enable --now lore-backup.timer
sudo systemctl start lore-backup.service     # one run now, to check it
systemctl list-timers lore-backup.timer
tail -20 /srv/lore/logs/backup.log
```
Each night at 03:30 this writes `/srv/lore/backups/daily/<stamp>/`, holding `db.dump`, `data.tar.gz`, `SHA256SUMS` and `info.txt`, and keeps 7 daily and 4 weekly backups. What it keeps, and why, is in the header of `deploy/backup.sh`.

Instead of systemd, a cron line works too: `crontab -e`, then
```
30 3 * * * /srv/lore/app/deploy/backup.sh >/dev/null 2>&1
```

**Off-site copy to Cloudflare R2 (optional, recommended):**
1. In the Cloudflare dashboard, go to **R2 → Create bucket** and name it `lore-backups`.
2. Go to **R2 → Manage API tokens → Create API token**, with permission **Object Read & Write** on that bucket only.
3. Put the four values in `.env`: `R2_ACCOUNT_ID` (shown on the R2 overview), `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and `R2_BUCKET`.
4. Install rclone: `sudo apt-get install -y rclone`.

The next run copies each backup to `lore/daily/` and `lore/weekly/` in the bucket and ages out old ones, keeping the same 7 + 4. The backups hold the users' books and sealed (not plain) AI keys, so keep the bucket private.

Restoring is in [`deploy/restore.md`](../../deploy/restore.md).

## 9. Updating

```
/srv/lore/app/deploy/update.sh                 # the newest origin/release
/srv/lore/app/deploy/update.sh <commit-or-tag> # a given one (also how to go back)
```
Each run:
1. fetches and builds `lore:<commit>`;
2. backs up the database into `/srv/lore/backups/predeploy/` (the newest 5 are kept);
3. migrates;
4. restarts `worker` and `web`;
5. waits for `/healthz`.

If `web` is not healthy within 3 minutes, the previous image goes back on. The database backup is restored only when a migration itself failed. One run at a time: a second one exits with "another deploy is running".

Don't restart the stack while someone's question is streaming if you can avoid it. A restart cuts that answer off, and a worker that is stopped hands its job back to the queue, where it resumes.

## 10. Resizing to 2 OCPU / 12 GB later

1. In the OCI console, open **Compute → Instances → the instance → Edit → Shape** and set 2 OCPUs and 12 GB (the A1 flex shape). The instance reboots, and Docker brings the stack back (`restart: unless-stopped`).
2. Give the services room. Edit the `mem_limit` lines in `deploy/compose.yaml` on a branch (for example web `3g`, worker `4g`, db `1g` with `shared_buffers=256MB` and `effective_cache_size=1GB`), merge into `release`, and run `update.sh`. Ollama stays at `1g`.
3. Optionally run a second worker, to process two users' books at once:
   ```
   lore-dc up -d --no-deps --scale worker=2 worker
   ```
   A user's own jobs never overlap.
4. The swap file can stay.

## Deploy on push, later (not enabled)

Today the server pulls: someone runs `update.sh`. To deploy on every push to `release` later, add a job to `.github/workflows/ci.yml` that runs after the tests:

```yaml
  deploy:
    needs: [web, python, postgres]
    if: github.event_name == 'push' && github.ref == 'refs/heads/release'
    runs-on: ubuntu-24.04
    environment: production          # a protected environment: approval, secrets
    steps:
      - name: update.sh over SSH
        env:
          SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
          KNOWN_HOSTS: ${{ secrets.DEPLOY_KNOWN_HOSTS }}
          SHA: ${{ github.sha }}
        run: |
          install -m 700 -d ~/.ssh
          printf '%s\n' "$SSH_KEY" > ~/.ssh/id && chmod 600 ~/.ssh/id
          printf '%s\n' "$KNOWN_HOSTS" > ~/.ssh/known_hosts
          ssh -i ~/.ssh/id deploy@<server> "$SHA"
```
On the server, a dedicated `deploy` user's `authorized_keys` line pins that key to one command, so the key can do nothing else:
```
command="/srv/lore/app/deploy/update.sh \"$SSH_ORIGINAL_COMMAND\"",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA… github-actions
```
Two more things are needed:
- SSH must be reachable from GitHub's runners. Today it is open; to close it later, use Cloudflare Access for SSH, or a self-hosted runner on the VM.
- Validate `$SSH_ORIGINAL_COMMAND` as a 40-character hex SHA before passing it on. Wrap update.sh in a small script to do this.
