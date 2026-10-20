import datetime as dt

from django.test import TransactionTestCase
from django.urls import reverse

from scraper import customers, engine
from scraper.models import Customer, CustomerPhone, GymMember, SalesRecord
from scraper.testing import FakeKronosMixin

D = dt.date


class CustomerTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.old = engine.new_session("2026-06-01", "2026-06-30", [1], ["sales"])
        self.new = engine.new_session("2026-07-01", "2026-07-31", [1], ["sales"])

    def sale(self, session, uid, phone, end, pack="Gold", revenue=100.0, renewed="No", name="Asha"):
        return SalesRecord.objects.create(session=session, center_id=1, user_id=uid, phone=phone, name=name,
                                          pack_name=pack, member_end_date=end, renewed=renewed, revenue=revenue,
                                          active_last_30d="Yes")

    def test_phone_normalising(self):
        n = customers.normalise_phone
        self.assertEqual(n("+91 98765-43210"), "9876543210")
        self.assertEqual(n("09876543210"), "9876543210")
        self.assertIsNone(n("12345"))
        self.assertIsNone(n(None))
        self.assertIsNone(customers.clean_user_id("None"))

    def test_one_customer_per_user_id_with_several_phones(self):
        a = self.sale(self.old, "U1", "9876543210", D(2026, 6, 20))
        b = self.sale(self.new, "U1", "+91 99999 11111", D(2026, 7, 20))
        customers.link_session(self.old)
        customers.link_session(self.new)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.customer_id, b.customer_id)
        c = Customer.objects.get(user_id="U1")
        self.assertEqual(set(c.phones.values_list("phone", flat=True)), {"9876543210", "9999911111"})

    def test_a_phone_with_two_user_ids_is_flagged_not_merged(self):
        self.sale(self.new, "U1", "9876543210", D(2026, 7, 20))
        self.sale(self.new, "U2", "9876543210", D(2026, 7, 21))
        customers.link_session(self.new)
        self.assertEqual(Customer.objects.count(), 2)
        self.assertTrue(CustomerPhone.objects.get(phone="9876543210").shared)

    def test_newest_session_wins_whatever_order_they_are_linked_in(self):
        self.sale(self.new, "U1", "9876543210", D(2026, 8, 30), pack="Annual", renewed="Yes")
        self.sale(self.old, "U1", "9876543210", D(2026, 6, 20), pack="Monthly")
        customers.link_session(self.new)
        customers.link_session(self.old)  # older data arriving later must not overwrite
        c = Customer.objects.get(user_id="U1")
        self.assertEqual((c.pack_name, c.pack_end_date, c.renewed, c.last_session_id),
                         ("Annual", D(2026, 8, 30), "Yes", self.new.pk))

    def test_gym_rows_join_the_same_customer(self):
        self.sale(self.new, "U1", "9876543210", D(2026, 7, 20))
        GymMember.objects.create(session=self.new, center_id=1, user_id="U1", username="Asha", pack_name="Gold",
                                 final_amount_paid=500.0, pack_end_date=D(2026, 9, 1), status="ACTIVE",
                                 new_vs_repeat="Repeat", last_class_date=D(2026, 7, 30))
        customers.link_session(self.new)
        c = Customer.objects.get()
        self.assertEqual((c.member_status, c.pack_end_date, c.last_class_date), ("ACTIVE", D(2026, 9, 1), D(2026, 7, 30)))
        self.assertEqual((c.sales.count(), c.gym.count()), (1, 1))

    def test_lead_status(self):
        today = D(2026, 10, 1)
        for uid, end in (("a", D(2026, 12, 1)), ("b", D(2026, 10, 15)), ("c", D(2026, 8, 1)),
                         ("d", D(2025, 1, 1)), ("e", None)):
            Customer.objects.create(user_id=uid, pack_end_date=end)
        customers.refresh_leads(today)
        got = dict(Customer.objects.values_list("user_id", "lead_status"))
        self.assertEqual(got, {"a": "active", "b": "expiring", "c": "expired", "d": "lapsed", "e": "unknown"})

    def test_link_pending_and_unlinked_rows(self):
        self.sale(self.new, "U1", "9876543210", D(2026, 7, 20))
        self.sale(self.new, "", "", D(2026, 7, 20))  # nothing to identify this one by
        customers.link_pending()
        self.assertEqual(SalesRecord.objects.filter(customer__isnull=True).count(), 1)
        self.assertEqual(Customer.objects.count(), 1)

    def test_a_finished_run_links_by_itself_and_admin_pages_load(self):
        session = engine.run_session(engine.new_session("2026-08-01", "2026-08-31", [1, 2, 3, 4], ["sales", "gym"]))
        self.assertFalse(SalesRecord.objects.filter(session=session, customer__isnull=True).exists())
        self.assertGreater(Customer.objects.count(), 0)
        for suffix in ("", "?lead_status=active", "?pack_end_date_from=2026-01-01&pack_end_date_to=2030-01-01",
                       "?phones__phone=98", "?q=Cult"):
            self.assertEqual(self.client.get(reverse("admin:scraper_customer_changelist") + suffix).status_code, 200, suffix)
        c = Customer.objects.first()
        self.assertEqual(self.client.get(reverse("admin:scraper_customer_change", args=[c.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:scraper_salesrecord_changelist") + f"?customer={c.pk}").status_code, 200)

    def test_a_short_session_with_an_expired_pack_does_not_move_the_pack_back(self):
        GymMember.objects.create(session=self.old, center_id=1, user_id="U1", username="Asha", pack_name="Annual",
                                 pack_end_date=D(2026, 12, 1), last_class_date=D(2026, 6, 28), status="ACTIVE")
        customers.link_session(self.old)
        self.sale(self.new, "U1", "9876543210", D(2026, 7, 30), pack="Monthly")  # an older pack expired later
        customers.link_session(self.new)
        c = Customer.objects.get(user_id="U1")
        self.assertEqual((c.pack_name, c.pack_end_date), ("Annual", D(2026, 12, 1)))
        self.assertEqual(c.as_of, D(2026, 7, 31))
