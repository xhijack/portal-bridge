"""Aturan 27–29: antrean notifikasi, pemilihan kanal, template, preferensi klien."""

from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime

from portal_bridge.utils import portal_url, settings

# Notifikasi yang tidak bisa dimatikan klien (aturan 29)
MANDATORY = {"magic_link", "signoff_pending", "signoff_due", "task_overdue"}

TEMPLATES = {
	"id": {
		"magic_link": ("Tautan masuk {product}", "Halo {name}, ini tautan masuk {product} Anda (berlaku {days} hari):\n{link}"),
		"weekly_approved": ("Update minggu {week_no} · {project_name}", "Update minggu {week_no} untuk {project_name} sudah terbit: {health}.\n{link}"),
		"meeting_note_sent": ("Notulen #{seq} · {project_name}", "Notulen rapat #{seq} \"{title}\" menunggu konfirmasi Anda sampai {confirm_due}.\n{link}"),
		"meeting_note_updated": ("Notulen #{seq} diperbarui", "Notulen #{seq} \"{title}\" diperbarui ke v{version}.\n{link}"),
		"meeting_note_deemed": ("Notulen #{seq} dianggap disetujui", "Notulen #{seq} \"{title}\" lewat batas konfirmasi dan dianggap disetujui.\n{link}"),
		"meeting_note_decided": ("Notulen #{seq}: {status}", "{actor} {status_text} notulen #{seq} \"{title}\".\n{link}"),
		"mention": ("{actor} menyebut Anda", "{actor} menyebut Anda di komentar: \"{excerpt}\"\n{link}"),
		"comment_new": ("Komentar baru · {ref_title}", "{actor} berkomentar di {ref_title}: \"{excerpt}\"\n{link}"),
		"task_h2": ("Tenggat 2 hari lagi: {subject}", "Tugas \"{subject}\" jatuh tempo {due}. {blocks}\n{link}"),
		"task_h0": ("Jatuh tempo hari ini: {subject}", "Tugas \"{subject}\" jatuh tempo hari ini. {blocks}\n{link}"),
		"task_overdue": ("Terlambat {late_days} hari: {subject}", "Tugas \"{subject}\" terlambat {late_days} hari. {blocks}\n{link}"),
		"task_overdue_pic": ("Tugas klien terlambat: {subject}", "Tugas klien \"{subject}\" ({assignee}) terlambat {late_days} hari, reminder sudah maksimal.\n{link}"),
		"signoff_pending": ("Menunggu persetujuan: {title}", "{kind} \"{title}\" menunggu persetujuan Anda sampai {due}.\n{link}"),
		"signoff_due": ("Besok batas persetujuan: {title}", "{kind} \"{title}\" jatuh tempo besok ({due}).\n{link}"),
		"signoff_decided": ("{kind} {status}: {title}", "{actor} {status_text} {kind} \"{title}\".\n{link}"),
	},
	"en": {
		"magic_link": ("Your {product} sign-in link", "Hi {name}, here is your {product} sign-in link (valid {days} days):\n{link}"),
	},
}

STATUS_TEXT = {
	"Approved": "menyetujui",
	"Rejected": "menolak",
	"Revision requested": "meminta revisi",
	"Confirmed": "mengonfirmasi",
	"Correction requested": "meminta koreksi",
}


def render(template: str, context: dict, locale: str | None = None) -> tuple[str, str]:
	locale = locale or settings().locale or "id"
	spec = TEMPLATES.get(locale, {}).get(template) or TEMPLATES["id"].get(template)
	if not spec:
		return template, json.dumps(context, default=str)
	ctx = {"product": settings().product_name or "Portal", "link": portal_url(), **context}
	subject, body = spec
	try:
		return subject.format(**ctx), body.format(**ctx)
	except KeyError:
		return subject, body


def user_prefs(user: str) -> dict:
	raw = frappe.db.get_value("User", user, "portal_notif_pref")
	if not raw:
		return {}
	try:
		return json.loads(raw)
	except Exception:
		return {}


def choose_channel(user: str) -> tuple[str, str | None]:
	s = settings()
	mobile = frappe.db.get_value("User", user, "mobile_no")
	if s.whatsapp_enabled and mobile:
		return "WhatsApp", mobile
	email = frappe.db.get_value("User", user, "email")
	if s.email_enabled and email and "@" in email:
		return "Email", email
	return "Log", None


def enqueue(user: str, template: str, context: dict, project: str | None = None, dedupe_key: str | None = None,
            send_after=None, force: bool = False):
	"""Masukkan satu notifikasi ke antrean. Mengembalikan nama dokumen atau None bila dilewati."""
	if not user or user in ("Guest", "Administrator"):
		return None
	if template not in MANDATORY and not force and user_prefs(user).get(template) is False:
		return None
	if dedupe_key and frappe.db.exists("Portal Notification", {"dedupe_key": dedupe_key, "status": ["in", ["Queued", "Sent"]]}):
		return None
	channel, _to = choose_channel(user)
	subject, body = render(template, context)
	doc = frappe.get_doc(
		{
			"doctype": "Portal Notification",
			"user": user,
			"project": project,
			"template": template,
			"channel": channel,
			"status": "Queued",
			"send_after": send_after,
			"dedupe_key": dedupe_key,
			"payload": json.dumps({"subject": subject, **context}, default=str),
			"message": body,
		}
	).insert(ignore_permissions=True)
	return doc.name


def enqueue_many(users: list[str], template: str, context: dict, project: str | None = None, dedupe_prefix: str | None = None):
	for u in set(users):
		enqueue(u, template, context, project, dedupe_key=f"{dedupe_prefix}:{u}" if dedupe_prefix else None)


def send(notification_name: str) -> bool:
	"""Kirim satu notifikasi sekarang (dipakai antrean dan magic link)."""
	from portal_bridge.messengers import send_email, send_whatsapp

	doc = frappe.get_doc("Portal Notification", notification_name)
	if doc.status != "Queued":
		return False
	payload = json.loads(doc.payload or "{}")
	subject = payload.get("subject") or doc.template
	try:
		channel, to = choose_channel(doc.user)
		doc.channel = channel
		if channel == "WhatsApp":
			send_whatsapp(to, doc.message)
		elif channel == "Email":
			send_email(to, subject, doc.message)
		else:
			frappe.logger("portal_bridge").info(f"[Log messenger] {doc.user}: {subject}\n{doc.message}")
		doc.status = "Sent"
		doc.sent_at = now_datetime()
		doc.error = None
	except Exception as e:
		doc.status = "Failed"
		doc.error = str(e)[:500]
		frappe.log_error("portal_bridge kirim notifikasi gagal", frappe.get_traceback())
	doc.save(ignore_permissions=True)
	return doc.status == "Sent"
