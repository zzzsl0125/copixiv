"""Tag alias API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from copixiv.web_api.deps import get_db
from copixiv.infrastructure.repositories.tag import TagRepository
from copixiv.web_api.schemas import TagAliasSuggestListResponse

router = APIRouter()


@router.get("/")
async def get_tag_aliases(db: Session = Depends(get_db)):
    repo = TagRepository(db)
    return await repo.get_aliases()


@router.get("/suggest", response_model=TagAliasSuggestListResponse)
async def suggest_tag_aliases(
    limit: int = Query(5, ge=1, le=50),
    offset: int = Query(0, ge=0),
    target_tag: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Suggest alias mappings by finding tags with similar names.

    Tags are ranked by ``reference_count`` descending.  For each
    high-reference tag the endpoint looks for other tags whose
    normalized names are similar and returns them as candidates.
    Tags already participating in an existing alias mapping are
    excluded from the results.
    """
    repo = TagRepository(db)
    return await repo.suggest_aliases(
        limit=limit, offset=offset, target_tag=target_tag,
    )


@router.post("/")
async def create_tag_alias(data: dict, db: Session = Depends(get_db)):
    if data.get("source") == data.get("target"):
        raise HTTPException(status_code=400, detail="原标签不能和目标标签相同")
    repo = TagRepository(db)
    try:
        alias = await repo.create_alias(data)
        await repo.apply_alias_retroactively(data["source"], data["target"])
        db.commit()
        return alias
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.delete("/{alias_id}")
async def delete_tag_alias(alias_id: int, db: Session = Depends(get_db)):
    repo = TagRepository(db)
    try:
        if not await repo.delete_alias(alias_id):
            raise HTTPException(status_code=404)
        db.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e)) from e
