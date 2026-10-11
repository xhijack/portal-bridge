"""Uji isolasi: klien A tidak melihat project/task klien B, dan task tanpa portal_visible tidak pernah muncul."""

import frappe
from frappe.tests.utils import FrappeTestCase

from portal_bridge.tests.factory import make_world
from portal_bridge.utils import clear_project_cache


class TestPermissions(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.w = make_world("pbperm")
		for u in (cls.w.a1, cls.w.a2, cls.w.b1):
			clear_project_cache(u)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _tasks_as(self, user):
		frappe.set_user(user)
		return set(frappe.get_list("Task", pluck="name"))

	def _projects_as(self, user):
		frappe.set_user(user)
		return set(frappe.get_list("Project", pluck="name"))

	def test_client_sees_only_own_project(self):
		self.assertEqual(self._projects_as(self.w.a1), {self.w.proj_a})
		self.assertEqual(self._projects_as(self.w.b1), {self.w.proj_b})

	def test_task_list_isolated_and_visibility_flag(self):
		seen_a = self._tasks_as(self.w.a1)
		self.assertIn(self.w.task_a_vis, seen_a)
		self.assertIn(self.w.task_a_client, seen_a)
		self.assertNotIn(self.w.task_a_hidden, seen_a, "task tanpa portal_visible bocor")
		self.assertNotIn(self.w.task_b_vis, seen_a, "task tenant lain bocor")
		self.assertNotIn(self.w.task_b_client, seen_a)
		# fase (grup) tampil karena portal_visible=1
		self.assertIn(self.w.phase_a, seen_a)
		self.assertNotIn(self.w.phase_b, seen_a)

	def test_single_doc_permission(self):
		frappe.set_user(self.w.a1)
		self.assertTrue(frappe.has_permission("Task", "read", self.w.task_a_vis))
		self.assertFalse(frappe.has_permission("Task", "read", self.w.task_a_hidden))
		self.assertFalse(frappe.has_permission("Task", "read", self.w.task_b_vis))
		self.assertFalse(frappe.has_permission("Task", "write", self.w.task_a_vis), "klien tidak boleh write Task langsung")
		self.assertTrue(frappe.has_permission("Project", "read", self.w.proj_a))
		self.assertFalse(frappe.has_permission("Project", "read", self.w.proj_b))
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("Task", self.w.task_b_vis).check_permission("read")

	def test_customer_portal_user_gets_customer_projects(self):
		from portal_bridge.utils import can_approve, client_projects, is_client, project_users

		frappe.set_user("Administrator")
		self.assertTrue(is_client(self.w.c1))
		self.assertEqual(client_projects(self.w.c1), [self.w.proj_b])
		self.assertTrue(can_approve(self.w.proj_b, self.w.c1))
		self.assertIn(self.w.c1, [u["user"] for u in project_users(self.w.proj_b, side="klien")])
		self.assertEqual(self._projects_as(self.w.c1), {self.w.proj_b})
		self.assertEqual(self._tasks_as(self.w.c1), {self.w.phase_b, self.w.task_b_vis, self.w.task_b_client})
		# role Portal Klien ditambahkan otomatis saat Customer disimpan
		self.assertTrue(frappe.db.exists("Has Role", {"parent": self.w.c1, "role": "Portal Klien"}))

	def test_client_cannot_list_customers_or_contacts(self):
		frappe.set_user(self.w.a1)
		for dt in ("Customer", "Contact", "Sales Order", "Sales Invoice", "User"):
			try:
				rows = frappe.get_list(dt, pluck="name", limit=5)
			except frappe.PermissionError:
				rows = []
			self.assertEqual(rows, [], f"klien bisa melihat daftar {dt}")

	def test_internal_user_unaffected(self):
		frappe.set_user(self.w.pic)
		names = set(frappe.get_list("Task", filters={"project": ["in", [self.w.proj_a, self.w.proj_b]]}, pluck="name"))
		self.assertIn(self.w.task_a_hidden, names)
		self.assertIn(self.w.task_b_vis, names)

	def test_api_require_access_cross_project(self):
		from portal_bridge import api

		frappe.set_user(self.w.a1)
		self.assertEqual(api.summary(self.w.proj_a)["project"]["project"], self.w.proj_a)
		with self.assertRaises(frappe.PermissionError):
			api.summary(self.w.proj_b)
		with self.assertRaises(frappe.PermissionError):
			api.plan(self.w.proj_b)
		with self.assertRaises(frappe.DoesNotExistError):
			api.task(self.w.task_a_hidden)

	def test_plan_never_contains_hidden(self):
		from portal_bridge import api

		frappe.set_user(self.w.a1)
		names = {t["name"] for ph in api.plan(self.w.proj_a)["phases"] for t in ph["tasks"]}
		self.assertIn(self.w.task_a_vis, names)
		self.assertNotIn(self.w.task_a_hidden, names)

	def test_disabled_portal_project_blocked(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("Project", self.w.proj_a, "portal_enabled", 0)
		clear_project_cache(self.w.a1)
		frappe.clear_cache(user=self.w.a1)
		try:
			self.assertEqual(self._projects_as(self.w.a1), set())
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_value("Project", self.w.proj_a, "portal_enabled", 1)
			clear_project_cache(self.w.a1)
