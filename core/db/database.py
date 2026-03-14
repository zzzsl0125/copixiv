# -*- coding: utf-8 -*-
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.orm.state import InstanceState
from . import models
from . import constants as C
from .models import Base
from pathlib import Path
from core.config import config

def get_database_url():
    db_path = config.path.database or "database/database.db"    
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"

engine = create_engine(
    get_database_url(),
    connect_args={"check_same_thread": False}, # Needed for SQLite
    pool_size=20, # Optional: only for some DBs, but SQLAlchemy ignores for NullPool (default for SQLite)
    max_overflow=0
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL") # Optional: faster, slightly less safe than FULL
    cursor.execute("PRAGMA busy_timeout=10000") # 10s timeout
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

from .repositories import (
    Novel, Author, Series, 
    System, TaskHistory, 
    ScheduledTask, FTSManager
)

class SessionContextManager:
    def __init__(self, session: Session, commit_on_exit: bool = False):
        self.session = session
        self.commit_on_exit = commit_on_exit

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None and self.commit_on_exit:
                self.session.commit()
            elif exc_type is not None:
                self.session.rollback()
        finally:
            self.session.close()

class Database:
    """
    Manages database connections and provides access to repositories.
    """
    def __init__(self):
        self.SessionLocal = SessionLocal
        self.engine = engine

    def init(self):
        """Initializes the database schema and FTS tables."""
        Base.metadata.create_all(bind=self.engine)
        # with self.get_session() as session:
        #     FTSManager(session).rebuild_novel_fts()
            
    def get_session(self, commit: bool = True):
        """
        Returns a database session.  
        If used as a context manager with commit=True, it automatically commits on exit.
        
        :param commit: Whether to commit on exit (context manager only)
        """
        session = self.SessionLocal()
        if commit:
            return SessionContextManager(session, commit_on_exit=True)
        return session
    
    def get_db(self):
        """Generator for dependency injection (e.g. FastAPI)."""
        session = self.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    @property
    def novel(self) -> type[Novel]:
        return Novel
    
    @property
    def author(self) -> type[Author]:
        return Author

    @property
    def series(self) -> type[Series]:
        return Series

    @property
    def system(self) -> type[System]:
        return System

    @property
    def task_history(self) -> type[TaskHistory]:
        return TaskHistory

    @property
    def scheduled_task(self) -> type[ScheduledTask]:
        return ScheduledTask

db = Database()
db.init()
