from typing import Annotated
from sqlmodel import SQLModel, Session, create_engine
from fastapi import Depends
from config import settings

engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10
)


#def create_db_and_tables():
#    """Create all SQLModel table models in the Supabase database on startup."""
#   SQLModel.metadata.create_all(engine)


def get_session():
    """FastAPI dependency — yields one Session per request, closes automatically."""
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
