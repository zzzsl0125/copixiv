"""M8 复现：get_novels 的 per_page 无边界 → 负值/超大值导致全表查询。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from copixiv.infrastructure.database.models import Base
from copixiv.web_api.endpoints import novels as novels_endpoint


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    app = FastAPI(title="test")
    app.state.session_factory = factory
    app.include_router(novels_endpoint.router, prefix="/api/novels", tags=["novels"])

    with TestClient(app) as c:
        yield c


def test_per_page_negative_rejected(client):
    r = client.get("/api/novels/", params={"per_page": -1})
    assert r.status_code == 422, f"负 per_page 应 422，实际 {r.status_code}"


def test_per_page_zero_rejected(client):
    r = client.get("/api/novels/", params={"per_page": 0})
    assert r.status_code == 422, f"per_page=0 应 422，实际 {r.status_code}"


def test_per_page_over_cap_rejected(client):
    r = client.get("/api/novels/", params={"per_page": 999999})
    assert r.status_code == 422, f"超大 per_page 应 422，实际 {r.status_code}"


def test_per_page_valid_accepted(client):
    r = client.get("/api/novels/", params={"per_page": 20})
    assert r.status_code == 200, r.text
