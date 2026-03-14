# Auto-generated config stub
from typing import Any, Dict

class JsonDict(dict):
    def __init__(self, *args, **kwargs) -> None: ...
    def __getattr__(self, attr: Any) -> Any: ...
    def __setattr__(self, attr: Any, value: Any) -> None: ...

class Config_Path:
    token: str
    database: str
    download: str

class Config_Pixiv_client:
    min_interval: int
    cooling_duration: int
    max_concurrency: int

class Config_Telegram:
    token: str
    chat_id: str

class Config_Frontend:
    default_min_like: int
    default_min_text: int

class Config_Task:
    notify_on_new_novel: bool
    timeout: int

class Config:
    config_path: str
    def __init__(self, config_path: str = ...) -> None: ...
    def load(self) -> None: ...
    def get(self, key: str, default: Any = ...) -> Any: ...
    def __getitem__(self, key: str) -> Any: ...
    def __getattr__(self, name: str) -> Any: ...
    path: 'Config_Path'
    pixiv_client: 'Config_Pixiv_client'
    telegram: 'Config_Telegram'
    frontend: 'Config_Frontend'
    task: 'Config_Task'

config: Config
