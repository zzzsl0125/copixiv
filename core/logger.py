import logging
import io
import os
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Generator, Callable

# ---------- 基础日志配置 ----------
def setup_logging(log_dir='log'):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # 清空日志文件
    backend_log_file = log_dir / "backend.log"
    frontend_log_file = log_dir / "frontend.log"
    if backend_log_file.exists():
        with open(backend_log_file, 'w') as f:
            f.truncate(0)
    if frontend_log_file.exists():
        with open(frontend_log_file, 'w') as f:
            f.truncate(0)
            
    # 清理三天前的日志
    for log_file in log_dir.glob("*.log"):
        file_date_str = log_file.stem.split('_')[-1]
        try:
            file_date = datetime.strptime(file_date_str, '%Y-%m-%d')
            if datetime.now() - file_date > timedelta(days=3):
                os.remove(log_file)
        except ValueError:
            continue

    # 创建 backend logger
    backend_logger = logging.getLogger('backend')
    backend_logger.setLevel(logging.INFO)
    backend_handler = logging.FileHandler(backend_log_file, encoding='utf-8')
    backend_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    backend_logger.addHandler(backend_handler)
    backend_logger.addHandler(logging.StreamHandler())

    # 创建 frontend logger
    frontend_logger = logging.getLogger('frontend')
    frontend_logger.setLevel(logging.INFO)
    frontend_handler = logging.FileHandler(frontend_log_file, encoding='utf-8')
    frontend_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    frontend_logger.addHandler(frontend_handler)
    
    # 控制第三方库日志
    logging.getLogger("urllib3").setLevel(logging.WARNING)

setup_logging()
logger = logging.getLogger('backend')

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
