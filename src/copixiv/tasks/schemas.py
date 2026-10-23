"""Task management API schemas — carried with the task feature (S1)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


def _parse_json_str(v: Any) -> Any:
    """Parse a JSON string to dict, or return the value as-is."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return v
    return v


# ---------------------------------------------------------------------------
# Task Management
# ---------------------------------------------------------------------------

class ScheduledTaskCreate(BaseModel):
    name: str
    task: str
    cron: str
    params: dict[str, Any] | None = None
    is_enabled: bool = False
    sort_index: int = 0

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, v: str) -> str:
        """Reject malformed cron expressions at the API boundary (422).

        Keeps the previous 5-field crontab contract: exactly five
        whitespace-separated fields (minute hour day month weekday).
        Validity itself is delegated to :func:`copixiv.tasks.kernel.next_occurrence`
        (croniter), whose ``ValueError`` becomes a Pydantic 422.
        """
        from datetime import timezone

        from copixiv.tasks.kernel import next_occurrence

        if len(v.split()) != 5:
            raise ValueError(
                "Invalid cron expression: exactly 5 fields are required "
                "(minute hour day-of-month month day-of-week)"
            )
        try:
            next_occurrence(v, datetime.now(timezone.utc))
        except ValueError as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc
        return v


class ScheduledTaskUpdate(BaseModel):
    name: str | None = None
    task: str | None = None
    cron: str | None = None
    params: dict[str, Any] | None = None
    is_enabled: bool | None = None
    sort_index: int | None = None

    @field_validator("cron")
    @classmethod
    def _validate_cron_optional(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return ScheduledTaskCreate._validate_cron(v)


class ScheduledTaskResponse(ScheduledTaskCreate):
    id: int
    model_config = ConfigDict(from_attributes=True)

    _parse_params = field_validator("params", mode="before")(_parse_json_str)


class TaskHistoryResponse(BaseModel):
    id: int
    name: str
    # Registered function name (``task_history.task_func``); ``name`` is the
    # user-facing display label.  NULL on legacy rows written before the
    # column existed.
    task_func: str | None = None
    # Same rule as ``result``: coerced, never fatal.
    arguments: dict | None = None
    status: str
    # timestamptz → datetime; Pydantic serializes to an ISO string on the wire.
    start_time: datetime
    end_time: datetime | None = None
    duration: float | None = None
    # ``result`` is the stored JSONB object **without its ``log`` key** — the
    # list endpoint strips it because it was 96 % of a 7.4 MB response while
    # the UI only shows it after clicking 「日志」.  Fetch it separately from
    # ``GET /api/tasks/history/{id}/log``.
    #
    # Stays ``dict`` on the wire — the generated front-end types depend on it —
    # but a malformed row is **coerced**, never allowed to fail validation.
    # The column is JSONB, so any JSON value can be in it; an older
    # ``recover_stale`` wrote ``json.dumps`` output there, leaving a bare
    # string.  Under a strict ``dict`` such a row raised and took the **whole
    # page** down with a 500 — one bad row, no history at all.  Production
    # holds three ``jsonb_typeof = 'string'`` rows today that only escape that
    # fate by happening to parse into a dict one level in.
    result: dict | None = None
    # True when the row *has* stored log text, so the UI can enable/disable
    # the log button without downloading the log to find out.  ``None`` on
    # legacy rows that predate the column.
    has_log: bool = False
    # ``progress`` is a JSONB column.  Tasks write a plain message string; keep
    # it permissive (Any) so a future dict-shaped progress doesn't break the
    # wire contract.
    progress: Any | None = None
    model_config = ConfigDict(from_attributes=True)

    @field_validator("arguments", "result", mode="before")
    @classmethod
    def _normalize_json_column(cls, v: Any) -> Any:
        """Wrap a non-object JSON value instead of rejecting the row.

        A scalar here is *historical corruption*, not a caller error: the only
        writer that produced it (``recover_stale`` printing ``json.dumps`` into
        a JSONB column) is fixed, and the three surviving rows are old.  The
        list endpoint must still render, so the value is preserved verbatim
        under ``raw`` rather than dropped — an operator can see what is actually
        in the column instead of an empty cell and no explanation.
        """
        # Parse first, then judge: a JSON *string* that decodes to an object is
        # a normal stored value, not corruption.  Doing this in one validator
        # keeps the order explicit — two ``mode="before"`` validators would run
        # in declaration order and could coerce before parsing.
        v = _parse_json_str(v)
        # A JSON *string* that itself encodes an object is the exact shape the
        # legacy ``recover_stale`` left behind (``json.dumps`` output stored as
        # a JSONB string, so it arrives quoted a second time).  Unwrap that one
        # extra layer — it is the same value the writer meant to store.
        if isinstance(v, str):
            v = _parse_json_str(v)
        if v is None or isinstance(v, dict):
            return v
        return {"raw": v}


class TaskHistoryLogResponse(BaseModel):
    """The stored execution log for one task-history row."""

    id: int
    log: str


class TaskBusyResponse(BaseModel):
    """Task functions that currently have a pending or running history row.

    Deliberately its own endpoint: the front-end needs exactly this set to
    gray out the "run" buttons, and reusing the history list for it meant
    polling a ~7.4 MB page every 5 seconds.
    """

    funcs: list[str]


class TaskHistoryListResponse(BaseModel):
    items: list[TaskHistoryResponse]
    total: int


class TaskMethod(BaseModel):
    name: str
    description: str | None = None
    # The task's Pydantic ``model_json_schema()``; ``None`` = no arguments.
    arguments: dict[str, Any] | None = None
