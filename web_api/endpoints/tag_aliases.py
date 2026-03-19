from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy.orm import Session
from typing import List
from sqlalchemy import select, desc

from core.db.database import db
from web_api.schemas import TagAliasCreate, TagAliasResponse, TagAliasSuggestResponse, TagAliasSuggestListResponse
from core.db.repositories.novel import Novel
from core.db.models import Tag, TagAlias

router = APIRouter(tags=["Tag Aliases"])

@router.get("/suggest", response_model=TagAliasSuggestListResponse)
def suggest_tag_aliases(
    limit: int = Query(5, ge=1, le=20), 
    offset: int = Query(0, ge=0), 
    target_tag: str | None = Query(None, description="If provided, only get suggestions for this specific target tag"),
    session: Session = Depends(db.get_db)
):
    """获取别名映射建议"""
    # 获取已存在的别名关系
    existing_sources = set(session.scalars(select(TagAlias.source)).all())
    
    suggestions = []
    current_offset = offset
    
    if target_tag:
        # 只针对指定的 target_tag 搜索候选
        tag = session.scalar(select(Tag).where(Tag.name == target_tag))
        if tag and tag.name not in existing_sources:
            first_char = tag.name[0].lower() if tag.name else ""
            if first_char:
                candidates = session.scalars(
                    select(Tag)
                    .where(
                        (Tag.name.ilike(f"{first_char}%")) | 
                        (Tag.name.ilike(f"%{tag.name}%"))
                    )
                    .where(Tag.id != tag.id)
                    .order_by(desc(Tag.reference_count))
                    .limit(50)
                ).all()
                
                valid_candidates = []
                for c in candidates:
                    if c.name not in existing_sources:
                        valid_candidates.append({
                            "id": c.id,
                            "name": c.name,
                            "reference_count": c.reference_count
                        })
                        
                if valid_candidates:
                    suggestions.append({
                        "target": {
                            "id": tag.id,
                            "name": tag.name,
                            "reference_count": tag.reference_count
                        },
                        "candidates": valid_candidates
                    })
        return {
            "items": suggestions,
            "next_offset": 0 # Not paginated when target_tag is specified
        }
        
    # 全局建议逻辑
    existing_targets = set(session.scalars(select(TagAlias.target)).all())
    
    while len(suggestions) < limit:
        # 每次取50个标签进行检查，避免一次性加载所有标签
        tags = session.scalars(select(Tag).order_by(desc(Tag.reference_count)).offset(current_offset).limit(50)).all()
        if not tags:
            break
            
        for tag in tags:
            current_offset += 1
            # 跳过已作为来源的标签，以及已作为目标的标签
            if tag.name in existing_sources or tag.name in existing_targets:
                continue
                
            first_char = tag.name[0].lower() if tag.name else ""
            if not first_char:
                continue
                
            candidates = session.scalars(
                select(Tag)
                .where(
                    (Tag.name.ilike(f"{first_char}%")) | 
                    (Tag.name.ilike(f"%{tag.name}%"))
                )
                .where(Tag.id != tag.id)
                .order_by(desc(Tag.reference_count))
                .limit(50)
            ).all()
            
            valid_candidates = []
            for c in candidates:
                if c.name not in existing_sources:
                    valid_candidates.append({
                        "id": c.id,
                        "name": c.name,
                        "reference_count": c.reference_count
                    })
                    
            if valid_candidates:
                suggestions.append({
                    "target": {
                        "id": tag.id,
                        "name": tag.name,
                        "reference_count": tag.reference_count
                    },
                    "candidates": valid_candidates
                })
                
                if len(suggestions) >= limit:
                    break
                    
    return {
        "items": suggestions,
        "next_offset": current_offset
    }

@router.get("/", response_model=List[TagAliasResponse])
def get_tag_aliases(session: Session = Depends(db.get_db)):
    """获取所有标签别名映射"""
    aliases = db.tag_alias(session).get_all()
    return aliases

@router.post("/", response_model=TagAliasResponse)
def create_tag_alias(
    alias: TagAliasCreate, 
    background_tasks: BackgroundTasks,
    session: Session = Depends(db.get_db)
):
    """
    添加或更新一个标签别名映射。
    在后台异步替换现有小说中包含的原标签为新标签。
    """
    if alias.source == alias.target:
        raise HTTPException(status_code=400, detail="原标签不能和目标标签相同")
        
    try:
        new_alias = db.tag_alias(session).add_alias(alias.source, alias.target)
        session.commit()
        
        # 触发追溯替换 (这可以在后台运行，因为重写标签可能需要一点时间)
        def apply_retroactive(source: str, target: str):
            with db.get_session(commit=True) as bg_session:
                novel_repo = Novel(bg_session)
                novel_repo.apply_tag_alias_retroactively(source, target)
                
        background_tasks.add_task(apply_retroactive, alias.source, alias.target)
        
        return new_alias
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{alias_id}")
def delete_tag_alias(alias_id: int, session: Session = Depends(db.get_db)):
    """删除一个标签别名映射"""
    try:
        db.tag_alias(session).delete_alias(alias_id)
        session.commit()
        return {"message": "删除成功"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
