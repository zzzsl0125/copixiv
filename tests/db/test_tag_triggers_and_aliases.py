"""Layer 2 DB tests: trg_sync_tag_refs triggers, single-hop alias cycle guard, and retroactive dedup."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from copixiv.core.draft import build_novel
from copixiv.core.exceptions import ValidationError
from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.persist import persist_novels
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

    # Delete novel 1 -> alpha drops to 1, gamma drops to 0
    with uow.begin():
        SQLAlchemyNovelRepository(uow.session).delete(1)
        assert _tag_count(uow.session, "alpha") == 1
        assert _tag_count(uow.session, "gamma") == 0


def test_alias_single_hop_and_cycle_guards(uow: SqlUnitOfWork):
    with uow.begin():
        tag_repo = SQLAlchemyTagRepository(uow.session)
        with pytest.raises(ValidationError, match="cannot be the same"):
            tag_repo.create_alias({"source": "a", "target": "a"})

        tag_repo.create_alias({"source": "a", "target": "b"})

        # Reverse cycle: b -> a
        with pytest.raises(ValidationError, match="cycle"):
            tag_repo.create_alias({"source": "b", "target": "a"})

        # Multi-hop: b -> c (target already source? wait: b is target, trying b as source)
        with pytest.raises(ValidationError, match="multi-hop"):
            tag_repo.create_alias({"source": "b", "target": "c"})

        # Multi-hop: x -> a (source 'a' is already source -> wait: a is source, trying c -> a makes a a target!)
        with pytest.raises(ValidationError, match="multi-hop"):
            tag_repo.create_alias({"source": "c", "target": "a"})


def test_apply_alias_retroactively_deduplicates_tags_and_updates_counts(uow: SqlUnitOfWork):
    with uow.begin():
        persist_novels(
            uow,
            [
                build_novel(
                    id=10, title="Both", author_id=1, author_name="A",
                    tags=["old_tag", "new_tag", "other"],
                ),
                build_novel(
                    id=11, title="OnlyOld", author_id=1, author_name="A",
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
        assert n10 is not None and sorted(n10.tags) == ["new_tag", "other"]
        assert n11 is not None and n11.tags == ["new_tag"]
        assert _tag_count(uow.session, "old_tag") == 0
        assert _tag_count(uow.session, "new_tag") == 2
        assert _tag_count(uow.session, "other") == 1
