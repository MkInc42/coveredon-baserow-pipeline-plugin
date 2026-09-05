"""Covered On pipeline plugin for Baserow 2.3.3.

Registers API endpoints for pipeline triage and stats at /api/coveredon_pipeline/.
Reads Baserow tables (Leads=885, Orgs=884) via the REST API with JWT auth.
"""
from baserow.core.registries import Plugin
from django.urls import path, include

from .api import urls as api_urls


class CoveredonPipelinePlugin(Plugin):
    type = "coveredon_pipeline"

    def get_api_urls(self):
        return [
            path(
                "coveredon_pipeline/",
                include(api_urls, namespace=self.type),
            ),
        ]