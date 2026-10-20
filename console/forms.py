"""Forms for the console pages: all input validation lives here, not in the views."""
import datetime as dt

from django import forms
from unfold.widgets import (UnfoldAdminCheckboxSelectMultipleWidget, UnfoldAdminRadioSelectWidget,
                            UnfoldAdminSelect2MultipleWidget, UnfoldAdminSelectWidget, UnfoldAdminSingleDateWidget, UnfoldAdminSingleTimeWidget,
                            UnfoldAdminTextInputWidget, UnfoldBooleanSwitchWidget)

from centers.models import Center, CenterGroup, CenterList
from scraper.conf import SPEED_CHOICES, SPEED_PROFILES
from scraper.models import AutoRun, Session
from scraper.reports import REPORTS, TYPED, report_choices


def default_chunk():
    from constance import config as settings
    return settings.SALES_CHUNK if settings.SALES_CHUNK in dict(Session.CHUNK_CHOICES) else Session.WHOLE


def last_month():
    first_of_month = dt.date.today().replace(day=1)
    last = first_of_month - dt.timedelta(days=1)
    return last.replace(day=1), last


class RunForm(forms.Form):
    """What to scrape: a period, a center list, some reports, how fast — or a session to continue."""

    start = forms.DateField(widget=UnfoldAdminSingleDateWidget)
    end = forms.DateField(widget=UnfoldAdminSingleDateWidget,
                          help_text="The sales report only holds the last 90 days before today.")
    centers = forms.ModelChoiceField(CenterList.objects.all(), to_field_name="name", empty_label=None,
                                     label="Centers", widget=UnfoldAdminRadioSelectWidget)
    pick = forms.ModelMultipleChoiceField(
        Center.objects.order_by("center_id"), required=False, label="Or choose centers",
        help_text="Optional, handy for testing: type to search and pick one or more. When any are chosen "
                  "they are used instead of the list above.",
        widget=UnfoldAdminSelect2MultipleWidget)
    groups = forms.ModelMultipleChoiceField(
        CenterGroup.objects.all(), required=False, label="Or choose groups",
        help_text="Optional: every gym of the chosen groups (together with any gyms chosen above).",
        widget=UnfoldAdminSelect2MultipleWidget)
    reports = forms.MultipleChoiceField(choices=report_choices, initial=list(TYPED),  # the extra cards start unticked
                                        help_text="The three core reports are ticked. The extra perf and sales cards are off until you tick them.",
                                        widget=UnfoldAdminCheckboxSelectMultipleWidget)
    speed = forms.ChoiceField(choices=SPEED_CHOICES, initial="normal", widget=UnfoldAdminSelectWidget)
    granularity = forms.ChoiceField(
        choices=Session.GRANULARITY_CHOICES, initial=Session.MONTH, widget=UnfoldAdminSelectWidget, required=False,
        label="Split by", help_text="The perf cards give one row per day, week or month. Sales cards are not split.")
    sales_chunk = forms.ChoiceField(
        choices=Session.CHUNK_CHOICES, widget=UnfoldAdminSelectWidget, required=False, label="Ask for sales by",
        help_text="Expired Pack User Details has no split of its own: ask for it a day or a week at a time "
                  "so missing days can be filled in later. Default is in Settings.")
    fill_missing = forms.BooleanField(required=False, label="Sales: only fetch days not stored yet",
                                      widget=UnfoldBooleanSwitchWidget)
    session = forms.ModelChoiceField(Session.objects.unfinished(), required=False,
                                     empty_label="Start a new session", widget=UnfoldAdminRadioSelectWidget)
    retry_empty = forms.BooleanField(required=False, label="Also retry empty results",
                                     widget=UnfoldBooleanSwitchWidget)
    recheck_known = forms.BooleanField(required=False, label="Also re-check known-empty centers",
                                       widget=UnfoldBooleanSwitchWidget)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        start, end = last_month()
        self.fields["start"].initial, self.fields["end"].initial = start, end
        self.fields["centers"].initial = "actual"
        self.fields["sales_chunk"].initial = default_chunk()

    def chosen_ids(self):
        """The gyms picked by hand plus the gyms of the chosen groups; empty = use the center list."""
        d = self.cleaned_data
        ids = {c.pk for c in d.get("pick") or []}
        for group in d.get("groups") or []:
            ids.update(group.centers.values_list("pk", flat=True))
        return sorted(ids)

    @property
    def center_count(self):
        """How many centers this run covers: the chosen ones, else the whole list."""
        return len(self.chosen_ids()) or self.cleaned_data["centers"].centers.count()

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["start"] > data["end"]:
            self.add_error("end", "The end date is before the start date.")
        return data

    @property
    def workers(self):
        return SPEED_PROFILES[self.cleaned_data["speed"]]

    def to_spec(self):
        """The arguments `scraper.jobs.enqueue` expects."""
        d = self.cleaned_data
        every_report = set(d["reports"]) == set(REPORTS)
        return {"start": d["start"].isoformat(), "end": d["end"].isoformat(), "list": d["centers"].name,
                "only": None if every_report else d["reports"], "workers": self.workers,
                "center_ids": self.chosen_ids() or None,
                "speed": d["speed"], "granularity": d["granularity"] or Session.MONTH,
                "sales_chunk": d["sales_chunk"] or default_chunk(), "fill_missing": d["fill_missing"],
                "session": d["session"].pk if d["session"] else None,
                "retry_empty": d["retry_empty"], "recheck_known": d["recheck_known"]}


class OtpForm(forms.Form):
    otp = forms.RegexField(r"^\d{4,8}$", label="OTP", widget=UnfoldAdminTextInputWidget,
                           error_messages={"invalid": "Enter the numeric code from the email."})


class CompareForm(forms.Form):
    """Two sessions to compare (b is measured against a)."""

    a = forms.ModelChoiceField(Session.objects.all(), label="Baseline", widget=UnfoldAdminSelectWidget)
    b = forms.ModelChoiceField(Session.objects.all(), label="Compare with", widget=UnfoldAdminSelectWidget)
    only_changed = forms.BooleanField(required=False, initial=True, label="Only centers that changed",
                                      widget=UnfoldBooleanSwitchWidget)


class SessionPickForm(forms.Form):
    """Pick one session (Monitor and Analytics)."""

    session = forms.ModelChoiceField(
        Session.objects.all(), widget=UnfoldAdminSelectWidget(attrs={"onchange": "this.form.submit()"}))


class CampaignForm(forms.ModelForm):
    """'Save as campaign' on the Run page: a name and a schedule; everything else is taken from the Run form."""

    use_required_attribute = False  # these fields only matter when Save as campaign is pressed, not when Start is

    class Meta:
        model = AutoRun
        fields = ["name", "frequency", "run_at", "weekday", "month_day", "cron", "period_rule", "days", "enabled",
                  "continue_unfinished"]
        widgets = {"name": UnfoldAdminTextInputWidget, "frequency": UnfoldAdminSelectWidget,
                   "weekday": UnfoldAdminSelectWidget, "period_rule": UnfoldAdminSelectWidget,
                   "run_at": UnfoldAdminSingleTimeWidget, "cron": UnfoldAdminTextInputWidget,
                   "month_day": UnfoldAdminTextInputWidget, "days": UnfoldAdminTextInputWidget,
                   "enabled": UnfoldBooleanSwitchWidget, "continue_unfinished": UnfoldBooleanSwitchWidget}
        labels = {"period_rule": "Period each run reports on", "enabled": "Switched on", "days": "Last N days"}

    def save_from(self, run_form, commit=True):
        """The campaign: this form's name and schedule, with the settings of the (valid) Run form."""
        d = run_form.cleaned_data
        auto = self.save(commit=False)
        auto.centers = d["centers"]
        auto.reports = d["reports"]
        auto.speed = d["speed"]
        auto.granularity = d["granularity"] or Session.MONTH
        auto.sales_chunk = d["sales_chunk"] or Session.WHOLE
        auto.fill_missing = d["fill_missing"]
        auto.retry_empty = d["retry_empty"]
        auto.recheck_known = d["recheck_known"]
        if auto.period_rule == AutoRun.FIXED:  # the dates typed on the Run page
            auto.start_date, auto.end_date = d["start"], d["end"]
        if commit:
            auto.save()
            auto.gyms.set(d["pick"])
            auto.groups.set(d["groups"])
        return auto
