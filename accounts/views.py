from __future__ import annotations

import uuid

from django.conf import settings
from django.middleware.csrf import get_token
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from accounts.models import User
from patients.models import Patient
from appointments.models import Appointment
from accounts.authentication import PublicEndpointAuthentication
from accounts.permissions import IsClinicAdmin, PublicEndpointPermission
from accounts.serializers import (
	ForgotPasswordSerializer,
	InviteAcceptSerializer,
	InviteCreateSerializer,
	InvitePreviewSerializer,
	LoginSerializer,
	ResetPasswordSerializer,
	SignupSerializer,
	UserMeSerializer,
	UserUpdateSerializer,
	VerifyEmailSerializer,
)
from accounts.services import (
	accept_invitation,
	clear_auth_cookies,
	create_invitation,
	get_or_create_clinic_entitlement,
	get_invitation_from_signed_token,
	log_auth_event,
	login_user,
	mint_tokens,
	reset_password_with_token,
	rotate_refresh_token,
	send_password_reset_email,
	send_verification_email,
	set_auth_cookies,
	signup_clinic_admin,
	verify_email,
)
from accounts.throttles import (
	InviteThrottle,
	LoginThrottle,
	PasswordResetThrottle,
	SignupThrottle,
)
from audit.models import AuthEvent, DeviceSession
from accounts.models import Invitation
from accounts.exceptions import SeatLimitReachedError
from clinics.models import Clinic
from subscriptions.models import ClinicEntitlement


class SignupView(APIView):
	# Public endpoint: skip cookie auth so a stale or invalid session cookie cannot block it.
	authentication_classes = [PublicEndpointAuthentication]
	permission_classes = [PublicEndpointPermission]
	throttle_classes = [SignupThrottle]

	def post(self, request):
		serializer = SignupSerializer(data=request.data)
		serializer.is_valid(raise_exception=True)
		user = signup_clinic_admin(**serializer.validated_data)
		send_verification_email(user)
		log_auth_event(request, AuthEvent.EventType.SIGNUP, user=user, email=user.email)
		return Response(
			{"detail": "Signup successful. Please verify your email before continuing."},
			status=status.HTTP_201_CREATED,
		)


class VerifyEmailView(APIView):
	# Public endpoint: skip cookie auth so a stale or invalid session cookie cannot block it.
	authentication_classes = [PublicEndpointAuthentication]
	permission_classes = [PublicEndpointPermission]

	def post(self, request):
		serializer = VerifyEmailSerializer(data=request.data)
		serializer.is_valid(raise_exception=True)
		user = verify_email(serializer.validated_data["token"])
		return Response({"detail": "Email verified successfully.", "email": user.email})


class ForgotPasswordView(APIView):
	# Public endpoint: skip cookie auth so a stale or invalid session cookie cannot block it.
	authentication_classes = [PublicEndpointAuthentication]
	permission_classes = [PublicEndpointPermission]
	throttle_classes = [PasswordResetThrottle]

	def post(self, request):
		serializer = ForgotPasswordSerializer(data=request.data)
		serializer.is_valid(raise_exception=True)
		email = serializer.validated_data["email"]
		send_password_reset_email(email)
		return Response({"detail": "If that email exists, a reset link has been sent."})


class ResetPasswordView(APIView):
	# Public endpoint: skip cookie auth so a stale or invalid session cookie cannot block it.
	authentication_classes = [PublicEndpointAuthentication]
	permission_classes = [PublicEndpointPermission]
	throttle_classes = [PasswordResetThrottle]

	def post(self, request):
		serializer = ResetPasswordSerializer(data=request.data)
		serializer.is_valid(raise_exception=True)
		user = reset_password_with_token(
			signed_token=serializer.validated_data["token"],
			password=serializer.validated_data["password"],
		)
		log_auth_event(request, AuthEvent.EventType.PASSWORD_RESET, user=user, email=user.email)
		return Response({"detail": "Password reset successful. Please login with your new password."})


class LoginView(APIView):
	# Public endpoint: skip cookie auth so a stale or invalid session cookie cannot block it.
	authentication_classes = [PublicEndpointAuthentication]
	permission_classes = [PublicEndpointPermission]
	throttle_classes = [LoginThrottle]

	def post(self, request):
		serializer = LoginSerializer(data=request.data)
		serializer.is_valid(raise_exception=True)
		email = serializer.validated_data["email"].lower()

		try:
			user = login_user(email=email, password=serializer.validated_data["password"])
		except Exception:
			log_auth_event(request, AuthEvent.EventType.LOGIN_FAILURE, email=email)
			raise

		access, refresh = mint_tokens(user)
		response = Response({"detail": "Login successful.", "is_verified": user.is_verified})
		set_auth_cookies(response, access, refresh)
		response.set_cookie(
			key="csrftoken",
			value=get_token(request),
			httponly=False,
			secure=settings.AUTH_COOKIE_SECURE,
			samesite=settings.AUTH_COOKIE_SAMESITE,
		)

		device_id = request.COOKIES.get("device_id") or str(uuid.uuid4())
		ip_address = request.META.get("REMOTE_ADDR") or "127.0.0.1"
		user_agent = request.META.get("HTTP_USER_AGENT", "")[:255]
		
		DeviceSession.objects.update_or_create(
			device_id=device_id,
			defaults={
				"user": user,
				"ip_address": ip_address,
				"user_agent": user_agent,
			}
		)

		response.set_cookie(
			key="device_id",
			value=device_id,
			httponly=True,
			secure=settings.AUTH_COOKIE_SECURE,
			samesite=settings.AUTH_COOKIE_SAMESITE,
			max_age=365 * 24 * 60 * 60,
		)

		log_auth_event(request, AuthEvent.EventType.LOGIN_SUCCESS, user=user, email=user.email)
		return response


class RefreshView(APIView):
	# Public endpoint: skip cookie auth so a stale or invalid session cookie cannot block it.
	authentication_classes = [PublicEndpointAuthentication]
	permission_classes = [PublicEndpointPermission]

	def post(self, request):
		raw_refresh = request.COOKIES.get(settings.AUTH_COOKIE_REFRESH)
		if not raw_refresh:
			return Response({"detail": "Refresh token missing."}, status=status.HTTP_401_UNAUTHORIZED)
		access, refresh = rotate_refresh_token(raw_refresh)
		response = Response({"detail": "Token refreshed."})
		set_auth_cookies(response, access, refresh)
		return response


class WebSocketTicketView(APIView):
	permission_classes = [IsAuthenticated]

	def get(self, request):
		return Response({"ticket": str(AccessToken.for_user(request.user))})


class LogoutView(APIView):
	permission_classes = [IsAuthenticated]

	def post(self, request):
		raw_refresh = request.COOKIES.get(settings.AUTH_COOKIE_REFRESH)
		if raw_refresh:
			try:
				RefreshToken(raw_refresh).blacklist()
			except Exception:
				pass
		# Log logout event
		log_auth_event(request, AuthEvent.EventType.LOGOUT, user=request.user, email=request.user.email)
		
		device_id = request.COOKIES.get("device_id")
		if device_id:
			DeviceSession.objects.filter(device_id=device_id, user=request.user).delete()

		response = Response({"detail": "Logged out."})
		clear_auth_cookies(response)
		response.delete_cookie("device_id")
		return response


class MeView(APIView):
	permission_classes = [IsAuthenticated]

	def get(self, request):
		return Response(UserMeSerializer(request.user, context={'request': request}).data)

	def patch(self, request):
		if "role" in request.data and request.data["role"] != request.user.role:
			return Response(
				{"detail": "Role escalation or role modification is disallowed through profile updates."},
				status=status.HTTP_403_FORBIDDEN,
			)
		serializer = UserUpdateSerializer(request.user, data=request.data, partial=True)
		serializer.is_valid(raise_exception=True)
		serializer.save()
		return Response(UserMeSerializer(request.user).data)


class AccessView(APIView):
	permission_classes = [IsClinicAdmin]

	def get(self, request):
		clinic = request.clinic
		entitlement = get_or_create_clinic_entitlement(clinic)
		members = User.objects.filter(clinic=clinic).order_by("role", "full_name")
		active_invites = [
			{
				"role": role_value,
				"active_invites": Invitation.objects.filter(
					clinic=clinic,
					role=role_value,
					used_at__isnull=True,
					revoked_at__isnull=True,
					expires_at__gte=timezone.now(),
				).count(),
			}
			for role_value, _ in User.Role.choices
		]
		seat_usage = []
		for role_value, role_label in User.Role.choices:
			active_users = members.filter(
				role=role_value,
				is_active=True,
				membership_status=User.MembershipStatus.ACTIVE,
			).count()
			active_invite_count = next(item["active_invites"] for item in active_invites if item["role"] == role_value)
			seat_usage.append(
				{
					"role": role_value,
					"role_label": role_label,
					"active_users": active_users,
					"active_invites": active_invite_count,
					"limit": entitlement.get_limit(role_value),
				}
			)

		return Response(
			{
				"members": UserMeSerializer(members, many=True).data,
				"seat_usage": seat_usage,
				"plan": entitlement.plan.name,
				"feature_flags": entitlement.get_feature_flags(),
				"clinic_name": clinic.name,
			}
		)


class DashboardStatsView(APIView):
	permission_classes = [IsClinicAdmin]

	def get(self, request):
		from patients.models import Patient
		from appointments.models import Appointment

		clinic = request.clinic
		
		# Alternatively, anyone with role=doctor or is_doctor=True
		total_doctors = User.objects.filter(clinic=clinic, is_active=True, is_doctor=True).count()

		total_receptionists = User.objects.filter(
			clinic=clinic,
			role=User.Role.RECEPTIONIST,
			is_active=True,
			membership_status=User.MembershipStatus.ACTIVE,
		).count()

		total_patients = Patient.objects.filter(clinic=clinic).count()

		now = timezone.now()
		upcoming_appointments_qs = Appointment.objects.filter(
			clinic=clinic,
			starts_at__gte=now,
			is_active=True
		).exclude(
			status__in=[Appointment.Status.COMPLETED, Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW]
		).order_by("starts_at")[:10]

		upcoming_appointments = [
			{
				"id": appt.id,
				"status": appt.status,
				"reason": appt.reason,
				"starts_at": appt.starts_at,
				"ends_at": appt.ends_at,
				"patient_name": appt.patient.full_name,
				"doctor_name": appt.doctor.user.full_name if appt.doctor and appt.doctor.user else None,
			}
			for appt in upcoming_appointments_qs
		]

		return Response({
			"total_doctors": total_doctors,
			"total_receptionists": total_receptionists,
			"total_patients": total_patients,
			"upcoming_appointments": upcoming_appointments
		})


class UserDeactivateView(APIView):
	permission_classes = [IsClinicAdmin]

	def post(self, request, user_id: int):
		try:
			target_user = User.objects.get(pk=user_id, clinic=request.clinic)
		except User.DoesNotExist:
			return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

		if target_user.pk == request.user.pk:
			return Response({"detail": "Cannot deactivate your own admin account."}, status=status.HTTP_400_BAD_REQUEST)

		target_user.is_active = False
		target_user.membership_status = User.MembershipStatus.SUSPENDED
		target_user.save(update_fields=["is_active", "membership_status", "updated_at"])

		if hasattr(target_user, "doctor_profile"):
			target_user.doctor_profile.is_active = False
			target_user.doctor_profile.save(update_fields=["is_active", "updated_at"])

		log_auth_event(
			request,
			AuthEvent.EventType.USER_DEACTIVATED,
			user=request.user,
			email=target_user.email,
			metadata={"target_user_id": target_user.pk, "target_role": target_user.role},
		)
		return Response({"detail": f"User {target_user.email} deactivated successfully. Seat reclaimed."})


class UserReactivateView(APIView):
	permission_classes = [IsClinicAdmin]

	def post(self, request, user_id: int):
		try:
			target_user = User.objects.get(pk=user_id, clinic=request.clinic)
		except User.DoesNotExist:
			return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

		if not (target_user.is_active and target_user.membership_status == User.MembershipStatus.ACTIVE):
			# Reactivation consumes a seat, same as an invitation.
			entitlement = get_or_create_clinic_entitlement(request.clinic)
			max_allowed = entitlement.get_limit(target_user.role)
			current_active = User.objects.filter(
				clinic=request.clinic,
				role=target_user.role,
				is_active=True,
				membership_status=User.MembershipStatus.ACTIVE,
			).count() + Invitation.objects.filter(
				clinic=request.clinic,
				role=target_user.role,
				used_at__isnull=True,
				revoked_at__isnull=True,
				expires_at__gte=timezone.now(),
			).count()
			if current_active >= max_allowed:
				raise SeatLimitReachedError(
					role=target_user.role,
					plan_name=entitlement.plan.name,
					current_active=current_active,
					max_allowed=max_allowed,
				)

		target_user.is_active = True
		target_user.membership_status = User.MembershipStatus.ACTIVE
		target_user.save(update_fields=["is_active", "membership_status", "updated_at"])

		if hasattr(target_user, "doctor_profile") and (target_user.role == User.Role.DOCTOR or target_user.is_doctor):
			target_user.doctor_profile.is_active = True
			target_user.doctor_profile.save(update_fields=["is_active", "updated_at"])

		log_auth_event(
			request,
			AuthEvent.EventType.USER_REACTIVATED,
			user=request.user,
			email=target_user.email,
			metadata={"target_user_id": target_user.pk, "target_role": target_user.role},
		)
		return Response({"detail": f"User {target_user.email} reactivated successfully."})


class UserToggleAdminView(APIView):
	permission_classes = [IsClinicAdmin]

	def post(self, request, user_id: int):
		try:
			target_user = User.objects.get(pk=user_id, clinic=request.clinic)
		except User.DoesNotExist:
			return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

		if target_user.pk == request.user.pk:
			return Response({"detail": "Cannot toggle your own admin status."}, status=status.HTTP_400_BAD_REQUEST)

		target_user.is_clinic_admin = not target_user.is_clinic_admin
		target_user.save(update_fields=["is_clinic_admin", "updated_at"])

		log_auth_event(
			request,
			AuthEvent.EventType.ROLE_CHANGED,
			user=request.user,
			email=target_user.email,
			metadata={"target_user_id": target_user.pk, "is_clinic_admin": target_user.is_clinic_admin},
		)
		action = "granted" if target_user.is_clinic_admin else "revoked"
		return Response({"detail": f"Admin rights {action} for {target_user.email}."})


class UserToggleDoctorView(APIView):
	permission_classes = [IsClinicAdmin]

	def post(self, request, user_id: int):
		try:
			target_user = User.objects.get(pk=user_id, clinic=request.clinic)
		except User.DoesNotExist:
			return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

		target_user.is_doctor = not getattr(target_user, 'is_doctor', False)
		target_user.save(update_fields=["is_doctor", "updated_at"])

		from doctors.models import Doctor
		profile = Doctor.objects.filter(user=target_user).first()
		if target_user.is_doctor and profile is None:
			Doctor.objects.create(clinic=target_user.clinic, user=target_user, full_name=target_user.full_name)
		elif profile is not None and target_user.role != User.Role.DOCTOR and profile.is_active != target_user.is_doctor:
			# Keep the doctor profile's visibility in sync with the flag.
			profile.is_active = target_user.is_doctor
			profile.save(update_fields=["is_active", "updated_at"])

		log_auth_event(
			request,
			AuthEvent.EventType.ROLE_CHANGED,
			user=request.user,
			email=target_user.email,
			metadata={"target_user_id": target_user.pk, "is_doctor": target_user.is_doctor},
		)
		action = "granted" if target_user.is_doctor else "revoked"
		return Response({"detail": f"Doctor status {action} for {target_user.email}."})


class InviteListCreateView(APIView):
	permission_classes = [IsClinicAdmin]

	def get_throttles(self):
		if self.request.method == 'POST':
			return [InviteThrottle()]
		return super().get_throttles()

	def get(self, request):
		invitations = Invitation.objects.filter(clinic=request.clinic).order_by("-created_at")
		data = []
		for invite in invitations:
			if invite.used_at:
				status_str = "Accepted"
			elif invite.revoked_at:
				status_str = "Revoked"
			elif invite.expires_at < timezone.now():
				status_str = "Expired"
			else:
				status_str = "Pending"
			data.append({
				"id": invite.id,
				"email": invite.email,
				"role": invite.role,
				"status": status_str,
				"created_at": invite.created_at,
			})
		return Response(data)

	def post(self, request):
		serializer = InviteCreateSerializer(data=request.data)
		serializer.is_valid(raise_exception=True)
		token = create_invitation(
			clinic=request.clinic,
			invited_by=request.user,
			email=serializer.validated_data["email"],
			role=serializer.validated_data["role"],
		)
		log_auth_event(
			request,
			AuthEvent.EventType.INVITE_CREATED,
			user=request.user,
			email=serializer.validated_data["email"],
			metadata={"role": serializer.validated_data["role"]}
		)
		return Response({"detail": "Invitation sent.", "token": token}, status=status.HTTP_201_CREATED)


class InvitePreviewView(APIView):
	# Public endpoint: skip cookie auth so a stale or invalid session cookie cannot block it.
	authentication_classes = [PublicEndpointAuthentication]
	permission_classes = [PublicEndpointPermission]

	def get(self, request, token: str):
		invite = get_invitation_from_signed_token(token)
		return Response(InvitePreviewSerializer(invite).data)


class InviteAcceptView(APIView):
	# Public endpoint: skip cookie auth so a stale or invalid session cookie cannot block it.
	authentication_classes = [PublicEndpointAuthentication]
	permission_classes = [PublicEndpointPermission]

	def post(self, request, token: str):
		serializer = InviteAcceptSerializer(data=request.data)
		serializer.is_valid(raise_exception=True)
		user = accept_invitation(
			signed_token=token,
			full_name=serializer.validated_data["full_name"],
			password=serializer.validated_data["password"],
		)
		access, refresh = mint_tokens(user)
		response = Response({"detail": "Invitation accepted.", "role": user.role}, status=status.HTTP_201_CREATED)
		set_auth_cookies(response, access, refresh)
		log_auth_event(request, AuthEvent.EventType.INVITE_ACCEPTED, user=user, email=user.email)
		return response

class MyClinicsView(APIView):
	permission_classes = [IsAuthenticated]

	def get(self, request):
		clinics = request.user.clinics.all()
		data = []
		for clinic in clinics:
			data.append({
				"id": clinic.id,
				"name": clinic.name,
				"is_active": clinic.id == getattr(request.clinic, "id", None)
			})
		return Response(data)
