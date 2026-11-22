"""Unit tests for search history filtering and recording."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from copixiv.features.novels.routes.query import _filter_recordable_conditions
from copixiv.features.search_history import record_search_history


def test_filter_recordable_conditions_drops_shelf_navigation_dsl():
    assert _filter_recordable_conditions(None) == []
    assert _filter_recordable_conditions([]) == []
    assert _filter_recordable_conditions([("is_favourite", "1")]) == []
    assert _filter_recordable_conditions([("is_special_follow", "1")]) == []
    assert _filter_recordable_conditions([("source", "local")]) == []
    assert _filter_recordable_conditions([("source", "pixiv")]) == []
    assert _filter_recordable_conditions([("is_favourite", "1"), ("tags", "百合")]) == [("tags", "百合")]
    assert _filter_recordable_conditions([("source", "local"), ("keyword", "崩坏:星穹铁道")]) == [
        ("keyword", "崩坏:星穹铁道")
    ]


def test_record_search_history_avoids_savepoints_and_prunes_to_50():
    session = MagicMock()
    uow = MagicMock()

    @contextmanager
    def _begin():
        yield

    uow.begin = _begin
    uow.session = session

    with (
        patch("copixiv.features.search_history.SQLAlchemyAuthorRepository") as author_cls,
        patch("copixiv.features.search_history.SQLAlchemySeriesRepository") as series_cls,
        patch("copixiv.features.search_history.SQLAlchemySearchHistoryRepository") as history_cls,
    ):
        author_cls.return_value.get_by_id.return_value = {"author_name": "Alice"}
        series_cls.return_value.get_by_id.return_value = {"series_name": "Saga"}

        record_search_history(
            [("author_id", "123"), ("series_id", "456"), ("tags", "A")],
            lambda: uow,
        )

    session.begin_nested.assert_not_called()
    assert author_cls.call_count == 1
    assert series_cls.call_count == 1
    assert history_cls.call_count == 1

    history_repo = history_cls.return_value
    assert history_repo.add_or_update.call_count == 3
    history_repo.add_or_update.assert_any_call("author_id", "123", "Alice")
    history_repo.add_or_update.assert_any_call("series_id", "456", "Saga")
    history_repo.add_or_update.assert_any_call("tags", "A", None)
    history_repo.prune_old.assert_called_once_with(keep=50)
