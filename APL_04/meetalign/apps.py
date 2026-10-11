from django.apps import AppConfig


class MeetalignConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'meetalign'

    def ready(self):
        from . import signals  # noqa: F401  시그널 등록
