"""Aturan 9–11: weekly update."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate, now_datetime

from portal_bridge import notify
from portal_bridge.setup.install import CLIENT_TASK_TYPE
from portal_bridge.utils import INTERNAL_ROLES, audit, is_client, project_users, today


def week_no_for(project: str, on=None) -> int:
	start = frappe.db.get_value("Project", project, "expected_start_date")
	on = getdate(on or today())
	if not start:
		return int(on.isocalendar()[1])
	return max(1, date_diff(on, getdate(start)) // 7 + 1)


def rule_9_draft(project: str, on=None) -> str | None:
	"""Draft otomatis: done = Task Completed 7 hari terakhir, in_progress = Working, client_actions = Task Klien terbuka."""
	on = getdate(on or today())
	week_no = week_no_for(project, on)
	if frappe.db.exists("Portal Weekly Update", {"project": project, "week_no": week_no}):
		return None
	period_end = on
	period_start = add_days(on, -6)
	done = frappe.get_all(
		"Task",
		filters={"project": project, "portal_visible": 1, "status": "Completed", "is_group": 0,
		         "completed_on": ["between", [period_start, period_end]]},
		fields=["subject"], order_by="completed_on asc",
	)
	working = frappe.get_all(
		"Task",
		filters={"project": project, "portal_visible": 1, "status": "Working", "is_group": 0},
		fields=["subject", "progress"], order_by="exp_end_date asc",
	)
	client_open = frappe.get_all(
		"Task",
		filters={"project": project, "portal_visible": 1, "type": CLIENT_TASK_TYPE, "is_group": 0,
		         "status": ["not in", ["Completed", "Cancelled"]]},
		fields=["name", "subject", "_assign"], order_by="exp_end_date asc",
	)

	def ul(items):
		return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>" if items else ""

	doc = frappe.get_doc(
		{
			"doctype": "Portal Weekly Update",
			"project": project,
			"week_no": week_no,
			"period_start": period_start,
			"period_end": period_end,
			"status": "Draft",
			"done": ul([d.subject for d in done]),
			"in_progress": ul([f"{w.subject} ({int(w.progress or 0)}%)" for w in working]),
			"risks": "",
			"client_actions": [{"text": c.subject, "task": c.name} for c in client_open],
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def rule_10_check_approver(doc):
	"""Hanya user internal (role Projects Manager/User atau System Manager) yang ada di Project User (atau Administrator)."""
	user = frappe.session.user
	if user == "Administrator":
		return
	roles = set(frappe.get_roles(user))
	if is_client(user) or not (roles & INTERNAL_ROLES):
		frappe.throw(_("Hanya PIC internal yang boleh menyetujui weekly update."), frappe.PermissionError)
	if "System Manager" in roles:
		return
	if user not in [u["user"] for u in project_users(doc.project, side="internal")]:
		frappe.throw(_("Anda tidak terdaftar sebagai PIC di proyek ini."), frappe.PermissionError)


def rule_11_new_version(name: str) -> str:
	"""Mengubah update yang sudah Approved = dokumen baru week_no sama, version naik; yang lama tetap di riwayat."""
	old = frappe.get_doc("Portal Weekly Update", name)
	if old.status != "Approved":
		frappe.throw(_("Versi baru hanya dibuat dari update yang sudah Approved."))
	new = frappe.copy_doc(old)
	new.status = "Draft"
	new.version = (old.version or 1) + 1
	new.approved_by = None
	new.approved_on = None
	new.amended_from_update = old.name
	new.insert(ignore_permissions=True)
	return new.name


def on_approved(doc):
	doc.approved_by = frappe.session.user
	doc.approved_on = now_datetime()
	project_name = frappe.db.get_value("Project", doc.project, "project_name")
	ctx = {
		"week_no": doc.week_no,
		"project_name": project_name,
		"health": doc.health or "-",
		"link": f"{frappe.utils.get_url()}",
	}
	from portal_bridge.utils import portal_url

	ctx["link"] = f"{portal_url()}/{doc.project}/ringkasan"
	for u in project_users(doc.project, side="klien"):
		notify.enqueue(u["user"], "weekly_approved", ctx, doc.project, dedupe_key=f"weekly:{doc.name}:{u['user']}")
	frappe.cache.delete_value(f"portal_summary:{doc.project}")
	audit("weekly_approved", doc.project, doc.doctype, doc.name)
