import asyncio
from datetime import datetime, timedelta

from core.pixiv_client import PixivClient
from core.db.database import db
from core.logger import logger

# add _ prefix to avoid treated as task function.
from core.novel_handler import handle_from_webview as _handle_from_webview
from core.novel_handler import handle_from_novelInfo as _handle_from_novelInfo
from core.util import is_chinese as _is_chinese

client = PixivClient()

def _batch_upsert(novels: list[dict], commit: bool = True) -> int:
    with db.get_session(commit) as session:
        new_count = db.novel(session).upsert_novels(novels) or 0
        db.author(session).update_summary({n['author_id'] for n in novels})
        db.series(session).update_summary({sid for n in novels if (sid := n.get('series_id'))})
        return new_count

async def _batch_handle(novels: list[dict], redownload: bool = False, sync_author: bool = True) -> int:

    ids = {n.id for n in novels}

    with db.get_session(False) as session:
        existing = db.novel(session).get_existing_ids(ids)
    
    if not redownload:
        need_download = ids - existing
        only_upsert = existing
    else:
        need_download = ids
        only_upsert = set()
        
    _batch_upsert([_handle_from_novelInfo(n) for n in novels if n.id in only_upsert])

    resp = await asyncio.gather(
        *[client.webview_novel(id) for id in need_download], 
        return_exceptions=True
    )
    
    valid_resp = []
    for id, res in zip(need_download, resp):
        if isinstance(res, Exception):
            logger.error(f"Failed to download novel {id}: {res}")
            continue
        try:
            valid_resp.append(_handle_from_webview(res))
        except Exception as e:
            logger.error(f"Failed to handle novel {id}: {e}")
            
    new_count = _batch_upsert(valid_resp)

    if sync_author:
        await _sync_empty_authors()
        
    return new_count

from core.util import build_path
async def _temp_batch_handle_for_recovery(novels: list[dict]):
    ids = [n.id for n in novels]
    paths = [build_path(n.id, n.title) for n in novels]

    needed = []
    for i, (id, path) in enumerate(zip(ids, paths)):
        if Path(path).exists():
            if n := _handle_from_novelInfo(novels[i]):
                _batch_upsert([n])
        else:
            needed.append(id)

    resp = await asyncio.gather(*[client.webview_novel(id) for id in needed])
    _batch_upsert([_handle_from_webview(n) for n in resp])

async def _temp_author_fetch_for_recovery(author_id: int):
    resp = await client.user_novels(author_id, fetch_all=True)
    await _temp_batch_handle_for_recovery(resp.novels)

async def novel_follow(days: int = 3) -> int:
    if days > 10:
        raise Exception('Process in batches for security. e.g. 30 = 10 + 20 + 30')
    
    async with client.force_account(2):
        fetch_til = datetime.now().astimezone() - timedelta(days=days)
        resp = await client.novel_follow(fetch_til=fetch_til)
    
    return await _batch_handle([n for n in resp.novels if _is_chinese(n)])

async def novel_ranking(days: int = 3) -> int:
    if days > 10:
        raise Exception('Process in smaller batches for security.')
    elif days < 2:
        raise Exception('Today\'s ranking maybe empty.')

    total_new = 0
    for delta in range(1, days):
        target = datetime.now().astimezone() - timedelta(days=delta)
        resp = await client.novel_ranking(date=target, fetch_all=True)
        total_new += await _batch_handle([n for n in resp.novels if _is_chinese(n)])
    return total_new
    
async def author_fetch(author_id: int) -> int:
    resp = await client.user_novels(author_id, fetch_all=True)
    return await _batch_handle(resp.novels)

async def author_check(author_id: int, author_name: str | None = None):
    async with client.force_account(2):
        await client.user_follow_add(author_id)

    if not author_name:
        resp = await client.user_detail(author_id)
        try:
            author_name = resp.user.name
        except Exception as e:
            logger.error(f'Failed to check author ({author_id})')
    with db.get_session(True) as session:
        db.author(session).update_author_name(author_id, author_name)

async def author_delete(author_id: int):
    with db.get_session(True) as session:
        db.author(session).delete_author_and_data(author_id)

async def _sync_empty_authors():
    with db.get_session() as session:
        empty_author_ids = db.author(session).get_empty_author_ids()

    for author_id in empty_author_ids:
        with db.get_session() as session:
            author_name = db.author(session).get_author_name(author_id)

        if author_name and author_name.strip():
            with db.get_session(True) as session:
                db.author(session).update_author_name(author_id, author_name)
        else:
            await author_check(author_id)

from pathlib import Path
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

async def novel_search(keyword: str = 'R-18', months: int = 2, minlike: int = 500) -> int:
    if months < 2:
        raise Exception('Process in smaller batches for security.')
    
    end = datetime.now() - timedelta(days=1)
    start = end - timedelta(days=months * 30)
    
    start_date = datetime(start.year, start.month, start.day)
    end_date = datetime(end.year, end.month, end.day)

    resp = await client.search_novel(
        keyword, 'keyword', 'popular_desc', 
        start_date=start_date, end_date=end_date,
        fetch_minlike=minlike,
    )
    return await _batch_handle([n for n in resp.novels if _is_chinese(n)])

async def batch_author_process(limit: int = 50):
    file_path = '/home/invocation/copixiv/author_ids.txt'
    
    import os
    if not os.path.exists(file_path):
        from core.logger import logger
        logger.warning(f"{file_path} not found.")
        return

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

        from core.logger import logger
        logger.info(f"Executing task for author ID: {aid}")
        success = False
        try:
            await _temp_author_fetch_for_recovery(author_id=aid)
            await author_check(author_id=aid)
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

    from core.logger import logger
    logger.info(f"Batch author process completed. Processed {processed_count} authors.")

