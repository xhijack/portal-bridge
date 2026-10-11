"""Aturan 5–8: tugas klien, status, keterlambatan, "menahan", reminder."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import date_diff, getdate, now_datetime

from portal_bridge import notify
from portal_bridge.setup.install import CLIENT_TASK_TYPE
from portal_bridge.utils import assigned_users, audit, elevated, internal_pics, is_client, settings, today, user_display

STATUS_KEY = {
	"Open": "open",
	"Working": "working",
	"Pending Review": "review",
	"Overdue": "late",
	"Template": "open",
	"Completed": "done",
	"Cancelled": "cancelled",
}


def late_days(task) -> int:
	if not task.get("exp_end_date") or task.get("status") in ("Completed", "Cancelled"):
		return 0
	d = date_diff(today(), getdate(task.exp_end_date))
	return d if d > 0 else 0


def status_key(task) -> str:
	"""Kata status untuk UI (frontend menerjemahkan): done/late/working/review/open/cancelled/pending_signoff."""
	if task.get("status") == "Completed":
		return "done"
	if late_days(task) > 0:
		return "late"
	if task.get("is_milestone") and task.get("portal_signoff_type"):
		pending = frappe.db.exists(
			"Portal Signoff", {"project": task.get("project"), "kind": task.portal_signoff_type, "status": "Pending", "docstatus": 0}
		)
		if pending:
			return "pending_signoff"
	return STATUS_KEY.get(task.get("status"), "open")


def rule_5_is_complete_client_task(task) -> tuple[bool, str | None]:
	"""Tugas klien harus punya assignment ke Website User klien dan exp_end_date."""
	if task.get("type") != CLIENT_TASK_TYPE:
		return True, None
	users = [u for u in assigned_users(task.get("_assign")) if is_client(u)]
	if not users:
		return False, "belum ada PIC klien"
	if not task.get("exp_end_date"):
		return False, "belum ada tenggat"
	return True, None


def rule_7_blocks(task_name: str, wbs_map: dict[str, str] | None = None) -> list[dict]:
	"""Task lain yang depends_on task ini (dibaca terbalik), beserta fasenya → teks "menahan …"."""
	rows = frappe.get_all(
		"Task Depends On",
		filters={"task": task_name, "parenttype": "Task"},
		fields=["parent"],
	)
	if not rows:
		return []
	names = [r.parent for r in rows]
	own_parent = frappe.db.get_value("Task", task_name, "parent_task")
	deps = frappe.get_all(
		"Task",
		filters={"name": ["in", names], "status": ["not in", ["Completed", "Cancelled"]]},
		fields=["name", "subject", "parent_task", "is_group", "portal_visible"],
	)
	out = []
	for d in deps:
		if d.is_group and d.name == own_parent:
			continue  # grup induk selalu "menunggu" anaknya; bukan blokir
		out.append(
			{
				"task": d.name,
				"subject": d.subject,
				"wbs": (wbs_map or {}).get(d.name),
				"phase": frappe.db.get_value("Task", d.parent_task, "subject") if d.parent_task else None,
				"is_phase": bool(d.is_group),
			}
		)
	return out


def blocks_text(blocks: list[dict]) -> str:
	parts = []
	for b in blocks:
		if b.get("is_phase"):
			parts.append(f"fase {b['subject']}")
		elif b.get("wbs"):
			parts.append(b["wbs"])
		else:
			parts.append(b["subject"])
	return " & ".join(parts)


def rule_6_complete(task_name: str, note: str | None = None):
	"""Klien menandai selesai: Task → Completed, completed_by, completed_on, progress 100. Tidak mengubah judul/tenggat/PIC."""
	task = frappe.get_doc("Task", task_name)
	user = frappe.session.user
	if is_client(user):
		if task.type != CLIENT_TASK_TYPE or not task.portal_visible:
			frappe.throw(_("Hanya tugas klien yang bisa ditandai selesai dari portal."), frappe.PermissionError)
		if user not in assigned_users(task._assign):
			frappe.throw(_("Tugas ini ditugaskan ke orang lain."), frappe.PermissionError)
	if task.status in ("Completed", "Cancelled"):
		frappe.throw(_("Tugas sudah {0}.").format(task.status))
	task.status = "Completed"
	task.progress = 100
	task.completed_by = user
	task.completed_on = today()
	task.flags.ignore_permissions = True
	with elevated():
		task.save(ignore_permissions=True)
	# tutup ToDo assignment
	frappe.db.set_value(
		"ToDo",
		{"reference_type": "Task", "reference_name": task_name, "status": "Open"},
		"status",
		"Closed",
		update_modified=False,
	)
	if note:
		task.add_comment("Comment", note)
	audit("task_complete", task.project, "Task", task_name, {"note": note})
	return task


def rule_8_reminders(now=None) -> int:
	"""Reminder H-2, H-0 pada jam reminder (waktu site), lalu tiap 2 hari saat terlambat maksimal N kali; setelah itu ke PIC.
	Idempoten per task per hari lewat dedupe_key. Mengembalikan jumlah notifikasi yang diantrekan."""
	now = now or now_datetime()
	s = settings()
	if now.hour != int(s.reminder_hour or 8):
		return 0
	tasks = frappe.get_all(
		"Task",
		filters={
			"type": CLIENT_TASK_TYPE,
			"portal_visible": 1,
			"status": ["not in", ["Completed", "Cancelled"]],
			"exp_end_date": ["is", "set"],
		},
		fields=["name", "subject", "project", "exp_end_date", "_assign", "status"],
	)
	count = 0
	tday = getdate(now)
	for t in tasks:
		if not frappe.db.get_value("Project", t.project, "portal_enabled"):
			continue
		days = date_diff(getdate(t.exp_end_date), tday)
		users = [u for u in assigned_users(t._assign) if is_client(u)]
		if not users:
			continue
		blocks = rule_7_blocks(t.name)
		ctx = {
			"subject": t.subject,
			"due": frappe.utils.formatdate(t.exp_end_date),
			"blocks": ("Menahan " + blocks_text(blocks) + ".") if blocks else "",
			"late_days": -days if days < 0 else 0,
			"link": f"{frappe.utils.get_url() if not s.portal_url else s.portal_url.rstrip('/')}/{t.project}/tugas?task={t.name}",
		}
		template = None
		if days == 2:
			template = "task_h2"
		elif days == 0:
			template = "task_h0"
		elif days < 0 and (-days) % 2 == 0:
			sent = frappe.db.count(
				"Portal Notification",
				{"template": "task_overdue", "dedupe_key": ["like", f"remind:{t.name}:%"], "status": ["in", ["Queued", "Sent"]]},
			)
			if sent < int(s.max_overdue_reminders or 3):
				template = "task_overdue"
			elif sent == int(s.max_overdue_reminders or 3):
				for pic in internal_pics(t.project):
					if notify.enqueue(pic, "task_overdue_pic", {**ctx, "assignee": user_display(users[0])["full_name"]}, t.project,
					                  dedupe_key=f"remind-pic:{t.name}:{pic}"):
						count += 1
				continue
		if not template:
			continue
		for u in users:
			if notify.enqueue(u, template, ctx, t.project, dedupe_key=f"remind:{t.name}:{tday}:{u}"):
				count += 1
	return count
