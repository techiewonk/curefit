"""Forms for the console pages: all input validation lives here, not in the views."""
import datetime as dt

from django import forms
from unfold.widgets import (UnfoldAdminCheckboxSelectMultipleWidget, UnfoldAdminRadioSelectWidget,
                            UnfoldAdminSelectWidget, UnfoldAdminSingleDateWidget,
                            UnfoldAdminTextInputWidget, UnfoldBooleanSwitchWidget)

from centers.models import CenterList
from scraper.conf import SPEED_PROFILES
from scraper.models import Session
from scraper.reports import GROUPS, REPORTS

SPEED_CHOICES = [("gentle", "Gentle — 1 center at a time"), ("normal", "Normal — 2 at a time"),
                 ("fast", "Fast — 4 at a time"),
                 ("adaptive", "Adaptive — starts at 2, slows down or speeds up by itself")]


def report_choices():
    """Reports grouped for the checkbox list: Core / Perf cards / Sales cards."""
    return [(group, [(name, f"{REPORTS[name]['label']} ({name})") for name in names])
            for group, names in GROUPS.items()]


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
    reports = forms.MultipleChoiceField(choices=report_choices, initial=list(REPORTS),
                                        widget=UnfoldAdminCheckboxSelectMultipleWidget)
    speed = forms.ChoiceField(choices=SPEED_CHOICES, initial="normal", widget=UnfoldAdminSelectWidget)
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
                "speed": d["speed"], "session": d["session"].pk if d["session"] else None,
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

    session = forms.ModelChoiceField(Session.objects.all(), widget=UnfoldAdminSelectWidget)
