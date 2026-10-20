from django.apps import AppConfig


class ConsoleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "console"

    def ready(self):
        import curefit_login
        curefit_login.QUIET = True  # the login helpers must never print tokens or cookies
