"""Which Metabase card each report reads. Add an entry here to scrape another card."""

KRONOS = "https://kronos.curefit.co"
PERF_REFERER = f"{KRONOS}/reports/perf-dashboard"
SALES_REFERER = f"{KRONOS}/reports/sales_perfromance"


def _perf_card(dashcard, card, label):
    return {
        "label": label, "dashboard": 1750, "dashcard": dashcard, "card": card,
        "referer": PERF_REFERER,
        "iframe_query": lambda cid, s, e: {"start": s, "end": e},
        # the perf dashboard splits its numbers by day, week or month
        "data_params": lambda s, e, granularity="month": {"start": s, "end": e, "granular": granularity},
    }


def _sales_card(dashcard, card, label):
    return {
        "label": label, "dashboard": 2536, "dashcard": dashcard, "card": card,
        "referer": SALES_REFERER,
        # the Kronos iframeUrl query and the Metabase data-call parameters differ per dashboard
        "iframe_query": lambda cid, s, e: {"start_date": s, "end_date": e, "center_id": str(cid)},
        "data_params": lambda s, e, granularity=None: {"start_date": s, "end_date": e},  # not split
    }


# typed reports (own table) first, then the generic cards (JSON rows)
REPORTS = {
    "sales": _sales_card(19822, 39684, "Expired Pack User Details (last 90 days)"),
    "perf": _perf_card(13395, 24755, "Total Revenue and Split"),
    "gym": _perf_card(13454, 24811, "My Gym Member"),
    "perf_pack_purchase": _perf_card(13385, 24754, "Pack Purchase and Split"),
    "perf_packs_split": _perf_card(13423, 24773, "Packs Split"),
    "perf_membership": _perf_card(13424, 24774, "Membership Count"),
    "perf_expiries": _perf_card(14436, 25931, "Expiries and Retention"),
    "perf_trial_funnel": _perf_card(13426, 24776, "Trial Funnel"),
    "perf_walkin_funnel": _perf_card(13432, 24783, "Walkin Funnel"),
    "perf_workout_feedback": _perf_card(13429, 24779, "Workout Feedback"),
    "perf_ratings": _perf_card(13431, 24781, "Ratings"),
    "perf_trainer_rating": _perf_card(18537, 34056, "Trainer level rating"),
    "sales_center_trials": _sales_card(19818, 39680, "Center Overview (Trials)"),
    "sales_cm_trials": _sales_card(19819, 39681, "CM Overview (Trials)"),
    "sales_lead_view": _sales_card(19817, 39679, "Lead View (Assigned Manager) - 30 days"),
    "sales_lead_quality": _sales_card(21562, 43869, "Lead Quality & Conversion"),
    "sales_center_walkins": _sales_card(19820, 39682, "Center Overview (Walk-Ins)"),
    "sales_cm_walkins": _sales_card(19821, 39683, "CM Overview (Walk-Ins)"),
}

TYPED = ("sales", "perf", "gym")
GENERIC = [r for r in REPORTS if r not in TYPED]

# groups for the Run page
GROUPS = {
    "Core": list(TYPED),
    "Perf cards": [r for r in GENERIC if r.startswith("perf_")],
    "Sales cards": [r for r in GENERIC if r.startswith("sales_")],
}

def report_choices():
    """Reports grouped for a checkbox list: Core / Perf cards / Sales cards."""
    return [(group, [(name, f"{REPORTS[name]['label']} ({name})") for name in names])
            for group, names in GROUPS.items()]


# column names for the per-center CSV files of the typed reports (Metabase supplies the others)
TYPED_COLUMNS = {
    "sales": ["user_id", "name", "phone", "pack_name", "member_end_date", "active_last_30d",
              "renewed", "revenue"],
    "perf": ["metric_date", "metric", "value"],
    "gym": ["userid", "username", "packname", "final_amountpaid", "Purchasedate", "Packstartdate",
            "Packenddate", "status", "offline_vs_online", "new_vs_repeat", "Userclasslevel",
            "last_class_date"],
}
