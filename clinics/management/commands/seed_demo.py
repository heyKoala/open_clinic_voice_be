import random
from datetime import timedelta, time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from faker import Faker

from clinics.models import Clinic
from accounts.models import User, Invitation
from doctors.models import Doctor, DoctorAbsence
from patients.models import Patient, MessageLog
from appointments.models import Appointment
from queue_mgmt.models import QueueToken
from clinical.models import ClinicalEncounter, PatientHealthRecord, SymptomSummary, ClinicalAttachment
from ai_agent.models import CallLog
from followups.models import FollowUp
from django.db.models import Q

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

class Command(BaseCommand):
    help = "Generate highly realistic synthetic clinic demo data"

    def add_arguments(self, parser):
        parser.add_argument('--clear', action='store_true', help='Clear existing demo data')
        parser.add_argument('--seed', type=int, help='Seed for deterministic random generation')

    def handle(self, *args, **options):
        seed_val = options.get('seed')
        if seed_val is not None:
            random.seed(seed_val)
            Faker.seed(seed_val)
            self.stdout.write(f"Using deterministic seed: {seed_val}")
        
        fake = Faker('en_IN')
        
        with transaction.atomic():
            clinic1, _ = Clinic.objects.get_or_create(name="ManageOPD Care Center")
            clinic2, _ = Clinic.objects.get_or_create(name="ManageOPD Downtown Branch")
            if clinic2.parent_id != clinic1.id:
                # Downtown is a centre of Care Center: it shares Care Center's phone number.
                clinic2.parent = clinic1
                clinic2.save(update_fields=["parent", "updated_at"])
            clinics_to_seed = [clinic1, clinic2]
            
            admin_user = User.objects.filter(email="admin@manageopd.local").first()
            if not admin_user:
                admin_user = User.objects.create_user(
                    email="admin@manageopd.local",
                    password="password123",
                    full_name="Likhit Admin",
                    role=User.Role.CLINIC_ADMIN,
                    clinic=clinic1,
                    is_clinic_admin=True,
                    is_doctor=True, # Dual role
                    is_verified=True
                )
                self.stdout.write(self.style.SUCCESS("Created dual-role Admin/Doctor user."))
            else:
                admin_user.is_doctor = True
                admin_user.save()
            
            # Associate admin with both clinics
            admin_user.clinics.add(clinic1, clinic2)

            if options.get('clear'):
                self.stdout.write("Clearing existing demo data for the demo clinics...")
                in_seed = Q(clinic__in=clinics_to_seed)
                by_doctor = in_seed | Q(doctor__clinic__in=clinics_to_seed)
                by_patient = by_doctor | Q(patient__clinic__in=clinics_to_seed)
                FollowUp.objects.filter(by_patient).delete()
                ClinicalAttachment.objects.filter(in_seed).delete()
                SymptomSummary.objects.filter(in_seed).delete()
                PatientHealthRecord.objects.filter(in_seed).delete()
                ClinicalEncounter.objects.filter(by_patient).delete()
                QueueToken.objects.filter(by_patient).delete()
                Appointment.objects.filter(by_patient).delete()
                MessageLog.objects.filter(in_seed).delete()
                CallLog.objects.filter(in_seed).delete()
                Invitation.objects.filter(in_seed).delete()
                DoctorAbsence.objects.filter(in_seed).delete()
                Doctor.objects.filter(Q(clinic__in=clinics_to_seed) | Q(user=admin_user)).delete()
                Patient.objects.filter(in_seed).delete()
                User.objects.filter(in_seed).exclude(id=admin_user.id).delete()
            
            for c_idx, clinic in enumerate(clinics_to_seed):
                self.stdout.write(self.style.SUCCESS(f"Seeding data for clinic: {clinic.name}"))

                # 1. GENERATE DOCTORS
                self.stdout.write("Generating Doctors...")
                specialties = [
                    "General Practice", "Cardiology", "Pediatrics", "Orthopedics", 
                    "Dermatology", "Gynecology", "Neurology", "ENT", "Ophthalmology", "Psychiatry"
                ]
                
                # Dual Role Admin — Doctor.user is one-to-one, so the admin practises only at the
                # primary clinic; reusing the profile elsewhere would put this clinic's
                # appointments on another clinic's doctor.
                doctors_list = []
                if c_idx == 0:
                    doc_admin, _ = Doctor.objects.get_or_create(
                        user=admin_user,
                        clinic=clinic,
                        defaults={
                            'full_name': admin_user.full_name,
                            'specialty': specialties[0],
                            'consultation_minutes': 15,
                            'max_patients_per_day': 30,
                            'available_from': time(9, 0),
                            'available_to': time(17, 0),
                            'lunch_from': time(13, 0),
                            'lunch_to': time(14, 0),
                            'working_days': [1,2,3,4,5,6]
                        }
                    )
                    doctors_list.append(doc_admin)

                for i in range(1, 5 if c_idx == 1 else 10):
                    d_email = f"doctor{i}_c{c_idx}@manageopd.local"
                    d_name = fake.name()
                    if d_name.startswith("Dr. "):
                        d_name = d_name[4:]
                        
                    if User.objects.filter(email=d_email).exists():
                        u = User.objects.get(email=d_email)
                    else:
                        u = User.objects.create_user(
                            email=d_email,
                            password="password123",
                            full_name=d_name,
                            role=User.Role.DOCTOR,
                            clinic=clinic,
                            is_doctor=True,
                            is_verified=True
                        )
                    doc, _ = Doctor.objects.get_or_create(
                        user=u,
                        clinic=clinic,
                        defaults={
                            'full_name': d_name,
                            'specialty': specialties[i % len(specialties)],
                            'consultation_minutes': random.choice([10, 15, 20, 30]),
                            'max_patients_per_day': random.choice([20, 30, 40, 50]),
                            'available_from': time(random.choice([8, 9, 10]), 0),
                            'available_to': time(random.choice([16, 17, 18]), 0),
                            'lunch_from': time(13, 0),
                            'lunch_to': time(14, 0),
                            'working_days': [1,2,3,4,5,6] if random.random() > 0.3 else [1,2,3,4,5]
                        }
                    )
                    doctors_list.append(doc)
                
                # 2. GENERATE RECEPTIONISTS
                self.stdout.write("Generating Receptionists...")
                for i in range(1, 3):
                    r_email = f"receptionist{i}_c{c_idx}@manageopd.local"
                    if not User.objects.filter(email=r_email).exists():
                        User.objects.create_user(
                            email=r_email,
                            password="password123",
                            full_name=fake.name(),
                            role=User.Role.RECEPTIONIST,
                            clinic=clinic,
                            is_verified=True
                        )

                # 3. GENERATE PATIENTS
                self.stdout.write("Generating Patients...")
                patients_list = []
                blood_groups = [c[0] for c in Patient.BloodGroup.choices if c[0] != 'unknown']
                genders = [c[0] for c in Patient.Gender.choices if c[0] != 'unspecified']
                
                num_patients = 50 if c_idx == 1 else 150
                
                # If we are in clinic2 (c_idx == 1) and have shared_patient_data from clinic1
                if c_idx == 1 and getattr(self, 'shared_patient_data', []):
                    for p_data in self.shared_patient_data:
                        # Copy the data but assign to current clinic
                        new_p_data = dict(p_data)
                        new_p_data['clinic'] = clinic
                        p = Patient.objects.create(**new_p_data)
                        patients_list.append(p)
                        num_patients -= 1
                        
                for i in range(num_patients):
                    phone = f"+91{fake.numerify('##########')}"
                    if Patient.objects.filter(clinic=clinic, phone=phone).exists():
                        continue

                    p_kwargs = dict(
                        clinic=clinic,
                        full_name=fake.name(),
                        date_of_birth=fake.date_of_birth(minimum_age=2, maximum_age=85),
                        gender=random.choice(genders),
                        blood_group=random.choice(blood_groups),
                        phone=phone,
                        email=fake.email() if random.random() > 0.5 else "",
                        emergency_contact_name=fake.name(),
                        emergency_contact_phone=f"+91{fake.numerify('##########')}",
                        preferred_language=random.choice(['en', 'hi', 'kn', 'ta', 'te']),
                        abha_number=fake.numerify('##-####-####-####') if random.random() > 0.7 else "",
                        notes='Patient prefers early morning appointments.' if random.random() > 0.5 else 'Requires assistance with mobility.' if random.random() > 0.8 else ""
                    )
                    p = Patient.objects.create(**p_kwargs)
                    patients_list.append(p)
                    
                    # Store first 20 patients of clinic 1 to be shared with clinic 2
                    if c_idx == 0:
                        if not hasattr(self, 'shared_patient_data'):
                            self.shared_patient_data = []
                        if len(self.shared_patient_data) < 20:
                            # save kwargs except clinic
                            del p_kwargs['clinic']
                            self.shared_patient_data.append(p_kwargs)
                    
                    if random.random() > 0.5:
                        cond = random.choice(CONDITIONS)
                        PatientHealthRecord.objects.create(
                            clinic=clinic, patient=p,
                            category=random.choice([c[0] for c in PatientHealthRecord.RecordCategory.choices]),
                            name=cond[0],
                            details=cond[1]
                        )

                # 4. GENERATE TIMELINE
                self.stdout.write("Generating Timeline (-7 to +7 days)...")
                today = timezone.localdate()
                start_date = today - timedelta(days=7)
                
                total_appts = 0
                total_encounters = 0
                
                for day_offset in range(15):
                    current_date = start_date + timedelta(days=day_offset)
                    is_past = current_date < today
                    is_today = current_date == today
                    
                    for doc in doctors_list:
                        if current_date.isoweekday() not in doc.working_days:
                            continue
                            
                        upper_bound = max(1, int(doc.max_patients_per_day * 0.8))
                        num_appointments = random.randint(doc.max_patients_per_day // 4, upper_bound)
                        
                        start_dt = timezone.make_aware(timezone.datetime.combine(current_date, doc.available_from))
                        end_dt = timezone.make_aware(timezone.datetime.combine(current_date, doc.available_to))
                        lunch_start = timezone.make_aware(timezone.datetime.combine(current_date, doc.lunch_from)) if doc.lunch_from else None
                        lunch_end = timezone.make_aware(timezone.datetime.combine(current_date, doc.lunch_to)) if doc.lunch_to else None
                        
                        curr_time = start_dt
                        slots = []
                        while curr_time + timedelta(minutes=doc.consultation_minutes) <= end_dt:
                            slot_end = curr_time + timedelta(minutes=doc.consultation_minutes)
                            if lunch_start and lunch_end and (curr_time >= lunch_start and curr_time < lunch_end):
                                curr_time = lunch_end
                                continue
                            slots.append(curr_time)
                            curr_time = slot_end
                            
                        booked_slots = random.sample(slots, min(num_appointments, len(slots)))
                        booked_slots.sort()
                        
                        queue_number = 1
                        
                        for slot in booked_slots:
                            patient = random.choice(patients_list)
                            appt_end = slot + timedelta(minutes=doc.consultation_minutes)
                            
                            if is_past:
                                status = random.choices(['completed', 'cancelled', 'no_show'], weights=[0.8, 0.1, 0.1])[0]
                            elif is_today:
                                status = random.choices(['scheduled', 'checked_in', 'in_consultation', 'completed', 'cancelled'], weights=[0.2, 0.2, 0.2, 0.3, 0.1])[0]
                            else:
                                status = random.choices(['scheduled', 'cancelled'], weights=[0.9, 0.1])[0]

                            source = 'manual' if random.random() > 0.2 else 'walk_in'
                            priority = 'normal' if random.random() > 0.1 else random.choice(['urgent', 'emergency'])
                                
                            appt = Appointment.objects.create(
                                clinic=clinic, patient=patient, doctor=doc,
                                starts_at=slot, ends_at=appt_end,
                                reason=random.choice(REASONS),
                                # "in_consultation" is a queue-token state; the appointment itself is checked in.
                                status='checked_in' if status == 'in_consultation' else status,
                                priority=priority, source=source
                            )
                            total_appts += 1
                            
                            if is_today and status in ['checked_in', 'in_consultation', 'completed']:
                                q_status = QueueToken.Status.CHECKED_IN if status == 'checked_in' else (QueueToken.Status.IN_CONSULTATION if status == 'in_consultation' else QueueToken.Status.COMPLETED)
                                QueueToken.objects.get_or_create(
                                    clinic=clinic, doctor=doc, service_date=current_date, token_number=queue_number,
                                    defaults={'appointment': appt, 'patient': patient, 'status': q_status}
                                )
                                queue_number += 1
                                
                            if status == 'completed':
                                enc = ClinicalEncounter.objects.create(
                                    clinic=clinic, patient=patient, doctor=doc, appointment=appt,
                                    chief_complaint=appt.reason,
                                    status=ClinicalEncounter.EncounterStatus.COMPLETED
                                )
                                total_encounters += 1
                                if random.random() > 0.5:
                                    SymptomSummary.objects.create(
                                        clinic=clinic, encounter=enc,
                                        summary_text=random.choice(SUMMARIES), status='approved',
                                        ai_model='gpt-4o-mini', ai_version='1.0', confidence=0.92
                                    )
                                    
                            if random.random() > 0.7:
                                MessageLog.objects.create(
                                    clinic=clinic, patient=patient,
                                    method=random.choice([c[0] for c in MessageLog.Method.choices]),
                                    message_text=f"Appointment {status}: {appt.reason}", is_sent=True,
                                    status=MessageLog.Status.SENT,
                                )
                                
                # 5. GENERATE FOLLOW-UPS for a sample of completed visits
                self.stdout.write("Generating Follow-ups...")
                completed = list(Appointment.objects.filter(clinic=clinic, status='completed').select_related('patient', 'doctor')[:200])
                total_followups = 0
                for appt in random.sample(completed, min(len(completed), 25)):
                    due = appt.starts_at + timedelta(days=random.choice([3, 7, 10, 14]))
                    if due < timezone.now():
                        fu_status = random.choices(['completed', 'failed', 'cancelled'], weights=[0.7, 0.2, 0.1])[0]
                    else:
                        fu_status = random.choice(['pending', 'scheduled'])
                    FollowUp.objects.create(
                        clinic=clinic, patient=appt.patient, doctor=appt.doctor, appointment=appt,
                        scheduled_for=due,
                        method=random.choice([m[0] for m in FollowUp.Method.choices]),
                        status=fu_status,
                        notes=f"Review after visit for {appt.reason.lower()}.",
                        outcome="Patient reports improvement." if fu_status == 'completed' else "",
                    )
                    total_followups += 1

                # 6. GENERATE INVITATIONS
                self.stdout.write("Generating Invitations...")
                for i in range(5):
                    Invitation.objects.create(
                        clinic=clinic, invited_by=admin_user, email=f"invited{i}_c{c_idx}@manageopd.local",
                        role=User.Role.DOCTOR, token_hash=fake.sha256(),
                        used_at=timezone.now() if random.random() > 0.5 else None,
                        expires_at=timezone.now() + timedelta(days=1) if random.random() > 0.5 else timezone.now() - timedelta(days=1)
                    )

                # ── 6. SEED AI CALL LOGS ─────────────────────────────────────────────
                self.stdout.write("Seeding AI Call Logs...")

                CALL_OUTCOMES = [
                    ("appointment_booked", 0.40),
                    ("appointment_booked", 0.10),
                    ("appointment_cancelled", 0.12),
                    ("information_only", 0.15),
                    ("unanswered", 0.08),
                    ("appointment_booked", 0.08),
                    ("other", 0.07),
                ]
                ALL_OUTCOMES = [o for o, _ in CALL_OUTCOMES]
                OUTCOME_WEIGHTS = [w for _, w in CALL_OUTCOMES]

                SAMPLE_TRANSCRIPTS = [
                    "AI: Welcome to {clinic}. How can I help you today?\nPatient: Hi, I'd like to book an appointment.\nAI: Of course! Which specialty do you need?\nPatient: General physician.\nAI: Great, Dr. Sharma is available tomorrow at 10 AM. Shall I confirm that?\nPatient: Yes, please.\nAI: Done! Your appointment is confirmed.",
                    "AI: Welcome to {clinic}. How can I help you today?\nPatient: I need to cancel my appointment.\nAI: I'll help with that. Could I have your name?\nPatient: Rahul Mehta.\nAI: Found it. Your appointment with Dr. Nair on Sept 17 has been cancelled.\nPatient: Thank you.",
                    "AI: Welcome to {clinic}. How can I help you today?\nPatient: What are your clinic hours?\nAI: We're open Monday to Saturday, 9 AM to 6 PM.\nPatient: Thanks, that's all I needed.",
                    "AI: Welcome to {clinic}. How can I help you today?\nPatient: I want to see a cardiologist.\nAI: Sure! We have Dr. Patel available. Would Thursday at 11 AM work?\nPatient: Perfect, book it.\nAI: Your appointment is confirmed with Dr. Patel on Thursday at 11 AM.",
                    "AI: Welcome to {clinic}. How can I help you today?\nPatient: [silence]\nAI: Hello, are you there?\nPatient: [call disconnected]",
                ]

                total_calls_seeded = 0
                now = timezone.now()

                for day_offset in range(30, 0, -1):
                    call_date = now - timedelta(days=day_offset)
                    daily_count = random.randint(2, 8)
                    for _ in range(daily_count):
                        patient = random.choice(patients_list) if patients_list else None
                        outcome = random.choices(ALL_OUTCOMES, weights=OUTCOME_WEIGHTS)[0]
                        direction = random.choices(
                            [CallLog.Direction.INBOUND, CallLog.Direction.OUTBOUND],
                            weights=[0.80, 0.20]
                        )[0]
                        duration = random.randint(15, 320)
                        transcript_template = random.choice(SAMPLE_TRANSCRIPTS)
                        transcript = transcript_template.replace("{clinic}", clinic.name)
                        occurred = call_date.replace(
                            hour=random.randint(9, 17),
                            minute=random.randint(0, 59),
                            second=0, microsecond=0
                        )
                        summary = f"Patient {'booked' if 'booked' in outcome else 'cancelled' if 'cancel' in outcome else 'inquired about'} an appointment. Duration was {duration} seconds. Handled successfully by ManageOPD AI."
                        if outcome == "unanswered":
                            summary = "Call was unanswered or disconnected by the patient."
                            
                        CallLog.objects.create(
                            clinic=clinic,
                            patient=patient,
                            direction=direction,
                            agent_name="ManageOPD AI",
                            language="en",
                            duration_seconds=duration,
                            outcome=outcome,
                            occurred_at=occurred,
                            transcript=transcript,
                            recording_url="https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
                            summary=summary,
                        )
                        total_calls_seeded += 1

                self.stdout.write(f"AI Call Logs seeded: {total_calls_seeded}")
                self.stdout.write(self.style.SUCCESS("-" * 40))
                self.stdout.write(self.style.SUCCESS(f"SEEDING COMPLETE FOR {clinic.name}. SUMMARY:"))
                self.stdout.write(f"Doctors created: {len(doctors_list)}")
                self.stdout.write(f"Receptionists created: 2")
                self.stdout.write(f"Patients created: {len(patients_list)}")
                self.stdout.write(f"Appointments generated: {total_appts}")
                self.stdout.write(f"Clinical Encounters generated: {total_encounters}")
                self.stdout.write(f"Follow-ups generated: {total_followups}")
                self.stdout.write(self.style.SUCCESS("-" * 40))
