from typing import List, Optional, Any, TypeVar, Union
from pixivpy3 import models
from pixivpy3.models import _to_camel
import pydantic

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
    print("Applying monkey patch for WebviewNovel...")

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
    print("Applied monkey patch for WebviewNovel (permissive mode)")

except Exception as e:
    print(f"Failed to apply monkey patch for WebviewNovel: {e}")
