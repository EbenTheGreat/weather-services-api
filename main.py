from fastapi import FastAPI, status
from router import v1
from logging_config import setup_logging
from contextlib import asynccontextmanager
from ai_layer.ai_routes import ai_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Create database tables on startup — runs once before the server starts accepting requests.
    """
    setup_logging()
    #create_db_and_tables()
    yield


app = FastAPI(
    title="Weather Bookmark API",
    description="API for managing weather bookmarks and weather data",
    lifespan=lifespan
)



app.include_router(v1)
app.include_router(ai_router)

@app.get("/", status_code=status.HTTP_200_OK)
async def root():
    return {"message": "Welcome to the Weather Bookmark API"}

