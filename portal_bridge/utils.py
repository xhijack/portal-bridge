"""Helper bersama: settings, peran, akses project, audit, hari kerja."""

from __future__ import annotations

import json
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import add_days, getdate, now_datetime

CLIENT_ROLE = "Portal Klien"
INTERNAL_ROLES = {"System Manager", "Projects Manager", "Projects User"}


def settings():
	return frappe.get_cached_doc("Portal Bridge Settings")


def portal_url() -> str:
	url = (settings().portal_url or "").rstrip("/")
	return url or frappe.utils.get_url()


# ---------------------------------------------------------------- peran


def is_client(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user in ("Guest", "Administrator"):
		return False
	return CLIENT_ROLE in frappe.get_roles(user)


def is_internal(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	if user == "Guest":
		return False
	roles = set(frappe.get_roles(user))
	return bool(roles & INTERNAL_ROLES) and not is_client(user)


def require_login():
	if frappe.session.user == "Guest":
		frappe.throw(_("Silakan masuk lewat tautan portal."), frappe.AuthenticationError)


# ---------------------------------------------------------------- akses project


def client_projects(user: str | None = None) -> list[str]:
	"""Project portal_enabled yang mencantumkan user di Project User."""
	user = user or frappe.session.user
	if user == "Guest":
		return []
	cache_key = f"portal_projects:{user}"
	cached = frappe.cache.get_value(cache_key)
	if cached is not None:
		return cached
	rows = frappe.get_all(
		"Project User",
		filters={"user": user, "parenttype": "Project"},
		fields=["parent"],
		order_by="idx asc",
	)
	names = sorted({r.parent for r in rows})
	if names:
		names = frappe.get_all(
			"Project",
			filters={"name": ["in", names], "portal_enabled": 1},
			pluck="name",
			order_by="name asc",
		)
	frappe.cache.set_value(cache_key, names, expires_in_sec=60)
	return names


def clear_project_cache(user: str | None = None):
	if user:
		frappe.cache.delete_value(f"portal_projects:{user}")


def accessible_projects(user: str | None = None) -> list[str]:
	user = user or frappe.session.user
	if is_client(user):
		return client_projects(user)
	if is_internal(user):
		return frappe.get_list(
			"Project",
			filters={"portal_enabled": 1},
			pluck="name",
			order_by="name asc",
			user=user,
		)
	return []


def require_access(project: str, user: str | None = None) -> str:
	"""Pemeriksaan akses untuk semua endpoint portal. Mengembalikan nama project."""
	require_login()
	user = user or frappe.session.user
	if not project or not frappe.db.exists("Project", project):
		frappe.throw(_("Proyek tidak ditemukan."), frappe.DoesNotExistError)
	if is_client(user):
		if project not in client_projects(user):
			frappe.throw(_("Anda tidak punya akses ke proyek ini."), frappe.PermissionError)
		return project
	if is_internal(user) and frappe.has_permission("Project", "read", project, user=user):
		if not frappe.db.get_value("Project", project, "portal_enabled"):
			frappe.throw(_("Proyek ini belum dibuka untuk portal."), frappe.PermissionError)
		return project
	frappe.throw(_("Anda tidak punya akses ke proyek ini."), frappe.PermissionError)


def can_approve(project: str, user: str | None = None) -> bool:
	user = user or frappe.session.user
	return bool(
		frappe.db.get_value(
			"Project User",
			{"parent": project, "parenttype": "Project", "user": user},
			"portal_can_approve",
		)
	)


def project_users(project: str, side: str | None = None) -> list[dict]:
	"""Daftar Project User dengan sisi (klien/internal) dan nama."""
	rows = frappe.get_all(
		"Project User",
		filters={"parent": project, "parenttype": "Project"},
		fields=["user", "full_name", "portal_can_approve", "idx"],
		order_by="idx asc",
	)
	out = []
	for r in rows:
		s = "klien" if is_client(r.user) else "internal"
		if side and s != side:
			continue
		out.append(
			{
				"user": r.user,
				"full_name": r.full_name or frappe.utils.get_fullname(r.user),
				"side": s,
				"can_approve": bool(r.portal_can_approve),
			}
		)
	return out


def internal_pics(project: str) -> list[str]:
	users = [u["user"] for u in project_users(project, side="internal")]
	if not users:
		owner = frappe.db.get_value("Project", project, "owner")
		if owner and owner != "Administrator":
			users = [owner]
	return users


# ---------------------------------------------------------------- audit


def audit(action: str, project: str | None = None, ref_doctype: str | None = None, ref_name: str | None = None,
          detail: dict | None = None, user: str | None = None):
	try:
		frappe.get_doc(
			{
				"doctype": "Portal Audit Log",
				"user": user or frappe.session.user,
				"action": action,
				"project": project,
				"ref_doctype": ref_doctype,
				"ref_name": ref_name,
				"ip": getattr(frappe.local, "request_ip", None),
				"detail": json.dumps(detail or {}, default=str),
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error("portal_bridge audit gagal", frappe.get_traceback())


# ---------------------------------------------------------------- hari kerja


def company_holidays(company: str | None) -> set:
	if not company:
		return set()
	holiday_list = frappe.get_cached_value("Company", company, "default_holiday_list")
	if not holiday_list:
		return set()
	return {getdate(d) for d in frappe.get_all("Holiday", filters={"parent": holiday_list}, pluck="holiday_date")}


def add_working_days(start, days: int, company: str | None = None):
	holidays = company_holidays(company)
	d = getdate(start)
	n = 0
	while n < days:
		d = add_days(d, 1)
		if d.weekday() >= 5 or d in holidays:
			continue
		n += 1
	return d


def project_company(project: str) -> str | None:
	return frappe.db.get_value("Project", project, "company")


# ---------------------------------------------------------------- misc


def today():
	return getdate(now_datetime())


def user_display(user: str) -> dict:
	if not user:
		return {"user": None, "full_name": None, "first_name": None, "side": None}
	full = frappe.utils.get_fullname(user) or user
	return {
		"user": user,
		"full_name": full,
		"first_name": full.split(" ")[0],
		"side": "klien" if is_client(user) else "internal",
	}


def assigned_users(assign_json: str | None) -> list[str]:
	if not assign_json:
		return []
	try:
		return [u for u in json.loads(assign_json) if u]
	except Exception:
		return []


def parse_bool(v) -> bool:
	if isinstance(v, str):
		return v.strip().lower() in ("1", "true", "yes", "ya", "on")
	return bool(v)


@contextmanager
def elevated():
	"""Jalankan efek samping sistem (buat Task, update Project, submit) sebagai Administrator.

	Dipakai setelah pemeriksaan akses selesai; identitas pelaku sudah dicatat sebelum masuk blok ini.
	"""
	prev = frappe.session.user
	frappe.set_user("Administrator")
	try:
		yield
	finally:
		frappe.set_user(prev)
