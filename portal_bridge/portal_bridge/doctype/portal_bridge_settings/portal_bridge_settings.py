# Copyright (c) 2026, PT Sopwer Teknologi Indonesia and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class PortalBridgeSettings(Document):
	def validate(self):
		if self.portal_url:
			self.portal_url = self.portal_url.rstrip("/")
