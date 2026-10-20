from django.db import models
from django.utils import timezone

from centers.models import Center


class Session(models.Model):
    """One scrape run (period + centers + reports). Also the folder name under runs/."""

    RUNNING, INCOMPLETE, COMPLETE, INTERRUPTED, LEGACY = (
        "running", "incomplete", "complete", "interrupted", "legacy")
    STATUS_CHOICES = [(s, s) for s in (RUNNING, INCOMPLETE, COMPLETE, INTERRUPTED, LEGACY)]

    session_id = models.CharField(max_length=40, primary_key=True)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=RUNNING, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    folder = models.CharField(max_length=500, blank=True)
    reports = models.JSONField(default=list, blank=True)
    center_ids = models.JSONField(default=list, blank=True)
    speed = models.CharField(max_length=16, default="normal")
    workers = models.PositiveSmallIntegerField(default=1)
    coverage = models.JSONField(null=True, blank=True)
    missing = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.session_id} · {self.start_date} → {self.end_date}"

    @property
    def missing_count(self):
        return sum(len(v) for v in (self.missing or {}).values())


class SectionResult(models.Model):
    """Outcome of one center x report inside a session (replaces the old scrape_log)."""

    OK, EMPTY, FAILED, SKIPPED, SLOW = "ok", "empty", "failed", "skipped", "slow"
    STATUS_CHOICES = [(s, s) for s in (OK, EMPTY, FAILED, SKIPPED, SLOW)]

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="results")
    center = models.ForeignKey(Center, on_delete=models.PROTECT, related_name="results")
    report = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, db_index=True)
    row_count = models.IntegerField(default=0)
    seconds = models.FloatField(null=True, blank=True, help_text="Time taken (for estimates)")
    error = models.TextField(blank=True)
    scraped_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["session", "center_id", "report"]
        constraints = [models.UniqueConstraint(fields=["session", "center", "report"],
                                               name="uniq_section_result")]
        indexes = [models.Index(fields=["session", "report", "status"])]

    def __str__(self):
        return f"{self.session_id} · {self.center_id} · {self.report} · {self.status}"


class _Record(models.Model):
    """Common shape of the scraped data tables: which session and center a row belongs to."""

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="+")
    center = models.ForeignKey(Center, on_delete=models.PROTECT, related_name="+")
    scraped_at = models.DateTimeField(default=timezone.now)

    class Meta:
        abstract = True


class SalesRecord(_Record):
    """'Expired Pack User Details - Upto Last 90 Days': one row per member pack."""

    user_id = models.CharField(max_length=40, db_index=True)
    name = models.CharField(max_length=255, blank=True, null=True)
    phone = models.CharField(max_length=40, blank=True, null=True)
    pack_name = models.CharField(max_length=255, blank=True, null=True)
    member_end_date = models.CharField(max_length=40, blank=True, null=True)
    active_last_30d = models.CharField(max_length=8, blank=True, null=True)
    renewed = models.CharField(max_length=8, blank=True, null=True)
    revenue = models.FloatField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["session", "center"])]


class PerfMetric(_Record):
    """'Total Revenue and Split': one row per metric and month."""

    metric_date = models.CharField(max_length=40)
    metric = models.CharField(max_length=255)
    value = models.FloatField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["session", "center"]),
                   models.Index(fields=["session", "metric"])]


class GymMember(_Record):
    """'My Gym Member': members and their packs."""

    user_id = models.CharField(max_length=40, db_index=True)
    username = models.CharField(max_length=255, blank=True, null=True)
    pack_name = models.CharField(max_length=255, blank=True, null=True)
    final_amount_paid = models.FloatField(null=True, blank=True)
    purchase_date = models.CharField(max_length=40, blank=True, null=True)
    pack_start_date = models.CharField(max_length=40, blank=True, null=True)
    pack_end_date = models.CharField(max_length=40, blank=True, null=True)
    status = models.CharField(max_length=32, blank=True, null=True)
    offline_vs_online = models.CharField(max_length=16, blank=True, null=True)
    new_vs_repeat = models.CharField(max_length=32, blank=True, null=True)
    class_level = models.CharField(max_length=64, blank=True, null=True)
    last_class_date = models.CharField(max_length=40, blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=["session", "center"])]


class CardRow(_Record):
    """Rows of the other dashboard cards: columns differ per card, so each row is JSON."""

    report = models.CharField(max_length=64)
    row_num = models.IntegerField()
    data = models.JSONField()

    class Meta:
        indexes = [models.Index(fields=["session", "report", "center"])]


class CardColumns(models.Model):
    """Column names of a generic card (taken from Metabase)."""

    report = models.CharField(max_length=64, primary_key=True)
    columns = models.JSONField(default=list)

    class Meta:
        verbose_name_plural = "card columns"

    def __str__(self):
        return self.report


class Job(models.Model):
    """A queued/running scrape. The job runner updates it; the admin pages read it."""

    QUEUED, RUNNING, DONE, ERROR, INTERRUPTED = "queued", "running", "done", "error", "interrupted"
    STATE_CHOICES = [(s, s) for s in (QUEUED, RUNNING, DONE, ERROR, INTERRUPTED)]

    id = models.CharField(max_length=40, primary_key=True)
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default=QUEUED, db_index=True)
    session = models.ForeignKey(Session, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="jobs")
    spec = models.JSONField(default=dict, blank=True)
    pid = models.IntegerField(null=True, blank=True)
    heartbeat = models.DateTimeField(null=True, blank=True)
    done = models.IntegerField(default=0)
    total = models.IntegerField(default=0)
    desc = models.CharField(max_length=300, blank=True)
    log = models.JSONField(default=list, blank=True)
    cells = models.JSONField(default=dict, blank=True, help_text="center|report -> failed")
    active = models.JSONField(default=list, blank=True, help_text="center|report in flight")
    workers_now = models.PositiveSmallIntegerField(default=1)
    stop_requested = models.BooleanField(default=False)
    pause_requested = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.id} · {self.state}"
