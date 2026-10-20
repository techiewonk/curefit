from django.apps import AppConfig


class ScraperConfig(AppConfig):
    name = "scraper"

    def ready(self):
        """Admin lists and detail pages print floats trimmed (354012.54, not 354012.54000000004)."""
        from django.contrib.admin import helpers
        from django.db import models
        from unfold import fields as unfold_fields
        from unfold import sections as unfold_sections
        from unfold import utils as unfold_utils
        from unfold.templatetags import unfold_list

        from . import numbers
        original = unfold_utils.display_for_field

        def display_for_field(value, field, empty_value_display):
            if isinstance(field, models.FloatField) and isinstance(value, float):
                return numbers.trim(value)
            return original(value, field, empty_value_display)

        for module in (unfold_utils, unfold_fields, unfold_sections, unfold_list):
            module.display_for_field = display_for_field
