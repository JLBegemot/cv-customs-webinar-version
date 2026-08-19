#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/smoke-local.sh — локальный smoke-тест сервиса.
#
# Скрипт поднимает Postgres + MinIO (docker compose), гоняет миграции,
# стартует uvicorn и проходит реальный сценарий через curl:
# register → login → upload PDF → list → download → delete. Ловит ошибки
# сборки/wiring'а: недостающий роут, несобранную миграцию, битый S3-конфиг.
#
# Pre-reqs (manual, one-time):
#   * Docker Desktop запущен (Postgres + MinIO).
#   * Python 3.12+ (см. resolve_python ниже) или готовый .venv от `uv sync`.
#   * .env заполнен (cp .env.example .env; главное — JWT_SECRET).
#
# Usage:
#   chmod +x scripts/smoke-local.sh
#   ./scripts/smoke-local.sh         # full boot + smoke
#   ./scripts/smoke-local.sh --stop  # tear down only
# ---------------------------------------------------------------------------

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/.smoke-logs"
mkdir -p "$LOG_DIR"

# --- helpers ---------------------------------------------------------------

log() { printf "\033[1;34m[smoke]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[smoke:FAIL]\033[0m %s\n" "$*" >&2; exit 1; }
ok()  { printf "\033[1;32m[smoke:OK]\033[0m %s\n" "$*"; }

wait_for_port() {
  # wait_for_port <host> <port> <label> [<timeout_seconds>]
  local host="$1" port="$2" label="$3" timeout="${4:-60}"
  local waited=0
  while ! (echo > /dev/tcp/"$host"/"$port") 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if (( waited >= timeout )); then
      fail "$label did not come up on $host:$port within ${timeout}s"
    fi
  done
  ok "$label is up on $host:$port"
}

stop_bg() {
  # Kill anything we spawned; ignore missing PIDs.
  for pidfile in "$LOG_DIR"/*.pid; do
    [[ -f "$pidfile" ]] || continue
    local pid
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      log "stopping pid=$pid ($(basename "$pidfile" .pid))"
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  done
}

if [[ "${1:-}" == "--stop" ]]; then
  stop_bg
  docker compose down
  ok "stopped"
  exit 0
fi

# Kill leftovers from a previous run BEFORE we try to bind the same ports
# again. Without this, a stale uvicorn keeps answering on :8000 and any code
# changes you made between runs silently don't apply.
log "cleaning up any leftover smoke procs from a previous run"
stop_bg

if command -v lsof >/dev/null 2>&1; then
  stray="$(lsof -ti tcp:8000 -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$stray" ]]; then
    fail "port 8000 is held by pid(s) $stray not managed by this script. Kill it first: kill $stray"
  fi
fi

trap 'log "interrupted — leaving background procs running; re-run with --stop to clean up"' INT TERM

# --- Step 1: backing services --------------------------------------------

log "bringing up postgres + minio (docker compose)"
docker compose up -d postgres minio minio-bootstrap >/dev/null

wait_for_port 127.0.0.1 5433 "postgres"
wait_for_port 127.0.0.1 9000 "minio"

# --- Step 2: python venv -------------------------------------------------

resolve_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    command -v "$PYTHON" >/dev/null 2>&1 && { echo "$PYTHON"; return; }
    fail "PYTHON=$PYTHON is set but not executable"
  fi
  for candidate in python3.12 python3.13; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return
    fi
  done
  fail "need Python 3.12+ but none found on PATH. Install with:  brew install python@3.12  (or use uv) and re-run."
}

if [[ ! -x .venv/bin/python ]]; then
  PY="$(resolve_python)"
  log "creating .venv with $PY ($($PY -V 2>&1))"
  "$PY" -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -e '.[test]'
fi

# shellcheck disable=SC1091
source .venv/bin/activate
ok "python $(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")') active"

# --- Step 3: env sanity --------------------------------------------------

if [[ ! -f .env ]]; then
  fail ".env is missing — cp .env.example .env and populate JWT_SECRET before re-running"
fi
# shellcheck disable=SC1091
set -a; source .env; set +a

# --- Step 4: migrations --------------------------------------------------

log "running alembic upgrade head"
alembic upgrade head

# --- Step 5: start web ---------------------------------------------------

log "starting uvicorn (backend) → logs: $LOG_DIR/web.log"
(
  uvicorn cv_customs.main:app --host 127.0.0.1 --port 8000 \
    >"$LOG_DIR/web.log" 2>&1 &
  echo $! >"$LOG_DIR/web.pid"
)

wait_for_port 127.0.0.1 8000 "backend"

# --- Step 6: shape checks ------------------------------------------------

BASE="http://127.0.0.1:8000"

log "GET /health"
curl -fsS "$BASE/health" | grep -q '"status"' || fail "/health didn't answer as JSON"
ok "/health responded"

log "GET /api/v1/resumes (expect 401/403 — auth required)"
code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/v1/resumes")"
[[ "$code" == "401" || "$code" == "403" ]] \
  || fail "/api/v1/resumes should 401/403 unauthenticated, got $code"
ok "resumes list is gated (HTTP $code)"

log "GET /openapi.json — confirm routes are mounted"
curl -fsS "$BASE/openapi.json" > "$LOG_DIR/openapi.json"
for path in \
    "/api/auth/register" \
    "/api/auth/login" \
    "/api/v1/auth/password/reset/request" \
    "/api/user/me" \
    "/api/v1/resumes/upload" \
    "/api/v1/resumes/{resume_id}/original" ; do
  if grep -q "\"$path\"" "$LOG_DIR/openapi.json"; then
    ok "openapi exposes $path"
  else
    fail "openapi is MISSING $path"
  fi
done

# --- Step 7: end-to-end walk ---------------------------------------------
# register → login → upload → list → download → delete, чистым curl + python
# для разбора JSON (jq может отсутствовать).

json_get() { python -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

EMAIL="smoke-$(date +%s)@example.com"
PASS="smoke-password"

log "POST /api/auth/register ($EMAIL)"
TOKEN="$(curl -fsS -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\": \"$EMAIL\", \"password\": \"$PASS\", \"consent\": true}" \
  | json_get access_token)"
[[ -n "$TOKEN" ]] || fail "register did not return access_token"
ok "registered, got JWT"

log "POST /api/auth/login"
TOKEN="$(curl -fsS -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\": \"$EMAIL\", \"password\": \"$PASS\"}" \
  | json_get access_token)"
[[ -n "$TOKEN" ]] || fail "login did not return access_token"
ok "login works"

AUTH="Authorization: Bearer $TOKEN"

log "POST /api/v1/resumes/upload (tiny PDF)"
printf '%%PDF-1.4\nsmoke resume body\n%%%%EOF\n' > "$LOG_DIR/smoke.pdf"
RESUME_ID="$(curl -fsS -X POST "$BASE/api/v1/resumes/upload" \
  -H "$AUTH" -F "file=@$LOG_DIR/smoke.pdf;type=application/pdf" \
  | json_get id)"
[[ -n "$RESUME_ID" ]] || fail "upload did not return id"
ok "uploaded resume $RESUME_ID"

log "GET /api/v1/resumes — new file is listed"
curl -fsS "$BASE/api/v1/resumes" -H "$AUTH" | grep -q "$RESUME_ID" \
  || fail "uploaded resume is not in the list"
ok "list contains the upload"

log "GET /api/v1/resumes/$RESUME_ID/original — bytes round-trip"
curl -fsS "$BASE/api/v1/resumes/$RESUME_ID/original" -H "$AUTH" \
  -o "$LOG_DIR/smoke-downloaded.pdf"
cmp -s "$LOG_DIR/smoke.pdf" "$LOG_DIR/smoke-downloaded.pdf" \
  || fail "downloaded blob differs from the uploaded file"
ok "download matches upload byte-for-byte"

log "DELETE /api/v1/resumes/$RESUME_ID"
code="$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
  "$BASE/api/v1/resumes/$RESUME_ID" -H "$AUTH")"
[[ "$code" == "204" ]] || fail "delete should 204, got $code"
ok "delete works"

# --- Step 8: summary ----------------------------------------------------

cat <<SUMMARY

=================================================================
 Smoke-test passed.

 Running services:
   - backend:  http://127.0.0.1:8000  (pid $(cat "$LOG_DIR/web.pid"))
   - docker:   postgres(5433) · minio(9000/9001)

 Logs in $LOG_DIR/

 To tear down:
   ./scripts/smoke-local.sh --stop

=================================================================
SUMMARY
