from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.core.mail import send_mail
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError

from accounts.exceptions import SeatLimitReachedError
from accounts.models import EmailVerificationToken, Invitation, PasswordResetToken, User
from accounts.utils import generate_raw_token, token_hash
from audit.models import AuthEvent
from clinics.models import Clinic
from doctors.models import Doctor
from subscriptions.models import ClinicEntitlement, SubscriptionPlan

invite_signer = TimestampSigner(salt="manageopd-invite")
verify_signer = TimestampSigner(salt="manageopd-verify")
password_reset_signer = TimestampSigner(salt="manageopd-password-reset")


def get_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log_auth_event(request, event_type: str, user: User | None = None, email: str | None = None, metadata: dict | None = None):
    AuthEvent.objects.create(
        user=user,
        email=email,
        event_type=event_type,
        ip_address=get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        metadata=metadata or {},
    )


def ensure_email_available(email: str):
    if User.objects.filter(email__iexact=email).exists():
        raise serializers.ValidationError({"email": "This email is already registered."})


def set_auth_cookies(response, access: str, refresh: str):
    response.set_cookie(
        key=settings.AUTH_COOKIE_ACCESS,
        value=access,
        httponly=settings.AUTH_COOKIE_HTTP_ONLY,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        max_age=15 * 60,
    )
    response.set_cookie(
        key=settings.AUTH_COOKIE_REFRESH,
        value=refresh,
        httponly=settings.AUTH_COOKIE_HTTP_ONLY,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        max_age=7 * 24 * 60 * 60,
    )


def clear_auth_cookies(response):
    response.delete_cookie(settings.AUTH_COOKIE_ACCESS)
    response.delete_cookie(settings.AUTH_COOKIE_REFRESH)


def _create_email_verification(user: User) -> str:
    raw = generate_raw_token()
    token_obj = EmailVerificationToken.objects.create(
        user=user,
        token_hash=token_hash(raw),
        expires_at=timezone.now() + timedelta(days=3),
    )
    payload = f"{token_obj.id}:{raw}"
    return verify_signer.sign(payload)


def send_verification_email(user: User):
    token = _create_email_verification(user)
    url = f"{settings.FRONTEND_URL}/verify-email?token={token}"
    send_mail(
        subject="Verify your ManageOPD account",
        message=f"Verify your account by opening this link: {url}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def _create_password_reset_token(user: User) -> str:
    raw = generate_raw_token()
    token_obj = PasswordResetToken.objects.create(
        user=user,
        token_hash=token_hash(raw),
        expires_at=timezone.now() + timedelta(hours=2),
    )
    payload = f"{token_obj.id}:{raw}"
    return password_reset_signer.sign(payload)


def send_password_reset_email(email: str):
    user = User.objects.filter(email__iexact=email).first()
    if not user:
        return

    token = _create_password_reset_token(user)
    url = f"{settings.FRONTEND_URL}/reset-password?token={token}"
    send_mail(
        subject="Reset your ManageOPD password",
        message=f"Reset your password by opening this link: {url}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def reset_password_with_token(*, signed_token: str, password: str):
    try:
        payload = password_reset_signer.unsign(signed_token, max_age=2 * 60 * 60)
    except SignatureExpired as exc:
        raise serializers.ValidationError({"token": "Password reset link expired."}) from exc
    except BadSignature as exc:
        raise serializers.ValidationError({"token": "Invalid password reset token."}) from exc

    token_id_str, raw = payload.split(":", 1)
    token_obj = PasswordResetToken.objects.filter(id=token_id_str).select_related("user").first()
    if not token_obj or token_obj.token_hash != token_hash(raw) or not token_obj.is_usable:
        raise serializers.ValidationError({"token": "Invalid password reset token."})

    with transaction.atomic():
        token_obj = PasswordResetToken.objects.select_for_update().select_related("user").get(pk=token_obj.pk)
        if not token_obj.is_usable:
            raise serializers.ValidationError({"token": "Password reset token already used or expired."})

        user = token_obj.user
        user.set_password(password)
        user.failed_login_attempts = 0
        user.lock_until = None
        user.save(update_fields=["password", "failed_login_attempts", "lock_until", "updated_at"])

        token_obj.used_at = timezone.now()
        token_obj.save(update_fields=["used_at", "updated_at"])

    return user


def get_or_create_clinic_entitlement(clinic: Clinic) -> ClinicEntitlement:
    entitlement, _ = ClinicEntitlement.objects.get_or_create(
        clinic=clinic,
        defaults={"plan": SubscriptionPlan.get_default_plan(SubscriptionPlan.PlanType.TRIAL)},
    )
    return entitlement


def signup_clinic_admin(*, clinic_name: str, full_name: str, email: str, password: str, is_also_doctor: bool = False) -> User:
    ensure_email_available(email)
    with transaction.atomic():
        clinic = Clinic.objects.create(name=clinic_name, subscription_status=Clinic.SubscriptionStatus.TRIAL)
        trial_plan = SubscriptionPlan.get_default_plan(SubscriptionPlan.PlanType.TRIAL)
        ClinicEntitlement.objects.create(clinic=clinic, plan=trial_plan)
        user = User.objects.create_user(
            clinic=clinic,
            email=email.lower(),
            full_name=full_name,
            password=password,
            role=User.Role.CLINIC_ADMIN,
            is_clinic_admin=True,
            is_doctor=is_also_doctor,
            membership_status=User.MembershipStatus.ACTIVE,
            is_verified=False,
        )
        if is_also_doctor:
            from doctors.models import Doctor
            Doctor.objects.create(clinic=clinic, user=user, full_name=full_name)
    return user


def _lock_duration(attempts: int) -> timedelta:
    minutes = min(60, 2 ** max(0, attempts - 4))
    return timedelta(minutes=minutes)


def login_user(email: str, password: str) -> User:
    user_qs = User.objects.filter(email__iexact=email)
    user = user_qs.first()
    now = timezone.now()
    if user and user.lock_until and user.lock_until > now:
        raise AuthenticationFailed("Account is temporarily locked. Please try again later.")

    authenticated = authenticate(email=email.lower(), password=password)
    if not authenticated:
        if user:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= 5:
                user.lock_until = now + _lock_duration(user.failed_login_attempts)
            user.save(update_fields=["failed_login_attempts", "lock_until", "updated_at"])
        raise AuthenticationFailed("Invalid credentials.")

    authenticated.failed_login_attempts = 0
    authenticated.lock_until = None
    authenticated.save(update_fields=["failed_login_attempts", "lock_until", "updated_at"])
    return authenticated


def mint_tokens(user: User):
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token), str(refresh)


def rotate_refresh_token(raw_refresh: str):
    try:
        refresh = RefreshToken(raw_refresh)
    except TokenError as exc:
        raise AuthenticationFailed("Refresh token is invalid or expired.") from exc
    try:
        user = User.objects.get(pk=refresh["user_id"])
    except User.DoesNotExist as exc:
        raise AuthenticationFailed("User no longer exists.") from exc
    if not user.is_active:
        raise AuthenticationFailed("User account is disabled.")
    try:
        refresh.blacklist()
    except Exception:
        pass
    new_refresh = RefreshToken.for_user(user)
    return str(new_refresh.access_token), str(new_refresh)


def create_invitation(*, clinic: Clinic, invited_by: User, email: str, role: str) -> str:
    ensure_email_available(email)

    with transaction.atomic():
        entitlement = (
            ClinicEntitlement.objects.select_for_update()
            .filter(clinic=clinic)
            .first()
        )
        if not entitlement:
            entitlement = get_or_create_clinic_entitlement(clinic)
            entitlement = ClinicEntitlement.objects.select_for_update().get(pk=entitlement.pk)

        max_allowed = entitlement.get_limit(role)

        active_users_count = User.objects.filter(
            clinic=clinic,
            role=role,
            is_active=True,
            membership_status=User.MembershipStatus.ACTIVE,
        ).count()

        now = timezone.now()
        active_invites_count = Invitation.objects.filter(
            clinic=clinic,
            role=role,
            used_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gte=now,
        ).count()

        current_active = active_users_count + active_invites_count
        if current_active >= max_allowed:
            raise SeatLimitReachedError(
                role=role,
                plan_name=entitlement.plan.name,
                current_active=current_active,
                max_allowed=max_allowed,
            )

        raw = generate_raw_token()
        hashed = token_hash(raw)
        try:
            invitation = Invitation.objects.create(
                clinic=clinic,
                invited_by=invited_by,
                email=email.lower(),
                role=role,
                token_hash=hashed,
            )
        except IntegrityError as exc:
            raise serializers.ValidationError({"detail": "Could not generate invitation token. Please retry."}) from exc

    signed_token = invite_signer.sign(f"{invitation.id}:{raw}")
    invite_url = f"{settings.FRONTEND_URL}/invite-accept?token={signed_token}"
    send_mail(
        subject=f"{invited_by.full_name} has invited you to join {clinic.name} on ManageOPD as a {role.replace('_', ' ')}",
        message=(
            f"{invited_by.full_name} has invited you to join {clinic.name} on ManageOPD as a {role.replace('_', ' ')}. "
            f"Open this link to accept: {invite_url}"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )
    return signed_token


def get_invitation_from_signed_token(signed_token: str) -> Invitation:
    try:
        payload = invite_signer.unsign(signed_token, max_age=7 * 24 * 60 * 60)
    except SignatureExpired as exc:
        raise serializers.ValidationError({"token": "Invitation has expired."}) from exc
    except BadSignature as exc:
        raise serializers.ValidationError({"token": "Invalid invitation token."}) from exc

    invitation_id_str, raw = payload.split(":", 1)
    invite = Invitation.objects.filter(id=invitation_id_str).select_related("clinic").first()
    if not invite or invite.token_hash != token_hash(raw):
        raise serializers.ValidationError({"token": "Invalid invitation token."})
    if not invite.is_usable:
        raise serializers.ValidationError({"token": "Invitation is no longer valid."})
    return invite


def accept_invitation(*, signed_token: str, full_name: str, password: str) -> User:
    invite = get_invitation_from_signed_token(signed_token)
    ensure_email_available(invite.email)

    with transaction.atomic():
        invite = Invitation.objects.select_for_update().get(pk=invite.pk)
        if not invite.is_usable:
            raise serializers.ValidationError({"token": "Invitation is no longer valid."})

        user = User.objects.create_user(
            clinic=invite.clinic,
            email=invite.email.lower(),
            full_name=full_name,
            password=password,
            role=invite.role,
            membership_status=User.MembershipStatus.ACTIVE,
            is_verified=True,
            email_verified_at=timezone.now(),
        )
        if user.role == User.Role.DOCTOR:
            Doctor.objects.get_or_create(
                user=user,
                clinic=invite.clinic,
                defaults={"full_name": full_name, "specialty": "General Practice"},
            )
        invite.used_at = timezone.now()
        invite.save(update_fields=["used_at", "updated_at"])
    return user


def verify_email(signed_token: str) -> User:
    try:
        payload = verify_signer.unsign(signed_token, max_age=3 * 24 * 60 * 60)
    except SignatureExpired as exc:
        raise serializers.ValidationError({"token": "Verification link expired."}) from exc
    except BadSignature as exc:
        raise serializers.ValidationError({"token": "Invalid verification token."}) from exc

    token_id_str, raw = payload.split(":", 1)
    token_obj = EmailVerificationToken.objects.filter(id=token_id_str).select_related("user").first()
    if not token_obj or token_obj.token_hash != token_hash(raw) or not token_obj.is_usable:
        raise serializers.ValidationError({"token": "Invalid verification token."})

    with transaction.atomic():
        token_obj = EmailVerificationToken.objects.select_for_update().select_related("user").get(pk=token_obj.pk)
        if not token_obj.is_usable:
            raise serializers.ValidationError({"token": "Verification token already used or expired."})
        token_obj.used_at = timezone.now()
        token_obj.save(update_fields=["used_at", "updated_at"])
        user = token_obj.user
        user.is_verified = True
        user.email_verified_at = timezone.now()
        user.save(update_fields=["is_verified", "email_verified_at", "updated_at"])
    return user
