import asyncio
import time
import os, json
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass

from core.config import config
from core.logger import logger
from core.pixiv_account import PixivAccount, TokenInfo, AccountStrategy
from core.util import RateLimitError, AccountInvalidError, RequestInfo

class AccountManager:

    def __init__(self, accounts: List[PixivAccount]): self.accounts = accounts
    
    def _available_accounts(self, strategy: AccountStrategy = None):
        if strategy is None:
            strategy = AccountStrategy()
        elif acc_name := strategy.force_account: 
            if acc := [a for a in self.accounts if a.username == acc_name]:
                return acc
        
        candidates = [
            a for a in self.accounts if a.available
            and (strategy.allow_special or not a.token_info.special)
            and (not strategy.need_premium or a.token_info.premium)
        ]
        return candidates

    def select(self, strategy: AccountStrategy = None) -> Optional[PixivAccount]:
        available = self._available_accounts(strategy)
        if not available:
            raise Exception(f'Without available account with strategy {strategy}')
        return min(available, key=lambda a: a.last_req_time)

class RequestManager:
    
    def __init__(self, config: dict = config):

        accounts = [
            PixivAccount(token_info)
            for token_info in self._load_tokens(config)
        ]
        if not accounts:
            raise ValueError('Without valid account.')
        self.accounts = AccountManager(accounts)

        cfg = config.get('pixiv_client', {})
        self.cooling_duration = cfg.get("cooling_duration", 120)
        self.max_workers = cfg.get("max_concurrency", 5)
        self.min_interval = cfg.get("min_interval", 2.0)

        self.queue = asyncio.Queue()
        self.workers: List[asyncio.Task] = []
        self.running = False
        
        self._queue_lock = asyncio.Lock()
        self._worker_lock = asyncio.Lock()
    
    def _load_tokens(self, config):
        path_cfg = config.get("path", {}).get("token")
        if not path_cfg: 
            logger.warning(f'Without valid token.')
            return
        
        try:
            tokens = list()
            with open(path_cfg, "r", encoding="utf-8") as f:
                for name, acc in json.load(f).items():
                    if acc.get("token"):
                        tokens.append(TokenInfo(
                            token=acc["token"],
                            username=name,
                            special=acc.get("special", False),
                            premium=acc.get("premium", False)
                        ))
            logger.info(f"Loaded {len(tokens)} tokens")
        except Exception as e:
            logger.error(f"Error loading tokens: {e}")
            raise

        return tokens
        
    async def add_task(self, request: RequestInfo) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request.future = future

        async with self._queue_lock:
            await self.queue.put(request)

        if not self.running:
            await self._start_workers()

        return future
    
    async def _start_workers(self):
        if self.running:
            return
        
        self.running = True
        async with self._worker_lock:
            self.workers = [
                asyncio.create_task(self._worker())
                for i in range(self.max_workers)
            ]
    
    async def stop(self):
        self.running = False

        async with self._worker_lock:
            for worker in self.workers:
                worker.cancel()
            
            if self.workers:
                await asyncio.gather(*self.workers, return_exceptions=True)
                self.workers.clear()

        async with self._queue_lock:
            while not self.queue.empty():
                try:
                    task = self.queue.get_nowait()
                    if not task.future.done():
                        task.future.set_exception(
                            asyncio.CancelledError("Service stopped")
                        )
                    self.queue.task_done()
                except asyncio.QueueEmpty:
                    break
    
    async def _worker(self):
        while self.running:
            try:
                task: RequestInfo = await asyncio.wait_for(self.queue.get(), timeout=1) 
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                async with self._worker_lock:
                    acc = self.accounts.select(task.strategy)
                    elapsed = time.time() - acc.last_req_time
                    if elapsed < self.min_interval:
                        await asyncio.sleep(self.min_interval - elapsed)
                    acc.last_req_time = time.time()

                logger.info(f"{acc} requesting - {task}")
                result = await acc.execute(
                    task.func.__name__, *task.args, **task.kwargs
                )
                if not task.future.done():
                    task.future.set_result(result)
            except RateLimitError:
                logger.warning(f"{acc} reach rate limit, cooling down and switching...")
                acc.start_cooldown(self.cooling_duration)
                task.account = self.accounts.select(task.strategy)
                await self.queue.put(task)
                self.queue.task_done()
                continue
            except AccountInvalidError:
                logger.error(f"{acc} invalid, switching...")
                task.account = self.accounts.select(task.strategy)
                await self.queue.put(task)
                self.queue.task_done()
                continue
            except Exception as e:
                if not task.future.done():
                    task.future.set_exception(e)
            finally:
                if task in self.queue._queue or self.queue._unfinished_tasks > 0:
                    try:
                        self.queue.task_done()
                    except ValueError:
                        pass