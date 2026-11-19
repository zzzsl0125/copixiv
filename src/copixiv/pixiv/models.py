"""Permissive Pydantic v2 DTO models for Pixiv App API & webview payloads."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class BasePixivModel(BaseModel):
    """Base model tolerating extra vendor fields and subscript access."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)


def _none_to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


class PixivUser(BasePixivModel):
    id: int = 0
    name: str = ""
    account: str = ""

    @field_validator("name", "account", mode="before")
    @classmethod
    def _coerce_str(cls, value: Any) -> str:
        return _none_to_str(value)


class PixivSeries(BasePixivModel):
    id: int | None = None
    title: str | None = None
    index: int | None = None

    @field_validator("id", "index", mode="before")
    @classmethod
    def _coerce_opt_int(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        return int(value)


class PixivTag(BasePixivModel):
    name: str = ""
    translated_name: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _coerce_name(cls, value: Any) -> str:
        return _none_to_str(value)


class PixivNovelInfo(BasePixivModel):
    """Novel metadata item returned by listing/search/ranking endpoints."""

    id: int
    title: str = ""
    caption: str = ""
    tags: list[PixivTag | str] = Field(default_factory=list)
    user: PixivUser = Field(default_factory=PixivUser)
    series: PixivSeries | None = None
    total_bookmarks: int = 0
    total_view: int = 0
    text_length: int = 0
    create_date: str = ""

    @field_validator("title", "caption", "create_date", mode="before")
    @classmethod
    def _coerce_str_fields(cls, value: Any) -> str:
        return _none_to_str(value)

    @field_validator("series", mode="before")
    @classmethod
    def _empty_series_to_none(cls, value: Any) -> Any:
        if isinstance(value, dict) and not value:
            return None
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def _null_tags_to_list(cls, value: Any) -> Any:
        return [] if value is None else value


class PixivNovelRating(BasePixivModel):
    like: int = 0
    bookmark: int = 0
    view: int = 0


class WebviewNovel(BasePixivModel):
    """Webview v2 novel payload embedded in HTML (camelCase keys)."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        alias_generator=to_camel,
    )

    id: int
    title: str = ""
    user_id: int = 0
    caption: str = ""
    cdate: str = ""
    text: str = ""
    cover_url: str | None = None
    series_id: int | None = None
    series_title: str | None = None
    series_navigation: dict[str, Any] | None = None
    rating: PixivNovelRating = Field(default_factory=PixivNovelRating)
    tags: list[Any] = Field(default_factory=list)
    images: dict[str, Any] | list[Any] | None = None
    illusts: dict[str, Any] | list[Any] | None = None

    @field_validator("title", "caption", "cdate", "text", mode="before")
    @classmethod
    def _coerce_str_fields(cls, value: Any) -> str:
        return _none_to_str(value)

    @field_validator("series_id", mode="before")
    @classmethod
    def _coerce_series_id(cls, value: Any) -> int | None:
        if value in (None, "", 0, "0"):
            return None
        return int(value)

    @field_validator("series_title", "cover_url", mode="before")
    @classmethod
    def _empty_opt_str(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value)

    @field_validator("series_navigation", mode="before")
    @classmethod
    def _normalize_nav(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or not value:
            return None
        return value

    @field_validator("rating", mode="before")
    @classmethod
    def _null_rating(cls, value: Any) -> Any:
        return {} if value is None else value

    @field_validator("tags", mode="before")
    @classmethod
    def _null_tags(cls, value: Any) -> Any:
        return [] if value is None else value


class UserDetailResponse(BasePixivModel):
    user: PixivUser = Field(default_factory=PixivUser)


class NovelListResponse(BasePixivModel):
    novels: list[PixivNovelInfo] = Field(default_factory=list)
    next_url: str | None = None
    series: PixivSeries | None = None
    novel_series_detail: dict[str, Any] | None = None

    @field_validator("novels", mode="before")
    @classmethod
    def _null_novels(cls, value: Any) -> Any:
        return [] if value is None else value

    @field_validator("series", mode="before")
    @classmethod
    def _empty_series(cls, value: Any) -> Any:
        if isinstance(value, dict) and not value:
            return None
        return value
