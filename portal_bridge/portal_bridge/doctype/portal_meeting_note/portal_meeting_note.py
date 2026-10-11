# Copyright (c) 2026, PT Sopwer Teknologi Indonesia and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from portal_bridge.utils import cleanup_portal_links

from portal_bridge.rules.meeting_notes import rule_12_on_sent, rule_15_on_edit_after_sent


class PortalMeetingNote(Document):
	def before_insert(self):
		# seq dipakai autoname, jadi harus terisi sebelum set_new_name()
		if not self.seq:
			last = frappe.db.get_value("Portal Meeting Note", {"project": self.project}, "max(seq)")
			self.seq = (last or 0) + 1

	def validate(self):
		for i, t in enumerate(self.topics, start=1):
			if not t.seq:
				t.seq = i
		before = self.get_doc_before_save() if not self.is_new() else None
		became_sent = self.status == "Sent" and (before is None or before.status == "Draft")
		if became_sent:
			rule_12_on_sent(self)
		elif before is not None:
			rule_15_on_edit_after_sent(self, before)

	def on_update(self):
		frappe.cache.delete_value(f"portal_summary:{self.project}")

	def on_trash(self):
		cleanup_portal_links(self.doctype, self.name)
