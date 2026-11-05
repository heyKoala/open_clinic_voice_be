from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from django.utils import timezone

from ai_agent.models import CallLog
from patients.models import MessageLog, Patient
from subscriptions.billing import billing_period, clinic_billing_summary, compute_bill


class TestComputeBill:
    def test_300_minutes_has_no_extra_voice_charge(self):
        bill = compute_bill(300 * 60, 0)
        assert bill["voice"]["extra_minutes"] == 0
        assert bill["voice"]["extra_charge"] == 0
        assert bill["voice"]["bundled_remaining"] == 200
        assert bill["total"] == 25_000

    def test_exactly_500_minutes_has_no_extra_voice_charge(self):
        bill = compute_bill(500 * 60, 0)
        assert bill["voice"]["extra_charge"] == 0
        assert bill["voice"]["bundled_remaining"] == 0
        assert bill["total"] == 25_000

    def test_650_minutes_charges_150_extra_minutes(self):
        bill = compute_bill(650 * 60, 0)
        assert bill["voice"]["bundled_used"] == 500
        assert bill["voice"]["extra_minutes"] == 150
        assert bill["voice"]["extra_charge"] == 1_500
        assert bill["total"] == 26_500

    def test_100_whatsapp_messages_cost_200(self):
        bill = compute_bill(0, 100)
        assert bill["whatsapp"]["charge"] == 200
        assert bill["total"] == 25_200

    def test_combined_example_is_26700(self):
        assert compute_bill(650 * 60, 100)["total"] == 26_700

    def test_partial_minutes_round_up_over_the_month(self):
        # 500 min + 1 second is the 501st minute: one billable extra minute.
        assert compute_bill(500 * 60 + 1, 0)["voice"]["extra_minutes"] == 1
        assert compute_bill(0, 0)["voice"]["total_minutes"] == 0


class TestBillingPeriod:
    def test_period_is_the_calendar_month_in_local_time(self):
        start, end = billing_period(datetime(2026, 11, 15, 12, 0, tzinfo=dt_timezone.utc))
        assert (start.year, start.month, start.day, start.hour) == (2026, 11, 1, 0)
        assert (end.year, end.month, end.day) == (2026, 12, 1)

    def test_december_rolls_into_january(self):
        _, end = billing_period(datetime(2026, 12, 20, tzinfo=dt_timezone.utc))
        assert (end.year, end.month, end.day) == (2027, 1, 1)


@pytest.mark.django_db
class TestClinicBillingSummary:
    def _call(self, clinic, minutes, when=None, **extra):
        return CallLog.objects.create(
            clinic=clinic, direction="inbound", occurred_at=when or timezone.now(),
            outcome="booked", duration_seconds=minutes * 60, **extra,
        )

    def _whatsapp(self, clinic, patient, status=MessageLog.Status.SENT):
        return MessageLog.objects.create(
            clinic=clinic, patient=patient, method=MessageLog.Method.WHATSAPP, status=status, message_text="hi",
        )

    def test_counts_only_this_months_real_usage(self, clinic, clinic_b):
        now = timezone.now()
        patient = Patient.objects.create(clinic=clinic, full_name="P", phone="+911234567890")
        self._call(clinic, 400, now)
        self._call(clinic, 250, now)
        self._call(clinic, 999, now - timedelta(days=45))            # an earlier month: the bundle has reset
        self._call(clinic, 100, now, is_test=True)                   # staff test call
        self._call(clinic_b, 100, now)                               # another clinic
        for _ in range(3):
            self._whatsapp(clinic, patient)
        self._whatsapp(clinic, patient, MessageLog.Status.FAILED)    # never went out
        self._whatsapp(clinic, patient, MessageLog.Status.PENDING)   # not sent yet
        MessageLog.objects.create(clinic=clinic, patient=patient, method=MessageLog.Method.SMS,
                                  status=MessageLog.Status.SENT, message_text="sms")

        summary = clinic_billing_summary(clinic, now)
        assert summary["voice"]["total_minutes"] == 650
        assert summary["voice"]["extra_charge"] == 1_500
        assert summary["whatsapp"]["messages"] == 3
        assert summary["total"] == 25_000 + 1_500 + 6

    def test_a_new_month_starts_with_the_full_bundle(self, clinic):
        # 31 Oct 20:00 UTC is 1 Nov 01:30 in India, so it belongs to November.
        self._call(clinic, 650, datetime(2026, 10, 31, 20, 0, tzinfo=dt_timezone.utc))
        october = clinic_billing_summary(clinic, datetime(2026, 10, 15, tzinfo=dt_timezone.utc))
        november = clinic_billing_summary(clinic, datetime(2026, 11, 15, tzinfo=dt_timezone.utc))
        assert october["voice"]["total_minutes"] == 0
        assert november["voice"]["total_minutes"] == 650

    def test_summary_endpoint_includes_billing(self, admin_client, clinic):
        self._call(clinic, 650)
        res = admin_client.get("/api/v1/subscriptions/summary/")
        assert res.status_code == 200
        assert res.data["billing"]["voice"]["extra_minutes"] == 150
        assert res.data["billing"]["total"] == 26_500
