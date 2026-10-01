from django.urls import path

from subscriptions.views import SubscriptionSummaryView, SubscriptionUpgradeView, VerifyRazorpayPaymentView

urlpatterns = [
    path("summary/", SubscriptionSummaryView.as_view(), name="subscription-summary"),
    path("upgrade/", SubscriptionUpgradeView.as_view(), name="subscription-upgrade"),
    path("verify/", VerifyRazorpayPaymentView.as_view(), name="subscription-verify"),
]