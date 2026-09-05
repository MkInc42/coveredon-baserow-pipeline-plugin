from django.urls import re_path

from .views import PingView, TriageView, StatsView, FunnelView, TimelineView, ChannelsView, UploadImageView, UploadImagesView

app_name = "coveredon_pipeline.api"

urlpatterns = [
    re_path(r"ping/$", PingView.as_view(), name="ping"),
    re_path(r"triage/$", TriageView.as_view(), name="triage"),
    re_path(r"stats/$", StatsView.as_view(), name="stats"),
    # Chart data endpoints for the Lead Console charts
    re_path(r"chart/funnel/$", FunnelView.as_view(), name="chart-funnel"),
    re_path(r"chart/timeline/$", TimelineView.as_view(), name="chart-timeline"),
    re_path(r"chart/channels/$", ChannelsView.as_view(), name="chart-channels"),
    # Image upload endpoints
    re_path(r"upload_image/$", UploadImageView.as_view(), name="upload_image"),
    re_path(r"upload_images/$", UploadImagesView.as_view(), name="upload_images"),
]