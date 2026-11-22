"""Layer 3 integration tests: POST /api/library/uploads HTTP contract."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.factories import make_simple_epub, make_test_app

from copixiv.features.library import api as library_api


def test_library_uploads_http_contract(
    db_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app = make_test_app(db_session_factory, tmp_path)
    epub_bytes = make_simple_epub("HTTP测试EPUB", "HTTP作者", "这是HTTP上传的EPUB正文")
    txt_bytes = "这是HTTP上传的TXT正文内容".encode()

    with TestClient(app, base_url="http://localhost") as client:
        # 1. Missing files -> 400
        no_files = client.post("/api/library/uploads")
        assert no_files.status_code == 400

        # 2. Invalid author_id -> 400 (non-positive) / 422 (non-int)
        bad_author = client.post(
            "/api/library/uploads",
            files=[("files", ("a.txt", txt_bytes, "text/plain"))],
            data={"author_id": "0"},
        )
        assert bad_author.status_code == 400

        non_int_author = client.post(
            "/api/library/uploads",
            files=[("files", ("a.txt", txt_bytes, "text/plain"))],
            data={"author_id": "abc"},
        )
        assert non_int_author.status_code == 422

        # 3. Mixed batch (TXT + EPUB + invalid) -> 200 with per-item statuses
        resp = client.post(
            "/api/library/uploads",
            files=[
                ("files", ("《第一本》作者：甲.txt", txt_bytes, "text/plain")),
                ("files", ("book.epub", epub_bytes, "application/epub+zip")),
                ("files", ("broken.epub", b"not-a-zip", "application/epub+zip")),
            ],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] == 2
        assert body["duplicate"] == 0
        assert body["failed"] == 1
        assert [r["status"] for r in body["results"]] == ["created", "created", "failed"]
        assert all(r["novel_id"] < 0 for r in body["results"][:2])

        # 4. Duplicate TXT with preset author_id -> 200 duplicate
        dup_resp = client.post(
            "/api/library/uploads",
            files=[("files", ("dup.txt", txt_bytes, "text/plain"))],
            data={"author_id": "9001"},
        )
        assert dup_resp.status_code == 200
        dup_body = dup_resp.json()
        assert dup_body["created"] == 0
        assert dup_body["duplicate"] == 1
        assert dup_body["results"][0]["status"] == "duplicate"

        # 5. Batch total byte limit -> 400
        monkeypatch.setattr(library_api, "MAX_BATCH_UPLOAD_BYTES", 10)
        too_big_batch = client.post(
            "/api/library/uploads",
            files=[
                ("files", ("a.txt", b"123456", "text/plain")),
                ("files", ("b.txt", b"123456", "text/plain")),
            ],
        )
        assert too_big_batch.status_code == 400
