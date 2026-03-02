import os
import json
from typing import List
from dataclasses import dataclass, field

from core.config import config
from core.logger import logger

@dataclass
class TokenInfo:
    """Token信息"""
    token: str
    special: bool = False
    premium: bool = False

class TokenManager:
    """管理所有可用的token"""
    
    def __init__(self, config: dict = config):
        self.config = config
        self.tokens: List[TokenInfo] = []
        self._load()

    def _load(self):
        """从配置文件加载tokens"""
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
            logger.info(f"Loaded {len(self.tokens)} tokens")
        except Exception as e:
            logger.error(f"Error loading tokens: {e}")
            raise
