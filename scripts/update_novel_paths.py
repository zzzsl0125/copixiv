#!/usr/bin/env python3
"""
更新novel表中的path字段，将/home/invocation/novel_manager/download/... 
更改为/home/invocation/copixiv/download/...
"""

import sqlite3
import logging
from pathlib import Path
import sys

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def update_novel_paths(db_path: str, batch_size: int = 1000):
    """
    更新novel表中的path字段
    
    Args:
        db_path: 数据库文件路径
        batch_size: 批量更新的大小
    """
    # 连接到数据库
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # 首先检查需要更新的记录数量
        cursor.execute(
            "SELECT COUNT(*) FROM novel WHERE path LIKE '/home/invocation/novel_manager/download/%'"
        )
        total_count = cursor.fetchone()[0]
        logger.info(f"需要更新的记录总数: {total_count}")
        
        if total_count == 0:
            logger.info("没有需要更新的记录")
            return
        
        # 获取需要更新的记录ID和原始路径
        cursor.execute(
            "SELECT id, path FROM novel WHERE path LIKE '/home/invocation/novel_manager/download/%'"
        )
        
        updated_count = 0
        batch_data = []
        
        for row in cursor.fetchall():
            novel_id, old_path = row
            
            # 替换路径前缀
            if old_path.startswith('/home/invocation/novel_manager/download/'):
                new_path = old_path.replace(
                    '/home/invocation/novel_manager/download/',
                    '/home/invocation/copixiv/download/',
                    1  # 只替换第一次出现
                )
                batch_data.append((new_path, novel_id))
                
                # 批量更新
                if len(batch_data) >= batch_size:
                    cursor.executemany(
                        "UPDATE novel SET path = ? WHERE id = ?",
                        batch_data
                    )
                    updated_count += len(batch_data)
                    logger.info(f"已更新 {updated_count}/{total_count} 条记录")
                    batch_data = []
        
        # 更新剩余记录
        if batch_data:
            cursor.executemany(
                "UPDATE novel SET path = ? WHERE id = ?",
                batch_data
            )
            updated_count += len(batch_data)
        
        # 提交事务
        conn.commit()
        logger.info(f"更新完成！总共更新了 {updated_count} 条记录")
        
        # 验证更新结果
        cursor.execute(
            "SELECT COUNT(*) FROM novel WHERE path LIKE '/home/invocation/novel_manager/download/%'"
        )
        remaining_count = cursor.fetchone()[0]
        logger.info(f"更新后仍包含旧前缀的记录数: {remaining_count}")
        
        # 显示一些示例
        cursor.execute(
            "SELECT path FROM novel WHERE path LIKE '/home/invocation/copixiv/download/%' LIMIT 5"
        )
        logger.info("更新后的示例路径:")
        for row in cursor.fetchall():
            logger.info(f"  {row[0]}")
            
    except Exception as e:
        logger.error(f"更新过程中发生错误: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

def main():
    """主函数"""
    # 数据库路径
    db_path = "database/database.db"
    
    # 检查数据库文件是否存在
    if not Path(db_path).exists():
        logger.error(f"数据库文件不存在: {db_path}")
        sys.exit(1)
    
    # 备份数据库（可选）
    # backup_path = f"{db_path}.backup"
    # if not Path(backup_path).exists():
    #     import shutil
    #     logger.info(f"创建数据库备份: {backup_path}")
    #     shutil.copy2(db_path, backup_path)
    
    # 执行更新
    try:
        update_novel_paths(db_path, batch_size=1000)
        logger.info("路径更新脚本执行完成！")
    except Exception as e:
        logger.error(f"脚本执行失败: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()