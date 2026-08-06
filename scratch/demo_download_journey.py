"""演示:下载一篇小说的完整旅程(离线模拟版)

真实入口是 DownloadNovelUseCase.execute()(src/copixiv/application/novel/download_novel.py)。
本脚本把五站逐一真实跑一遍,只有两处不碰网络、用替身代替:
  - 第 1 站「取数」:FakePixivClient 代替 PixivClient(真实是 pixivpy3 调 Pixiv API)
  - 第 4 站「图片」:用 PIL 现场生成假图,代替 ImageDownloader 从 i.pximg.net 下载

产物留在 scratch/demo_download/ 下,跑完可以自己去看。
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from copixiv.domain.services.novel_factory import build_from_webview
from copixiv.infrastructure.storage.file_storage import FileStorage
from copixiv.infrastructure.epub.builder import EpubBuilder
from copixiv.infrastructure.database.models import Base, Author, Novel
from copixiv.infrastructure.database.engine import create_session_factory
from copixiv.infrastructure.repositories.novel import SQLAlchemyNovelRepository

NOVEL_ID = 12345678
TITLE = "雨夜的猫"
AUTHOR_ID = 1001
DOWNLOAD_DIR = "scratch/demo_download"


def sep(title: str) -> None:
    print(f"\n{'=' * 68}\n【{title}】\n{'=' * 68}")


# ---------------------------------------------------------------------------
# 第 1 站:取数 —— 真实代码:client.webview_novel(novel_id)
# ---------------------------------------------------------------------------

class FakePixivClient:
    """替身。真实 PixivClient.webview_novel() 的返回是 pixivpy3 的 Novel 对象,
    这里用 SimpleNamespace 造一个结构一模一样的假对象。"""

    async def webview_novel(self, novel_id: int):
        return SimpleNamespace(
            id=novel_id,
            title=TITLE,
            user_id=AUTHOR_ID,
            rating=SimpleNamespace(bookmark=123, view=4567),
            text=(
                "雨夜的猫\n\n"
                "我推开窗,雨声里传来一声轻轻的喵。\n"
                "[uploadedimage:999]\n\n"
                "它蹲在檐下,眼睛像两颗琥珀。\n"
                "[uploadedimage:888]\n\n"
                "我蹲下来,它也蹲下来。我们隔着雨,谁也不先开口。"
            ),
            caption="一个关于猫的小故事",
            series_id=None,          # 不在系列里 → series 相关字段全空
            series_title=None,
            series_navigation=None,
            cdate="2024-05-01 12:34:56",
            tags=["猫", {"name": "日常"}, "短篇/随笔"],
            images={
                "999": {"urls": {"original": "https://i.pximg.net/.../u_999.jpg"}},
                "888": {"urls": {"original": "https://i.pximg.net/.../u_888.jpg"}},
            },
            illusts={},              # 关联插图,这篇没有
            cover_url="https://i.pximg.net/.../cover.jpg",
        )


async def stage1_fetch() -> SimpleNamespace:
    resp = await FakePixivClient().webview_novel(NOVEL_ID)
    print(f"拿到 API 响应: id={resp.id} 标题=「{resp.title}」 "
          f"字符数={len(resp.text)} 标签={resp.tags}")
    return resp


# ---------------------------------------------------------------------------
# 第 2 站:解析 —— 真实代码:build_from_webview(resp, download_dir)
# ---------------------------------------------------------------------------

def stage2_build(resp: SimpleNamespace) -> dict:
    data = build_from_webview(resp, DOWNLOAD_DIR)
    print(f"规范字典: path={data['path']}")
    print(f"           like={data['like']} view={data['view']} "
          f"text={data['text']} has_epub={data['has_epub']}")
    print(f"           标签={data['tag']}")
    print(f"           瞬态字段: content={'有' if data['content'] else '无'} "
          f"images={list(data['images'])} cover_url={'有' if data['cover_url'] else '无'}")
    return data


# ---------------------------------------------------------------------------
# 第 3 站:存正文 —— 真实代码:file_storage.save_novel_text(id, title, content)
# ---------------------------------------------------------------------------

def stage3_save_text(data: dict) -> None:
    storage = FileStorage(DOWNLOAD_DIR)
    content = data.pop("content")   # UseCase 里就是这样:用完即丢
    path = storage.save_novel_text(data["id"], data["title"], content)
    print(f"正文已写盘: {path} ({len(content)} 字符)")
    print(f"           pop 掉 content 后,字典里还剩 {sorted(data.keys())}")


# ---------------------------------------------------------------------------
# 第 4 站:图片 + EPUB —— 真实代码:image_downloader.process_novel_assets()
# 真实流程:ImageDownloader 在线程池里下载图片,然后调 EpubBuilder.create_epub()
# 这里用 PIL 生成假图,模拟「图片已下载好」的磁盘状态,再真实跑 EpubBuilder。
# ---------------------------------------------------------------------------

def _make_fake_image(path: Path, color: tuple, size=(320, 480)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")


def stage4_images_and_epub(data: dict) -> None:
    base = Path(data["path"]).parent  # 图片和 txt/epub 同目录

    # 文件名规则(ImageDownloader 定的):
    #   封面    {novel_id}_c_cover.jpg
    #   内嵌图  {novel_id}_u_{img_id}.jpg
    #   关联图  {novel_id}_p_{illust_id}.jpg
    _make_fake_image(base / f"{NOVEL_ID}_c_cover.jpg", (30, 60, 120))
    _make_fake_image(base / f"{NOVEL_ID}_u_999.jpg", (200, 80, 40), (300, 300))
    _make_fake_image(base / f"{NOVEL_ID}_u_888.jpg", (40, 120, 60), (300, 300))
    print("假图就位(模拟下载完成):")
    for f in sorted(base.iterdir()):
        print(f"   {f.name}")

    ok = EpubBuilder().create_epub(data)
    print(f"EpubBuilder.create_epub() → {ok}")

    epub_path = next(base.glob("*.epub"))
    print(f"EPUB 生成: {epub_path}")

    # EPUB 本质是个 zip 包,直接拆开看内容
    # (ebooklib 打包时把文件都放在 EPUB/ 前缀目录下)
    with zipfile.ZipFile(epub_path) as z:
        names = z.namelist()
        content_xhtml = z.read("EPUB/content.xhtml").decode("utf-8")
    print(f"包内图片文件: {sorted(n for n in names if 'images/' in n)}")
    has_img = '<img src="images/999.jpg"' in content_xhtml
    has_txt = "隔着雨,谁也不先开口" in content_xhtml
    print(f"正文里占位符已替换成 <img>: {has_img}")
    print(f"正文内容完整: {has_txt}")


# ---------------------------------------------------------------------------
# 第 5 站:落库 —— 真实代码:novel_repo.upsert_novels([data])
# 注意:data 还带着 images/illusts/cover_url 这些瞬态字段,
# 仓库层用「表列名白名单」把它们过滤掉,只写真正的列。
# ---------------------------------------------------------------------------

async def stage5_persist(data: dict) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session = create_session_factory(engine)()
    session.add(Author(author_id=AUTHOR_ID, author_name="夜猫子"))
    session.commit()

    repo = SQLAlchemyNovelRepository(session)
    count = await repo.upsert_novels([data])
    print(f"upsert_novels → 新增 {count} 篇")

    row = session.query(Novel).filter_by(id=NOVEL_ID).one()
    print(f"库里查回: title=「{row.title}」 has_epub={row.has_epub}")
    print(f"          like={row.like} view={row.view} path={row.path}")
    print(f"          author_id={row.author_id}")
    session.close()


async def main() -> None:
    sep("第 1 站 / 5:取数  PixivClient.webview_novel()")
    resp = await stage1_fetch()

    sep("第 2 站 / 5:解析  build_from_webview() → 规范字典")
    data = stage2_build(resp)

    sep("第 3 站 / 5:存正文  FileStorage.save_novel_text()")
    stage3_save_text(data)

    sep("第 4 站 / 5:图片 + EPUB  ImageDownloader → EpubBuilder")
    stage4_images_and_epub(data)

    sep("第 5 站 / 5:落库  SQLAlchemyNovelRepository.upsert_novels()")
    await stage5_persist(data)

    sep("旅程结束,磁盘上留下了什么")
    for f in sorted(Path(DOWNLOAD_DIR).rglob("*")):
        if f.is_file():
            print(f"   {f} ({f.stat().st_size} 字节)")


if __name__ == "__main__":
    asyncio.run(main())
