"""Tests for ImageDownloader.await_all() — the persist-phase gate."""

import asyncio
import time

from copixiv.infrastructure.storage.image_downloader import ImageDownloader


def _asset_data(tmp_path, nid: int) -> dict:
    """A data dict that passes process_novel_assets' early-return checks."""
    return {
        "id": nid,
        "path": str(tmp_path / f"{nid}" / f"novel{nid}.txt"),
        "images": {"1": {}},
        "illusts": {},
    }


class TestAwaitAll:
    async def test_awaits_inflight_tasks(self, tmp_path, monkeypatch):
        dl = ImageDownloader(max_workers=2)
        finished: list[int] = []

        def slow_work(data):
            time.sleep(0.2)
            finished.append(data["id"])

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
            return f"boom for {data['id']}"

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
        session = self.FakeSession(self.FakeResponse([b"hello", b" world"]))
        monkeypatch.setattr(
            "copixiv.infrastructure.storage.image_downloader._create_session",
            lambda: session,
        )

        dl = ImageDownloader(max_workers=1)
        try:
            assert dl.download_image("http://x/img.jpg", save_path) is True
        finally:
            dl.shutdown()

        assert save_path.read_bytes() == b"hello world"
        assert session.get_calls == 1

    def test_skips_existing_nonempty_file_without_network(self, tmp_path):
        save_path = tmp_path / "img.jpg"
        save_path.write_bytes(b"already-there")

        dl = ImageDownloader(max_workers=1)
        try:
            assert dl.download_image("http://x/img.jpg", save_path) is True
        finally:
            dl.shutdown()

        assert save_path.read_bytes() == b"already-there"

    def test_content_length_mismatch_fails_without_leaving_file(self, tmp_path, monkeypatch):
        save_path = tmp_path / "img.jpg"
        session = self.FakeSession(
            self.FakeResponse([b"short"], content_length="100"),
        )
        monkeypatch.setattr(
            "copixiv.infrastructure.storage.image_downloader._create_session",
            lambda: session,
        )

        dl = ImageDownloader(max_workers=1)
        try:
            assert dl.download_image("http://x/img.jpg", save_path) is False
        finally:
            dl.shutdown()

        assert not save_path.exists()
        assert list(tmp_path.glob("*.tmp")) == []
