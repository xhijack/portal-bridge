"""Aturan 16–19: komentar berulir."""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime, time_diff_in_seconds

from portal_bridge import notify
from portal_bridge.utils import audit, internal_pics, is_client, portal_url, project_users, settings, user_display

MENTION_RE = re.compile(r"@\[([^\]]+)\]\(([^)]+)\)|@([\w.+-]+@[\w.-]+)")


def _ref_link(project: str, ref_doctype: str, ref_name: str, comment: str | None = None) -> str:
	path = {
		"Portal Meeting Note": f"/{project}/meeting-notes/{ref_name}",
		"Portal Signoff": f"/{project}/approvals/{ref_name}",
		"Task": f"/{project}/tasks?task={ref_name}",
		"Issue": f"/{project}/feedback/{ref_name}",
	}.get(ref_doctype, f"/{project}/overview")
	return f"{portal_url()}{path}" + (f"#komentar-{comment}" if comment else "")


def plain_text(body: str) -> str:
	"""@[Nama](email) → @Nama untuk kutipan di notifikasi."""
	return MENTION_RE.sub(lambda m: "@" + (m.group(1) or m.group(3) or ""), body or "")


def parse_mentions(body: str, project: str, explicit: list[str] | None = None) -> list[str]:
	"""@nama → baris mentions. Hanya Project User project itu (klien dan internal) yang sah (aturan 17)."""
	allowed = {u["user"] for u in project_users(project)}
	found = set(explicit or [])
	for m in MENTION_RE.finditer(body or ""):
		email = m.group(2) or m.group(3)
		if email:
			found.add(email)
	return [u for u in found if u in allowed]


def rule_16_add(project: str, ref_doctype: str, ref_name: str, section: str, body: str, parent: str | None = None,
                mentions: list[str] | None = None):
	"""Komentar menempel pada section; utas = tanpa parent; maksimal satu tingkat balasan."""
	if not (body or "").strip():
		frappe.throw(_("Komentar kosong."), frappe.ValidationError)
	if ref_doctype not in ("Portal Meeting Note", "Portal Signoff", "Task", "Portal Weekly Update", "Issue"):
		frappe.throw(_("Komentar tidak didukung untuk dokumen ini."), frappe.ValidationError)
	if frappe.db.get_value(ref_doctype, ref_name, "project") != project:
		frappe.throw(_("Dokumen bukan milik proyek ini."), frappe.PermissionError)
	if parent:
		p = frappe.db.get_value("Portal Comment", parent, ["parent_comment", "project", "section"], as_dict=True)
		if not p or p.project != project:
			frappe.throw(_("Utas tidak ditemukan."), frappe.DoesNotExistError)
		if p.parent_comment:
			frappe.throw(_("Balasan hanya satu tingkat; balas ke utas utamanya."), frappe.ValidationError)
		section = p.section
	mention_users = parse_mentions(body, project, mentions)
	doc = frappe.get_doc(
		{
			"doctype": "Portal Comment",
			"project": project,
			"ref_doctype": ref_doctype,
			"ref_name": ref_name,
			"section": section or "all",
			"parent_comment": parent,
			"author_user": frappe.session.user,
			"body": body.strip(),
			"status": "Open",
			"mentions": [{"user": u} for u in mention_users],
		}
	)
	doc.insert(ignore_permissions=True)
	rule_17_notify_mentions(doc)
	_notify_thread(doc)
	audit("comment_add", project, ref_doctype, ref_name, {"comment": doc.name, "section": section})
	return doc


def rule_17_notify_mentions(doc):
	actor = user_display(doc.author_user)["full_name"]
	link = _ref_link(doc.project, doc.ref_doctype, doc.ref_name, doc.parent_comment or doc.name)
	for m in doc.mentions:
		if m.user == doc.author_user:
			continue
		notify.enqueue(m.user, "mention", {"actor": actor, "excerpt": plain_text(doc.body)[:120], "link": link}, doc.project,
		               dedupe_key=f"mention:{doc.name}:{m.user}")


def _notify_thread(doc):
	"""Komentar baru di utas: beri tahu penulis utas & peserta (ringkasan maks 1/jam lewat dedupe per jam)."""
	actor = user_display(doc.author_user)["full_name"]
	link = _ref_link(doc.project, doc.ref_doctype, doc.ref_name, doc.parent_comment or doc.name)
	title_field = "subject" if doc.ref_doctype in ("Task", "Issue") else "title"
	ref_title = frappe.db.get_value(doc.ref_doctype, doc.ref_name, title_field) or doc.ref_name
	participants = set()
	if doc.parent_comment:
		participants.add(frappe.db.get_value("Portal Comment", doc.parent_comment, "author_user"))
		participants.update(frappe.get_all("Portal Comment", filters={"parent_comment": doc.parent_comment}, pluck="author_user"))
	else:
		participants.update(internal_pics(doc.project))
	if doc.ref_doctype == "Issue":
		# pelapor feedback selalu ikut diberi tahu
		raised_by = frappe.db.get_value("Issue", doc.ref_name, "raised_by")
		if raised_by and frappe.db.exists("User", raised_by):
			participants.add(raised_by)
	mentioned = {m.user for m in doc.mentions}
	hour_key = now_datetime().strftime("%Y%m%d%H")
	for u in participants - mentioned - {doc.author_user, None}:
		notify.enqueue(u, "comment_new", {"actor": actor, "excerpt": plain_text(doc.body)[:120], "ref_title": ref_title, "link": link},
		               doc.project, dedupe_key=f"thread:{doc.ref_name}:{u}:{hour_key}")


def rule_18_resolve(name: str):
	"""Tandai selesai hanya oleh penulis utas atau user internal."""
	doc = frappe.get_doc("Portal Comment", name)
	if doc.parent_comment:
		frappe.throw(_("Tandai selesai pada utas utamanya."), frappe.ValidationError)
	user = frappe.session.user
	if is_client(user) and doc.author_user != user:
		frappe.throw(_("Hanya penulis utas atau tim penyedia jasa yang bisa menandai selesai."), frappe.PermissionError)
	doc.status = "Resolved"
	doc.resolved_by = user
	doc.resolved_on = now_datetime()
	doc.save(ignore_permissions=True)
	audit("comment_resolve", doc.project, "Portal Comment", name)
	return doc


def rule_19_edit(name: str, body: str):
	doc = frappe.get_doc("Portal Comment", name)
	if doc.author_user != frappe.session.user:
		frappe.throw(_("Hanya penulis yang bisa mengubah komentar."), frappe.PermissionError)
	if not (body or "").strip():
		frappe.throw(_("Komentar kosong."), frappe.ValidationError)
	doc.body = body.strip()
	doc.edited = 1
	doc.set("mentions", [{"user": u} for u in parse_mentions(doc.body, doc.project)])
	doc.save(ignore_permissions=True)
	audit("comment_edit", doc.project, "Portal Comment", name)
	return doc


def rule_19_delete(name: str):
	"""Klien tidak bisa menghapus setelah N menit (default 10)."""
	doc = frappe.get_doc("Portal Comment", name)
	user = frappe.session.user
	if doc.author_user != user and is_client(user):
		frappe.throw(_("Hanya penulis yang bisa menghapus komentar."), frappe.PermissionError)
	if is_client(user):
		limit = int(settings().comment_delete_minutes or 10) * 60
		if time_diff_in_seconds(now_datetime(), get_datetime(doc.creation)) > limit:
			frappe.throw(_("Komentar tidak bisa dihapus setelah {0} menit.").format(limit // 60), frappe.PermissionError)
	if frappe.db.exists("Portal Comment", {"parent_comment": name}):
		frappe.throw(_("Utas yang sudah dibalas tidak bisa dihapus."), frappe.ValidationError)
	project = doc.project
	doc.delete(ignore_permissions=True)
	audit("comment_delete", project, "Portal Comment", name)


def serialize(c, counts_only=False) -> dict:
	author = user_display(c.author_user)
	return {
		"name": c.name,
		"section": c.section,
		"parent": c.parent_comment,
		"author": author,
		"body": c.body,
		"status": c.status,
		"edited": bool(c.edited),
		"created_at": c.creation,
		"resolved_by": c.resolved_by,
		"applied_version": c.applied_version,
		"mentions": [m.user for m in c.get("mentions", [])] if hasattr(c, "get") else [],
	}


def threads(ref_doctype: str, ref_name: str) -> list[dict]:
	rows = frappe.get_all(
		"Portal Comment",
		filters={"ref_doctype": ref_doctype, "ref_name": ref_name},
		fields=["name", "section", "parent_comment", "author_user", "body", "status", "edited", "creation", "resolved_by",
		        "applied_version"],
		order_by="creation asc",
	)
	mentions = {}
	if rows:
		for m in frappe.get_all("Portal Comment Mention", filters={"parent": ["in", [r.name for r in rows]]}, fields=["parent", "user"]):
			mentions.setdefault(m.parent, []).append(m.user)
	by_name = {}
	for r in rows:
		d = serialize(r)
		d["mentions"] = mentions.get(r.name, [])
		d["replies"] = []
		by_name[r.name] = d
	out = []
	for r in rows:  # dua lintasan: balasan bisa lebih tua dari induk (mis. setelah edit)
		d = by_name[r.name]
		if r.parent_comment and r.parent_comment in by_name:
			by_name[r.parent_comment]["replies"].append(d)
		elif not r.parent_comment:
			out.append(d)
	return out


def open_counts(ref_doctype: str, ref_name: str) -> dict[str, int]:
	rows = frappe.get_all(
		"Portal Comment",
		filters={"ref_doctype": ref_doctype, "ref_name": ref_name, "parent_comment": ["in", ["", None]]},
		fields=["section", "status"],
	)
	counts: dict[str, int] = {}
	for r in rows:
		counts[r.section] = counts.get(r.section, 0) + 1
		if r.status == "Open":
			counts["__open__"] = counts.get("__open__", 0) + 1
	return counts
