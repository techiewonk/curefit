from django.db import models


class Center(models.Model):
    """One Curefit center as Kronos reports it (refreshed by the center sync)."""

    center_id = models.PositiveIntegerField(primary_key=True, verbose_name="ID")
    name = models.CharField(max_length=255, blank=True)
    code = models.CharField(max_length=32, blank=True)
    state = models.CharField(max_length=16, blank=True, db_index=True)  # ACTIVE / INACTIVE
    center_type = models.CharField(max_length=32, blank=True)
    locality = models.CharField(max_length=128, blank=True)
    cluster = models.CharField(max_length=128, blank=True, db_index=True)
    city_id = models.IntegerField(null=True, blank=True)
    country_id = models.IntegerField(null=True, blank=True)
    capacity = models.IntegerField(null=True, blank=True)
    upcoming = models.BooleanField(default=False)
    sale_disabled = models.BooleanField(default=False)
    is_published = models.BooleanField(default=False)
    phone = models.CharField(max_length=32, blank=True)
    launch_date = models.DateField(null=True, blank=True)
    shutdown_date = models.DateField(null=True, blank=True)
    center_service_id = models.IntegerField(null=True, blank=True)
    seller_id = models.CharField(max_length=64, blank=True)
    monthly_target = models.FloatField(null=True, blank=True, help_text="Revenue target per month, in rupees.")
    note = models.CharField(max_length=200, blank=True, help_text="A free note shown beside the gym in the revenue report.")
    raw = models.JSONField(default=dict, blank=True, help_text="Full Kronos record")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["center_id"]

    def __str__(self):
        return f"{self.center_id} · {self.name}".strip(" ·")

    @property
    def is_active(self):
        return self.state == "ACTIVE"


class CenterGroup(models.Model):
    """A named set of centers for reports (GYMS, Gym Hybrid, ...). Unlike a center list it is not
    about what to scrape: it groups the results. A center can be in several groups."""

    name = models.CharField(max_length=60, unique=True)
    centers = models.ManyToManyField(Center, blank=True, related_name="groups")
    position = models.PositiveSmallIntegerField(default=0, help_text="Order on the report pages (small first).")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "name"]

    def __str__(self):
        return self.name


class CenterList(models.Model):
    """A named set of centers to scrape. 'actual' is the real run, 'test' a handful for trials."""

    name = models.SlugField(max_length=40, unique=True)
    description = models.CharField(max_length=200, blank=True)
    centers = models.ManyToManyField(Center, blank=True, related_name="lists")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CenterSyncLog(models.Model):
    """What changed each time the center list was refreshed from Kronos."""

    synced_at = models.DateTimeField(auto_now_add=True)
    total = models.IntegerField(default=0)
    active = models.IntegerField(default=0)
    added = models.JSONField(default=list, blank=True)
    removed = models.JSONField(default=list, blank=True)
    state_changed = models.JSONField(default=list, blank=True)
    renamed = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-synced_at"]

    def __str__(self):
        return f"{self.synced_at:%Y-%m-%d %H:%M} · {self.total} centers"


class KnownEmpty(models.Model):
    """A center that is consistently empty for a report (so it is not counted as a failure)."""

    center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name="known_empty")
    report = models.CharField(max_length=64)
    note = models.CharField(max_length=255, blank=True)
    since = models.DateTimeField(auto_now_add=True)
    last_checked = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["report", "center_id"]
        constraints = [models.UniqueConstraint(fields=["center", "report"], name="uniq_known_empty")]
        verbose_name_plural = "known empty"

    def __str__(self):
        return f"{self.center_id} · {self.report}"
