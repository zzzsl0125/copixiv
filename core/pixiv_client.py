import asyncio
import time
import os
import json
import contextvars
from datetime import datetime
from dateutil import parser as date_parser
from typing import Callable, List, Any, Coroutine, TypeVar, Optional
from functools import wraps
from dataclasses import dataclass
from contextlib import asynccontextmanager

from pixivpy3 import AppPixivAPI, PixivError

from core.config import config
from core.logger import logger

T = TypeVar("T")

def retry_async(retries=3, backoff=2.0, exceptions=(Exception,)):
    """异步函数重试装饰器，遇到特定错误直接抛出"""
    def decorator(func: Callable[..., Coroutine[Any, Any, T]]):
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exc = None
            for attempt in range(retries):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if "permanently invalid" in str(e) or "Page not found" in str(e):
                        raise
                    if attempt + 1 == retries:
                        break
                    wait = backoff * (2 ** attempt)
                    logger.warning(f"[Retry] {func.__name__} 失败 ({attempt+1}/{retries}): {e}. {wait}s后重试")
                    await asyncio.sleep(wait)
            raise last_exc or Exception("Unknown error")
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
        real_args = self.args[1:] if len(self.args) > 1 else self.args
        args_str = ', '.join(map(str, real_args))
        kwargs_str = ', '.join(f"{k}={v}" for k, v in self.kwargs.items())
        param = f"{args_str}, {kwargs_str}" if args_str and kwargs_str else args_str or kwargs_str
        return f"{self.func.__name__}({param})" if param else f"{self.func.__name__}()"

class RequestQueue:
    """每个账号的请求队列，控制并发和频率"""
    def __init__(self, refresh_token: str, config: dict):
        self.token = refresh_token
        self.queue = asyncio.Queue()
        self.running = False
        self.workers: List[asyncio.Task] = []
        cfg = config.get('pixiv_client', {})
        self.max_workers = cfg.get("max_concurrency", 2)
        self.min_interval = cfg.get("min_interval", 1.5)
        self.cooling_duration = cfg.get("cooling_duration", 120)
        self._last_req = 0.0
        self._cool_until = 0.0
        self._lock = asyncio.Lock()

    async def add_task(self, func: Callable, *args, **kwargs) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self.queue.put(RequestInfo(func, args, kwargs, future))
        if not self.running:
            await self._start()
        return future

    async def _start(self):
        if self.running:
            return
        self.running = True
        self.workers = [asyncio.create_task(self._worker(i)) for i in range(self.max_workers)]

    async def _worker(self, wid):
        while self.running:
            if time.time() < self._cool_until:
                await asyncio.sleep(1)
                continue
            try:
                task = await asyncio.wait_for(self.queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            async with self._lock:
                elapsed = time.time() - self._last_req
                if elapsed < self.min_interval:
                    await asyncio.sleep(self.min_interval - elapsed)
                self._last_req = time.time()

            try:
                logger.info(f"{self.token[:4]}... requesting - {task}")
                result = await task.func(*task.args, **task.kwargs)
                if isinstance(result, dict) and "Rate Limit" in result.get("error", {}).get("message", ""):
                    self._cool_until = time.time() + self.cooling_duration
                    logger.warning(f"!!! [Account Rate Limit] 触发冷却 {self.cooling_duration}s !!!")
                    await self.queue.put(task)  # 重新排队
                    continue
                if not task.future.done():
                    task.future.set_result(result)
            except Exception as e:
                if not task.future.done():
                    task.future.set_exception(e)
            finally:
                self.queue.task_done()

@dataclass
class TokenInfo:
    token: str
    special: bool = False
    premium: bool = False

class TokenManager:
    def __init__(self, config: dict):
        self.config = config
        self.tokens: List[TokenInfo] = []
        self._load()

    def _load(self):
        path_cfg = self.config.get("path", {}).get("token")
        if not path_cfg:
            return
        if not os.path.isabs(path_cfg):
            base = os.path.dirname(os.path.abspath(self.config.config_path))
            path_cfg = os.path.join(base, path_cfg)
        try:
            with open(path_cfg, "r", encoding="utf-8") as f:
                for acc in json.load(f).values():
                    if acc.get("token"):
                        self.tokens.append(TokenInfo(
                            token=acc["token"],
                            special=acc.get("special", False),
                            premium=acc.get("premium", False)
                        ))
        except Exception as e:
            logger.error(f"Error loading tokens: {e}")

class PixivAccount:
    def __init__(self, token_info: TokenInfo, config: dict):
        self.api = AppPixivAPI()
        self.token_info = token_info
        self.queue = RequestQueue(token_info.token, config)
        self.is_auth = False
        self.is_valid = True

        def _custom_load_result(res, *args, **kwargs):
            return self.api.parse_result(res)
        self.api._load_result = _custom_load_result

        def _custom_load_model(cls, data, *args, **kwargs):
            return data
        self.api.__class__._load_model = classmethod(_custom_load_model)

        def _novel_ranking(
            api_self,
            mode = "day_r18",
            filter = "for_ios",
            date = None,
            offset = None,
            req_auth = True,
        ):
            url = f"{api_self.hosts}/v1/novel/ranking"
            params = {
                "mode": mode,
                "filter": filter,
            }
            if date:
                params["date"] = api_self._format_date(date)
            if offset:
                params["offset"] = offset
            r = api_self.no_auth_requests_call("GET", url, params=params, req_auth=req_auth)
            return api_self.parse_result(r)
        
        self.api.novel_ranking = _novel_ranking

    async def authenticate(self):
        if not self.is_valid:
            raise Exception(f"Account {self} is permanently invalid.")
        if not self.is_auth:
            try:
                await asyncio.to_thread(self.api.auth, refresh_token=self.token_info.token)
                self.is_auth = True
            except Exception as e:
                if "invalid_grant" in str(e).lower() or "oauth" in str(e).lower():
                    self.is_valid = False
                    logger.error(f"Account {self} auth failed permanently: {e}")
                raise e

    def __str__(self):
        return f"PixivAccount({self.token_info.token[:4]}...)"

class PixivClient:
    _instance = None

    def __new__(cls, config_override=None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, config_override=None):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True
        self.config = config_override or config
        self.tokens = TokenManager(self.config).tokens
        if not self.tokens:
            raise ValueError("No tokens loaded")
        self.accounts = [PixivAccount(t, self.config) for t in self.tokens]
        if not self.accounts:
            raise Exception("No valid accounts")
        self._forced = contextvars.ContextVar('forced_account', default=None)
        self._strategy = contextvars.ContextVar('strategy', default=AccountStrategy())
        # 动态代理所有Pixiv API方法
        self._proxy = _Proxy(self)

    def __getattr__(self, name):
        # 优先返回代理方法，否则返回第一个账号的原始API属性（极少用）
        if hasattr(self._proxy, name):
            return getattr(self._proxy, name)
        return getattr(self.accounts[0].api, name)

    @asynccontextmanager
    async def force_account(self, index: int):
        if not 0 <= index < len(self.accounts):
            raise IndexError("Account index out of range")
        token = self._forced.set(self.accounts[index])
        try:
            yield self
        finally:
            self._forced.reset(token)

    @asynccontextmanager
    async def account_rule(self, need_premium=False, allow_special=False):
        token = self._strategy.set(AccountStrategy(need_premium, allow_special))
        try:
            yield self
        finally:
            self._strategy.reset(token)

class _Proxy:
    """代理类，处理API调用、账号选择和自动翻页"""
    def __init__(self, client: PixivClient):
        self._client = client

    def _select_account(self) -> PixivAccount:
        forced = self._client._forced.get()
        if forced and forced.is_valid:
            return forced

        strategy = self._client._strategy.get()
        candidates = [a for a in self._client.accounts if a.is_valid and
                      (strategy.allow_special or not a.token_info.special) and
                      (not strategy.need_premium or a.token_info.premium)]
        if not candidates:
            raise RuntimeError("No suitable account available")

        now = time.time()
        available = [a for a in candidates if now > a.queue._cool_until]
        if not available:
            return min(candidates, key=lambda a: a.queue._cool_until)
        # 选择队列任务数最少且最近请求时间最早的
        return min(available, key=lambda a: (a.queue.queue.qsize(), a.queue._last_req))

    async def _execute(self, acc: PixivAccount, name: str, *args, **kwargs):
        await acc.authenticate()
        
        async def _do_api():
            try:
                return await asyncio.to_thread(getattr(acc.api, name), *args, **kwargs)
            except PixivError as e:
                if "Rate Limit" in str(e) or "currently restricted" in str(e):
                    return {"error": {"message": "Rate Limit"}}
                raise
        _do_api.__name__ = name
        
        is_api = name.startswith(('user_', 'novel_', 'illust_', 'search_', 'webview_'))
        if is_api:
            retry_cfg = self._client.config.get("retry", {"count": 3, "backoff": 2.0})
            _do_api = retry_async(retries=retry_cfg["count"], backoff=retry_cfg["backoff"])(_do_api)
            
        return await _do_api()

    def __getattr__(self, name):
        # 为每个API方法生成包装器
        execute = self._execute

        async def wrapper(*args, **kwargs):
            fetch_all = kwargs.pop('fetch_all', False)
            fetch_til = kwargs.pop('fetch_til', None)
            fetch_minlike = kwargs.pop('fetch_minlike', 0)
            
            result = await self._call_with_fallback(execute, name, args, kwargs)
            if not (fetch_all or fetch_til or fetch_minlike):
                return result
            
            return await self._auto_fetch(execute, name, result, fetch_til, fetch_minlike)

        wrapper.__name__ = name
        return wrapper

    async def _call_with_fallback(self, execute, name, args, kwargs):
        """调用一个账号，失败时自动换号"""
        while True:
            acc = self._select_account()
            future = await acc.queue.add_task(execute, acc, name, *args, **kwargs)
            try:
                return await future
            except Exception as e:
                if "permanently invalid" in str(e):
                    logger.warning(f"Account {acc} invalid, switching")
                    continue
                raise

    async def _auto_fetch(self, execute, name, result, fetch_til, fetch_minlike):
        """自动获取后续页面"""
        
        while result.next_url:
            next_qs = self._client.accounts[0].api.parse_qs(result.next_url)  # 随便用一个账号解析
            next_result = await self._call_with_fallback(execute, name, (), next_qs)

            result.novels += next_result.novels
            result.next_url = next_result.next_url

            if fetch_til and next_result.novels:
                last_item = next_result.novels[-1]
                if date_parser.parse(last_item.get('create_date')) < fetch_til:
                    break
            if fetch_minlike and next_result.novels:
                last_item = next_result.novels[-1]
                if last_item.get('total_bookmarks') < fetch_minlike:
                    break
        return result