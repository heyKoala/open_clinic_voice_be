from __future__ import annotations

from rest_framework.permissions import BasePermission

from accounts.models import User
from clinics.context import bind_active_clinic


class PublicEndpointPermission(BasePermission):
    def has_permission(self, request, view):
        return True


class AuthenticatedAndVerified(BasePermission):
    message = "Email verification is required."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_verified):
            return False
        # Authentication paths other than CookieJWTAuthentication (session auth, forced auth)
        # run after ClinicContextMiddleware, which saw an anonymous user; resolve the clinic now.
        if getattr(request, "clinic", None) is None:
            bind_active_clinic(request, user)
        return True


class IsClinicAdmin(AuthenticatedAndVerified):
    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        
        has_role = request.user.role == User.Role.CLINIC_ADMIN
        is_admin = getattr(request.user, 'is_clinic_admin', False)
        return has_role or is_admin


class HasAnyRole(AuthenticatedAndVerified):
    allowed_roles: tuple[str, ...] = ()
    message = "You do not have access to this area."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if not self.allowed_roles:
            return True
        if request.user.role in self.allowed_roles:
            return True
        if User.Role.DOCTOR in self.allowed_roles and getattr(request.user, 'is_doctor', False):
            return True
        return False


class IsAdminOrDoctor(HasAnyRole):
    allowed_roles = (User.Role.CLINIC_ADMIN, User.Role.DOCTOR)


class IsAdminOrReceptionist(HasAnyRole):
    allowed_roles = (User.Role.CLINIC_ADMIN, User.Role.RECEPTIONIST)


class IsDoctorOnly(HasAnyRole):
    allowed_roles = (User.Role.DOCTOR,)


class IsReceptionistOnly(HasAnyRole):
    allowed_roles = (User.Role.RECEPTIONIST,)


class IsDoctorOrReceptionistOnly(HasAnyRole):
    allowed_roles = (User.Role.DOCTOR, User.Role.RECEPTIONIST)


class IsDoctor(AuthenticatedAndVerified):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.role == User.Role.DOCTOR


class IsReceptionist(AuthenticatedAndVerified):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.role == User.Role.RECEPTIONIST


class IsDoctorOrReceptionist(AuthenticatedAndVerified):
    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return request.user.role in (User.Role.DOCTOR, User.Role.RECEPTIONIST)
