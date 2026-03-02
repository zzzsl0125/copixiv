import asyncio
import contextvars
from datetime import datetime
from dateutil import parser as date_parser
from typing import Callable, List, Any, Coroutine, TypeVar, Optional
from functools import wraps
from contextlib import asynccontextmanager
from dataclasses import dataclass

from pixivpy3 import PixivError

from core.config import config
from core.logger import logger
from core.pixiv_account import PixivAccount
from core.pixiv_token import TokenManager
from core.util import RateLimitError, AccountInvalidError

@dataclass
class AccountStrategy:
    """账号选择策略"""
    need_premium: bool = False
    allow_special: bool = False

class PixivClient:
    """Pixiv客户端主类"""
    
    _instance = None
    
    def __new__(cls, config=None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, config=config):
        if getattr(self, '_initialized', False):
            return
            
        self._initialized = True
        self.config = config
        
        self.accounts = [
            PixivAccount(token, self.config) 
            for token in TokenManager(self.config).tokens
        ]
        if not self.accounts:
            raise ValueError("No valid accounts loaded")
        
        self._forced_account = contextvars.ContextVar('forced_account', default=None)
        self._strategy = contextvars.ContextVar('strategy', default=AccountStrategy())
        
        self._proxy = _Proxy(self)
        
    def __getattr__(self, name):
        """代理到_Proxy"""
        if hasattr(self._proxy, name):
            return getattr(self._proxy, name)
        raise AttributeError(f"'PixivClient' object has no attribute '{name}'")
    
    @asynccontextmanager
    async def force_account(self, index: int):
        """强制使用指定账号"""
        if not 0 <= index < len(self.accounts):
            raise IndexError(f"Account index {index} out of range [0, {len(self.accounts)-1}]")
            
        token = self._forced_account.set(self.accounts[index])
        try:
            yield self
        finally:
            self._forced_account.reset(token)
    
    @asynccontextmanager
    async def account_rule(self, need_premium=False, allow_special=False):
        """设置账号选择规则"""
        token = self._strategy.set(AccountStrategy(need_premium, allow_special))
        try:
            yield self
        finally:
            self._strategy.reset(token)
    
    def select_account(self) -> PixivAccount:
        """根据策略选择一个合适的账号"""
        
        forced: PixivAccount = self._forced_account.get()
        if forced and forced.is_valid:
            return forced
        
        strategy = self._strategy.get()
        
        candidates = [
            a for a in self.accounts 
            if a.is_valid 
            and (strategy.allow_special or not a.token_info.special)
            and (not strategy.need_premium or a.token_info.premium)
        ]
        
        if not candidates:
            raise Exception(
                f"No suitable account available "
                f"(need_premium={strategy.need_premium}, "
                f"allow_special={strategy.allow_special})"
            )
        
        available = [a for a in candidates if a.is_available]
        if available:
            # 选择队列最短且最近请求时间最早的
            return min(
                available, 
                key=lambda a: (a.queue.queue_size, a.queue._last_req_time)
            )
        
        # 所有账号都在冷却，选择最快结束冷却的
        return min(candidates, key=lambda a: a.queue.cooldown_remaining)
    
    async def execute(self, method: str, *args, **kwargs) -> Any:
        """执行API调用及其衍生调用"""
        fetch_all = kwargs.pop('fetch_all', False)
        fetch_til = kwargs.pop('fetch_til', None)
        fetch_minlike = kwargs.pop('fetch_minlike', 0)
        
        
        result = await self._execute(method, args, kwargs)
        
        if fetch_all or fetch_til or fetch_minlike:
            result = await self._auto_fetch(
                method, result, fetch_til, fetch_minlike
            )
            
        return result
    
    async def _execute(
            self, method: str, args: tuple, kwargs: dict
    ) -> Any:
        """执行API调用，失败时自动切换账号"""
        retry_cfg = self.config.get("retry", {"count": 3, "backoff": 2})

        count = 0
        while count < retry_cfg["count"]:
            try:
                account = self.select_account()
                return await account.execute(method, *args, **kwargs)
                
            except AccountInvalidError as e:
                logger.warning(f"{e.reason} 无效, 尝试切换")
                continue
                
            except RateLimitError as e:
                logger.warning(f"{e.reason} 被限流, 尝试切换")
                continue
                
            except PixivError as e:
                error_msg = str(e).lower()
                if "404" in error_msg or "not found" in error_msg:
                    raise
                
                count += 1
                wait = retry_cfg["backoff"] * (2 ** count)
                logger.warning(f"Pixiv错误: {e}")
                logger.warning(f'等待 {wait}s 后重试 [{count}/{retry_cfg["count"]}]')
                await asyncio.sleep(wait)
                continue
                
        raise Exception(f"Failed to execute {method}(args={args}, kwargs={kwargs})")
    
    async def _auto_fetch(
            self, method: str, first_result: Any, 
            fetch_til: Optional[datetime], fetch_minlike: int
    ) -> Any:
        """处理衍生请求"""
        result = first_result
        
        while result.next_url:
            next_qs = self.accounts[0].api.parse_qs(result.next_url)
            next_result = await self._execute(method, (), next_qs)
            
            result.novels += next_result.novels
            result.next_url = next_result.next_url
            
            if next_result.novels:
                last_item = next_result.novels[-1]
                
                if fetch_til and 'create_date' in last_item:
                    item_date = date_parser.parse(last_item['create_date'])
                    if item_date < fetch_til:
                        break
                        
                if fetch_minlike and 'total_bookmarks' in last_item:
                    if last_item['total_bookmarks'] < fetch_minlike:
                        break

        return result

# ==================== 代理类 ====================

class _Proxy:
    """代理类，将所有API方法转发到client.execute"""
    
    def __init__(self, client: PixivClient):
        self._client = client
    
    def __getattr__(self, name):
        """为每个API方法生成包装器"""
        
        async def wrapper(*args, **kwargs):
            return await self._client.execute(name, *args, **kwargs)
        
        wrapper.__name__ = name
        wrapper.__doc__ = f"Execute pixiv API: {name}"
        
        return wrapper