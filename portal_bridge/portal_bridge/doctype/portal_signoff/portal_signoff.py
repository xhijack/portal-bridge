# Copyright (c) 2026, PT Sopwer Teknologi Indonesia and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from portal_bridge.utils import cleanup_portal_links

from portal_bridge.rules.signoff import (
	notify_pending,
	rule_20_on_create,
	rule_23_on_cr_approved,
	rule_23_validate_cr,
	rule_24_close_gate,
)


class PortalSignoff(Document):
	def validate(self):
		if self.is_new() and self.amended_from:
			# Amend: versi baru mulai dari Pending tanpa keputusan lama (aturan 22)
			self.status = "Pending"
			for f in ("decided_by_user", "decided_name", "decided_on", "decided_ip", "client_note", "pdf_file", "pdf_hash"):
				self.set(f, None)
			self.requested_on = None
			self.due = None
		if self.is_new():
			rule_20_on_create(self)
		rule_23_validate_cr(self)
		if self._action == "submit" and self.status != "Approved":
			frappe.throw(_("Hanya sign-off berstatus Approved yang di-submit; keputusan dibuat klien lewat portal."))

	def after_insert(self):
		if self.status == "Pending":
			notify_pending(self)

	def on_submit(self):
		rule_24_close_gate(self)
		rule_23_on_cr_approved(self)

	def on_update(self):
		frappe.cache.delete_value(f"portal_summary:{self.project}")

	def on_trash(self):
		cleanup_portal_links(self.doctype, self.name)
