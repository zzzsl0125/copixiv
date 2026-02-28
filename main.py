from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from core.task_manager import task_executor, scheduler
from web_api.endpoints import novels, tasks, system

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

# Configure CORS for frontend development
origins = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173", # Vite default port
    "http://127.0.0.1:5173",
    "*" # Allow LAN access for development
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(novels.router, prefix="/api/novels", tags=["novels"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(system.router, prefix="/api/system", tags=["system"])

if __name__ == "__main__":
    import uvicorn
    # Run the application using uvicorn
    # Reload=True for development to auto-restart on code changes
    uvicorn.run("main:app", host="0.0.0.0", port=9000) # for dev: reload=True
