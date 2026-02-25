from fastapi import FastAPI
from nicegui import app as nicegui_app, ui
from fastapi.middleware.cors import CORSMiddleware
from web_api.endpoints import novels
import os

from core.db.database import db
from core.db.repositories.novel import Novel as NovelRepository
from core.db import models

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


# ==========================================
# NiceGUI Web UI
# ==========================================

def get_novel_repo():
    db_session = next(db.get_db())
    return NovelRepository(db_session)

def fetch_novels(keyword=""):
    repo = get_novel_repo()
    queries = {}
    if keyword:
        queries[keyword] = "keyword"
    
    # fetch top 50
    return repo.get_novels(queries=queries, per_page=50)

def download_novel(novel_id):
    # Use repository's epub building method
    repo = get_novel_repo()
    path = repo.get_by_id(models.Novel, novel_id).get('path')
    
    if path and os.path.exists(path):
        ui.download(path)
        ui.notify(f'开始下载: id={novel_id}.epub', type='positive')
    else:
        ui.notify(f'无法生成或找到 id={novel_id} 的文件', type='negative')

@ui.page('/')
def index_page():
    ui.label('我的小说库').classes('text-4xl font-bold mb-6 text-center')
    
    with ui.column().classes('w-full max-w-6xl mx-auto p-4'):
        # Search Bar
        with ui.row().classes('w-full items-center gap-4 mb-8 justify-center'):
            search_input = ui.input(label='搜索小说名称...').classes('w-full max-w-2xl').props('outlined clearable')
            
            def do_search():
                kw = search_input.value or ""
                results = fetch_novels(kw)
                render_cards(results)
                
            ui.button('搜索', on_click=do_search).props('color="primary" icon="search" size="md"')
        
        # Container for cards
        cards_container = ui.row().classes('w-full gap-6 justify-center')
        
        def render_cards(novels_data):
            cards_container.clear()
            with cards_container:
                if not novels_data:
                    ui.label("没有找到相关小说").classes("text-xl text-gray-500 mt-8")
                    return
                    
                for novel in novels_data:
                    novel_id = novel.get('id')
                    title = novel.get('title', '未知书名')
                    author_id = novel.get('author_id', '未知')
                    like_count = novel.get('like', 0)
                    text_count = novel.get('text', 0)
                    desc = novel.get('desc', '暂无简介')
                    tags = novel.get('tags', [])
                    
                    # Ensure desc is a string and truncate
                    if not isinstance(desc, str):
                        desc = ""
                    short_desc = (desc[:60] + '...') if len(desc) > 60 else desc
                    
                    # Create card for each novel
                    with ui.card().classes('w-72 flex flex-col items-start gap-2 p-4 hover:shadow-lg transition-shadow duration-300').style('min-height: 250px;'):
                        # Title
                        ui.label(title).classes('text-xl font-bold line-clamp-1 w-full').tooltip(title)
                        
                        # Author and stats
                        with ui.row().classes('w-full justify-between text-sm text-gray-500'):
                            ui.label(f"作者 ID: {author_id}")
                            ui.label(f"❤️ {like_count}")
                        
                        # Words count
                        ui.label(f"字数: {text_count}").classes('text-xs text-gray-400')
                        
                        # Tags (limit to 3)
                        if tags:
                            with ui.row().classes('w-full gap-1 mt-1'):
                                for tag in tags[:3]:
                                    ui.badge(tag).props('color="grey-4" text-color="grey-9"')
                        
                        ui.separator().classes('w-full mt-2 mb-2')
                        
                        # Description
                        ui.label(short_desc).classes('text-sm text-gray-600 flex-grow')
                        
                        # Action Button
                        ui.button('下载 EPUB', on_click=lambda n_id=novel_id, n_title=title: download_novel(n_id, n_title)).props('color="secondary" size="sm" icon="download"').classes('w-full mt-auto')

        # initial fetch and render
        initial_rows = fetch_novels()
        render_cards(initial_rows)


# Mount NiceGUI to FastAPI
ui.run_with(
    app,
    title="小说库中心",
    storage_secret="my-super-secret-novel-key"
)


if __name__ == "__main__":
    import uvicorn
    # Run the application using uvicorn
    # Reload=True for development to auto-restart on code changes
    uvicorn.run("main:app", host="0.0.0.0", port=9000, reload=True)
