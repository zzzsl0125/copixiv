"""Layer 2 DB tests: trg_sync_tag_refs triggers, alias guards, retroactive dedup, and Rust tag suggestions."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from copixiv.core.draft import build_novel
from copixiv.core.exceptions import ValidationError
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.ingest import persist_novels
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository


def _tag_count(session, name: str) -> int:
    row = session.execute(
        select(models.Tag.reference_count).where(models.Tag.name == name)
    ).scalar_one_or_none()
    return int(row or 0)


def test_tag_triggers_track_insert_update_delete(uow: SqlUnitOfWork):
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(id=1, title="T1", author_id=1, author_name="A", tags=["alpha", "beta"]),
                build_novel(id=2, title="T2", author_id=1, author_name="A", tags=["alpha"]),
            ],
        )
        assert _tag_count(uow.session, "alpha") == 2
        assert _tag_count(uow.session, "beta") == 1

    # Update tags: remove beta, add gamma on novel 1
    with uow.begin():
        repo = SQLAlchemyNovelRepository(uow.session)
        repo.remove_tags_from_novels([1], {"beta"})
        repo.add_tags_to_novels([1], {"gamma"})
        assert _tag_count(uow.session, "alpha") == 2
        assert _tag_count(uow.session, "beta") == 0
        assert _tag_count(uow.session, "gamma") == 1

    # Delete novel 1 -> alpha drops to 1, gamma drops to 0 and is pruned
    with uow.begin():
        SQLAlchemyNovelRepository(uow.session).delete(1)
        assert _tag_count(uow.session, "alpha") == 1
        assert _tag_count(uow.session, "gamma") == 0
        assert (
            uow.session.execute(select(models.Tag).where(models.Tag.name == "gamma")).scalar_one_or_none()
            is None
        )
        # Trigger must not create any session temp tables (migration 0016+)
        temp_count = uow.session.execute(
            text("SELECT COUNT(*) FROM pg_class WHERE relpersistence = 't' AND relname = '_tag_ref_deltas'")
        ).scalar_one()
        assert temp_count == 0

    # Non-tag UPDATE leaves reference counts untouched (0017 EXCEPT early exit)
    with uow.begin():
        SQLAlchemyNovelRepository(uow.session).toggle_favourite(2)
        assert _tag_count(uow.session, "alpha") == 1


def test_aliased_zero_ref_tag_survives_until_alias_deleted(uow: SqlUnitOfWork):
    with uow.begin():
        persist_novels(
            uow,
            [build_novel(id=20, title="Aliased", author_id=1, author_name="A", tags=["src_alias"])],
        )

    with uow.begin():
        tag_repo = SQLAlchemyTagRepository(uow.session)
        alias = tag_repo.create_alias({"source": "src_alias", "target": "dst_alias"})
        tag_repo.apply_alias_retroactively("src_alias", "dst_alias")

        # src_alias stays because tag_alias references it, even with reference_count == 0
        src_row = uow.session.execute(
            select(models.Tag).where(models.Tag.name == "src_alias")
        ).scalar_one_or_none()
        assert src_row is not None
        assert src_row.reference_count == 0

        # Deleting the alias immediately prunes the unreferenced src_alias row
        assert tag_repo.delete_aliases_by_ids([alias["id"]]) == 1
        assert (
            uow.session.execute(select(models.Tag).where(models.Tag.name == "src_alias")).scalar_one_or_none()
            is None
        )
        assert _tag_count(uow.session, "dst_alias") == 1


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
                    tags=["old_tag", "new_tag", "other"],
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
        assert sorted(n10.tags) == ["new_tag", "other"]
        assert n11 is not None
        assert n11.tags == ["new_tag"]
        assert _tag_count(uow.session, "old_tag") == 0
        assert _tag_count(uow.session, "new_tag") == 2
        assert _tag_count(uow.session, "other") == 1


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
