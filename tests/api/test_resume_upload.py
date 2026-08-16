"""Тесты файлового хранилища резюме: upload / list / get / download / delete.

Стенд — SQLite-in-memory + fake-S3 (словарь в памяти). Парсинга в
упрощённом сервисе нет: upload валидирует формат по magic-байтам, кладёт
блоб в S3 и возвращает метаданные. PDF-байты собираются руками (sniff
смотрит только на ``%PDF-`` префикс), DOCX — stdlib-``zipfile`` с
``word/``-записью внутри.
"""

from __future__ import annotations

import io
import zipfile

import pytest


# ---------------------------------------------------------------------------
# Fixture files
# ---------------------------------------------------------------------------


def _make_pdf_bytes(payload: str = "hello") -> bytes:
    return b"%PDF-1.4\n" + payload.encode() + b"\n%%EOF\n"


def _make_docx_bytes(payload: str = "hello") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", f"<w:document>{payload}</w:document>")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fake S3
# ---------------------------------------------------------------------------


class FakeS3:
    """In-memory замена ``infra.storage.S3Client`` — только нужные методы."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_put = False

    async def put_object(self, key, body, content_type=None):
        if self.fail_put:
            raise RuntimeError("S3 down")
        self.objects[key] = body
        return key

    async def get_object(self, key):
        return self.objects[key]

    async def delete_object(self, key):
        self.objects.pop(key, None)


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def resume_ctx(build_client, session_factory, make_user, auth_headers):
    user_id = await make_user("alex@example.com")

    from cv_customs.api import resumes as resumes_module

    s3 = FakeS3()
    return {
        "client": build_client(routers=[resumes_module.router], s3=s3),
        "headers": auth_headers(user_id),
        "user_id": user_id,
        "s3": s3,
        "SessionLocal": session_factory,
    }


def _upload(ctx, data: bytes, filename: str = "cv.pdf"):
    return ctx["client"].post(
        "/api/v1/resumes/upload",
        files={"file": (filename, data, "application/octet-stream")},
        headers=ctx["headers"],
    )


# ---------------------------------------------------------------------------
# Upload happy paths
# ---------------------------------------------------------------------------


async def test_upload_pdf_stores_blob_and_metadata(resume_ctx):
    data = _make_pdf_bytes("resume body")
    resp = _upload(resume_ctx, data, filename="my cv.pdf")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["mime"] == "application/pdf"
    assert body["filename"] == "my cv.pdf"
    assert body["size_bytes"] == len(data)

    key = f"resumes/{body['id']}/original.pdf"
    assert resume_ctx["s3"].objects[key] == data


async def test_upload_docx_stores_blob_and_metadata(resume_ctx):
    data = _make_docx_bytes()
    resp = _upload(resume_ctx, data, filename="cv.docx")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["mime"] == "application/docx"
    key = f"resumes/{body['id']}/original.docx"
    assert resume_ctx["s3"].objects[key] == data


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------


async def test_upload_rejects_unknown_format(resume_ctx):
    resp = _upload(resume_ctx, b"plain text, not a resume")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "UPLOAD_UNSUPPORTED_FORMAT"


async def test_upload_rejects_empty_body(resume_ctx):
    resp = _upload(resume_ctx, b"")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "UPLOAD_EMPTY"


async def test_upload_rejects_too_large(resume_ctx, monkeypatch):
    from cv_customs.config import settings

    monkeypatch.setattr(settings, "resume_upload_max_bytes", 64)
    resp = _upload(resume_ctx, _make_pdf_bytes("x" * 200))
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == "UPLOAD_TOO_LARGE"


async def test_upload_without_s3_returns_503(build_client, make_user, auth_headers):
    from cv_customs.api import resumes as resumes_module

    user_id = await make_user("nos3@example.com")
    client = build_client(routers=[resumes_module.router], s3=None)
    resp = client.post(
        "/api/v1/resumes/upload",
        files={"file": ("cv.pdf", _make_pdf_bytes(), "application/pdf")},
        headers=auth_headers(user_id),
    )
    assert resp.status_code == 503


async def test_upload_s3_failure_returns_503_and_no_row(resume_ctx):
    resume_ctx["s3"].fail_put = True
    resp = _upload(resume_ctx, _make_pdf_bytes())
    assert resp.status_code == 503

    listing = resume_ctx["client"].get("/api/v1/resumes", headers=resume_ctx["headers"])
    assert listing.json()["total"] == 0


# ---------------------------------------------------------------------------
# List / detail / download / delete
# ---------------------------------------------------------------------------


async def test_list_and_detail(resume_ctx):
    created = _upload(resume_ctx, _make_pdf_bytes(), filename="a.pdf").json()

    listing = resume_ctx["client"].get("/api/v1/resumes", headers=resume_ctx["headers"])
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == created["id"]
    assert body["items"][0]["filename"] == "a.pdf"

    detail = resume_ctx["client"].get(
        f"/api/v1/resumes/{created['id']}", headers=resume_ctx["headers"]
    )
    assert detail.status_code == 200, detail.text
    # Сверяются значимые поля, а не весь дамп: ассерт на целиком равный JSON
    # ломался бы от любого нового поля в ответе, ничего не сообщая о контракте.
    for field in ("id", "filename", "mime", "size_bytes", "created_at"):
        assert detail.json()[field] == created[field], field


async def test_download_original_roundtrip(resume_ctx):
    data = _make_pdf_bytes("download me")
    created = _upload(resume_ctx, data, filename="orig.pdf").json()

    resp = resume_ctx["client"].get(
        f"/api/v1/resumes/{created['id']}/original",
        headers=resume_ctx["headers"],
    )
    assert resp.status_code == 200
    assert resp.content == data
    assert resp.headers["content-type"].startswith("application/pdf")
    assert 'filename="orig.pdf"' in resp.headers["content-disposition"]


async def test_delete_removes_row_and_blob(resume_ctx):
    created = _upload(resume_ctx, _make_pdf_bytes()).json()
    key = f"resumes/{created['id']}/original.pdf"
    assert key in resume_ctx["s3"].objects

    resp = resume_ctx["client"].delete(
        f"/api/v1/resumes/{created['id']}", headers=resume_ctx["headers"]
    )
    assert resp.status_code == 204
    assert key not in resume_ctx["s3"].objects

    listing = resume_ctx["client"].get("/api/v1/resumes", headers=resume_ctx["headers"])
    assert listing.json()["total"] == 0

    detail = resume_ctx["client"].get(
        f"/api/v1/resumes/{created['id']}", headers=resume_ctx["headers"]
    )
    assert detail.status_code == 404


# ---------------------------------------------------------------------------
# Ownership / auth
# ---------------------------------------------------------------------------


async def test_cannot_access_other_users_resume(resume_ctx, make_user, auth_headers):
    created = _upload(resume_ctx, _make_pdf_bytes()).json()

    other_id = await make_user("mallory@example.com")
    other_headers = auth_headers(other_id)

    for method, url in [
        ("get", f"/api/v1/resumes/{created['id']}"),
        ("get", f"/api/v1/resumes/{created['id']}/original"),
        ("delete", f"/api/v1/resumes/{created['id']}"),
    ]:
        resp = getattr(resume_ctx["client"], method)(url, headers=other_headers)
        assert resp.status_code == 404, (method, url, resp.status_code)


async def test_upload_requires_auth(resume_ctx):
    resp = resume_ctx["client"].post(
        "/api/v1/resumes/upload",
        files={"file": ("cv.pdf", _make_pdf_bytes(), "application/pdf")},
    )
    assert resp.status_code == 401


async def test_reading_endpoints_require_auth(resume_ctx):
    created = _upload(resume_ctx, _make_pdf_bytes()).json()

    for method, url in [
        ("get", "/api/v1/resumes"),
        ("get", f"/api/v1/resumes/{created['id']}"),
        ("get", f"/api/v1/resumes/{created['id']}/original"),
        ("delete", f"/api/v1/resumes/{created['id']}"),
    ]:
        resp = getattr(resume_ctx["client"], method)(url)
        assert resp.status_code == 401, (method, url, resp.text)


async def test_malformed_uuid_in_path_is_404_not_422(resume_ctx):
    """Невалидный UUID ловится хендлером руками и схлопывается в тот же 404,
    что и «чужое/несуществующее»: 422 подсказывал бы, что id вообще бывает
    валидным, и разделял бы ветки для перебора."""

    for url in [
        "/api/v1/resumes/not-a-uuid",
        "/api/v1/resumes/not-a-uuid/original",
    ]:
        resp = resume_ctx["client"].get(url, headers=resume_ctx["headers"])
        assert resp.status_code == 404, (url, resp.text)

    deleted = resume_ctx["client"].delete(
        "/api/v1/resumes/not-a-uuid", headers=resume_ctx["headers"]
    )
    assert deleted.status_code == 404, deleted.text


async def test_unknown_but_valid_uuid_is_404(resume_ctx):
    import uuid

    resp = resume_ctx["client"].get(
        f"/api/v1/resumes/{uuid.uuid4()}", headers=resume_ctx["headers"]
    )
    assert resp.status_code == 404, resp.text
