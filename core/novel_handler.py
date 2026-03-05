import threading
import os, re
from pathlib import Path
from typing import Optional, Dict, Any, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pixivpy3 import models
from pixivpy3.utils import ParsedJson

from core.util import build_path, parse_tags, guess_series_order, has_image_placeholders, is_chinese
from core.epub_builder import create_epub
from core.logger import logger

def _get_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    session.mount('http://', HTTPAdapter(max_retries=retries))
    session.mount('https://', HTTPAdapter(max_retries=retries))
    session.headers.update({
        "Referer": "https://www.pixiv.net/",
        "User-Agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)"
    })
    return session

def download_image(url: str, save_path: Path, session: Optional[requests.Session] = None) -> bool:

    if save_path.exists():
        return True
    
    local_session = session or _get_session()
    should_close = session is None
    
    try:
        response = local_session.get(url, stream=True, timeout=10)
        response.raise_for_status()
        
        expected_size = int(response.headers.get('content-length', 0))
        downloaded_size = 0
        
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)
                    
        if expected_size and downloaded_size != expected_size:
            raise Exception(f"Incomplete download: expected {expected_size} bytes, got {downloaded_size} bytes")
            
        return True
    except Exception as e:
        logger.error(f"Failed to download image {url}: {e}")
        # 如果下载失败，尝试删除可能残留的不完整文件
        if save_path.exists():
            try:
                os.remove(save_path)
            except OSError:
                pass
        return False
    finally:
        if should_close:
            local_session.close()

def _download_novel_assets(data: Dict[str, Any]) -> None:
    """
    下载小说的相关资源（封面、插图），并尝试创建 EPUB。
    运行在单独的线程中。
    """
    base_path = Path(data["path"]).parent
    novel_id = str(data["id"])
    images = data.get("images", {})
    illusts = data.get("illusts", {})
    cover_url = data.get("cover_url")

    # 记录下载的文件以便清理
    downloaded_files = []
    session = _get_session()

    try:
        # 1. 下载封面
        if cover_url:
            ext = Path(cover_url).suffix or ".jpg"
            save_path = base_path / f"{novel_id}_c_cover{ext}"
            if download_image(cover_url, save_path, session):
                downloaded_files.append(save_path)

        # 2. 处理 'images' (uploaded images)
        if images:
            for img_id, img_info in images.items():
                urls = img_info.get("urls", {})
                url = urls.get("original") or urls.get("large") or urls.get("medium") or urls.get("small")
                
                if url:
                    ext = Path(url).suffix or ".jpg"
                    save_path = base_path / f"{novel_id}_u_{img_id}{ext}"
                    if download_image(url, save_path, session):
                        downloaded_files.append(save_path)

        # 3. 处理 'illusts' (linked illustrations)
        if illusts:
            for illust_id, illust_info_wrapper in illusts.items():
                # 处理数据结构的差异
                illust_data = illust_info_wrapper.get("illust") if isinstance(illust_info_wrapper, dict) else illust_info_wrapper
                
                if isinstance(illust_data, dict):
                    images_info = illust_data.get("images", {})
                    url = images_info.get("original") or images_info.get("medium") or images_info.get("small")
                    
                    if url:
                        ext = Path(url).suffix or ".jpg"
                        save_path = base_path / f"{novel_id}_p_{illust_id}{ext}"
                        if download_image(url, save_path, session):
                            downloaded_files.append(save_path)
        
        # 4. 创建 EPUB
        # create_epub 会自行读取目录下的图片和文本文件
        if create_epub(data):
            # 如果 EPUB 创建成功，清理下载的图片资源
            for f in downloaded_files:
                try:
                    os.remove(f)
                except OSError as e:
                    logger.warning(f"Failed to remove temporary file {f}: {e}")
                    
    except Exception as e:
        logger.error(f"Error processing assets for novel {novel_id}: {e}")
    finally:
        session.close()

def process_novel_assets(data: Dict[str, Any], force: bool = False) -> None:  
    try:
        path = Path(data["path"]).with_suffix(".epub")
        if path.exists() and not force: 
            logger.info(f'skip assets process for novel {data["id"]}')
            return
        if not data.get("images") and not data.get("illusts"): 
            return 
        
        threading.Thread(
            target=_download_novel_assets, 
            args=(data.copy(),),
            daemon=False
        ).start()
    except Exception as e:
        logger.error(f"Failed to process novel {data.get('id')}: {e}")
    finally:
        data.pop("images", None)
        data.pop("illusts", None)
        data.pop("cover_url", None)
    
def save_novel_text(data: Dict[str, Any], force: bool = False) -> None:
    """
    保存小说文本内容。
    """
    path = Path(data["path"])
    if path.exists() and not force:
        logger.info(f'skip text download for novel {data["id"]}')
        return
        
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data["content"], encoding="utf-8")
        data.pop("content", None)
        logger.info(f'Downloaded: ({data['id']}){data['title']}')
    except Exception as e:
        logger.error(f"Failed to save novel text to {path}: {e}")
        raise

def handle_from_webview(data: ParsedJson, force: bool = False) -> Dict: 
    
    data = {
        "id": data.id,
        "title": data.title,
        "author_id": data.userId,
        "author_name": None,
        "path": build_path(data.id, data.title),
        "like": data.rating.bookmark,
        "view": data.rating.view,
        "text": len(data.text),
        "caption": data.caption,
        "series_id": data.seriesId,
        "series_name": data.seriesTitle,
        "series_index": guess_series_order(data.seriesNavigation),
        "create_time": data.cdate,
        # 0: Cannot/No need, 1: Needs making, 2: Has epub
        "has_epub": 1 if has_image_placeholders(data.text) else 0,
        "tag": parse_tags(data.tags),
        # temp fields for asset processing under
        # need to be pop before upsert to database
        "content": data.text,
        "images": data.images,
        "illusts": data.illusts,
        "cover_url": data.cover_url
    }

    save_novel_text(data, force)
    
    process_novel_assets(data, force)
    
    return data

def handle_from_novelInfo(data: ParsedJson) -> Dict:
    
    return {
        "id": data.id,
        "title": data.title,
        "author_id": data.user.id,
        "author_name": data.user.name,
        "path": build_path(data.id, data.title),
        "like": data.total_bookmarks,
        "view": data.total_view,
        "text": data.text_length,
        "caption": data.caption,
        "series_id": data.series.id if data.series else None,
        "series_name": data.series.title if data.series else None,
        "series_index": data.series.index if data.series.index else None, # manually
        "create_time": data.create_date[:10],
        "has_epub": 0,
        "tag": parse_tags([tag.name for tag in data.tags]),
        "content": None,
        "images": data.image_urls,
        "illusts": None,
        "cover_url": None
    }