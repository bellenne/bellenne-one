#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
COMPOSE_FILE=""
OLD_PULSE_CONTAINER="bellennepulse"
OLD_ECHO_CONTAINER="market-replies-web"
OLD_VECTOR_CONTAINER="wb-ads-statistics"
BACKUP_BASE=""
APPLY=false
ALLOW_MULTIPLE_ECHO_USERS=false
TARGET_MUTATED=false
OLD_CONTAINERS_STOPPED=false
BACKUP_ROOT=""
SOURCE_ROOT=""
declare -a PREVIOUSLY_RUNNING=()

usage() {
  cat <<'EOF'
Перенос production-данных Pulse, WBAnsewer и AdsStatistics в BellenneOne.

По умолчанию выполняется только проверка без остановки контейнеров и записи данных.

Использование:
  ./scripts/migrate-production-data.sh
  ./scripts/migrate-production-data.sh --apply

Параметры:
  --apply                         Выполнить миграцию.
  --project-dir PATH              Каталог BellenneOne.
  --compose-file PATH             Compose-файл BellenneOne.
  --old-pulse-container NAME      Старый контейнер Pulse.
  --old-echo-container NAME       Старый контейнер WBAnsewer/Echo.
  --old-vector-container NAME     Старый контейнер AdsStatistics/Vector.
  --backup-dir PATH               Каталог для резервных копий.
  --allow-multiple-echo-users     Разрешить БД Echo с несколькими пользователями.
  -h, --help                      Показать справку.

Ожидаемые исходные файлы:
  Pulse:  /app/data/bellennepulse.db
  Echo:   /app/data/app.db
  Vector: /data/ads_statistics.db, /data/.token_key
EOF
}

log() {
  printf '[migration] %s\n' "$*"
}

warn() {
  printf '[migration] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[migration] ERROR: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --apply)
      APPLY=true
      shift
      ;;
    --project-dir)
      (($# >= 2)) || die "Для --project-dir требуется путь."
      PROJECT_DIR="$2"
      shift 2
      ;;
    --compose-file)
      (($# >= 2)) || die "Для --compose-file требуется путь."
      COMPOSE_FILE="$2"
      shift 2
      ;;
    --old-pulse-container)
      (($# >= 2)) || die "Для --old-pulse-container требуется имя."
      OLD_PULSE_CONTAINER="$2"
      shift 2
      ;;
    --old-echo-container)
      (($# >= 2)) || die "Для --old-echo-container требуется имя."
      OLD_ECHO_CONTAINER="$2"
      shift 2
      ;;
    --old-vector-container)
      (($# >= 2)) || die "Для --old-vector-container требуется имя."
      OLD_VECTOR_CONTAINER="$2"
      shift 2
      ;;
    --backup-dir)
      (($# >= 2)) || die "Для --backup-dir требуется путь."
      BACKUP_BASE="$2"
      shift 2
      ;;
    --allow-multiple-echo-users)
      ALLOW_MULTIPLE_ECHO_USERS=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Неизвестный параметр: $1"
      ;;
  esac
done

PROJECT_DIR="$(cd -- "$PROJECT_DIR" 2>/dev/null && pwd -P)" || die "Каталог BellenneOne не найден: $PROJECT_DIR"
if [[ -z "$COMPOSE_FILE" ]]; then
  COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"
elif [[ "$COMPOSE_FILE" != /* ]]; then
  COMPOSE_FILE="$PROJECT_DIR/$COMPOSE_FILE"
fi
[[ -f "$COMPOSE_FILE" ]] || die "Compose-файл не найден: $COMPOSE_FILE"

if [[ -z "$BACKUP_BASE" ]]; then
  BACKUP_BASE="$PROJECT_DIR/backups"
elif [[ "$BACKUP_BASE" != /* ]]; then
  BACKUP_BASE="$PROJECT_DIR/$BACKUP_BASE"
fi

compose() {
  docker compose --project-directory "$PROJECT_DIR" -f "$COMPOSE_FILE" "$@"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Не найдена команда: $1"
}

container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
}

container_is_running() {
  [[ "$(docker inspect --format '{{.State.Running}}' "$1")" == "true" ]]
}

container_image() {
  docker inspect --format '{{.Config.Image}}' "$1"
}

container_env() {
  local container="$1"
  local key="$2"
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container" \
    | awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}'
}

source_file_exists() {
  local container="$1"
  local path="$2"
  local image
  image="$(container_image "$container")"
  docker run --rm --user root --volumes-from "$container:ro" --entrypoint sh "$image" \
    -c "test -s '$path'" >/dev/null
}

source_file_size() {
  local container="$1"
  local path="$2"
  local image
  image="$(container_image "$container")"
  docker run --rm --user root --volumes-from "$container:ro" --entrypoint sh "$image" \
    -c "if test -f '$path'; then stat -c %s '$path'; else printf '0'; fi"
}

target_container_id() {
  local service="$1"
  compose ps -aq "$service" | head -n 1
}

copy_from_container() {
  local container="$1"
  local source="$2"
  local destination="$3"
  docker cp "$container:$source" "$destination"
}

copy_optional_from_container() {
  local container="$1"
  local source="$2"
  local destination="$3"
  if source_file_exists "$container" "$source"; then
    copy_from_container "$container" "$source" "$destination"
    return 0
  fi
  return 1
}

validate_sqlite() {
  local service="$1"
  local database="$2"
  local label="$3"
  compose run --rm --no-deps --user root \
    -v "$database:/migration/source.db:ro" \
    "$service" python -c '
import sqlite3
import sys

connection = sqlite3.connect("file:/migration/source.db?mode=ro&immutable=1", uri=True)
result = connection.execute("PRAGMA integrity_check").fetchone()[0]
if result != "ok":
    raise SystemExit(f"integrity_check: {result}")
tables = {
    row[0]
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = '\''table'\''"
    )
}
users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] if "users" in tables else 0
print(f"integrity=ok users={users} tables={len(tables)}")
' | sed "s/^/[migration] $label: /"
}

sqlite_user_count() {
  local service="$1"
  local database="$2"
  compose run --rm --no-deps --user root \
    -v "$database:/migration/source.db:ro" \
    "$service" python -c '
import sqlite3

connection = sqlite3.connect("file:/migration/source.db?mode=ro&immutable=1", uri=True)
tables = {
    row[0]
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = '\''table'\''"
    )
}
print(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] if "users" in tables else 0)
'
}

backup_target_directory() {
  local service="$1"
  local container_path="$2"
  local destination="$3"
  local container
  container="$(target_container_id "$service")"
  [[ -n "$container" ]] || die "Не найден целевой контейнер сервиса $service."
  mkdir -p -- "$destination"
  docker cp "$container:$container_path/." "$destination"
}

restore_owners() {
  compose run --rm --no-deps --user root pulse \
    sh -c 'chown -R bellennepulse:bellennepulse /app/data'
  compose run --rm --no-deps --user root echo \
    sh -c 'chown -R bellenneecho:bellenneecho /app/data'
  compose run --rm --no-deps --user root vector \
    sh -c 'chown -R bellennevector:bellennevector /data'
}

wait_for_service() {
  local service="$1"
  local timeout_seconds="$2"
  local container status elapsed=0
  container="$(target_container_id "$service")"
  [[ -n "$container" ]] || return 1

  while ((elapsed < timeout_seconds)); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")"
    if [[ "$status" == "healthy" || "$status" == "running" ]]; then
      log "$service: $status"
      return 0
    fi
    if [[ "$status" == "unhealthy" || "$status" == "exited" || "$status" == "dead" ]]; then
      warn "$service: $status"
      return 1
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  warn "$service не стал healthy за ${timeout_seconds} секунд."
  return 1
}

on_error() {
  local exit_code=$?
  trap - ERR
  set +e
  warn "Миграция прервана."
  if [[ "$TARGET_MUTATED" == "true" ]]; then
    warn "Целевые данные уже изменялись. Не запускайте повторную миграцию вслепую."
    warn "Резервная копия текущего BellenneOne: $BACKUP_ROOT/current"
    warn "Исходный production-снимок: $SOURCE_ROOT"
  elif [[ "$OLD_CONTAINERS_STOPPED" == "true" && ${#PREVIOUSLY_RUNNING[@]} -gt 0 ]]; then
    warn "Целевые данные не изменялись; возвращаю ранее работавшие старые контейнеры."
    docker start "${PREVIOUSLY_RUNNING[@]}" >/dev/null
  fi
  exit "$exit_code"
}

require_command docker
require_command awk
require_command sed
require_command sha256sum

docker info >/dev/null 2>&1 || die "Docker Engine недоступен."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 недоступен."
compose config --quiet

for container in "$OLD_PULSE_CONTAINER" "$OLD_ECHO_CONTAINER" "$OLD_VECTOR_CONTAINER"; do
  container_exists "$container" || die "Не найден старый контейнер: $container"
done

source_file_exists "$OLD_PULSE_CONTAINER" /app/data/bellennepulse.db \
  || die "В $OLD_PULSE_CONTAINER не найдена /app/data/bellennepulse.db"
source_file_exists "$OLD_ECHO_CONTAINER" /app/data/app.db \
  || die "В $OLD_ECHO_CONTAINER не найдена /app/data/app.db"
source_file_exists "$OLD_VECTOR_CONTAINER" /data/ads_statistics.db \
  || die "В $OLD_VECTOR_CONTAINER не найдена /data/ads_statistics.db"
source_file_exists "$OLD_VECTOR_CONTAINER" /data/.token_key \
  || die "В $OLD_VECTOR_CONTAINER не найдена /data/.token_key — API-токен Vector нельзя будет расшифровать."

if [[ "$APPLY" != "true" ]]; then
  log "Dry-run успешно завершён."
  log "Найдены старые контейнеры и обязательные файлы баз данных."
  log "Для миграции выполните эту же команду с --apply."
  exit 0
fi

trap on_error ERR

log "Подготавливаю целевые контейнеры BellenneOne."
compose create shell pulse echo vector gateway >/dev/null

TARGET_PULSE_CONTAINER="$(target_container_id pulse)"
[[ -n "$TARGET_PULSE_CONTAINER" ]] || die "Не удалось определить целевой контейнер Pulse."

OLD_APP_SECRET="$(container_env "$OLD_PULSE_CONTAINER" APP_SECRET_KEY)"
TARGET_APP_SECRET="$(container_env "$TARGET_PULSE_CONTAINER" APP_SECRET_KEY)"
[[ -n "$OLD_APP_SECRET" ]] || die "В старом Pulse отсутствует APP_SECRET_KEY."
[[ "$OLD_APP_SECRET" == "$TARGET_APP_SECRET" ]] \
  || die "APP_SECRET_KEY нового BellenneOne отличается от старого Pulse. Исправьте production .env."

OLD_CREDENTIAL_KEY="$(container_env "$OLD_PULSE_CONTAINER" CREDENTIALS_ENCRYPTION_KEY)"
TARGET_CREDENTIAL_KEY="$(container_env "$TARGET_PULSE_CONTAINER" CREDENTIALS_ENCRYPTION_KEY)"
if [[ -n "$OLD_CREDENTIAL_KEY" && "$OLD_CREDENTIAL_KEY" != "$TARGET_CREDENTIAL_KEY" ]]; then
  die "CREDENTIALS_ENCRYPTION_KEY нового BellenneOne отличается от старого Pulse."
fi

STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_ROOT="$BACKUP_BASE/production-migration-$STAMP"
SOURCE_ROOT="$BACKUP_ROOT/source"
mkdir -p -- "$SOURCE_ROOT/pulse" "$SOURCE_ROOT/echo" "$SOURCE_ROOT/vector" "$BACKUP_ROOT/current"
chmod 700 "$BACKUP_ROOT"

for container in "$OLD_PULSE_CONTAINER" "$OLD_ECHO_CONTAINER" "$OLD_VECTOR_CONTAINER"; do
  if container_is_running "$container"; then
    PREVIOUSLY_RUNNING+=("$container")
  fi
done

log "Останавливаю старые приложения для согласованного снимка SQLite."
docker stop "$OLD_PULSE_CONTAINER" "$OLD_ECHO_CONTAINER" "$OLD_VECTOR_CONTAINER" >/dev/null
OLD_CONTAINERS_STOPPED=true

for pair in \
  "$OLD_PULSE_CONTAINER:/app/data/bellennepulse.db-wal" \
  "$OLD_ECHO_CONTAINER:/app/data/app.db-wal" \
  "$OLD_VECTOR_CONTAINER:/data/ads_statistics.db-wal"; do
  container="${pair%%:*}"
  path="${pair#*:}"
  wal_size="$(source_file_size "$container" "$path")"
  [[ "$wal_size" == "0" ]] \
    || die "После остановки остался непустой WAL ($container:$path, $wal_size байт). Нужен SQLite checkpoint."
done

log "Сохраняю production-снимок старых приложений."
copy_from_container "$OLD_PULSE_CONTAINER" /app/data/bellennepulse.db "$SOURCE_ROOT/pulse/bellennepulse.db"
copy_from_container "$OLD_ECHO_CONTAINER" /app/data/app.db "$SOURCE_ROOT/echo/app.db"
copy_from_container "$OLD_VECTOR_CONTAINER" /data/ads_statistics.db "$SOURCE_ROOT/vector/ads_statistics.db"
copy_from_container "$OLD_VECTOR_CONTAINER" /data/.token_key "$SOURCE_ROOT/vector/.token_key"
copy_optional_from_container "$OLD_VECTOR_CONTAINER" /data/.session_key "$SOURCE_ROOT/vector/.session_key" \
  || warn "У старого Vector нет .session_key; старые Vector-сессии не сохранятся."

sha256sum \
  "$SOURCE_ROOT/pulse/bellennepulse.db" \
  "$SOURCE_ROOT/echo/app.db" \
  "$SOURCE_ROOT/vector/ads_statistics.db" \
  "$SOURCE_ROOT/vector/.token_key" \
  > "$BACKUP_ROOT/source.sha256"

validate_sqlite pulse "$SOURCE_ROOT/pulse/bellennepulse.db" Pulse
validate_sqlite echo "$SOURCE_ROOT/echo/app.db" Echo
validate_sqlite vector "$SOURCE_ROOT/vector/ads_statistics.db" Vector

ECHO_USERS="$(sqlite_user_count echo "$SOURCE_ROOT/echo/app.db" | tail -n 1)"
if ((ECHO_USERS > 1)) && [[ "$ALLOW_MULTIPLE_ECHO_USERS" != "true" ]]; then
  die "В Echo найдено пользователей: $ECHO_USERS. Автопривязка неоднозначна; проверьте пользователей и повторите с --allow-multiple-echo-users."
fi

log "Останавливаю BellenneOne и создаю резервную копию его текущих volumes."
compose stop gateway pulse echo vector >/dev/null
backup_target_directory pulse /app/data "$BACKUP_ROOT/current/pulse"
backup_target_directory echo /app/data "$BACKUP_ROOT/current/echo"
backup_target_directory vector /data "$BACKUP_ROOT/current/vector"

TARGET_MUTATED=true

log "Удаляю только известные WAL/SHM и старые целевые файлы."
compose run --rm --no-deps --user root pulse sh -c \
  'rm -f /app/data/bellennepulse.db /app/data/bellennepulse.db-wal /app/data/bellennepulse.db-shm'
compose run --rm --no-deps --user root echo sh -c \
  'rm -f /app/data/bellenneecho.db /app/data/bellenneecho.db-wal /app/data/bellenneecho.db-shm'
compose run --rm --no-deps --user root vector sh -c \
  'rm -f /data/bellennevector.db /data/bellennevector.db-wal /data/bellennevector.db-shm /data/.token_key /data/.session_key'

TARGET_PULSE_CONTAINER="$(target_container_id pulse)"
TARGET_ECHO_CONTAINER="$(target_container_id echo)"
TARGET_VECTOR_CONTAINER="$(target_container_id vector)"

docker cp "$SOURCE_ROOT/pulse/bellennepulse.db" \
  "$TARGET_PULSE_CONTAINER:/app/data/bellennepulse.db"
docker cp "$SOURCE_ROOT/echo/app.db" \
  "$TARGET_ECHO_CONTAINER:/app/data/bellenneecho.db"
docker cp "$SOURCE_ROOT/vector/ads_statistics.db" \
  "$TARGET_VECTOR_CONTAINER:/data/bellennevector.db"
docker cp "$SOURCE_ROOT/vector/.token_key" \
  "$TARGET_VECTOR_CONTAINER:/data/.token_key"
if [[ -f "$SOURCE_ROOT/vector/.session_key" ]]; then
  docker cp "$SOURCE_ROOT/vector/.session_key" \
    "$TARGET_VECTOR_CONTAINER:/data/.session_key"
fi

restore_owners

log "Запускаю BellenneOne и встроенные миграции схем."
compose up -d

for service in shell pulse echo vector gateway; do
  wait_for_service "$service" 120
done

TARGET_MUTATED=false
trap - ERR

log "Миграция завершена успешно."
log "Резервная копия: $BACKUP_ROOT/current"
log "Исходный production-снимок: $SOURCE_ROOT"
log "Контрольные суммы: $BACKUP_ROOT/source.sha256"
log "Старые контейнеры оставлены остановленными. Не удаляйте их до полной проверки данных."
log "Оставьте ECHO_DRY_RUN=true до проверки кабинетов, шаблонов и диапазонов рейтинга."
