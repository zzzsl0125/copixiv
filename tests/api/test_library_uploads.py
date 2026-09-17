"""「我的上传」上传端点测试 —— ``POST /api/library/uploads``。

权威：``docs/LIBRARY_DESIGN.md`` §4（上传管线 / 响应契约）与 §7（不变量）。

用与 ``create_app()`` 同样的接线（router + DomainError handler + app.state 依赖）
搭一个最小 app，``file_storage`` 指向 ``tmp_path``。响应形状按前端
``frontend/src/types/uploads.ts`` 钉死：逐条结果 + 三个计数；部分成功 200；
无文件 400；失败条目不写任何库表。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from copixiv.app import _domain_error_http_status
from copixiv.config import AppConfig
from copixiv.core.exceptions import DomainError
from copixiv.db.models import Author, FailedNovel, Novel
from copixiv.features.library import api as library
from copixiv.features.library.ingest import (
    DECODE_ERROR,
    UNKNOWN_AUTHOR,
    body_fingerprint,
)
from copixiv.features.novels import api as novels
from copixiv.storage.file_storage import FileStorage

# 四种候选编码（utf-16 必须带 BOM）全部失败的字节流。
UNDECODABLE = b"\xff\x00\xff\x00\xff"


@pytest.fixture(autouse=True)
def _isolated_db(clean_db):
    """共享 PG 测试库，每个测试前清空。"""
    yield


class _FakeTaskManager:
    def run_task(self, name: str, params: dict | None = None) -> int:
        return 1


@pytest.fixture
def download_dir(tmp_path) -> Path:
    return tmp_path / "download"


@pytest.fixture
def client(session_factory, download_dir):
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=_domain_error_http_status(exc),
            content={"detail": exc.detail},
        )

    app.state.session_factory = session_factory
    app.state.config = AppConfig()
    app.state.file_storage = FileStorage(download_dir=str(download_dir))
    app.state.task_manager = _FakeTaskManager()

    app.include_router(library.router, prefix="/api/library", tags=["library"])
    # 顺带挂 novels —— 「我的上传」书架就是它的一次特殊搜索（§5）
    app.include_router(novels.router, prefix="/api/novels", tags=["novels"])

    with TestClient(app) as c:
        yield c


def _upload(client, files: list[tuple[str, bytes]]):
    """multipart，字段名 ``files`` 重复多次（前端的调用形态）。"""
    return client.post(
        "/api/library/uploads",
        files=[("files", (name, data, "text/plain")) for name, data in files],
    )


def _local_rows(session_factory) -> list[Novel]:
    with session_factory() as session:
        return list(session.execute(
            select(Novel).where(Novel.source == "local")
        ).scalars())


# ---------------------------------------------------------------------------
# 响应契约
# ---------------------------------------------------------------------------


def test_created_item_matches_the_wire_contract(client, session_factory):
    response = _upload(client, [("我的书.txt", "第一行\r\n第二行".encode("utf-8"))])

    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 1 and body["duplicate"] == 0 and body["failed"] == 0
    assert len(body["results"]) == 1

    item = body["results"][0]
    # 契约字段一字不差；未用到的可选字段不下发（response_model_exclude_none）
    assert set(item) == {
        "filename", "status", "novel_id", "title", "author_name", "text",
    }
    assert item["filename"] == "我的书.txt"
    assert item["status"] == "created"
    assert item["novel_id"] < 0
    assert item["title"] == "我的书"
    assert item["author_name"] == UNKNOWN_AUTHOR
    assert item["text"] == len("第一行\n第二行")

    row = _local_rows(session_factory)[0]
    assert row.id == item["novel_id"]


def test_partial_success_returns_200_with_per_item_results(client, session_factory):
    response = _upload(client, [
        ("我的书.txt", "我的正文".encode("utf-8")),
        ("dup.txt", "我的正文".encode("utf-8")),
        ("bad.txt", UNDECODABLE),
    ])

    assert response.status_code == 200
    body = response.json()
    assert (body["created"], body["duplicate"], body["failed"]) == (1, 1, 1)

    created, duplicate, failed = body["results"]
    assert created["status"] == "created" and created["novel_id"] < 0
    # duplicate 指出已在库中那本
    assert duplicate["status"] == "duplicate"
    assert duplicate["novel_id"] == created["novel_id"]
    assert duplicate["title"] == created["title"] == "我的书"
    # failed 只有 filename / status / error
    assert failed == {
        "filename": "bad.txt", "status": "failed", "error": DECODE_ERROR,
    }
    # 失败项不写库（§4-7：连 failed_novel 也不写）
    with session_factory() as session:
        assert session.execute(select(FailedNovel)).first() is None
    assert len(_local_rows(session_factory)) == 1


def test_no_files_is_400(client):
    assert client.post("/api/library/uploads").status_code == 400
    assert client.post("/api/library/uploads", files=[]).status_code == 400
    # 带了别的表单字段但没有 files，同样 400
    assert client.post(
        "/api/library/uploads", data={"note": "x"},
    ).status_code == 400


# ---------------------------------------------------------------------------
# 落库形态（§3/§7-3/§7-4/§7-5）
# ---------------------------------------------------------------------------


def test_row_values_and_file_location(client, session_factory, download_dir):
    _upload(client, [("【某某】我的书(完结).txt", "正文内容".encode("utf-8"))])

    row = _local_rows(session_factory)[0]
    assert row.source == "local"
    assert row.source_key == body_fingerprint("正文内容")
    assert row.has_epub == 3                      # §7-4 / §3.3
    assert row.author_name == "某某"              # §7-3（非 NULL）
    assert row.author_id is None and row.series_id is None
    assert row.like == 0 and row.view == 0
    assert row.text == len("正文内容")
    assert row.create_time is not None
    # §7-5：path 落在 download/local/ 下，且文件真在磁盘上
    path = Path(row.path)
    assert path.parent == download_dir / "local"
    assert path.name.startswith("我的书_")
    assert path.read_text(encoding="utf-8") == "正文内容"


def test_undecodable_upload_writes_nothing(client, session_factory):
    response = _upload(client, [("坏.txt", UNDECODABLE)])

    assert response.json()["results"][0]["error"] == DECODE_ERROR
    with session_factory() as session:
        assert session.execute(select(Novel)).first() is None
        assert session.execute(select(FailedNovel)).first() is None


# ---------------------------------------------------------------------------
# §7-7：二次上传被拒，且唯一索引在数据库层生效
# ---------------------------------------------------------------------------


def test_second_upload_is_duplicate_and_unique_index_holds(
    client, session_factory,
):
    _upload(client, [("第一次.txt", "同一份正文".encode("utf-8"))])
    row = _local_rows(session_factory)[0]

    response = _upload(client, [("第二次.txt", "同一份正文".encode("utf-8"))])
    item = response.json()["results"][0]
    assert item["status"] == "duplicate"
    assert item["novel_id"] == row.id
    assert item["title"] == row.title
    assert len(_local_rows(session_factory)) == 1

    # 数据库层：同 (source, source_key) 再来一行直接被 ux_novel_source_key 拒
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO novel (id, title, author_name, path, \"like\", "
                    "view, \"text\", has_epub, shuffle, tags, is_favourite, "
                    "source, source_key) VALUES (:id, '手工复制', '未知作者', "
                    "'download/local/manual.txt', 0, 0, 0, 3, 0, '{}', false, "
                    "'local', :key)"
                ),
                {"id": -999_999_000, "key": row.source_key},
            )
            session.commit()

    # pixiv 行的 source_key 为 NULL —— 部分唯一索引不影响存量
    with session_factory() as session:
        session.execute(
            text(
                "INSERT INTO novel (id, title, author_name, path, \"like\", "
                "view, \"text\", has_epub, shuffle, tags, is_favourite) "
                "VALUES (42, 'pixiv 书', '作者', 'download/0000/x.txt', 0, 0, "
                "0, 0, 0, '{}', false)"
            )
        )
        session.commit()
        assert session.execute(
            select(Novel.source).where(Novel.id == 42)
        ).scalar() == "pixiv"


# ---------------------------------------------------------------------------
# 列表 / 详情响应带上 source（前端卡片据此显示「本地」徽章，§2/§5）
# ---------------------------------------------------------------------------


def test_list_and_detail_carry_source(client, session_factory):
    upload = _upload(client, [("本地书.txt", "本地正文".encode("utf-8"))])
    local_id = upload.json()["results"][0]["novel_id"]
    with session_factory() as session:
        session.add(Author(author_id=42, author_name="作者42"))
        session.flush()
        session.add(Novel(
            id=42, title="pixiv 书", author_id=42, author_name="作者42",
            path="download/0000/x.txt", like=5000, text=8000,
        ))
        session.commit()

    # 「我的上传」书架 = keyword=source:local; + 阈值归零（§5）
    response = client.get("/api/novels/", params={
        "keyword": "source:local;", "min_like": 0, "min_text": 0,
        "exclude_blocked": "false",
    })
    assert response.status_code == 200
    listed = response.json()["novels"]
    assert [n["id"] for n in listed] == [local_id]
    assert listed[0]["source"] == "local"

    # 阈值不归零就是空书架（§5 唯一不能省的一步）—— 服务端不做默认阈值，
    # 前端会把 config.ts 的 500 / 3000 显式带上（App.vue）。
    assert client.get("/api/novels/", params={
        "keyword": "source:local;", "min_like": 500, "min_text": 3000,
        "exclude_blocked": "false",
    }).json()["novels"] == []

    # 详情（by-ids，卡片展开用）同样带 source；pixiv 行是默认值
    detail = client.post("/api/novels/by-ids", json={
        "novel_ids": [local_id, 42],
    }).json()["novels"]
    assert {n["id"]: n["source"] for n in detail} == {
        local_id: "local", 42: "pixiv",
    }
