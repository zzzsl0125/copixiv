import os
import yaml
from typing import Any, Dict

class JsonDict(dict):
    """general json object that allows attributes to be bound to and also behaves like a dict"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key, value in self.items():
            if isinstance(value, dict) and not isinstance(value, JsonDict):
                self[key] = JsonDict(value)

    def __getattr__(self, attr: Any) -> Any:
        if attr in self:
            return self[attr]
        raise KeyError(f"Config attribute '{attr}' not found")

    def __setattr__(self, attr: Any, value: Any) -> None:
        if isinstance(value, dict) and not isinstance(value, JsonDict):
            self[attr] = JsonDict(value)
        else:
            self[attr] = value

class Config:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self._config = JsonDict()
        self.load()

    def load(self):
        if not os.path.exists(self.config_path):
            # Fallback or empty if config.yaml doesn't exist
            print(f"Warning: {self.config_path} not found.")
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
                self._config = JsonDict(loaded)
        except Exception as e:
            print(f"Error loading config.yaml: {e}")
            return

    def __getattr__(self, attr: Any) -> Any:
        return getattr(self._config, attr)

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._config[key]

# Singleton instance
config = Config()
