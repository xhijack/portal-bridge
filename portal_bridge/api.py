"""API portal (endpoint gabungan) di /api/method/portal_bridge.api.*

Semua endpoint memanggil require_access(project) dan menulis audit untuk aksi tulis.
Frontend tetap boleh memakai /api/resource/<DocType> langsung; permission hook membatasinya.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate
from frappe.utils.file_manager import save_file

from portal_bridge.rules import billing as billing_rules
from portal_bridge.rules import client_tasks as ct
from portal_bridge.rules import comments as cm
from portal_bridge.rules import meeting_notes as mn
from portal_bridge.rules import progress as pr
from portal_bridge.rules import signoff as so
from portal_bridge.rules.weekly import rule_11_new_version
from portal_bridge.setup.install import CLIENT_TASK_TYPE
from portal_bridge.utils import (
	assigned_users,
	audit,
	can_approve,
	is_client,
	is_internal,
	project_users,
	require_access,
	require_login,
	settings,
	today,
	user_display,
)

ALLOWED_UPLOAD_EXT = {"csv", "xlsx", "xls", "pdf", "jpg", "jpeg", "png", "docx"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


# ---------------------------------------------------------------- helpers


def _project_header(project: str) -> dict:
	p = frappe.db.get_value(
		"Project",
		project,
		["name", "project_name", "customer", "status", "expected_start_date", "expected_end_date", "percent_complete",
		 "company", "portal_budget_man_days"],
		as_dict=True,
	)
	customer_name = frappe.db.get_value("Customer", p.customer, "customer_name") if p.customer else None
	internal = project_users(project, side="internal")
	go_live = frappe.db.get_value(
		"Task", {"project": project, "is_milestone": 1, "portal_signoff_type": "Go-live", "status": ["!=", "Cancelled"]}, "exp_end_date"
	) or p.expected_end_date
	return {
		"project": p.name,
		"name": p.project_name,
		"customer": p.customer,
		"customer_name": customer_name,
		"status": p.status,
		"start": p.expected_start_date,
		"end": p.expected_end_date,
		"go_live": go_live,
		"pic": user_display(internal[0]["user"]) if internal else None,
		"company": p.company,
		"budget_man_days": flt(p.portal_budget_man_days) or None,
	}


def _wbs_map(project: str) -> dict[str, str]:
	"""Nomor WBS rekursif: fase 1 → 1.1 → 1.1.1 …"""
	roots, _ = pr.load_tree(project)
	m: dict[str, str] = {}

	def walk(nodes, prefix):
		n = 0
		for node in nodes:
			n += 1
			code = f"{prefix}.{n}" if prefix else str(n)
			m[node.name] = code
			walk(node.get("children", []), code)

	groups = [r for r in roots if r.is_group]
	walk(groups, "")
	return m


def _task_row(t, wbs_map: dict[str, str], with_blocks=True, depth=0) -> dict:
	users = assigned_users(t.get("_assign"))
	assignee = user_display(users[0]) if users else None
	is_group = bool(t.get("is_group"))
	blocks = ct.rule_7_blocks(t.name, wbs_map) if (with_blocks and not is_group) else []
	kids = t.get("children", []) if is_group else []
	return {
		"name": t.name,
		"wbs": wbs_map.get(t.name),
		"subject": t.subject,
		"type": t.type,
		"is_client": t.type == CLIENT_TASK_TYPE,
		"is_group": is_group,
		"depth": depth,
		"parent": t.get("parent_task"),
		"children_count": len(kids),
		"leaf_count": len(pr.leaves(t)) if is_group else 0,
		"done_count": sum(1 for l in pr.leaves(t) if l.status == "Completed") if is_group else 0,
		"assignee": assignee,
		"assignees": [user_display(u) for u in users],
		"start": t.exp_start_date,
		"due": t.exp_end_date,
		"actual_start": t.get("act_start_date"),
		"actual_end": t.get("act_end_date") or (t.get("completed_on") if t.status == "Completed" else None),
		"status": ("done" if (is_group and kids and all(l.status == "Completed" for l in pr.leaves(t))) else ("working" if is_group else ct.status_key(t))),
		"raw_status": t.status,
		"progress": cint(pr.node_progress(t)) if is_group else (100 if t.status == "Completed" else cint(t.progress)),
		"late_days": 0 if is_group else ct.late_days(t),
		"blocks": blocks,
		"blocks_text": ct.blocks_text(blocks),
		"is_milestone": bool(t.is_milestone),
		"signoff_type": t.portal_signoff_type,
		"phase": t.get("parent_task"),
		"completed_on": t.completed_on,
		"priority": t.priority,
	}


def _flatten(node, wbs, depth, out):
	for k in node.get("children", []):
		out.append(_task_row(k, wbs, depth=depth))
		if k.get("is_group"):
			_flatten(k, wbs, depth + 1, out)


def _client_tasks(project: str, only_user: str | None = None, status: str | None = None) -> list[dict]:
	filters = {"project": project, "portal_visible": 1, "type": CLIENT_TASK_TYPE, "is_group": 0, "status": ["!=", "Cancelled"]}
	if status == "open":
		filters["status"] = ["not in", ["Completed", "Cancelled"]]
	elif status == "done":
		filters["status"] = "Completed"
	rows = frappe.get_all("Task", filters=filters, fields=pr.TASK_FIELDS + ["project"], order_by="exp_end_date asc, idx asc")
	wbs = _wbs_map(project)
	out = []
	for t in rows:
		row = _task_row(t, wbs)
		row["mine"] = bool(only_user and only_user in assigned_users(t._assign))
		row["phase_label"] = frappe.db.get_value("Task", t.parent_task, "subject") if t.parent_task else None
		if status == "late" and row["status"] != "late":
			continue
		out.append(row)
	# terlambat dulu, lalu tenggat terdekat
	out.sort(key=lambda r: (0 if r["status"] == "late" else 1, str(r["due"] or "9999")))
	return out


# ---------------------------------------------------------------- summary & plan


@frappe.whitelist()
def summary(project: str):
	project = require_access(project)
	user = frappe.session.user
	header = _project_header(project)
	phases = pr.rule_3_phases(project)
	health = pr.rule_4_health(project)
	client_tasks_all = _client_tasks(project, only_user=user)
	mine = [t for t in client_tasks_all if t["mine"]] if is_client(user) else client_tasks_all
	open_mine = [t for t in mine if t["status"] != "done"]
	open_all = [t for t in client_tasks_all if t["status"] != "done"]

	pending_signoffs = frappe.get_all(
		"Portal Signoff",
		filters={"project": project, "status": "Pending", "docstatus": 0},
		fields=["name", "kind", "title", "doc_version", "requested_on", "due", "impact_man_days", "impact_timeline_days", "summary"],
		order_by="due asc",
	)
	weekly = frappe.get_all(
		"Portal Weekly Update",
		filters={"project": project, "status": "Approved"},
		fields=["name", "week_no", "period_start", "period_end", "health", "approved_by", "approved_on", "done", "in_progress", "risks", "version"],
		order_by="week_no desc, version desc",
		limit=1,
	)
	weekly_latest = None
	if weekly:
		w = weekly[0]
		w["approved_by"] = user_display(w.approved_by)
		w["client_actions"] = frappe.get_all(
			"Portal Weekly Client Action", filters={"parent": w.name}, fields=["text", "task"], order_by="idx asc"
		)
		weekly_latest = w

	notes = frappe.get_all(
		"Portal Meeting Note",
		filters={"project": project, "status": ["!=", "Draft"]},
		fields=["name", "seq", "title", "meeting_date", "status", "confirm_due", "version"],
		order_by="meeting_date desc, seq desc",
		limit=12,
	)
	decisions = []
	for n in notes:
		for d in frappe.get_all(
			"Portal Meeting Decision",
			filters={"parent": n.name},
			fields=["code", "kind", "body", "proposed_by", "approved_by", "status", "review_date"],
			order_by="idx asc",
		):
			d["meeting_note"] = n.name
			d["seq"] = n.seq
			d["date"] = n.meeting_date
			decisions.append(d)
		if len(decisions) >= 5:
			break
	pending_note = next((n for n in notes if n.status in ("Sent", "Correction requested")), None)
	if pending_note:
		pending_note["open_comments"] = cm.open_counts("Portal Meeting Note", pending_note.name).get("__open__", 0)

	timesheet_hours = frappe.db.sql(
		"""select coalesce(sum(total_hours),0) from `tabTimesheet` where parent_project=%s and docstatus=1""", project
	)[0][0]
	man_days_used = round(flt(timesheet_hours) / 8, 1)

	return {
		"project": header,
		"health": health,
		"phases": phases,
		"active_phase": next((p for p in phases if p["status"] == "active"), None),
		"progress": pr.rule_1_progress(project),
		"kpi": {
			"progress": pr.rule_1_progress(project),
			"client_tasks_open": len(open_mine),
			"client_tasks_total": len(mine),
			"client_tasks_late": sum(1 for t in open_mine if t["status"] == "late"),
			"approvals_pending": len(pending_signoffs),
			"man_days_used": man_days_used,
			"man_days_budget": header["budget_man_days"],
		},
		"client_tasks": open_mine[:5],
		"approvals": [
			{**s, "requested_by": None} for s in pending_signoffs[:3]
		],
		"weekly_latest": weekly_latest,
		"billing": billing_rules.rule_25_terms(project),
		"decisions_risks": decisions[:5],
		"meeting_note_pending": pending_note,
		"counts": {
			"tasks": len(open_mine),
			"notulen": 1 if pending_note else 0,
			"persetujuan": len(pending_signoffs) if (not is_client(user) or can_approve(project, user)) else 0,
		},
		"can_approve": can_approve(project, user) if is_client(user) else True,
		"all_client_tasks_open": len(open_all),
	}


@frappe.whitelist()
def plan(project: str, filter: str | None = None, q: str | None = None):
	project = require_access(project)
	header = _project_header(project)
	phases = pr.rule_3_phases(project, with_tree=True)
	wbs = _wbs_map(project)
	q = (q or "").strip().lower()
	all_rows = []
	out_phases = []
	for ph in phases:
		rows: list = []
		_flatten(ph.pop("tree"), wbs, 0, rows)
		all_rows += [r for r in rows if not r["is_group"]]
		out_phases.append({**ph, "tasks": rows})
	counts = {
		"all": len(all_rows),
		"client": sum(1 for r in all_rows if r["is_client"]),
		"late": sum(1 for r in all_rows if r["status"] == "late"),
		"gate": sum(1 for r in all_rows if r["is_milestone"] and r["signoff_type"]),
	}

	def keep(r):
		if r["is_group"]:
			return True  # grup disaring di bawah bila tak punya anak yang lolos
		if filter == "client" and not r["is_client"]:
			return False
		if filter == "late" and r["status"] != "late":
			return False
		if filter == "gate" and not (r["is_milestone"] and r["signoff_type"]):
			return False
		if q and q not in (r["subject"] or "").lower() and q not in ((r["assignee"] or {}).get("full_name") or "").lower():
			return False
		return True

	if filter or q:
		for ph in out_phases:
			kept = [r for r in ph["tasks"] if keep(r)]
			# buang grup yang tidak punya daun lolos di bawahnya
			leaf_parents = set()
			by_name = {r["name"]: r for r in ph["tasks"]}
			for r in kept:
				if r["is_group"]:
					continue
				p_ = r["parent"]
				while p_ and p_ in by_name:
					leaf_parents.add(p_)
					p_ = by_name[p_]["parent"]
			ph["tasks"] = [r for r in kept if not r["is_group"] or r["name"] in leaf_parents]
	start = getdate(header["start"]) if header["start"] else None
	end = getdate(header["end"]) if header["end"] else None
	return {
		"project": header,
		"health": pr.rule_4_health(project),
		"progress": pr.rule_1_progress(project),
		"baseline": header["start"],
		"total_days": (end - start).days if start and end else None,
		"day_of": (today() - start).days if start else None,
		"today": today(),
		"counts": counts,
		"phases": out_phases,
	}


# ---------------------------------------------------------------- tugas klien


@frappe.whitelist()
def my_tasks(project: str, status: str | None = None):
	project = require_access(project)
	user = frappe.session.user
	rows = _client_tasks(project, only_user=user, status=status)
	return {"tasks": rows, "counts": {
		"open": sum(1 for r in rows if r["status"] != "done"),
		"late": sum(1 for r in rows if r["status"] == "late"),
		"done": sum(1 for r in rows if r["status"] == "done"),
	}}


@frappe.whitelist()
def task(name: str):
	t = frappe.db.get_value("Task", name, pr.TASK_FIELDS + ["project", "description"], as_dict=True)
	if not t:
		frappe.throw(_("Tugas tidak ditemukan."), frappe.DoesNotExistError)
	project = require_access(t.project)
	if not t.portal_visible and not t.is_group:
		frappe.throw(_("Tugas tidak ditemukan."), frappe.DoesNotExistError)
	wbs = _wbs_map(project)
	if t.is_group:
		roots, by_name = pr.load_tree(project)
		node = by_name.get(name)
		if node is not None:
			t = node
	row = _task_row(t, wbs)
	row["children"] = [_task_row(k, wbs, with_blocks=False, depth=1) for k in t.get("children", [])] if t.is_group else []
	row["description"] = t.description
	row["mine"] = frappe.session.user in assigned_users(t._assign)
	row["phase_label"] = frappe.db.get_value("Task", t.parent_task, "subject") if t.parent_task else None
	row["files"] = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Task", "attached_to_name": name, "portal_visible": 1},
		fields=["name", "file_name", "file_url", "file_size", "creation", "owner"],
		order_by="creation desc",
	)
	row["comments"] = cm.threads("Task", name)
	audit("task_view", project, "Task", name)
	return row


@frappe.whitelist(methods=["POST"])
def complete_task(name: str, note: str | None = None):
	t = frappe.db.get_value("Task", name, ["project"], as_dict=True)
	if not t:
		frappe.throw(_("Tugas tidak ditemukan."), frappe.DoesNotExistError)
	require_access(t.project)
	doc = ct.rule_6_complete(name, note)
	return {"ok": True, "status": "done", "completed_on": doc.completed_on}


@frappe.whitelist(methods=["POST"])
def upload_task_file(task: str):
	"""Unggah file ke Task (multipart field 'file'); is_private=1, portal_visible=1; tipe & ukuran dibatasi."""
	t = frappe.db.get_value("Task", task, ["project", "type", "portal_visible", "_assign"], as_dict=True)
	if not t or not t.portal_visible:
		frappe.throw(_("Tugas tidak ditemukan."), frappe.DoesNotExistError)
	require_access(t.project)
	user = frappe.session.user
	if is_client(user) and user not in assigned_users(t._assign):
		frappe.throw(_("Hanya PIC tugas yang bisa mengunggah."), frappe.PermissionError)
	files = frappe.request.files if getattr(frappe, "request", None) else None
	if not files or "file" not in files:
		frappe.throw(_("Tidak ada file."), frappe.ValidationError)
	f = files["file"]
	filename = frappe.utils.sanitize_html(f.filename or "file")
	ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
	if ext not in ALLOWED_UPLOAD_EXT:
		frappe.throw(_("Tipe file tidak diizinkan. Gunakan: {0}.").format(", ".join(sorted(ALLOWED_UPLOAD_EXT))), frappe.ValidationError)
	content = f.stream.read()
	if len(content) > MAX_UPLOAD_BYTES:
		frappe.throw(_("Ukuran file maksimal 25 MB."), frappe.ValidationError)
	file_doc = save_file(filename, content, "Task", task, is_private=1)
	file_doc.db_set("portal_visible", 1, update_modified=False)
	audit("task_upload", t.project, "Task", task, {"file": file_doc.name, "size": len(content)})
	return {"ok": True, "file": {"name": file_doc.name, "file_name": file_doc.file_name, "file_url": file_doc.file_url,
	                               "file_size": file_doc.file_size}}


# ---------------------------------------------------------------- weekly


@frappe.whitelist()
def weekly_updates(project: str):
	project = require_access(project)
	rows = frappe.get_all(
		"Portal Weekly Update",
		filters={"project": project, "status": "Approved"},
		fields=["name", "week_no", "period_start", "period_end", "health", "approved_by", "approved_on", "version"],
		order_by="week_no desc, version desc",
	)
	for r in rows:
		r["approved_by"] = user_display(r.approved_by)
	return rows


@frappe.whitelist()
def weekly_update(name: str):
	doc = frappe.get_doc("Portal Weekly Update", name)
	require_access(doc.project)
	if doc.status != "Approved" and is_client():
		frappe.throw(_("Tidak ditemukan."), frappe.DoesNotExistError)
	return {
		"name": doc.name, "week_no": doc.week_no, "period_start": doc.period_start, "period_end": doc.period_end,
		"health": doc.health, "done": doc.done, "in_progress": doc.in_progress, "risks": doc.risks,
		"client_actions": [{"text": r.text, "task": r.task} for r in doc.client_actions],
		"approved_by": user_display(doc.approved_by), "approved_on": doc.approved_on, "version": doc.version,
	}


@frappe.whitelist(methods=["POST"])
def weekly_new_version(name: str):
	if not is_internal():
		frappe.throw(_("Hanya tim internal."), frappe.PermissionError)
	return {"name": rule_11_new_version(name)}


# ---------------------------------------------------------------- notulen


def _note_row(n) -> dict:
	counts = cm.open_counts("Portal Meeting Note", n.name)
	return {
		"name": n.name, "seq": n.seq, "title": n.title, "date": n.meeting_date, "status": n.status,
		"confirm_due": n.confirm_due, "version": n.version, "open_comments": counts.get("__open__", 0),
	}


@frappe.whitelist()
def meeting_notes(project: str):
	project = require_access(project)
	filters = {"project": project}
	if is_client():
		filters["status"] = ["!=", "Draft"]
	rows = frappe.get_all(
		"Portal Meeting Note", filters=filters,
		fields=["name", "seq", "title", "meeting_date", "status", "confirm_due", "version"],
		order_by="seq desc",
	)
	return [_note_row(n) for n in rows]


@frappe.whitelist()
def meeting_note(name: str):
	doc = frappe.get_doc("Portal Meeting Note", name)
	project = require_access(doc.project)
	if doc.status == "Draft" and is_client():
		frappe.throw(_("Tidak ditemukan."), frappe.DoesNotExistError)
	siblings = frappe.get_all(
		"Portal Meeting Note", filters={"project": project, "status": ["!=", "Draft"]}, fields=["name", "seq", "title"], order_by="seq asc"
	)
	idx = next((i for i, s in enumerate(siblings) if s.name == name), None)
	counts = cm.open_counts("Portal Meeting Note", name)
	audit("meeting_note_view", project, doc.doctype, name)
	return {
		**_note_row(doc),
		"start_time": doc.start_time, "end_time": doc.end_time, "location": doc.location,
		"sent_on": doc.sent_on, "confirmed_by": user_display(doc.confirmed_by) if doc.confirmed_by else None,
		"confirmed_on": doc.confirmed_on, "pdf_file": doc.pdf_file, "pdf_hash": doc.pdf_hash,
		"attendees": [{"user": a.user, "full_name": a.full_name or (frappe.utils.get_fullname(a.user) if a.user else None),
		               "role": a.role, "side": a.side, "present": bool(a.present)} for a in doc.attendees],
		"topics": [{"seq": t.seq or i, "title": t.title, "body": t.body} for i, t in enumerate(doc.topics, start=1)],
		"decisions": [{"code": d.code, "kind": d.kind, "body": d.body, "proposed_by": d.proposed_by, "approved_by": d.approved_by,
		               "status": d.status, "review_date": d.review_date} for d in doc.decisions],
		"actions": [{"body": a.body, "assignee": user_display(a.assignee) if a.assignee else None, "due": a.due, "task": a.task,
		             "task_status": ct.status_key(frappe.db.get_value("Task", a.task, ["status", "exp_end_date", "is_milestone", "portal_signoff_type", "project"], as_dict=True)) if a.task else None}
		            for a in doc.actions],
		"attachments": frappe.get_all("File", filters={"attached_to_doctype": doc.doctype, "attached_to_name": name, "portal_visible": 1},
		                              fields=["name", "file_name", "file_url", "file_size"], order_by="creation asc"),
		"comment_counts": counts,
		"comments": cm.threads("Portal Meeting Note", name),
		"prev": siblings[idx - 1] if idx else None,
		"next": siblings[idx + 1] if idx is not None and idx + 1 < len(siblings) else None,
		"all": siblings,
		"can_decide": can_approve(project) if is_client() else False,
		"mentionables": project_users(project),
	}


@frappe.whitelist(methods=["POST"])
def confirm_meeting_note(name: str, full_name: str, attest=None):
	doc_project = frappe.db.get_value("Portal Meeting Note", name, "project")
	require_access(doc_project)
	return mn.rule_13_confirm(name, full_name, attest)


@frappe.whitelist(methods=["POST"])
def request_meeting_note_correction(name: str, comment: str):
	doc_project = frappe.db.get_value("Portal Meeting Note", name, "project")
	require_access(doc_project)
	return mn.rule_13_request_correction(name, comment)


# ---------------------------------------------------------------- komentar


@frappe.whitelist()
def comments(ref_doctype: str, ref_name: str):
	project = frappe.db.get_value(ref_doctype, ref_name, "project")
	require_access(project)
	return {"threads": cm.threads(ref_doctype, ref_name), "counts": cm.open_counts(ref_doctype, ref_name)}


@frappe.whitelist(methods=["POST"])
def add_comment(project: str, ref_doctype: str, ref_name: str, body: str, section: str = "all", parent: str | None = None,
                mentions=None):
	project = require_access(project)
	if isinstance(mentions, str):
		mentions = frappe.parse_json(mentions) if mentions else []
	doc = cm.rule_16_add(project, ref_doctype, ref_name, section, body, parent, mentions)
	return cm.serialize(doc)


@frappe.whitelist(methods=["POST"])
def resolve_comment(name: str):
	project = frappe.db.get_value("Portal Comment", name, "project")
	require_access(project)
	return cm.serialize(cm.rule_18_resolve(name))


@frappe.whitelist(methods=["POST"])
def edit_comment(name: str, body: str):
	project = frappe.db.get_value("Portal Comment", name, "project")
	require_access(project)
	return cm.serialize(cm.rule_19_edit(name, body))


@frappe.whitelist(methods=["POST"])
def delete_comment(name: str):
	project = frappe.db.get_value("Portal Comment", name, "project")
	require_access(project)
	cm.rule_19_delete(name)
	return {"ok": True}


# ---------------------------------------------------------------- sign-off


@frappe.whitelist()
def signoffs(project: str):
	project = require_access(project)
	rows = frappe.get_all(
		"Portal Signoff", filters={"project": project, "docstatus": ["!=", 2]}, pluck="name",
		order_by="requested_on desc",
	)
	out = [so.serialize(frappe.get_doc("Portal Signoff", n)) for n in rows]
	out.sort(key=lambda r: (0 if r["status"] == "Pending" else 1, str(r["due"] or "")))
	return {"signoffs": out, "can_decide": can_approve(project) if is_client() else False}


@frappe.whitelist()
def signoff(name: str):
	doc = frappe.get_doc("Portal Signoff", name)
	project = require_access(doc.project)
	audit("signoff_view", project, doc.doctype, name)
	d = so.serialize(doc, detail=True)
	d["comments"] = cm.threads("Portal Signoff", name)
	d["can_decide"] = (can_approve(project) if is_client() else False) and doc.status == "Pending" and doc.docstatus == 0
	d["approvers"] = [u["full_name"] for u in project_users(project, side="klien") if u["can_approve"]]
	d["customer_name"] = frappe.db.get_value("Customer", frappe.db.get_value("Project", project, "customer"), "customer_name")
	d["contact_name"] = frappe.utils.get_fullname(frappe.session.user)
	return d


@frappe.whitelist(methods=["POST"])
def decide_signoff(name: str, decision: str, full_name: str | None = None, attest=None, note: str | None = None):
	project = frappe.db.get_value("Portal Signoff", name, "project")
	require_access(project)
	return so.rule_21_decide(name, decision, full_name, attest, note)


# ---------------------------------------------------------------- dokumen, tagihan, profil


@frappe.whitelist()
def documents(project: str):
	project = require_access(project)
	groups = {"brd": [], "notulen": [], "signoff": [], "training": [], "lampiran": []}
	tasks = frappe.get_all("Task", filters={"project": project, "portal_visible": 1}, fields=["name", "subject"])
	task_names = {t.name: t.subject for t in tasks}
	notes = {n.name: n for n in frappe.get_all("Portal Meeting Note", filters={"project": project, "status": ["!=", "Draft"]},
	                                           fields=["name", "title", "seq", "version"])}
	signoffs_ = {s.name: s for s in frappe.get_all("Portal Signoff", filters={"project": project, "docstatus": ["!=", 2]},
	                                               fields=["name", "title", "kind", "doc_version"])}
	files = frappe.get_all(
		"File",
		filters={"portal_visible": 1, "attached_to_doctype": ["in", ["Project", "Task", "Portal Meeting Note", "Portal Signoff"]]},
		fields=["name", "file_name", "file_url", "file_size", "creation", "owner", "attached_to_doctype", "attached_to_name"],
		order_by="creation desc",
	)
	for f in files:
		dt, dn = f.attached_to_doctype, f.attached_to_name
		row = {"name": f.name, "file_name": f.file_name, "file_url": f.file_url, "size": f.file_size, "date": f.creation,
		       "by": user_display(f.owner), "ref_doctype": dt, "ref_name": dn, "version": None, "title": None}
		if dt == "Project" and dn == project:
			row["title"] = "Lampiran proyek"
			groups["lampiran"].append(row)
		elif dt == "Task" and dn in task_names:
			row["title"] = task_names[dn]
			groups["lampiran"].append(row)
		elif dt == "Portal Meeting Note" and dn in notes:
			row["title"] = f"Notulen #{notes[dn].seq} {notes[dn].title}"
			row["version"] = notes[dn].version
			groups["notulen"].append(row)
		elif dt == "Portal Signoff" and dn in signoffs_:
			s = signoffs_[dn]
			row["title"] = s.title
			row["version"] = s.doc_version
			key = "brd" if s.kind == "BRD" else ("training" if s.kind == "Training" else "signoff")
			groups[key].append(row)
	audit("documents_view", project)
	return groups


@frappe.whitelist()
def billing(project: str):
	project = require_access(project)
	audit("billing_view", project)
	data = billing_rules.rule_25_terms(project)
	data["currency"] = data["currency"] or "IDR"
	return data


@frappe.whitelist(methods=["POST"])
def set_notif_pref(pref):
	require_login()
	if isinstance(pref, str):
		pref = frappe.parse_json(pref)
	if not isinstance(pref, dict):
		frappe.throw(_("Format preferensi salah."), frappe.ValidationError)
	from portal_bridge.notify import MANDATORY

	clean = {k: bool(v) for k, v in pref.items() if k not in MANDATORY}
	frappe.db.set_value("User", frappe.session.user, "portal_notif_pref", json.dumps(clean), update_modified=False)
	audit("notif_pref", ref_doctype="User", ref_name=frappe.session.user, detail=clean)
	return clean


@frappe.whitelist()
def projects():
	"""Daftar project yang bisa diakses + hitungan ringkas untuk pemilih proyek."""
	require_login()
	from portal_bridge.auth import me as _me

	return _me()["projects"]
