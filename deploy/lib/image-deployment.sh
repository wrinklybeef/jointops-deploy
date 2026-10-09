#!/usr/bin/env bash
# Shared operations. Callers supply compose[], image, record, backup and phase.
# These globals are deliberately provided/consumed by the sourcing scripts.
# shellcheck disable=SC2154,SC2034

pull_candidate() {
  phase=pull
  docker pull --platform linux/amd64 "$image"
  new_id=$(docker image inspect --format '{{.Id}}' "$image")
  printf 'requested_image_id=%s\n' "$new_id" >> "$record"
}

backup_database() {
  phase=backup
  # Variables intentionally expand inside the existing PostgreSQL container.
  # shellcheck disable=SC2016
  "${compose[@]}" exec -T postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup"
  test -s "$backup"
  # Validate data blocks too; --list only checks the archive's table of contents.
  "${compose[@]}" exec -T postgres pg_restore --file=/dev/null < "$backup"
  sha256sum "$backup" > "$backup.sha256"
  printf 'database_backup=%s\n' "$backup" >> "$record"
}

migrate_candidate() {
  phase=migration
  "${compose[@]}" run --rm --no-deps --pull never -T jointops-app python -m alembic upgrade head
  "${compose[@]}" run --rm --no-deps --pull never -T jointops-app python -m alembic current --check-heads
}

start_candidate() {
  phase=start
  "${compose[@]}" up -d --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 180 jointops-app
  phase=health
  "${compose[@]}" exec -T jointops-app python /app/deploy/healthcheck.py
  local container running_id
  container=$("${compose[@]}" ps -q jointops-app)
  running_id=$(docker inspect --format '{{.Image}}' "$container")
  test "$running_id" = "$new_id"
  test "$(docker inspect --format '{{.Config.Image}}' "$container")" = "$image"
  test "$(docker inspect --format '{{.State.Health.Status}}' "$container")" = healthy
  docker inspect --format 'running_image={{.Config.Image}} health={{.State.Health.Status}} image_id={{.Image}}' "$container"
  printf 'result=healthy\n' >> "$record"
}
