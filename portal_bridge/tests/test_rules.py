"""Uji aturan bisnis inti: tugas klien, notulen, sign-off, komentar."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from portal_bridge import api
from portal_bridge.rules import client_tasks as ct
from portal_bridge.rules import comments as cm
from portal_bridge.rules import signoff as so
from portal_bridge.tests.factory import make_task, make_world


class TestRules(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.w = make_world("pbrule")
		frappe.db.set_single_value("Portal Bridge Settings", "whatsapp_enabled", 0)
		frappe.db.set_single_value("Portal Bridge Settings", "email_enabled", 0)

	def tearDown(self):
		frappe.set_user("Administrator")

	# aturan 5–7
	def test_client_task_late_and_blocks(self):
		late = make_task(self.w.proj_a, "A tugas klien terlambat", self.w.phase_a, client=True, assignee=self.w.a1,
		                 due=add_days(nowdate(), -2))
		dep = make_task(self.w.proj_a, "A menunggu data", self.w.phase_a)
		d = frappe.get_doc("Task", dep)
		d.append("depends_on", {"task": late})
		d.save(ignore_permissions=True)
		frappe.set_user(self.w.a1)
		rows = api.my_tasks(self.w.proj_a)["tasks"]
		mine = next(r for r in rows if r["name"] == late)
		self.assertEqual(mine["status"], "late")
		self.assertEqual(mine["late_days"], 2)
		self.assertTrue(mine["mine"])
		self.assertIn("A menunggu data", [b["subject"] for b in mine["blocks"]])
		self.assertNotIn("Fase A1", mine["blocks_text"])

	# aturan 6
	def test_client_completes_own_task_only(self):
		frappe.set_user(self.w.a2)
		with self.assertRaises(frappe.PermissionError):
			api.complete_task(self.w.task_a_client)
		frappe.set_user(self.w.a1)
		with self.assertRaises(frappe.PermissionError):
			api.complete_task(self.w.task_a_vis)  # bukan tugas klien
		res = api.complete_task(self.w.task_a_client, note="sudah dikirim")
		self.assertEqual(res["status"], "done")
		t = frappe.db.get_value("Task", self.w.task_a_client, ["status", "completed_by", "progress"], as_dict=True)
		self.assertEqual((t.status, t.completed_by, int(t.progress)), ("Completed", self.w.a1, 100))
		self.assertTrue(frappe.db.exists("Portal Audit Log", {"action": "task_complete", "ref_name": self.w.task_a_client}))

	# aturan 12–14
	def test_meeting_note_sent_creates_tasks_and_confirm(self):
		frappe.set_user("Administrator")
		note = frappe.get_doc(
			{
				"doctype": "Portal Meeting Note", "project": self.w.proj_a, "title": "Rapat uji", "meeting_date": nowdate(),
				"topics": [{"title": "Topik 1", "body": "isi", "resolution": "<p>sudah dijawab</p>"}],
				"actions": [{"body": "Kirim data X", "assignee": self.w.a2, "due": add_days(nowdate(), 7)},
				            {"body": "Siapkan server", "assignee": self.w.pic, "due": add_days(nowdate(), 3)}],
			}
		).insert(ignore_permissions=True)
		self.assertEqual(note.seq, 1)
		self.assertEqual(note.topics[0].resolution, "<p>sudah dijawab</p>")
		note.status = "Sent"
		note.save(ignore_permissions=True)
		note.reload()
		self.assertIsNotNone(note.confirm_due)
		self.assertGreater(note.confirm_due, frappe.utils.getdate(nowdate()))
		t1 = frappe.get_doc("Task", note.actions[0].task)
		self.assertEqual((t1.type, t1.portal_visible, t1.parent_task), ("Klien", 1, self.w.phase_a))
		self.assertIn(self.w.a2, t1._assign)
		t2 = frappe.get_doc("Task", note.actions[1].task)
		self.assertIsNone(t2.type or None)
		# klien tanpa can_approve ditolak, a1 boleh; nama harus cocok; attest wajib
		frappe.set_user(self.w.a2)
		with self.assertRaises(frappe.PermissionError):
			api.confirm_meeting_note(note.name, "Adi Uji", 1)
		frappe.set_user(self.w.a1)
		with self.assertRaises(frappe.ValidationError):
			api.confirm_meeting_note(note.name, "Ani Uji", 0)
		with self.assertRaises(frappe.ValidationError):
			api.confirm_meeting_note(note.name, "Orang Lain", 1)
		res = api.confirm_meeting_note(note.name, "ani uji", 1)
		self.assertEqual(res["status"], "Confirmed")
		self.assertEqual(len(res["pdf_hash"]), 64)
		from portal_bridge.evidence import verify

		self.assertTrue(verify(res["pdf_file"], res["pdf_hash"]))
		# deemed approved untuk yang lewat batas
		frappe.set_user("Administrator")
		note2 = frappe.get_doc({"doctype": "Portal Meeting Note", "project": self.w.proj_a, "title": "Rapat 2", "meeting_date": nowdate(),
		                        "status": "Sent"}).insert(ignore_permissions=True)
		frappe.db.set_value("Portal Meeting Note", note2.name, "confirm_due", add_days(nowdate(), -1))
		from portal_bridge.rules.meeting_notes import rule_14_close_overdue

		self.assertEqual(rule_14_close_overdue(), 1)
		self.assertEqual(frappe.db.get_value("Portal Meeting Note", note2.name, "status"), "Deemed approved")

	# aturan 20–24
	def test_signoff_decide_and_cr_effects(self):
		frappe.set_user("Administrator")
		gate = make_task(self.w.proj_a, "UAT sign-off", self.w.phase_a, is_milestone=1, portal_signoff_type="UAT")
		uat = frappe.get_doc({"doctype": "Portal Signoff", "project": self.w.proj_a, "kind": "UAT", "title": "UAT selesai"}).insert(ignore_permissions=True)
		self.assertEqual(uat.status, "Pending")
		self.assertIsNotNone(uat.due)
		self.assertTrue(frappe.db.exists("Portal Notification", {"template": "signoff_pending", "user": self.w.a1}))
		frappe.set_user(self.w.a2)
		with self.assertRaises(frappe.PermissionError):
			api.decide_signoff(uat.name, "approve", "Adi Uji", 1)
		frappe.set_user(self.w.a1)
		with self.assertRaises(frappe.ValidationError):
			api.decide_signoff(uat.name, "approve", "Ani Uji", 0)
		with self.assertRaises(frappe.ValidationError):
			api.decide_signoff(uat.name, "approve", "Salah Nama", 1)
		with self.assertRaises(frappe.ValidationError):
			api.decide_signoff(uat.name, "reject", None, None, "")
		res = api.decide_signoff(uat.name, "approve", "ANI UJI", "true")
		self.assertEqual(res["status"], "Approved")
		doc = frappe.get_doc("Portal Signoff", uat.name)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.pdf_hash, res["pdf_hash"])
		self.assertEqual(frappe.db.get_value("Task", gate, "status"), "Completed")
		with self.assertRaises(frappe.ValidationError):
			api.decide_signoff(uat.name, "reject", None, None, "lagi")
		# CR
		frappe.set_user("Administrator")
		end_before = frappe.db.get_value("Project", self.w.proj_a, "expected_end_date")
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Portal Signoff", "project": self.w.proj_a, "kind": "Change Request", "title": "CR tanpa dampak"}).insert(ignore_permissions=True)
		cr = frappe.get_doc({"doctype": "Portal Signoff", "project": self.w.proj_a, "kind": "Change Request", "title": "CR 1",
		                     "impact_man_days": 2, "impact_timeline_days": 3,
		                     "new_tasks": [{"subject": "Task dari CR", "exp_end_date": add_days(nowdate(), 10)}]}).insert(ignore_permissions=True)
		frappe.set_user(self.w.a1)
		api.decide_signoff(cr.name, "approve", "Ani Uji", 1)
		cr.reload()
		self.assertTrue(cr.new_tasks[0].task)
		self.assertEqual(frappe.db.get_value("Task", cr.new_tasks[0].task, "portal_visible"), 1)
		self.assertEqual(frappe.db.get_value("Project", self.w.proj_a, "expected_end_date"), add_days(end_before, 3))

	# aturan 16–19
	def test_comments_thread_rules(self):
		frappe.set_user(self.w.a1)
		root = api.add_comment(self.w.proj_a, "Task", self.w.task_a_vis, "Pertanyaan @[Pic](%s)" % self.w.pic, section="all")
		self.assertEqual(root["mentions"], [self.w.pic])
		self.assertTrue(frappe.db.exists("Portal Notification", {"template": "mention", "user": self.w.pic}))
		reply = api.add_comment(self.w.proj_a, "Task", self.w.task_a_vis, "Balasan", parent=root["name"])
		with self.assertRaises(frappe.ValidationError):
			api.add_comment(self.w.proj_a, "Task", self.w.task_a_vis, "Balasan 2 tingkat", parent=reply["name"])
		with self.assertRaises(frappe.PermissionError):
			api.add_comment(self.w.proj_a, "Task", self.w.task_b_vis, "lintas proyek")
		frappe.set_user(self.w.a2)
		with self.assertRaises(frappe.PermissionError):
			api.resolve_comment(root["name"])
		frappe.set_user(self.w.a1)
		self.assertEqual(api.resolve_comment(root["name"])["status"], "Resolved")
		# hapus setelah 10 menit ditolak untuk klien
		frappe.db.set_value("Portal Comment", reply["name"], "creation", add_days(nowdate(), -1))
		with self.assertRaises(frappe.PermissionError):
			api.delete_comment(reply["name"])
		threads = api.comments("Task", self.w.task_a_vis)["threads"]
		self.assertEqual(len(threads[0]["replies"]), 1)
