#!/usr/bin/env bash
# LORE's server-side updater. Run on the server as the deploy user:
#
#   /srv/lore/app/deploy/update.sh [ref]        # ref defaults to origin/release
#
# 1. git fetch, then check out <ref> (detached) in the checkout;
# 2. build the image, tagged lore:<commit>;
# 3. back up the central database (pg_dump);
# 4. run the migrations (the one-shot `migrate` service);
# 5. restart worker and web on the new image;
# 6. wait for /healthz.
# On a failure it puts the previous image back on web and worker, and restores
# the database backup ONLY when a migration ran and failed. One run at a time
# (a lock); everything is logged to $LORE_ROOT/logs/deploy.log.
#
# Settings (environment, all optional):
#   LORE_ROOT            /srv/lore             state, logs and backups live here
#   LORE_REPO            $LORE_ROOT/app        the git checkout
#   LORE_ENV_FILE        $LORE_ROOT/.env       the production environment
#   LORE_COMPOSE_PROJECT lore                  the compose project name
#   LORE_HEALTH_TIMEOUT  180                   seconds to wait for /healthz
#   LORE_BUILD_NICE      10                    nice level for the image build
set -Eeuo pipefail

# The checkout below replaces this very file; bash reads a script as it runs,
# so run from a private copy.
if [[ -z "${LORE_UPDATE_COPY:-}" ]]; then
  copy="$(mktemp "${TMPDIR:-/tmp}/lore-update.XXXXXX")"
  cp -- "${BASH_SOURCE[0]}" "$copy"
  LORE_UPDATE_COPY="$copy" exec bash "$copy" "$@"
fi
trap 'rm -f -- "$LORE_UPDATE_COPY"' EXIT

LORE_ROOT="${LORE_ROOT:-/srv/lore}"
REPO="${LORE_REPO:-$LORE_ROOT/app}"
export LORE_ENV_FILE="${LORE_ENV_FILE:-$LORE_ROOT/.env}"
PROJECT="${LORE_COMPOSE_PROJECT:-lore}"
HEALTH_TIMEOUT="${LORE_HEALTH_TIMEOUT:-180}"
BUILD_NICE="${LORE_BUILD_NICE:-10}"
REF="${1:-origin/release}"

STATE="$LORE_ROOT/state"
LOGS="$LORE_ROOT/logs"
PREDEPLOY="$LORE_ROOT/backups/predeploy"
mkdir -p "$STATE" "$LOGS" "$PREDEPLOY"
LOG="$LOGS/deploy.log"

exec 9>"$STATE/deploy.lock"
if ! flock -n 9; then
  echo "update.sh: another deploy is running (lock $STATE/deploy.lock)" >&2
  exit 75
fi

exec > >(while IFS= read -r line; do printf '%s %s\n' "$(date -u +%FT%TZ)" "$line"; done | tee -a "$LOG") 2>&1

log() { echo "[deploy] $*"; }
die() { log "FAILED: $*"; exit 1; }

dc() {
  docker compose -p "$PROJECT" -f "$REPO/deploy/compose.yaml" --env-file "$LORE_ENV_FILE" "$@"
}

[[ -f "$LORE_ENV_FILE" ]] || die "no environment file at $LORE_ENV_FILE (see deploy/.env.example)"
if [[ "$(stat -c %a "$LORE_ENV_FILE")" != 600 ]]; then
  log "warning: $LORE_ENV_FILE should be mode 600 (chmod 600 $LORE_ENV_FILE)"
fi
[[ -d "$REPO/.git" ]] || die "no git checkout at $REPO"

previous_tag="$(cat "$STATE/current-tag" 2>/dev/null || true)"
previous_commit="$(git -C "$REPO" rev-parse HEAD)"

log "start: ref=$REF previous=${previous_tag:-none}"

# --- 1. fetch and check out -------------------------------------------------------
git -C "$REPO" fetch --prune --tags origin
git -C "$REPO" checkout --quiet --detach "$REF" || die "cannot check out $REF"
commit="$(git -C "$REPO" rev-parse --short=12 HEAD)"
tag="$commit"
log "checked out $(git -C "$REPO" log -1 --format='%h %s')"

back_to_previous_checkout() {
  git -C "$REPO" checkout --quiet --detach "$previous_commit" || true
}

# --- 2. build ---------------------------------------------------------------------
if docker image inspect "lore:$tag" >/dev/null 2>&1; then
  log "image lore:$tag already built"
else
  log "building lore:$tag"
  if ! nice -n "$BUILD_NICE" docker build -f "$REPO/deploy/Dockerfile" \
        --label "org.opencontainers.image.revision=$commit" -t "lore:$tag" "$REPO"; then
    back_to_previous_checkout
    die "image build failed; nothing was changed"
  fi
fi

# --- 3. database and Ollama up, then a backup ---------------------------------------
log "starting db and ollama"
LORE_IMAGE_TAG="${previous_tag:-$tag}" dc up -d --wait db ollama \
  || { back_to_previous_checkout; die "db or ollama did not become healthy"; }

if ! dc exec -T ollama ollama show nomic-embed-text >/dev/null 2>&1; then
  log "pulling nomic-embed-text into ollama"
  dc exec -T ollama ollama pull nomic-embed-text || log "warning: model pull failed"
fi

backup="$PREDEPLOY/lore-$(date -u +%Y%m%dT%H%M%SZ)-$tag.dump"
log "backing up the database to $backup"
if ! dc exec -T db pg_dump -U lore -d lore -Fc > "$backup.part"; then
  rm -f "$backup.part"
  back_to_previous_checkout
  die "database backup failed; nothing was changed"
fi
mv "$backup.part" "$backup"
chmod 600 "$backup"
# keep the newest 5 pre-deploy dumps
ls -1t "$PREDEPLOY"/lore-*.dump 2>/dev/null | tail -n +6 | xargs -r rm -f --

# --- 4. migrations ------------------------------------------------------------------
revision() {
  dc exec -T db psql -U lore -d lore -tAc \
    "SELECT version_num FROM alembic_version" 2>/dev/null | tr -d '[:space:]' || true
}
before="$(revision)"
head="$(docker run --rm --entrypoint python "lore:$tag" -c \
  'from alembic.script import ScriptDirectory
from lore.central.migrate import alembic_config
print(ScriptDirectory.from_config(alembic_config("")).get_current_head())' | tr -d '[:space:]')" \
  || head=""
log "migrations: database at ${before:-none}, image head ${head:-unknown}"

restore_database() {
  log "restoring the database from $backup"
  dc stop web worker || true
  dc exec -T db psql -U lore -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS lore WITH (FORCE)" -c "CREATE DATABASE lore OWNER lore"
  dc exec -T db pg_restore -U lore -d lore --no-owner --exit-on-error < "$backup"
  log "database restored to ${before:-empty}"
}

wait_healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) id state
  while (( SECONDS < deadline )); do
    id="$(dc ps -q web 2>/dev/null || true)"
    if [[ -n "$id" ]]; then
      state="$(docker inspect -f '{{.State.Status}}' "$id" 2>/dev/null || true)"
      if [[ "$state" == running ]] && dc exec -T web python -c \
          'import sys,urllib.request as u; sys.exit(0 if u.urlopen("http://127.0.0.1:8000/healthz", timeout=4).status == 200 else 1)' \
          >/dev/null 2>&1; then
        return 0
      fi
    fi
    sleep 3
  done
  return 1
}

restart_previous() {
  if [[ -z "$previous_tag" ]]; then
    log "no previous image to go back to (first deploy): web and worker stay stopped"
    dc stop web worker || true
    return
  fi
  log "rolling back web and worker to lore:$previous_tag"
  LORE_IMAGE_TAG="$previous_tag" dc up -d --no-deps web worker
  if wait_healthy; then log "lore:$previous_tag is healthy again"
  else log "lore:$previous_tag is NOT healthy either: look at 'docker compose logs web'"; fi
}

if ! LORE_IMAGE_TAG="$tag" dc run --rm --no-deps migrate; then
  after="$(revision)"
  if [[ -n "$head" && "$before" != "$head" ]]; then
    log "a migration ran and failed (database now at ${after:-none})"
    restore_database
  else
    log "migrate failed before any migration ran; the database is untouched"
  fi
  back_to_previous_checkout
  restart_previous
  die "migrations failed"
fi
after="$(revision)"
log "migrations: database now at ${after:-none}"

# --- 5. restart, 6. health --------------------------------------------------------
log "restarting worker and web on lore:$tag"
LORE_IMAGE_TAG="$tag" dc up -d --no-deps worker web
# cloudflared (profile `tunnel`) and anything else not running yet.
LORE_IMAGE_TAG="$tag" dc up -d --no-recreate

if ! wait_healthy; then
  log "web did not answer /healthz within ${HEALTH_TIMEOUT}s; its last log lines:"
  dc logs --no-color --tail 40 web || true
  if [[ "$before" != "$after" ]]; then
    log "note: migrations moved the database ${before:-none} -> $after and it is NOT"
    log "restored (they succeeded). The previous image runs on the newer schema; to go"
    log "back fully, restore $backup by hand (deploy/restore.md)."
  fi
  back_to_previous_checkout
  restart_previous
  die "lore:$tag failed its health check; rolled back"
fi

# --- done ---------------------------------------------------------------------------
echo "$tag" > "$STATE/current-tag"
[[ -n "$previous_tag" && "$previous_tag" != "$tag" ]] && echo "$previous_tag" > "$STATE/previous-tag"
docker tag "lore:$tag" lore:latest
# keep the three newest lore:<commit> images (plus whatever is running)
docker image ls lore --format '{{.Tag}} {{.CreatedAt}}' \
  | grep -v '^latest ' | sort -k2 -r | awk 'NR>3 {print $1}' \
  | while read -r old; do
      [[ "$old" == "$tag" || "$old" == "$previous_tag" ]] && continue
      docker image rm "lore:$old" >/dev/null 2>&1 && log "removed old image lore:$old" || true
    done
log "deployed lore:$tag ($(git -C "$REPO" log -1 --format='%h %s'))"
