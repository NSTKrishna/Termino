"""User-facing errors and the exit-code contract (DESIGN.md §6)."""

from __future__ import annotations

# Exit codes for cron / SCADA scripts.
EXIT_OK, EXIT_WARN, EXIT_ALARM, EXIT_ERROR = 0, 1, 2, 3

STATUS_EXIT = {"Healthy": EXIT_OK, "Watch": EXIT_WARN, "Degraded": EXIT_WARN, "Critical": EXIT_ALARM}
SEVERITY_EXIT = {"INFO": EXIT_OK, "WARN": EXIT_WARN, "ALARM": EXIT_ALARM, "CRITICAL": EXIT_ALARM}
SEVERITY_RANK = {"INFO": 0, "WARN": 1, "ALARM": 2, "CRITICAL": 3}


class TbfmonError(Exception):
    """An error the user can fix. Rendered without a traceback; exit code 3."""

    def __init__(self, message: str, hint: str | None = None, code: int = EXIT_ERROR):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code

    def to_dict(self) -> dict:
        return {"error": self.message, "hint": self.hint}


def worst_exit(codes) -> int:
    return max(codes, default=EXIT_OK)
