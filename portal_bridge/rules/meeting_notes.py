"""Aturan 12–15: notulen rapat, konfirmasi, deemed approved, versi."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.desk.form.assign_to import add as assign_add
from frappe.utils import now_datetime

from portal_bridge import notify
from portal_bridge.evidence import make_evidence
from portal_bridge.rules.progress import active_phase
from portal_bridge.setup.install import CLIENT_TASK_TYPE
from portal_bridge.utils import (
	add_working_days,
	audit,
	can_approve,
	elevated,
	internal_pics,
	is_client,
	portal_url,
	project_company,
	project_users,
	settings,
	today,
)


def _link(doc):
	return f"{portal_url()}/{doc.project}/meeting-notes/{doc.name}"


def rule_12_on_sent(doc):
	"""Sent: confirm_due = +N hari kerja (Holiday List company); baris actions tanpa task → Task standar + assign."""
	doc.sent_on = now_datetime()
	doc.confirm_due = add_working_days(today(), int(settings().meeting_note_confirm_days or 2), project_company(doc.project))
	phase = active_phase(doc.project)
	for row in doc.actions:
		if row.task or not row.body:
			continue
		task_type = CLIENT_TASK_TYPE if (row.assignee and is_client(row.assignee)) else None
		with elevated():
			row.task = _make_action_task(doc, row, task_type, phase)
	ctx = {"seq": doc.seq, "title": doc.title, "confirm_due": frappe.utils.formatdate(doc.confirm_due), "link": _link(doc)}
	for u in project_users(doc.project, side="klien"):
		notify.enqueue(u["user"], "meeting_note_sent", ctx, doc.project, dedupe_key=f"mn-sent:{doc.name}:{doc.version}:{u['user']}")
	audit("meeting_note_sent", doc.project, doc.doctype, doc.name)


def _make_action_task(doc, row, task_type, phase) -> str:
	task = frappe.get_doc(
		{
			"doctype": "Task",
			"subject": row.body[:140],
			"project": doc.project,
			"type": task_type,
			"exp_start_date": today(),
			"exp_end_date": row.due,
			"parent_task": phase["name"] if phase else None,
			"portal_visible": 1,
			"description": f"Tindak lanjut notulen #{doc.seq} {doc.title}",
		}
	)
	task.insert(ignore_permissions=True)
	if row.assignee:
		assign_add(
			{"assign_to": [row.assignee], "doctype": "Task", "name": task.name, "date": row.due, "description": task.subject},
			ignore_permissions=True,
		)
	return task.name


def rule_13_confirm(name: str, full_name: str, attest) -> dict:
	"""Klien dengan portal_can_approve mengonfirmasi: status Confirmed, PDF + hash, audit dengan IP."""
	doc = frappe.get_doc("Portal Meeting Note", name)
	_check_can_decide(doc)
	if not frappe.utils.cint(attest) and not (isinstance(attest, str) and attest.lower() in ("true", "1")):
		frappe.throw(_("Centang pernyataan konfirmasi terlebih dahulu."), frappe.ValidationError)
	_check_name(full_name)
	if doc.status not in ("Sent", "Correction requested"):
		frappe.throw(_("Notulen ini tidak dalam status menunggu konfirmasi."))
	doc.status = "Confirmed"
	doc.confirmed_by = frappe.session.user
	doc.confirmed_on = now_datetime()
	doc.flags.portal_decision = True
	doc.save(ignore_permissions=True)
	file_url, sha = make_evidence(doc, "Portal Meeting Note", version=doc.version, decided_by=frappe.session.user)
	doc.db_set({"pdf_file": file_url, "pdf_hash": sha}, update_modified=False)
	audit("meeting_note_confirm", doc.project, doc.doctype, doc.name, {"full_name": full_name, "sha256": sha, "version": doc.version})
	_notify_pics(doc, "Confirmed")
	return {"status": doc.status, "pdf_file": file_url, "pdf_hash": sha}


def rule_13_request_correction(name: str, comment: str) -> dict:
	doc = frappe.get_doc("Portal Meeting Note", name)
	_check_can_decide(doc)
	if not (comment or "").strip():
		frappe.throw(_("Tulis koreksi yang diminta."), frappe.ValidationError)
	if doc.status != "Sent":
		frappe.throw(_("Notulen ini tidak dalam status menunggu konfirmasi."))
	from portal_bridge.rules.comments import rule_16_add

	rule_16_add(doc.project, doc.doctype, doc.name, "all", comment)
	doc.status = "Correction requested"
	doc.flags.portal_decision = True
	doc.save(ignore_permissions=True)
	audit("meeting_note_correction", doc.project, doc.doctype, doc.name, {"comment": comment[:200]})
	_notify_pics(doc, "Correction requested")
	return {"status": doc.status}


def rule_14_close_overdue(on=None) -> int:
	"""Lewat confirm_due tanpa aksi → Deemed approved + audit."""
	on = on or today()
	names = frappe.get_all("Portal Meeting Note", filters={"status": "Sent", "confirm_due": ["<", on]}, pluck="name")
	for n in names:
		doc = frappe.get_doc("Portal Meeting Note", n)
		doc.status = "Deemed approved"
		doc.flags.portal_decision = True
		doc.save(ignore_permissions=True)
		audit("meeting_note_deemed", doc.project, doc.doctype, doc.name, {"confirm_due": str(doc.confirm_due)}, user="Administrator")
		ctx = {"seq": doc.seq, "title": doc.title, "link": _link(doc)}
		for u in project_users(doc.project):
			notify.enqueue(u["user"], "meeting_note_deemed", ctx, doc.project, dedupe_key=f"mn-deemed:{doc.name}:{u['user']}")
	return len(names)


def rule_15_on_edit_after_sent(doc, before):
	"""Edit setelah Sent → version naik, notif 'diperbarui ke v{n}'. Perubahan status oleh portal tidak dihitung."""
	if doc.flags.portal_decision or before.status == "Draft":
		return
	tracked = ("title", "meeting_date", "start_time", "end_time", "location")
	changed = any(doc.get(f) != before.get(f) for f in tracked)
	for table in ("attendees", "topics", "decisions", "actions"):
		a = [r.as_dict(no_default_fields=True, no_child_table_fields=True) for r in doc.get(table)]
		b = [r.as_dict(no_default_fields=True, no_child_table_fields=True) for r in before.get(table)]
		for row in a + b:
			for k in ("name", "parent", "parentfield", "parenttype", "idx", "modified", "creation", "owner", "modified_by", "docstatus"):
				row.pop(k, None)
		if a != b:
			changed = True
	if not changed:
		return
	doc.version = (before.version or 1) + 1
	if doc.status in ("Confirmed", "Deemed approved", "Correction requested"):
		# konfirmasi lama tidak berlaku untuk versi baru
		doc.status = "Sent"
		doc.confirmed_by = None
		doc.confirmed_on = None
		doc.pdf_file = None
		doc.pdf_hash = None
		doc.confirm_due = add_working_days(today(), int(settings().meeting_note_confirm_days or 2), project_company(doc.project))
	ctx = {"seq": doc.seq, "title": doc.title, "version": doc.version, "link": _link(doc)}
	for u in project_users(doc.project, side="klien"):
		notify.enqueue(u["user"], "meeting_note_updated", ctx, doc.project, dedupe_key=f"mn-upd:{doc.name}:{doc.version}:{u['user']}")


def _check_can_decide(doc):
	user = frappe.session.user
	if is_client(user):
		if not can_approve(doc.project, user):
			frappe.throw(_("Hanya user yang berwenang yang bisa mengonfirmasi notulen."), frappe.PermissionError)
	elif not frappe.has_permission("Portal Meeting Note", "write", doc, user=user):
		frappe.throw(_("Tidak berwenang."), frappe.PermissionError)


def _check_name(full_name: str):
	expected = (frappe.utils.get_fullname(frappe.session.user) or "").strip().lower()
	if not full_name or full_name.strip().lower() != expected:
		frappe.throw(_("Nama lengkap harus sama dengan nama Anda yang terdaftar ({0}).").format(expected.title()),
		             frappe.ValidationError)


def _notify_pics(doc, status):
	ctx = {
		"seq": doc.seq, "title": doc.title, "status": status, "status_text": notify.STATUS_TEXT.get(status, status),
		"actor": frappe.utils.get_fullname(frappe.session.user), "link": frappe.utils.get_url_to_form(doc.doctype, doc.name),
	}
	for pic in internal_pics(doc.project):
		notify.enqueue(pic, "meeting_note_decided", ctx, doc.project)
