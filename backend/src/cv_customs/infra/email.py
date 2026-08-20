"""Outbound email client (Phase-1 MVP, Week 2).

Transactional email is a background concern for the API — we don't want the
HTTP layer blocking on the provider, but we do want a single abstraction so
password-reset / welcome / trial-ending / payment-failed flows all go
through the same code path.

Two implementations live here:

* :class:`YandexPostboxEmailClient` — real send via Yandex Cloud Postbox,
  used whenever ``settings.yc_postbox_access_key_id`` is set. Postbox
  exposes the AWS SES v2 API, so we authenticate with Yandex Cloud Service
  Account static access keys and reuse ``aioboto3`` (already pulled in for
  S3 in Week 1) — no second SDK required.
* :class:`NullEmailClient` — dev/test fallback that buffers the outbound
  record and logs at INFO. ``tests/`` assert against this buffer so the
  unit layer doesn't need a real Postbox account or any HTTP mocks.

A module-level factory :func:`create_email_client` picks the right one at
app startup (called from the lifespan) and stores it on ``app.state.email``.

``template_alias`` maps directly to a Postbox template name — create one
per transactional email in the Postbox console (or via the ``CreateEmail
Template`` SES v2 call) with the same alias we pass here. Template model
fields are serialised to JSON and sent as ``TemplateData``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from ..config import settings

log = logging.getLogger(__name__)


class EmailClient(Protocol):
    async def send_template(
        self,
        *,
        template_alias: str,
        to: str,
        model: Mapping[str, Any],
        tag: str | None = None,
    ) -> None: ...


@dataclass
class NullEmailClient:
    """In-memory logger used in dev / tests.

    Captures the last N sends so tests can assert a reset email was
    actually enqueued without poking stdout. Keeping the buffer small
    prevents memory growth in long-lived dev processes.
    """

    buffer_size: int = 64
    sent: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.sent = []

    async def send_template(
        self,
        *,
        template_alias: str,
        to: str,
        model: Mapping[str, Any],
        tag: str | None = None,
    ) -> None:
        record = {
            "template_alias": template_alias,
            "to": to,
            "model": dict(model),
            "tag": tag,
        }
        assert self.sent is not None  # for type-checker
        self.sent.append(record)
        if len(self.sent) > self.buffer_size:
            self.sent = self.sent[-self.buffer_size :]
        log.info(
            "email.send_template[null] to=%s template=%s tag=%s",
            to,
            template_alias,
            tag,
        )


@dataclass
class YandexPostboxEmailClient:
    """Yandex Cloud Postbox client over the SES v2 wire protocol.

    Postbox is API-compatible with AWS SES v2, so ``aioboto3`` gives us a
    native async client once we override the endpoint URL. A fresh client
    is built per send (boto session objects are not reentrant across event
    loops) — the cost is negligible compared to the outbound HTTPS call
    and it dodges long-lived resource leaks on hot-reload in dev.

    Messages are sent via templates: ``template_alias`` must already exist
    in Postbox (see the docs at the top of this module). Postbox rejects
    sends from unverified sender addresses with ``MessageRejected`` — add
    and verify ``FROM_EMAIL`` in the console before flipping this on in
    production.
    """

    access_key_id: str
    secret_access_key: str
    region: str
    endpoint_url: str
    from_email: str
    from_name: str
    _session: Any = None

    def __post_init__(self) -> None:
        try:
            import aioboto3  # type: ignore import-not-found
        except Exception as exc:  # pragma: no cover — missing dep
            raise RuntimeError(
                "YandexPostboxEmailClient requires the `aioboto3` package"
            ) from exc
        self._session = aioboto3.Session()

    async def send_template(
        self,
        *,
        template_alias: str,
        to: str,
        model: Mapping[str, Any],
        tag: str | None = None,
    ) -> None:
        assert self._session is not None  # set in __post_init__
        from_header = f"{self.from_name} <{self.from_email}>"
        template_data = json.dumps(dict(model))
        # Postbox supports SES v2 message tags — surface our ``tag`` as a
        # single ``Tags`` entry so downstream analytics can group by
        # template / flow without us inventing a new header.
        email_tags = [{"Name": "tag", "Value": tag}] if tag else []

        try:
            async with self._session.client(
                "sesv2",
                endpoint_url=self.endpoint_url,
                region_name=self.region,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
            ) as client:
                await client.send_email(
                    FromEmailAddress=from_header,
                    Destination={"ToAddresses": [to]},
                    Content={
                        "Template": {
                            "TemplateName": template_alias,
                            "TemplateData": template_data,
                        }
                    },
                    EmailTags=email_tags,
                )
        except Exception as exc:  # pragma: no cover — runtime networking
            log.error(
                "email.send_template[postbox] failed to=%s template=%s err=%s",
                to,
                template_alias,
                exc,
            )
            raise


def create_email_client() -> EmailClient:
    """Pick the right email client for the current environment.

    Called once from the FastAPI lifespan. The null client is also the
    default in tests, which keeps them fast and offline.
    """

    if settings.yc_postbox_access_key_id and settings.yc_postbox_secret_access_key:
        return YandexPostboxEmailClient(
            access_key_id=settings.yc_postbox_access_key_id,
            secret_access_key=settings.yc_postbox_secret_access_key,
            region=settings.yc_postbox_region,
            endpoint_url=settings.yc_postbox_endpoint_url,
            from_email=settings.from_email,
            from_name=settings.from_name,
        )
    log.info("email: YC_POSTBOX_ACCESS_KEY_ID / SECRET unset — using NullEmailClient")
    return NullEmailClient()
