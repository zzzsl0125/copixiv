import re
from pathlib import Path
from typing import List, Optional, Union

from pixivpy3 import models, PixivError

from core.config import config

class AccountInvalidError(PixivError):
    """账号永久无效（token失效）"""
    pass

class RateLimitError(PixivError):
    """账号被限流"""
    pass

def safe_filename(text: str, max_length: int = 240) -> str:

    # 移除无效字符 (Windows/Linux 通用非法字符)
    # 包含: < > : " / \ | ? * 以及控制字符
    clean_text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
    
    # 替换连续空白字符为一个空格，并去除首尾空白
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    
    if not clean_text:
        return "untitled"
    
    # 编码并截断 (按 UTF-8 字节长度截断，避免截断多字节字符的一半)
    encoded_text = clean_text.encode('utf-8')
    if len(encoded_text) > max_length:
        encoded_text = encoded_text[:max_length]
        # 如果截断点在多字节字符中间，需要回退
        while True:
            try:
                clean_text = encoded_text.decode('utf-8')
                break
            except UnicodeDecodeError:
                encoded_text = encoded_text[:-1]
    else:
        clean_text = encoded_text.decode('utf-8')
            
    return clean_text.strip()

def build_path(id: Union[str, int], name: str) -> str:

    # 从配置获取下载路径，默认为 "downloads"
    download_dir = config.get('path', {}).get('download') or "downloads"
    
    id_str = str(id)
    # 根据 ID 分组目录，避免单目录下文件过多
    # 小于 10,000,000 的 ID 放入 0000 目录 (早期作品)
    # 否则取 ID 前 4 位作为子目录
    subdir = "0000" if int(id_str) < 10_000_000 else id_str.zfill(8)[:4]
    
    filename = f"{safe_filename(name)}_{id_str}.txt"
    return str(Path(download_dir) / subdir / filename)

def guess_series_order(navigation: Union[models.EmptyObject, dict, None]) -> Optional[int]:

    try:
        navigation: dict = navigation.model_extra
    except Exception:
        return None
    
    if prev_novel := navigation.get('prevNovel'):
        return int(prev_novel.get('contentOrder')) + 1
    
    if next_novel := navigation.get('nextNovel'):
        return int(next_novel.get('contentOrder')) - 1
    
    return None

CLEAN_PATTERN = re.compile(r"[()]")
SPLIT_PATTERN = re.compile(r"[/|#、\\]")
def parse_tags(tags: List[Union[str, dict]]) -> List[str]:

    result = set()
    
    for tag in tags:
        # 处理可能的标签对象 (pixiv API 有时返回对象列表 {'name': 'tag', ...})
        tag_text = tag.get('name', '') if isinstance(tag, dict) else str(tag)
        
        # 移除括号内容
        cleaned_tag = CLEAN_PATTERN.sub('', tag_text)
        
        # 分割多重标签
        for part in SPLIT_PATTERN.split(cleaned_tag):
            part = part.strip().lower()
            if part: result.add(part)

    return list(result)
    
import langid
JAPANESE_REGEX = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')
CHINESE_TAG_KEYWORDS = {
    "中文", "中文作品", "中文注意", "简中", "简体中文", "繁体", "繁體", "中文中国语",
    "中国文", "中文語", "中文语", "中国语", "中国語", "中阈语", "中國语", "中國語",
    "中国語注意", "中國語注意", "中国语注意", "Chinese", "chinese", "中國", "中国", 
}
def is_chinese(data: models.NovelInfo) -> bool:

    if any(tag in CHINESE_TAG_KEYWORDS for tag in parse_tags(data.tags)):
        return True
    
    sample = data.title + data.caption
    if not sample.strip(): return False
    if JAPANESE_REGEX.search(sample): return False

    try:
        return langid.classify(sample)[0] == "zh"
    except Exception:
        return False

HAS_IMAGE_PATTERN = re.compile(r'\[(uploadedimage|pixivimage):([\d\-]+)\]')
def has_image_placeholders(content: str) -> bool:
    """
    检查内容是否包含图片占位符。
    匹配 [uploadedimage:id] 或 [pixivimage:id]
    """
    return bool(re.search(HAS_IMAGE_PATTERN, content))

import importlib
def import_string(dotted_path: str):
    """
    Import a dotted module path and return the attribute/class designated by the
    last name in the path. Raise ImportError if the import failed.
    """
    try:
        module_path, class_name = dotted_path.rsplit('.', 1)
    except ValueError as err:
        raise ImportError("%s doesn't look like a module path" % dotted_path) from err

    module = importlib.import_module(module_path)

    try:
        return getattr(module, class_name)
    except AttributeError as err:
        raise ImportError('Module "%s" does not define a "%s" attribute/class' % (
            module_path, class_name)
        ) from err


