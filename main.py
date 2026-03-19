from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from core.task_manager import task_executor, scheduler
from web_api.endpoints import novels, tasks, system, tag_preferences, search_history, tokens, tag_aliases

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start the task executor and scheduler
    task_executor.start()
    scheduler.start()
    yield
    # Shutdown: Stop them gracefully
    scheduler.stop()
    task_executor.stop()

app = FastAPI(title="Novel Database API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(novels.router, prefix="/api/novels", tags=["novels"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(tag_preferences.router, prefix="/api/tag-preferences", tags=["tag_preferences"])
app.include_router(tag_aliases.router, prefix="/api/tag-aliases", tags=["tag_aliases"])
app.include_router(search_history.router, prefix="/api/search-history", tags=["search_history"])
app.include_router(tokens.router, prefix="/api/tokens", tags=["tokens"])

if __name__ == "__main__":
    import uvicorn
    from core.logger import setup_logging
    
    # Run the application using uvicorn
    # Reload=True for development to auto-restart on code changes
     # Disable uvicorn default log config to use our loguru setup
    uvicorn.run("main:app", host="0.0.0.0", port=9000, log_config=None)
