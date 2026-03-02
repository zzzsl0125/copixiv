import asyncio
import time
from typing import Callable, List, TypeVar
from dataclasses import dataclass

from pixivpy3 import PixivError

from core.config import config
from core.logger import logger

T = TypeVar("T")

@dataclass
class RequestInfo:
    """请求信息"""
    func: Callable
    args: tuple
    kwargs: dict
    future: asyncio.Future

    def __str__(self):
        args_str = ', '.join(map(str, self.args))
        kwargs_str = ', '.join(f"{k}={v}" for k, v in self.kwargs.items())
        split = ', ' if self.args and self.kwargs else ''
        return f"{self.func.__name__}({args_str}{split}{kwargs_str})"

class RequestQueue:
    """每个账号的请求队列，控制并发和频率"""
    
    def __init__(self, acc_id: str, config: dict = config):
        self.id = acc_id

        self.queue = asyncio.Queue()
        self.workers: List[asyncio.Task] = []
        self.running = False
        
        cfg = config.get('pixiv_client', {})
        self.max_workers = cfg.get("max_concurrency", 2)
        self.min_interval = cfg.get("min_interval", 2)
        self.cooling_duration = cfg.get("cooling_duration", 120)

        self._last_req_time = 0.0
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()
        
    @property
    def is_in_cooldown(self) -> bool:
        """是否在冷却中"""
        return time.time() < self._cooldown_until
    
    @property
    def cooldown_remaining(self) -> float:
        """剩余冷却时间"""
        return max(0, self._cooldown_until - time.time())
    
    @property
    def queue_size(self) -> int:
        """队列大小"""
        return self.queue.qsize()
    
    def start_cooldown(self):
        """开始冷却"""
        self._cooldown_until = time.time() + self.cooling_duration
        logger.warning(f"{self.id} 触发冷却 {self.cooling_duration}s")
    
    async def add_task(self, func: Callable, *args, **kwargs) -> asyncio.Future:
        """添加任务到队列"""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self.queue.put(RequestInfo(func, args, kwargs, future))
        
        if not self.running:
            await self._start()
            
        return future
    
    async def _start(self):
        """启动工作协程"""
        if self.running:
            return
        self.running = True
        self.workers = [
            asyncio.create_task(self._worker(i)) 
            for i in range(self.max_workers)
        ]
    
    async def stop(self):
        """停止工作协程"""
        self.running = False
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
    
    async def _worker(self, worker_id: int):
        """工作协程：处理队列中的请求"""
        while self.running:
            if self.is_in_cooldown:
                await asyncio.sleep(self.cooldown_remaining)
                continue
                
            try:
                task = await asyncio.wait_for(self.queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
                
            async with self._lock:
                elapsed = time.time() - self._last_req_time
                if elapsed < self.min_interval:
                    await asyncio.sleep(self.min_interval - elapsed)
                self._last_req_time = time.time()
            
            try:
                logger.info(f"{self.id} worker-{worker_id} requesting: {task}")
                result = await task.func(*task.args, **task.kwargs)
                
                if not task.future.done():
                    task.future.set_result(result)
                    
            except PixivError as e:
                self.start_cooldown()
                    
                if not task.future.done():
                    task.future.set_exception(e)
                    
            except Exception as e:
                if not task.future.done():
                    task.future.set_exception(e)
                    
            finally:
                self.queue.task_done()
