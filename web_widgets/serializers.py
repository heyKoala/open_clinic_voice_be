import re

from rest_framework import serializers

from web_widgets.models import DEFAULT_BRANDING, WebWidget
from web_widgets.utils import build_embed_snippet

COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
# An origin is scheme + host (+ port), with no path: what the browser sends in the Origin header.
ORIGIN = re.compile(r"^https?://[a-z0-9.\-]+(:\d{1,5})?$")
POSITIONS = ("bottom-right", "bottom-left")


class WebWidgetSerializer(serializers.ModelSerializer):
    branding = serializers.JSONField(required=False)
    allowed_origins = serializers.ListField(child=serializers.CharField(max_length=255), required=False, max_length=50)
    embed_snippet = serializers.SerializerMethodField()

    class Meta:
        model = WebWidget
        fields = [
            "id", "name", "public_key", "allowed_origins", "is_active", "mode", "show_text_input", "branding",
            "greeting_message", "embed_snippet", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "public_key", "created_at", "updated_at"]

    def get_embed_snippet(self, obj) -> str:
        return build_embed_snippet(obj, self.context.get("request"))

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["branding"] = instance.resolved_branding
        return data

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Give the widget a name.")
        return value

    def validate_allowed_origins(self, value):
        cleaned = []
        for raw in value:
            origin = raw.strip().lower().rstrip("/")
            if not origin:
                continue
            if origin != "*" and not ORIGIN.match(origin):
                raise serializers.ValidationError(
                    f'"{raw}" is not a website address. Use the form https://www.example.com, with no path after it.'
                )
            if origin not in cleaned:
                cleaned.append(origin)
        return cleaned

    def validate_branding(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Branding must be an object.")
        branding = {key: value[key] for key in DEFAULT_BRANDING if value.get(key) not in (None, "")}
        color = branding.get("primary_color")
        if color is not None and not (isinstance(color, str) and COLOR.match(color)):
            raise serializers.ValidationError("The colour must be a hex code such as #111827.")
        if "position" in branding and branding["position"] not in POSITIONS:
            raise serializers.ValidationError("The position must be bottom-right or bottom-left.")
        label = branding.get("button_label")
        if label is not None:
            if not isinstance(label, str) or len(label.strip()) > 40:
                raise serializers.ValidationError("The button label can be at most 40 characters.")
            branding["button_label"] = label.strip()
        return branding

    def validate_greeting_message(self, value):
        value = value.strip()
        if len(value) > 300:
            raise serializers.ValidationError("The greeting can be at most 300 characters.")
        return value
