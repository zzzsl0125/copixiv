# -*- coding: utf-8 -*-
import random
from sqlalchemy import select, func, case
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from ... import models

class RandomPoolMixin:
    """
    Mixin for Random Novel Pool operations.
    Requires self.session.
    """

    def get_random_novels(
        self,
        count: int = 20,
        min_likes: int = 0,
        min_texts: int = 0,
    ) -> list[dict]:
        """
        Retrieves a random selection of novels from the pre-populated random pool.
        """
        # 1. Fetch a batch of random novel IDs from the pool
        random_ids_query = (
            select(models.RandomNovelPool.novel_id)
            .where(
                models.RandomNovelPool.min_likes == min_likes,
                models.RandomNovelPool.min_texts == min_texts
            )
            .order_by(func.random())
            .limit(count)
        )
        random_ids = self.session.execute(random_ids_query).scalars().all()

        if not random_ids:
            return []

        # 2. Fetch the full novel data for those IDs
        query = (
            select(
                *models.Novel.__table__.c,
                func.group_concat(models.Tag.name).label('tags'),
                case((models.Favourite.novel_id != None, 1), else_=0).label('is_favourite')
            )
            .where(models.Novel.id.in_(random_ids))
            .outerjoin(models.NovelTag, models.Novel.id == models.NovelTag.novel_id)
            .outerjoin(models.Tag, models.NovelTag.tag_id == models.Tag.id)
            .outerjoin(models.Favourite, models.Novel.id == models.Favourite.novel_id)
            .group_by(models.Novel.id)
        )
        result = self.session.execute(query)
        novels = [dict(row._mapping) for row in result.fetchall()]
        
        # 3. Sort results to match the random order from the pool query
        id_order = {id: i for i, id in enumerate(random_ids)}
        novels.sort(key=lambda n: id_order.get(n['id'], float('inf')))

        return novels

    def clear_random_novel_pool(self):
        """Clears the entire random novel pool."""
        self.session.query(models.RandomNovelPool).delete()

    def populate_random_novel_pool(self, min_likes: int, min_texts: int, limit: int = 1000):
        """
        Populates the random novel pool for a specific criteria combination.
        """
        # 1. Fetch all eligible novel IDs for this specific criteria
        eligible_ids_query = (
            select(models.Novel.id)
            .where(
                func.coalesce(models.Novel.like, 0) >= min_likes,
                models.Novel.text >= min_texts
            )
        )
        
        # Only apply random order if there are filters, as it's slow on the full table.
        if min_likes > 0 or min_texts > 0:
            eligible_ids_query = eligible_ids_query.order_by(func.random())
            
        eligible_ids_query = eligible_ids_query.limit(limit)
        
        eligible_ids = self.session.execute(eligible_ids_query).scalars().all()
        
        # 2. Shuffle the IDs
        random.shuffle(eligible_ids)
        
        # 3. Insert into the pool table
        pool_data = [{
            'novel_id': id,
            'min_likes': min_likes,
            'min_texts': min_texts
        } for id in eligible_ids]
        
        if pool_data:
            # Use sqlite_insert with ON CONFLICT DO NOTHING to avoid IntegrityError
            stmt = sqlite_insert(models.RandomNovelPool).values(pool_data)
            stmt = stmt.on_conflict_do_nothing(index_elements=['novel_id', 'min_likes', 'min_texts'])
            self.session.execute(stmt)