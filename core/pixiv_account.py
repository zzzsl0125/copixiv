import asyncio
import time
from enum import Enum
from typing import Optional
from dataclasses import dataclass

from pixivpy3 import AppPixivAPI, PixivError

from core.logger import logger
from core.util import RateLimitError, AccountInvalidError

@dataclass
class TokenInfo:
    token: str
    username: str
    special: bool = False
    premium: bool = False

@dataclass
class AccountStrategy: 
    need_premium: bool = False
    allow_special: bool = False
    force_account: str = None

class AccountStatus(Enum):
    INACTIVE = "inactive"       # 待认证
    ACTIVE = "active"           # 工作中
    INVALID = "invalid"         # 已失效

class PixivAccount:
    
    def __init__(self, token_info: TokenInfo):
        self.api = self._create_api()
        self.token_info = token_info

        self.status = AccountStatus.INACTIVE
        self.token = token_info.token
        self.username = token_info.username
        
        self.last_req_time = 0.0
        self._cooldown_until = 0.0
        
        self._auth_lock = asyncio.Lock()
        self._req_lock = asyncio.Lock()

        self._auto_inactive_task: Optional[asyncio.Task] = None

    def __str__(self): return f"Account {self.username[:6]}"
    
    @property
    def in_cooldown(self): return time.time() < self._cooldown_until

    @property
    def cooldown_remaining(self): return max(0, self._cooldown_until - time.time())

    def start_cooldown(self, duration: int = 120): self._cooldown_until = time.time() + duration
    
    @property
    def valid(self): return self.status != AccountStatus.INVALID
    
    @property
    def available(self): return self.valid and not self.in_cooldown

    def set_inactive(self):
        self.status = AccountStatus.INACTIVE
        self._cancel_auto_inactive()
    
    async def _auto_inactive(self, delay: int = 3500):
        await asyncio.sleep(delay)
        if self.status == AccountStatus.ACTIVE:
            self.set_inactive()

    def _cancel_auto_inactive(self):
        if self._auto_inactive_task and \
            not self._auto_inactive_task.done():
            self._auto_inactive_task.cancel()
            self._auto_inactive_task = None
    
    def _create_api(self) -> AppPixivAPI:
        api = AppPixivAPI()
        
        def _custom_load_result(res, *args, **kwargs):
            return api.parse_result(res)
        api._load_result = _custom_load_result
        
        def _custom_load_model(cls, data, *args, **kwargs):
            return data
        api.__class__._load_model = classmethod(_custom_load_model)
        
        def _novel_ranking(
            mode="day_r18", filter="for_ios",
            date=None, offset=None, req_auth=True,
        ):
            url = f"{api.hosts}/v1/novel/ranking"
            params = {"mode": mode, "filter": filter}
            if date:
                params["date"] = api._format_date(date)
            if offset:
                params["offset"] = offset
            r = api.no_auth_requests_call(
                "GET", url, params=params, req_auth=req_auth
            )
            return api.parse_result(r)
        
        api.novel_ranking = _novel_ranking
        return api
    
    async def authenticate(self):
        if self.status == AccountStatus.INVALID:
            raise AccountInvalidError(self)
            
        if self.status == AccountStatus.ACTIVE:
            return
        
        async with self._auth_lock:
            try:
                await asyncio.to_thread(
                    self.api.auth, refresh_token=self.token
                )
                self.status = AccountStatus.ACTIVE
                logger.info(f"{self} 认证成功")

                self._cancel_auto_inactive()
                self._auto_inactive_task = asyncio.create_task(
                    self._auto_inactive()
                )

            except PixivError as e:
                if e.reason.startswith('[ERROR] auth() failed'):
                    self.status = AccountStatus.INVALID
                    raise AccountInvalidError(self)
                raise
    
    async def execute(self, method: str, *args, **kwargs):
        await self.authenticate()
        
        try:
            return await asyncio.to_thread(
                getattr(self.api, method), *args, **kwargs
            )
        except PixivError as e:
            error_msg = str(e).lower()
            if "invalid_grant" in error_msg:
                self.status = AccountStatus.INVALID
                raise AccountInvalidError(self) from e
            
            if "rate limit" in error_msg or \
                "currently restricted" in error_msg:
                raise RateLimitError(self) from e
            
            raise
