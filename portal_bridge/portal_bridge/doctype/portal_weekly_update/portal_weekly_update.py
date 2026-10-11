# Copyright (c) 2026, PT Sopwer Teknologi Indonesia and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from portal_bridge.utils import cleanup_portal_links

from portal_bridge.rules.weekly import on_approved, rule_10_check_approver


class PortalWeeklyUpdate(Document):
	def validate(self):
		before = self.get_doc_before_save() if not self.is_new() else None
		if before and before.status == "Approved":
			# aturan 11: yang sudah Approved tidak diubah; buat versi baru
			frappe.throw(_("Weekly update yang sudah Approved tidak bisa diubah. Buat versi baru (portal_bridge.api.weekly_new_version)."))
		if self.status == "Approved" and (not before or before.status != "Approved"):
			if frappe.db.exists("Portal Weekly Update", {"project": self.project, "week_no": self.week_no, "status": "Approved",
			                                             "name": ["!=", self.name], "version": self.version}):
				frappe.throw(_("Sudah ada update Approved untuk minggu {0} versi {1}.").format(self.week_no, self.version))
			rule_10_check_approver(self)
			self.flags.just_approved = True

	def on_update(self):
		if self.flags.just_approved:
			on_approved(self)
			self.db_set({"approved_by": self.approved_by, "approved_on": self.approved_on}, update_modified=False)
		frappe.cache.delete_value(f"portal_summary:{self.project}")

	def on_trash(self):
		cleanup_portal_links(self.doctype, self.name)
