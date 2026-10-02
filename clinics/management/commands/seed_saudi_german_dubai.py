"""Seed one centre modelled on Saudi German Hospital Dubai (https://www.saudigerman.com/dubai/).

Creates the hospital as a single main clinic with its details, opening hours and AI receptionist
greeting/prompt, an admin, five doctors of different specialties and one receptionist. Every
account (admin, receptionist and each doctor) can sign in with its email and PASSWORD below. Safe to
run again: existing records are updated in place and their passwords are set back to PASSWORD.

    python manage.py seed_saudi_german_dubai

The hospital's facts and the doctors' names come from the hospital's public listings. The doctors'
weekly schedules, the email addresses and the admin/receptionist accounts are made up for the demo.
"""
from datetime import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from ai_agent.models import VoiceAgentSettings
from clinics.models import Clinic, ClinicConfiguration
from doctors.models import Doctor
from subscriptions.models import ClinicEntitlement, SubscriptionPlan

CLINIC_NAME = "Saudi German Hospital Dubai"
EMAIL_DOMAIN = "sghdubai.local"
PASSWORD = "password123"

CLINIC = {
    "address": "Hessa Street 331 West, Al Barsha 3, Exit 36 Sheikh Zayed Road, opposite the American School of Dubai, "
               "Dubai, United Arab Emirates",
    "clinic_type": Clinic.ClinicType.MULTI_DOCTOR,
    "phone": "+971 4 389 0000",
    "website": "https://www.saudigerman.com/dubai/",
    "description": (
        "Saudi German Hospital Dubai is a multi-specialty tertiary care hospital in Al Barsha 3, part of the Saudi German "
        "Health group, and has served Dubai since 2012. It is accredited by JCI and CAP. The outpatient department covers "
        "more than 35 specialties, including cardiology, orthopedics, pediatrics, obstetrics and gynecology, internal "
        "medicine, general surgery, neurology, oncology, nephrology, ophthalmology and ENT. The Emergency Department is "
        "open 24 hours a day, 7 days a week. Appointments can also be booked on the toll-free number 800 2211."
    ),
    "facilities": (
        "24/7 Emergency Department, pharmacy, laboratory and radiology. Adult intensive care (24 beds), neonatal intensive "
        "care (12 beds) and pediatric intensive care (11 beds). Six operating theatres, two cardiac catheterisation labs "
        "and a 10-bed dialysis unit. Physiotherapy and rehabilitation centre. Private and VIP rooms, cafe, free Wi-Fi, "
        "and interpreters for international patients."
    ),
    "holiday_calendar": (
        "Outpatient clinics run from 8 AM to 8 PM. The Emergency Department never closes, including on public holidays."
    ),
    "subscription_status": Clinic.SubscriptionStatus.ACTIVE,
    "is_onboarded": True,
}

CONFIGURATION = {
    "timezone": "Asia/Dubai",
    "working_hours_start": time(8, 0),
    "working_hours_end": time(20, 0),
    "default_consultation_minutes": 20,
    "ai_default_language": "en",
    "token_prefix": "SGH",
}

FIRST_MESSAGE = "Thank you for calling {clinic_name}. How may I help you today?"

# Call-specific details (today's date, the caller's number, the hospital's address and facilities)
# are appended to this automatically when a call starts.
SYSTEM_PROMPT = (
    "You are the receptionist for {clinic_name}, a multi-specialty hospital in Al Barsha 3, Dubai, answering phone calls "
    "from patients. Be warm, polite and professional. Speak English by default and reply in Arabic if the caller speaks Arabic. "
    "Your main tasks are: "
    "1. Schedule, reschedule, or cancel outpatient appointments. "
    "2. If an appointment slot is unavailable or the doctor is away, suggest the next available slot. "
    "3. If the patient wants to book an appointment, DO NOT list all doctors at once. First, ask which specialty they need "
    "(e.g., Cardiology, Orthopedics, Pediatrics, Obstetrics and Gynecology, Internal Medicine). "
    "4. Once they specify a specialty, use the provided tools to fetch doctors in that specialty, and only list those specific doctors to the patient. "
    "5. To cancel an appointment, FIRST use the lookup_appointments tool to search for it using their name or phone number. "
    "6. If the lookup tool returns multiple appointments (e.g., people with the same first name), read out the doctor names, dates, and times, "
    "and ask the patient to confirm which one is theirs before you cancel it. "
    "7. Answer questions about the hospital's location, outpatient hours (8 AM to 8 PM) and facilities using only the details you are given. "
    "Use the provided tools to fetch available slots and manage appointments. "
    "SAFETY: "
    "1. You are not a doctor. Never diagnose, recommend medicines or give medical advice; offer an appointment with the right specialty instead. "
    "2. If the caller describes an emergency (chest pain, difficulty breathing, signs of a stroke, heavy bleeding, loss of consciousness, "
    "a serious accident), tell them to call 998 for an ambulance or come straight to the Emergency Department, which is open 24 hours. Do not book an appointment for it. "
    "3. If you do not know something (for example insurance coverage or prices), say so and offer the hospital's main number. Never guess. "
    "CRITICAL BEHAVIOR: "
    "1. KEEP YOUR RESPONSES EXTREMELY SHORT AND CONCISE. "
    "2. Never use more than 1 or 2 short sentences. "
    "3. Do not list out all available options unless asked. "
    "4. Respond naturally and quickly, as if you are on a real, fast-paced phone call. "
    "BOOKING RULES: "
    "1. Before booking, you MUST ask for the patient's full name. Never book with a placeholder such as 'Unknown', 'Caller' or 'Patient'. "
    "2. You also need the patient's phone number (see CALLER below). "
    "3. Before calling book_appointment, repeat the doctor, centre, date, time and patient name back to the caller and get a yes."
)

ADMIN = {"email": f"admin@{EMAIL_DOMAIN}", "full_name": "SGH Dubai Admin"}
RECEPTIONIST = {"email": f"reception@{EMAIL_DOMAIN}", "full_name": "Aisha Khan", "gender": "female"}

MON_TO_FRI = [1, 2, 3, 4, 5]
MON_TO_SAT = [1, 2, 3, 4, 5, 6]

# One doctor per specialty. Names, titles and degrees are from the hospital's public listings;
# the schedules are illustrative and sit inside the 8 AM - 8 PM outpatient hours.
DOCTORS = [
    {
        "email": f"shereef.elbardisy@{EMAIL_DOMAIN}", "full_name": "Shereef Elbardisy", "gender": "male",
        "specialty": "Cardiology", "degree": "Consultant Interventional Cardiologist",
        "consultation_minutes": 20, "max_patients_per_day": 18,
        "available_from": time(9, 0), "available_to": time(17, 0), "lunch_from": time(13, 0), "lunch_to": time(14, 0),
        "working_days": MON_TO_FRI,
    },
    {
        "email": f"ashok.kumar@{EMAIL_DOMAIN}", "full_name": "Ashok Kumar", "gender": "male",
        "specialty": "Orthopedics", "degree": "MS Ortho (AIIMS), MRCS (Glasgow)",
        "consultation_minutes": 20, "max_patients_per_day": 20,
        "available_from": time(10, 0), "available_to": time(18, 0), "lunch_from": time(13, 30), "lunch_to": time(14, 30),
        "working_days": MON_TO_SAT,
    },
    {
        "email": f"hamza.rahhal@{EMAIL_DOMAIN}", "full_name": "Hamza Rahhal", "gender": "male",
        "specialty": "Pediatrics", "degree": "Consultant Pediatrician",
        "consultation_minutes": 15, "max_patients_per_day": 28,
        "available_from": time(8, 0), "available_to": time(16, 0), "lunch_from": time(12, 0), "lunch_to": time(13, 0),
        "working_days": MON_TO_SAT,
    },
    {
        "email": f"ghassan.lotfi@{EMAIL_DOMAIN}", "full_name": "Ghassan Lotfi", "gender": "male",
        "specialty": "Obstetrics and Gynecology", "degree": "Obstetrician and Gynecologist",
        "consultation_minutes": 30, "max_patients_per_day": 14,
        "available_from": time(11, 0), "available_to": time(19, 0), "lunch_from": time(14, 0), "lunch_to": time(15, 0),
        "working_days": MON_TO_FRI,
    },
    {
        "email": f"hussein.nofal@{EMAIL_DOMAIN}", "full_name": "Hussein Nofal", "gender": "male",
        "specialty": "Internal Medicine", "degree": "Specialist Internal Medicine",
        "consultation_minutes": 15, "max_patients_per_day": 30,
        "available_from": time(12, 0), "available_to": time(20, 0), "lunch_from": time(15, 0), "lunch_to": time(16, 0),
        "working_days": MON_TO_SAT,
    },
]

DOCTOR_FIELDS = (
    "full_name", "specialty", "degree", "consultation_minutes", "max_patients_per_day",
    "available_from", "available_to", "lunch_from", "lunch_to", "working_days",
)


class Command(BaseCommand):
    help = "Seed Saudi German Hospital Dubai as one centre with an admin, 5 doctors and a receptionist"

    def _user(self, clinic, *, email, full_name, role, **extra):
        """Create the account, or bring an existing one's details and password back to the seed's."""
        user = User.objects.filter(email=email).first()
        if user is None:
            self.created += 1
            return User.objects.create_user(
                email=email, password=PASSWORD, full_name=full_name, role=role, clinic=clinic,
                membership_status=User.MembershipStatus.ACTIVE, is_verified=True, email_verified_at=timezone.now(), **extra,
            )
        if user.clinic_id != clinic.id:
            raise SystemExit(f"{email} already belongs to another clinic; not touching it.")
        for field, value in {"full_name": full_name, "role": role, **extra}.items():
            setattr(user, field, value)
        user.set_password(PASSWORD)
        user.save()
        return user

    @transaction.atomic
    def handle(self, *args, **options):
        self.created = 0

        clinic, _ = Clinic.objects.update_or_create(name=CLINIC_NAME, defaults=CLINIC)
        ClinicConfiguration.objects.update_or_create(clinic=clinic, defaults=CONFIGURATION)
        # Five doctors need more seats than the trial plan's three.
        growth = SubscriptionPlan.get_default_plan(SubscriptionPlan.PlanType.GROWTH)
        ClinicEntitlement.objects.update_or_create(clinic=clinic, defaults={"plan": growth})
        VoiceAgentSettings.objects.update_or_create(
            clinic=clinic, defaults={"first_message": FIRST_MESSAGE, "system_prompt": SYSTEM_PROMPT},
        )

        self._user(clinic, role=User.Role.CLINIC_ADMIN, is_clinic_admin=True, **ADMIN)
        self._user(clinic, role=User.Role.RECEPTIONIST, **RECEPTIONIST)
        for entry in DOCTORS:
            user = self._user(
                clinic, email=entry["email"], full_name=entry["full_name"], gender=entry["gender"],
                role=User.Role.DOCTOR, is_doctor=True,
            )
            Doctor.objects.update_or_create(
                user=user, defaults={"clinic": clinic, "is_active": True, **{field: entry[field] for field in DOCTOR_FIELDS}},
            )

        self.stdout.write(self.style.SUCCESS(f"Seeded {clinic.name} (clinic id {clinic.id}); {self.created} new accounts."))
        self.stdout.write(f"Sign in with any of these emails and the password '{PASSWORD}':")
        self.stdout.write(f"  Admin:        {ADMIN['email']}")
        self.stdout.write(f"  Receptionist: {RECEPTIONIST['email']}")
        for entry in DOCTORS:
            self.stdout.write(f"  Doctor:       {entry['email']}  ({entry['specialty']})")
