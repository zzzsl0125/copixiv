"""Novel query builder — builds filtered, paginated, sorted SELECTs."""

from typing import Any

from sqlalchemy import select, func, case, and_, Select
from sqlalchemy.orm import Session

from copixiv.infrastructure.database import models
from copixiv.infrastructure.database import constants as C


class BaseQueryBuilder:
    """Shared query-building helpers for pagination, ordering, FTS."""

    def __init__(self, session: Session, main_model: type):
        self.session = session
        self.main_model = main_model
        self.params: dict[str, Any] = {}
        self._fts_query: str | None = None

    def with_keyword(
        self,
        keyword_string: str,
        fts_table: str,
        apply_to: Select | None = None,
    ) -> tuple[Select, Any]:
        """Add FTS5 MATCH to the query. Returns (modified_stmt, condition_or_None)."""
        if not keyword_string.strip():
            return apply_to, None

        # Build a safe FTS5 query string
        tokens = [
            f'"{t}"' if " " in t else t
            for t in keyword_string.split()
        ]
        fts_query = " AND ".join(tokens)
        self._fts_query = fts_query

        fts_t = models.Base.metadata.tables.get(fts_table)
        if fts_t is None:
            # FTS table not yet created — skip
            return apply_to, None

        condition = fts_t.c.id.in_(
            select(fts_t.c.id).where(
                func.fts_match(fts_t.c, fts_query)
            )
        )
        if apply_to is not None:
            return apply_to, condition
        return apply_to, condition

    @property
    def fts_query(self) -> str | None:
        return self._fts_query

    def with_pagination(
        self,
        cursor: dict | None,
        order_by: str,
        order_direction: str,
        apply_to: Select | None = None,
    ) -> Select:
        """Apply cursor-based pagination."""
        stmt = apply_to
        if cursor:
            col = getattr(self.main_model, order_by, None)
            # Build the pagination condition
            if stmt is not None and col is not None:
                stmt = stmt.where(
                    # We need to handle with a composite cursor
                    col < cursor[order_by]
                )
        return stmt

    def with_ordering(
        self,
        order_by: str,
        order_direction: str,
        apply_to: Select | None = None,
    ) -> Select:
        stmt = apply_to
        if stmt is None:
            return stmt
        col = getattr(self.main_model, order_by, None)
        if col is not None:
            if order_direction.upper() == "DESC":
                stmt = stmt.order_by(col.desc())
            else:
                stmt = stmt.order_by(col.asc())
        return stmt

    def with_limit(
        self,
        limit: int,
        apply_to: Select | None = None,
    ) -> Select:
        stmt = apply_to
        if stmt is not None:
            stmt = stmt.limit(limit)
        return stmt

    def _apply_ordering(
        self,
        query: Select,
        order_by: str,
        order_direction: str,
    ) -> Select:
        col = getattr(self.main_model, order_by, None)
        if col is not None:
            if order_direction.upper() == "DESC":
                return query.order_by(col.desc())
            else:
                return query.order_by(col.asc())
        return query


class NovelQueryBuilder(BaseQueryBuilder):
    """Builds Novel list queries with full filtering, pagination, and sorting."""

    def __init__(self, repo, **params):
        super().__init__(repo.session, models.Novel)
        self.repo = repo
        self.params = params

    def build(self) -> tuple[Select, dict]:
        id_filter_subquery = self._build_id_filter_subquery()
        main_query = self._build_main_query(id_filter_subquery)
        return main_query, self.params

    # ---- ID filter subquery -------------------------------------------------

    def _build_id_filter_subquery(
        self, count_mode: bool = False
    ) -> Select:
        id_stmt = select(self.main_model.id)
        subquery_wheres: list = []

        queries = self.params.get("queries") or {}

        # Standard queries (tags, keywords, fields)
        id_stmt = self._process_standard_queries(queries, subquery_wheres, id_stmt)

        # Thresholds
        self._apply_thresholds(subquery_wheres)

        if subquery_wheres:
            id_stmt = id_stmt.where(and_(*subquery_wheres))

        if not count_mode:
            id_stmt = self._apply_pagination_and_sorting(id_stmt)

        return id_stmt.subquery("filtered_ids")

    def _process_standard_queries(
        self, queries: dict, subquery_wheres: list, id_stmt: Select
    ) -> Select:
        tags: set[str] = set()
        keywords: set[str] = set()

        for value, qtype in queries.items():
            if not isinstance(value, str) or value.strip() == "":
                continue
            if qtype == C.FIELD_TAGS:
                tags.add(value)
            elif qtype == C.FIELD_KEYWORD:
                keywords.add(value)
            else:
                self._process_single_query(qtype, value, subquery_wheres)

        if keywords:
            id_stmt = self._apply_keyword_search(keywords, id_stmt, subquery_wheres)
        if tags:
            id_stmt = self._apply_tag_filter(tags, id_stmt)

        return id_stmt

    def _process_single_query(
        self, qtype: str, value: str, subquery_wheres: list
    ) -> None:
        self.repo._validate_query_field(qtype)

        if qtype == C.FIELD_IS_FAVOURITE:
            subquery_wheres.append(
                select(1)
                .where(models.Favourite.novel_id == self.main_model.id)
                .exists()
            )
        elif qtype == C.FIELD_IS_SPECIAL_FOLLOW:
            subquery_wheres.append(
                select(1)
                .where(models.SpecialFollow.author_id == self.main_model.author_id)
                .exists()
            )
        elif value and qtype in self.repo.VALID_NOVEL_FIELDS:
            model_field = getattr(self.main_model, qtype)
            if qtype in (C.COL_AUTHOR_ID, C.COL_SERIES_ID, C.COL_ID):
                subquery_wheres.append(model_field.in_([value]))

    def _apply_keyword_search(
        self, keywords: set[str], id_stmt: Select, subquery_wheres: list
    ) -> Select:
        keyword_string = " ".join(filter(None, keywords))
        if not keyword_string:
            return id_stmt
        stmt, condition = self.with_keyword(
            keyword_string, C.TABLE_NOVEL_FTS, apply_to=id_stmt
        )
        if condition is not None:
            subquery_wheres.append(condition)
        return stmt

    def _apply_tag_filter(self, tags: set[str], id_stmt: Select) -> Select:
        if not tags:
            return id_stmt
        if len(tags) == 1:
            tag_name = next(iter(tags))
            return id_stmt.where(
                select(1)
                .select_from(models.NovelTag)
                .join(models.Tag, models.NovelTag.tag_id == models.Tag.id)
                .where(
                    models.Tag.name == tag_name,
                    models.NovelTag.novel_id == self.main_model.id,
                )
                .exists()
            )
        return (
            id_stmt
            .join(models.NovelTag, self.main_model.id == models.NovelTag.novel_id)
            .join(models.Tag, models.NovelTag.tag_id == models.Tag.id)
            .where(models.Tag.name.in_(tags))
            .group_by(self.main_model.id)
            .having(func.count(self.main_model.id) == len(tags))
        )

    def _apply_thresholds(self, subquery_wheres: list) -> None:
        if self.params.get("min_like") is not None:
            subquery_wheres.append(
                func.coalesce(self.main_model.like, 0) >= self.params["min_like"]
            )
        if self.params.get("min_text") is not None:
            subquery_wheres.append(
                self.main_model.text >= self.params["min_text"]
            )

    def _apply_pagination_and_sorting(self, id_stmt: Select) -> Select:
        id_stmt = self.with_pagination(
            self.params.get("cursor"),
            self.params["order_by"],
            self.params["order_direction"],
            apply_to=id_stmt,
        )
        id_stmt = self.with_ordering(
            self.params["order_by"],
            self.params["order_direction"],
            apply_to=id_stmt,
        )
        id_stmt = self.with_limit(
            self.params["per_page"], apply_to=id_stmt
        )
        return id_stmt

    # ---- main query ---------------------------------------------------------

    def _build_main_query(self, id_filter_subquery) -> Select:
        main_query = (
            select(
                *self.main_model.__table__.c,
                func.group_concat(models.Tag.name, "|||").label(C.COL_TAGS),
                case(
                    (models.Favourite.novel_id != None, 1), else_=0
                ).label(C.FIELD_IS_FAVOURITE),
                case(
                    (models.SpecialFollow.author_id != None, 1), else_=0
                ).label(C.FIELD_IS_SPECIAL_FOLLOW),
            )
            .select_from(self.main_model)
            .join(
                id_filter_subquery,
                self.main_model.id == id_filter_subquery.c.id,
            )
            .outerjoin(
                models.NovelTag, self.main_model.id == models.NovelTag.novel_id
            )
            .outerjoin(models.Tag, models.NovelTag.tag_id == models.Tag.id)
            .outerjoin(
                models.Favourite, self.main_model.id == models.Favourite.novel_id
            )
            .outerjoin(
                models.SpecialFollow,
                self.main_model.author_id == models.SpecialFollow.author_id,
            )
            .group_by(self.main_model.id)
        )
        return self._apply_ordering(
            main_query,
            self.params["order_by"],
            self.params["order_direction"],
        )
