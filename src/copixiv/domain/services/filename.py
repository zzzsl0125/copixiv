"""Safe filename and path generation — pure functions."""

import re
from pathlib import Path

# Characters illegal in Windows / Linux filenames
_ILLEGAL_CHARS: re.Pattern[str] = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RUN: re.Pattern[str] = re.compile(r"\s+")


def safe_filename(text: str, max_length: int = 240) -> str:
    """Clean *text* of illegal filename characters and truncate to *max_length* bytes.

    Returns ``"untitled"`` if the result would be empty.
    """
    clean = _ILLEGAL_CHARS.sub("", text)
    clean = _WHITESPACE_RUN.sub(" ", clean).strip()
    if not clean:
        return "untitled"

    encoded = clean.encode("utf-8")
    if len(encoded) > max_length:
        encoded = encoded[:max_length]
        # Don't split a multi-byte character
        while True:
            try:
                clean = encoded.decode("utf-8")
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]
    return clean.strip()


def build_path(
    novel_id: int | str,
    title: str,
    download_dir: str = "download",
) -> str:
    """Generate the filesystem path where a novel's text should be stored.

    Format: ``{download_dir}/{subdir}/{safe_title}_{novel_id}.txt``
    """
    id_int = int(novel_id)
    subdir = "0000" if id_int < 10_000_000 else str(id_int).zfill(8)[:4]
    filename = f"{safe_filename(title)}_{id_int}.txt"
    return str(Path(download_dir) / subdir / filename)
