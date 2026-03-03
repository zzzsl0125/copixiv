import asyncio
from pathlib import Path
from datetime import datetime, timedelta

from core.pixiv_client import PixivClient
from core.db.database import db
from core.logger import logger

# add _ prefix to avoid treated as task function.
from core.novel_handler import handle_from_webview as _handle_from_webview
from core.novel_handler import handle_from_novelInfo as _handle_from_novelInfo
from core.util import is_chinese as _is_chinese, build_path as _build_path

client = PixivClient()

def _batch_upsert(novels: list[dict], commit: bool = True) -> int:
    with db.get_session(commit) as session:
        new_count = db.novel(session).upsert_novels(novels) or 0
        db.author(session).update_summary({n['author_id'] for n in novels})
        db.series(session).update_summary({sid for n in novels if (sid := n.get('series_id'))})
        return new_count

async def _batch_handle(
        novels: list[dict], 
        redownload: bool = False,    # 强制重新下载文件
        sync_author: bool = True,    # 自行获取缺失的作者名
) -> int:

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
            
    new_count = _batch_upsert(valid_resp)

    if sync_author:
        await sync_empty_authors()
        
    return new_count

async def novel_follow(days: int = 3) -> int:
    async with client.account_rule(force_account="carriegriggs78@gmail.com"):
        fetch_til = datetime.now().astimezone() - timedelta(days=days)
        resp = await client.novel_follow(fetch_til=fetch_til)
    return await _batch_handle([n for n in resp.novels if _is_chinese(n)])

async def novel_ranking(mode: str = 'day_r18', days: int = 3) -> int:
    total_new = 0
    for delta in range(1, max(2, days)):
        target = datetime.now().astimezone() - timedelta(days=delta)
        resp = await client.novel_ranking(mode=mode, date=target, fetch_all=True)
        total_new += await _batch_handle([n for n in resp.novels if _is_chinese(n)])
    return total_new
    
async def novel_search(keyword: str = 'R-18', months: int = 1, minlike: int = 500) -> int:

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

async def author_fetch(author_id: int) -> int:
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

async def sync_empty_authors():
    with db.get_session() as session:
        empty_author_ids = db.author(session).get_empty_author_ids()

    for author_id in empty_author_ids:
        if author_id is None: continue
        
        with db.get_session() as session:
            author_name = db.author(session).get_author_name(author_id)
        
        if not author_name:
            try:
                await _author_follow(author_id)
                resp = await client.user_detail(author_id)
                author_name = resp.user.name
            except Exception as e:
                logger.error(f'Failed to sync author {author_id}: {e}')
            
        with db.get_session(True) as session:
            db.author(session).update_author_name(author_id, author_name)

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

async def batch_author_process(limit: int = 1000):
    file_path = Path('/home/invocation/copixiv/author_ids.txt')
    if not file_path.exists(): 
        return logger.error(f'{file_path} not found.')

    with open(file_path, 'r') as f:
        lines = f.readlines()

    processed_count = 0
    for i, line in enumerate(lines):
        if processed_count >= limit:
            break
            
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
            
        try:
            aid = int(stripped)
        except ValueError:
            continue

        logger.info(f"Executing task for author ID: {aid}")
        success = False
        try:
            await author_fetch(aid)
            logger.info(f"Successfully finished author {aid}")
            success = True
        except Exception as e:
            logger.error(f"Error checking author {aid}: {e}")
        
        if success:
            lines[i] = f"# {line}"
            with open(file_path, 'w') as f:
                f.writelines(lines)
            logger.info(f"Marked author {aid} as completed.")
        else:
            lines.append(line)
            lines[i] = ""
            with open(file_path, 'w') as f:
                f.writelines(lines)
            logger.info(f"Moved failed author {aid} to end of file.")
            
        processed_count += 1

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