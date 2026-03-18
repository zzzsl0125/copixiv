import asyncio
import contextvars
from dateutil import parser as date_parser
from contextlib import asynccontextmanager

from core.config import config, Config
from core.util import RequestInfo
from core.pixiv_account import  AccountStrategy
from core.request_queue import RequestManager

import core.pixiv_patch

class PixivClient:
    
    _instance = None
    
    def __new__(cls, config=None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, config: Config = config):
        if getattr(self, '_initialized', False):
            return
            
        self._initialized = True
        self.config = config

        self.manager = RequestManager(config)
        
        self._strategy = contextvars.ContextVar('strategy', default=AccountStrategy())
        
        self._proxy = _Proxy(self)
        
    def __getattr__(self, name):
        if hasattr(self._proxy, name):
            return getattr(self._proxy, name)
        raise AttributeError(f"'PixivClient' object has no attribute '{name}'")
    
    @asynccontextmanager
    async def account_rule(self, need_premium=False, allow_special=False, force_account=None):
        token = self._strategy.set(AccountStrategy(need_premium, allow_special, force_account))
        try:
            yield self
        finally:
            self._strategy.reset(token)
    
    async def execute(self, method: str, *args, **kwargs) -> list:
        
        fetch_all = kwargs.pop('fetch_all', None)
        fetch_til = kwargs.pop('fetch_til', None)
        fetch_minlike = kwargs.pop('fetch_minlike', None)
        handler = kwargs.pop('handler', None)

        strategy = self._strategy.get()
        request = RequestInfo(
            method=method, 
            args=args, kwargs=kwargs, future=None, strategy=strategy
        )
        
        future = await self.manager.add_task(request)
        result = await future

        handler_tasks = []
        def trigger_handler(res):
            if handler and res:
                handler_tasks.append(asyncio.create_task(handler(res)))

        trigger_handler(result)
        async def _before_return(result):
            if result is None:
                return None
            if handler_tasks:
                results_list = await asyncio.gather(*handler_tasks)
                result.handler_results = [item for sublist in results_list for item in (sublist or [])]
            else:
                result.handler_results = []
            return result
        
        continue_fetch = fetch_all or fetch_til or fetch_minlike
        if not continue_fetch or result is None: 
            return await _before_return(result)
        
        while result.next_url:
            account = self.manager.accounts.select(request.strategy)
            next_qs = account.api.parse_qs(result.next_url)

            request.args = ()
            request.kwargs = next_qs
            
            future = await self.manager.add_task(request)
            next_result = await future

            result.next_url = next_result.next_url
            if not handler:
                result.novels += next_result.novels
            
            trigger_handler(next_result)
            
            if next_result.novels:
                last_item = next_result.novels[-1]
                if fetch_til and 'create_date' in last_item:
                    item_date = date_parser.parse(last_item['create_date'])
                    if item_date < fetch_til:
                        break
                        
                if fetch_minlike and 'total_bookmarks' in last_item:
                    if last_item['total_bookmarks'] < fetch_minlike:
                        break
            
        return await _before_return(result)

class _Proxy:
    
    def __init__(self, client: PixivClient):
        self._client = client
    
    def __getattr__(self, name):
        """为每个API方法生成包装器"""
        
        async def wrapper(*args, **kwargs):
            return await self._client.execute(name, *args, **kwargs)
        
        wrapper.__name__ = name
        wrapper.__doc__ = f"Execute pixiv API: {name}"
        
        return wrapper