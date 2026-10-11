"""Pekerjaan terjadwal (lihat hooks.scheduler_events)."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, now_datetime

from portal_bridge import notify
from portal_bridge.rules.client_tasks import rule_8_reminders
from portal_bridge.rules.meeting_notes import rule_14_close_overdue
from portal_bridge.rules.weekly import rule_9_draft
from portal_bridge.utils import portal_url, project_users, today


def send_notifications(limit: int = 200) -> int:
	names = frappe.get_all(
		"Portal Notification",
		filters=[["status", "=", "Queued"], ["send_after", "is", "not set"]],
		pluck="name",
		order_by="creation asc",
		limit=limit,
	)
	names += frappe.get_all(
		"Portal Notification",
		filters=[["status", "=", "Queued"], ["send_after", "<=", now_datetime()]],
		pluck="name",
		order_by="creation asc",
		limit=limit,
	)
	sent = 0
	for n in dict.fromkeys(names):
		if notify.send(n):
			sent += 1
		frappe.db.commit()
	return sent


def remind_client_tasks() -> int:
	n = rule_8_reminders()
	frappe.db.commit()
	return n


def draft_weekly() -> list[str]:
	created = []
	for p in frappe.get_all("Project", filters={"portal_enabled": 1, "status": "Open"}, pluck="name"):
		try:
			name = rule_9_draft(p)
			if name:
				created.append(name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error("portal_bridge draft_weekly gagal", frappe.get_traceback())
	return created


def close_confirmations() -> int:
	n = rule_14_close_overdue()
	frappe.db.commit()
	return n


def signoff_due_reminders() -> int:
	tomorrow = add_days(today(), 1)
	rows = frappe.get_all(
		"Portal Signoff",
		filters={"status": "Pending", "docstatus": 0, "due": tomorrow},
		fields=["name", "project", "kind", "title", "due"],
	)
	count = 0
	for r in rows:
		ctx = {"kind": r.kind, "title": r.title, "due": frappe.utils.formatdate(r.due),
		       "link": f"{portal_url()}/{r.project}/approvals/{r.name}"}
		for u in project_users(r.project, side="klien"):
			if u["can_approve"] and notify.enqueue(u["user"], "signoff_due", ctx, r.project, dedupe_key=f"so-due:{r.name}:{u['user']}"):
				count += 1
	frappe.db.commit()
	return count
