from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from web_api.endpoints import novels

app = FastAPI(title="Novel Database API")

# Configure CORS for frontend development
origins = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173", # Vite default port
    "http://127.0.0.1:5173",
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

@app.get("/")
def read_root():
    return {"message": "Welcome to Novel Database API"}

if __name__ == "__main__":
    import uvicorn
    # Run the application using uvicorn
    # Reload=True for development to auto-restart on code changes
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
