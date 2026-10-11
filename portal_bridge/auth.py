"""Masuk lewat magic link (WhatsApp/email) tanpa kata sandi.

Alur: frontend POST request_magic_link(phone|email) → selalu {sent: true} → pesan berisi
{portal_url}/masuk/{token} → frontend POST exchange(token) → sesi Frappe untuk Website User klien
(cookie sid) → semua panggilan /api/resource dan /api/method berjalan sebagai klien itu, dibatasi
permission hook portal_bridge.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, now_datetime

from portal_bridge import notify
from portal_bridge.messengers import normalize_phone
from portal_bridge.utils import (
	CLIENT_ROLE,
	accessible_projects,
	audit,
	can_approve,
	clear_project_cache,
	is_client,
	is_internal,
	portal_url,
	settings,
	user_display,
)

RATE_PER_IDENTITY = (3, 3600)
RATE_PER_IP = (20, 3600)


def _hash(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


def _rate_limit(key: str, limit: int, seconds: int):
	cache_key = f"portal_rl:{key}"
	count = frappe.cache.get_value(cache_key, expires=True) or 0
	if int(count) >= limit:
		frappe.throw(_("Terlalu banyak permintaan. Coba lagi nanti."), frappe.RateLimitExceededError)
	frappe.cache.set_value(cache_key, int(count) + 1, expires_in_sec=seconds)


def find_portal_user(phone: str | None = None, email: str | None = None) -> str | None:
	"""Cari Website User berole Portal Klien (atau user internal untuk pratinjau) dari nomor WA / email."""
	user = None
	if email:
		user = frappe.db.get_value("User", {"email": email.strip().lower(), "enabled": 1}, "name")
	elif phone:
		target = normalize_phone(phone)
		if len(target) < 8:
			return None
		rows = frappe.get_all(
			"User",
			filters={"enabled": 1, "mobile_no": ["is", "set"], "name": ["not in", ["Guest", "Administrator"]]},
			fields=["name", "mobile_no"],
		)
		for r in rows:
			if normalize_phone(r.mobile_no) == target:
				user = r.name
				break
	if not user:
		return None
	if not (is_client(user) or is_internal(user)):
		return None
	return user


def issue_magic_link(user: str, target: str = "/", send: bool = True) -> tuple[str, str]:
	"""Buat token baru (token lama dibatalkan), kirim pesan. Mengembalikan (nama dokumen, token mentah)."""
	s = settings()
	token = secrets.token_urlsafe(32)
	now = now_datetime()
	# token baru membatalkan yang lama
	for old in frappe.get_all("Portal Magic Link", filters={"user": user, "expires_at": [">", now]}, pluck="name"):
		frappe.db.set_value("Portal Magic Link", old, "expires_at", now, update_modified=False)
	doc = frappe.get_doc(
		{
			"doctype": "Portal Magic Link",
			"user": user,
			"token_hash": _hash(token),
			"target_path": target or "/",
			"expires_at": add_days(now, int(s.magic_link_days or 7)),
			"ip": getattr(frappe.local, "request_ip", None),
			"user_agent": (frappe.get_request_header("User-Agent") or "")[:300] if getattr(frappe.local, "request", None) else None,
		}
	).insert(ignore_permissions=True)
	link = f"{portal_url()}/masuk/{token}"
	if target and target != "/":
		link += f"?target={frappe.utils.quote(target)}"
	if send:
		n = notify.enqueue(
			user,
			"magic_link",
			{"name": user_display(user)["first_name"], "days": s.magic_link_days or 7, "link": link},
			force=True,
		)
		if n:
			notify.send(n)
			ndoc = frappe.db.get_value("Portal Notification", n, ["channel", "status"], as_dict=True)
			doc.db_set({"sent_via": ndoc.channel, "sent_to": notify.choose_channel(user)[1]}, update_modified=False)
	audit("magic_link_issued", ref_doctype="User", ref_name=user, user=user)
	return doc.name, token


@frappe.whitelist(allow_guest=True, methods=["POST"])
def request_magic_link(phone: str | None = None, email: str | None = None, target: str = "/"):
	"""Publik. Selalu mengembalikan {sent: true} agar tidak membocorkan nomor/email terdaftar."""
	ip = getattr(frappe.local, "request_ip", None) or "local"
	_rate_limit(f"ip:{ip}", *RATE_PER_IP)
	ident = (email or "").strip().lower() or normalize_phone(phone or "")
	if not ident:
		frappe.throw(_("Isi nomor WhatsApp atau email."), frappe.ValidationError)
	_rate_limit(f"id:{ident}", *RATE_PER_IDENTITY)
	user = find_portal_user(phone=phone, email=email)
	if user:
		try:
			issue_magic_link(user, target or "/")
		except Exception:
			frappe.log_error("portal_bridge magic link gagal", frappe.get_traceback())
	if not frappe.flags.in_test:
		frappe.db.commit()
	return {"sent": True}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def exchange(token: str, target: str | None = None):
	"""Tukar token menjadi sesi Frappe. Token berlaku sampai expires_at atau sampai token baru diterbitkan."""
	if not token:
		frappe.throw(_("Token kosong."), frappe.AuthenticationError)
	row = frappe.db.get_value(
		"Portal Magic Link",
		{"token_hash": _hash(token)},
		["name", "user", "expires_at", "used_at", "target_path"],
		as_dict=True,
	)
	if not row or get_datetime(row.expires_at) < now_datetime():
		frappe.local.response["http_status_code"] = 401
		return {"ok": False, "code": "expired", "message": _("Tautan sudah kedaluwarsa. Minta tautan baru.")}
	user = row.user
	if not frappe.db.get_value("User", user, "enabled"):
		frappe.local.response["http_status_code"] = 401
		return {"ok": False, "code": "disabled", "message": _("Akun dinonaktifkan.")}

	frappe.db.set_value(
		"Portal Magic Link",
		row.name,
		{"used_at": now_datetime(), "ip": getattr(frappe.local, "request_ip", None)},
		update_modified=False,
	)
	session_days = int(settings().session_days or 30)
	session_end = (datetime.now(tz=timezone.utc) + timedelta(days=session_days)).isoformat()
	login_manager = getattr(frappe.local, "login_manager", None)
	if login_manager:
		login_manager.login_as(user, session_end=session_end)
	else:  # mode uji
		frappe.set_user(user)
	clear_project_cache(user)
	audit("login", ref_doctype="User", ref_name=user, user=user, detail={"via": "magic_link"})
	if not frappe.flags.in_test:
		frappe.db.commit()
	return {"ok": True, "target": target or row.target_path or "/", "me": me()}


@frappe.whitelist()
def me():
	user = frappe.session.user
	if user == "Guest":
		frappe.local.response["http_status_code"] = 401
		frappe.throw(_("Belum masuk."), frappe.AuthenticationError)
	s = settings()
	projects = []
	for p in accessible_projects(user):
		row = frappe.db.get_value("Project", p, ["project_name", "customer", "status"], as_dict=True)
		projects.append(
			{
				"project": p,
				"name": row.project_name,
				"customer": row.customer,
				"status": row.status,
				"can_approve": can_approve(p, user) if is_client(user) else True,
			}
		)
	notif_pref = frappe.db.get_value("User", user, "portal_notif_pref")
	return {
		"user": user_display(user),
		"role": "klien" if is_client(user) else ("internal" if is_internal(user) else "none"),
		"mobile_no": frappe.db.get_value("User", user, "mobile_no"),
		"projects": projects,
		"notif_pref": frappe.parse_json(notif_pref) if notif_pref else {},
		"tenant": {
			"name": s.product_name,
			"logo": s.logo,
			"locale": s.locale or "id",
			"theme": {
				"brand": s.brand_color,
				"brand_hover": s.brand_hover_color,
				"accent": s.accent_color,
				"density_default": s.density_default or "rapat",
			},
			"magic_link_days": s.magic_link_days or 7,
		},
	}


@frappe.whitelist(methods=["POST"])
def logout_all():
	from frappe.sessions import clear_sessions

	user = frappe.session.user
	if user == "Guest":
		return {"ok": True}
	audit("logout_all", ref_doctype="User", ref_name=user)
	clear_sessions(user, keep_current=False)
	frappe.local.login_manager.logout()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def preview_link(user: str, target: str = "/"):
	"""PIC internal membuat tautan masuk untuk klien (mis. dari Desk) tanpa mengirim pesan."""
	if not is_internal():
		frappe.throw(_("Hanya tim internal."), frappe.PermissionError)
	if not is_client(user):
		frappe.throw(_("User bukan klien portal."), frappe.ValidationError)
	_name, token = issue_magic_link(user, target, send=False)
	link = f"{portal_url()}/masuk/{token}"
	audit("preview_link", ref_doctype="User", ref_name=user)
	return {"link": link}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def health():
	"""Publik. Dipakai frontend untuk memastikan portal_bridge terpasang dan tersetel di site ini.
	Hanya informasi branding dan daftar peringatan setup; tidak ada data proyek."""
	from portal_bridge import __version__

	s = settings()
	warnings = []
	if not s.portal_url:
		warnings.append("portal_url belum diisi di Portal Bridge Settings; tautan di pesan akan menunjuk ke site Frappe")
	if not s.whatsapp_enabled and not s.email_enabled:
		warnings.append("Tidak ada kanal pengiriman aktif (WhatsApp/email) di Portal Bridge Settings")
	if not frappe.db.exists("Role", CLIENT_ROLE):
		warnings.append("Role Portal Klien belum ada; jalankan bench migrate")
	if not frappe.db.count("Project", {"portal_enabled": 1}):
		warnings.append("Belum ada Project dengan portal_enabled")
	return {
		"ok": True,
		"app": "portal_bridge",
		"version": __version__,
		"frappe": frappe.__version__,
		"product_name": s.product_name or "Portal",
		"logo": s.logo,
		"locale": s.locale or "id",
		"theme": {"brand": s.brand_color, "brand_hover": s.brand_hover_color, "accent": s.accent_color},
		"magic_link_days": s.magic_link_days or 7,
		"portal_url": s.portal_url,
		"channels": {"whatsapp": bool(s.whatsapp_enabled and s.whatsapp_base_url), "email": bool(s.email_enabled)},
		"warnings": warnings,
	}


@frappe.whitelist()
def diagnose(user: str):
	"""System Manager: jelaskan kenapa seorang user melihat (atau tidak melihat) project di portal."""
	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Hanya System Manager."), frappe.PermissionError)
	from portal_bridge.utils import client_projects, customers_of

	u = frappe.db.get_value("User", user, ["name", "enabled", "user_type", "mobile_no", "email"], as_dict=True)
	if not u:
		return {"user": user, "error": "User tidak ada"}
	roles = frappe.get_roles(user)
	customers = customers_of(user)
	project_user_rows = frappe.get_all("Project User", filters={"user": user, "parenttype": "Project"}, fields=["parent", "portal_can_approve"])
	customer_projects = frappe.get_all("Project", filters={"customer": ["in", customers or [""]]}, fields=["name", "project_name", "portal_enabled", "customer"]) if customers else []
	enabled_projects = frappe.get_all("Project", filters={"portal_enabled": 1}, fields=["name", "project_name", "customer"])
	clear_project_cache(user)
	reasons = []
	if not u.enabled:
		reasons.append("User dinonaktifkan")
	if u.user_type != "Website User" and CLIENT_ROLE not in roles:
		reasons.append("User bukan Website User dan tidak berole Portal Klien → diperlakukan sebagai internal; hanya melihat project portal_enabled yang ia punya izin baca (role Projects Manager/User)")
	if u.user_type == "Website User" and not customers and not project_user_rows:
		reasons.append("Website User ini tidak tercantum di Customer → Portal Users maupun Project → Users")
	if customers and not any(p.portal_enabled for p in customer_projects):
		reasons.append("Customer-nya ada, tapi tidak ada project customer itu yang portal_enabled (atau field customer di Project kosong/berbeda)")
	if project_user_rows and not frappe.db.exists("Project", {"name": ["in", [r.parent for r in project_user_rows]], "portal_enabled": 1}):
		reasons.append("Tercantum di Project → Users, tapi project itu belum portal_enabled")
	return {
		"user": u,
		"roles": roles,
		"is_client": is_client(user),
		"is_internal": is_internal(user),
		"customers_with_portal_user": customers,
		"customer_projects": customer_projects,
		"project_user_rows": project_user_rows,
		"all_enabled_projects": enabled_projects,
		"visible_projects": client_projects(user) if is_client(user) else accessible_projects(user),
		"reasons": reasons or ["Tidak ada masalah terdeteksi; user seharusnya melihat visible_projects"],
		"bridge_version": __import__("portal_bridge").__version__,
	}
