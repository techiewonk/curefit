import datetime as dt

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from centers.models import Center
from scraper import packstate
from scraper.models import Customer, GymPack

SAME = {"Sec-Fetch-Site": "same-origin"}


class PackStateTests(TestCase):
    def test_one_order_decides(self):
        today = dt.date(2026, 10, 22)
        d = lambda n: today + dt.timedelta(days=n)  # noqa: E731
        cases = [((d(-60), d(-1), "Active"), "ended"), ((d(-60), d(-1), "Paused"), "ended"),
                 ((d(3), d(90), "Active"), "upcoming"), ((d(3), d(90), "Paused"), "upcoming"),
                 ((d(-5), d(90), "Paused"), "paused"), ((d(-5), d(10), "Active"), "ending"),
                 ((d(-5), d(0), None), "ending"), ((d(-5), d(90), "Active"), "active"),
                 ((None, None, "Active"), "unknown")]
        for args, want in cases:
            self.assertEqual(packstate.state(*args, today=today), want, args)

    def test_the_filter_matches_the_label(self):
        center = Center.objects.create(center_id=1, name="Gym 1")
        today = timezone.localdate()
        d = lambda n: today + dt.timedelta(days=n)  # noqa: E731
        rows = [(d(-60), d(-1), "Active"), (d(3), d(90), "Active"), (d(-5), d(90), "Paused"),
                (d(-5), d(10), "Active"), (d(-5), d(200), "Active"), (None, None, "Active")]
        for i, (s, e, st) in enumerate(rows):
            GymPack.objects.create(key=str(i), center=center, user_id=f"u{i}", pack_start_date=s, pack_end_date=e,
                                   status=st)
        for name, _ in packstate.CHOICES:
            via_filter = set(GymPack.objects.filter(packstate.q(name)).values_list("key", flat=True))
            via_label = {p.key for p in GymPack.objects.all()
                         if packstate.state(p.pack_start_date, p.pack_end_date, p.status) == name}
            self.assertEqual(via_filter, via_label, name)


class PackColoursInTheListsTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("admin", "a@b.c", "x"))
        center = Center.objects.create(center_id=1, name="Gym 1")
        today = timezone.localdate()
        GymPack.objects.create(key="later", center=center, user_id="u1", username="Asha", status="Active",
                               pack_start_date=today + dt.timedelta(days=5), pack_end_date=today + dt.timedelta(days=95))
        GymPack.objects.create(key="now", center=center, user_id="u2", username="Ravi", status="Paused",
                               pack_start_date=today - dt.timedelta(days=5), pack_end_date=today + dt.timedelta(days=95))
        Customer.objects.create(user_id="u1", name="Asha", member_status="Active", lead_status="expired",
                                pack_end_date=today - dt.timedelta(days=3))
        Customer.objects.create(user_id="u2", name="Ravi", member_status="Paused", lead_status="active")

    def test_gym_packs_show_where_each_pack_stands_and_filter_future_members(self):
        url = reverse("admin:scraper_gympack_changelist")
        r = self.client.get(url, headers=SAME)
        self.assertContains(r, "Starts later")
        self.assertContains(r, "Paused")
        r = self.client.get(url + "?now=upcoming", headers=SAME)
        self.assertContains(r, "Asha")
        self.assertNotContains(r, "Ravi")

    def test_customers_show_badges_and_the_next_pack(self):
        url = reverse("admin:scraper_customer_changelist")
        r = self.client.get(url, headers=SAME)
        self.assertContains(r, "Expired recently")
        self.assertContains(r, "text-red-700")  # the end date already past
        self.assertContains(r, (timezone.localdate() + dt.timedelta(days=5)).strftime("%d %b %Y"))
        r = self.client.get(url + "?starts_later=yes", headers=SAME)
        self.assertContains(r, "Asha")
        self.assertNotContains(r, "Ravi")
