"""Limits for the anonymous widget endpoints, per widget and visitor IP. Kept in code rather than in
DEFAULT_THROTTLE_RATES so they cannot be loosened by configuration."""
from rest_framework.throttling import SimpleRateThrottle


class _PerWidgetThrottle(SimpleRateThrottle):
    def get_rate(self):
        return self.rate

    def get_cache_key(self, request, view):
        return f"throttle:{self.scope}:{view.kwargs.get('public_key', '_')}:{self.get_ident(request)}"


class PublicWidgetTokenThrottle(_PerWidgetThrottle):
    """Each request starts a call, so this is the one that protects the hospital's credits."""
    scope = "widget_token"
    rate = "30/min"


class PublicWidgetConfigThrottle(_PerWidgetThrottle):
    """Fetched on every page load of the website, so it is higher."""
    scope = "widget_config"
    rate = "120/min"
