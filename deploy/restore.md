# Restoring LORE from a backup

A backup is a directory made by `deploy/backup.sh`:

```
/srv/lore/backups/daily/<YYYYMMDDTHHMMSSZ>/      (or weekly/<stamp>/)
  db.dump        pg_dump -Fc of the central database
  data.tar.gz    the lore data volume (every user's library)
  SHA256SUMS     checksums of the two
  info.txt       when, which commit and image, which migration
```
`update.sh` also leaves a database-only dump before each deploy, in `/srv/lore/backups/predeploy/lore-<stamp>-<tag>.dump`.

Restore the database and the data volume **from the same backup**: the central database's users, books and jobs must match the libraries on disk. Every step was run end to end on a scratch stack before this was written (see `.superpowers/sdd/deploy-report.md`).

All commands run on the server, as the user who owns `/srv/lore`:

```
lore-dc() { docker compose -p lore -f /srv/lore/app/deploy/compose.yaml --env-file /srv/lore/.env "$@"; }
B=/srv/lore/backups/daily/20261108T033000Z      # the backup to restore
```

## 0. Check the backup

```
(cd "$B" && sha256sum -c SHA256SUMS)          # both "OK"
cat "$B/info.txt"
```
To go back fully, also run the code that made the backup: `deploy/update.sh <commit from info.txt>`. Do it **after** the restore, or **before** it if the newer code's migrations have already moved the schema past the dump.

## 1. Stop what writes

```
lore-dc stop web worker
```
`db` keeps running. The site shows Cloudflare's error page until step 4.

## 2. The central database

```
lore-dc exec -T db psql -U lore -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS lore WITH (FORCE)" \
  -c "CREATE DATABASE lore OWNER lore"
lore-dc exec -T db pg_restore -U lore -d lore --no-owner --exit-on-error < "$B/db.dump"
lore-dc exec -T db psql -U lore -d lore -tAc "SELECT version_num FROM alembic_version"   # matches info.txt
```

## 3. The data volume

This **replaces** everything in the volume:
```
docker run --rm --network none -v lore_lore-data:/data -v "$B:/backup:ro" postgres:16 \
  sh -c 'find /data -mindepth 1 -delete && tar -C /data -xzf /backup/data.tar.gz && chown -R 10001:10001 /data'
```
`10001` is the `lore` user inside the image. The volume is `lore_lore-data` for the compose project `lore`.

## 4. Start again

```
lore-dc up -d --no-deps worker web
lore-dc ps                                     # web becomes "healthy" within a minute
curl -sS https://lore.jrvn.site/healthz        # {"ok":true}
```
Sign in and open a library to check that the books are there.

## Only the database (a pre-deploy dump)

`update.sh` restores its own pre-deploy dump when a migration fails. To do it by hand, follow steps 1, 2 and 4 with the predeploy file in place of `$B/db.dump`.

## From Cloudflare R2 (the server's own copies are gone)

```
sudo apt-get install -y rclone
export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
       RCLONE_CONFIG_R2_ENDPOINT="https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com" \
       RCLONE_CONFIG_R2_ACCESS_KEY_ID=… RCLONE_CONFIG_R2_SECRET_ACCESS_KEY=…
rclone lsf R2:lore-backups/lore/daily/                     # the stamps available
mkdir -p /srv/lore/backups/restore
rclone copy R2:lore-backups/lore/daily/<stamp> /srv/lore/backups/restore/<stamp>
B=/srv/lore/backups/restore/<stamp>
```
Type the two secrets into an editor-created file and `source` it, rather than on the command line, so they stay out of shell history. Then follow steps 0–4.

## A new server (disaster recovery)

1. Set the server up as in `docs/deploy/server.md`, steps 1–4. Use the **same** `LORE_KEY_ENCRYPTION_KEY` (from your off-server copy); without it, the restored users' stored AI keys cannot be opened, and they will have to enter them again. The other secrets can be new, which signs everyone out.
2. Run `deploy/update.sh <commit from info.txt>` once. It creates the volumes and an empty, migrated database.
3. Fetch the backup (from R2, above), then follow steps 0–4.
4. Point the Cloudflare Tunnel at the new server. Either put the same `CLOUDFLARE_TUNNEL_TOKEN` in `.env`, or create a new tunnel with the same public hostname.
