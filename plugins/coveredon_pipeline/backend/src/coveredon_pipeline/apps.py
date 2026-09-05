from baserow.core.registries import plugin_registry
from django.apps import AppConfig


class CoveredonPipelineConfig(AppConfig):
    name = "coveredon_pipeline"

    def ready(self):
        from .plugins import CoveredonPipelinePlugin

        plugin_registry.register(CoveredonPipelinePlugin())