from __future__ import annotations

import re

from rest_framework import serializers

PHONE_CHARS = re.compile(r"^[0-9+()\-.\s]+$")


class LeadSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    email = serializers.EmailField(max_length=254)
    phone = serializers.CharField(max_length=32)
    organisation = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Please enter your name.")
        return value

    def validate_phone(self, value):
        value = value.strip()
        digits = re.sub(r"\D", "", value)
        if not PHONE_CHARS.match(value) or not 7 <= len(digits) <= 15:
            raise serializers.ValidationError("Please enter a valid phone number.")
        return value
