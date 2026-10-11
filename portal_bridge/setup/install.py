"""Setup idempoten: dijalankan after_install dan after_migrate.

Satu sumber skema untuk Custom Field, Role, Task Type, DocPerm, dan Print Format portal.
Boleh dijalankan berulang kali tanpa duplikat.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.permissions import add_permission, update_permission_property

CLIENT_ROLE = "Portal Klien"
CLIENT_TASK_TYPE = "Klien"

CUSTOM_FIELDS = {
	"Project": [
		{
			"fieldname": "portal_section",
			"fieldtype": "Section Break",
			"label": "Portal Klien",
			"insert_after": "users",
			"collapsible": 1,
		},
		{
			"fieldname": "portal_enabled",
			"fieldtype": "Check",
			"label": "Portal Enabled",
			"insert_after": "portal_section",
			"description": "Buka portal klien untuk project ini",
		},
		{
			"fieldname": "portal_budget_man_days",
			"fieldtype": "Float",
			"label": "Portal Budget Man Days",
			"insert_after": "portal_enabled",
			"description": "Anggaran man-day yang ditampilkan di KPI portal (opsional)",
		},
	],
	"Project User": [
		{
			"fieldname": "portal_can_approve",
			"fieldtype": "Check",
			"label": "Portal Can Approve",
			"insert_after": "hide_timesheets",
			"in_list_view": 1,
			"description": "Boleh konfirmasi notulen dan sign-off",
		},
	],
	"Portal User": [
		{
			"fieldname": "portal_can_approve",
			"fieldtype": "Check",
			"label": "Portal Can Approve",
			"insert_after": "user",
			"in_list_view": 1,
			"description": "Boleh konfirmasi notulen dan sign-off untuk semua project customer ini",
		},
	],
	"Task": [
		{
			"fieldname": "portal_section",
			"fieldtype": "Section Break",
			"label": "Portal Klien",
			"insert_after": "is_milestone",
			"collapsible": 1,
		},
		{
			"fieldname": "portal_visible",
			"fieldtype": "Check",
			"label": "Portal Visible",
			"insert_after": "portal_section",
			"in_standard_filter": 1,
			"description": "Tampil ke klien. Otomatis 1 untuk task bertipe Klien atau milestone",
		},
		{
			"fieldname": "portal_signoff_type",
			"fieldtype": "Select",
			"label": "Portal Signoff Type",
			"options": "\nBRD\nUAT\nTraining\nGo-live\nBAST",
			"insert_after": "portal_visible",
			"depends_on": "is_milestone",
			"description": "Gerbang sign-off; task selesai saat Portal Signoff jenis ini Approved",
		},
	],
	"File": [
		{
			"fieldname": "portal_visible",
			"fieldtype": "Check",
			"label": "Portal Visible",
			"insert_after": "is_private",
			"description": "Boleh diunduh klien lewat portal",
		},
	],
	"User": [
		{
			"fieldname": "portal_notif_pref",
			"fieldtype": "Small Text",
			"label": "Portal Notification Preference",
			"insert_after": "mobile_no",
			"hidden": 1,
			"description": "JSON preferensi notifikasi portal",
		},
	],
}

# DocPerm untuk role Portal Klien pada DocType bawaan. Tulis-menulis klien hanya lewat API portal
# (ignore_permissions setelah pemeriksaan), jadi di sini hanya read.
CLIENT_DOCPERMS = {
	"Project": {"read": 1},
	"Task": {"read": 1},
	"File": {"read": 1},
	"Task Type": {"read": 1},
}

# DocPerm yang pernah diberikan lalu dicabut (Customer/Contact: klien tidak boleh melihat daftar pelanggan lain)
REVOKED_CLIENT_DOCPERMS = ("Customer", "Contact")

INTERNAL_ROLES = ("Projects Manager", "Projects User")


def after_install():
	setup()


def after_migrate():
	setup()


def setup():
	seed_settings()
	make_role()
	make_task_type()
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True, update=True)
	make_docperms()
	make_print_formats()
	frappe.db.commit()
	frappe.clear_cache()


def seed_settings():
	"""Single DocType tidak punya baris default sampai disimpan; isi nilai awal bila kosong."""
	doc = frappe.get_single("Portal Bridge Settings")
	defaults = {
		"product_name": "Sopwer Portal", "locale": "id", "brand_color": "#2E78D9", "brand_hover_color": "#1F5FB8", "accent_color": "#FBB217",
		"density_default": "rapat", "magic_link_days": 7, "session_days": 30, "meeting_note_confirm_days": 2, "signoff_due_days": 5,
		"reminder_hour": 8, "max_overdue_reminders": 3, "comment_delete_minutes": 10, "whatsapp_send_path": "/api/send",
		"whatsapp_sender_label": "Portal", "email_enabled": 1,
	}
	changed = False
	for k, v in defaults.items():
		if doc.get(k) in (None, ""):
			doc.set(k, v)
			changed = True
	if changed:
		doc.save(ignore_permissions=True)


def make_role():
	if not frappe.db.exists("Role", CLIENT_ROLE):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": CLIENT_ROLE,
				"desk_access": 0,
				"is_custom": 1,
				"disabled": 0,
			}
		).insert(ignore_permissions=True)


def make_task_type():
	if not frappe.db.exists("Task Type", CLIENT_TASK_TYPE):
		frappe.get_doc(
			{
				"doctype": "Task Type",
				"name": CLIENT_TASK_TYPE,
				"description": "Tugas yang dikerjakan sisi klien (data, UAT, keputusan)",
			}
		).insert(ignore_permissions=True)


def make_docperms():
	for doctype in REVOKED_CLIENT_DOCPERMS:
		for name in frappe.get_all("Custom DocPerm", filters={"parent": doctype, "role": CLIENT_ROLE}, pluck="name"):
			frappe.delete_doc("Custom DocPerm", name, ignore_permissions=True, force=True)
	for doctype, rights in CLIENT_DOCPERMS.items():
		if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": CLIENT_ROLE, "permlevel": 0}):
			add_permission(doctype, CLIENT_ROLE, 0)
		for ptype, value in rights.items():
			update_permission_property(doctype, CLIENT_ROLE, 0, ptype, value, validate=False)


def make_print_formats():
	from portal_bridge.setup.print_formats import PRINT_FORMATS

	for name, spec in PRINT_FORMATS.items():
		if frappe.db.exists("Print Format", name):
			doc = frappe.get_doc("Print Format", name)
			if doc.html != spec["html"]:
				doc.html = spec["html"]
				doc.save(ignore_permissions=True)
			continue
		frappe.get_doc(
			{
				"doctype": "Print Format",
				"name": name,
				"doc_type": spec["doc_type"],
				"module": "Portal Bridge",
				"print_format_type": "Jinja",
				"standard": "No",
				"custom_format": 1,
				"disabled": 0,
				"html": spec["html"],
				"font_size": 11,
				"margin_top": 15,
				"margin_bottom": 15,
				"margin_left": 15,
				"margin_right": 15,
			}
		).insert(ignore_permissions=True)
