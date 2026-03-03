#!/usr/bin/env python3
"""
SQLite数据库迁移脚本 - 支持字段重命名和删除
将旧数据库数据迁移到新数据库结构
"""

import sqlite3
import json
from typing import Dict, List, Any
import logging
from pathlib import Path

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class SQLiteMigrator:
    """SQLite数据库迁移器"""
    
    def __init__(self, old_db_path: str, new_db_path: str, table_mappings: Dict[str, Dict] = None):
        """
        初始化迁移器
        
        Args:
            old_db_path: 旧数据库路径
            new_db_path: 新数据库路径
            table_mappings: 表字段映射配置，格式：
                {
                    'novel': {
                        'old_name1': 'new_name1',  # 重命名字段
                        'old_name2': None,         # 删除的字段
                        # 省略的字段表示保留原名
                    }
                }
        """
        self.old_db_path = old_db_path
        self.new_db_path = new_db_path
        self.table_mappings = table_mappings or {}
        
    def get_table_schema(self, db_path: str, table_name: str) -> List[Dict]:
        """获取表结构信息"""
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 获取表结构
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = []
        for col in cursor.fetchall():
            columns.append({
                'cid': col[0],
                'name': col[1],
                'type': col[2],
                'notnull': col[3],
                'dflt_value': col[4],
                'pk': col[5]
            })
        
        conn.close()
        return columns
    
    def get_all_tables(self, db_path: str) -> List[str]:
        """获取数据库中所有表名"""
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        return tables
    
    def map_columns(self, old_columns: List[str], new_columns: List[str], 
                    table_name: str) -> Dict[str, str]:
        """
        建立新旧字段映射关系
        
        Returns:
            字典: {新字段名: 旧字段名}
        """
        mapping = {}
        table_config = self.table_mappings.get(table_name, {})
        
        # 构建旧字段名到新字段名的映射
        old_to_new = {}
        for old_col, new_col in table_config.items():
            if new_col is not None:  # None表示删除该字段
                old_to_new[old_col] = new_col
        
        # 对于新表中的每个字段，找到对应的旧字段
        for new_col in new_columns:
            # 检查是否在映射配置中（作为目标字段）
            found = False
            for old_col, mapped_new_col in old_to_new.items():
                if mapped_new_col == new_col:
                    mapping[new_col] = old_col
                    found = True
                    break
            
            # 如果没有找到映射，尝试直接使用相同名称
            if not found and new_col in old_columns:
                mapping[new_col] = new_col
        
        return mapping
    
    def migrate_table(self, table_name: str, chunk_size: int = 1000):
        """
        迁移单个表的数据
        
        Args:
            table_name: 表名
            chunk_size: 批量处理的行数
        """
        logger.info(f"开始迁移表: {table_name}")
        
        # 获取新旧表结构
        old_schema = self.get_table_schema(self.old_db_path, table_name)
        new_schema = self.get_table_schema(self.new_db_path, table_name)
        
        old_columns = [col['name'] for col in old_schema]
        new_columns = [col['name'] for col in new_schema]
        
        # 建立字段映射
        column_mapping = self.map_columns(old_columns, new_columns, table_name)
        
        if not column_mapping:
            logger.warning(f"表 {table_name} 没有可迁移的字段，跳过")
            return
        
        logger.info(f"字段映射: {column_mapping}")
        
        # 连接数据库
        old_conn = sqlite3.connect(self.old_db_path)
        new_conn = sqlite3.connect(self.new_db_path)
        old_conn.row_factory = sqlite3.Row
        new_conn.row_factory = sqlite3.Row
        
        old_cursor = old_conn.cursor()
        new_cursor = new_conn.cursor()
        
        try:
            # 开始事务
            new_conn.execute("BEGIN TRANSACTION")
            
            # 查询旧表数据
            old_cols_str = ', '.join(set(column_mapping.values()))  # 去重的旧表字段
            old_cursor.execute(f"SELECT {old_cols_str} FROM {table_name}")
            
            # 准备插入语句
            insert_cols = list(column_mapping.keys())
            placeholders = ', '.join(['?' for _ in insert_cols])
            insert_sql = f"INSERT INTO {table_name} ({', '.join(insert_cols)}) VALUES ({placeholders})"
            
            # 批量处理数据
            row_count = 0
            while True:
                rows = old_cursor.fetchmany(chunk_size)
                if not rows:
                    break
                
                batch_data = []
                for row in rows:
                    # 根据映射构建新表的数据行
                    new_row = []
                    for new_col in insert_cols:
                        old_col = column_mapping[new_col]
                        value = row[old_col]
                        
                        # 处理特殊类型（如JSON）
                        if isinstance(value, str) and value.startswith(('{', '[')):
                            try:
                                # 尝试解析JSON，确保格式正确
                                json.loads(value)
                                # 如果已经是有效的JSON，保留原样
                            except json.JSONDecodeError:
                                pass  # 不是JSON，保持原样
                        
                        new_row.append(value)
                    
                    batch_data.append(new_row)
                
                # 批量插入
                new_cursor.executemany(insert_sql, batch_data)
                row_count += len(batch_data)
                logger.info(f"表 {table_name}: 已迁移 {row_count} 行")
            
            # 提交事务
            new_conn.commit()
            logger.info(f"表 {table_name} 迁移完成，共迁移 {row_count} 行")
            
        except Exception as e:
            new_conn.rollback()
            logger.error(f"迁移表 {table_name} 失败: {str(e)}")
            raise
        finally:
            old_conn.close()
            new_conn.close()
    
    def migrate_database(self, tables: List[str] = None, chunk_size: int = 1000):
        """
        迁移整个数据库
        
        Args:
            tables: 要迁移的表列表，None表示迁移所有表
            chunk_size: 批量处理的行数
        """
        logger.info("开始数据库迁移")
        logger.info(f"旧数据库: {self.old_db_path}")
        logger.info(f"新数据库: {self.new_db_path}")
        
        # 检查数据库文件是否存在
        if not Path(self.old_db_path).exists():
            raise FileNotFoundError(f"旧数据库文件不存在: {self.old_db_path}")
        
        if not Path(self.new_db_path).exists():
            logger.warning(f"新数据库文件不存在，将创建: {self.new_db_path}")
        
        # 获取所有表
        old_tables = self.get_all_tables(self.old_db_path)
        new_tables = self.get_all_tables(self.new_db_path)
        
        logger.info(f"旧数据库表: {old_tables}")
        logger.info(f"新数据库表: {new_tables}")
        
        # 确定要迁移的表
        if tables is None:
            # 迁移新旧数据库中都存在的表
            tables_to_migrate = set(old_tables) & set(new_tables)
        else:
            tables_to_migrate = tables
        
        logger.info(f"将迁移的表: {tables_to_migrate}")
        
        # 迁移每个表
        for table in tables_to_migrate:
            try:
                self.migrate_table(table, chunk_size)
            except Exception as e:
                logger.error(f"迁移表 {table} 时出错: {str(e)}")
                # 可以选择继续或停止
                # raise
        
        logger.info("数据库迁移完成")


def main():
    """主函数"""
    # 配置文件（可以从外部JSON文件加载）
    config = {
        # 数据库路径
        'old_db': '/home/invocation/novel_manager/database/database.db',
        'new_db': '/home/invocation/copixiv/database/database.db',
        
        # 表字段映射配置
        'table_mappings': {
            'novel': {
                # 重命名字段: 旧字段名 -> 新字段名
                'likes': 'like',
                'views': 'view',
                'texts': 'text',
                'author': 'author_name',
                # 删除的字段设置为 None
                'index': None,
                'source': None,
                # 省略的字段（如 'id', 'created_at'）保留原名
            },
            'author': {
                'likes': 'like',
                'views': 'view',
                'texts': 'text',
                'average_likes': None,
            },
            'series': {
                'likes': 'like',
                'views': 'view',
                'texts': 'text',
                'author_name': None,
                'average_likes': None,
                'tags': None,
            },
            'tag' : {},
            'novel_tag': {},
            'tag_preferences': {},
            # 可以为其他表也定义映射
            # 'users': {
            #     'username': 'user_name',
            #     'password': None,  # 删除密码字段
            # }
        },
        
        # 批量处理大小
        'chunk_size': 500
    }
    
    # 或者从JSON文件加载配置
    # with open('migration_config.json', 'r', encoding='utf-8') as f:
    #     config = json.load(f)
    
    try:
        # 创建迁移器
        migrator = SQLiteMigrator(
            old_db_path=config['old_db'],
            new_db_path=config['new_db'],
            table_mappings=config.get('table_mappings', {})
        )
        
        # 执行迁移
        migrator.migrate_database(
            tables=[t for t in config.get('table_mappings', {})],  # 指定要迁移的表，None表示迁移所有
            chunk_size=config.get('chunk_size', 1000)
        )
        
        logger.info("迁移成功完成！")
        
    except Exception as e:
        logger.error(f"迁移失败: {str(e)}")
        return 1
    
    return 0


if __name__ == "__main__":
    exit(main())