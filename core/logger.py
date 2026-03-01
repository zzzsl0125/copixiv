import logging
import io
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from typing import Generator, Callable

# ---------- 基础日志配置 ----------
def setup_logging(log_dir='log', app_name='copixiv'):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = log_dir / f"{app_name}_{datetime.now():%Y-%m-%d}.log"
    
    # 清理已有的 handlers，防止重复添加
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
        
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding='utf-8')
        ]
    )
    
    # 控制第三方库日志
    logging.getLogger("urllib3").setLevel(logging.WARNING)

setup_logging()
logger = logging.getLogger(__name__)

# ---------- 日志捕获工具 ----------
@contextmanager
def capture_logs(logger_name: str = None, level=logging.INFO) -> Generator[Callable[[], str], None, None]:
    """捕获特定logger的日志"""
    target_logger = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    
    target_logger.addHandler(handler)
    try:
        yield buffer.getvalue
    finally:
        target_logger.removeHandler(handler)
        buffer.close()
