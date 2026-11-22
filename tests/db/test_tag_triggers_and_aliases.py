"""Layer 2 DB tests: alias guards, retroactive dedup, and Rust lazy TagIndex sync."""

from __future__ import annotations

import pytest

from copixiv.core.draft import build_novel
from copixiv.core.exceptions import ValidationError
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.ingest import persist_novels
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository


def _candidate_counts(tag_repo: SQLAlchemyTagRepository, target_tag: str) -> dict[str, int]:
    res = tag_repo.suggest_aliases(limit=10, offset=0, target_tag=target_tag)
    if not res["items"]:
        return {}
    item = res["items"][0]
    counts = {item["target"]["name"]: item["target"]["reference_count"]}
    for cand in item["candidates"]:
        counts[cand["name"]] = cand["reference_count"]
    return counts


def test_rust_tag_index_tracks_insert_update_delete_without_pg_tag_table(uow: SqlUnitOfWork):
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=1,
                    title="T1",
                    author_id=1,
                    author_name="A",
                    tags=["alpha", "alpha_pair", "beta"],
                ),
                build_novel(
                    id=2,
                    title="T2",
                    author_id=1,
                    author_name="A",
                    tags=["alpha"],
                ),
            ],
        )

    with uow.begin():
        counts = _candidate_counts(SQLAlchemyTagRepository(uow.session), "alpha")
        assert counts == {"alpha": 2, "alpha_pair": 1}

    # Update tags: remove beta & alpha_pair, add alpha_extra on novel 1
    with uow.begin():
        repo = SQLAlchemyNovelRepository(uow.session)
        repo.remove_tags_from_novels([1], {"beta", "alpha_pair"})
        repo.add_tags_to_novels([1], {"alpha_extra"})

    with uow.begin():
        counts = _candidate_counts(SQLAlchemyTagRepository(uow.session), "alpha")
        assert counts == {"alpha": 2, "alpha_extra": 1}

    # Delete novel 1 -> alpha drops to 1, alpha_extra drops to 0 and disappears
    with uow.begin():
        SQLAlchemyNovelRepository(uow.session).delete(1)

    with uow.begin():
        assert (
            SQLAlchemyTagRepository(uow.session).suggest_aliases(limit=5, offset=0, target_tag="alpha")[
                "items"
            ]
            == []
        )
        assert (
            SQLAlchemyTagRepository(uow.session).suggest_aliases(limit=5, offset=0, target_tag="alpha_extra")[
                "items"
            ]
            == []
        )

    # Non-tag UPDATE keeps derived tag counts intact
    with uow.begin():
        SQLAlchemyNovelRepository(uow.session).toggle_favourite(2)
        SQLAlchemyNovelRepository(uow.session).add_tags_to_novels([2], {"alpha_v2"})

    with uow.begin():
        counts = _candidate_counts(SQLAlchemyTagRepository(uow.session), "alpha")
        assert counts == {"alpha": 1, "alpha_v2": 1}


def test_aliased_tags_excluded_until_alias_deleted(uow: SqlUnitOfWork):
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=20,
                    title="Aliased",
                    author_id=1,
                    author_name="A",
                    tags=["src_alias", "dst_alias_pair"],
                ),
                build_novel(
                    id=21,
                    title="Dest",
                    author_id=1,
                    author_name="A",
                    tags=["dst_alias"],
                ),
            ],
        )

    with uow.begin():
        tag_repo = SQLAlchemyTagRepository(uow.session)
        alias = tag_repo.create_alias({"source": "src_alias", "target": "dst_alias"})
        tag_repo.apply_alias_retroactively("src_alias", "dst_alias")

    with uow.begin():
        tag_repo = SQLAlchemyTagRepository(uow.session)
        assert tag_repo.get_aliases() == [{"id": alias["id"], "source": "src_alias", "target": "dst_alias"}]
        assert tag_repo.suggest_aliases(limit=5, offset=0, target_tag="src_alias")["items"] == []
        assert tag_repo.suggest_aliases(limit=5, offset=0, target_tag="dst_alias")["items"] == []

        # Deleting the alias restores dst_alias suggestions while 0-ref src_alias stays absent
        assert tag_repo.delete_aliases_by_ids([alias["id"]]) == 1

    with uow.begin():
        tag_repo = SQLAlchemyTagRepository(uow.session)
        assert tag_repo.get_aliases() == []
        counts = _candidate_counts(tag_repo, "dst_alias")
        assert counts == {"dst_alias": 2, "dst_alias_pair": 1}
        assert tag_repo.suggest_aliases(limit=5, offset=0, target_tag="src_alias")["items"] == []


def test_alias_single_hop_and_cycle_guards(uow: SqlUnitOfWork):
    with uow.begin():
        tag_repo = SQLAlchemyTagRepository(uow.session)
        with pytest.raises(ValidationError, match="不能和目标标签相同"):
            tag_repo.create_alias({"source": "a", "target": "a"})

        tag_repo.create_alias({"source": "a", "target": "b"})

        # Reverse cycle: b -> a
        with pytest.raises(ValidationError, match="循环别名"):
            tag_repo.create_alias({"source": "b", "target": "a"})

        # Multi-hop: b -> c
        with pytest.raises(ValidationError, match="多级别名"):
            tag_repo.create_alias({"source": "b", "target": "c"})

        # Multi-hop: c -> a
        with pytest.raises(ValidationError, match="多级别名"):
            tag_repo.create_alias({"source": "c", "target": "a"})


def test_apply_alias_retroactively_deduplicates_tags_and_updates_counts(uow: SqlUnitOfWork):
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=10,
                    title="Both",
                    author_id=1,
                    author_name="A",
                    tags=["old_tag", "new_tag", "new_tag_pair", "other"],
                ),
                build_novel(
                    id=11,
                    title="OnlyOld",
                    author_id=1,
                    author_name="A",
                    tags=["old_tag"],
                ),
            ],
        )

    with uow.begin():
        tag_repo = SQLAlchemyTagRepository(uow.session)
        affected = tag_repo.apply_alias_retroactively("old_tag", "new_tag")
        assert affected == 2

        n10 = uow.session.get(models.Novel, 10)
        n11 = uow.session.get(models.Novel, 11)
        assert n10 is not None
        assert sorted(n10.tags) == ["new_tag", "new_tag_pair", "other"]
        assert n11 is not None
        assert n11.tags == ["new_tag"]

    with uow.begin():
        tag_repo = SQLAlchemyTagRepository(uow.session)
        assert _candidate_counts(tag_repo, "new_tag") == {"new_tag": 2, "new_tag_pair": 1}
        assert tag_repo.suggest_aliases(limit=5, offset=0, target_tag="old_tag")["items"] == []


def test_suggest_aliases_end_to_end_via_rust_tag_index(uow: SqlUnitOfWork):
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(id=101, title="N1", author_id=1, author_name="A", tags=["百合", "百合漫画"]),
                build_novel(
                    id=102, title="N2", author_id=1, author_name="A", tags=["日常", "%高中生", "%高中生女子"]
                ),
                build_novel(
                    id=103,
                    title="N3",
                    author_id=1,
                    author_name="A",
                    tags=["aliased_src", "aliased_tgt", "aliased_tgt_pair"],
                ),
            ],
        )
    with uow.begin():
        tag_repo = SQLAlchemyTagRepository(uow.session)
        created_alias = tag_repo.create_alias({"source": "aliased_src", "target": "aliased_tgt"})
        alias_id = created_alias["id"]

    with uow.begin():
        tag_repo = SQLAlchemyTagRepository(uow.session)
        # Literal '%' does not act as SQL wildcard
        pct = tag_repo.suggest_aliases(limit=5, offset=0, target_tag="%高中生")
        assert pct["next_cursor"] is None
        assert len(pct["items"]) == 1
        assert [c["name"] for c in pct["items"][0]["candidates"]] == ["%高中生女子"]

        # Aliased source AND target are both excluded from suggestions
        assert tag_repo.suggest_aliases(limit=5, offset=0, target_tag="aliased_src")["items"] == []
        assert tag_repo.suggest_aliases(limit=5, offset=0, target_tag="aliased_tgt")["items"] == []

        # New novel committed -> TagIndex syncs incrementally without restart
        persist_novels(
            uow,
            [build_novel(id=104, title="N4", author_id=1, author_name="A", tags=["百合小説"])],
        )

    with uow.begin():
        yuri = SQLAlchemyTagRepository(uow.session).suggest_aliases(limit=5, offset=0, target_tag="百合")
        cand_names = [c["name"] for c in yuri["items"][0]["candidates"]]
        assert "百合漫画" in cand_names
        assert "百合小説" in cand_names

        # Core bulk delete via delete_aliases_by_ids marks aliases_dirty so TagIndex refreshes
        deleted_count = SQLAlchemyTagRepository(uow.session).delete_aliases_by_ids([alias_id])
        assert deleted_count == 1

    with uow.begin():
        tgt_after = SQLAlchemyTagRepository(uow.session).suggest_aliases(
            limit=5, offset=0, target_tag="aliased_tgt"
        )
        assert len(tgt_after["items"]) == 1
        assert [c["name"] for c in tgt_after["items"][0]["candidates"]] == [
            "aliased_tgt_pair",
            "aliased_src",
        ]

        # needs_rebuild() self-heals on an empty committed transaction and on read entrypoints
        eng = uow.session.info["query_engine"]
        eng.mark_needs_rebuild()
        assert eng.needs_rebuild() is True

    assert eng.needs_rebuild() is False

    with uow.begin():
        eng.mark_needs_rebuild()
        assert eng.needs_rebuild() is True
        res = SQLAlchemyTagRepository(uow.session).suggest_aliases(limit=5, offset=0, target_tag="百合")
        assert len(res["items"]) == 1
        assert eng.needs_rebuild() is False
