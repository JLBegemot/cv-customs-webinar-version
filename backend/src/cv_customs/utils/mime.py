"""Server-side MIME sniffing for resume uploads (Phase-1 MVP, Week 3).

Clients can lie about ``Content-Type`` and ``filename`` extensions. The
uploaded blob is what's going to end up in S3 and (indirectly) in the
LLM prompt, so we need the server to decide what kind of file it really
is before we trust it.

The plan originally called for ``python-magic``, which pulls in a C
dependency (``libmagic``) that's finicky on Alpine/Lambda. A tiny magic-
byte sniffer is all we need for the two formats Week 3 cares about
(PDF + DOCX), so we roll our own. If a future week needs to support
more exotic formats we can pivot to ``python-magic`` or ``filetype``
without ripping up the call-sites — they only care about the return
value of :func:`sniff_mime`.
"""

from __future__ import annotations

from typing import Literal

DetectedMime = Literal["application/pdf", "application/docx", "unknown"]

_DOCX_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF-"


def sniff_mime(data: bytes) -> DetectedMime:
    """Return the detected MIME type, or ``"unknown"``.

    * PDFs always start with ``%PDF-`` (per ISO 32000, even if the byte
      order mark is absent).
    * DOCX is a ZIP archive containing ``[Content_Types].xml``; the ZIP
      local file header magic is ``PK\\x03\\x04``. We additionally check
      for the ``word/`` path inside the ZIP central directory, which
      distinguishes Word from other Office / ZIP-based formats (xlsx,
      pptx, odt, ...).
    """

    if not data:
        return "unknown"

    if data.startswith(_PDF_MAGIC):
        return "application/pdf"

    if data.startswith(_DOCX_MAGIC):
        # Cheap heuristic: DOCX archives always mention ``word/`` inside
        # the first few kilobytes (the Content_Types mapping + one of the
        # ``word/_rels/...`` entries). Checking a bounded prefix keeps
        # this O(1) without parsing the whole zip.
        prefix = data[: 8 * 1024]
        if b"word/" in prefix:
            return "application/docx"

    return "unknown"


def expected_extension(mime: DetectedMime) -> str:
    """Map a sniffed MIME to the canonical extension stored in S3.

    Kept here (and not in the storage layer) so the upload handler and
    the extractor both read from the same mapping.
    """

    if mime == "application/pdf":
        return "pdf"
    if mime == "application/docx":
        return "docx"
    raise ValueError(f"no extension known for mime={mime!r}")
