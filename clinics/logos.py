"""Hospital logos: vetting an upload, storing it, and serving it.

Django serves no /media/ here, and a logo is shown on every page, so logos have their own small public endpoint
(``/api/v1/clinics/<id>/logo/``). A logo is not private (it is on the hospital's own screens). The danger of an upload
is the file itself, so:

* A PNG or JPEG is opened with Pillow and **re-encoded as a small PNG** (so nothing hidden in the original survives).
* An SVG is only accepted if it is plain drawing (no scripts, event handlers, links, styles or external references),
  and it is served with a policy that blocks scripts even if one slipped through.
* The file name is ours (never the uploader's) and the type comes from the bytes, never from the browser's claim.
"""
from __future__ import annotations

import io
import re
import uuid

from django.core.files.base import ContentFile
from django.http import FileResponse
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from clinics.models import Clinic

MAX_LOGO_BYTES = 1024 * 1024
MAX_PIXELS = 512

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

# Anything in an SVG that can run code, load something, or restyle the page.
UNSAFE_SVG = re.compile(
    r"<\s*(script|foreignObject|iframe|object|embed|use|image|a|style|animate|set|link|audio|video)\b"
    r"|\bon\w+\s*=|javascript:|data:|vbscript:|<!ENTITY|<!DOCTYPE|<\?xml-stylesheet|(xlink:)?href\s*=|url\s*\(",
    re.IGNORECASE,
)

CONTENT_TYPES = {"png": "image/png", "svg": "image/svg+xml"}


def vet_logo(upload) -> tuple[bytes, str]:
    """Return ``(bytes_to_store, extension)`` for an acceptable logo, else raise a ValidationError."""
    data = upload.read(MAX_LOGO_BYTES + 1)
    if len(data) > MAX_LOGO_BYTES:
        raise serializers.ValidationError("The picture must be under 1 MB.")
    if not data:
        raise serializers.ValidationError("The picture is empty.")

    if data.startswith(PNG_MAGIC) or data.startswith(JPEG_MAGIC):
        from PIL import Image, UnidentifiedImageError

        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            raise serializers.ValidationError("This picture could not be read. Use a PNG, JPG or SVG.")
        image = image.convert("RGBA")
        image.thumbnail((MAX_PIXELS, MAX_PIXELS))
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue(), "png"

    head = data[:2000].lstrip().lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in data[:4000].lower():
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise serializers.ValidationError("The SVG must be plain UTF-8 text.")
        if UNSAFE_SVG.search(text):
            raise serializers.ValidationError("This SVG contains scripts, links or styles, which aren't allowed. Export a plain SVG or use a PNG.")
        return data, "svg"

    raise serializers.ValidationError("Use a PNG, JPG or SVG picture.")


def save_logo(clinic: Clinic, upload) -> None:
    """Vet and store the logo for a hospital and point its ``logo_url`` at it. The caller saves the clinic."""
    data, extension = vet_logo(upload)
    previous = clinic.logo_file.name if clinic.logo_file else ""
    clinic.logo_file.save(f"{clinic.id or 'new'}-{uuid.uuid4().hex[:12]}.{extension}", ContentFile(data), save=False)
    clinic.logo_url = f"/api/v1/clinics/{clinic.id}/logo/?v={uuid.uuid4().hex[:8]}"
    if previous:
        try:
            clinic.logo_file.storage.delete(previous)
        except Exception:  # an old file that is already gone is not a reason to fail
            pass


class ClinicLogoView(APIView):
    """The hospital's uploaded logo. Public: it is only a picture, and ``<img>`` tags can't send credentials."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = []

    def get(self, request, pk):
        clinic = Clinic.objects.filter(pk=pk, parent__isnull=True).first()
        if clinic is None or not clinic.logo_file:
            return Response({"detail": "No logo."}, status=404)
        extension = clinic.logo_file.name.rsplit(".", 1)[-1].lower()
        response = FileResponse(clinic.logo_file.open("rb"), content_type=CONTENT_TYPES.get(extension, "application/octet-stream"))
        response["Cache-Control"] = "public, max-age=86400"
        response["X-Content-Type-Options"] = "nosniff"
        # Even if a script got into an SVG, opening the file directly must not run it.
        response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return response
