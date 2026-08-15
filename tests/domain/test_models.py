"""Pure unit tests for domain models — zero I/O."""

import pytest
from copixiv.domain.models.novel import Novel
from copixiv.domain.models.author import Author
from copixiv.domain.models.series import Series
from copixiv.domain.models.tag import Tag, TagPreference, TagAlias, TagPreferenceType


class TestNovel:
    def test_minimal_construction(self):
        n = Novel(id=1, title="Test", author_id=100)
        assert n.id == 1
        assert n.title == "Test"
        assert n.author_id == 100
        assert n.tags == []
        assert n.is_favourite == 0

    def test_full_construction(self):
        n = Novel(
            id=42,
            title="長篇",
            author_id=7,
            author_name="author1",
            path="/tmp/novel.txt",
            like=500,
            view=2000,
            text=8000,
            caption="A story",
            series_id=3,
            series_name="My Series",
            series_index=1,
            create_time="2024-01-15",
            has_epub=2,
            tags=["tag1", "tag2"],
            is_favourite=1,
            is_special_follow=1,
        )
        assert n.like == 500
        assert n.series_index == 1
        assert n.tags == ["tag1", "tag2"]
        assert n.is_favourite == 1

    def test_default_values(self):
        n = Novel(id=1, title="T", author_id=1)
        assert n.like == 0
        assert n.view == 0
        assert n.text == 0
        assert n.has_epub is None
        assert n.author_name is None
        assert n.series_id is None


class TestAuthor:
    def test_minimal(self):
        a = Author(author_id=1)
        assert a.author_id == 1
        assert a.novel_count == 0

    def test_full(self):
        from datetime import datetime
        a = Author(
            author_id=99,
            author_name="名無し",
            novel_count=15,
            like=300,
            view=5000,
            text=120000,
            last_update=datetime(2024, 6, 1),
        )
        assert a.novel_count == 15
        assert a.last_update == datetime(2024, 6, 1)


class TestSeries:
    def test_minimal(self):
        s = Series(series_id=10)
        assert s.series_id == 10

    def test_with_author(self):
        s = Series(series_id=5, series_name="三部作", author_id=2, novel_count=3)
        assert s.author_id == 2
        assert s.novel_count == 3


class TestTag:
    def test_tag(self):
        t = Tag(id=1, name="R-18", reference_count=42)
        assert t.name == "R-18"
        assert t.reference_count == 42

    def test_tag_preference(self):
        tp = TagPreference(tag="NTR", preference=TagPreferenceType.blocked)
        assert tp.preference == TagPreferenceType.blocked

    def test_tag_alias(self):
        ta = TagAlias(source="R18", target="R-18")
        assert ta.source == "R18"
        assert ta.target == "R-18"
