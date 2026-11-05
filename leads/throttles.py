from rest_framework.throttling import AnonRateThrottle


class LeadThrottle(AnonRateThrottle):
    scope = "leads"
