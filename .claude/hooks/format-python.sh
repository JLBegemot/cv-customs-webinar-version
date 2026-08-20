#!/bin/sh
# PostToolUse: форматирует записанный .py файл через ruff.
#
# Модель забывает форматировать, хук — нет. Работает только по .py и только
# по файлам внутри репозитория; всё остальное пропускает молча.
#
# Хук не блокирует работу: сам вызов инструмента уже состоялся. Но и молчать
# при поломке он не должен — иначе репозиторий тихо расходится с `ruff format`
# (так и случилось: хук выходил `|| exit 0` на любой ошибке, и накопилось
# десять неотформатированных файлов). Поэтому: не смог отформатировать —
# сказал об этом в stderr и вышел 1.

set -eu

ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty')

[ -n "$FILE" ] || exit 0

case "$FILE" in
  *.py) ;;
  *) exit 0 ;;
esac

# Относительный путь считаем от корня репозитория.
case "$FILE" in
  /*) ;;
  *) FILE="$ROOT/$FILE" ;;
esac

case "$FILE" in
  "$ROOT"/*) ;;
  *) exit 0 ;;   # файл вне репозитория — не наше дело
esac

[ -f "$FILE" ] || exit 0

cd "$ROOT"

# Ищем ruff: сначала окружение проекта, потом PATH. `uv run` — последним:
# он способен уйти в синхронизацию окружения, а хук должен быть быстрым.
if [ -x ".venv/bin/ruff" ]; then
  RUFF=".venv/bin/ruff"
elif command -v ruff >/dev/null 2>&1; then
  RUFF="ruff"
elif command -v uv >/dev/null 2>&1; then
  RUFF="uv run --quiet ruff"
else
  echo "format-python: ruff не найден (ни .venv/bin/ruff, ни PATH, ни uv) — файл '$FILE' не отформатирован. Поставь: uv sync --extra dev" >&2
  exit 1
fi

ERR=$($RUFF format -q "$FILE" 2>&1) && exit 0

# Не парсится — это работа pytest, а не форматтера, и это не повод шуметь.
case "$ERR" in
  *"error: Failed to parse"*|*"SyntaxError"*) exit 0 ;;
esac

echo "format-python: ruff format упал на '$FILE': $ERR" >&2
exit 1
