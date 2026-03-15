from typing import List, Optional
from core.db.database import db
from core.db import models
from core.logger import logger
from core.pixiv_account import TokenInfo

class TokenManager:
    @staticmethod
    def get_valid_tokens() -> List[TokenInfo]:
        tokens = []
        try:
            with db.get_session() as session:
                db_tokens = session.query(models.Token).filter_by(valid=True).all()
                for t in db_tokens:
                    tokens.append(TokenInfo(
                        token=t.token,
                        username=t.name,
                        special=t.special,
                        premium=t.premium,
                        valid=t.valid
                    ))
            return tokens
        except Exception as e:
            logger.error(f"Error loading tokens from database: {e}")
            return []

    @staticmethod
    def mark_invalid(username: str):
        try:
            with db.get_session() as session:
                token_record = session.query(models.Token).filter_by(name=username).first()
                if token_record:
                    token_record.valid = False
                    logger.info(f"Marked token for user {username} as invalid in database")
        except Exception as e:
            logger.error(f"Error marking token invalid for {username}: {e}")

token_manager = TokenManager()
