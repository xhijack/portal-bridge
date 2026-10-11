"""Lapis isolasi di sisi Frappe untuk role Portal Klien.

permission_query_conditions membatasi daftar (frappe.get_list / /api/resource),
has_permission membatasi dokumen tunggal (frappe.get_doc / /api/resource/<name>).
Untuk user internal, hook mengembalikan None sehingga aturan bawaan Frappe yang berlaku.
"""

from __future__ import annotations

import frappe

from portal_bridge.utils import client_projects, is_client

DENY = "1=0"


def _in_clause(values: list[str]) -> str:
	return ", ".join(frappe.db.escape(v) for v in values)


def _projects_or_none(user: str):
	if not is_client(user):
		return None
	return client_projects(user)


# ------------------------------------------------------------- query conditions


def project_query(user):
	projects = _projects_or_none(user)
	if projects is None:
		return None
	if not projects:
		return DENY
	return f"`tabProject`.`name` in ({_in_clause(projects)}) and `tabProject`.`portal_enabled` = 1"


def task_query(user):
	projects = _projects_or_none(user)
	if projects is None:
		return None
	if not projects:
		return DENY
	return (
		f"`tabTask`.`project` in ({_in_clause(projects)}) and `tabTask`.`portal_visible` = 1 "
		"and `tabTask`.`status` != 'Cancelled'"
	)


def file_query(user):
	projects = _projects_or_none(user)
	if projects is None:
		return None
	if not projects:
		return DENY
	# File portal_visible yang menempel pada Project / Task / dokumen portal milik project yang diizinkan
	tasks = frappe.get_all(
		"Task", filters={"project": ["in", projects], "portal_visible": 1}, pluck="name"
	)
	portal_docs = []
	for dt in ("Portal Meeting Note", "Portal Signoff", "Portal Weekly Update"):
		portal_docs += [(dt, n) for n in frappe.get_all(dt, filters={"project": ["in", projects]}, pluck="name")]
	clauses = [f"(`tabFile`.`attached_to_doctype` = 'Project' and `tabFile`.`attached_to_name` in ({_in_clause(projects)}))"]
	if tasks:
		clauses.append(f"(`tabFile`.`attached_to_doctype` = 'Task' and `tabFile`.`attached_to_name` in ({_in_clause(tasks)}))")
	for dt in ("Portal Meeting Note", "Portal Signoff", "Portal Weekly Update"):
		names = [n for d, n in portal_docs if d == dt]
		if names:
			clauses.append(
				f"(`tabFile`.`attached_to_doctype` = {frappe.db.escape(dt)} and `tabFile`.`attached_to_name` in ({_in_clause(names)}))"
			)
	return f"`tabFile`.`portal_visible` = 1 and ({' or '.join(clauses)})"


def _portal_doc_query(table: str, user, extra: str = ""):
	projects = _projects_or_none(user)
	if projects is None:
		return None
	if not projects:
		return DENY
	return f"`tab{table}`.`project` in ({_in_clause(projects)}){extra}"


def weekly_query(user):
	return _portal_doc_query("Portal Weekly Update", user, " and `tabPortal Weekly Update`.`status` = 'Approved'")


def meeting_note_query(user):
	return _portal_doc_query("Portal Meeting Note", user, " and `tabPortal Meeting Note`.`status` != 'Draft'")


def signoff_query(user):
	return _portal_doc_query("Portal Signoff", user)


def comment_query(user):
	return _portal_doc_query("Portal Comment", user)


def evidence_query(user):
	return _portal_doc_query("Portal Evidence", user)


def user_query(user):
	"""Klien hanya boleh melihat dirinya sendiri di daftar User."""
	if not is_client(user):
		return None
	return f"`tabUser`.`name` = {frappe.db.escape(user)}"


# ------------------------------------------------------------- has_permission


def _client_doc_check(doc, ptype, user, allowed: bool):
	"""Klien: True bila dokumen boleh dibaca; selain read selalu False (tulis hanya lewat API portal)."""
	if ptype not in ("read", "select", "print"):
		return False
	return bool(allowed)


def project_has_permission(doc, ptype, user):
	if not is_client(user):
		return None
	return _client_doc_check(doc, ptype, user, doc.name in client_projects(user) and bool(doc.get("portal_enabled")))


def task_has_permission(doc, ptype, user):
	if not is_client(user):
		return None
	ok = doc.project in client_projects(user) and bool(doc.get("portal_visible")) and doc.status != "Cancelled"
	return _client_doc_check(doc, ptype, user, ok)


def file_has_permission(doc, ptype, user):
	if not is_client(user):
		return None
	if not doc.get("portal_visible"):
		return False
	projects = client_projects(user)
	dt, dn = doc.attached_to_doctype, doc.attached_to_name
	ok = False
	if dt == "Project":
		ok = dn in projects
	elif dt == "Task":
		ok = bool(
			frappe.db.get_value("Task", {"name": dn, "project": ["in", projects], "portal_visible": 1}, "name")
		) if projects else False
	elif dt in ("Portal Meeting Note", "Portal Signoff", "Portal Weekly Update"):
		ok = bool(frappe.db.get_value(dt, {"name": dn, "project": ["in", projects]}, "name")) if projects else False
	return _client_doc_check(doc, ptype, user, ok)


def _portal_doc_has_permission(doc, ptype, user, extra_ok=True):
	if not is_client(user):
		return None
	ok = doc.project in client_projects(user) and extra_ok
	return _client_doc_check(doc, ptype, user, ok)


def weekly_has_permission(doc, ptype, user):
	return _portal_doc_has_permission(doc, ptype, user, doc.status == "Approved")


def meeting_note_has_permission(doc, ptype, user):
	return _portal_doc_has_permission(doc, ptype, user, doc.status != "Draft")


def signoff_has_permission(doc, ptype, user):
	return _portal_doc_has_permission(doc, ptype, user)


def comment_has_permission(doc, ptype, user):
	if not is_client(user):
		return None
	ok = doc.project in client_projects(user)
	if ptype in ("read", "select"):
		return ok
	# klien boleh tulis/hapus komentarnya sendiri (aturan 19 dicek di API)
	return ok and doc.author_user == user


def evidence_has_permission(doc, ptype, user):
	return _portal_doc_has_permission(doc, ptype, user)


def user_has_permission(doc, ptype, user):
	if not is_client(user):
		return None
	return doc.name == user and ptype in ("read", "select")
