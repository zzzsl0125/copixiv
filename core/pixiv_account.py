import asyncio
from typing import Any
from enum import Enum

from pixivpy3 import AppPixivAPI, PixivError

from core.config import config
from core.logger import logger
from core.request_queue import RequestQueue
from core.pixiv_token import TokenInfo
from core.util import RateLimitError, AccountInvalidError

class AccountStatus(Enum):
    """账号状态"""
    INACTIVE = "inactive"       # 待认证
    ACTIVE = "active"           # 工作中
    INVALID = "invalid"         # 已失效

class PixivAccount:
    """单个Pixiv账号"""
    
    def __init__(self, token_info: TokenInfo, config: dict = config):
        self.token_info = token_info
        self.api = self._create_api()
        self.queue = RequestQueue(self.id, config)
        self.status = AccountStatus.INACTIVE
        self._auth_lock = asyncio.Lock()
        
    def _create_api(self) -> AppPixivAPI:
        """创建并配置API实例"""
        api = AppPixivAPI()
        
        def _custom_load_result(res, *args, **kwargs):
            return api.parse_result(res)
        api._load_result = _custom_load_result
        
        def _custom_load_model(cls, data, *args, **kwargs):
            return data
        api.__class__._load_model = classmethod(_custom_load_model)
        
        def _novel_ranking(
            api_self, mode="day_r18", filter="for_ios",
            date=None, offset=None, req_auth=True,
        ):
            url = f"{api_self.hosts}/v1/novel/ranking"
            params = {"mode": mode, "filter": filter}
            if date:
                params["date"] = api_self._format_date(date)
            if offset:
                params["offset"] = offset
            r = api_self.no_auth_requests_call(
                "GET", url, params=params, req_auth=req_auth
            )
            return api_self.parse_result(r)
        
        api.novel_ranking = _novel_ranking
        return api
    
    @property
    def id(self) -> str:
        """账号标识"""
        mark = "[*]" if self.token_info.special else ""
        return f"Account {mark}{self.token_info.token[:4]}..."
    
    def __str__(self): return self.id
    
    @property
    def is_valid(self) -> bool:
        """账号是否有效"""
        return self.status != AccountStatus.INVALID
    
    @property
    def is_available(self) -> bool:
        """账号当前是否可用（有效且不在冷却）"""
        return self.is_valid and not self.queue.is_in_cooldown
    
    async def authenticate(self):
        """认证账号"""
        if self.status == AccountStatus.INVALID:
            raise AccountInvalidError(self.id)
            
        if self.status == AccountStatus.ACTIVE:
            return
        
        async with self._auth_lock:
            try:
                await asyncio.to_thread(
                    self.api.auth, 
                    refresh_token=self.token_info.token
                )
                self.status = AccountStatus.ACTIVE
                logger.info(f"{self.id} 认证成功")
            except PixivError as e:
                if e.reason.startswith('[ERROR] auth() failed'):
                    self.status = AccountStatus.INVALID
                    raise AccountInvalidError(self.id)
                raise
    
    async def execute(self, method: str, *args, **kwargs) -> Any:
        """执行API调用"""
        await self.authenticate()
        
        async def _call(*args, **kwargs):
            try:
                return await asyncio.to_thread(
                    getattr(self.api, method), 
                    *args, **kwargs
                )
            except PixivError as e:
                error_msg = str(e).lower()
                if "invalid_grant" in error_msg:
                    self.status = AccountStatus.INVALID
                    raise AccountInvalidError(self.id) from e
                
                if "rate limit" in error_msg or \
                    "currently restricted" in error_msg:
                    raise RateLimitError(self.id) from e
                
                raise

        _call.__name__ = method
        
        future = await self.queue.add_task(_call, *args, **kwargs)
        return await future

