from __future__ import annotations

from platform_admin.models import PlatformAuditLog


def log_platform_action(actor, action: str, clinic=None, details: dict | None = None) -> PlatformAuditLog:
    """Record a platform admin's change. Every platform write goes through here, in the same transaction as the change."""
    return PlatformAuditLog.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_email=getattr(actor, "email", "") or "",
        action=action,
        clinic=clinic,
        clinic_name=getattr(clinic, "name", "") or "",
        details=details or {},
    )
