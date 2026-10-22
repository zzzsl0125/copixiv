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
from pathlib import Path

from copixiv.core.services import (
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


def is_readable_container(path: Path) -> bool:
    """True when *path* exists and is a readable zip container.

    Deliberately weak: this answers "can it even be opened as an EPUB", not
    "is it complete".  A zip whose XHTML still holds raw markers passes
    here — combine it with :func:`is_complete` (``check_epub`` does) before
    calling a novel done.
    """
    return path.is_file() and zipfile.is_zipfile(path)


def has_raw_markers(path: Path) -> bool:
    """True when a built EPUB still carries an unresolved image marker.

    Such a file is readable but *not finished*: the reader would see
    ``[pixivimage:123]`` as literal text.  The "already exists ⇒ done" guard
    must not treat it as complete.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.lower().endswith(_TEXT_SUFFIXES):
                    continue
                if has_image_placeholders_bytes(archive.read(name)):
                    return True
    except (OSError, zipfile.BadZipFile, KeyError):
        return False
    return False


def has_embedded_images(path: Path) -> bool:
    """True when the EPUB embedded at least one illustration.

    Guards promotion to ``DONE``: a file written from a draft whose images
    had not landed yet is empty of pictures and must be regenerated instead
    of being blessed as done.  The cover is excluded on purpose (see
    ``_IMAGES_PREFIX``).
    """
    try:
        with zipfile.ZipFile(path) as archive:
            # A real file entry, not the "images/" directory entry itself —
            # an empty EPUB can carry the directory and nothing in it.
            return any(
                _IMAGES_PREFIX in info.filename and not info.is_dir()
                for info in archive.infolist()
            )
    except (OSError, zipfile.BadZipFile):
        return False


def is_complete(path: Path) -> bool:
    """True when *path* is a usable book: no raw marker left, ≥1 image.

    This is the single "the produced EPUB is actually finished" predicate.
    Every promotion to ``DONE`` uses it, so a file written while its images
    were missing can never be blessed regardless of the row's current status.
    """
    return has_embedded_images(path) and not has_raw_markers(path)


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
    "has_embedded_images",
    "has_raw_markers",
    "is_complete",
    "is_readable_container",
    "needs_epub",
]
