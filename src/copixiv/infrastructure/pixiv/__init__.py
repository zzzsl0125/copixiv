"""Pixiv access module — the project's module-quality benchmark.

Public API (docs/MODULARITY.md §M2).  Anything not listed here is an
implementation detail; other modules must not import it:

- ``PixivClient`` (``client.py``) — explicit async API methods, concurrency
  limit, pagination and retry.
- ``AccountPool`` (``accounts.py``) — LRU account selection with
  ContextVar-propagated strategy.
- ``PixivAccount`` / ``AccountStrategy`` / ``TokenInfo`` (``account.py``)
  — single-account state machine and strategy dataclasses.
- Exceptions: ``RateLimitError``, ``AccountInvalidError``, ``PixivApiError``
  (``account.py``).

Vendor boundary (docs/MODULARITY.md §2.2): ``import pixivpy3`` is only
allowed in ``patch.py`` (monkey patches) and ``account.py`` (the single
adapter that owns an ``AppPixivAPI`` instance).  Everything else in this
package — and the rest of the project — sees only the port protocols in
``copixiv.domain.ports.pixiv`` and the exceptions above.
"""
