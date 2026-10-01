from __future__ import annotations

import logging

from django.db import transaction as db_transaction
from django.db.models import Count
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import IsClinicAdmin
from accounts.services import get_or_create_clinic_entitlement, log_auth_event
from audit.models import AuthEvent
from common.audit import log_data_change
from subscriptions.models import ClinicEntitlement, SubscriptionPlan
import razorpay
from django.conf import settings
from clinics.models import Clinic, PaymentTransaction

logger = logging.getLogger(__name__)


class SubscriptionSummaryView(APIView):
	permission_classes = [IsClinicAdmin]

	def get(self, request):
		clinic = request.clinic
		entitlement = get_or_create_clinic_entitlement(clinic)
		active_counts = (
			User.objects.filter(clinic=clinic, is_active=True, membership_status=User.MembershipStatus.ACTIVE)
			.values("role")
			.annotate(total=Count("id"))
		)
		counts_by_role = {row["role"]: row["total"] for row in active_counts}
		return Response(
			{
				"clinic_name": clinic.name,
				"subscription_status": clinic.subscription_status,
				"trial_ends_at": clinic.trial_ends_at,
				"plan": entitlement.plan.name,
				"feature_flags": entitlement.get_feature_flags(),
				"limits": {
					role_value: entitlement.get_limit(role_value)
					for role_value, _ in User.Role.choices
				},
				"usage": {
					role_value: counts_by_role.get(role_value, 0)
					for role_value, _ in User.Role.choices
				},
				"plan_options": [
					{
						"value": plan_value,
						"label": SubscriptionPlan.PlanType(plan_value).label,
					}
					for plan_value, _ in SubscriptionPlan.PlanType.choices
				],
			}
		)


GROWTH_PLAN_PRICE_INR = 3999


def _razorpay_client():
	return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def _apply_plan(clinic, plan_value):
	entitlement = get_or_create_clinic_entitlement(clinic)
	entitlement.plan = SubscriptionPlan.get_default_plan(plan_value)
	entitlement.save(update_fields=["plan", "updated_at"])
	clinic.subscription_status = (
		Clinic.SubscriptionStatus.TRIAL if plan_value == SubscriptionPlan.PlanType.TRIAL else Clinic.SubscriptionStatus.ACTIVE
	)
	clinic.save(update_fields=["subscription_status", "updated_at"])
	return entitlement


class SubscriptionUpgradeView(APIView):
	permission_classes = [IsClinicAdmin]

	def post(self, request):
		plan_value = request.data.get("plan")
		if plan_value not in SubscriptionPlan.PlanType.values:
			return Response({"detail": "A valid plan is required."}, status=status.HTTP_400_BAD_REQUEST)

		clinic = request.clinic
		if plan_value == SubscriptionPlan.PlanType.TRIAL:
			# Free plan — switch directly
			entitlement = _apply_plan(clinic, plan_value)
			log_data_change(entitlement, "update", request=request, metadata={"plan": plan_value})
			log_auth_event(
				request,
				AuthEvent.EventType.DATA_EXPORTED,
				user=request.user,
				email=request.user.email,
				metadata={"event": "subscription_upgrade", "plan": plan_value},
			)
			return Response(
				{
					"detail": "Subscription upgraded.",
					"plan": entitlement.plan.name,
					"subscription_status": clinic.subscription_status,
				}
			)

		if plan_value == SubscriptionPlan.PlanType.ENTERPRISE:
			return Response(
				{"detail": "Enterprise plans are arranged with our sales team. Please contact support."},
				status=status.HTTP_400_BAD_REQUEST,
			)

		# Growth — create a Razorpay order; the plan is applied once payment is verified.
		try:
			payment = _razorpay_client().order.create(data={
				"amount": GROWTH_PLAN_PRICE_INR * 100,  # paise
				"currency": "INR",
				"receipt": f"receipt_{clinic.id}_{request.user.id}",
			})
		except Exception:
			logger.exception("Razorpay order creation failed for clinic %s", clinic.id)
			return Response(
				{"detail": "Could not start the payment. Please try again later."},
				status=status.HTTP_502_BAD_GATEWAY,
			)

		PaymentTransaction.objects.create(
			clinic=clinic,
			razorpay_order_id=payment["id"],
			amount=GROWTH_PLAN_PRICE_INR,
			plan=plan_value,
			status=PaymentTransaction.Status.CREATED,
		)
		return Response({
			"razorpay_order_id": payment["id"],
			"amount": payment["amount"],
			"currency": payment["currency"],
			"key_id": settings.RAZORPAY_KEY_ID,
			"requires_action": True,
		})


class VerifyRazorpayPaymentView(APIView):
	permission_classes = [IsClinicAdmin]

	def post(self, request):
		razorpay_payment_id = request.data.get("razorpay_payment_id")
		razorpay_order_id = request.data.get("razorpay_order_id")
		razorpay_signature = request.data.get("razorpay_signature")

		if not all([razorpay_payment_id, razorpay_order_id, razorpay_signature]):
			return Response({"detail": "Missing payment parameters"}, status=status.HTTP_400_BAD_REQUEST)

		with db_transaction.atomic():
			payment_txn = PaymentTransaction.objects.select_for_update().filter(
				clinic=request.clinic,
				razorpay_order_id=razorpay_order_id,
			).first()
			if not payment_txn:
				return Response({"detail": "Transaction not found"}, status=status.HTTP_404_NOT_FOUND)

			if payment_txn.status == PaymentTransaction.Status.CAPTURED:
				# Already processed (e.g. a retried request) — don't apply the plan twice.
				return Response({
					"detail": "Payment already verified.",
					"plan": get_or_create_clinic_entitlement(request.clinic).plan.name,
				})

			try:
				_razorpay_client().utility.verify_payment_signature({
					"razorpay_order_id": razorpay_order_id,
					"razorpay_payment_id": razorpay_payment_id,
					"razorpay_signature": razorpay_signature,
				})
			except razorpay.errors.SignatureVerificationError:
				payment_txn.status = PaymentTransaction.Status.FAILED
				payment_txn.save(update_fields=["status", "updated_at"])
				return Response({"detail": "Signature verification failed"}, status=status.HTTP_400_BAD_REQUEST)

			payment_txn.razorpay_payment_id = razorpay_payment_id
			payment_txn.razorpay_signature = razorpay_signature
			payment_txn.status = PaymentTransaction.Status.CAPTURED
			payment_txn.save(update_fields=["razorpay_payment_id", "razorpay_signature", "status", "updated_at"])

			entitlement = _apply_plan(request.clinic, payment_txn.plan)
			log_data_change(entitlement, "update", request=request, metadata={"plan": payment_txn.plan, "razorpay_order_id": razorpay_order_id})

		return Response({
			"detail": "Payment successful. Subscription upgraded.",
			"plan": entitlement.plan.name,
		})
