#!/usr/bin/env python
"""Снапшот OpenAPI-контракта: ``docs/openapi.json``.

Контракт у нас не внешний swagger, а само приложение, поэтому снапшот
``app.openapi()`` кладём в ``docs/openapi.json`` и версионируем. Дельта
контракта после этого — обычный ``git diff docs/openapi.json``. На снапшоте
держатся проектирование тест-кейсов и ``scripts/validate_cases.py``.

Режимы:

* ``--dump``   — перегенерировать ``docs/openapi.json``;
* ``--check``  — упасть, если снапшот разошёлся с кодом (локальная проверка).

Снапшот снимается со **всеми включёнными feature-флагами** — иначе ручки
за флагом (``/api/v1/auth/mfa/*`` за ``FEATURE_MFA``) не попадут в контракт.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "docs" / "openapi.json"

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

# Значения по умолчанию, без которых не импортируется cv_customs.config.
_ENV_DEFAULTS = {
    "JWT_SECRET": "snapshot-secret-key-must-be-at-least-32-chars-long",
    "JWT_EXPIRE_HOURS": "4",
    # Контракт должен включать всё, что вообще может быть примонтировано.
    "FEATURE_MFA": "true",
}


def _load_live_spec() -> dict:
    for key, value in _ENV_DEFAULTS.items():
        os.environ[key] = value

    from cv_customs.main import app

    return app.openapi()


def operations(spec: dict) -> set[tuple[str, str]]:
    """``{("POST", "/api/v1/resumes/upload"), ...}`` — все операции контракта."""

    return {
        (method.upper(), path)
        for path, item in spec.get("paths", {}).items()
        for method in item
        if method in _HTTP_METHODS
    }


def _serialize(spec: dict) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def cmd_dump() -> int:
    spec = _load_live_spec()
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.write_text(_serialize(spec), encoding="utf-8")
    print(f"{SPEC_PATH.relative_to(REPO_ROOT)}: {len(operations(spec))} операций")
    return 0


def cmd_check() -> int:
    live = _serialize(_load_live_spec())
    stored = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.exists() else ""

    if live == stored:
        print("контракт совпадает со снапшотом")
        return 0

    live_ops = operations(json.loads(live))
    stored_ops = operations(json.loads(stored)) if stored else set()

    print("снапшот разошёлся с кодом — перегенерируй docs/openapi.json:")
    print("    uv run python scripts/openapi_snapshot.py --dump")
    for method, path in sorted(live_ops - stored_ops):
        print(f"  + {method:7} {path}")
    for method, path in sorted(stored_ops - live_ops):
        print(f"  - {method:7} {path}")
    if live_ops == stored_ops:
        print("  (набор операций тот же, различаются схемы или описания)")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dump", action="store_true", help="перегенерировать снапшот")
    group.add_argument("--check", action="store_true", help="проверить дрейф снапшота")
    args = parser.parse_args()

    return cmd_dump() if args.dump else cmd_check()


if __name__ == "__main__":
    sys.exit(main())
