import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from sqlalchemy import select

from core.pixiv_client import PixivClient
from core.db.database import db
from core.db import models
from core.logger import logger

# add _ prefix to avoid treated as task function.
from core.novel_handler import handle_from_webview as _handle_from_webview
from core.novel_handler import handle_from_novelInfo as _handle_from_novelInfo
from core.util import is_chinese as _is_chinese, build_path as _build_path

client = PixivClient()

def _batch_upsert(novels: list[dict], commit: bool = True, force_update: list[str] = []) -> int:
    with db.get_session(commit) as session:
        new_count = db.novel(session).upsert_novels(novels, force_update) or 0
        db.author(session).update_summary({n['author_id'] for n in novels})
        db.series(session).update_summary({sid for n in novels if (sid := n.get('series_id'))})
        return new_count

async def _batch_handle(
        novels: list[dict], 
        redownload: bool = False,    # 强制重新下载文件
        sync_author: bool = True,    # 自行获取缺失的作者名
) -> list[str]:
    
    if not novels: return []

    ids = {n.id for n in novels}

    # filter from database 
    with db.get_session(False) as session:
        existing = db.novel(session).get_existing_ids(ids)
    
    if not redownload:
        need_download = ids - existing
        only_upsert = existing
    else:
        need_download = ids
        only_upsert = set()

    # directly update to database    
    _batch_upsert([
        _handle_from_novelInfo(n) 
        for n in novels if n.id in only_upsert
    ])

    # filter from file and get text content
    resp = await asyncio.gather(
        *[
            client.webview_novel(n.id) for n in novels if n.id in need_download  
            # and (redownload or not Path(_build_path(n.id, n.title)).exists())
        ], return_exceptions=True
    )
    
    # save text content and download image for epub creation
    valid_resp = []
    for id, res in zip(need_download, resp):
        if isinstance(res, Exception):
            logger.error(f"Failed to download novel {id}: {res}")
            continue
        try:
            valid_resp.append(_handle_from_webview(res, redownload))
        except Exception as e:
            logger.error(f"Failed to handle novel {id}: {e}")
            
    new_titles = [n['title'] for n in valid_resp]
    _batch_upsert(valid_resp)

    if sync_author:
        await sync_empty_name()
        
    return new_titles

async def novel_fetch(id: int, redownload: bool = True):
    resp = await client.webview_novel(id)
    return _batch_upsert([_handle_from_webview(resp, redownload)], True)

async def novel_follow(days: int = 3) :
    async with client.account_rule(force_account="carriegriggs78@gmail.com"):
        fetch_til = datetime.now().astimezone() - timedelta(days=days)
        resp = await client.novel_follow(fetch_til=fetch_til)
    return await _batch_handle([n for n in resp.novels if _is_chinese(n)])

async def novel_ranking(mode: str = 'day_r18', days: int = 3):
    total_new = list()
    for delta in range(1, max(2, days)):
        target = datetime.now().astimezone() - timedelta(days=delta)
        resp = await client.novel_ranking(mode=mode, date=target, fetch_all=True)
        total_new.extend(await _batch_handle([n for n in resp.novels if _is_chinese(n)]))
    return total_new
    
async def novel_search(keyword: str = 'R-18', months: int = 1, minlike: int = 500):

    end = datetime.now() - timedelta(days=1)
    start = end - timedelta(days=max(1, months) * 30)

    start_date = datetime(start.year, start.month, start.day)
    end_date = datetime(end.year, end.month, end.day)

    async with client.account_rule(force_account="1303357668"):
        resp = await client.search_novel(
            keyword, 'keyword', 'popular_desc', 
            start_date=start_date, end_date=end_date,
            fetch_minlike=minlike,
        )
    return await _batch_handle([n for n in resp.novels if _is_chinese(n)])

async def author_fetch(author_id: int):
    resp = await client.user_novels(author_id, fetch_all=True)
    return await _batch_handle(resp.novels)

async def _author_follow(author_id: int):
    async with client.account_rule(
        force_account="carriegriggs78@gmail.com"
    ): return await client.user_follow_add(author_id)

async def author_delete(author_id: int):
    with db.get_session(True) as session:
        db.author(session).delete_author_and_data(author_id)
    
    async with client.account_rule(
        force_account="carriegriggs78@gmail.com"
    ): return await client.user_follow_delete(author_id)

async def sync_empty_name():
    with db.get_session() as session:
        stmt = select(models.Novel).where(models.Novel.author_name.is_(None))
        ids = [n.id for n in session.execute(stmt).scalars().all()]

    novels = list()
    for id in ids:
        resp = await client.novel_detail(id)
        novels.append(_handle_from_novelInfo(resp))
    _batch_upsert(novels)

    with db.get_session() as session:
        db.author(session).update_summary(db.author(session).get_empty_author_ids())
        db.series(session).update_summary(db.series(session).get_empty_series_ids())

async def sync_series_index():
    with db.get_session() as session:
        series = set(db.series(session).series_with_empty_index())
    if not len(series): return 
    logger.info(f'Found {len(series)} series without index.')
    for s in series:
        resp = await client.novel_series(s, fetch_all=True)
        if not resp.novels: continue
        for i in range(len(resp.novels)): 
            resp.novels[i].series['index'] = i + 1
        await _batch_handle(resp.novels)

async def sync_has_epub():
    with db.get_session() as session:
        novels = db.novel(session).get_pending_epub_novels()
        
    if not novels:
        return
        
    completed_ids = []
    for novel_id, path_str in novels:
        if path_str:
            epub_path = Path(path_str).with_suffix(".epub")
            if epub_path.exists():
                completed_ids.append(novel_id)
                
    if completed_ids:
        with db.get_session(True) as session:
            db.novel(session).update_has_epub_status(completed_ids, 2)

async def author_special_follow():
    with db.get_session() as session:
        author_ids = db.author(session).get_special_follow_author_ids()

    if not author_ids:
        logger.info("No special followed authors found.")
        return []
    
    all_novels = []

    for author_id in author_ids:
        # Fetch only the first page, as we only care about the latest novels
        resp = await client.user_novels(author_id)
        if resp.novels:
            all_novels.extend(resp.novels)
        else:
            logger.info(f"No novels found for author {author_id}.")
 
    return await _batch_handle(all_novels)

async def random_pool_renew():
    with db.get_session(True) as session:
        like_tiers = [0, 500, 2500, 5000]
        text_tiers = [0, 3000, 10000, 30000]
        for like in like_tiers:
            for text in text_tiers:
                db.novel(session).populate_random_novel_pool(like, text) 
        
async def rebuild_fts():
    from core.db.repositories.fts_manager import FTSManager
    with db.get_session() as session:
        FTSManager(session).rebuild_novel_fts()