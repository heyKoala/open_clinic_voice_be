"""API for the hospital's knowledge base (ai_agent.knowledge).

Admins (AI Agent → Knowledge):
  GET/POST  /ai/knowledge/                 list documents; add one (file upload, url, or text)
  GET/PATCH/DELETE /ai/knowledge/<id>/     one document; switch it on/off; delete it (and its chunks)
  POST      /ai/knowledge/<id>/reindex/    read it again (e.g. a web page that changed)
  POST      /ai/knowledge/ask/             {"question"}: what the AI would answer, to try it out

The voice agent, during a call (token in the path, like the other tools):
  POST      /ai/tools/rock8/<clinic_id>/<token>/knowledge/   {"user_query"} -> {"result": "..."}
"""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from accounts.permissions import IsClinicAdmin
from ai_agent import knowledge
from ai_agent.models import KnowledgeDocument
from ai_agent.tool_views import AIToolBaseView
from common.audit import log_data_change
from clinics.views import _owned_root


class KnowledgeDocumentSerializer(serializers.ModelSerializer):
    file_name = serializers.SerializerMethodField()
    # A file upload is a form, and a form with no is_active field would otherwise read as "off".
    is_active = serializers.BooleanField(required=False, default=True)

    class Meta:
        model = KnowledgeDocument
        fields = ["id", "name", "type", "file", "file_name", "url", "text", "status", "error", "chunk_count",
                  "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "status", "error", "chunk_count", "created_at", "updated_at"]
        extra_kwargs = {"file": {"write_only": True, "required": False}, "text": {"required": False},
                        "url": {"required": False}, "name": {"required": False}}

    def get_file_name(self, obj):
        return obj.file.name.rsplit("/", 1)[-1] if obj.file else ""

    def validate(self, attrs):
        kind = attrs.get("type", getattr(self.instance, "type", None))
        if self.instance is None:
            if kind == KnowledgeDocument.Type.FILE:
                upload = attrs.get("file")
                if not upload:
                    raise serializers.ValidationError({"file": "Choose a file to upload."})
                ext = upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else ""
                if ext not in knowledge.ALLOWED_EXTENSIONS:
                    raise serializers.ValidationError({"file": "Use PDF, Word (.docx), text, Markdown or CSV."})
                if upload.size > knowledge.MAX_FILE_BYTES:
                    raise serializers.ValidationError({"file": "Files up to 20 MB only."})
                attrs.setdefault("name", upload.name)
            elif kind == KnowledgeDocument.Type.URL:
                if not attrs.get("url"):
                    raise serializers.ValidationError({"url": "Enter the page's address."})
                attrs.setdefault("name", attrs["url"])
            elif kind == KnowledgeDocument.Type.TEXT:
                if not (attrs.get("text") or "").strip():
                    raise serializers.ValidationError({"text": "Paste some text."})
                if not attrs.get("name"):
                    raise serializers.ValidationError({"name": "Give it a name, e.g. 'Consultation fees'."})
            else:
                raise serializers.ValidationError({"type": "Choose file, url or text."})
        attrs["name"] = (attrs.get("name") or getattr(self.instance, "name", ""))[:255]
        return attrs


class KnowledgeDocumentViewSet(viewsets.ModelViewSet):
    serializer_class = KnowledgeDocumentSerializer
    permission_classes = [IsClinicAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def _root(self):
        root = _owned_root(self.request)
        if root is None:
            raise serializers.ValidationError({"detail": "Only the main hospital's admins manage the knowledge base."})
        return root

    def get_queryset(self):
        return KnowledgeDocument.objects.filter(clinic__in=self.request.clinic.root.group_clinics())

    def perform_create(self, serializer):
        document = serializer.save(clinic=self._root())
        log_data_change(document, "create", request=self.request, metadata={"knowledge": document.name})
        knowledge.index(document)

    def perform_update(self, serializer):
        document = serializer.save()
        # A changed text is read again; a changed name or switch isn't.
        if "text" in serializer.validated_data and document.type == KnowledgeDocument.Type.TEXT:
            knowledge.index(document)

    def perform_destroy(self, instance):
        log_data_change(instance, "delete", request=self.request, metadata={"knowledge": instance.name})
        if instance.file:
            instance.file.delete(save=False)
        instance.delete()  # chunks go with it (CASCADE)

    @action(detail=True, methods=["post"])
    def reindex(self, request, pk=None):
        document = self.get_object()
        knowledge.index(document)
        return Response(self.get_serializer(document).data)

    @action(detail=False, methods=["post"])
    def ask(self, request):
        question = str(request.data.get("question") or "").strip()
        if not question:
            return Response({"detail": "Ask a question."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(knowledge.answer(request.clinic, question))


class AIToolKnowledgeView(AIToolBaseView):
    """What the agent calls mid-conversation. Always answers 200 with a spoken sentence."""

    def post(self, request, *args, **kwargs):
        clinic = self.get_clinic(kwargs)
        question = str(request.data.get("user_query") or request.data.get("query") or "").strip()
        if not question:
            return Response({"success": False, "result": "What would you like to know?"})
        result = knowledge.answer(clinic, question)
        return Response({"success": True, "found": result["found"], "result": result["answer"]})
