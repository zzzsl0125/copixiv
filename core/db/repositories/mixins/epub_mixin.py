# -*- coding: utf-8 -*-
from datetime import datetime
from sqlalchemy import select, and_, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from ... import models

class EpubMixin:
    """
    Mixin for EPUB Conversion status management.
    Requires self.session and self.update_field(novel_id, field, value).
    """

    def initialize_epub_conversion_status(self, batch_size: int = 1000):
        """
        Initializes the EPUB conversion status.
        Resets any 'processing' or 'failed' status to 'pending' to retry tasks.
        """
        self.session.query(models.NovelEpubConversion).filter(
            or_(
                models.NovelEpubConversion.status == 'processing',
                models.NovelEpubConversion.status == 'failed'
            )
        ).update(
            {models.NovelEpubConversion.status: 'pending'},
            synchronize_session=False
        )

    def get_novels_for_epub_scan(self, offset: int, limit: int) -> list[dict]:
        """
        Retrieves novels that need to be scanned for images.
        Returns novels where has_epub is 0 (Cannot/No need) and not in NovelEpubConversion table.
        Wait... if has_epub is 0, it means we think it doesn't need epub.
        But maybe this function is intended to scan old novels that might need epub?
        The original logic was `has_epub == False` (which could mean 0 or 1 in new scheme if we mapped False->0).
        But if we now actively set 0 or 1, maybe we only want to scan those that are 0 but haven't been checked?
        Actually, `get_novels_for_epub_scan` seems to be part of a background process to find novels that *might* have images but were missed?
        Or is it the main process to find candidates?
        If `handle_novel_data` now sets 1 for novels with placeholders, then `get_novels_for_epub_scan` might need to look for `has_epub == 1`?
        No, `get_novels_for_epub_scan` says "Retrieves novels that need to be scanned for images".
        If we already scanned them at ingestion (which we now do), maybe this function is obsolete or for backfilling?
        Let's assume it's for backfilling or finding missed ones.
        I will change it to `models.Novel.has_epub == 0` to catch those marked as "no need" but maybe we want to double check?
        OR, maybe I should leave it as `has_epub != 2` (not done)?
        
        Let's look at `mark_epub_created`. It sets `has_epub` to True. Now it should be 2.
        """
        stmt = (
            select(models.Novel)
            .outerjoin(models.NovelEpubConversion, models.Novel.id == models.NovelEpubConversion.novel_id)
            .where(
                and_(
                    models.Novel.has_epub == 0, # Only scan those marked as "no need" (or legacy False)
                    models.NovelEpubConversion.novel_id.is_(None)
                )
            )
            .offset(offset)
            .limit(limit)
        )
        result = self.session.execute(stmt).scalars().all()
        # Return dicts for compatibility
        return [{c.name: getattr(n, c.name) for c in n.__table__.columns} for n in result]

    def mark_novels_as_no_images(self, novel_ids: set[int]):
        """
        Marks novels as having no images in the conversion table.
        """
        if not novel_ids:
            return
            
        values = [
            {
                'novel_id': nid,
                'status': 'no_images',
                'last_processed': datetime.now().isoformat()
            }
            for nid in novel_ids
        ]
        
        stmt = sqlite_insert(models.NovelEpubConversion).values(values)
        stmt = stmt.on_conflict_do_nothing(index_elements=['novel_id'])
        self.session.execute(stmt)

    def add_pending_epub_conversions(self, novel_ids: set[int]):
        """
        Adds novels to the conversion queue with 'pending' status.
        """
        if not novel_ids:
            return
            
        values = [
            {
                'novel_id': nid,
                'status': 'pending',
                'last_processed': datetime.now().isoformat()
            }
            for nid in novel_ids
        ]
        
        stmt = sqlite_insert(models.NovelEpubConversion).values(values)
        stmt = stmt.on_conflict_do_nothing(index_elements=['novel_id'])
        self.session.execute(stmt)
        
    def get_novels_with_pending_epub(self, limit: int) -> list[dict]:
        """
        Retrieves novels that have 'pending' status in NovelEpubConversion.
        """
        stmt = (
            select(models.Novel)
            .join(models.NovelEpubConversion, models.Novel.id == models.NovelEpubConversion.novel_id)
            .where(models.NovelEpubConversion.status == 'pending')
            .limit(limit)
        )
        result = self.session.execute(stmt).scalars().all()
        return [{c.name: getattr(n, c.name) for c in n.__table__.columns} for n in result]
        
    def mark_epub_created(self, novel_id: int):
        """
        Marks EPUB creation as successful.
        """
        # Update Novel table using helper method from Repository
        if hasattr(self, 'update_field'):
            self.update_field(novel_id, 'has_epub', 2) # 2: Done
        else:
             # Fallback
             stmt = (
                 models.Novel.__table__.update()
                 .where(models.Novel.id == novel_id)
                 .values(has_epub=2)
             )
             self.session.execute(stmt)
        
        # Update Conversion table
        stmt = sqlite_insert(models.NovelEpubConversion).values(
            novel_id=novel_id,
            status='success',
            last_processed=datetime.now().isoformat()
        ).on_conflict_do_update(
            index_elements=['novel_id'],
            set_={'status': 'success', 'last_processed': datetime.now().isoformat()}
        )
        self.session.execute(stmt)

    def mark_epub_failed(self, novel_id: int, error_message: str):
        """
        Marks EPUB creation as failed.
        """
        # Update Conversion table
        stmt = sqlite_insert(models.NovelEpubConversion).values(
            novel_id=novel_id,
            status='failed',
            last_processed=datetime.now().isoformat()
        ).on_conflict_do_update(
            index_elements=['novel_id'],
            set_={'status': 'failed', 'last_processed': datetime.now().isoformat()}
        )
        self.session.execute(stmt)

    def mark_epub_not_found(self, novel_id: int):
        """
        Marks EPUB creation as failed because novel was not found (404).
        """
        # Update Conversion table
        stmt = sqlite_insert(models.NovelEpubConversion).values(
            novel_id=novel_id,
            status='not_found',
            last_processed=datetime.now().isoformat()
        ).on_conflict_do_update(
            index_elements=['novel_id'],
            set_={'status': 'not_found', 'last_processed': datetime.now().isoformat()}
        )
        self.session.execute(stmt)
