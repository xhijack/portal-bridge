"""Aturan 1–4: progres, fase, kesehatan proyek."""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate

from portal_bridge.utils import settings, today

PHASE_FIELDS = ["name", "subject", "status", "exp_start_date", "exp_end_date", "progress", "task_weight", "idx"]
TASK_FIELDS = [
	"name", "subject", "status", "type", "progress", "exp_start_date", "exp_end_date", "task_weight",
	"is_milestone", "portal_signoff_type", "parent_task", "_assign", "completed_on", "completed_by", "priority",
]


def rule_1_progress(project: str) -> float:
	"""Progres = Project.percent_complete (ERPNext menghitung dengan metode Task Weight); portal tidak menghitung ulang."""
	if settings().progress_from_visible_only:
		return rule_2_visible_only_progress(project)
	return flt(frappe.db.get_value("Project", project, "percent_complete"))


def rule_2_visible_only_progress(project: str) -> float:
	"""Opsi: progres hanya dari task yang tampil ke klien, rumus sama dengan ERPNext (Σ w×p / Σ w)."""
	rows = frappe.get_all(
		"Task",
		filters={"project": project, "portal_visible": 1, "is_group": 0, "status": ["!=", "Cancelled"]},
		fields=["task_weight", "progress"],
	)
	total_w = sum(flt(r.task_weight) for r in rows)
	if not total_w:
		if not rows:
			return 0.0
		return round(sum(flt(r.progress) for r in rows) / len(rows), 2)
	return round(sum(flt(r.task_weight) * flt(r.progress) for r in rows) / total_w, 2)


def rule_3_phases(project: str) -> list[dict]:
	"""Fase = Task grup level 1 urut exp_start_date; fase aktif = grup pertama dengan anak belum Completed."""
	groups = frappe.get_all(
		"Task",
		filters={"project": project, "is_group": 1, "parent_task": ["in", ["", None]], "status": ["!=", "Cancelled"]},
		fields=PHASE_FIELDS,
		order_by="exp_start_date asc, idx asc, creation asc",
	)
	children = frappe.get_all(
		"Task",
		filters={"project": project, "is_group": 0, "parent_task": ["in", [g.name for g in groups] or [""]],
		         "status": ["!=", "Cancelled"]},
		fields=TASK_FIELDS,
		order_by="exp_start_date asc, idx asc, creation asc",
	)
	by_parent: dict[str, list] = {}
	for c in children:
		by_parent.setdefault(c.parent_task, []).append(c)

	phases = []
	active_found = False
	for i, g in enumerate(groups, start=1):
		kids = by_parent.get(g.name, [])
		visible_kids = [k for k in kids if k.get("portal_visible", 1)]
		done = all(k.status == "Completed" for k in kids) if kids else g.status == "Completed"
		started = any(k.status in ("Working", "Pending Review", "Completed") for k in kids) or (
			g.exp_start_date and getdate(g.exp_start_date) <= today()
		)
		if done:
			status = "done"
		elif not active_found:
			status = "active"
			active_found = True
		elif started:
			status = "working"
		else:
			status = "planned"
		gate = next((k for k in kids if k.is_milestone and k.portal_signoff_type), None)
		phases.append(
			{
				"name": g.name,
				"no": i,
				"label": g.subject,
				"status": status,
				"start": g.exp_start_date,
				"end": g.exp_end_date,
				"progress": flt(g.progress),
				"task_count": len(visible_kids),
				"client_task_count": sum(1 for k in visible_kids if k.type == "Klien"),
				"late_count": sum(1 for k in visible_kids if _is_late(k)),
				"gate": {"name": gate.name, "subject": gate.subject, "kind": gate.portal_signoff_type, "due": gate.exp_end_date,
				         "status": gate.status} if gate else None,
			}
		)
	return phases


def active_phase(project: str) -> dict | None:
	for p in rule_3_phases(project):
		if p["status"] == "active":
			return p
	return None


def rule_4_health(project: str) -> dict:
	"""Kesehatan dari Portal Weekly Update Approved terakhir; tanpa update → 'Belum ada penilaian'."""
	row = frappe.get_all(
		"Portal Weekly Update",
		filters={"project": project, "status": "Approved"},
		fields=["name", "health", "week_no", "approved_on", "version"],
		order_by="week_no desc, version desc",
		limit=1,
	)
	if not row:
		return {"health": None, "label": "Belum ada penilaian", "weekly": None}
	r = row[0]
	return {"health": r.health, "label": r.health or "Belum ada penilaian", "weekly": r.name, "week_no": r.week_no}


def _is_late(task) -> bool:
	return bool(task.exp_end_date) and getdate(task.exp_end_date) < today() and task.status not in ("Completed", "Cancelled")
