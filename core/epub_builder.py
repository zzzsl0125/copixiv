import io
import logging
import re
from pathlib import Path
from typing import Dict, Optional, List

from PIL import Image
from ebooklib import epub

from core.util import safe_filename, HAS_IMAGE_PATTERN

logger = logging.getLogger(__name__)

def _add_image_to_epub(image_path: Path, image_id: str, book: epub.EpubBook, compress_quality: int = 75) -> bool:
    """将图片添加到EPUB书籍，并进行压缩"""
    if not image_path.exists():
        logger.warning(f"Image not found: {image_path}")
        return False

    try:
        # 尝试打开并处理图片
        with Image.open(image_path) as pil_img:
            img_format = pil_img.format.lower() if pil_img.format else 'jpeg'
            
            # 统一转换为 RGB 模式的 JPEG 以确保兼容性并压缩
            # 如果原图是 PNG 且带透明通道，转换为 RGB 白色背景或保留 PNG
            # 这里选择统一转 JPEG 节省体积
            if pil_img.mode in ('RGBA', 'LA') or (pil_img.mode == 'P' and 'transparency' in pil_img.info):
                 # 处理透明背景，创建一个白色背景
                background = Image.new('RGB', pil_img.size, (255, 255, 255))
                background.paste(pil_img, mask=pil_img.split()[-1]) # 使用 alpha 通道作为 mask
                pil_img = background
                img_format = 'jpeg'
            elif pil_img.mode != 'RGB':
                pil_img = pil_img.convert('RGB')
                img_format = 'jpeg'
            
            # 即使原图是 JPEG，也重新保存以应用压缩质量
            if img_format in ['jpeg', 'jpg']:
                buffer = io.BytesIO()
                pil_img.save(buffer, format='JPEG', quality=compress_quality, optimize=True)
                image_data = buffer.getvalue()
                media_type = 'image/jpeg'
                file_ext = '.jpg'
            else:
                # 其他格式（如不需要压缩的情况，虽然上面逻辑基本都会转 JPEG）
                buffer = io.BytesIO()
                pil_img.save(buffer, format=img_format)
                image_data = buffer.getvalue()
                media_type = f'image/{img_format}'
                file_ext = f'.{img_format}'

        epub_image = epub.EpubImage()
        epub_image.file_name = f'images/{image_id}{file_ext}'
        epub_image.media_type = media_type
        epub_image.content = image_data
        
        # 检查是否已存在（避免重复添加）
        if book.get_item_with_href(epub_image.file_name):
             return True
             
        book.add_item(epub_image)
        return True

    except Exception as e:
        logger.error(f"Failed to process image {image_path.name}: {e}")
        return False

def _process_images_for_epub(content: str, image_map: Dict[str, Path], book: epub.EpubBook, compress_quality: int = 75) -> str:
    """
    处理内容中的图片占位符，将图片添加到 EPUB 并替换占位符。
    """
    processed_ids = set()

    def replace_match(match):
        # image_type = match.group(1)
        image_id = match.group(2)
        
        if image_path := image_map.get(image_id):
            if _add_image_to_epub(image_path, image_id, book, compress_quality):
                processed_ids.add(image_id)
            else:
                return match.group(0) # 添加失败保留原样
            return f'<div class="illust-container"><img src="images/{image_id}.jpg" alt="Image {image_id}" class="illust" /></div>'
            
        return match.group(0) # 未找到映射，保留原样

    return HAS_IMAGE_PATTERN.sub(replace_match, content)

def _set_cover_image(book: epub.EpubBook, cover_path: Optional[Path]):
    """设置 EPUB 的封面图片"""
    if not cover_path or not cover_path.exists():
        return

    try:
        # 读取并作为封面设置
        # set_cover 会自动处理 content.opf 中的 meta
        with open(cover_path, 'rb') as f:
            book.set_cover("cover.jpg", f.read())
        logger.debug(f"Set {cover_path.name} as EPUB cover.")
    except Exception as e:
        logger.error(f"Failed to set cover: {e}")

def create_epub(data: dict, compress_quality: int = 75) -> bool:
    
    path_str = data.get("path")
    if not path_str:
        logger.error("No path provided in data for EPUB creation")
        return False
        
    novel_path = Path(path_str)
    if not novel_path.exists():
        logger.error(f"Source text file not found: {novel_path}")
        return False

    title = data.get("title", "Untitled")
    # 如果没有提供作者名，尝试从 data 中获取 user_name (有些 API 响应可能是 user_name)
    author_name = data.get("author_name") or str(data.get("author_id", "Unknown Author"))
    novel_id = str(data.get("id"))

    book = epub.EpubBook()
    book.set_identifier(novel_id)
    book.set_title(title)
    book.set_language('zh')
    book.add_author(author_name)

    parent_dir = novel_path.parent
    
    # 1. 设置封面
    cover_path = None
    # 优先查找下载的封面
    for ext in ['.jpg', '.png', '.jpeg']:
        potential = parent_dir / f"{novel_id}_c_cover{ext}"
        if potential.exists():
            cover_path = potential
            break
    _set_cover_image(book, cover_path)

    # 2. 读取文本内容
    try:
        with open(novel_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        logger.error(f"Failed to read novel text: {e}")
        return False

    if not content.strip():
        logger.warning(f"Content is empty for {novel_path}, skipping EPUB creation.")
        return False

    # 3. 构建图片映射 (ID -> Path)
    # 扫描目录查找属于该小说的图片
    image_map = {}
    
    # 优化：不遍历所有文件，只查找 data 中记录的图片
    # 如果 data 中有 images/illusts 信息，优先使用
    known_images = []
    
    if "images" in data and isinstance(data["images"], dict):
         known_images.extend([(k, 'u') for k in data["images"].keys()])
    
    if "illusts" in data and isinstance(data["illusts"], dict):
         known_images.extend([(k, 'p') for k in data["illusts"].keys()])
         
    # 尝试查找已知图片
    for img_id, img_type in known_images:
        for ext in ['.jpg', '.png', '.jpeg', '.gif']:
            # 命名规则: {novel_id}_{type}_{img_id}{ext}
            img_path = parent_dir / f"{novel_id}_{img_type}_{img_id}{ext}"
            if img_path.exists():
                image_map[img_id] = img_path
                break
    
    # 如果 data 中没有详细图片信息，回退到目录扫描（保持兼容性）
    if not image_map:
        for file in parent_dir.iterdir():
            if file.name.startswith(f"{novel_id}_") and file.suffix.lower() in ['.jpg', '.png', '.jpeg', '.gif']:
                parts = file.stem.split('_')
                # Expect: [novel_id, type, id, ...]
                if len(parts) >= 3:
                    img_type_part = parts[1]
                    img_id_part = parts[2]
                    if img_type_part in ('u', 'p'):
                        image_map[img_id_part] = file

    # 4. 处理内容中的图片引用
    processed_content = _process_images_for_epub(content, image_map, book, compress_quality)

    # 5. 构建 HTML 内容
    # 将换行符转换为 <br/>，并包装在 HTML 结构中
    # 简单的文本转 HTML 处理
    html_body = processed_content.replace('\n', '<br/>')
    
    html_content = f'''
    <html>
        <head>
            <title>{title}</title>
            <link rel="stylesheet" type="text/css" href="style/nav.css" />
        </head>
        <body>
            <h1>{title}</h1>
            <p class="author">Author: {author_name}</p>
            <hr/>
            {html_body}
        </body>
    </html>
    '''

    main_content = epub.EpubHtml(title="正文", file_name='content.xhtml', lang='zh')
    main_content.content = html_content
    book.add_item(main_content)

    # 6. 设置 TOC 和导航
    book.toc = [main_content]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # 7. 添加 CSS
    style = '''
    body { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; margin: 5%; text-align: justify; }
    h1 { text-align: center; }
    .author { text-align: center; font-style: italic; margin-bottom: 2em; }
    .illust-container { text-align: center; margin: 1em 0; }
    .illust { max-width: 100%; height: auto; }
    .cover-container { text-align: center; height: 100%; display: flex; justify-content: center; align-items: center; }
    .cover-image { max-width: 100%; max-height: 100%; object-fit: contain; }
    '''
    nav_css = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=style)
    book.add_item(nav_css)

    # 构建书脊
    spine = ['nav']

    # 如果有封面，创建封面页
    if cover_path and cover_path.exists():
        # cover.jpg 已经在 set_cover 中被添加了，通常 file_name="cover.jpg"
        # 我们可以创建一个专门的封面 HTML 页
        cover_page_content = '''
        <html>
            <head>
                <title>Cover</title>
                <link rel="stylesheet" type="text/css" href="style/nav.css" />
            </head>
            <body>
                <div class="cover-container">
                    <img src="cover.jpg" alt="Cover" class="cover-image" />
                </div>
            </body>
        </html>
        '''
        cover_page = epub.EpubHtml(title="封面", file_name="cover_page.xhtml", lang='zh')
        cover_page.content = cover_page_content
        book.add_item(cover_page)
        spine.append(cover_page)
        
        # 将封面页也添加到目录
        book.toc.insert(0, cover_page)

    spine.append(main_content)
    book.spine = spine

    # 8. 写入文件
    safe_title_str = safe_filename(title)
    output_path = parent_dir / f"{safe_title_str}_{novel_id}.epub"
    
    try:
        epub.write_epub(output_path, book, {})
        logger.info(f"Successfully created EPUB: {output_path.name}")
        # data['has_epub'] = True
        # 此处设值无效，需要额外统一扫描入库
        return True
    except Exception as e:
        logger.error(f"Failed to write EPUB file: {e}")
        return False
