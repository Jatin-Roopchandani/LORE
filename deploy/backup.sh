#!/usr/bin/env bash
# LORE's nightly backup (deploy/systemd/lore-backup.timer runs it at 03:30).
#
#   /srv/lore/app/deploy/backup.sh
#
# Makes $LORE_ROOT/backups/daily/<UTC stamp>/ with:
#   db.dump        pg_dump -Fc of the central database (users, sessions, sealed
#                  AI keys, chats, traces, the job queue)
#   data.tar.gz    the whole lore data volume (/data): every user's library,
#                  i.e. uploaded originals (inbox), extractions (paid for with
#                  the user's own key, so NOT treated as a cache), the Kuzu
#                  graph, LanceDB vectors, per-user SQLite and the small
#                  answer caches. Nothing in it is a cheap, re-derivable cache,
#                  so nothing is skipped by default; LORE_BACKUP_EXCLUDE takes
#                  extra tar --exclude patterns if that ever changes.
#   SHA256SUMS, info.txt
# The worker is stopped while the data volume is read (its running job goes
# back to the queue and resumes after), so the graph and vector files are not
# copied mid-write. The web app keeps serving.
#
# Keeps 7 daily and 4 weekly (Sunday's daily, hard-linked) locally. When
# R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET are set
# in the environment file and rclone is installed, each new backup is also
# copied to Cloudflare R2, which keeps the same 7 + 4.
#
# Settings: LORE_ROOT (/srv/lore), LORE_REPO ($LORE_ROOT/app), LORE_ENV_FILE
# ($LORE_ROOT/.env), LORE_COMPOSE_PROJECT (lore), LORE_BACKUP_DIR
# ($LORE_ROOT/backups), LORE_BACKUP_STOP_WORKER (1), LORE_BACKUP_EXCLUDE ("").
set -Eeuo pipefail

LORE_ROOT="${LORE_ROOT:-/srv/lore}"
REPO="${LORE_REPO:-$LORE_ROOT/app}"
export LORE_ENV_FILE="${LORE_ENV_FILE:-$LORE_ROOT/.env}"
PROJECT="${LORE_COMPOSE_PROJECT:-lore}"
BACKUPS="${LORE_BACKUP_DIR:-$LORE_ROOT/backups}"
STOP_WORKER="${LORE_BACKUP_STOP_WORKER:-1}"
DAILY_KEEP=7
WEEKLY_KEEP=4
TOOL_IMAGE=postgres:16   # already on the server (the db service); has tar and gzip

mkdir -p "$LORE_ROOT/state" "$LORE_ROOT/logs" "$BACKUPS/daily" "$BACKUPS/weekly"
exec > >(while IFS= read -r line; do printf '%s %s\n' "$(date -u +%FT%TZ)" "$line"; done \
          | tee -a "$LORE_ROOT/logs/backup.log") 2>&1
log() { echo "[backup] $*"; }

# Never at the same time as a deploy (same lock as update.sh); wait up to an hour.
exec 9>"$LORE_ROOT/state/deploy.lock"
flock -w 3600 9 || { log "FAILED: a deploy held the lock for an hour"; exit 75; }

dc() {
  docker compose -p "$PROJECT" -f "$REPO/deploy/compose.yaml" --env-file "$LORE_ENV_FILE" "$@"
}
# One value from the environment file, without sourcing it (values hold spaces).
env_get() {
  sed -n "s/^[[:space:]]*$1=//p" "$LORE_ENV_FILE" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dir="$BACKUPS/daily/$stamp"
umask 077
mkdir -p "$dir.part"
chmod 700 "$BACKUPS" "$dir.part"
log "start: $dir"

# --- the central database ---------------------------------------------------------
dc exec -T db pg_dump -U lore -d lore -Fc > "$dir.part/db.dump"
log "db.dump: $(du -h "$dir.part/db.dump" | cut -f1)"

# --- the data volume ----------------------------------------------------------------
worker_was_running=0
if [[ "$STOP_WORKER" == 1 && -n "$(dc ps -q --status running worker 2>/dev/null)" ]]; then
  worker_was_running=1
  log "stopping the worker while the data volume is read"
  dc stop worker
fi
restart_worker() {
  if [[ "$worker_was_running" == 1 ]]; then
    dc start worker && log "worker started again"
    worker_was_running=0
  fi
}
trap restart_worker EXIT

excludes=()
for pattern in ${LORE_BACKUP_EXCLUDE:-}; do excludes+=("--exclude=$pattern"); done
docker run --rm --network none -e OWNER="$(id -u):$(id -g)" \
  -v "${PROJECT}_lore-data:/data:ro" -v "$dir.part:/out" "$TOOL_IMAGE" \
  sh -c 'tar -C /data -czf /out/data.tar.gz "$@" . && chown "$OWNER" /out/data.tar.gz' \
  tar "${excludes[@]}"
restart_worker
log "data.tar.gz: $(du -h "$dir.part/data.tar.gz" | cut -f1)"

{
  echo "made:   $stamp"
  echo "commit: $(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
  echo "image:  lore:$(cat "$LORE_ROOT/state/current-tag" 2>/dev/null || echo unknown)"
  echo "alembic: $(dc exec -T db psql -U lore -d lore -tAc 'SELECT version_num FROM alembic_version' 2>/dev/null | tr -d '[:space:]')"
} > "$dir.part/info.txt"
(cd "$dir.part" && sha256sum db.dump data.tar.gz > SHA256SUMS)
mv "$dir.part" "$dir"

# --- weekly copy and retention ------------------------------------------------------
if [[ "$(date -u +%u)" == 7 || -z "$(ls -A "$BACKUPS/weekly")" ]]; then
  cp -al "$dir" "$BACKUPS/weekly/$stamp"
  log "kept as this week's backup"
fi
prune() {  # keep the newest $2 backups in $1
  ls -1d "$1"/*/ 2>/dev/null | sort -r | tail -n +"$(($2 + 1))" | while read -r old; do
    rm -rf -- "$old" && log "removed ${old%/}"
  done
}
rm -rf -- "$BACKUPS"/daily/*.part 2>/dev/null || true
prune "$BACKUPS/daily" "$DAILY_KEEP"
prune "$BACKUPS/weekly" "$WEEKLY_KEEP"

# --- optional copy to Cloudflare R2 -------------------------------------------------
r2_account="$(env_get R2_ACCOUNT_ID)"; r2_bucket="$(env_get R2_BUCKET)"
if [[ -n "$r2_account" && -n "$r2_bucket" && -n "$(env_get R2_ACCESS_KEY_ID)" ]]; then
  if ! command -v rclone >/dev/null; then
    log "warning: R2 is configured but rclone is not installed; local backup only"
  else
    # An env-only rclone remote named R2: no rclone.conf with the secret in it.
    export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
           RCLONE_CONFIG_R2_ENDPOINT="https://$r2_account.r2.cloudflarestorage.com" \
           RCLONE_CONFIG_R2_ACCESS_KEY_ID="$(env_get R2_ACCESS_KEY_ID)" \
           RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$(env_get R2_SECRET_ACCESS_KEY)" \
           RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
    # Copy (never sync: an empty local directory must not empty the bucket),
    # then age out what the local retention dropped: 7 days, 4 weeks.
    rclone copy --quiet "$dir" "R2:$r2_bucket/lore/daily/$stamp"
    if [[ -d "$BACKUPS/weekly/$stamp" ]]; then
      rclone copy --quiet "$dir" "R2:$r2_bucket/lore/weekly/$stamp"
    fi
    rclone delete --quiet --min-age 7d12h --rmdirs "R2:$r2_bucket/lore/daily"
    rclone delete --quiet --min-age 28d12h --rmdirs "R2:$r2_bucket/lore/weekly"
    log "copied to R2 bucket $r2_bucket"
  fi
fi

log "done: $dir ($(du -sh "$dir" | cut -f1))"
