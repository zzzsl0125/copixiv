from typing import List, Optional, Any, TypeVar, Union

from pixivpy3 import models
from pixivpy3.models import _to_camel
import pydantic
from core.logger import logger

# --- Monkey Patch for WebviewNovel ---
# Fixes validation errors when API returns dict instead of list for certain fields
# We redefine WebviewNovel with looser types (Union[list, dict]) for problematic fields
try:
    from pixivpy3.models import _PYDANTIC_MAJOR_VERSION, NovelRating, NovelNavigationInfo, EmptyObject, BasePixivpyModel

    if _PYDANTIC_MAJOR_VERSION == 1:
        from pydantic import BaseModel, Field

        to_camel = _to_camel
        ConfigDict = dict
    elif _PYDANTIC_MAJOR_VERSION == 2:
        from pydantic import BaseModel, ConfigDict, Field  # type: ignore[assignment]
        from pydantic.alias_generators import to_camel  # type: ignore[assignment]
    else:
        msg = f"Unsupported Pydantic version: {pydantic.__version__}"
        raise ValueError(msg)

    ModelT = TypeVar("ModelT", bound=BaseModel)

    class PatchedWebviewNovel(BasePixivpyModel):
        if _PYDANTIC_MAJOR_VERSION == 2:
            model_config = ConfigDict(
                extra="allow",  # see `novel_text` method for reasons why
                populate_by_name=True,
                alias_generator=to_camel,
            )
        else:

            class Config:
                extra = "allow"
                alias_generator = to_camel
                allow_population_by_field_name = True


        id: str
        title: str
        series_id: Optional[str]
        series_title: Optional[str]
        series_is_watched: Optional[bool]
        user_id: str
        cover_url: str
        tags: List[str]
        caption: str
        cdate: str
        rating: NovelRating
        text: str
        marker: Optional[str]
        illusts: Union[list, dict] # list when empty, dict with content
        images: Union[list, dict] # as above
        series_navigation: Union[NovelNavigationInfo, EmptyObject, None]
        glossary_items: Union[list, dict] # as above
        replaceable_item_ids: List[str]
        ai_type: int
        is_original: bool

    # Apply the patch
    models.WebviewNovel = PatchedWebviewNovel
    logger.info("Applied monkey patch for WebviewNovel (permissive mode)")

except Exception as e:
    logger.error(f"Failed to apply monkey patch for WebviewNovel: {e}")

# --- Monkey Patch for AppPixivAPI.novel_ranking ---
try:
    from pixivpy3 import AppPixivAPI
    from pixivpy3.aapi import _MODE, _FILTER, DateOrStr, ParsedJson
    
    def novel_ranking(
        self,
        mode: _MODE = "day_r18",
        filter: _FILTER = "for_ios",
        date: DateOrStr | None = None,
        offset: int | str | None = None,
        req_auth: bool = True,
    ) -> ParsedJson:
        url = f"{self.hosts}/v1/novel/ranking"
        params: dict[str, Any] = {
            "mode": mode,
            "filter": filter,
        }
        if date:
            params["date"] = self._format_date(date)
        if offset:
            params["offset"] = offset
        r = self.no_auth_requests_call("GET", url, params=params, req_auth=req_auth)
        return self.parse_result(r)

    AppPixivAPI.novel_ranking = novel_ranking
    logger.info("Applied monkey patch for AppPixivAPI.novel_ranking")
except Exception as e:
    logger.error(f"Failed to apply monkey patch for AppPixivAPI.novel_ranking: {e}")


# --- Monkey Patch for UserInfoDetailed ---
try:
    from typing import Optional
    class PatchedProfile(BasePixivpyModel):
        webpage: Optional[str]
        gender: str
        birth: str
        birth_day: str
        birth_year: int
        region: str
        address_id: int
        country_code: str
        job: str
        job_id: int
        total_follow_users: int
        total_mypixiv_users: int
        total_illusts: int
        total_manga: int
        total_novels: int
        total_illust_bookmarks_public: int
        total_illust_series: int
        total_novel_series: int
        background_image_url: Optional[str]
        twitter_account: str
        twitter_url: Optional[str]
        pawoo_url: Optional[str]
        is_premium: bool
        is_using_custom_profile_image: bool
    
    # Apply the patch
    models.Profile = PatchedProfile
    logger.info("Applied monkey patch for Profile (permissive mode)")

except Exception as e:
    logger.error(f"Failed to apply monkey patch for Profile: {e}")


# --- Monkey Patch for UserInfoDetailed ---
try:
    from pixivpy3.models import UserInfo, ProfilePublicity, Workspace
    from typing import Optional
    class PatchedUserInfoDetailed(BasePixivpyModel):
        user: Optional[UserInfo]
        profile: Optional[PatchedProfile]
        profile_publicity: Optional[ProfilePublicity]
        workspace: Optional[Workspace]
    
    # Apply the patch
    models.UserInfoDetailed = PatchedUserInfoDetailed
    logger.info("Applied monkey patch for Profile (permissive mode)")

except Exception as e:
    logger.error(f"Failed to apply monkey patch for Profile: {e}")


