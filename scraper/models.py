import datetime

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from centers.models import Center, CenterList


class SessionQuerySet(models.QuerySet):
    def unfinished(self):
        return self.exclude(status=Session.COMPLETE)

    def for_period(self, start, end, granularity=None):
        qs = self.filter(start_date=start, end_date=end)
        return qs.filter(granularity=granularity) if granularity else qs


class Session(models.Model):
    """One scrape run (period + centers + reports). Also the folder name under runs/."""

    RUNNING, INCOMPLETE, COMPLETE, INTERRUPTED = "running", "incomplete", "complete", "interrupted"
    STATUS_CHOICES = [(s, s) for s in (RUNNING, INCOMPLETE, COMPLETE, INTERRUPTED)]
    DAY, WEEK, MONTH = "day", "week", "month"
    GRANULARITY_CHOICES = [(DAY, "Day"), (WEEK, "Week"), (MONTH, "Month")]

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
    granularity = models.CharField(
        max_length=5, choices=GRANULARITY_CHOICES, default=MONTH,
        help_text="How the perf cards split the period: one row per day, week or month.")
    coverage = models.JSONField(null=True, blank=True)
    missing = models.JSONField(default=dict, blank=True)
    checks = models.JSONField(default=list, blank=True, help_text="Automatic findings after the run")

    objects = SessionQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.session_id} · {self.start_date} → {self.end_date} · by {self.granularity}"

    @property
    def is_unfinished(self):
        return self.status != self.COMPLETE

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
    member_end_date = models.DateField(blank=True, null=True)
    active_last_30d = models.CharField(max_length=8, blank=True, null=True)
    renewed = models.CharField(max_length=8, blank=True, null=True)
    revenue = models.FloatField(null=True, blank=True)
    customer = models.ForeignKey("scraper.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="sales")

    class Meta:
        indexes = [models.Index(fields=["session", "center"])]


class PerfMetric(_Record):
    """'Total Revenue and Split': one row per metric and month."""

    metric_date = models.DateField(blank=True, null=True)
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
    purchase_date = models.DateField(blank=True, null=True)
    pack_start_date = models.DateField(blank=True, null=True)
    pack_end_date = models.DateField(blank=True, null=True)
    status = models.CharField(max_length=32, blank=True, null=True)
    offline_vs_online = models.CharField(max_length=16, blank=True, null=True)
    new_vs_repeat = models.CharField(max_length=32, blank=True, null=True)
    class_level = models.CharField(max_length=64, blank=True, null=True)
    last_class_date = models.DateField(blank=True, null=True)
    customer = models.ForeignKey("scraper.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="gym")

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


class JobQuerySet(models.QuerySet):
    def active(self):
        return self.filter(state__in=(Job.QUEUED, Job.RUNNING))


class Job(models.Model):
    """A queued/running scrape. The job runner updates it; the admin pages read it."""

    QUEUED, RUNNING, DONE, ERROR, INTERRUPTED, CANCELLED = (
        "queued", "running", "done", "error", "interrupted", "cancelled")
    STATE_CHOICES = [(s, s) for s in (QUEUED, RUNNING, DONE, ERROR, INTERRUPTED, CANCELLED)]

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

    objects = JobQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.id} · {self.state}"

    @property
    def is_active(self):
        return self.state in (self.QUEUED, self.RUNNING)

    @property
    def percent(self):
        return round(100 * self.done / self.total) if self.total else 0

    @property
    def eta_seconds(self):
        """Time left, extrapolated from the pace so far (None until something finished)."""
        if not (self.is_active and self.started_at and self.done):
            return None
        elapsed = (timezone.now() - self.started_at).total_seconds()
        return elapsed / self.done * (self.total - self.done)


class AutoRun(models.Model):
    """A scrape that starts by itself every day, for the days up to yesterday (a daily report)."""

    DAILY, WEEKLY, MONTHLY, CRON = "daily", "weekly", "monthly", "cron"
    FREQUENCY_CHOICES = [(DAILY, "Daily: the days up to yesterday"), (WEEKLY, "Weekly: last week, Monday to Sunday"),
                         (MONTHLY, "Monthly: last calendar month"),
                         (CRON, "Cron expression: your own schedule (reports the days up to yesterday)")]
    WEEKDAYS = [(0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"), (4, "Friday"),
                (5, "Saturday"), (6, "Sunday")]

    name = models.CharField(max_length=80, unique=True)
    enabled = models.BooleanField(default=False)
    frequency = models.CharField(max_length=8, choices=FREQUENCY_CHOICES, default=DAILY)
    cron = models.CharField(
        max_length=60, blank=True,
        help_text="Cron runs only: minute hour day-of-month month weekday, in local time. "
                  "For example 30 6 * * * = every day at 06:30, 0 7 * * 1-5 = weekdays at 07:00.")
    run_at = models.TimeField(default=datetime.time(6, 30), help_text="Local time of day.")
    weekday = models.PositiveSmallIntegerField(default=0, choices=WEEKDAYS, help_text="Weekly runs: which day.")
    month_day = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(28)],
        help_text="Monthly runs: day of the month (1-28).")
    centers = models.ForeignKey(CenterList, on_delete=models.PROTECT, related_name="auto_runs")
    reports = models.JSONField(default=list)
    speed = models.CharField(max_length=16, default="adaptive")
    granularity = models.CharField(
        max_length=5, choices=Session.GRANULARITY_CHOICES, default=Session.DAY,
        help_text="How the perf cards split the period: one row per day, week or month.")
    days = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text="Daily runs: 1 = yesterday only; 7 = the last seven days up to yesterday.")
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_session = models.ForeignKey(Session, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    last_status = models.CharField(max_length=10, blank=True)  # started / skipped / failed
    last_message = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        from croniter import croniter
        from django.core.exceptions import ValidationError
        if self.frequency == self.CRON and not croniter.is_valid(self.cron or ""):
            raise ValidationError({"cron": "Enter a valid cron expression, for example 30 6 * * *."})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from . import autorun
        autorun.sync_schedule(self)  # keep django-q2's schedule in step with enabled / run_at

    def delete(self, *args, **kwargs):
        from . import autorun
        autorun.remove_schedule(self)
        return super().delete(*args, **kwargs)


class Customer(models.Model):
    """One person across sessions and centers. Identified by the Kronos user id; phone numbers
    (several allowed) are the fallback. Holds the newest status seen, so no history is kept here:
    the purchases themselves stay on the sales / gym rows of their sessions."""

    ACTIVE, EXPIRING, EXPIRED, LAPSED, UNKNOWN = "active", "expiring", "expired", "lapsed", "unknown"
    LEAD_CHOICES = [(ACTIVE, "Active"), (EXPIRING, "Expiring soon"), (EXPIRED, "Expired recently"),
                    (LAPSED, "Lapsed"), (UNKNOWN, "No pack date")]

    user_id = models.CharField(max_length=40, unique=True, null=True, blank=True,
                               help_text="Kronos user id. Empty for a person only known by phone number.")
    name = models.CharField(max_length=255, blank=True)
    center = models.ForeignKey(Center, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                               help_text="Center of the newest pack.")
    pack_name = models.CharField(max_length=255, blank=True)
    pack_end_date = models.DateField(null=True, blank=True, db_index=True)
    renewed = models.CharField(max_length=8, blank=True)
    active_last_30d = models.CharField(max_length=8, blank=True)
    member_status = models.CharField(max_length=32, blank=True)
    new_vs_repeat = models.CharField(max_length=32, blank=True)
    last_class_date = models.DateField(null=True, blank=True)
    last_amount = models.FloatField(null=True, blank=True, help_text="Amount of the newest purchase.")
    lead_status = models.CharField(max_length=10, choices=LEAD_CHOICES, default=UNKNOWN, db_index=True)
    as_of = models.DateField(null=True, blank=True, help_text="End date of the session this status comes from.")
    last_session = models.ForeignKey(Session, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    first_seen = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-pack_end_date", "id"]

    def __str__(self):
        return f"{self.name or 'Unnamed'} ({self.user_id or 'phone only'})"


class CustomerPhone(models.Model):
    """A normalised phone number (last 10 digits). Unique; one customer may have several."""

    phone = models.CharField(max_length=10, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="phones")
    shared = models.BooleanField(default=False, db_index=True,
                                 help_text="Seen with more than one user id: check before treating them as one person.")

    def __str__(self):
        return self.phone
