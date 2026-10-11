"""Aturan 1–4: progres, fase, kesehatan proyek."""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate

from portal_bridge.utils import settings, today

PHASE_FIELDS = ["name", "subject", "status", "exp_start_date", "exp_end_date", "progress", "task_weight", "idx"]
TASK_FIELDS = [
	"name", "subject", "status", "type", "progress", "exp_start_date", "exp_end_date", "task_weight",
	"is_milestone", "portal_signoff_type", "parent_task", "_assign", "completed_on", "completed_by", "priority",
	"is_group", "portal_visible", "act_start_date", "act_end_date", "idx",
]


def rule_1_progress(project: str) -> float:
	"""Progres = Project.percent_complete (ERPNext). Bila ERPNext belum menghitung (0 padahal ada task selesai)
	atau tenant memilih progress_from_visible_only, portal menghitung dari task dengan rumus yang sama."""
	if settings().progress_from_visible_only:
		return rule_2_visible_only_progress(project)
	pc = flt(frappe.db.get_value("Project", project, "percent_complete"))
	if pc > 0:
		return pc
	return weighted_progress(project, visible_only=False)


def weighted_progress(project: str, visible_only: bool) -> float:
	"""Σ(task_weight × progress) / Σ(task_weight) atas task daun; tanpa bobot → rata-rata."""
	filters = {"project": project, "is_group": 0, "status": ["!=", "Cancelled"]}
	if visible_only:
		filters["portal_visible"] = 1
	rows = frappe.get_all("Task", filters=filters, fields=["task_weight", "progress", "status"])
	if not rows:
		return 0.0
	prog = [100.0 if r.status == "Completed" else flt(r.progress) for r in rows]
	total_w = sum(flt(r.task_weight) for r in rows)
	if not total_w:
		return round(sum(prog) / len(rows), 1)
	return round(sum(flt(r.task_weight) * p for r, p in zip(rows, prog)) / total_w, 1)


def rule_2_visible_only_progress(project: str) -> float:
	"""Opsi: progres hanya dari task yang tampil ke klien."""
	return weighted_progress(project, visible_only=True)


def load_tree(project: str) -> tuple[list, dict]:
	"""Semua task project (kecuali Cancelled) sebagai pohon. Daun hanya yang portal_visible=1;
	grup ikut tampil bila ia sendiri visible atau punya keturunan yang tampil."""
	rows = frappe.get_all(
		"Task",
		filters={"project": project, "status": ["!=", "Cancelled"]},
		fields=TASK_FIELDS,
		order_by="exp_start_date asc, idx asc, creation asc",
		limit_page_length=5000,
	)
	by_name = {r.name: r for r in rows}
	children: dict = {}
	for r in rows:
		parent = r.parent_task if r.parent_task in by_name else None
		children.setdefault(parent, []).append(r)

	def prune(node) -> bool:
		kids = [k for k in children.get(node.name, []) if prune(k)]
		node["children"] = kids
		if node.is_group:
			return bool(kids) or bool(node.portal_visible)
		return bool(node.portal_visible)

	roots = [r for r in children.get(None, []) if prune(r)]
	return roots, by_name


def leaves(node) -> list:
	if not node.get("is_group"):
		return [node]
	out = []
	for k in node.get("children", []):
		out += leaves(k)
	return out


def node_progress(node) -> float:
	ls = leaves(node)
	if not ls:
		return 100.0 if node.status == "Completed" else flt(node.progress)
	prog = [100.0 if l.status == "Completed" else flt(l.progress) for l in ls]
	total_w = sum(flt(l.task_weight) for l in ls)
	if not total_w:
		return round(sum(prog) / len(ls), 1)
	return round(sum(flt(l.task_weight) * p for l, p in zip(ls, prog)) / total_w, 1)


def rule_3_phases(project: str, with_tree: bool = False) -> list[dict]:
	"""Fase = Task grup level 1 urut exp_start_date; fase aktif = grup pertama dengan keturunan belum Completed.
	with_tree=True menyertakan pohon anak (grup bertingkat) di key 'tree'."""
	roots, _ = load_tree(project)
	groups = [r for r in roots if r.is_group]
	orphans = [r for r in roots if not r.is_group]
	phases = []
	active_found = False
	for i, g in enumerate(groups, start=1):
		ls = leaves(g)
		done = all(l.status == "Completed" for l in ls) if ls else g.status == "Completed"
		started = any(l.status in ("Working", "Pending Review", "Completed") for l in ls) or (
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
		gate = next((l for l in ls if l.is_milestone and l.portal_signoff_type), None)
		ph = {
			"name": g.name,
			"no": i,
			"label": g.subject,
			"status": status,
			"start": g.exp_start_date,
			"end": g.exp_end_date,
			"actual_start": g.act_start_date,
			"actual_end": g.act_end_date,
			"progress": node_progress(g),
			"task_count": len(ls),
			"done_count": sum(1 for l in ls if l.status == "Completed"),
			"client_task_count": sum(1 for l in ls if l.type == "Klien"),
			"late_count": sum(1 for l in ls if _is_late(l)),
			"gate": {"name": gate.name, "subject": gate.subject, "kind": gate.portal_signoff_type, "due": gate.exp_end_date,
			         "status": gate.status} if gate else None,
		}
		if with_tree:
			ph["tree"] = g
		phases.append(ph)
	if orphans and with_tree:
		phases.append({"name": None, "no": len(groups) + 1, "label": "Lainnya", "status": "planned", "start": None, "end": None,
		               "progress": 0, "task_count": len(orphans), "done_count": sum(1 for o in orphans if o.status == "Completed"),
		               "client_task_count": 0, "late_count": 0, "gate": None, "tree": frappe._dict(children=orphans, is_group=1, name=None)})
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
