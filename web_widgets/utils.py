from __future__ import annotations

from django.conf import settings


def client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


def validate_origin(widget, request) -> bool:
    """Browsers always send Origin on these cross-site requests; a request without one is not from a website."""
    return widget.origin_allowed(request.headers.get("Origin"))


def apply_widget_cors(response, request, widget):
    """CORS headers for one widget: only its own allowed sites, and never with cookies. The app's global CORS
    settings (which allow cookies for our own frontend) do not cover these URLs; see CORS_URLS_REGEX."""
    origin = request.headers.get("Origin", "")
    if origin and widget.origin_allowed(origin):
        response["Access-Control-Allow-Origin"] = origin
        response["Access-Control-Allow-Credentials"] = "false"
        response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response["Access-Control-Allow-Headers"] = request.headers.get("Access-Control-Request-Headers", "") or "Content-Type"
        response["Access-Control-Max-Age"] = "600"
        vary = response.get("Vary", "")
        if "Origin" not in vary:
            response["Vary"] = (vary + ", Origin").strip(", ")
    return response


def api_base(request=None) -> str:
    """The backend's public address, which the widget script calls from the hospital's website."""
    base = getattr(settings, "PUBLIC_BASE_URL", "") or getattr(settings, "BACKEND_URL", "")
    if not base and request is not None:
        base = f"{request.scheme}://{request.get_host()}"
    return base.rstrip("/")


def bundle_url() -> str:
    """Where widget.js is served: by default from the frontend, which builds it into /widget/widget.js."""
    return getattr(settings, "WIDGET_BUNDLE_URL", "") or f"{settings.FRONTEND_URL.rstrip('/')}/widget/widget.js"


def build_embed_snippet(widget, request=None) -> str:
    """What the hospital pastes into its website. data-api-base is needed because the script is served by
    the frontend while the API is on another host."""
    tag = (f'<script async src="{bundle_url()}" data-public-key="{widget.public_key}" '
           f'data-api-base="{api_base(request)}"></script>')
    if widget.mode != widget.Mode.HEADLESS:
        return tag
    return (
        f"{tag}\n"
        "<script>\n"
        "  // Start a call from your own button.\n"
        '  document.querySelector("#your-button").addEventListener("click", () => {\n'
        f'    const call = window.HeyKoalaVoice.start({{ publicKey: "{widget.public_key}" }})\n'
        '    call.on("status", (status) => console.log("call status:", status))\n'
        '    call.on("transcript", (line) => console.log(line.role, line.text))\n'
        '    call.on("end", () => console.log("call ended"))\n'
        "    // call.mute(); call.unmute(); call.sendText(\"hi\"); call.end()\n"
        "  })\n"
        "</script>"
    )
