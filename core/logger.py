import logging
import logging.config
import yaml
import os
from datetime import datetime
from pathlib import Path

def setup_logging(
    default_path='log_config.yaml', 
    default_level=logging.INFO, 
    env_key='LOG_CFG',
    log_dir='logs',
    app_name='app'
):
    """
    Setup logging configuration
    """
    path = Path(default_path)
    value = os.getenv(env_key, None)
    
    if value:
        path = Path(value)

    if path.exists():
        with open(path, 'rt') as f:
            try:
                config = yaml.safe_load(f.read())
                
                # Dynamic log filename
                timestamp = datetime.now().strftime("%Y-%m-%d")
                log_filename = f"{app_name}_{timestamp}.log"
                
                # Ensure log directory exists
                log_path = Path(log_dir)
                log_path.mkdir(parents=True, exist_ok=True)
                
                # Update filename in file handler
                if 'handlers' in config and 'file' in config['handlers']:
                    config['handlers']['file']['filename'] = str(log_path / log_filename)
                
                logging.config.dictConfig(config)
                logging.info(f"Logging configured from {path}, output to {log_path / log_filename}")
                return

            except Exception as e:
                print(f"Error loading logging configuration: {e}")
                # Fallback to basic config if YAML loading fails
                logging.basicConfig(level=default_level)
                logging.error(f"Failed to load configuration from {path}. Using default configs.")
    else:
        logging.basicConfig(level=default_level)
        logging.info("Logging configuration file not found. Using default configs.")

import io
class TaskLogHandler(logging.Handler):
    """
    A custom logging handler that captures logs to a string buffer.
    """
    def __init__(self):
        super().__init__()
        self.buffer = io.StringIO()

    def emit(self, record):
        try:
            msg = self.format(record)
            self.buffer.write(msg + '\n')
        except Exception:
            self.handleError(record)

    def get_logs(self) -> str:
        return self.buffer.getvalue()

    def close(self):
        self.buffer.close()
        super().close()

setup_logging()
logger = logging.getLogger(__name__)
