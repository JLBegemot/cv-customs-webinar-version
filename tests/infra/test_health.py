"""Инфра-контракт ``GET /health`` и request-id middleware.

Пробник обязан быть «мелким»: он отвечает 200 без единого обращения во
внешние сервисы, поэтому стенд поднимается с умолчаниями ``email=None`` /
``s3=None`` — если бы хендлер ходил в S3, тест бы упал.
``RequestIdMiddleware`` навешивается на всё приложение, поэтому проверяется
здесь же, на самой дешёвой ручке.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def client(build_client):
    return build_client()


def test_health_is_shallow_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "version": 1}


def test_health_needs_no_auth(client):
    # Пробник дёргает load balancer без токена — заголовков нет вообще.
    assert client.get("/health").status_code == 200


def test_health_request_id_header_echoed(client):
    resp = client.get("/health")
    assert resp.headers.get("x-request-id")


def test_request_id_reuses_incoming_header(client):
    """Свой ``X-Request-ID`` от прокси не перетирается — иначе теряется
    сквозная трассировка запроса между сервисами."""

    resp = client.get("/health", headers={"X-Request-ID": "trace-me-42"})
    assert resp.headers["x-request-id"] == "trace-me-42"


def test_request_ids_are_unique_per_request(client):
    first = client.get("/health").headers["x-request-id"]
    second = client.get("/health").headers["x-request-id"]
    assert first != second
