"""Контракт публикации политики конфиденциальности (152-ФЗ ст. 18.1 ч. 2).

Требование закона — «неограниченный доступ» к документу, поэтому главное,
что здесь проверяется: обе ручки отвечают **без токена**, а JSON- и
plain-text-варианты отдают один и тот же текст (расхождение означало бы,
что опубликованы две разные редакции политики).
"""

from __future__ import annotations

import pytest

from cv_customs.api.legal import PRIVACY_POLICY_TEXT


@pytest.fixture
def client(build_client):
    return build_client()


def test_privacy_policy_is_public_and_returns_the_document(client):
    resp = client.get("/api/legal/privacy-policy")
    assert resp.status_code == 200, resp.text

    text = resp.json()["text"]
    assert text == PRIVACY_POLICY_TEXT
    # Документ должен называть оператора и закон, иначе он не выполняет
    # роль публикуемой политики.
    assert "152-ФЗ" in text
    assert "Персональные данные" in text


def test_privacy_policy_plain_variant_serves_the_same_text(client):
    plain = client.get("/api/legal/privacy-policy/text")
    assert plain.status_code == 200, plain.text
    assert plain.headers["content-type"].startswith("text/plain")
    assert plain.text == PRIVACY_POLICY_TEXT


def test_privacy_policy_ignores_a_bogus_token(client):
    """Публичная ручка не должна падать в 401 из-за мусорного заголовка —
    иначе SPA с протухшим токеном перестаёт показывать политику."""

    resp = client.get(
        "/api/legal/privacy-policy", headers={"Authorization": "Bearer garbage"}
    )
    assert resp.status_code == 200, resp.text
