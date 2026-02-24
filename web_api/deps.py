from typing import Generator
from core.db.database import db

def get_db() -> Generator:
    """Dependency injection for database session."""
    yield from db.get_db()
