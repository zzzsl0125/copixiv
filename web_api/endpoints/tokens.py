from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from core.db.database import db
from core.db.models import Token
from web_api.deps import get_db
from web_api.schemas import TokenCreate, TokenUpdate, TokenResponse

router = APIRouter()

@router.get("", response_model=List[TokenResponse])
def get_tokens(session: Session = Depends(get_db)):
    """获取所有 Token 列表"""
    tokens = session.query(Token).order_by(Token.sort_index.asc(), Token.id.asc()).all()
    return tokens

@router.post("", response_model=TokenResponse)
def create_token(token_in: TokenCreate, session: Session = Depends(get_db)):
    """新增 Token"""
    existing = session.query(Token).filter(Token.name == token_in.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Token 名字已存在")
    
    new_token = Token(
        name=token_in.name,
        token=token_in.token,
        premium=token_in.premium,
        special=token_in.special,
        valid=token_in.valid
    )
    session.add(new_token)
    session.commit()
    session.refresh(new_token)
    return new_token

@router.put("/{token_id}", response_model=TokenResponse)
def update_token(token_id: int, token_in: TokenUpdate, session: Session = Depends(get_db)):
    """修改 Token"""
    token_obj = session.query(Token).filter(Token.id == token_id).first()
    if not token_obj:
        raise HTTPException(status_code=404, detail="Token 未找到")
        
    if token_in.name is not None:
        existing = session.query(Token).filter(Token.name == token_in.name, Token.id != token_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="Token 名字已被其他记录占用")
        token_obj.name = token_in.name
        
    if token_in.token is not None:
        token_obj.token = token_in.token
    if token_in.premium is not None:
        token_obj.premium = token_in.premium
    if token_in.special is not None:
        token_obj.special = token_in.special
    if token_in.valid is not None:
        token_obj.valid = token_in.valid
        
    session.commit()
    session.refresh(token_obj)
    return token_obj

@router.delete("/{token_id}")
def delete_token(token_id: int, session: Session = Depends(get_db)):
    """删除 Token"""
    token_obj = session.query(Token).filter(Token.id == token_id).first()
    if not token_obj:
        raise HTTPException(status_code=404, detail="Token 未找到")
    session.delete(token_obj)
    session.commit()
    return {"message": "删除成功"}

@router.post("/reorder")
def reorder_tokens(token_ids: List[int], session: Session = Depends(get_db)):
    """更新 Token 排序"""
    try:
        for index, token_id in enumerate(token_ids):
            session.query(Token).filter(Token.id == token_id).update({Token.sort_index: index})
        session.commit()
        return {"message": "排序已更新"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"更新排序失败: {str(e)}")
