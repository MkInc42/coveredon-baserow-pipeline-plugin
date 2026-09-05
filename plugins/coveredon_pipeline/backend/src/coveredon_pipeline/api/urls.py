from django.urls import re_path

from .views import PingView, TriageView, StatsView

app_name = "coveredon_pipeline.api"

urlpatterns = [
    re_path(r"ping/$", PingView.as_view(), name="ping"),
    re_path(r"triage/$", TriageView.as_view(), name="triage"),
    re_path(r"stats/$", StatsView.as_view(), name="stats"),
]