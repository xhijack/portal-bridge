"""Aturan 20–24: sign-off digital dan Change Request."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, getdate, now_datetime

from portal_bridge import notify
from portal_bridge.evidence import make_evidence
from portal_bridge.rules.progress import active_phase
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
	user_display,
)

GATE_KINDS = ("UAT", "Go-live", "BAST", "BRD", "Training")


def _link(doc):
	return f"{portal_url()}/{doc.project}/approvals/{doc.name}"


def rule_20_on_create(doc):
	"""Dibuat PIC (Desk/MCP): requested_by/on, due default +N hari kerja; Pending tampil di portal."""
	if not doc.requested_by:
		doc.requested_by = frappe.session.user
	if not doc.requested_on:
		doc.requested_on = now_datetime()
	if not doc.due:
		doc.due = add_working_days(today(), int(settings().signoff_due_days or 5), project_company(doc.project))
	if doc.kind == "Change Request":
		rule_23_validate_cr(doc)


def notify_pending(doc):
	ctx = {"kind": doc.kind, "title": doc.title, "due": frappe.utils.formatdate(doc.due), "link": _link(doc)}
	for u in project_users(doc.project, side="klien"):
		if u["can_approve"]:
			notify.enqueue(u["user"], "signoff_pending", ctx, doc.project, dedupe_key=f"so-pending:{doc.name}:{u['user']}")
	frappe.cache.delete_value(f"portal_summary:{doc.project}")


def rule_21_decide(name: str, decision: str, full_name: str | None, attest, note: str | None = None) -> dict:
	"""Hanya portal_can_approve. Setujui: pernyataan + nama lengkap cocok (case-insensitive) → decided_*, PDF, hash, submit."""
	doc = frappe.get_doc("Portal Signoff", name)
	user = frappe.session.user
	if doc.docstatus != 0 or doc.status != "Pending":
		frappe.throw(_("Dokumen ini sudah diputuskan."), frappe.ValidationError)
	if is_client(user):
		if not can_approve(doc.project, user):
			frappe.throw(_("Hanya user yang berwenang yang bisa memutuskan sign-off."), frappe.PermissionError)
	elif not frappe.has_permission("Portal Signoff", "submit", doc, user=user):
		frappe.throw(_("Tidak berwenang."), frappe.PermissionError)
	decision = (decision or "").lower()
	if decision not in ("approve", "revision", "reject"):
		frappe.throw(_("Keputusan tidak dikenal."), frappe.ValidationError)

	if decision == "approve":
		attest_ok = attest in (True, 1, "1", "true", "True", "on")
		if not attest_ok:
			frappe.throw(_("Centang pernyataan kewenangan terlebih dahulu."), frappe.ValidationError)
		expected = (frappe.utils.get_fullname(user) or "").strip().lower()
		if not full_name or full_name.strip().lower() != expected:
			frappe.throw(_("Nama lengkap harus sama dengan nama Anda yang terdaftar."), frappe.ValidationError)
		doc.status = "Approved"
	elif decision == "revision":
		if not (note or "").strip():
			frappe.throw(_("Tulis revisi yang diminta."), frappe.ValidationError)
		doc.status = "Revision requested"
	else:
		if not (note or "").strip():
			frappe.throw(_("Tulis alasan penolakan."), frappe.ValidationError)
		doc.status = "Rejected"

	doc.decided_by_user = user
	doc.decided_name = full_name or frappe.utils.get_fullname(user)
	doc.decided_on = now_datetime()
	doc.decided_ip = getattr(frappe.local, "request_ip", None)
	doc.client_note = note
	doc.flags.portal_decision = True
	doc.save(ignore_permissions=True)

	result = {"status": doc.status}
	if doc.status == "Approved":
		file_url, sha = make_evidence(doc, "Portal Signoff", version=_version_no(doc), decided_by=user)
		doc.db_set({"pdf_file": file_url, "pdf_hash": sha}, update_modified=False)
		doc.reload()
		doc.flags.portal_decision = True
		with elevated():
			doc.submit()  # on_submit → rule_23/24 (buat Task, update Project, Sales Invoice Draft)
		result.update({"pdf_file": file_url, "pdf_hash": sha})
	audit(f"signoff_{decision}", doc.project, doc.doctype, doc.name, {"full_name": full_name, "note": note, **result})

	ctx = {"kind": doc.kind, "title": doc.title, "status": doc.status, "status_text": notify.STATUS_TEXT.get(doc.status, doc.status),
	       "actor": user_display(user)["full_name"], "link": frappe.utils.get_url_to_form(doc.doctype, doc.name)}
	for pic in internal_pics(doc.project):
		notify.enqueue(pic, "signoff_decided", ctx, doc.project)
	frappe.cache.delete_value(f"portal_summary:{doc.project}")
	return result


def _version_no(doc) -> int:
	n = 1
	cur = doc.amended_from
	while cur:
		n += 1
		cur = frappe.db.get_value("Portal Signoff", cur, "amended_from")
	return n


def rule_22_block_change(doc):
	"""Setelah submitted tidak bisa diubah; perubahan lewat Amend standar (amended_from)."""
	if doc.docstatus == 1 and not doc.flags.portal_decision:
		frappe.throw(_("Sign-off yang sudah disetujui tidak bisa diubah. Gunakan Amend."), frappe.ValidationError)


def rule_23_validate_cr(doc):
	if doc.kind != "Change Request":
		return
	missing = []
	if doc.impact_man_days is None:
		missing.append("impact_man_days")
	if doc.impact_timeline_days is None:
		missing.append("impact_timeline_days")
	if missing:
		frappe.throw(_("Change Request wajib mengisi dampak man-day dan timeline."), frappe.ValidationError)


def rule_23_on_cr_approved(doc):
	"""CR Approved: Task baru dari new_tasks (portal_visible), Project.expected_end_date maju, Sales Invoice Draft per termin."""
	if doc.kind != "Change Request":
		return
	phase = active_phase(doc.project)
	for row in doc.new_tasks:
		if row.task:
			continue
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"subject": row.subject,
				"project": doc.project,
				"type": row.type,
				"exp_start_date": today(),
				"exp_end_date": row.exp_end_date,
				"parent_task": phase["name"] if phase else None,
				"portal_visible": 1,
				"description": f"Dari Change Request {doc.name}: {doc.title}",
			}
		).insert(ignore_permissions=True)
		row.db_set("task", task.name, update_modified=False)
	if (doc.impact_timeline_days or 0) > 0:
		end = frappe.db.get_value("Project", doc.project, "expected_end_date")
		if end:
			frappe.db.set_value("Project", doc.project, "expected_end_date", add_days(getdate(end), int(doc.impact_timeline_days)))
	if doc.impact_invoices:
		_make_invoice_drafts(doc)


def _make_invoice_drafts(doc):
	project = frappe.get_doc("Project", doc.project)
	if not project.customer:
		return
	company = project.company
	item_code = _service_item(company)
	if not item_code:
		return
	for row in doc.impact_invoices:
		try:
			inv = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"customer": project.customer,
					"company": company,
					"project": doc.project,
					"remarks": f"Change Request {doc.name}: {doc.title} — {row.payment_term}",
					"items": [
						{
							"item_code": item_code,
							"qty": 1,
							"rate": row.amount,
							"description": f"{doc.title} — {row.payment_term}",
							"project": doc.project,
						}
					],
				}
			)
			inv.set_missing_values()
			inv.insert(ignore_permissions=True)  # Draft, tidak di-submit
		except Exception:
			frappe.log_error("portal_bridge: gagal membuat Sales Invoice Draft dari CR", frappe.get_traceback())


def _service_item(company: str | None) -> str | None:
	row = frappe.get_all("Item", filters={"is_stock_item": 0, "disabled": 0, "is_sales_item": 1}, pluck="name", limit=1,
	                     order_by="modified desc")
	return row[0] if row else None


def rule_24_close_gate(doc):
	"""UAT/Go-live/BAST (dan BRD/Training) Approved → Task milestone dengan portal_signoff_type sama → Completed."""
	if doc.kind == "Change Request":
		return
	tasks = frappe.get_all(
		"Task",
		filters={"project": doc.project, "is_milestone": 1, "portal_signoff_type": doc.kind,
		         "status": ["not in", ["Completed", "Cancelled"]]},
		pluck="name",
	)
	for name in tasks:
		t = frappe.get_doc("Task", name)
		t.status = "Completed"
		t.progress = 100
		t.completed_by = doc.decided_by_user
		t.completed_on = today()
		t.save(ignore_permissions=True)
	frappe.cache.delete_value(f"portal_summary:{doc.project}")


def serialize(doc, detail=False) -> dict:
	d = {
		"name": doc.name,
		"project": doc.project,
		"kind": doc.kind,
		"title": doc.title,
		"doc_version": doc.doc_version,
		"status": doc.status,
		"docstatus": doc.docstatus,
		"requested_by": user_display(doc.requested_by) if doc.requested_by else None,
		"requested_on": doc.requested_on,
		"due": doc.due,
		"decided": {
			"user": doc.decided_by_user, "name": doc.decided_name, "on": doc.decided_on, "note": doc.client_note,
		} if doc.decided_on else None,
		"pdf_file": doc.pdf_file,
		"pdf_hash": doc.pdf_hash,
		"amended_from": doc.amended_from,
		"version_no": _version_no(doc),
	}
	if detail:
		d.update(
			{
				"summary": doc.summary,
				"attachment": doc.attachment,
				"impact": {
					"man_days": doc.impact_man_days,
					"timeline_days": doc.impact_timeline_days,
					"invoices": [{"payment_term": r.payment_term, "amount": r.amount} for r in doc.impact_invoices],
					"new_tasks": [{"subject": r.subject, "exp_end_date": r.exp_end_date, "type": r.type, "task": r.task} for r in doc.new_tasks],
				} if doc.kind == "Change Request" else None,
				"history": _history(doc),
			}
		)
	return d


def _history(doc) -> list[dict]:
	out = []
	cur = doc.amended_from
	while cur:
		r = frappe.db.get_value("Portal Signoff", cur, ["name", "status", "decided_on", "decided_name", "pdf_file", "pdf_hash", "amended_from"], as_dict=True)
		if not r:
			break
		out.append(r)
		cur = r.amended_from
	return out
