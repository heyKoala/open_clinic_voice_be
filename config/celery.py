import os
from celery import Celery

# Set the default Django settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('manageopd')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django apps.
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')  # noqa: T201


app.conf.beat_schedule = {
    'auto-complete-expired-appointments': {
        'task': 'appointments.tasks.auto_complete_expired_appointments',
        'schedule': 300.0,  # Run every 5 minutes
    },
    'send-pending-whatsapp-messages': {
        'task': 'patients.tasks.process_pending_messages',
        'schedule': 60.0,
    },
    'cleanup-expired-reports': {
        'task': 'reports.tasks.cleanup_expired_reports',
        'schedule': 60.0 * 60,
    },
}
