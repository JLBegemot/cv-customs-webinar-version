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
# Охват: Write и Edit перехватываются целиком (у них есть file_path).
# Для Bash разбирается сама команда — редиректы (> >> 1> 2> &>) и типовые
# мутирующие вызовы (sed -i, tee, cp, mv, rm, truncate, dd, install, patch,
# ln). Запись через heredoc внутри скрипта или через питоновский open() хук
# не увидит: это заслон от механической оплошности, а не песочница.
#
# Выход 2 = запретить вызов инструмента; stderr уходит модели как обратная связь.

set -eu

ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
MARKER="$ROOT/.claude/tmp/api-tests-active"

[ -f "$MARKER" ] || exit 0

INPUT=$(cat)

BLOCKED=$(printf '%s' "$INPUT" | ROOT="$ROOT" python3 -c '
import json, os, shlex, sys

ROOT = os.path.realpath(os.environ["ROOT"])
ALLOWED = ("tests/", "plans/", "docs/", ".claude/tmp/")
REDIRECTS = (">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>", ">|")
MUTATORS = {
    "tee": "all", "cp": "last", "mv": "last", "rm": "all", "truncate": "all",
    "dd": "all", "install": "last", "patch": "all", "ln": "last",
}

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = data.get("tool_name") or ""
ti = data.get("tool_input") or {}

targets = []
if tool in ("Write", "Edit", "NotebookEdit"):
    fp = ti.get("file_path") or ti.get("notebook_path")
    if fp:
        targets.append(fp)
elif tool == "Bash":
    cmd = ti.get("command") or ""
    try:
        tokens = shlex.split(cmd, comments=False)
    except ValueError:
        sys.exit(0)
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in REDIRECTS and i + 1 < len(tokens):
            targets.append(tokens[i + 1]); i += 2; continue
        for r in REDIRECTS:
            if len(r) > 1 and t.startswith(r) and len(t) > len(r):
                targets.append(t[len(r):])
                break
        base = os.path.basename(t)
        if base == "sed":
            args = []
            j = i + 1
            inplace = False
            while j < len(tokens) and tokens[j] not in ("|", "&&", ";", "||"):
                if tokens[j].startswith("-i"):
                    inplace = True
                elif tokens[j] and not tokens[j].startswith("-"):
                    args.append(tokens[j])
                j += 1
            if inplace and len(args) > 1:
                targets.extend(args[1:])
        elif base in MUTATORS:
            args = []
            j = i + 1
            while j < len(tokens) and tokens[j] not in ("|", "&&", ";", "||"):
                if tokens[j] and not tokens[j].startswith("-"):
                    args.append(tokens[j])
                j += 1
            if args:
                targets.extend(args if MUTATORS[base] == "all" else args[-1:])
        i += 1
else:
    sys.exit(0)

bad = []
for raw in targets:
    if not raw or raw.startswith("-"):
        continue
    path = os.path.realpath(os.path.join(ROOT, os.path.expanduser(raw)))
    if path == ROOT or not path.startswith(ROOT + os.sep):
        continue  # вне репозитория — не наше дело
    rel = path[len(ROOT) + 1:]
    if not rel.startswith(ALLOWED):
        bad.append(rel)

print("\n".join(dict.fromkeys(bad)))
' 2>/dev/null || true)

[ -n "$BLOCKED" ] || exit 0

cat >&2 <<EOF
Запись заблокирована: идёт прогон /api-tests, писать можно только
под tests/, plans/, docs/ и .claude/tmp/.

Под запрет попало:
$BLOCKED

Если правка production-кода действительно нужна — это отдельное решение
(FORBID-010, docs/test-rules.md): вынеси вопрос пользователю, а не меняй
хендлер, чтобы тест позеленел.

Пайплайн закончен — удали маркер .claude/tmp/api-tests-active.
EOF
exit 2
