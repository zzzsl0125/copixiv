# -*- coding: utf-8 -*-
import jieba
from sqlalchemy.orm import Session
from sqlalchemy import text, select, func, Table, Column, Integer, String
from sqlalchemy.dialects.sqlite import insert
from .. import models
from .. import constants as C

from core.logger import logger

class FTSManager:
    """
    Manages Full-Text Search (FTS) operations, including table creation,
    rebuilding, and index updating.
    """
    TABLE_NOVEL_FTS = C.TABLE_NOVEL_FTS
    COL_TITLE = C.COL_TITLE
    COL_AUTHOR = C.COL_AUTHOR
    COL_SERIES_NAME = C.COL_SERIES_NAME
    COL_TAGS = C.COL_TAGS

    def __init__(self, session: Session):
        self.session = session
        # Define the Table object for SQLAlchemy Core expressions
        # We use models.Base.metadata so it shares the same MetaData registry,
        # but since it's a virtual table created manually, we mark it to extend existing definition if any.
        self.novel_fts_table = Table(
            self.TABLE_NOVEL_FTS,
            models.Base.metadata,
            Column('rowid', Integer, primary_key=True),
            Column(self.COL_TITLE, String),
            Column(self.COL_AUTHOR, String),
            Column(self.COL_SERIES_NAME, String),
            Column(self.COL_TAGS, String),
            extend_existing=True
        )

    @staticmethod
    def tokenize(text_content: str | None) -> str:
        """
        Uses jieba to tokenize text for FTS, providing better search accuracy.
        Skips tokenization for purely numeric strings to allow direct matching.
        """
        if not text_content:
            return ""
        # If the text is purely numeric, don't tokenize it.
        if text_content.isdigit():
            return text_content
        # Use search engine mode for better recall for non-numeric text
        return " ".join(jieba.cut_for_search(text_content))

    def rebuild_novel_fts(self):
        """Rebuilds the entire Novel FTS table efficiently."""
        logger.info(f"--- Rebuilding {self.TABLE_NOVEL_FTS} table... ---")

        # Ensure the FTS virtual table exists
        # Virtual table creation is specific to SQLite and hard to represent purely in generic DDL
        self.session.execute(text(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {self.TABLE_NOVEL_FTS} USING fts5(
                {self.COL_TITLE}, {self.COL_AUTHOR}, {self.COL_SERIES_NAME}, {self.COL_TAGS},
                tokenize = 'porter unicode61'
            );
        """))
        
        # Clear the FTS table using Core expression
        self.session.execute(self.novel_fts_table.delete())

        # Fetch all novels and their tags
        novels_stmt = select(
            models.Novel.id, models.Novel.title, models.Novel.author_name, models.Novel.series_name,
            func.group_concat(models.Tag.name, ' ').label(C.COL_TAGS)
        ).outerjoin(models.NovelTag, models.Novel.id == models.NovelTag.novel_id).outerjoin(models.Tag, models.NovelTag.tag_id == models.Tag.id).group_by(models.Novel.id)
        
        all_novels = self.session.execute(novels_stmt).fetchall()

        batch_size = 150
        for i in range(0, len(all_novels), batch_size):
            batch_novels = all_novels[i:i + batch_size]
            fts_data = []
            for novel in batch_novels:
                fts_data.append({
                    'rowid': novel.id,
                    self.COL_TITLE: self.tokenize(novel.title),
                    self.COL_AUTHOR: self.tokenize(novel.author_name),
                    self.COL_SERIES_NAME: self.tokenize(novel.series_name),
                    self.COL_TAGS: self.tokenize(getattr(novel, C.COL_TAGS) or ''),
                })

            if fts_data:
                stmt = insert(self.novel_fts_table).values(fts_data)
                self.session.execute(stmt)
        
        # Verify count
        # Using count() function with select
        count_stmt = select(func.count()).select_from(self.novel_fts_table)
        count = self.session.execute(count_stmt).scalar() or 0
        
        logger.info(f"--- Successfully rebuilt {self.TABLE_NOVEL_FTS} with {count} novels. ---")

    def update_novel_fts_index(self, novel_ids: list[int]):
        """
        Updates the FTS index for a given list of novel IDs.
        """
        if not novel_ids:
            return

        # Get all necessary data in one query
        novels_stmt = (
            select(
                models.Novel.id, models.Novel.title, models.Novel.author_name, models.Novel.series_name,
                func.group_concat(models.Tag.name, ' ').label(C.COL_TAGS)
            )
            .outerjoin(models.NovelTag, models.Novel.id == models.NovelTag.novel_id)
            .outerjoin(models.Tag, models.NovelTag.tag_id == models.Tag.id)
            .where(models.Novel.id.in_(novel_ids))
            .group_by(models.Novel.id)
        )
        
        novels_to_update = self.session.execute(novels_stmt).fetchall()

        if not novels_to_update:
            return

        fts_data = [
            {
                'rowid': novel.id,
                self.COL_TITLE: self.tokenize(novel.title),
                self.COL_AUTHOR: self.tokenize(novel.author_name),
                self.COL_SERIES_NAME: self.tokenize(novel.series_name),
                self.COL_TAGS: self.tokenize(getattr(novel, C.COL_TAGS) or ''),
            } for novel in novels_to_update
        ]

        # Use Core INSERT OR REPLACE
        if fts_data:
            stmt = insert(self.novel_fts_table).values(fts_data).prefix_with("OR REPLACE")
            self.session.execute(stmt)

    def delete_novel_fts(self, novel_id: int):
        """Deletes a novel's FTS entry."""
        stmt = self.novel_fts_table.delete().where(self.novel_fts_table.c.rowid == novel_id)
        self.session.execute(stmt)
