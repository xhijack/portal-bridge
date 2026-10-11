"""Pabrik data uji: 2 customer × 2 project × 2 klien, dibersihkan oleh rollback FrappeTestCase."""

import frappe
from frappe.desk.form.assign_to import add as assign_add
from frappe.utils import add_days, nowdate

from portal_bridge.setup.install import CLIENT_ROLE, CLIENT_TASK_TYPE


def company():
	return frappe.db.get_single_value("Global Defaults", "default_company") or frappe.get_all("Company", pluck="name")[0]


def make_client(email, first="Klien", mobile=None, enabled=1):
	if frappe.db.exists("User", email):
		return email
	frappe.get_doc(
		{
			"doctype": "User", "email": email, "first_name": first, "last_name": "Uji", "mobile_no": mobile,
			"user_type": "Website User", "send_welcome_email": 0, "enabled": enabled, "roles": [{"role": CLIENT_ROLE}],
		}
	).insert(ignore_permissions=True)
	return email


def make_internal(email, first="PIC"):
	if frappe.db.exists("User", email):
		return email
	frappe.get_doc(
		{
			"doctype": "User", "email": email, "first_name": first, "last_name": "Internal", "user_type": "System User",
			"send_welcome_email": 0, "roles": [{"role": "Projects User"}],
		}
	).insert(ignore_permissions=True)
	return email


def make_customer(name):
	if not frappe.db.exists("Customer", name):
		frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_type": "Company",
		                "customer_group": "All Customer Groups", "territory": "All Territories"}).insert(ignore_permissions=True)
	return name


def make_project(name, customer, users, enabled=1, can_approve=None):
	doc = frappe.get_doc(
		{
			"doctype": "Project", "project_name": name, "customer": customer, "company": company(), "status": "Open",
			"expected_start_date": add_days(nowdate(), -30), "expected_end_date": add_days(nowdate(), 60),
			"percent_complete_method": "Task Weight", "portal_enabled": enabled,
			"users": [{"user": u, "welcome_email_sent": 1, "portal_can_approve": 1 if (can_approve and u in can_approve) else 0} for u in users],
		}
	).insert(ignore_permissions=True)
	return doc.name


def make_phase(project, subject, start=None, end=None):
	return frappe.get_doc({"doctype": "Task", "subject": subject, "project": project, "is_group": 1,
	                       "exp_start_date": start or add_days(nowdate(), -10), "exp_end_date": end or add_days(nowdate(), 10),
	                       "portal_visible": 1}).insert(ignore_permissions=True).name


def make_task(project, subject, phase=None, visible=1, client=False, assignee=None, due=None, status="Open", **kw):
	doc = frappe.get_doc(
		{
			"doctype": "Task", "subject": subject, "project": project, "parent_task": phase, "portal_visible": visible,
			"type": CLIENT_TASK_TYPE if client else None, "exp_start_date": add_days(nowdate(), -5),
			"exp_end_date": due or add_days(nowdate(), 5), "status": status, "task_weight": 1, **kw,
		}
	).insert(ignore_permissions=True)
	if client:
		# before_save menyetel portal_visible=1 untuk tipe Klien; paksa nilai uji bila diminta tersembunyi
		if not visible:
			doc.db_set("portal_visible", 0, update_modified=False)
	if assignee:
		assign_add({"assign_to": [assignee], "doctype": "Task", "name": doc.name, "description": subject}, ignore_permissions=True)
	return doc.name


def cleanup(prefix):
	"""Hapus sisa data uji dari run sebelumnya yang sempat ter-commit."""
	frappe.set_user("Administrator")
	for p in frappe.get_all("Project", filters={"project_name": ["like", f"{prefix} %"]}, pluck="name"):
		for dt in ("Portal Comment", "Portal Evidence", "Portal Notification", "Portal Audit Log"):
			frappe.db.delete(dt, {"project": p})
		for dt in ("Portal Signoff", "Portal Meeting Note", "Portal Weekly Update"):
			for n in frappe.get_all(dt, filters={"project": p}, pluck="name"):
				d = frappe.get_doc(dt, n)
				if d.docstatus == 1:
					d.flags.portal_decision = True
					d.cancel()
				d.delete(ignore_permissions=True)
		tasks = frappe.get_all("Task", filters={"project": p}, pluck="name")
		if tasks:
			frappe.db.delete("ToDo", {"reference_type": "Task", "reference_name": ["in", tasks]})
		for t in frappe.get_all("Task", filters={"project": p, "is_group": 0}, pluck="name"):
			frappe.delete_doc("Task", t, ignore_permissions=True, force=True)
		for t in frappe.get_all("Task", filters={"project": p}, pluck="name"):
			frappe.delete_doc("Task", t, ignore_permissions=True, force=True)
		frappe.delete_doc("Project", p, ignore_permissions=True, force=True)
	for c in frappe.get_all("Customer", filters={"customer_name": ["like", f"{prefix} %"]}, pluck="name"):
		frappe.delete_doc("Customer", c, ignore_permissions=True, force=True)
	for u in frappe.get_all("User", filters={"name": ["like", f"{prefix}-%"]}, pluck="name"):
		frappe.db.delete("Portal Magic Link", {"user": u})
		frappe.db.delete("Portal Notification", {"user": u})
		frappe.db.delete("Portal Audit Log", {"user": u})
		frappe.delete_doc("User", u, ignore_permissions=True, force=True)


def make_world(prefix="pbt"):
	"""2 tenant-klien terpisah: A (klien a1, a2) dan B (klien b1)."""
	cleanup(prefix)
	w = frappe._dict()
	ph = "62811" + str(abs(hash(prefix)) % 100000).zfill(5)
	w.pic = make_internal(f"{prefix}-pic@sopwer.test", "Pic")
	w.a1 = make_client(f"{prefix}-a1@klien-a.test", "Ani", ph + "1")
	w.a2 = make_client(f"{prefix}-a2@klien-a.test", "Adi", ph + "2")
	w.b1 = make_client(f"{prefix}-b1@klien-b.test", "Budi", ph + "3")
	w.cust_a = make_customer(f"{prefix} Klien A")
	w.cust_b = make_customer(f"{prefix} Klien B")
	w.proj_a = make_project(f"{prefix} Proyek A", w.cust_a, [w.pic, w.a1, w.a2], can_approve=[w.a1])
	w.proj_b = make_project(f"{prefix} Proyek B", w.cust_b, [w.pic, w.b1], can_approve=[w.b1])
	w.phase_a = make_phase(w.proj_a, "Fase A1")
	w.phase_b = make_phase(w.proj_b, "Fase B1")
	w.task_a_vis = make_task(w.proj_a, "A tampil", w.phase_a, visible=1)
	w.task_a_hidden = make_task(w.proj_a, "A internal", w.phase_a, visible=0)
	w.task_a_client = make_task(w.proj_a, "A tugas klien", w.phase_a, client=True, assignee=w.a1, due=add_days(nowdate(), -2))
	w.task_b_vis = make_task(w.proj_b, "B tampil", w.phase_b, visible=1)
	w.task_b_client = make_task(w.proj_b, "B tugas klien", w.phase_b, client=True, assignee=w.b1)
	frappe.clear_cache(user=w.a1)
	frappe.clear_cache(user=w.a2)
	frappe.clear_cache(user=w.b1)
	return w
