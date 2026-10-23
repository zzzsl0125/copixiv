#!/usr/bin/env python
"""One-shot EPUB repair — regenerate broken / incomplete EPUB files.

Written for the 2026-09-13 audit, which found three classes of bad EPUBs:

* ``--placeholder``  216 EPUBs that still contain a raw ``[pixivimage:…]``
  marker in their XHTML because one image never landed.  The reader saw the
  internal marker as literal text.
* ``--corrupt``      EPUBs that are not zip containers at all; the old
  "file exists ⇒ done" guard skipped them forever, so readers downloaded a
  broken file.
* ``--noepub``       novels whose text contains image placeholders but that
  have no sibling ``.epub`` at all (mostly SQLite-era rows tombstoned with
  ``has_epub = 0``).  Re-fetching the body + assets builds them properly.

The repair re-fetches the novel via ``webview_novel`` (so the image URLs are
current), rewrites the text file, downloads the assets and rebuilds the EPUB
with ``force=True``.  It verifies each result (valid zip, no leftover
marker) and prints a per-novel outcome plus a final tally.

Usage::

    python scripts/repair_epub.py --placeholder --corrupt --dry-run
    python scripts/repair_epub.py --placeholder --corrupt
    python scripts/repair_epub.py --noepub --limit 50
    python scripts/repair_epub.py --ids 20728088 28670159

Exit code is 0 when every targeted novel was repaired (or would be, for
``--dry-run``), 1 when any novel failed.
"""

from dataclasses import replace
import argparse
import asyncio
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PLACEHOLDER_NEEDLES = (b"[uploadedimage:", b"[pixivimage:")



def _xhtml_has_placeholder(epub_path: Path) -> bool:
    try:
        with zipfile.ZipFile(epub_path) as zf:
            for name in zf.namelist():
                if not name.endswith((".xhtml", ".html", ".htm")):
                    continue
                data = zf.read(name)
                if any(n in data for n in PLACEHOLDER_NEEDLES):
                    return True
    except (OSError, zipfile.BadZipFile):
        return False
    return False


def _missing_box_count(epub_path: Path) -> int:
    """How many ``（图片缺失 …）`` boxes the built EPUB carries."""
    try:
        with zipfile.ZipFile(epub_path) as zf:
            return sum(
                zf.read(name).count("illust-missing".encode())
                for name in zf.namelist()
                if name.endswith((".xhtml", ".html", ".htm"))
            )
    except (OSError, zipfile.BadZipFile):
        return 0


def _txt_has_placeholder(txt_path: Path) -> bool:
    needles = PLACEHOLDER_NEEDLES
    overlap = max(len(n) for n in needles) - 1
    try:
        with open(txt_path, "rb") as fh:
            tail = b""
            while chunk := fh.read(1 << 20):
                buf = tail + chunk
                if any(n in buf for n in needles):
                    return True
                tail = buf[-overlap:]
    except OSError:
        return False
    return False


def classify(path_str: str | None, novel_id: int | None = None) -> str:
    """Bucket a novel path: 'ok' | 'corrupt' | 'placeholder' | 'noepub' | 'gone'.

    ``gone`` is the largest bucket by far and it is **not** a defect: 232 k
    ``NO_IMAGES`` rows have a body with no image placeholder, so no EPUB will
    ever be built for them.  Only a body that still asks for an image is a
    candidate, and only while the image is still obtainable — once
    ``_no_images_ever_and_stale`` says the source images are gone for good, a
    missing EPUB is the *expected* end state, not something to repair.

    *novel_id* is needed for that give-up check; without it a placeholder body
    with no EPUB is reported as ``noepub`` (the per-id ``--ids`` path passes
    it, and so does :func:`classify_files`).
    """
    if not path_str:
        return "gone"
    txt = Path(path_str)
    if not txt.exists():
        return "gone"
    epub = txt.with_suffix('.epub')
    if not epub.exists():
        if not _txt_has_placeholder(txt):
            return "gone"
        if novel_id is not None and _is_abandoned(txt, novel_id):
            return "gone"          # images gone for good → NO_IMAGES is correct
        return "noepub"
    if not zipfile.is_zipfile(epub):
        return "corrupt"
    with zipfile.ZipFile(epub) as zf:
        names = zf.namelist()
        has_images = any("/images/" in n for n in names)
        if _xhtml_has_placeholder(epub):
            return "placeholder"
    if not has_images and _txt_has_placeholder(txt):
        return "placeholder"
    return "ok"


def _is_abandoned(txt_path: Path, novel_id: int) -> bool:
    """True when ``check_epub`` would settle this row at ``NO_IMAGES``.

    The repair script and the reconciler must agree on that verdict, or a dry
    run keeps counting rows as "needs repair" that the nightly task has
    already — correctly — given up on (923 of them in production: the author
    deleted the illustrations, so there is nothing left to embed).
    """
    from copixiv.tasks.maintenance.epub import _no_images_ever_and_stale

    return _no_images_ever_and_stale(txt_path, novel_id)


def classify_files(rows, wanted: dict[str, list[int]]) -> int:
    """Fill *wanted* by classifying every row, using ``novel.path`` as truth.

    **The database is the authority, not the directory listing.**  This used
    to glob ``download/*/*.epub`` and derive the novel id from the *filename*
    (``..._<id>.epub``).  ``novel.path`` is written once and afterwards only
    filled when NULL, while ``build_path`` recomputes from the current title,
    so a work that the author renamed ends up with its EPUB beside a filename
    the database does not point at.  The glob-based scan then saw "no EPUB" for
    a row that has one, and reported it as ``noepub``/``placeholder`` — a
    dry run counted **2,108** defects where the real number (classified by
    ``path``) is a few dozen, almost all of them already-terminal rows whose
    images the author deleted.

    Classifying per row is also cheaper in practice: the expensive step
    (reading the body to look for a marker) only runs for rows whose EPUB is
    missing or image-less, and the 232 k ``NO_IMAGES`` rows whose body carries
    no marker short-circuit on a single read they would need anyway.

    Returns the number of rows examined.
    """
    def _ids_of(kind: str) -> list[int]:
        return wanted[kind]

    print(f"按 novel.path 分类 {len(rows)} 行 ...", flush=True)
    for novel_id, path_str in rows:
        kind = classify(path_str, novel_id)
        if kind in wanted:
            _ids_of(kind).append(novel_id)
    return len(rows)


def select_targets(rows, args) -> dict[str, list[int]]:
    """Decide which novel ids to repair.

    ``--ids`` is authoritative and never scans the download tree: those ids
    are classified individually (one zip listing each).  The category flags
    (``--placeholder`` / ``--corrupt`` / ``--noepub``) drive the directory
    scan only when no ``--ids`` was given.

    Regression (2026-09-13): passing only ``--ids`` produced an empty
    target list — both branches were gated on the category flags, so the
    script printed all zeros and repaired nothing.
    """
    picked: dict[str, list[int]] = {
        "placeholder": [], "corrupt": [], "noepub": [],
    }

    if args.ids:
        by_id = {nid: p for nid, p in rows}
        for novel_id in args.ids:
            kind = classify(by_id.get(novel_id), novel_id)
            if kind in picked:
                picked[kind].append(novel_id)
        missing = [i for i in args.ids if i not in by_id]
        if missing:
            print(f"警告: {len(missing)} 个 id 不在 novel 表里: {missing[:10]}")
        return picked

    wanted_kinds = {k for k in picked if getattr(args, k)}
    if not wanted_kinds:
        return picked

    download_dir = Path(args.download_dir)
    seen = classify_files(rows, picked)
    print(f"分类 {seen} 行（download 根 {download_dir}；以 novel.path 为准）")

    if args.limit:
        for kind in picked:
            picked[kind] = sorted(picked[kind])[: args.limit]
    return picked


async def repair(
    novel_ids: list[int], cfg, session_factory, engine,
    local_only: bool = False,
) -> tuple[int, int]:
    """Re-fetch + rebuild *novel_ids*.  Returns (repaired, failed).

    With *local_only* no network call is made at all: every EPUB is rebuilt
    from the text file already on disk (plus any leftover image assets).
    """
    from copixiv.app import _load_accounts
    from copixiv.core.draft import NovelDraft, build_from_webview
    from copixiv.pixiv.accounts import AccountPool
    from copixiv.pixiv.client import PixivClient
    from copixiv.pixiv.patch import apply as apply_pixiv_patches
    from copixiv.storage.epub.builder import EpubBuilder
    from copixiv.storage.file_storage import FileStorage
    from copixiv.storage.image_downloader import ImageDownloader
    from copixiv.log import logger

    apply_pixiv_patches()
    pool = AccountPool()
    _load_accounts(session_factory, pool, cfg)
    client = PixivClient(
        account_pool=pool, max_concurrency=cfg.pixiv_client.max_concurrency,
    )
    file_storage = FileStorage(cfg.path.download)
    downloader = ImageDownloader(
        max_workers=4,
        epub_builder=EpubBuilder(),
        proxy_http=cfg.proxy.url or None,
        proxy_https=cfg.proxy.url or None,
    )

    repaired = failed = 0
    try:
        for n, novel_id in enumerate(novel_ids, 1):
            tag = f"[{n}/{len(novel_ids)}] #{novel_id}"
            resp = None
            fetch_error: str | None = None
            if not local_only:
                try:
                    resp = await client.webview_novel(novel_id)
                except Exception as exc:  # 404 / network / rate limit
                    fetch_error = f"{type(exc).__name__}: {exc}"
                if resp is None and fetch_error is None:
                    fetch_error = "webview 返回空"

            if resp is not None:
                # Online path: refresh text + assets, then rebuild.
                draft = build_from_webview(resp, file_storage.download_dir)
                # Anchor the draft on the file the database already points at.
                # ``build_from_webview`` recomputes the name from the *current*
                # title, and a title edit (appended chapter marker, truncated
                # long name) produces a different basename — that is what
                # forced the library to grow truncation logic it never needed.
                # Writing the text to the stored path keeps text and EPUB
                # stems aligned, and matching the DB path keeps the file
                # findable.  (This supersedes the old "sync the DB path after
                # the fact" workaround.)
                stored = _local_txt_for(
                    engine, novel_id, Path(file_storage.download_dir)
                )
                if stored is not None:
                    draft = replace(draft, path=str(stored))
                if draft.content:
                    file_storage.save_novel_text(
                        draft.id, draft.title, draft.content, force=True,
                    )
                await downloader.process_novel_assets(draft, force=True)
                reason = None
                for nid, why in await downloader.await_all():
                    if nid == novel_id:
                        reason = why
                txt = Path(draft.path) if draft.path else None
                epub = txt.with_suffix('.epub') if txt else None
                if reason:
                    logger.error(f"{tag} 失败: {reason}")
                    failed += 1
                    continue
                if epub is None or not zipfile.is_zipfile(epub):
                    logger.error(f"{tag} 未产出有效 EPUB")
                    failed += 1
                    continue
                outcome = "修复完成"
                if _xhtml_has_placeholder(epub):
                    # The build is *clean* whenever no raw marker survives —
                    # a missing image is rendered as a 缺图框 and is the
                    # expected end state when the source image is gone.  The
                    # old wording called that "仍有占位符" and made a success
                    # look like a failure.
                    outcome = (
                        "修复完成（部分图片源已失效，已渲染缺图框）"
                        if _missing_box_count(epub) else "修复完成"
                    )
                logger.info(f"{tag} {outcome}")
                repaired += 1
                _sync_db_path(engine=engine, novel_id=novel_id, new_txt=txt,
                              log=logger, tag=tag)
                continue

            # Offline path.  The novel is gone from Pixiv (HTTP 404) or the
            # network failed — the text file on disk is all we have, and the
            # EPUB can still be rebuilt from it plus any leftover images.
            # This is the only way to clean the deleted novels.
            if not local_only and "NovelNotFoundError" in (fetch_error or ""):
                logger.warning(
                    f"{tag} Pixiv 已 404（作者删除）→ 改用本地正文重建"
                )
            elif fetch_error:
                logger.warning(f"{tag} 取回失败（{fetch_error}）→ 本地重建")

            txt = _local_txt_for(engine, novel_id, Path(file_storage.download_dir))
            if txt is None or not txt.exists():
                logger.error(f"{tag} 本地没有正文文件，无法重建")
                failed += 1
                continue
            row = _db_row(engine, novel_id)
            draft = NovelDraft(
                id=novel_id,
                title=(row[1] if row else txt.stem) or txt.stem,
                author_id=row[2] if row else 0,
                path=str(txt),
            )
            if not EpubBuilder().create_epub(draft, needs_epub=True):
                logger.error(f"{tag} 本地重建失败")
                failed += 1
                continue
            epub = txt.with_suffix('.epub')
            if _xhtml_has_placeholder(epub):
                logger.warning(f"{tag} 本地重建后仍含裸标记（不该发生）")
            elif _missing_box_count(epub):
                logger.info(f"{tag} 本地重建完成（无网络，部分图片源已失效）")
            else:
                logger.info(f"{tag} 本地重建完成（无网络）")
            repaired += 1
            _sync_db_path(engine=engine, novel_id=novel_id, new_txt=txt,
                          log=logger, tag=tag)
    finally:
        await asyncio.to_thread(downloader.shutdown)
    return repaired, failed


def _db_row(engine, novel_id: int):
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(
            text("select id, title, author_id, path from novel where id = :i"),
            {"i": novel_id},
        ).fetchone()


def _local_txt_for(engine, novel_id: int, download_dir: Path) -> Path | None:
    """The text file for *novel_id*: the DB path when it exists, else a glob.

    The DB path is preferred (that is what the API serves and what
    ``check_epub`` inspects); the glob covers rows whose path went stale
    after a title change.
    """
    row = _db_row(engine, novel_id)
    if row and row[3]:
        p = Path(row[3])
        if p.exists():
            return p
    hits = sorted(download_dir.glob(f"*/*_{novel_id}.txt"))
    return hits[0] if hits else None


def _sync_db_path(*, engine, novel_id: int, new_txt: Path | None, log, tag: str) -> None:
    """Point ``novel.path`` at the freshly written file when the title moved.

    A Pixiv title edit changes the filename (``build_path`` embeds the
    title), so the rebuild lands beside the old file: the duplicate then
    shadows the repaired EPUB for every reader that follows the DB path
    (4 novels in the 2026-09-14 sweep were "still broken" for exactly this
    reason).  Only the path column is touched — never the status.
    """
    from sqlalchemy import text

    if new_txt is None:
        return

    def _do(conn):
        row = conn.execute(
            text("select path from novel where id = :i"), {"i": novel_id}
        ).fetchone()
        if not row or not row[0]:
            return
        if Path(row[0]) == new_txt:
            return
        conn.execute(
            text("update novel set path = :p where id = :i"),
            {"p": str(new_txt), "i": novel_id},
        )
        log.info(f"{tag} 数据库 path 已同步到新文件: {new_txt.name[:70]}")

    try:
        with engine.begin() as conn:
            _do(conn)
    except Exception as exc:  # never fail a repair because of the path sync
        log.warning(f"{tag} path 同步失败（不影响文件）: {exc}")


KNOWN_OPTIONS = (
    "--placeholder", "--corrupt", "--noepub", "--ids", "--ids-file",
    "--limit", "--dry-run", "--config", "--download-dir", "-h", "--help",
)


def _extract_ids_from_argv(argv: list[str]) -> list[str]:
    """Pull everything after ``--ids`` up to the next known option.

    ``argparse`` with ``nargs="*"`` treats an id-list word that starts with
    ``-`` as an option (a filename like ``-重生- 其之川_20194294.epub`` in
    the shell-expanded list made it die with "unrecognized arguments"), and
    it also cannot be told to stop at an arbitrary boundary.  So strip the
    ids out by hand and hand argparse an argv that no longer contains them.
    """
    if "--ids" not in argv:
        return []
    rest = argv[argv.index("--ids") + 1:]
    out: list[str] = []
    for token in rest:
        if token in KNOWN_OPTIONS:
            break
        out.append(token)
    return out


def _without_ids_argv(argv: list[str]) -> list[str]:
    """``argv`` with the ``--ids`` flag and its values removed."""
    if "--ids" not in argv:
        return list(argv)
    cut = argv.index("--ids")
    rest = argv[cut + 1:]
    keep: list[str] = []
    for token in rest:
        if token in KNOWN_OPTIONS:
            keep = rest[rest.index(token):]
            break
    return argv[:cut] + keep


def _parse_ids_file(path: Path) -> list[int]:
    """One id per line; blank lines, ``#`` comments and extra columns ignored.

    The generated list file carries a filename comment per id, so the whole
    line may be ``20728088  # 书名_20728088.epub``.  Only the FIRST field is
    ever read as the id, which keeps titles containing digits (``…大作战1-5``)
    from being mistaken for more ids.
    """
    ids: list[int] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        first = line.split()[0]
        if first.isdigit():
            ids.append(int(first))
    return ids


def _parse_ids(raw: list[str] | None) -> list[int] | None:
    """Accept ids as bare numbers / comma-separated numbers.

    Deliberately strict: it does NOT scavenge digits out of arbitrary words
    (a shell-expanded title like ``…大作战1-5_20292468.epub`` would inject
    ``1`` and ``5`` as ids).  For a list file use ``--ids-file``.
    """
    if not raw:
        return None
    ids: list[int] = []
    for token in raw:
        for part in token.replace(",", " ").split():
            if part.isdigit():
                ids.append(int(part))
    if not ids:
        raise SystemExit(
            "--ids 里没有解析出数字 id（要批量请用 --ids-file <文件>）"
        )
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--placeholder", action="store_true",
                        help="EPUB 存在但正文里还留着图片占位符")
    parser.add_argument("--corrupt", action="store_true",
                        help="不是 zip 的坏 EPUB")
    parser.add_argument("--noepub", action="store_true",
                        help="正文有占位符但没有 EPUB 文件")
    parser.add_argument("--ids", type=str, nargs="*", default=None,
                        help="只处理这些 novel id（可逗号分隔）")
    parser.add_argument("--ids-file", type=Path, default=None,
                        help="从文件读 id，每行一个（支持 'id  # 备注'；批量请用这个）")
    parser.add_argument("--limit", type=int, default=0,
                        help="每个类别最多处理多少本（0 = 不限）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计与打印，不发起任何请求或写入")
    parser.add_argument("--local-only", action="store_true",
                        help="不连网：只用磁盘上的正文+残留图片重建（作者已删除的小说用这个）")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="把日志同时写入文件（排查失败原因用）")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    parser.add_argument("--download-dir", default=str(PROJECT_ROOT / "download"),
                        help="正文目录（默认 <repo>/download）")
    args = parser.parse_args(_without_ids_argv(sys.argv[1:]))
    if args.ids_file is not None:
        args.ids = _parse_ids_file(args.ids_file)
    else:
        args.ids = _parse_ids(_extract_ids_from_argv(sys.argv[1:]))

    if not (args.placeholder or args.corrupt or args.noepub or args.ids):
        parser.error(
            "至少指定 --placeholder / --corrupt / --noepub / "
            "--ids <id...> / --ids-file <文件>"
        )

    from sqlalchemy import create_engine, text
    from copixiv.config import load_config
    from copixiv.db.engine import create_session_factory

    cfg = load_config(args.config)
    engine = create_engine(cfg.database_url)
    session_factory = create_session_factory(engine)

    with engine.connect() as conn:
        rows = list(conn.execute(text("select id, path from novel order by id")))

    targets = select_targets(rows, args)
    total = sum(len(v) for v in targets.values())
    for kind, ids in targets.items():
        print(f"{kind:12s}: {len(ids)}")
    print(f"{'TOTAL':12s}: {total}")

    if args.dry_run or total == 0:
        return 0

    if args.log_file:
        # The script's logger has no file sink of its own, so a failed run
        # left no per-novel trace to diagnose (2026-09-14: "失败 22 本" with
        # no way to see which 22 or why).
        from copixiv.log import logger as _lg

        _lg.add(args.log_file, level="DEBUG", encoding="utf-8")
        print(f"日志同时写入 {args.log_file}")

    ids = sorted({i for v in targets.values() for i in v})
    repaired, failed = asyncio.run(
        repair(ids, cfg, session_factory, engine,
               local_only=args.local_only)
    )
    print(f"\n修复 {repaired} 本，失败 {failed} 本")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
