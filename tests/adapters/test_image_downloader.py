"""Tests for ImageDownloader.await_all() — the persist-phase gate."""

import asyncio
import time

from copixiv.core.models import Novel
from copixiv.storage.epub.builder import EpubBuilder
from copixiv.storage.image_downloader import ImageDownloader


def _asset_data(tmp_path, nid: int) -> Novel:
    """A novel model that passes process_novel_assets' early-return checks."""
    return Novel(
        id=nid,
        title=f"novel{nid}",
        author_id=0,
        path=str(tmp_path / f"{nid}" / f"novel{nid}.txt"),
        images={"1": {}},
        illusts={},
    )


class TestAwaitAll:
    async def test_awaits_inflight_tasks(self, tmp_path, monkeypatch):
        dl = ImageDownloader(max_workers=2)
        finished: list[int] = []

        def slow_work(data):
            time.sleep(0.2)
            finished.append(data.id)

        monkeypatch.setattr(dl, "_download_assets", slow_work)
        await dl.process_novel_assets(_asset_data(tmp_path, 1))
        await dl.process_novel_assets(_asset_data(tmp_path, 2))

        assert len(dl._futures) == 2

        await dl.await_all()

        # Event-based proof that await_all waited for the workers — a
        # clock-based lower bound flaked under parallel test load.
        assert sorted(finished) == [1, 2]
        assert dl._futures == []       # in-flight list drained
        dl.shutdown()

    async def test_returns_immediately_when_idle(self):
        dl = ImageDownloader(max_workers=2)
        t0 = time.perf_counter()
        failures = await dl.await_all()
        # Generous bound: anything under a worker round (0.2s) proves we
        # did not wait for the executor.
        assert time.perf_counter() - t0 < 0.2
        assert failures == []
        dl.shutdown()

    async def test_collects_failures(self, tmp_path, monkeypatch):
        """Failed asset tasks are returned as (novel_id, reason) pairs."""
        dl = ImageDownloader(max_workers=2)

        def failing_work(data):
            return f"boom for {data.id}"

        monkeypatch.setattr(dl, "_download_assets", failing_work)
        await dl.process_novel_assets(_asset_data(tmp_path, 1))
        await dl.process_novel_assets(_asset_data(tmp_path, 2))

        failures = await dl.await_all()
        assert failures == [
            (1, "boom for 1"), (2, "boom for 2"),
        ]
        dl.shutdown()

    async def test_new_submits_are_not_waited_on(self, tmp_path, monkeypatch):
        """await_all swaps the list: submissions made while waiting belong
        to the next round and are not waited on."""
        import threading

        dl = ImageDownloader(max_workers=1)
        entered = threading.Event()

        def slow_work(data):
            entered.set()
            time.sleep(0.2)

        monkeypatch.setattr(dl, "_download_assets", slow_work)
        await dl.process_novel_assets(_asset_data(tmp_path, 1))

        async def submit_while_waiting():
            while not entered.is_set():   # worker started → await_all is waiting
                await asyncio.sleep(0.01)
            await dl.process_novel_assets(_asset_data(tmp_path, 2))

        await asyncio.gather(dl.await_all(), submit_while_waiting())

        assert len(dl._futures) == 1   # the late submit is kept for next round
        await dl.await_all()           # and is waited on by the next gate
        assert dl._futures == []
        dl.shutdown()


class TestDownloadImageRealPath:
    """The real download loop — previously only _download_assets was stubbed."""

    class FakeResponse:
        def __init__(self, chunks: list[bytes], content_length: str | None = None):
            self._chunks = chunks
            self.headers = {"content-length": content_length} if content_length else {}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield from self._chunks

    class FakeSession:
        def __init__(self, response):
            self._response = response
            self.get_calls = 0

        def get(self, url, **kwargs):
            self.get_calls += 1
            return self._response

        def close(self):
            pass

    def test_downloads_and_writes_file(self, tmp_path, monkeypatch):
        save_path = tmp_path / "img.jpg"
        # Real JPEG bytes: the download now verifies content (a non-image
        # response is rejected and retried — 2026-09-16).
        import io as _io

        from PIL import Image as _Image

        buf = _io.BytesIO()
        _Image.new("RGB", (4, 4), (7, 7, 7)).save(buf, format="JPEG")
        payload = buf.getvalue()
        session = self.FakeSession(
            self.FakeResponse([payload[:10], payload[10:]])
        )
        monkeypatch.setattr(
            "copixiv.storage.image_downloader.create_image_session",
            lambda *a, **k: session,
        )

        dl = ImageDownloader(max_workers=1)
        try:
            assert dl.download_image("http://x/img.jpg", save_path) is True
        finally:
            dl.shutdown()

        assert save_path.read_bytes() == payload
        assert session.get_calls == 1

    def test_skips_existing_nonempty_file_without_network(self, tmp_path):
        save_path = tmp_path / "img.jpg"
        from PIL import Image as _Image

        _Image.new("RGB", (4, 4), (5, 5, 5)).save(save_path)
        before = save_path.read_bytes()

        dl = ImageDownloader(max_workers=1)
        try:
            assert dl.download_image("http://x/img.jpg", save_path) is True
        finally:
            dl.shutdown()

        # A decodable cached file is reused as-is — no request is made.
        assert save_path.read_bytes() == before

    def test_content_length_mismatch_fails_without_leaving_file(self, tmp_path, monkeypatch):
        save_path = tmp_path / "img.jpg"
        session = self.FakeSession(
            self.FakeResponse([b"short"], content_length="100"),
        )
        monkeypatch.setattr(
            "copixiv.storage.image_downloader.create_image_session",
            lambda *a, **k: session,
        )

        dl = ImageDownloader(max_workers=1)
        try:
            assert dl.download_image("http://x/img.jpg", save_path) is False
        finally:
            dl.shutdown()

        assert not save_path.exists()
        assert list(tmp_path.glob("*.tmp")) == []


class TestDrainOutcomes:
    """``drain_outcomes()`` — the producer-reported outcome ledger.

    It is the data source for flipping ``has_epub`` to DONE from inside the
    persisting transaction (contract 修复1): "done" means the EPUB is on
    disk, "skipped" means none is warranted, "failed" is already in
    ``await_all``'s failure list.
    """

    @staticmethod
    def _text(tmp_path, nid: int, body: str):
        path = tmp_path / str(nid) / f"novel{nid}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    async def test_done_after_epub_written(self, tmp_path):
        body = "正文 [uploadedimage:1]"
        path = self._text(tmp_path, 1, body)
        dl = ImageDownloader(
            max_workers=1, min_interval=0, epub_builder=EpubBuilder(),
        )
        try:
            await dl.process_novel_assets(Novel(
                id=1, title="novel1", author_id=0, path=str(path),
                content=body,
            ))
            assert await dl.await_all() == []
            assert dl.drain_outcomes() == {1: "done"}
            assert dl.drain_outcomes() == {}      # drained: second read is empty
        finally:
            dl.shutdown()

    async def test_done_when_valid_epub_already_exists(self, tmp_path):
        body = "正文 [uploadedimage:1]"
        path = self._text(tmp_path, 6, body)
        assert EpubBuilder().create_epub(Novel(
            id=6, title="novel6", author_id=0, path=str(path),
        )) is True

        dl = ImageDownloader(
            max_workers=1, min_interval=0, epub_builder=EpubBuilder(),
        )
        try:
            await dl.process_novel_assets(Novel(
                id=6, title="novel6", author_id=0, path=str(path),
                content=body, images={"1": {}},
            ))
            assert dl._futures == []              # short-circuit, no worker
            assert dl.drain_outcomes() == {6: "done"}
        finally:
            dl.shutdown()

    async def test_skipped_when_text_has_no_placeholder(self, tmp_path):
        """``needs_epub=False``: the builder writes nothing → not DONE."""
        path = self._text(tmp_path, 2, "纯文字正文")
        dl = ImageDownloader(
            max_workers=1, min_interval=0, epub_builder=EpubBuilder(),
        )
        try:
            # images is non-empty, so the guard lets the worker decide from
            # the body — the builder then returns True without writing.
            await dl.process_novel_assets(Novel(
                id=2, title="novel2", author_id=0, path=str(path),
                content="纯文字正文", images={"1": {}},
            ))
            assert await dl.await_all() == []
            assert dl.drain_outcomes() == {2: "skipped"}
            assert not path.with_suffix(".epub").exists()
        finally:
            dl.shutdown()

    async def test_skipped_when_api_payload_has_no_placeholder(self, tmp_path):
        """Early return (no assets and no placeholder) → skipped, no submit."""
        dl = ImageDownloader(max_workers=1)
        try:
            await dl.process_novel_assets(Novel(
                id=3, title="novel3", author_id=0,
                path=str(tmp_path / "3" / "novel3.txt"),
                content="纯文字正文",
            ))
            assert dl._futures == []
            assert dl.drain_outcomes() == {3: "skipped"}
        finally:
            dl.shutdown()

    async def test_skipped_without_path(self):
        dl = ImageDownloader(max_workers=1)
        try:
            await dl.process_novel_assets(Novel(
                id=4, title="novel4", author_id=0, path=None,
                content="正文 [uploadedimage:1]",
            ))
            assert dl.drain_outcomes() == {4: "skipped"}
        finally:
            dl.shutdown()

    async def test_failed_when_create_epub_returns_false(self, tmp_path):
        body = "正文 [uploadedimage:1]"
        path = self._text(tmp_path, 5, body)

        class FailingBuilder:
            def create_epub(self, novel, needs_epub=None):
                return False

        dl = ImageDownloader(
            max_workers=1, min_interval=0, epub_builder=FailingBuilder(),
        )
        try:
            await dl.process_novel_assets(Novel(
                id=5, title="novel5", author_id=0, path=str(path),
                content=body,
            ))
            failures = await dl.await_all()
            assert failures == [(5, "EPUB 生成失败: novel 5")]
            assert dl.drain_outcomes() == {5: "failed"}
        finally:
            dl.shutdown()
