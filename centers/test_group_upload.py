from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from centers.models import Center, CenterGroup

SAME = {"Sec-Fetch-Site": "same-origin"}


def csv_file(text, name="gyms.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


class GroupCsvUploadTests(TestCase):
    def setUp(self):
        for pk in (1, 9, 10, 14):
            Center.objects.update_or_create(pk=pk, defaults={"name": f"Gym {pk}"})
        self.client.force_login(get_user_model().objects.create_superuser("admin", "a@b.c", "x"))

    def post(self, url, **data):
        data = {"name": "Bengaluru", "position": 0, "csv_mode": "add", **data}
        return self.client.post(url, data, headers=SAME)

    def ids(self, name="Bengaluru"):
        return set(CenterGroup.objects.get(name=name).centers.values_list("pk", flat=True))

    def test_create_a_group_from_a_csv_with_ids_and_names(self):
        r = self.post(reverse("admin:centers_centergroup_add"),
                      gyms_csv=csv_file("﻿ID,Name\n10,Gym 10\n9,Gym 9\n\n10,again\n"))
        self.assertEqual(r.status_code, 302, r.content[:2000])
        self.assertEqual(self.ids(), {9, 10})

    def test_add_keeps_the_gyms_and_replace_sets_exactly_the_files(self):
        group = CenterGroup.objects.create(name="Bengaluru")
        group.centers.add(1)
        url = reverse("admin:centers_centergroup_change", args=[group.pk])
        self.post(url, centers=[1], gyms_csv=csv_file("center_id;name\n14;Gym 14\n"))
        self.assertEqual(self.ids(), {1, 14})
        self.post(url, centers=[1, 14], csv_mode="replace", gyms_csv=csv_file("ID\tName\n9\tGym 9\n"))
        self.assertEqual(self.ids(), {9})

    def test_unknown_ids_stop_the_save_and_are_listed(self):
        r = self.post(reverse("admin:centers_centergroup_add"), gyms_csv=csv_file("ID,Name\n10,ok\n99999,nope\n"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "99999")
        self.assertFalse(CenterGroup.objects.filter(name="Bengaluru").exists())

    def test_a_file_without_an_id_column_is_refused(self):
        r = self.post(reverse("admin:centers_centergroup_add"), gyms_csv=csv_file("Name\nGym 10\n"))
        self.assertContains(r, "No ID column")

    def test_ids_without_a_header_and_text_lines_skipped(self):
        r = self.post(reverse("admin:centers_centergroup_add"), gyms_csv=csv_file("10,Gym 10\nTotal,\n14\n"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.ids(), {10, 14})

    def test_the_reserved_name_still_refused_and_the_form_offers_the_sample(self):
        r = self.post(reverse("admin:centers_centergroup_add"), name="Ungrouped", gyms_csv=csv_file("ID\n10\n"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(CenterGroup.objects.filter(name="Ungrouped").exists())
        page = self.client.get(reverse("admin:centers_centergroup_add"), headers=SAME)
        self.assertContains(page, "group_sample.csv")
        self.assertContains(page, 'enctype="multipart/form-data"')
        self.assertTrue(finders.find("centers/group_sample.csv"))
