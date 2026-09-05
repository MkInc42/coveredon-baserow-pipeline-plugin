from django.urls import re_path

from .views import PingView, TriageView, StatsView, UploadImageView, UploadImagesView

app_name = "coveredon_pipeline.api"

urlpatterns = [
    re_path(r"ping/$", PingView.as_view(), name="ping"),
    re_path(r"triage/$", TriageView.as_view(), name="triage"),
    re_path(r"stats/$", StatsView.as_view(), name="stats"),
    re_path(r"upload_image/$", UploadImageView.as_view(), name="upload_image"),
    re_path(r"upload_images/$", UploadImagesView.as_view(), name="upload_images"),
]