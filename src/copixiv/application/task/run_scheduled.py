"""Use case: run a scheduled task immediately."""

from copixiv.domain.exceptions import NotFoundError


class RunScheduledUseCase:
    """Trigger immediate execution of a scheduled task.

    Raises:
        NotFoundError: If the task_id is not found.
    """

    def __init__(self, task_manager):
        self._task_manager = task_manager

    def execute(self, task_id: int) -> None:
        """Run the task now."""
        try:
            self._task_manager.run_task_now(task_id)
        except ValueError as exc:
            raise NotFoundError(str(exc))
