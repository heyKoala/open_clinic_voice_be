import os, django, random
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from patients.models import Patient
from clinical.models import PatientHealthRecord, SymptomSummary
from appointments.models import Appointment

REASONS = ['Fever and chills', 'Routine checkup', 'Severe headache', 'Follow-up for hypertension', 'Lower back pain', 'Persistent cough', 'Stomach ache', 'Skin rash', 'Annual physical', 'General consultation', 'Joint pain', 'Acidity and heartburn']

CONDITIONS = [
    ('Hypertension', 'Diagnosed 2 years ago, managed with medication.'),
    ('Type 2 Diabetes', 'HbA1c levels slightly elevated.'),
    ('Asthma', 'Occasional flare-ups during winter.'),
    ('Allergy: Penicillin', 'Severe allergic reaction reported in childhood.'),
    ('Migraine', 'Experiences monthly episodes.'),
    ('Hypothyroidism', 'Taking Levothyroxine daily.'),
    ('GERD', 'Advised dietary changes.')
]

SUMMARIES = [
    'Patient presented with complaints of a mild fever and persistent cough for the last 3 days. No history of breathing difficulties. Advised rest and prescribed antipyretics.',
    'Follow-up visit for hypertension. Blood pressure is well-controlled at 120/80. Advised to continue current medication and maintain a low-sodium diet.',
    'Patient reported severe lower back pain radiating to the left leg. Suspected sciatica. Recommended MRI and physiotherapy.',
    'Routine physical examination. Vitals are stable. Ordered a complete blood count and lipid profile for annual screening.',
    'Consultation for a skin rash on the arms and chest, itchy in nature. Likely allergic dermatitis. Prescribed topical corticosteroids and antihistamines.'
]

for appt in Appointment.objects.all():
    appt.reason = random.choice(REASONS)
    appt.save(update_fields=['reason'])

for hr in PatientHealthRecord.objects.all():
    cond = random.choice(CONDITIONS)
    hr.name = cond[0]
    hr.details = cond[1]
    hr.save(update_fields=['name', 'details'])

for s in SymptomSummary.objects.all():
    s.summary_text = random.choice(SUMMARIES)
    s.save(update_fields=['summary_text'])

for p in Patient.objects.all():
    if p.notes:
        p.notes = 'Patient prefers early morning appointments.' if random.random() > 0.5 else 'Requires assistance with mobility.'
        p.save(update_fields=['notes'])

print('Data fixed!')
