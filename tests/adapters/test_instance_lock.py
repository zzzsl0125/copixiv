"""Instance-exclusive ``flock`` regression — phase 2 removed it (R1 / F12).

Under PostgreSQL there is no SQLite single-writer rule to protect, so the
``flock`` instance lock and the ``_acquire_instance_lock`` helper were
removed from the composition root (db_greenfield_design.md §3.10).  This
test pins that removal so the SQLite-era lock never silently returns.
"""

import copixiv.app as app


def test_sqlite_instance_lock_helper_is_gone():
    assert not hasattr(app, "_acquire_instance_lock")
    assert not hasattr(app, "_warmup_database_cache")
    assert not hasattr(app, "_rebuild_fts_if_needed")


def test_app_singletons_have_no_instance_lock_field():
    import dataclasses

    assert "instance_lock" not in {f.name for f in dataclasses.fields(app._AppSingletons)}
