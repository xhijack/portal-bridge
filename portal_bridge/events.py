"""doc_events untuk DocType bawaan."""

import frappe

from portal_bridge.setup.install import CLIENT_TASK_TYPE


def task_before_save(doc, method=None):
	# Aturan: task bertipe Klien atau milestone otomatis tampil ke klien
	if doc.type == CLIENT_TASK_TYPE or doc.is_milestone:
		doc.portal_visible = 1


def task_on_update(doc, method=None):
	# Invalidasi cache ringkasan project
	if doc.project:
		frappe.cache.delete_value(f"portal_summary:{doc.project}")


def _ensure_client_role(users):
	"""Website User yang dicantumkan di Customer.portal_users / Project.users otomatis diberi role Portal Klien
	agar /api/resource bawaan ikut terbuka (dibatasi permission hook)."""
	from portal_bridge.setup.install import CLIENT_ROLE
	from portal_bridge.utils import clear_project_cache

	for u in {x for x in users if x}:
		clear_project_cache(u)
		if frappe.db.get_value("User", u, "user_type") != "Website User":
			continue
		if not frappe.db.exists("Has Role", {"parent": u, "parenttype": "User", "role": CLIENT_ROLE}):
			user = frappe.get_doc("User", u)
			user.append("roles", {"role": CLIENT_ROLE})
			user.flags.ignore_permissions = True
			user.save(ignore_permissions=True)


def customer_on_update(doc, method=None):
	_ensure_client_role([r.user for r in doc.get("portal_users", [])])


def project_on_update(doc, method=None):
	_ensure_client_role([r.user for r in doc.get("users", [])])
	frappe.cache.delete_value(f"portal_summary:{doc.name}")
