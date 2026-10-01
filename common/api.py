from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied

from accounts.permissions import AuthenticatedAndVerified
from common.audit import log_data_change, is_sensitive_field


class ClinicScopedModelViewSet(viewsets.ModelViewSet):
    permission_classes = [AuthenticatedAndVerified]
    def get_queryset(self):
        return super().get_queryset().filter(clinic=self.request.clinic)
    def perform_create(self, serializer):
        instance = serializer.save(clinic=self.request.clinic)
        log_data_change(instance, "create", request=self.request)
        return instance

    def perform_update(self, serializer):
        if serializer.instance.clinic_id != self.request.clinic.id:
            raise PermissionDenied("Cross-clinic updates are not permitted.")
        
        # Log sensitive field changes
        instance = serializer.instance
        model_name = instance.__class__.__name__
        
        for field, new_value in serializer.validated_data.items():
            old_value = getattr(instance, field, None)
            if old_value != new_value and is_sensitive_field(model_name, field):
                from common.audit import log_sensitive_field_change
                log_sensitive_field_change(instance, field, old_value, new_value, request=self.request)
        
        instance = serializer.save()
        log_data_change(instance, "update", request=self.request)
        return instance

    def perform_destroy(self, instance):
        if instance.clinic_id != self.request.clinic.id:
            raise PermissionDenied("Cross-clinic deletes are not permitted.")
        
        # Soft delete if model has is_active field
        if hasattr(instance, 'is_active'):
            instance.is_active = False
            instance.save()
            log_data_change(instance, "deactivate", request=self.request)
        else:
            log_data_change(instance, "delete", request=self.request)
            instance.delete()
