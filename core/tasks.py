import asyncio
from core.pixiv_client import PixivClient
from core.db.database import db
from core.novel_handler import handle_from_webview
from datetime import datetime, timedelta

def batch_upsert(novels: list[dict], commit: bool = True):
    with db.get_session(commit) as session:
        db.novel(session).upsert_novels(novels)
        db.author(session).update_summary({n['author_id'] for n in novels})
        db.series(session).update_summary({sid for n in novels if (sid := n.get('series_id'))})

async def fetch_latest_followed_novels(days: int = 3):
    client = PixivClient()
    async with client.force_account(2):
        fetch_til = datetime.now().astimezone() - timedelta(days=days)
        resp = await client.novel_follow(fetch_til=fetch_til)
    resp = await asyncio.gather(*[client.webview_novel(n.id) for n in resp.novels])
    batch_upsert([handle_from_webview(n) for n in resp])
