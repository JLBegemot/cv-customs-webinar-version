---
name: python-backend-fastapi
description: Guides work on the cv-customs FastAPI backend (under `backend/src/cv_customs/`). Use whenever the user asks to add, edit, debug, test, or review anything in the Python backend — including new endpoints, request/response schemas, SQLAlchemy models or migrations, repository functions, JWT auth, file storage, or pytest tests. Trigger even if the user does not say "backend" explicitly: words like endpoint, route, API, FastAPI, Pydantic, SQLAlchemy, alembic, migration, async session, repository, JWT, token, login, register, conftest, or references to files under `backend/src/cv_customs/` or `tests/` should all activate this skill. Also trigger for "why does the API return X", code-review requests on backend files, and for performance, security, or reliability questions about the Python service. Do NOT use for designing or writing API test suites from a task — that is the `/api-tests` skill.
---

# Backend: FastAPI + SQLAlchemy async + Pydantic

This skill describes how the `cv-customs/backend/src/cv_customs/` backend is
organized and the conventions you should follow when changing it. Read it
whenever you touch anything under `backend/src/` or `tests/`.

Note the asymmetric layout: **code lives under `backend/src/cv_customs/`,
tests live in the repo-root `tests/`** (`testpaths = ["tests"]`,
`packages = ["backend/src/cv_customs"]` in `pyproject.toml`).

## What this service does

A Python 3.12 FastAPI service backed by PostgreSQL (async SQLAlchemy),
meant for local-only runs. Signed-in users upload résumé files (PDF/DOCX),
which are stored **as-is**: metadata row in Postgres + blob in S3/MinIO.
The service also exposes auth (register, login, password reset, optional
TOTP MFA), legal, and account endpoints. Sign-in is **email + password
only**, registration activates the account immediately (no email
verification).

## Work in this order

Do backend changes from the contract inward. Skipping ahead to writing
SQL or glue code without first pinning the request/response shape is the
single most common source of churn here.

1. Nail down the API contract: URL, method, request body (Pydantic),
   response shape (Pydantic), and the success + failure status codes.
2. Write or extend the repository function in `db/repositories.py` — keep
   it a pure data-access function, no HTTP concerns.
3. Implement the endpoint handler in the right router under `api/`. Keep it
   thin: parse input → call repository (and infra adapters where they
   exist, e.g. `infra/storage.py`) → map result to response model.
4. Decide what can go wrong and translate each failure into a clean
   `HTTPException` with a stable error shape.
5. Add tests in `tests/` before finalizing — success path, at least one
   authorization failure, and at least one validation failure.
6. If schema changed, add an Alembic migration and verify it runs both
   `upgrade` and `downgrade`.

## Project layout

```
backend/src/cv_customs/
├── main.py                 FastAPI app assembly (lifespan, CORS, routers)
├── config.py               Env-driven config (JWT_SECRET, DATABASE_URL, ...)
├── api/
│   ├── auth.py             /api/auth — register, login + shared JWT helpers
│   ├── auth_v1.py          /api/v1/auth — register, login, logout
│   ├── auth_reset.py       /api/v1/auth/password/reset/{request,confirm}
│   ├── auth_mfa.py         /api/v1/auth/mfa/* — mounted only when FEATURE_MFA
│   ├── user.py             /api/user — me, personal-data, revoke-consent, account
│   ├── legal.py            /api/legal — privacy policy
│   └── resumes.py          /api/v1/resumes — upload, list, get, download, delete
├── db/
│   ├── models.py           SQLAlchemy 2.0 ORM (Mapped[...] style)
│   ├── session.py          Async engine, async_sessionmaker, get_session dep
│   └── repositories.py     Data-access functions (no HTTP, no business rules)
├── infra/                  storage (S3), email, request (client_ip), logging
└── utils/                  mime (magic-byte sniffing)
```

Business logic lives in the router handler; there is no separate
application-service layer. Don't introduce one without checking with the
user first — it would be a meaningful architectural change.

The generated contract snapshot is `docs/openapi.json` (MFA included).
Regenerate it whenever routes or schemas change:
`uv run python scripts/openapi_snapshot.py --dump`.

## Stack and versions

- Python 3.12
- FastAPI ≥ 0.115, Uvicorn ≥ 0.32
- SQLAlchemy 2.0 with `asyncio`, asyncpg ≥ 0.30 (aiosqlite for tests)
- Pydantic v2 (schemas inline in routers; `pydantic-settings` for config)
- Alembic ≥ 1.14
- `python-jose[cryptography]` for JWT (HS256), `bcrypt` for passwords
- pytest ≥ 8.4 + pytest-asyncio for tests
- Package manager: **uv** (`uv sync`, `uv run ...`). Prefer `uv run` over
  activating a venv.

## App assembly

`main.py` wires everything. When adding a new router, import it and call
`app.include_router(...)` here. The lifespan handler builds the S3 and
email clients and disposes the engine on shutdown; schema management is
Alembic's job (`alembic upgrade head` before boot). CORS is restricted to
`CORS_ALLOWED_ORIGINS`.

```python
app = FastAPI(title="CV Customs (webinar edition)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth_router)
app.include_router(user_router)
# ...
```

## Endpoint workflow

### 1) Define the contract

Pydantic schemas live **inline in the router file**, not in a separate
`schemas/` directory. That's on purpose: it keeps the contract next to the
handler so a reviewer can see both at once. Keep the schema strict — use
`EmailStr`, `Field(..., min_length=...)`, and `field_validator` instead of
trusting client input.

```python
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    consent: bool = False
    cross_border_consent: bool = False
```

For responses, define a `FooResponse` (or reuse an existing one when the
shape is shared across endpoints) so FastAPI generates an accurate OpenAPI
schema. Don't return raw ORM objects — you'll leak fields and break
clients the next time the model changes.

### 2) Handler skeleton

```python
@router.get("/{resume_id}")
async def get_resume_detail(
    resume_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ResumeFileOut:
    resume = await _load_resume(session, resume_id=resume_id, user=user)
    return _file_out(resume)
```

Why this shape:

- `session` and `user` are **dependencies**, never globals. That is what
  makes tests tractable — you can override either via
  `app.dependency_overrides`.
- `Request` is injected only when you actually need it (IP for audit
  logs). Don't take it habitually.
- The handler delegates to a helper (`_load_resume`) whenever the same
  logic serves more than one route — here, detail/download/delete share
  the owner-scoped 404 lookup.

### 3) Error strategy

Raise `HTTPException(status_code, detail)` at the handler boundary. The
detail must be a short, stable string (it is part of the client contract —
clients match on it). Never include SQL errors, tracebacks, or internal
identifiers.

```python
resume = await repo.get_resume(session, resume_id=rid, user_id=user.id)
if not resume:
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Resume not found")
```

Notice the pattern: "not found or not yours" collapses to the same 404.
That avoids an enumeration oracle where a 403 would confirm that a resource
exists.

Conventional status codes used here:
- `401` for auth failures (missing, invalid, or expired token)
- `403` for authenticated-but-not-allowed (rare — prefer `404`)
- `404` for missing resources
- `422` for validation (FastAPI produces this automatically for Pydantic
  failures — don't re-raise it manually)
- `503` when the blob store (S3/MinIO) is unavailable — uploads and
  downloads must fail loudly, never silently skip the blob

There are no custom exception handlers; FastAPI/Starlette's defaults are
fine.

## Database and repositories

SQLAlchemy 2.0 `Mapped[...]` style, async sessions, no sync ORM anywhere
in the request path. The session dependency handles commit/rollback
scoping implicitly per request — don't open a session manually from a
handler.

```python
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
```

### Repository conventions

All query/write functions live in `db/repositories.py`. Each one takes
`session: AsyncSession` as its first argument and has no knowledge of
HTTP — no `HTTPException`, no `Request`. Keep the names action-verb first:
`create_user_with_email`, `get_user_by_email_ci`, `get_resume`,
`create_resume_file`.

```python
async def create_user_with_email(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    consent_given_at: datetime | None = None,
    cross_border_consent_at: datetime | None = None,
) -> User:
    user = User(
        email=email,
        password_hash=password_hash,
        consent_given_at=consent_given_at,
        cross_border_consent_at=cross_border_consent_at,
    )
    session.add(user)
    await session.flush()
    return user
```

Why `flush()` and not `commit()`: the session dependency commits on
successful handler return; flushing is enough to populate defaults (IDs,
timestamps) so the handler can reference them. That also means a later
error can roll the whole request back atomically.

For list endpoints, return `(items, total_count)` so the handler can build
a paginated response without a second query.

### Performance pitfalls to watch for

- N+1 queries — if a loop accesses `obj.related`, add a `joinedload` or
  `selectinload` to the fetching query.
- Lazy loading after the session has closed — with `expire_on_commit=False`
  attributes are usable, but relationships still need to be loaded during
  the session's lifetime.
- Missing indexes on fields you filter or join on frequently. Check
  `models.py` before adding a new filter.

## Auth

JWT (HS256) signed with `JWT_SECRET`, expiring after `JWT_EXPIRE_HOURS` (4
by default). Payload carries `sub` (user UUID), `exp`, and `iat`. Passwords
are hashed with bcrypt at registration — never store plaintext, never log
either the password or the token.

```python
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = uuid.UUID(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    user = await repo.get_user_by_id(session, user_id)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user
```

Protect a route by depending on `get_current_user`. For endpoints that
must work anonymously or switch on auth state, take an optional auth
dependency instead of branching on `Authorization: None` inside a
"protected" handler.

The client IP for audit logs comes from `infra/request.client_ip`,
which honours `X-Forwarded-For`.

## Security checklist

Apply this to every new endpoint, before opening a PR:

- Authentication is enforced on every protected route via dependency.
- Authorization is enforced **per resource**, not just per endpoint
  group. Collapsing "not found" and "not yours" to `404` is the default
  pattern.
- Input is validated at the boundary via Pydantic. No raw dict parsing.
- Schemas are strict — don't add open-ended `dict` or `Any` fields that
  could let clients over-post data straight into the model.
- Secrets, tokens, raw credentials, and password hashes never appear in
  logs or error responses.
- SQL is parameterized through SQLAlchemy — never string-format values
  into a query.
- File uploads: validate MIME/size before persisting; do not trust the
  filename for on-disk paths.

## Testing

**The rules live in [`docs/test-rules.md`](../../../docs/test-rules.md) — that
file is the single source of truth.** Read it before writing or reviewing a
test; do not restate its rules here or anywhere else.

The short version: `pytest` + `pytest-asyncio`, aiosqlite in-memory DB, the
whole stand (app, DB, JWT headers, email/S3 doubles) comes from
`tests/conftest.py` — a test file only seeds its own data.

To design and write a test suite for a task, use the `/api-tests` skill
rather than doing it ad hoc from here.

## Migrations

Under `alembic/versions/`. Workflow:

1. Change `db/models.py`.
2. `uv run alembic revision --autogenerate -m "describe change"`.
3. **Read the generated migration.** Autogenerate misses things: enum
   changes, server defaults, CHECK constraints, and index drops for
   example. Edit the file until it does what you actually want.
4. Run `uv run alembic upgrade head` locally against a scratch DB.
5. Run `uv run alembic downgrade -1` and back to `upgrade head` to prove
   it's reversible.
6. Commit models + migration together so the repo always matches.

Never edit an already-applied migration — write a new one.

## Observability and reliability

- Track latency and error rates for critical endpoints (auth, upload).
- Log with structured context (request id, user id, operation) so failures
  are traceable without scanning raw text. Avoid f-string-heavy logs that
  encode variables into the message.
- Always paginate list endpoints. Never return an unbounded collection.
- Add timeouts on every outbound HTTP call; add retry only where the
  operation is idempotent. Blind retries amplify outages.

## Code review rubric

Prioritize findings in this order and label each:

1. **Correctness** and data-integrity risks.
2. **Security** / access-control issues.
3. **Reliability** and error handling gaps.
4. **Performance** and scalability concerns (queries, async correctness,
   blocking calls).
5. **Maintainability** and readability.

Severity labels:

- `Critical`: must fix before merge.
- `High`: strongly recommended before merge.
- `Medium`: meaningful improvement, follow-up OK.
- `Low`: optional polish.

## Response style

When answering a backend request, walk the user through:

1. A brief implementation plan (two or three sentences).
2. The actual code changes, layer by layer (schema → repository →
   handler → test).
3. Risks and edge cases you thought about — especially around auth,
   concurrency, and failure modes.
4. A concrete test plan with named cases.

Default to showing your reasoning. Switch to terse output only if the
user asks for concise answers.
