from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from .base_repository import BaseRepository
from core.db import models

class TagAliasRepository(BaseRepository):
    def __init__(self, session: Session):
        super().__init__(session)

    def get_all(self) -> list[dict]:
        """获取所有标签别名映射"""
        stmt = select(models.TagAlias)
        result = self.session.execute(stmt).scalars().all()
        return [
            {"id": item.id, "source": item.source, "target": item.target}
            for item in result
        ]

    def get_alias_map(self) -> dict[str, str]:
        """获取原标签到目标标签的映射字典，供入库时快速查找"""
        stmt = select(models.TagAlias.source, models.TagAlias.target)
        result = self.session.execute(stmt).all()
        return {row.source: row.target for row in result}

    def add_alias(self, source: str, target: str) -> dict:
        """添加一个新的标签别名映射"""
        # 如果存在则更新，如果不存在则插入
        existing = self.session.execute(
            select(models.TagAlias).where(models.TagAlias.source == source)
        ).scalar_one_or_none()
        
        if existing:
            existing.target = target
            self.session.add(existing)
            self.session.flush()
            return {"id": existing.id, "source": existing.source, "target": existing.target}
        else:
            new_alias = models.TagAlias(source=source, target=target)
            self.session.add(new_alias)
            self.session.flush()
            return {"id": new_alias.id, "source": new_alias.source, "target": new_alias.target}

    def delete_alias(self, alias_id: int):
        """删除一个标签别名映射"""
        stmt = delete(models.TagAlias).where(models.TagAlias.id == alias_id)
        self.session.execute(stmt)
