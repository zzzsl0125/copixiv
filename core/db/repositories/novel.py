# -*- coding: utf-8 -*-
import logging
from typing import Any, Literal, List, Set
from sqlalchemy import select, func, case, and_, or_, Select, Table
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .base_repository import BaseRepository
from .fts_manager import FTSManager
from .query_builder import BaseQueryBuilder
from .mixins import RandomPoolMixin, EpubMixin
from .. import models
from .. import constants as C

class NovelQueryBuilder(BaseQueryBuilder):
    """
    专门用于构建 Novel 查询的构建器。
    """
    def __init__(self, repo: 'Novel', **params):
        super().__init__(repo.session, models.Novel)
        self.repo = repo
        self.params = params

    def build(self):
        # 1. 构建子查询以过滤 Novel ID
        id_filter_subquery = self._build_id_filter_subquery()

        # 2. 使用过滤后的 ID 构建主查询
        main_query = self._build_main_query(id_filter_subquery)
        
        return main_query, self.params

    def _build_id_filter_subquery(self):
        """构建一个子查询，仅返回符合所有过滤条件的小说 ID。"""
        # 从主模型 ID 的基本选择开始
        id_stmt = select(self.main_model.id)
        subquery_wheres = []

        queries = self.params.get('queries') or {}

        # 1. 处理标准查询 (tags, keywords, fields)
        id_stmt = self._process_standard_queries(queries, subquery_wheres, id_stmt)
        
        # 2. 应用阈值条件 (min_likes, min_texts)
        self._apply_thresholds(subquery_wheres)
        
        # 3. 应用所有收集到的 WHERE 条件
        if subquery_wheres:
            id_stmt = id_stmt.where(and_(*subquery_wheres))
        
        # 4. 应用分页和排序
        id_stmt = self._apply_pagination_and_sorting(id_stmt)

        return id_stmt.subquery('filtered_ids')

    def _process_standard_queries(self, queries: dict, subquery_wheres: List, id_stmt: Select):
        """处理标签、关键词和其他字段查询。"""
        tags, keywords = set(), set()
        print(queries)

        for value, type in queries.items():
            if not isinstance(value, str):
                raise ValueError(f"Query value must be string")
            if value.strip() == "":
                continue

            if type == C.FIELD_TAGS:
                tags.add(value)
            elif type == C.FIELD_KEYWORD:
                keywords.add(value)
            else:
                self._process_single_query(type, value, subquery_wheres)
        
        if keywords:
            id_stmt = self._apply_keyword_search(keywords, id_stmt, subquery_wheres)

        if tags:
            id_stmt = self._apply_tag_filter(tags, id_stmt)
        
        return id_stmt

    def _process_single_query(self, type: str, value: str, subquery_wheres: List):
        """处理单个非标签、非关键词的查询条件。"""
        self.repo._validate_query_field(type)
        
        if type == C.FIELD_IS_FAVOURITE:
            subquery_wheres.append(self.main_model.id.in_(select(models.Favourite.novel_id)))

        elif type == C.FIELD_IS_SPECIAL_FOLLOW:
            subquery_wheres.append(self.main_model.author_id.in_((
                select(self.main_model.author_id)
                    .join(models.Favourite, self.main_model.id == models.Favourite.novel_id)
                    .distinct()
            )))

        elif value and type in self.repo.VALID_NOVEL_FIELDS:
            model_field = getattr(self.main_model, type)
            if type in [C.COL_AUTHOR_ID, C.COL_SERIES_ID, C.COL_ID]:
                subquery_wheres.append(model_field.in_(value))
            else:
                # 不应当进入这个分支
                like_conditions = [model_field.like(f"%{v}%") for v in value]
                subquery_wheres.append(or_(*like_conditions))

    def _apply_keyword_search(self, keywords: List[str], id_stmt: Select, subquery_wheres: List):
        """应用全文检索关键词搜索。"""
        keyword_string = " ".join(filter(None, keywords))
        if keyword_string:
            stmt, condition = self.with_keyword(keyword_string, C.TABLE_NOVEL_FTS, apply_to=id_stmt)
            if condition is not None:
                subquery_wheres.append(condition)
            return stmt
        return id_stmt

    def _apply_tag_filter(self, tags: Set[str], id_stmt: Select):
        """应用标签过滤：必须包含所有指定标签。"""
        if not tags:
            return id_stmt
             
        # 对于每个标签，创建一个EXISTS子查询，确保小说包含该标签
        for tag in tags:
            # 子查询：检查当前小说是否有关联的指定标签
            tag_subquery = (
                select(1)
                .select_from(models.NovelTag)
                .join(models.Tag, models.NovelTag.tag_id == models.Tag.id)
                .where(
                    models.NovelTag.novel_id == self.main_model.id,
                    models.Tag.name == tag
                )
                .exists()
            )
            id_stmt = id_stmt.where(tag_subquery)
        
        return id_stmt

    def _apply_thresholds(self, subquery_wheres: List):
        """应用最小喜欢数和最小文本数过滤。"""
        if self.params.get('min_likes') is not None:
            subquery_wheres.append(func.coalesce(self.main_model.like, 0) >= self.params['min_likes'])
        if self.params.get('min_texts') is not None:
            subquery_wheres.append(self.main_model.text >= self.params['min_texts'])

    def _apply_pagination_and_sorting(self, id_stmt):
        """应用分页和排序逻辑。"""
        # 复用基类方法
        id_stmt = self.with_pagination(
            self.params.get('cursor'), 
            self.params['order_by'], 
            self.params['order_direction'], 
            apply_to=id_stmt
        )
        id_stmt = self.with_ordering(
            self.params['order_by'], 
            self.params['order_direction'], 
            apply_to=id_stmt
        )
        id_stmt = self.with_limit(
            self.params['per_page'], 
            apply_to=id_stmt
        )
        return id_stmt

    def _build_main_query(self, id_filter_subquery):
        """构建最终查询，将过滤后的 ID 与其他表连接以获取所需数据。"""
        main_query = (
            select(
                *self.main_model.__table__.c,
                func.group_concat(models.Tag.name, '|||').label(C.COL_TAGS),
                case((models.Favourite.novel_id != None, 1), else_=0).label(C.FIELD_IS_FAVOURITE)
            )
            .select_from(self.main_model)
            .join(id_filter_subquery, self.main_model.id == id_filter_subquery.c.id)
            .outerjoin(models.NovelTag, self.main_model.id == models.NovelTag.novel_id)
            .outerjoin(models.Tag, models.NovelTag.tag_id == models.Tag.id)
            .outerjoin(models.Favourite, self.main_model.id == models.Favourite.novel_id)
            .group_by(self.main_model.id)
        )

        # 使用基类的排序逻辑，确保排序一致性
        order_by = self.params['order_by']
        order_direction = self.params['order_direction']
        
        return self._apply_ordering(main_query, order_by, order_direction)


class Novel(BaseRepository, RandomPoolMixin, EpubMixin):

    def __init__(self, session: Session):
        super().__init__(session)
        self.logger = logging.getLogger(__name__)
        # 防止动态字段名导致 SQL 注入的白名单
        self.VALID_NOVEL_FIELDS = {c.name for c in models.Novel.__table__.c}
        self.UPDATABLE_NOVEL_FIELDS = list(self.VALID_NOVEL_FIELDS - {C.COL_ID, C.COL_INDEX})
        self.VALID_NOVEL_QUERY_FIELDS = self.VALID_NOVEL_FIELDS | {
            C.FIELD_TAGS, C.FIELD_KEYWORD, C.FIELD_IS_FAVOURITE, 
            C.FIELD_IS_SPECIAL_FOLLOW, C.ORDER_BY_NONE, C.ORDER_BY_RANDOM
        }
    
    def upsert_novels(self, novels: list[dict], force_update: list[str] = []):
        """novels always from webview_novel"""
        if not novels: 
            return

        # Prepare a map of id -> tags
        novel_tags_map = {}
        processed_novels = []

        # First pass: Extract tags and prepare clean novel data
        for n in novels:
            novel_tags_map[n.get('id')] = n.pop('tag', [])
            processed_novels.append(n)

        # Fields that should be updated if the novel already exists
        update_fields_set = set(['like', 'view'] + force_update)
        
        new_novel_ids = []

        for novel in processed_novels:
            filtered_data = {
                k: v for k, v in novel.items() 
                if k in self.VALID_NOVEL_FIELDS
            }
            existing = self.session.get(models.Novel, novel.get('id'))
            
            if existing:
                for key in update_fields_set:
                    setattr(existing, key, novel.get(key))
            else:
                # Create new novel with all provided fields
                new_novel = models.Novel(**filtered_data)
                self.session.add(new_novel)
                new_novel_ids.append(novel.get('id'))
        
        # Flush to ensure IDs are generated and objects are in session
        self.session.flush()

        # Update tags
        for nid, tag_list in novel_tags_map.items():
            self.rewrite_tags(nid, set(tag_list))

        # Update FTS for new novels
        FTSManager(self.session).update_novel_fts_index(new_novel_ids)

    def update_field(self, novel_id: int, field: str, value: Any):
        """更新特定小说的单个字段。"""
        if field not in self.UPDATABLE_NOVEL_FIELDS:
            raise ValueError(f"Invalid or non-updatable field: {field}")
        
        novel = self.session.get(models.Novel, novel_id)
        if novel:
            setattr(novel, field, value)
            self.session.add(novel)

    def get_novels(
        self,
        queries: dict | None = None,
        order_by: str = C.COL_LIKES,
        order_direction: Literal['ASC', 'DESC'] = 'DESC',
        cursor: str | None = None,
        per_page: int = 50,
        min_like: int | None = None,
        min_text: int | None = None,
    ) -> list[dict]:
        """
        queries: {value: type},  
        e.g. {"magical girl": "tag", "illya": "keyword"}  
        e.g. {"1919810": "id", "114514": "author_id"}
        """

        if order_by:
            self._validate_query_field(order_by)
        
        if queries:
            for q_type in queries.values():
                self._validate_query_field(q_type)
        
        params = {
            'queries': queries,
            'order_by': order_by,
            'order_direction': order_direction,
            'cursor': cursor,
            'per_page': per_page,
            'min_like': min_like,
            'min_text': min_text
        }
        
        builder = NovelQueryBuilder(self, **params)
        query, params = builder.build()
        
        result = self.session.execute(query, params)
        novels = [dict(row._mapping) for row in result.fetchall()]
        
        return self._process_novel_rows(novels)

    def get_existing_ids(self, novel_ids: list[int]) -> set[int]:
        """从小说 ID 列表中，返回数据库中已存在的 ID 集合。"""
        if not novel_ids: return set()
        stmt = select(models.Novel.id).where(models.Novel.id.in_(novel_ids))
        result = self.session.execute(stmt).scalars().all()
        return set(result)

    def delete(self, novel_id: int):
        """删除小说及其相关数据（标签关联、FTS 条目）。"""
        FTSManager(self.session).delete_novel_fts(novel_id)
        novel = self.session.get(models.Novel, novel_id)
        if novel: self.session.delete(novel)

    def delete_tag(self, tag_id: int):
        """从数据库中删除标签。"""
        tag = self.session.get(models.Tag, tag_id)
        if tag: self.session.delete(tag)

    def toggle_favourite(self, novel_id: int):
        """切换小说的收藏状态。"""
        fav = self.session.query(models.Favourite)\
                            .filter(models.Favourite.novel_id == novel_id)\
                            .one_or_none()

        if fav:
            self.session.delete(fav)
        else:
            new_fav = models.Favourite(novel_id=novel_id)
            self.session.add(new_fav)
    
    def get_favourited_author_ids(self) -> list[int]:
        stmt = select(models.Novel.author_id)\
                .distinct()\
                .join(models.Favourite, models.Novel.id == models.Favourite.novel_id)
        return self.session.execute(stmt).scalars().all()
    
    def rebuild_fts(self):
        FTSManager(self.session).rebuild_novel_fts()

    def _fetch_set(self, stmt) -> set:
        result = self.session.execute(stmt).scalars().all()
        return set(result) if result else set()

    def rewrite_tags(self, novel_id: int, new_tags: set):
        if not new_tags:
            self.session.query(models.NovelTag)\
                .filter(models.NovelTag.novel_id == novel_id)\
                .delete(synchronize_session=False)
            return

        existing = self.session.execute(
            select(models.Tag.name)\
                .join(models.NovelTag, models.Tag.id == models.NovelTag.tag_id)\
                .where(models.NovelTag.novel_id == novel_id)
        ).scalars().all()
        existing = set(existing)

        tags_to_add = new_tags - existing
        tags_to_remove = existing - new_tags

        if tags_to_remove:
            tag_ids_stmt = select(models.Tag.id).where(models.Tag.name.in_(tags_to_remove))
            self.session.query(models.NovelTag).filter(
                models.NovelTag.novel_id == novel_id,
                models.NovelTag.tag_id.in_(tag_ids_stmt)
            ).delete(synchronize_session=False)
            self._update_tag_reference_count(tags_to_remove, -1)

        if tags_to_add:
            self._add_tags(novel_id, tags_to_add)

    def _add_tags(self, novel_id: int, tags_to_add: set[str]):
        # 1. 批量插入新标签（如果不存在）
        insert_stmt = sqlite_insert(models.Tag).values(
            [{"name": tag} for tag in tags_to_add]
        ).on_conflict_do_nothing(index_elements=['name'])
        self.session.execute(insert_stmt)

        # 2. 获取标签 ID
        tag_ids_stmt = select(models.Tag.id).where(models.Tag.name.in_(tags_to_add))
        tag_ids = self.session.execute(tag_ids_stmt).scalars().all()

        # 3. 创建关联
        if tag_ids:
            link_values = [{"novel_id": novel_id, "tag_id": tag_id} for tag_id in tag_ids]
            # 使用 bulk_insert_mappings 可能比逐个添加快，但这里也可以用 insert().values()
            self.session.bulk_insert_mappings(models.NovelTag, link_values)
        
        # 4. 更新引用计数
        self._update_tag_reference_count(tags_to_add, 1)

    def _update_tag_reference_count(self, tags: set[str], delta: int):
        """增加或减少一组标签的引用计数。"""
        if not tags:
            return
        
        self.session.query(models.Tag).filter(models.Tag.name.in_(tags)).update(
            {'reference_count': models.Tag.reference_count + delta},
            synchronize_session=False
        )

    def _validate_query_field(self, field: str):
        """验证用于查询的字段以防止注入。"""
        if field not in self.VALID_NOVEL_QUERY_FIELDS:
            raise ValueError(f"Invalid or disallowed query field: {field}")

    def _process_novel_rows(self, novels: list[dict]) -> list[dict]:
        """后处理小说行，将连接的标签字符串拆分为列表。"""
        if not novels:
            return []
        for novel in novels:
            tags_str = novel.get(C.COL_TAGS)
            if tags_str and isinstance(tags_str, str):
                if '|||' in tags_str:
                    novel[C.COL_TAGS] = tags_str.split('|||')
                else:
                    # Fallback for single tag or legacy comma
                    novel[C.COL_TAGS] = tags_str.split(',') if ',' in tags_str else [tags_str]
            else:
                novel[C.COL_TAGS] = []
        return novels
