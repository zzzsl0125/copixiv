"""演示:EPUB 处理的三种模式 —— fire-and-forget vs 等待接口 vs 同步化

结构模拟项目 ImageDownloader(ThreadPoolExecutor + submit),但用假任务
代替真实下载,方便精确测量时间线:

  第 1 幕:现状(fire-and-forget)—— 调用返回时文件还没生成(竞态)
  第 2 幕:方案 A —— await_all() 等待接口:persist 前等所有 in-flight 完成
  第 3 幕:方案 B —— 同步化:process 内部等待,调用方天然拿到就绪状态
  第 4 幕:关键细节 —— await_all 的实现不能阻塞事件循环(wrap_future)
  第 5 幕:三种模式对比总结
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

OUT = Path("scratch/demo_epub_out")


def sep(title: str) -> None:
    print(f"\n{'=' * 68}\n【{title}】\n{'=' * 68}")


def make_novel(nid: int, work: float) -> dict:
    return {"id": nid, "work": work, "out": OUT / str(nid)}


# ---------------------------------------------------------------------------
# 微缩版 ImageDownloader —— 结构和项目 ImageDownloader 一致
# ---------------------------------------------------------------------------

class MicroImageDownloader:
    """模拟:process_novel_assets 丢线程池后立刻返回(fire-and-forget)。"""

    def __init__(self, max_workers: int = 2):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._futures: list[Future] = []

    def process_novel_assets(self, data: dict) -> None:
        self._futures.append(self._executor.submit(self._work, data))

    def _work(self, data: dict) -> int:
        # 模拟「下载图片 + PIL 压缩 + 写 EPUB」的耗时工作
        time.sleep(data["work"])
        path = data["out"] / f"{data['id']}.epub"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fake epub")
        return data["id"]

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# 第 1 幕:现状 —— 调用返回时,文件还没生成
# ---------------------------------------------------------------------------

def act1_fire_and_forget() -> None:
    dl = MicroImageDownloader()
    dl.process_novel_assets(make_novel(1, work=0.3))

    t0 = time.perf_counter()
    exists_now = (OUT / "1" / "1.epub").exists()
    print(f"调用返回后立刻检查: 文件存在 = {exists_now} ← 竞态!任务说完成了,文件没好")
    time.sleep(0.4)
    print(f"0.4s 后检查:         文件存在 = {(OUT / '1' / '1.epub').exists()}")
    print(f"(从调用到文件就绪实际耗时 {time.perf_counter() - t0:.2f}s)")
    dl.shutdown()


# ---------------------------------------------------------------------------
# 第 2 幕:方案 A —— await_all() 等待接口
# ---------------------------------------------------------------------------

class MicroDownloaderWithWait(MicroImageDownloader):
    """方案 A:新增 await_all() —— persist 前等所有 in-flight 完成。"""

    async def await_all(self) -> None:
        # 关键:用 asyncio.wrap_future 包装,await 时不阻塞事件循环
        # (直接 f.result() 会卡住整个服务器)
        for f in self._futures:
            await asyncio.wrap_future(f)
        self._futures.clear()


async def act2_await_all() -> None:
    dl = MicroDownloaderWithWait()

    t0 = time.perf_counter()
    dl.process_novel_assets(make_novel(2, work=0.3))
    dl.process_novel_assets(make_novel(3, work=0.3))

    await dl.await_all()                       # persist 前的「关卡」
    ready = all((OUT / str(i) / f"{i}.epub").exists() for i in (2, 3))
    print(f"await_all() 之后: 2 篇文件都就绪 = {ready}")
    print(f"总耗时 {time.perf_counter() - t0:.2f}s "
          f"(两个 0.3s 任务并行,≈0.3s —— 等待不损失并发)")
    dl.shutdown()


# ---------------------------------------------------------------------------
# 第 3 幕:方案 B —— 同步化:process 内部就等
# ---------------------------------------------------------------------------

class MicroSyncDownloader(MicroImageDownloader):
    """方案 B:process_novel_assets 是 async,内部 await 线程池结果。"""

    async def process_novel_assets(self, data: dict) -> None:
        await asyncio.to_thread(self._work, data)   # 调用方拿到时已经就绪


async def act3_sync() -> None:
    dl = MicroSyncDownloader()

    t0 = time.perf_counter()
    await asyncio.gather(
        dl.process_novel_assets(make_novel(4, work=0.3)),
        dl.process_novel_assets(make_novel(5, work=0.3)),
    )
    ready = all((OUT / str(i) / f"{i}.epub").exists() for i in (4, 5))
    print(f"同步化后: 调用方拿到结果时文件已就绪 = {ready}")
    print(f"总耗时 {time.perf_counter() - t0:.2f}s (并发仍在,代价是每篇要等最慢环节)")
    dl.shutdown()


# ---------------------------------------------------------------------------
# 第 4 幕:关键细节 —— await_all 为什么不能 f.result()
# ---------------------------------------------------------------------------

async def act4_do_not_block() -> None:
    print("对比:两个 0.5s 任务,等待期间事件循环还能不能转?")
    print("(心跳每 0.05s 跳一次,共 6 次 —— 阻塞会让心跳出现大空洞)\n")

    async def heartbeat(ticks: list[str]) -> None:
        t0 = time.perf_counter()
        while len(ticks) < 6:
            ticks.append(f"{time.perf_counter() - t0:.2f}")
            await asyncio.sleep(0.05)

    # 错误示范:f.result() 是同步阻塞 —— 事件循环被卡死
    dl = MicroImageDownloader()
    dl.process_novel_assets(make_novel(6, work=0.5))
    dl.process_novel_assets(make_novel(7, work=0.5))

    ticks1: list[str] = []

    async def wrong_wait() -> None:
        await asyncio.sleep(0.15)          # 让心跳先跳三下
        for f in dl._futures:
            f.result()                     # ← 阻塞 0.35s,心跳停摆

    await asyncio.gather(heartbeat(ticks1), wrong_wait())
    print(f"f.result() 版本心跳时刻: {ticks1}")
    print(f"             ↑ 0.15s 之后出现约 0.35s 空洞(事件循环被卡住)\n")

    # 正确示范:asyncio.wrap_future —— await 不阻塞
    dl2 = MicroDownloaderWithWait()
    dl2.process_novel_assets(make_novel(8, work=0.5))
    dl2.process_novel_assets(make_novel(9, work=0.5))

    ticks2: list[str] = []

    async def right_wait() -> None:
        await asyncio.sleep(0.15)
        await dl2.await_all()

    await asyncio.gather(heartbeat(ticks2), right_wait())
    print(f"wrap_future 版本心跳时刻: {ticks2}")
    print("             ↑ 全程均匀,等待期间事件循环照常转")
    dl.shutdown()
    dl2.shutdown()


async def main() -> None:
    sep("第 1 幕:现状 fire-and-forget —— 竞态")
    act1_fire_and_forget()

    sep("第 2 幕:方案 A —— await_all() 等待接口")
    await act2_await_all()

    sep("第 3 幕:方案 B —— 同步化")
    await act3_sync()

    sep("第 4 幕:关键细节 —— await_all 不能 f.result()")
    await act4_do_not_block()

    sep("第 5 幕:三种模式对比")
    print("""
                     竞态    persist 前文件就绪   批量并发   事件循环安全
  现状 fire-and-forget  有       ❌(要等)           ✅         ✅
  方案 A await_all()    无(关卡)  ✅(等完才走)       ✅         ✅(wrap_future)
  方案 B 同步化         无       ✅                ✅         ✅(to_thread)

  方案 A 的特点:process 仍然 fire-and-forget(下载期间不等),
              但在 persist 前加一道「关卡」等所有 in-flight 完成。
  方案 B 的特点:每篇都要等自己的图片+EPUB 全部完成才返回,
              批量场景总耗时 = 最慢的一篇(并发仍保留)。
""")


if __name__ == "__main__":
    asyncio.run(main())
