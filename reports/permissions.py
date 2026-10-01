from __future__ import annotations

from accounts.models import User


REPORT_ROLE_MATRIX = {
	"patient_summary": [User.Role.CLINIC_ADMIN, User.Role.DOCTOR],
	"appointment_analytics": [User.Role.CLINIC_ADMIN],
	"clinical_encounters": [User.Role.CLINIC_ADMIN, User.Role.DOCTOR],
	"follow_up_status": [User.Role.CLINIC_ADMIN, User.Role.DOCTOR, User.Role.RECEPTIONIST],
	"queue_performance": [User.Role.CLINIC_ADMIN, User.Role.DOCTOR, User.Role.RECEPTIONIST],
	"revenue_report": [User.Role.CLINIC_ADMIN],
	"custom": [User.Role.CLINIC_ADMIN],
}


def allowed_roles_for(report_type: str) -> list[str]:
	return list(REPORT_ROLE_MATRIX.get(report_type, [User.Role.CLINIC_ADMIN]))


def role_can_access(report_type: str, role: str) -> bool:
	return role in allowed_roles_for(report_type)


def role_can_manage(role: str) -> bool:
	return role == User.Role.CLINIC_ADMIN