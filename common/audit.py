from django.contrib.auth import get_user_model
from audit.models import DataChangeEvent


User = get_user_model()


def log_data_change(
    instance,
    action: str,
    user=None,
    field_name=None,
    old_value=None,
    new_value=None,
    request=None,
    metadata=None,
):
    """
    Log a data change event for audit purposes.
    
    Args:
        instance: The model instance being changed
        action: The action type (create, update, delete, deactivate, reactivate)
        user: The user performing the action (optional, will use request.user if not provided)
        field_name: The specific field being changed (for updates)
        old_value: The previous value of the field
        new_value: The new value of the field
        request: The HTTP request (to extract IP address and user)
        metadata: Additional metadata to store with the event
    """
    if request and not user:
        user = request.user
    
    # Get clinic from instance if it has one
    clinic = getattr(instance, "clinic", None)
    
    # Get IP address from request
    ip_address = None
    if request and hasattr(request, "META"):
        ip_address = request.META.get("REMOTE_ADDR") or request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
    
    DataChangeEvent.objects.create(
        user=user if user and user.is_authenticated else None,
        clinic=clinic,
        model_name=instance.__class__.__name__,
        object_id=str(instance.pk),
        action=action,
        field_name=field_name,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None,
        ip_address=ip_address,
        metadata=metadata or {},
    )


def log_sensitive_field_change(instance, field_name, old_value, new_value, request=None):
    """
    Log a change to a sensitive field.
    """
    log_data_change(
        instance=instance,
        action=DataChangeEvent.Action.UPDATE,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        request=request,
    )


SENSITIVE_FIELDS = {
    "Patient": ["phone", "email", #"abha_number", 
    "date_of_birth"],
    "Doctor": ["user", "is_active"],
    "Appointment": ["status", "starts_at", "ends_at"],
    "QueueToken": ["status"],
    "FollowUp": ["status", "scheduled_for"],
    "CallLog": ["transcript", "outcome"],
    "AgentConfiguration": ["system_prompt", "is_active"],
    "ClinicConfiguration": ["ai_enabled", "ai_default_language"],
}


def is_sensitive_field(model_name, field_name):
    """Check if a field is considered sensitive for audit logging."""
    return field_name in SENSITIVE_FIELDS.get(model_name, [])
