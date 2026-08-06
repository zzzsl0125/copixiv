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

        def slow_work(data):
            time.sleep(0.2)

        monkeypatch.setattr(dl, "_download_assets", slow_work)
        await dl.process_novel_assets(_asset_data(tmp_path, 1))
        await dl.process_novel_assets(_asset_data(tmp_path, 2))

        assert len(dl._futures) == 2

        t0 = time.perf_counter()
        await dl.await_all()
        elapsed = time.perf_counter() - t0

        assert elapsed >= 0.2          # really waited for the workers
        assert dl._futures == []       # in-flight list drained
        dl.shutdown()

    async def test_returns_immediately_when_idle(self):
        dl = ImageDownloader(max_workers=2)
        t0 = time.perf_counter()
        failures = await dl.await_all()
        assert time.perf_counter() - t0 < 0.05
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
