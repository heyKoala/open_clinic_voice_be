from urllib.parse import parse_qs
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.tokens import AccessToken
from accounts.models import User
from clinics.context import resolve_active_clinic

@database_sync_to_async
def get_user_with_clinic(token_key, requested_clinic_id=None):
    try:
        access_token = AccessToken(token_key)
        user_id = access_token["user_id"]
        user = User.objects.select_related("clinic").get(id=user_id, is_active=True)
        user.clinic = resolve_active_clinic(user, requested_clinic_id)
        return user
    except Exception:
        return AnonymousUser()

class JWTAuthMiddleware:
    """
    Middleware to authenticate WebSockets using JWT access token from cookies.
    """
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        from http.cookies import SimpleCookie
        headers = dict(scope.get("headers", []))
        
        cookie_header = headers.get(b"cookie", b"").decode("utf-8")
        token = None
        
        if cookie_header:
            cookie = SimpleCookie(cookie_header)
            if "mvx_access" in cookie:
                token = cookie["mvx_access"].value
        
        query_string = scope.get("query_string", b"").decode("utf-8")
        params = parse_qs(query_string)
        requested_clinic_id = params.get("active_clinic_id", [None])[0]

        # Try the explicit ticket first, then the cookie: a browser can still send an expired
        # access cookie alongside a freshly issued, valid ticket.
        candidates = [params["token"][0]] if params.get("token") else []
        if token:
            candidates.append(token)
        scope["user"] = AnonymousUser()
        for candidate in candidates:
            user = await get_user_with_clinic(candidate, requested_clinic_id)
            if user.is_authenticated:
                scope["user"] = user
                break

        return await self.inner(scope, receive, send)
