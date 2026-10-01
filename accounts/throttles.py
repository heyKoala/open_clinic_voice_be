from rest_framework.throttling import AnonRateThrottle


class SignupThrottle(AnonRateThrottle):
    scope = "signup"


class LoginThrottle(AnonRateThrottle):
    scope = "login"


class InviteThrottle(AnonRateThrottle):
    scope = "invite"


class PasswordResetThrottle(AnonRateThrottle):
    scope = "password_reset"
