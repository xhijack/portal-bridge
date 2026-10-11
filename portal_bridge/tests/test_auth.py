"""Uji magic link: hanya hash yang disimpan, token baru membatalkan lama, kedaluwarsa, tukar sesi."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from portal_bridge import auth
from portal_bridge.tests.factory import make_world


class TestAuth(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.w = make_world("pbauth")
		frappe.db.set_single_value("Portal Bridge Settings", "whatsapp_enabled", 0)
		frappe.db.set_single_value("Portal Bridge Settings", "email_enabled", 0)  # kanal Log saja saat uji

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_find_user_by_phone_and_email(self):
		self.assertEqual(auth.find_portal_user(phone=frappe.db.get_value("User", self.w.a1, "mobile_no")), self.w.a1)
		self.assertEqual(auth.find_portal_user(phone="+" + frappe.db.get_value("User", self.w.a1, "mobile_no")), self.w.a1)
		self.assertEqual(auth.find_portal_user(email=self.w.b1.upper()), self.w.b1)
		self.assertIsNone(auth.find_portal_user(email="tidak-ada@klien.test"))
		self.assertIsNone(auth.find_portal_user(phone="08"))

	def test_issue_stores_hash_only_and_invalidates_previous(self):
		name1, token1 = auth.issue_magic_link(self.w.a1, "/x", send=False)
		row = frappe.db.get_value("Portal Magic Link", name1, ["token_hash", "expires_at"], as_dict=True)
		self.assertNotEqual(row.token_hash, token1)
		self.assertEqual(len(row.token_hash), 64)
		self.assertGreater(row.expires_at, now_datetime())
		name2, token2 = auth.issue_magic_link(self.w.a1, "/y", send=False)
		self.assertLessEqual(frappe.db.get_value("Portal Magic Link", name1, "expires_at"), now_datetime())
		res = auth.exchange(token1)
		self.assertFalse(res["ok"])
		self.assertEqual(res["code"], "expired")
		frappe.set_user("Administrator")
		res2 = auth.exchange(token2)
		self.assertTrue(res2["ok"])
		self.assertEqual(res2["target"], "/y")
		self.assertEqual(frappe.session.user, self.w.a1)
		self.assertIsNotNone(frappe.db.get_value("Portal Magic Link", name2, "used_at"))
		self.assertEqual([p["project"] for p in res2["me"]["projects"]], [self.w.proj_a])
		self.assertTrue(res2["me"]["projects"][0]["can_approve"])

	def test_expired_token_rejected(self):
		name, token = auth.issue_magic_link(self.w.b1, send=False)
		frappe.db.set_value("Portal Magic Link", name, "expires_at", add_days(now_datetime(), -1))
		res = auth.exchange(token)
		self.assertFalse(res["ok"])

	def test_request_magic_link_never_leaks(self):
		frappe.set_user("Guest")
		self.assertEqual(auth.request_magic_link(email="tidak-ada@klien.test"), {"sent": True})
		self.assertEqual(auth.request_magic_link(email=self.w.a2), {"sent": True})
		self.assertTrue(frappe.db.exists("Portal Magic Link", {"user": self.w.a2}))
		self.assertEqual(frappe.db.get_value("Portal Notification", {"user": self.w.a2, "template": "magic_link"}, "status"), "Sent")

	def test_rate_limit_per_identity(self):
		frappe.set_user("Guest")
		frappe.cache.delete_value("portal_rl:id:tes-rl@klien.test")
		for _ in range(3):
			auth.request_magic_link(email="tes-rl@klien.test")
		with self.assertRaises(frappe.RateLimitExceededError):
			auth.request_magic_link(email="tes-rl@klien.test")
		frappe.cache.delete_value("portal_rl:id:tes-rl@klien.test")

	def test_me_requires_login(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.AuthenticationError):
			auth.me()
