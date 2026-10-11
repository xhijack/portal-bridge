app_name = "portal_bridge"
app_title = "Portal Bridge"
app_publisher = "PT Sopwer Teknologi Indonesia"
app_description = "Jembatan portal klien proyek implementasi (auth magic link, permission, DocType portal, endpoint gabungan)"
app_email = "ramdani@sopwer.net"
app_license = "mit"

required_apps = ["frappe", "erpnext"]

# Setup (idempoten): Custom Field, Role, Task Type, DocPerm, Print Format
after_install = "portal_bridge.setup.install.after_install"
after_migrate = "portal_bridge.setup.install.after_migrate"

# Permission untuk role Portal Klien (Website User): hanya dokumen bertanda portal_visible
# pada project yang user-nya tercatat di Project User.
permission_query_conditions = {
	"Project": "portal_bridge.permissions.project_query",
	"Task": "portal_bridge.permissions.task_query",
	"File": "portal_bridge.permissions.file_query",
	"Portal Weekly Update": "portal_bridge.permissions.weekly_query",
	"Portal Meeting Note": "portal_bridge.permissions.meeting_note_query",
	"Portal Signoff": "portal_bridge.permissions.signoff_query",
	"Portal Comment": "portal_bridge.permissions.comment_query",
	"Portal Evidence": "portal_bridge.permissions.evidence_query",
}

has_permission = {
	"Project": "portal_bridge.permissions.project_has_permission",
	"Task": "portal_bridge.permissions.task_has_permission",
	"File": "portal_bridge.permissions.file_has_permission",
	"Portal Weekly Update": "portal_bridge.permissions.weekly_has_permission",
	"Portal Meeting Note": "portal_bridge.permissions.meeting_note_has_permission",
	"Portal Signoff": "portal_bridge.permissions.signoff_has_permission",
	"Portal Comment": "portal_bridge.permissions.comment_has_permission",
	"Portal Evidence": "portal_bridge.permissions.evidence_has_permission",
}

doc_events = {
	"Task": {
		"before_save": "portal_bridge.events.task_before_save",
		"on_update": "portal_bridge.events.task_on_update",
	},
}

scheduler_events = {
	"cron": {
		# antrean notifikasi tiap 5 menit
		"*/5 * * * *": ["portal_bridge.tasks.send_notifications"],
		# draft weekly update tiap Jumat 09:00 (waktu site)
		"0 9 * * 5": ["portal_bridge.tasks.draft_weekly"],
	},
	"hourly": [
		"portal_bridge.tasks.remind_client_tasks",
	],
	"daily": [
		"portal_bridge.tasks.close_confirmations",
		"portal_bridge.tasks.signoff_due_reminders",
	],
}

# Endpoint yang dibuka untuk sesi klien (Website User) tetap lewat whitelist di modul masing-masing.
