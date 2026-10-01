from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import APIException


class SeatLimitReachedError(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = "seat_limit_reached"

    def __init__(self, *, role: str, plan_name: str, current_active: int, max_allowed: int):
        self.detail = {
            "error_code": self.default_code,
            "detail": f"Seat limit reached for role '{role}'. ({current_active}/{max_allowed} consumed)",
            "plan_name": str(plan_name),
            "role": str(role),
            "current_active": int(current_active),
            "max_allowed": int(max_allowed),
        }
