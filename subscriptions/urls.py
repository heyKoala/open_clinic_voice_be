from django.urls import path

from subscriptions.views import CreditBalanceView, SubscriptionSummaryView, SubscriptionUpgradeView, VerifyRazorpayPaymentView

urlpatterns = [
    path("summary/", SubscriptionSummaryView.as_view(), name="subscription-summary"),
    path("credits/", CreditBalanceView.as_view(), name="subscription-credits"),
    path("upgrade/", SubscriptionUpgradeView.as_view(), name="subscription-upgrade"),
    path("verify/", VerifyRazorpayPaymentView.as_view(), name="subscription-verify"),
]