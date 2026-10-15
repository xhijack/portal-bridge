"""Uji aturan F1–F6: feedback klien lewat Issue (buat batch + file titipan, isolasi, ubah, tutup/buka kembali, notifikasi)."""

import frappe
from frappe.tests.utils import FrappeTestCase

from portal_bridge import api
from portal_bridge.rules import feedback as fb
from portal_bridge.tests.factory import make_world
from portal_bridge.utils import clear_project_cache

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class TestFeedback(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.w = make_world("pbfb")
		frappe.db.set_single_value("Portal Bridge Settings", "whatsapp_enabled", 0)
		frappe.db.set_single_value("Portal Bridge Settings", "email_enabled", 0)
		for u in (cls.w.a1, cls.w.a2, cls.w.b1):
			clear_project_cache(u)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _create(self, user, project, items):
		frappe.set_user(user)
		return api.create_feedback(project, items)["created"]

	# F1 + F2 + F6
	def test_batch_create_with_staged_file_and_single_pic_notification(self):
		frappe.set_user(self.w.a1)
		staged = fb.stage_file("layar.png", PNG)
		self.assertFalse(staged.attached_to_doctype)
		names = self._create(self.w.a1, self.w.proj_a, [
			{"subject": "Tombol simpan tidak merespons", "kind": "bug", "description": "<p>Di layar PO</p>", "task": self.w.task_a_vis, "files": [staged.name]},
			{"subject": "Tambah kolom catatan", "kind": "change"},
			{"subject": "Bagaimana cara ekspor?", "kind": "question", "date": "2026-10-01"},
		])
		self.assertEqual(len(names), 3)
		i1 = frappe.get_doc("Issue", names[0])
		self.assertEqual((i1.project, i1.customer, i1.raised_by, i1.status, i1.portal_visible, i1.issue_type, i1.portal_task),
		                 (self.w.proj_a, self.w.cust_a, self.w.a1, "Open", 1, "Bug", self.w.task_a_vis))
		f = frappe.db.get_value("File", staged.name, ["attached_to_doctype", "attached_to_name"], as_dict=True)
		self.assertEqual((f.attached_to_doctype, f.attached_to_name), ("Issue", names[0]))
		self.assertEqual(str(frappe.db.get_value("Issue", names[2], "opening_date")), "2026-10-01")
		# satu notifikasi ringkas ke PIC, bukan tiga
		notifs = frappe.get_all("Portal Notification", filters={"user": self.w.pic, "template": "feedback_new", "project": self.w.proj_a})
		self.assertEqual(len(notifs), 1)
		self.assertTrue(frappe.db.exists("Portal Audit Log", {"action": "feedback_create", "ref_name": names[0]}))
		# daftar untuk klien: 3 baris, lampiran ikut, counts
		data = api.feedback(self.w.proj_a)
		self.assertEqual(data["counts"]["all"], 3)
		row = next(r for r in data["items"] if r["name"] == names[0])
		self.assertEqual(row["kind"], "bug")
		self.assertEqual(row["task"]["name"], self.w.task_a_vis)
		self.assertEqual(len(row["attachments"]), 1)
		self.assertTrue(row["attachments"][0]["is_image"])
		self.assertTrue(row["mine"] and row["can_edit"])

	def test_validation_subject_task_kind(self):
		frappe.set_user(self.w.a1)
		with self.assertRaises(frappe.ValidationError):
			api.create_feedback(self.w.proj_a, [{"subject": "  "}])
		with self.assertRaises(frappe.ValidationError):
			api.create_feedback(self.w.proj_a, [{"subject": "x", "task": self.w.task_b_vis}])  # task proyek lain
		with self.assertRaises(frappe.ValidationError):
			api.create_feedback(self.w.proj_a, [{"subject": "x", "task": self.w.task_a_hidden}])  # task tersembunyi
		with self.assertRaises(frappe.ValidationError):
			api.create_feedback(self.w.proj_a, [{"subject": "x", "kind": "lainnya"}])

	# kebijakan unggah mengikuti System Settings (Allowed File Extensions; kosong = semua tipe)
	def test_upload_policy_follows_system_settings(self):
		frappe.set_user("Administrator")
		prev = frappe.db.get_single_value("System Settings", "allowed_file_extensions")
		try:
			frappe.db.set_single_value("System Settings", "allowed_file_extensions", "png\nPDF\n")
			frappe.clear_document_cache("System Settings", "System Settings")
			self.assertEqual(fb.upload_policy()["extensions"], ["pdf", "png"])
			frappe.set_user(self.w.a1)
			fb.stage_file("ok.png", PNG)
			with self.assertRaises(frappe.ValidationError):
				fb.stage_file("tolak.exe", b"x")
			frappe.set_user("Administrator")
			frappe.db.set_single_value("System Settings", "allowed_file_extensions", "")
			frappe.clear_document_cache("System Settings", "System Settings")
			self.assertIsNone(fb.upload_policy()["extensions"])
			frappe.set_user(self.w.a1)
			fb.stage_file("bebas.xyz", b"x")  # kosong = semua tipe, seperti Desk
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_single_value("System Settings", "allowed_file_extensions", prev)
			frappe.clear_document_cache("System Settings", "System Settings")

	# F2: file titipan hanya milik pengunggah
	def test_staged_file_belongs_to_uploader(self):
		frappe.set_user(self.w.a1)
		staged = fb.stage_file("bukti.png", PNG)
		frappe.set_user(self.w.a2)
		with self.assertRaises(frappe.PermissionError):
			api.create_feedback(self.w.proj_a, [{"subject": "pakai file orang", "files": [staged.name]}])
		# a1 sendiri bisa, dan file tak bisa dipakai dua kali
		frappe.set_user(self.w.a1)
		n = api.create_feedback(self.w.proj_a, [{"subject": "milik sendiri", "files": [staged.name]}])["created"][0]
		self.assertEqual(frappe.db.get_value("File", staged.name, "attached_to_name"), n)
		with self.assertRaises(frappe.PermissionError):
			api.create_feedback(self.w.proj_a, [{"subject": "dua kali", "files": [staged.name]}])

	# isolasi antar tenant + permission hook
	def test_isolation_between_clients(self):
		names = self._create(self.w.a1, self.w.proj_a, [{"subject": "rahasia A"}])
		frappe.set_user(self.w.b1)
		with self.assertRaises(frappe.PermissionError):
			api.feedback(self.w.proj_a)
		with self.assertRaises(frappe.PermissionError):
			api.feedback_item(names[0])
		self.assertNotIn(names[0], frappe.get_list("Issue", pluck="name"))
		self.assertFalse(frappe.has_permission("Issue", "read", names[0], user=self.w.b1))
		# a2 (satu project) boleh lihat tapi tidak boleh ubah milik a1
		frappe.set_user(self.w.a2)
		self.assertIn(names[0], frappe.get_list("Issue", pluck="name"))
		self.assertEqual(api.feedback_item(names[0])["subject"], "rahasia A")
		with self.assertRaises(frappe.PermissionError):
			api.update_feedback(names[0], subject="diubah a2")
		# Issue internal tanpa portal_visible tidak bocor
		frappe.set_user("Administrator")
		internal = frappe.get_doc({"doctype": "Issue", "subject": "internal saja", "project": self.w.proj_a, "raised_by": self.w.pic}).insert(ignore_permissions=True)
		frappe.set_user(self.w.a1)
		self.assertNotIn(internal.name, frappe.get_list("Issue", pluck="name"))
		self.assertNotIn(internal.name, [r["name"] for r in api.feedback(self.w.proj_a)["items"]])

	# F3
	def test_client_edits_only_while_open(self):
		name = self._create(self.w.a1, self.w.proj_a, [{"subject": "typo"}])[0]
		frappe.set_user(self.w.a1)
		api.update_feedback(name, subject="Typo di judul laporan", kind="bug", task=self.w.task_a_client)
		d = frappe.db.get_value("Issue", name, ["subject", "issue_type", "portal_task"], as_dict=True)
		self.assertEqual((d.subject, d.issue_type, d.portal_task), ("Typo di judul laporan", "Bug", self.w.task_a_client))
		frappe.set_user("Administrator")
		doc = frappe.get_doc("Issue", name)
		doc.status = "Replied"
		doc.resolution_details = "<p>Sudah diperbaiki di build 12</p>"
		doc.save()
		frappe.set_user(self.w.a1)
		with self.assertRaises(frappe.ValidationError):
			api.update_feedback(name, subject="coba ubah lagi")
		self.assertFalse(api.feedback_item(name)["can_edit"])

	# F4 + F5
	def test_resolve_notifies_client_then_close_or_reopen(self):
		name = self._create(self.w.a1, self.w.proj_a, [{"subject": "angka salah"}])[0]
		frappe.set_user(self.w.a1)
		with self.assertRaises(frappe.ValidationError):
			api.decide_feedback(name, "close")  # belum Resolved
		frappe.set_user("Administrator")
		doc = frappe.get_doc("Issue", name)
		doc.status = "Resolved"
		doc.resolution_details = "<p>Rumus diperbaiki</p>"
		doc.save()
		self.assertTrue(frappe.db.exists("Portal Notification", {"user": self.w.a1, "template": "feedback_resolved"}))
		frappe.set_user(self.w.a1)
		item = api.feedback_item(name)
		self.assertTrue(item["can_close"] and item["can_reopen"])
		with self.assertRaises(frappe.ValidationError):
			api.decide_feedback(name, "reopen")  # alasan wajib
		res = api.decide_feedback(name, "reopen", note="Masih salah di halaman 2")
		self.assertEqual(res["status"], "Open")
		self.assertTrue(frappe.db.exists("Portal Comment", {"ref_doctype": "Issue", "ref_name": name, "author_user": self.w.a1}))
		self.assertTrue(frappe.db.exists("Portal Notification", {"user": self.w.pic, "template": "feedback_reopened"}))
		# PIC selesaikan lagi → klien konfirmasi
		frappe.set_user("Administrator")
		doc = frappe.get_doc("Issue", name)
		doc.status = "Resolved"
		doc.save()
		frappe.set_user(self.w.a1)
		self.assertEqual(api.decide_feedback(name, "close")["status"], "Closed")
		self.assertTrue(frappe.db.exists("Portal Notification", {"user": self.w.pic, "template": "feedback_closed"}))
		self.assertEqual(api.feedback(self.w.proj_a, status="done")["items"][0]["status"], "Closed")

	# komentar di Issue + badge + status ikut percakapan + penanda aktivitas
	def test_comments_and_summary_count(self):
		name = self._create(self.w.a1, self.w.proj_a, [{"subject": "pertanyaan"}])[0]
		frappe.set_user(self.w.a1)
		row = next(r for r in api.feedback(self.w.proj_a)["items"] if r["name"] == name)
		self.assertEqual((row["awaiting"], row["last_comment"], row["has_update"]), ("team", None, False))  # buatan sendiri, belum ada komentar
		frappe.set_user(self.w.pic)
		c = api.add_comment(self.w.proj_a, "Issue", name, "Bisa dijelaskan lebih detail?")
		self.assertEqual(c["section"], "all")
		self.assertTrue(frappe.db.exists("Portal Notification", {"user": self.w.a1, "template": "comment_new"}))
		self.assertEqual(frappe.db.get_value("Issue", name, "status"), "Replied")  # tim menanggapi → Replied
		frappe.set_user(self.w.a1)
		row = next(r for r in api.feedback(self.w.proj_a)["items"] if r["name"] == name)
		self.assertEqual((row["awaiting"], row["has_update"], row["last_comment"]["author"]["side"]), ("client", True, "internal"))
		self.assertEqual(api.feedback_item(name)["open_comments"], 1)  # membuka detail = sudah dilihat
		row = next(r for r in api.feedback(self.w.proj_a)["items"] if r["name"] == name)
		self.assertFalse(row["has_update"])
		api.add_comment(self.w.proj_a, "Issue", name, "Sudah, di layar PO.")
		self.assertEqual(frappe.db.get_value("Issue", name, "status"), "Open")  # klien membalas → kembali Open
		row = next(r for r in api.feedback(self.w.proj_a)["items"] if r["name"] == name)
		self.assertEqual(row["awaiting"], "team")
		data = api.feedback(self.w.proj_a)
		self.assertIn(name, [i["name"] for i in data["items"] if i["awaiting"] == "team"])
		self.assertGreaterEqual(data["counts"]["unanswered"], 1)
		self.assertEqual(api.summary(self.w.proj_a)["counts"]["feedback"], 0)
		frappe.set_user("Administrator")
		doc = frappe.get_doc("Issue", name)
		doc.status = "Resolved"
		doc.save()
		frappe.cache.delete_value(f"portal_summary:{self.w.proj_a}")
		frappe.set_user(self.w.a1)
		self.assertEqual(api.summary(self.w.proj_a)["counts"]["feedback"], 1)

	# lampiran pada komentar: file titipan ditautkan ke Portal Comment, ikut di threads, klien lain proyek tak bisa baca
	def test_comment_attachments(self):
		name = self._create(self.w.a1, self.w.proj_a, [{"subject": "ada lampiran di komentar"}])[0]
		frappe.set_user(self.w.a1)
		staged = fb.stage_file("bukti-komentar.png", PNG)
		c = api.add_comment(self.w.proj_a, "Issue", name, "", files=[staged.name])  # boleh tanpa teks bila ada file
		self.assertEqual(len(c["attachments"]), 1)
		f = frappe.db.get_value("File", staged.name, ["attached_to_doctype", "attached_to_name"], as_dict=True)
		self.assertEqual((f.attached_to_doctype, f.attached_to_name), ("Portal Comment", c["name"]))
		th = api.feedback_item(name)["comments"]
		self.assertEqual(th[0]["attachments"][0]["name"], staged.name)
		self.assertTrue(frappe.has_permission("File", "read", staged.name, user=self.w.a2))
		self.assertFalse(frappe.has_permission("File", "read", staged.name, user=self.w.b1))
		frappe.set_user(self.w.a2)
		other = fb.stage_file("x.png", PNG)
		frappe.set_user(self.w.a1)
		with self.assertRaises(frappe.PermissionError):
			api.add_comment(self.w.proj_a, "Issue", name, "pakai file orang", files=[other.name])
		with self.assertRaises(frappe.ValidationError):
			api.add_comment(self.w.proj_a, "Issue", name, "  ")
