
import asyncio
import time, os, json
import contextvars
from datetime import datetime
from dateutil import parser as date_parser
from typing import Callable, List, Any, Coroutine, TypeVar, Optional
from functools import wraps
from dataclasses import dataclass
from contextlib import asynccontextmanager

from pixivpy3 import AppPixivAPI, PixivError
from pixivpy3.aapi import _MODE, _FILTER, DateOrStr
from pixivpy3.utils import ParsedJson

from core.config import config
from core.logger import logger
from core.pixiv_patch import *  # Apply monkey patch to models

T = TypeVar("T")

def retry_async(retries: int = 3, backoff_factor: float = 2.0, exceptions: tuple = (Exception,)):
    def decorator(func: Callable[..., Coroutine[Any, Any, T]]):
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None
            for attempt in range(retries):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt + 1 == retries:
                        break
                    wait_time = backoff_factor * (2 ** attempt)
                    logger.warning(f"[Retry] {func.__name__} 失败 ({attempt + 1}/{retries}): {e}. {wait_time}s 后重试...")
                    await asyncio.sleep(wait_time)
            raise last_exception or Exception("Unknown Error")

        return wrapper

    return decorator

@dataclass
class AccountStrategy:
    need_premium: bool = False
    allow_special: bool = False

@dataclass
class RequestInfo:
    func: Callable
    args: tuple
    kwargs: dict
    future: asyncio.Future

    def __str__(self):
        # 跳过第一个参数（永远是 PixivAccount 对象）
        real_args = self.args[1:] if len(self.args) > 1 else self.args
        args_str = ', '.join(map(str, real_args))
        
        kwargs_str = ', '.join(f"{k}={v}" for k, v in self.kwargs.items())
        
        # 拼接参数（处理 args + kwargs 的逗号）
        if args_str and kwargs_str:
            param_str = f"{args_str}, {kwargs_str}"
        else:
            param_str = args_str or kwargs_str
        
        return f"{self.func.__name__}({param_str})" if param_str else f"{self.func.__name__}()"

class RequestQueue:
    def __init__(self, config: dict, refresh_token: str):
        self.refresh_token = refresh_token
        self.queue = asyncio.Queue()

        self.running = False
        self.workers: List[asyncio.Task] = []
        self.max_concurrency = config.get('pixiv_client', {}).get("max_concurrency", 2)

        self._last_request_time = 0.0
        self._lock = asyncio.Lock()
        self.min_interval = config.get('pixiv_client', {}).get("min_interval", 1.5)

        self._cooling_until = 0.0
        self.cooling_duration = config.get('pixiv_client', {}).get("cooling_duration", 120)
        self._processing_count = 0

    async def add_task(self, func: Callable, *args, **kwargs) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        task = RequestInfo(func, args, kwargs, future)
        await self.queue.put(task)
        if not self.running:
            await self._start_workers()
        return future

    async def _start_workers(self):
        if self.running:
            return
        self.running = True
        self.workers = [asyncio.create_task(self._worker(i)) for i in range(self.max_concurrency)]

    async def _worker(self, worker_id: int):
        while self.running:
            try:
                # 检查全局冷却
                current_time = time.time()
                if current_time < self._cooling_until:
                    await asyncio.sleep(1.0)
                    continue
                task = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            self._processing_count += 1
            try:
                # 频率限制
                async with self._lock:
                    elapsed = time.time() - self._last_request_time
                    if elapsed < self.min_interval:
                        await asyncio.sleep(self.min_interval - elapsed)
                    self._last_request_time = time.time()

                # 执行任务 (此处的 task.func 已经是带重试逻辑的异步函数)
                logger.info(f"{self.refresh_token[:4]}... requesting - {task}")
                result = await task.func(*task.args, **task.kwargs)

                # 判定 Rate Limit
                if isinstance(result, dict) and "Rate Limit" in str(result.get("error", {}).get("message", "")):
                    self._cooling_until = time.time() + self.cooling_duration
                    logger.warning(f"!!! [Account Rate Limit] 触发冷却 {self.cooling_duration}s !!!")
                    await self.queue.put(task)  # 重新放回
                    continue

                if not task.future.done():
                    task.future.set_result(result)
            except Exception as e:
                if not task.future.done():
                    task.future.set_exception(e)
            finally:
                self.queue.task_done()
                self._processing_count -= 1

@dataclass
class TokenInfo:
    token: str
    special: bool = False
    premium: bool = False

class TokenManager:
    def __init__(self, config: dict):
        self.config = config
        self._tokens: List[TokenInfo] = []
        self.load()

    def load(self):
        path_config = self.config.get("path", {})
        if not path_config:
            return
            
        token_path = path_config.get("token")
        if token_path:
            if not os.path.isabs(token_path):
                # Resolve relative to config.yaml location (which is config.config_path)
                # Assuming config.yaml is in root, token path is relative to root
                base_dir = os.path.dirname(os.path.abspath(self.config.config_path))
                token_path = os.path.join(base_dir, token_path)
            
            if os.path.exists(token_path):
                try:
                    with open(token_path, "r", encoding="utf-8") as f:
                        tokens_data = json.load(f)
                        self._tokens = []
                        for acc in tokens_data.values():
                            if isinstance(acc, dict) and acc.get("token"):
                                self._tokens.append(TokenInfo(
                                    token=acc.get("token"),
                                    special=acc.get("special", False),
                                    premium=acc.get("premium", False)
                                ))
                except Exception as e:
                    logger.error(f"Error loading tokens from {token_path}: {e}")
            else:
                logger.warning(f"Warning: Token file {token_path} not found.")

    @property
    def tokens(self) -> List[TokenInfo]:
        return self._tokens

class PixivAccount:
    def __init__(self, token_info: TokenInfo, config: dict):
        self.api = AppPixivAPI()
        self.token_info = token_info
        self.queue = RequestQueue(config, self.token_info.token)
        self.is_auth = False

    async def authenticate(self):
        if not self.is_auth:
            await asyncio.to_thread(self.api.auth, refresh_token=self.token_info.token)
            self.is_auth = True

    def __str__(self):
        return f"PixivAccount({self.token_info.token[:4]}...)"

class PixivClient:
    def __init__(self, config_override: dict = None):
        self.config = config_override or config
        self.tokens  = TokenManager(config).tokens
        
        if not self.tokens:
            raise ValueError("Check config for missing item: path.token or check your token file")
            
        self.accounts: List[PixivAccount] = [
            PixivAccount(tk_info, self.config) for tk_info in self.tokens
        ]

        # 上下文变量：实现真正隔离的 force_account（每个协程独立）
        self._forced_account_var: contextvars.ContextVar[Optional[PixivAccount]] = \
            contextvars.ContextVar('pixiv_forced_account', default=None)

        # 策略变量
        self._strategy_var: contextvars.ContextVar[AccountStrategy] = \
            contextvars.ContextVar('pixiv_account_strategy', default=AccountStrategy())

        # 默认的全局代理（自动调度）
        self.call = self._AsyncProxy(self)

    @asynccontextmanager
    async def force_account(self, index: int):
        """
        async with client.force_account(0):
            await client.illust_detail(123)
        """
        if not (0 <= index < len(self.accounts)):
            raise IndexError(f"账号索引 {index} 超出范围")

        acc = self.accounts[index]
        token = self._forced_account_var.set(acc)
        try:
            yield self
        finally:
            self._forced_account_var.reset(token)

    @asynccontextmanager
    async def account_rule(self, need_premium: bool = False, allow_special: bool = False):
        """
        async with client.account_rule(need_premium=True):
            await client.illust_detail(123)
        """
        current_strategy = self._strategy_var.get()
        # Merge or override? Override is simpler and usually what's expected in a block.
        # But maybe we want to allow_special if outer block allows it?
        # Let's simple override for now, or just create new based on args.
        
        new_strategy = AccountStrategy(
            need_premium=need_premium,
            allow_special=allow_special
        )
        
        token = self._strategy_var.set(new_strategy)
        try:
            yield self
        finally:
            self._strategy_var.reset(token)

    def __getattr__(self, name: str):
        """让使用者可以直接 client.illust_detail(...) 而不用 client.call.xxx"""
        if name.startswith(('user_', 'novel_', 'illust_', 'search_', 'webview_')):
             return getattr(self.call, name)
        
        if self.accounts:
            try:
                attr = getattr(self.accounts[0].api, name)
                return attr
            except AttributeError:
                pass
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    class _AsyncProxy:
        def __init__(self, master: 'PixivClient'):
            self._master = master

        def _select_account(self) -> PixivAccount:
            # 1. force_account 上下文锁定的账号
            # 2. 智能调度 (受 strategy 影响)

            forced = self._master._forced_account_var.get()
            if forced is not None:
                return forced

            strategy = self._master._strategy_var.get()
            
            # Filter candidates based on strategy
            candidates = []
            for acc in self._master.accounts:
                # Rule 1: Special check
                if acc.token_info.special and not strategy.allow_special:
                    continue
                
                # Rule 2: Premium check
                if strategy.need_premium and not acc.token_info.premium:
                    continue
                    
                candidates.append(acc)

            if not candidates:
                raise RuntimeError(f"No accounts available satisfying strategy: {strategy}")

            now = time.time()
            available = [a for a in candidates if now > a.queue._cooling_until]
            if not available:
                return min(candidates, key=lambda a: a.queue._cooling_until)
            # minimum by (queue length, last request time)
            return min(available, key=lambda a: (
                a.queue.queue.qsize() + a.queue._processing_count, 
                a.queue._last_request_time
            ))
        
        def __getattr__(self, name):
            retry_cfg = self._master.config.get("retry", {"count": 3, "backoff": 2.0})
            @retry_async(retries=retry_cfg.get("count"), backoff_factor=retry_cfg.get("backoff"))
            async def wrapped_execution(acc: PixivAccount, *args, **kwargs):
                await acc.authenticate()
                original_func = getattr(acc.api, name)
                try:
                    result = await asyncio.to_thread(original_func, *args, **kwargs)
                except PixivError as e:
                    # Allow PixivError to propagate so _worker can handle Rate Limit checks
                    raise e
                except Exception as e:
                    raise RuntimeError(f"Error executing {name} with account {acc}: {e}") from e
                return result

            # for clearly logging
            wrapped_execution.__name__ = name
            wrapped_execution.__qualname__ = f"wrapped_{name}"

            async def wrapper(*args, **kwargs):
                fetch_all: bool = kwargs.pop('fetch_all', False)
                fetch_til: datetime = kwargs.pop('fetch_til', None)
                fetch_minlike: int = kwargs.pop('fetch_minlike', 0)

                acc = self._select_account()
                future = await acc.queue.add_task(wrapped_execution, acc, *args, **kwargs)
                result = await future

                auto_fetch = fetch_til or fetch_minlike or fetch_all
                
                while auto_fetch and result.next_url:
                    next_qs = acc.api.parse_qs(result.next_url)
                    acc = self._select_account()

                    future = await acc.queue.add_task(wrapped_execution, acc, **next_qs)
                    next_result = await future

                    result.novels.extend(next_result.novels)
                    result.next_url = next_result.next_url

                    if fetch_til:
                        sample = next_result.novels[-1]
                        target_date = sample.get('create_date')
                        if date_parser.parse(target_date) < fetch_til:
                            break
                    
                    if fetch_minlike:
                        sample = next_result.novels[-1]
                        like = sample.get('total_bookmarks')
                        if like < fetch_minlike:
                            break

                return result

            return wrapper
        
