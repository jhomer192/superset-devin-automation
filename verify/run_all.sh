#!/usr/bin/env bash
# Full regression verification, meant to run on a Devin VM (or any Linux box with docker,
# node >= 20 and python >= 3.11).
#
#   verify/run_all.sh --head <sha> --base <sha> --issues 5,7 [--regression 1,3] \
#                     [--requirements PRD-SEC-1,PRD-OPS-1] [--repo jhomer192/superset]
#
# Steps, in order (each is a stage; the JSON result records which stage failed):
#   1. clone the target repo twice: HEAD (merged commit) and BASE (its first parent)
#   2. start a real PostgreSQL (docker) + Redis for the integration lane
#   3. install backend requirements for HEAD into a venv; npm ci && npm run build the frontend
#   4. per checkout: db upgrade / init / create admin on its own database, boot gunicorn
#      (HEAD on :8088, BASE on :8089), wait for /health, run every probe against THAT app
#   5. probes for --issues must pass at HEAD and fail at BASE
#      run every probe for --regression at HEAD (must pass); BASE result recorded only
#      run every probe of the PRD requirements in --requirements at HEAD (must pass)
#   6. write verify/out/result.json matching orchestrator.schema.VERIFICATION_SCHEMA and exit
#      0 iff every acceptance condition held.
#
# Probes never see agent judgement: verify/collect.py turns exit codes into the verdict.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/.." && pwd)"
REPO="jhomer192/superset"
HEAD_SHA=""; BASE_SHA=""; ISSUES=""; REGRESSION=""; REQUIREMENTS=""
WORK="${SDA_WORKDIR:-${root}/verify/work}"
OUT="${root}/verify/out"
PG_PORT="${SDA_PG_PORT:-55432}"
REDIS_PORT="${SDA_REDIS_PORT:-56379}"
SUPERSET_PORT="${SDA_SUPERSET_PORT:-8088}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --head) HEAD_SHA="$2"; shift 2 ;;
    --base) BASE_SHA="$2"; shift 2 ;;
    --issues) ISSUES="$2"; shift 2 ;;
    --regression) REGRESSION="$2"; shift 2 ;;
    --requirements) REQUIREMENTS="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[[ -n "${HEAD_SHA}" ]] || { echo "--head required" >&2; exit 2; }

mkdir -p "${WORK}" "${OUT}"
STAGE_LOG="${OUT}/stages.log"
: >"${STAGE_LOG}"
stage() { echo "[$(date -u +%H:%M:%S)] STAGE $1" | tee -a "${STAGE_LOG}" >&2; }
fail_stage() {
  local stage="$1" msg="$2"
  python3 "${here}/collect.py" --error --stage "${stage}" --message "${msg}" \
    --head "${HEAD_SHA}" --base "${BASE_SHA}" --out "${OUT}/result.json"
  exit 1
}

cleanup() {
  [[ -n "${GUNICORN_PID:-}" ]] && kill "${GUNICORN_PID}" 2>/dev/null || true
  docker rm -f sda-postgres sda-redis >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ---- 1. clones ------------------------------------------------------------------------------
stage clone
HEAD_DIR="${WORK}/head"; BASE_DIR="${WORK}/base"
clone_at() {
  local dir="$1" sha="$2"
  if [[ ! -d "${dir}/.git" ]]; then
    git clone --quiet "https://github.com/${REPO}.git" "${dir}" || return 1
  fi
  git -C "${dir}" fetch --quiet origin "${sha}" || true
  git -C "${dir}" checkout --quiet --force "${sha}"
}
clone_at "${HEAD_DIR}" "${HEAD_SHA}" || fail_stage clone "could not clone ${REPO}@${HEAD_SHA}"
if [[ -z "${BASE_SHA}" ]]; then
  BASE_SHA="$(git -C "${HEAD_DIR}" rev-parse "${HEAD_SHA}^1")" || fail_stage clone "no first parent for ${HEAD_SHA}"
fi
clone_at "${BASE_DIR}" "${BASE_SHA}" || fail_stage clone "could not check out base ${BASE_SHA}"

# ---- 2. services ------------------------------------------------------------------------------
stage services
docker rm -f sda-postgres sda-redis >/dev/null 2>&1 || true
docker run -d --name sda-postgres -e POSTGRES_USER=superset -e POSTGRES_PASSWORD=superset \
  -e POSTGRES_DB=superset -p "${PG_PORT}:5432" postgres:16 >/dev/null \
  || fail_stage services "docker run postgres failed"
docker run -d --name sda-redis -p "${REDIS_PORT}:6379" redis:7 >/dev/null \
  || fail_stage services "docker run redis failed"
for _ in $(seq 1 60); do
  docker exec sda-postgres pg_isready -U superset >/dev/null 2>&1 && break
  sleep 1
done
docker exec sda-postgres pg_isready -U superset >/dev/null 2>&1 || fail_stage services "postgres never became ready"
export SUPERSET__SQLALCHEMY_DATABASE_URI="postgresql+psycopg2://superset:superset@127.0.0.1:${PG_PORT}/superset"
export REDIS_HOST=127.0.0.1 REDIS_PORT="${REDIS_PORT}"

# ---- 3. dependencies + frontend build -----------------------------------------------------------
stage install
VENV="${WORK}/venv"
[[ -d "${VENV}" ]] || python3 -m venv "${VENV}"
export PROBE_PYTHON="${VENV}/bin/python"
"${PROBE_PYTHON}" -m pip install --quiet --upgrade pip wheel >/dev/null
(
  cd "${HEAD_DIR}"
  grep -v '^mysqlclient' requirements/development.txt >"${WORK}/requirements.txt"
  "${PROBE_PYTHON}" -m pip install --quiet -r "${WORK}/requirements.txt" -e . psycopg2-binary gunicorn
) || fail_stage install "backend requirements failed at ${HEAD_SHA}"

stage frontend_build
(
  cd "${HEAD_DIR}/superset-frontend"
  npm ci --no-audit --no-fund && npm run build
) >"${OUT}/frontend_build.log" 2>&1 || fail_stage frontend_build "npm ci / npm run build failed at ${HEAD_SHA} (see verify/out/frontend_build.log)"

# ---- 4 + 5. boot each checkout on its own DB and port, probe that role against that app -------------
stage boot
cat >"${WORK}/superset_config.py" <<EOF
import os
SECRET_KEY = "sda-verify-" + "k" * 64
SQLALCHEMY_DATABASE_URI = os.environ["SUPERSET__SQLALCHEMY_DATABASE_URI"]
TALISMAN_ENABLED = False
WTF_CSRF_ENABLED = False
FEATURE_FLAGS = {"ALERT_REPORTS": True}
EOF
export SUPERSET_CONFIG_PATH="${WORK}/superset_config.py"
export SUPERSET_ADMIN_USER=admin SUPERSET_ADMIN_PASSWORD=admin
PROBE_RESULTS="${OUT}/probes.jsonl"; : >"${PROBE_RESULTS}"
PROBE_LIST="${OUT}/probes.tsv"
python3 "${here}/list_probes.py" --issues "${ISSUES}" --regression "${REGRESSION}" \
  --requirements "${REQUIREMENTS}" >"${PROBE_LIST}"

boot_app() {  # <checkout> <role> <port> -- own database per role; sets SUPERSET_URL and GUNICORN_PID
  local src="$1" role="$2" port="$3"
  local db="superset_${role}"
  docker exec sda-postgres psql -U superset -d superset -qc "CREATE DATABASE ${db}" >/dev/null 2>&1 || true
  export SUPERSET__SQLALCHEMY_DATABASE_URI="postgresql+psycopg2://superset:superset@127.0.0.1:${PG_PORT}/${db}"
  (
    cd "${src}"
    export PYTHONPATH="${src}${PYTHONPATH:+:${PYTHONPATH}}"
    "${VENV}/bin/superset" db upgrade
    "${VENV}/bin/superset" fab create-admin --username admin --firstname a --lastname a \
      --email admin@example.com --password admin
    "${VENV}/bin/superset" init
  ) >"${OUT}/boot-${role}.log" 2>&1 || fail_stage boot "superset db upgrade/init failed at ${role} (see verify/out/boot-${role}.log)"
  (
    cd "${src}"
    PYTHONPATH="${src}${PYTHONPATH:+:${PYTHONPATH}}" "${VENV}/bin/gunicorn" -w 2 -b "127.0.0.1:${port}" \
      "superset.app:create_app()" >>"${OUT}/boot-${role}.log" 2>&1 &
    echo $! >"${WORK}/gunicorn-${role}.pid"
  )
  GUNICORN_PID="$(cat "${WORK}/gunicorn-${role}.pid")"
  export SUPERSET_URL="http://127.0.0.1:${port}"
  for _ in $(seq 1 120); do
    curl -fsS "${SUPERSET_URL}/health" >/dev/null 2>&1 && break
    sleep 2
  done
  curl -fsS "${SUPERSET_URL}/health" >/dev/null 2>&1 || fail_stage boot "Superset at ${role} never answered /health"
}

run_probe() {  # <issue> <probe_id> <kind> <script> <checkout> <role>
  local issue="$1" id="$2" kind="$3" script="$4" src="$5" role="$6"
  local log
  log="${OUT}/$(echo "${id}" | tr '/' '_')-${role}.log"
  SUPERSET_SRC="${src}" bash "${root}/${script}" >"${log}" 2>&1
  local code=$?
  python3 - "$PROBE_RESULTS" "$issue" "$id" "$kind" "$role" "$code" "$log" <<'PY'
import json, sys
path, issue, pid, kind, role, code, log = sys.argv[1:]
with open(path, "a") as f:
    f.write(json.dumps({"issue": int(issue), "probe": pid, "kind": kind, "role": role,
                        "exit_code": int(code), "log": log}) + "\n")
PY
  echo "  ${role}/${id} -> exit ${code}" >&2
}

probe_checkout() {  # <checkout> <role> <port>
  boot_app "$@"
  stage probes
  while IFS=$'\t' read -r issue pid kind script; do
    run_probe "${issue}" "${pid}" "${kind}" "${script}" "$1" "$2"
  done <"${PROBE_LIST}"
  kill "${GUNICORN_PID}" 2>/dev/null || true
  wait "${GUNICORN_PID}" 2>/dev/null || true
}

probe_checkout "${HEAD_DIR}" head "${SUPERSET_PORT}"
HEAD_URL="${SUPERSET_URL}"
probe_checkout "${BASE_DIR}" base "$((SUPERSET_PORT + 1))"
SUPERSET_URL="${HEAD_URL}"

# ---- 6. verdict -------------------------------------------------------------------------
stage verdict
python3 "${here}/collect.py" --probes "${PROBE_RESULTS}" --issues "${ISSUES}" --regression "${REGRESSION}" \
  --requirements "${REQUIREMENTS}" --head "${HEAD_SHA}" --base "${BASE_SHA}" --health-url "${SUPERSET_URL}/health" --out "${OUT}/result.json"
