#!/bin/sh
# PreToolUse-гейт на запись файлов во время прогона пайплайна /api-tests.
#
# Действует ТОЛЬКО пока существует маркер .claude/tmp/api-tests-active: его
# создаёт /api-tests на шаге генерации и удаляет на отгрузке. Вне пайплайна
# хук молча пропускает всё — иначе он ломал бы обычную работу по бэкенду
# и фронтенду.
#
# Правило: пока пайплайн активен, писать можно только под tests/, plans/,
# docs/ и .claude/tmp/. Правка production-кода «чтобы тест позеленел» —
# отдельное решение пользователя (FORBID-010), а не побочный эффект генерации.
#
# Выход 2 = запретить вызов инструмента; stderr уходит модели как обратная связь.

set -eu

ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
MARKER="$ROOT/.claude/tmp/api-tests-active"

[ -f "$MARKER" ] || exit 0

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')

[ -n "$FILE" ] || exit 0

# Приводим к пути относительно корня репозитория.
case "$FILE" in
  "$ROOT"/*) REL="${FILE#"$ROOT"/}" ;;
  /*)        REL="$FILE" ;;
  *)         REL="$FILE" ;;
esac

case "$REL" in
  tests/*|plans/*|docs/*|.claude/tmp/*)
    exit 0
    ;;
esac

cat >&2 <<EOF
Запись в '$REL' заблокирована: идёт прогон /api-tests, писать можно только
под tests/, plans/, docs/ и .claude/tmp/.

Если правка production-кода действительно нужна — это отдельное решение
(FORBID-010, docs/test-rules.md): вынеси вопрос пользователю, а не меняй
хендлер, чтобы тест позеленел.

Пайплайн закончен — удали маркер .claude/tmp/api-tests-active.
EOF
exit 2
