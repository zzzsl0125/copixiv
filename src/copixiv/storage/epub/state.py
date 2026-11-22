"""EPUB file/body state predicates — the single owner of "what is true".

This module answers **facts about a file or a body**, never transitions:

* :func:`is_readable_container` — the path opens as a zip at all (weakest);
* :func:`has_raw_markers` — a built EPUB still carries unresolved
  ``[uploadedimage:…]`` / ``[pixivimage:…]`` text in its XHTML;
* :func:`has_embedded_images` — a built EPUB embedded at least one
  illustration under ``images/``;
* :func:`is_complete` — the produced EPUB is actually finished
  (:func:`has_embedded_images` and not :func:`has_raw_markers`).

Why one module: before this, four call sites each re-decided these facts with
their own regex or zip walk — ``builder.is_valid_epub``,
``image_downloader._epub_has_raw_marker``, ``maintenance._epub_is_complete``
and ``core.services.has_image_placeholders`` (three copies of the marker
regex).  Any fix to one silently left the other three disagreeing, which is
how a marker-bearing EPUB could be *done* for the downloader and *pending*
for the reconciler.

The marker pattern itself is **not** duplicated here: text is delegated to
:func:`copixiv.core.services.has_image_placeholders` (the only owner of the
syntax), and bytes to :func:`copixiv.core.services.has_image_placeholders_bytes`
so the 238 k-row sweep never decodes a novel just to look for a marker.

The ``has_epub`` *state machine* (``NO`` / ``PENDING`` / ``DONE`` /
``NO_IMAGES``) stays in :mod:`copixiv.tasks.maintenance` — it is a product
decision (when is a book finished for the reader), not a file fact.
"""

from __future__ import annotations

import zipfile
import zlib
from pathlib import Path

from copixiv.core.text import (
    has_image_placeholders,
    has_image_placeholders_bytes,
)

# Members scanned for raw markers / read as text.  Kept case-insensitive
# because real EPUBs ship ``.XHTML`` and ``.HTML``.
_TEXT_SUFFIXES = (".xhtml", ".html", ".htm")

# The illustration folder inside the generated EPUB.  Deliberately only
# ``images/``: the cover lives at ``cover.jpg`` and must not satisfy this
# check, or every cover-only EPUB would pass as having its illustrations
# embedded (the 216-filelineage regression).
_IMAGES_PREFIX = "images/"


def inspect_epub(path: Path) -> tuple[bool, bool]:
    """Return ``(is_readable, is_complete)`` in a single ZIP open.

    * ``is_readable``: *path* exists and opens as a valid ZIP container.
    * ``is_complete``: *is_readable*, embeds at least one illustration under
      ``images/``, and carries no unresolved raw image markers in XHTML.
    """
    if not path.is_file():
        return False, False
    try:
        with zipfile.ZipFile(path) as archive:
            if not _archive_has_images(archive):
                return True, False
            try:
                has_markers = _archive_has_raw_markers(archive)
            except (OSError, KeyError, zipfile.BadZipFile, zlib.error, RuntimeError):
                return True, False
            return True, not has_markers
    except (OSError, zipfile.BadZipFile):
        return False, False


# ---------------------------------------------------------------------------
# Archive-level helpers
# ---------------------------------------------------------------------------


def _archive_has_raw_markers(archive: zipfile.ZipFile) -> bool:
    """Scan *archive*'s XHTML members for an unresolved marker.

    Read/decompression errors (CRC mismatch, truncated stream, missing member)
    propagate so callers like :func:`is_complete` treat a damaged archive as
    incomplete rather than clean.
    """
    for name in archive.namelist():
        if not name.lower().endswith(_TEXT_SUFFIXES):
            continue
        if has_image_placeholders_bytes(archive.read(name)):
            return True
    return False


def _archive_has_images(archive: zipfile.ZipFile) -> bool:
    """True when *archive* carries a real file entry under ``images/``."""
    # A real file entry, not the "images/" directory entry itself — an empty
    # EPUB can carry the directory and nothing in it.
    return any(_IMAGES_PREFIX in info.filename and not info.is_dir() for info in archive.infolist())


def needs_epub(body: str | None) -> bool | None:
    """Whether *body* warrants an EPUB, or ``None`` when it cannot be judged.

    ``True`` = the text has image placeholders (something to embed),
    ``False`` = plain text (building would create an empty shell), ``None`` =
    the caller has no body at all (a metadata-only draft) and must not guess.
    This is a thin, named wrapper over the marker predicate so call sites stop
    spelling out ``has_image_placeholders(...)`` with their own coalescing.
    """
    if body is None:
        return None
    return has_image_placeholders(body)


__all__ = [
    "inspect_epub",
    "needs_epub",
]
