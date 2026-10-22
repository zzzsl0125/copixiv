"""API schema ↔ domain model drift guard.

``NovelBase`` is deliberately **not** a subclass of the domain ``Novel``
(docs/SIMPLIFY_PLAN-v2.md §7.4): Pydantic's ``ConfigDict(exclude=...)`` is
silently ignored (the excluded field still serialises, and a test would not
notice), ``Field(exclude=True)`` keeps the field out of the payload but still
emits it in JSON Schema — and the schema is the frontend's codegen contract
(block B), so a leaked ``content`` / ``path`` would become a generated
TypeScript field.

Keeping the declarations apart costs one explicit list; this test replaces
the "single source" benefit of inheritance.  **Failure mode it prevents:** add
a domain field but forget to mirror it in ``NovelBase`` (or to exclude it
here), and the generated types silently drift from the API — either a field
the frontend never sees, or (if exposed by accident) an internal one it should
not.  When a new internal field is added, add it to the exclusion set below
with a reason; when a public one is added, add it to ``NovelBase``.
"""

from copixiv.core.models import Novel
from copixiv.features.novels.schemas import NovelBase

# Fields the API intentionally does not carry: local file anchor, the random
# browse key, and the local-book dedup hash.
_API_EXCLUDED_FIELDS = {"path", "shuffle", "source_key"}


def test_novelbase_covers_domain_fields():
    assert set(NovelBase.model_fields) == set(Novel.model_fields) - (
        _API_EXCLUDED_FIELDS
    )
