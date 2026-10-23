"""``scripts/repair_epub.py`` — defect classification must agree with the DB.

The 2026-10 dry run reported 2,108 defects while the database, classified by
``novel.path``, held a few dozen.  Two independent causes, both pinned here:

1. the scan derived the novel id from the **filename** on disk, so a work the
   author renamed (``novel.path`` is written once; ``build_path`` recomputes
   from the current title) looked like "no EPUB" even though its EPUB was
   sitting beside the other name;
2. a placeholder body with no EPUB was reported as repairable long after the
   source images were gone — the state ``check_epub`` correctly settles at
   ``NO_IMAGES``.  The script must use the same give-up rule.

These are script-level behaviours, so the tests import the module directly.
"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "repair_epub.py"


@pytest.fixture(scope="module")
def repair():
    spec = importlib.util.spec_from_file_location("repair_epub", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _txt(path: Path, body: str = "正文 [uploadedimage:1] 尾巴") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _epub_with_marker(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("content.xhtml", "<html><body>[uploadedimage:1]</body></html>")
    return path


def _fresh(path: Path) -> Path:
    """Make *path* look like a recent download (inside the give-up window)."""
    import os
    import time

    now = time.time()
    os.utime(path, (now, now))
    return path


class TestClassify:
    def test_placeholder_body_without_epub_is_noepub(self, repair, tmp_path):
        txt = _fresh(_txt(tmp_path / "a" / "x_1.txt"))
        assert repair.classify(str(txt), 1) == "noepub"

    def test_body_without_placeholder_is_not_a_defect(self, repair, tmp_path):
        """232 k ``NO_IMAGES`` rows: no EPUB will ever be built — not a defect."""
        txt = _fresh(_txt(tmp_path / "a" / "x_2.txt", body="纯文字正文"))
        assert repair.classify(str(txt), 2) == "gone"

    def test_abandoned_row_is_not_a_defect(self, repair, tmp_path):
        """Old body + no image file → ``check_epub`` settles it at NO_IMAGES.

        The repair script must agree, otherwise every dry run counts these as
        "repairable" forever (923 such rows in production).
        """
        import os
        import time

        txt = _txt(tmp_path / "a" / "x_3.txt")
        old = time.time() - 30 * 86400
        os.utime(txt, (old, old))
        assert repair.classify(str(txt), 3) == "gone"

    def test_epub_with_raw_marker_is_placeholder(self, repair, tmp_path):
        txt = _fresh(_txt(tmp_path / "a" / "x_4.txt"))
        _epub_with_marker(txt.with_suffix(".epub"))
        assert repair.classify(str(txt), 4) == "placeholder"

    def test_missing_text_is_gone(self, repair, tmp_path):
        assert repair.classify(str(tmp_path / "a" / "nope_5.txt"), 5) == "gone"
        assert repair.classify(None, 6) == "gone"


class TestClassifyFilesUsesDbPath:
    """The scan must classify the row's stored path, not a directory listing."""

    def test_stored_path_wins_over_a_renamed_sibling(self, repair, tmp_path):
        """A renamed work keeps its EPUB beside the *stored* name.

        The old scan globbed ``*/*.epub`` and read the id out of the filename,
        so after a source rename the orphan copy made the row look like it had
        no EPUB at all.
        """
        stored = _fresh(_txt(tmp_path / "0001" / "旧标题_7.txt"))
        # The EPUB lives beside the stored path — that is what readers resolve.
        _epub_with_marker(stored.with_suffix(".epub"))
        # A stray file under the new title (the drift the scan used to follow).
        _txt(tmp_path / "0001" / "新标题_7.txt", body="新正文")

        rows = [(7, str(stored))]
        wanted = {"placeholder": [], "corrupt": [], "noepub": []}
        repair.classify_files(rows, wanted)

        assert wanted["placeholder"] == [7]
        assert wanted["noepub"] == []

    def test_noepub_only_lists_rows_still_worth_building(self, repair, tmp_path):
        """Rows the give-up rule has settled are not offered for repair."""
        import os
        import time

        fresh = _fresh(_txt(tmp_path / "0001" / "fresh_11.txt"))
        stale = _txt(tmp_path / "0001" / "stale_12.txt")
        old = time.time() - 30 * 86400
        os.utime(stale, (old, old))

        rows = [(11, str(fresh)), (12, str(stale))]
        wanted = {"placeholder": [], "corrupt": [], "noepub": []}
        repair.classify_files(rows, wanted)

        assert wanted["noepub"] == [11]
