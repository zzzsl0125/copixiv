import os
import yaml
from typing import Any, Dict

class Config:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self._config: Dict[str, Any] = {}
        self.load()

    def load(self):
        if not os.path.exists(self.config_path):
            # Fallback or empty if config.yaml doesn't exist
            print(f"Warning: {self.config_path} not found.")
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Error loading config.yaml: {e}")
            return

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._config[key]

# Singleton instance
config = Config()
