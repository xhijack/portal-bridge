"""Data demo pilot: PT Maju Bersama Jaya, "Implementasi ERP", 10 fase, 28 task (sesuai mock Rencana.html).

Jalankan: bench --site <site> execute portal_bridge.demo.load
Hapus:    bench --site <site> execute portal_bridge.demo.unload
"""

from __future__ import annotations

import json

import frappe
from frappe.desk.form.assign_to import add as assign_add
from frappe.utils import getdate

from portal_bridge.setup.install import CLIENT_ROLE, CLIENT_TASK_TYPE

CUSTOMER = "PT Maju Bersama Jaya"
PROJECT_NAME = "Implementasi ERP"
Y = 2026

CLIENTS = [
	("dewi@majubersama.co.id", "Dewi", "Wulandari", "6281200000001", 1, "Project Champion"),
	("anita@majubersama.co.id", "Anita", "Sari", "6281200000002", 0, "Finance"),
	("rudi@majubersama.co.id", "Rudi", "Santoso", "6281200000003", 0, "Gudang"),
	("hendra@majubersama.co.id", "Hendra", "Prasetyo", "6281200000004", 1, "Direktur"),
]
INTERNAL = [
	("taufik@sopwer.id", "Taufik", "Hidayat"),
	("ganjar@sopwer.id", "Ganjar", "Wibowo"),
	("nanda@sopwer.id", "Nanda", "Pratama"),
	("aulia@sopwer.id", "Aulia", "Rahma"),
]
U = {k.split("@")[0].capitalize(): k for k, *_ in CLIENTS + INTERNAL}

# (fase, [ (subject, pic, start, end, status, progress, flags) ])
# flags: K=klien, M:<kind>=milestone gate
PLAN = [
	("Kick off", [
		("Rapat kick off & aturan main proyek", "Taufik", (9, 2), (9, 2), "Completed", 100, ""),
		("Tunjuk Project Champion & user kunci", "Dewi", (9, 2), (9, 5), "Completed", 100, "K"),
	]),
	("Requirement (BRD)", [
		("Workshop proses penjualan & pembelian", "Taufik", (9, 3), (9, 9), "Completed", 100, ""),
		("Workshop gudang & akuntansi", "Taufik", (9, 10), (9, 12), "Completed", 100, ""),
		("BRD v1.0 sign-off", "Dewi", (9, 15), (9, 19), "Completed", 100, "K,M:BRD"),
	]),
	("Konfigurasi sistem", [
		("Setup company, CoA, pajak", "Taufik", (9, 22), (9, 24), "Completed", 100, ""),
		("Gudang, lokasi, satuan item", "Taufik", (9, 23), (9, 25), "Completed", 100, ""),
		("Role & hak akses 14 user", "Ganjar", (9, 25), (9, 26), "Completed", 100, ""),
		("Penomoran dokumen & print format dasar", "Ganjar", (9, 26), (9, 30), "Completed", 100, ""),
	]),
	("Development & integrasi", [
		("Print format faktur pajak sesuai contoh klien", "Nanda", (9, 22), (10, 1), "Completed", 100, ""),
		("Integrasi notifikasi WhatsApp untuk Sales Order", "Aulia", (9, 29), (10, 3), "Completed", 100, ""),
		("Alur retur tanpa faktur (CR BRD v1.2)", "Nanda", (10, 13), (10, 17), "Open", 0, ""),
	]),
	("Migrasi data", [
		("Impor master item ke staging (1.248 item)", "Taufik", (9, 29), (10, 3), "Completed", 100, ""),
		("Kirim master data pelanggan (CSV)", "Anita", (10, 1), (10, 8), "Open", 0, "K"),
		("Validasi saldo awal stok Gudang A", "Rudi", (10, 6), (10, 14), "Working", 40, "K"),
		("Rekonsiliasi saldo piutang per pelanggan", "Taufik", (10, 6), (10, 17), "Working", 55, ""),
	]),
	("UAT", [
		("Tunjuk 2 user kasir cabang untuk UAT", "Dewi", (10, 13), (10, 17), "Open", 0, "K"),
		("Jalankan 12 skenario UAT penjualan & pembelian", "Dewi", (10, 20), (10, 31), "Open", 0, "K"),
		("UAT sign-off", "Dewi", (11, 7), (11, 7), "Open", 0, "K,M:UAT"),
	]),
	("Training per role", [
		("Training penjualan & kasir (2 cabang)", "Taufik", (11, 10), (11, 12), "Open", 0, ""),
		("Training gudang & pembelian", "Ganjar", (11, 13), (11, 14), "Open", 0, ""),
		("Training akuntansi + training sign-off", "Anita", (11, 17), (11, 21), "Open", 0, "K,M:Training"),
	]),
	("Opening balance", [
		("Saldo awal akun, piutang, utang per 30 Nov", "Anita", (11, 24), (11, 28), "Open", 0, "K"),
		("Stok opname & saldo stok 2 gudang", "Rudi", (11, 28), (12, 3), "Open", 0, "K"),
		("Rekonsiliasi & persetujuan saldo awal", "Taufik", (12, 3), (12, 5), "Open", 0, ""),
	]),
	("Go-live & BAST", [
		("Cutover dari sistem lama & go-live", "Taufik", (12, 8), (12, 12), "Open", 0, "M:Go-live"),
		("BAST sign-off", "Hendra", (12, 15), (12, 15), "Open", 0, "K,M:BAST"),
	]),
	("Hypercare 30 hari", [
		("Hypercare & serah terima ke Support", "Ganjar", (12, 15), (13, 15), "Open", 0, ""),
	]),
]


def _date(m, d):
	if m > 12:
		return getdate(f"{Y + 1}-{m - 12:02d}-{d:02d}")
	return getdate(f"{Y}-{m:02d}-{d:02d}")


def _ensure_user(email, first, last, mobile=None, website=True):
	if frappe.db.exists("User", email):
		return email
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": first,
			"last_name": last,
			"mobile_no": mobile,
			"user_type": "Website User" if website else "System User",
			"send_welcome_email": 0,
			"enabled": 1,
			"roles": [{"role": CLIENT_ROLE}] if website else [{"role": "Projects User"}],
		}
	)
	doc.flags.ignore_permissions = True
	doc.flags.no_welcome_mail = True
	doc.insert(ignore_permissions=True)
	return doc.name


def load(with_billing: bool = True):
	frappe.set_user("Administrator")
	if frappe.db.exists("Project", {"project_name": PROJECT_NAME, "customer": CUSTOMER}):
		print("Demo sudah ada; jalankan portal_bridge.demo.unload dulu bila ingin memuat ulang.")
		return
	company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.get_all("Company", pluck="name")[0]

	# --- customer & users
	if not frappe.db.exists("Customer", CUSTOMER):
		frappe.get_doc({"doctype": "Customer", "customer_name": CUSTOMER, "customer_type": "Company",
		                "customer_group": frappe.db.get_single_value("Selling Settings", "customer_group") or "All Customer Groups",
		                "territory": frappe.db.get_single_value("Selling Settings", "territory") or "All Territories"}).insert(ignore_permissions=True)
	for email, first, last, mobile, _can, _role in CLIENTS:
		_ensure_user(email, first, last, mobile, website=True)
	for email, first, last in INTERNAL:
		_ensure_user(email, first, last, website=False)

	# --- project
	project = frappe.get_doc(
		{
			"doctype": "Project",
			"project_name": PROJECT_NAME,
			"customer": CUSTOMER,
			"company": company,
			"status": "Open",
			"expected_start_date": _date(9, 1),
			"expected_end_date": _date(13, 15),
			"percent_complete_method": "Task Weight",
			"portal_enabled": 1,
			"portal_budget_man_days": 60,
			"users": [
				{"user": email, "welcome_email_sent": 1, "portal_can_approve": can} for email, _f, _l, _m, can, _r in CLIENTS
			] + [{"user": email, "welcome_email_sent": 1} for email, *_ in INTERNAL],
		}
	)
	project.insert(ignore_permissions=True)

	# --- tasks
	task_by_wbs = {}
	for pi, (phase_label, tasks) in enumerate(PLAN, start=1):
		g = frappe.get_doc(
			{"doctype": "Task", "subject": phase_label, "project": project.name, "is_group": 1,
			 "exp_start_date": _date(*tasks[0][2]), "exp_end_date": _date(*max(t[3] for t in tasks)), "portal_visible": 1,
			 "status": "Completed" if all(t[4] == "Completed" for t in tasks) else "Open"}
		).insert(ignore_permissions=True)
		task_by_wbs[str(pi)] = g.name
		for ti, (subject, pic, start, end, status, progress, flags) in enumerate(tasks, start=1):
			fl = flags.split(",") if flags else []
			is_client = "K" in fl
			gate = next((x.split(":")[1] for x in fl if x.startswith("M:")), None)
			t = frappe.get_doc(
				{
					"doctype": "Task", "subject": subject, "project": project.name, "parent_task": g.name,
					"type": CLIENT_TASK_TYPE if is_client else None, "exp_start_date": _date(*start), "exp_end_date": _date(*end),
					"status": status, "progress": progress, "task_weight": 1, "is_milestone": 1 if gate else 0,
					"portal_signoff_type": gate, "portal_visible": 1,
					"completed_on": _date(*end) if status == "Completed" else None,
					"completed_by": U[pic] if status == "Completed" else None,
				}
			)
			t.insert(ignore_permissions=True)
			task_by_wbs[f"{pi}.{ti}"] = t.name
			assign_add({"assign_to": [U[pic]], "doctype": "Task", "name": t.name, "date": str(_date(*end)), "description": subject},
			           ignore_permissions=True)
	# dependensi: 5.4 dan fase 6 menunggu 5.2
	for dependent in ("5.4", "6"):
		d = frappe.get_doc("Task", task_by_wbs[dependent])
		d.append("depends_on", {"task": task_by_wbs["5.2"]})
		d.save(ignore_permissions=True)

	# --- weekly update minggu 6 (Approved)
	wu = frappe.get_doc(
		{
			"doctype": "Portal Weekly Update", "project": project.name, "week_no": 6, "period_start": _date(10, 3), "period_end": _date(10, 9),
			"status": "Draft", "health": "On track",
			"done": "<ul><li>Impor master item (1.248) ke staging</li><li>Print format faktur pajak</li><li>Notifikasi WhatsApp untuk SO</li></ul>",
			"in_progress": "<ul><li>Rekonsiliasi piutang per pelanggan (55%)</li><li>12 skenario UAT penjualan & pembelian</li></ul>",
			"risks": "<ul><li>Master pelanggan belum diterima; UAT mundur jika lewat 14 Okt</li></ul>",
			"client_actions": [
				{"text": "Kirim master pelanggan (Anita)", "task": task_by_wbs["5.2"]},
				{"text": "Konfirmasi 2 user UAT (Dewi)", "task": task_by_wbs["6.1"]},
				{"text": "Setujui BRD v1.2 (Dewi)"},
			],
		}
	).insert(ignore_permissions=True)
	wu.status = "Approved"
	wu.save(ignore_permissions=True)
	wu.db_set({"approved_on": f"{Y}-10-09 16:40:00", "approved_by": U["Taufik"]}, update_modified=False)

	# --- notulen 1..6
	topics6 = [
		("Status migrasi data", "Master item 1.248 sudah di staging; master pelanggan masih ditunggu dari Anita."),
		("Skenario UAT", "12 skenario penjualan & pembelian disiapkan; butuh 2 user kasir cabang."),
		("Kebijakan stok negatif", "Diputuskan stok negatif dilarang di semua gudang mulai go-live."),
	]
	for seq in range(1, 7):
		doc = frappe.get_doc(
			{
				"doctype": "Portal Meeting Note", "project": project.name, "seq": seq,
				"title": ["Kick off", "Workshop penjualan", "Workshop gudang & akuntansi", "Review konfigurasi", "Progres migrasi",
				          "Weekly meeting migrasi & UAT"][seq - 1],
				"meeting_date": [_date(9, 2), _date(9, 9), _date(9, 12), _date(9, 30), _date(10, 2), _date(10, 9)][seq - 1],
				"start_time": "10:00:00", "end_time": "11:30:00", "location": "Google Meet",
				"status": "Draft",
				"attendees": [{"user": U["Dewi"], "full_name": "Dewi Wulandari", "role": "Project Champion", "side": "Klien", "present": 1},
				              {"user": U["Anita"], "full_name": "Anita Sari", "role": "Finance", "side": "Klien", "present": 1},
				              {"user": U["Taufik"], "full_name": "Taufik Hidayat", "role": "PIC", "side": "Internal", "present": 1}],
				"topics": [{"seq": i, "title": t, "body": b} for i, (t, b) in enumerate(topics6 if seq == 6 else topics6[:1], start=1)],
				"decisions": ([
					{"code": "K-11", "kind": "Keputusan", "body": "Stok negatif dilarang di semua gudang mulai go-live.", "proposed_by": "Dewi", "approved_by": "Taufik", "status": "Approved"},
					{"code": "R-04", "kind": "Risiko", "body": "Data pelanggan terlambat; UAT mundur 1 minggu jika lewat 14 Okt.", "proposed_by": "Taufik", "status": "Proposed", "review_date": _date(10, 14)},
				] if seq == 6 else ([{"code": "K-10", "kind": "Keputusan", "body": "Faktur pajak melanjutkan penomoran sistem lama.", "proposed_by": "Anita", "approved_by": "Dewi", "status": "Approved"}] if seq == 5 else [])),
				"actions": ([{"body": "Kirim master data pelanggan (CSV)", "assignee": U["Anita"], "due": _date(10, 8), "task": task_by_wbs["5.2"]},
				             {"body": "Tunjuk 2 user kasir cabang untuk UAT", "assignee": U["Dewi"], "due": _date(10, 17), "task": task_by_wbs["6.1"]}] if seq == 6 else []),
			}
		).insert(ignore_permissions=True)
		doc.status = "Sent"
		doc.save(ignore_permissions=True)
		if seq < 6:
			doc.db_set({"status": "Confirmed", "confirmed_by": U["Dewi"], "confirmed_on": f"{doc.meeting_date} 17:00:00"}, update_modified=False)
		else:
			doc.db_set({"confirm_due": _date(10, 13), "sent_on": f"{Y}-10-09 16:52:00"}, update_modified=False)
	note6 = frappe.db.get_value("Portal Meeting Note", {"project": project.name, "seq": 6}, "name")
	for user, section, body in [
		(U["Dewi"], "topics:2", "Dua user kasir: Sinta (cabang Bekasi) dan Yoga (cabang Karawang). Saya konfirmasi Senin."),
		(U["Taufik"], "topics:2", "Terima kasih, Bu Dewi. Akun UAT kami siapkan setelah nama masuk."),
		(U["Anita"], "topics:1", "CSV pelanggan masih dirapikan, kolom NPWP banyak kosong. Target kirim 13 Okt."),
	]:
		frappe.get_doc({"doctype": "Portal Comment", "project": project.name, "ref_doctype": "Portal Meeting Note", "ref_name": note6,
		                "section": section, "author_user": user, "body": body, "status": "Open"}).insert(ignore_permissions=True)

	# --- sign-off: BRD v1.0 approved (submitted), CR BRD v1.2 pending
	brd = frappe.get_doc({"doctype": "Portal Signoff", "project": project.name, "kind": "BRD", "title": "BRD v1.0 — Business Requirement Document",
	                      "doc_version": "1.0", "status": "Pending", "requested_by": U["Taufik"], "requested_on": f"{Y}-09-15 09:00:00",
	                      "due": _date(9, 19), "summary": "Dokumen kebutuhan bisnis penjualan, pembelian, gudang, akuntansi (14 halaman)."})
	brd.insert(ignore_permissions=True)
	brd.status = "Approved"
	brd.decided_by_user = U["Dewi"]
	brd.decided_name = "Dewi Wulandari"
	brd.decided_on = f"{Y}-09-19 14:10:00"
	brd.decided_ip = "103.10.66.1"
	brd.flags.portal_decision = True
	brd.save(ignore_permissions=True)
	brd.flags.portal_decision = True
	brd.submit()
	frappe.get_doc(
		{
			"doctype": "Portal Signoff", "project": project.name, "kind": "Change Request", "title": "BRD v1.2 — Revisi alur retur penjualan",
			"doc_version": "1.2", "status": "Pending", "requested_by": U["Taufik"], "requested_on": f"{Y}-10-08 10:00:00", "due": _date(10, 15),
			"summary": "Retur tanpa faktur dengan persetujuan supervisor via WhatsApp. Dampak +2 man-day, timeline tetap.",
			"impact_man_days": 2, "impact_timeline_days": 0,
			"new_tasks": [{"subject": "Alur retur tanpa faktur (CR BRD v1.2)", "exp_end_date": _date(10, 17)}],
		}
	).insert(ignore_permissions=True)

	# --- billing (opsional, butuh akun default company)
	if with_billing:
		try:
			_billing(project, company)
		except Exception:
			frappe.log_error("portal_bridge demo billing gagal", frappe.get_traceback())
			print("Peringatan: data tagihan demo gagal dibuat (lihat Error Log); sisanya tetap dimuat.")

	frappe.db.commit()
	print(f"Demo dimuat: Project {project.name} ({PROJECT_NAME}), 10 fase, 28 task, 6 notulen, 2 sign-off, 1 weekly update.")
	print("Klien: " + ", ".join(f"{e} (WA {m})" for e, _f, _l, m, _c, _r in CLIENTS))
	return project.name


def _billing(project, company):
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	item_code = "IMPL-ERP"
	if not frappe.db.exists("Item", item_code):
		frappe.get_doc({"doctype": "Item", "item_code": item_code, "item_name": "Jasa implementasi ERP", "item_group": "Services",
		                "stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1, "is_purchase_item": 0}).insert(ignore_permissions=True)
	so = frappe.get_doc(
		{
			"doctype": "Sales Order", "customer": CUSTOMER, "company": company, "transaction_date": _date(8, 25), "delivery_date": _date(13, 15),
			"project": project.name, "order_type": "Sales",
			"items": [{"item_code": item_code, "qty": 1, "rate": 150_000_000, "delivery_date": _date(13, 15)}],
			"payment_schedule": [
				{"description": "Uang muka 30%", "due_date": _date(9, 1), "invoice_portion": 30, "payment_amount": 45_000_000},
				{"description": "Setelah UAT sign-off 40%", "due_date": _date(11, 7), "invoice_portion": 40, "payment_amount": 60_000_000},
				{"description": "Setelah BAST 30%", "due_date": _date(12, 15), "invoice_portion": 30, "payment_amount": 45_000_000},
			],
		}
	)
	so.flags.ignore_permissions = True
	so.insert(ignore_permissions=True)
	so.submit()
	project.db_set("sales_order", so.name, update_modified=False)
	inv = make_sales_invoice(so.name)
	inv.posting_date = _date(8, 25)
	inv.set_posting_time = 1
	inv.due_date = _date(9, 1)
	inv.items[0].rate = 45_000_000
	inv.items[0].qty = 1
	inv.payment_schedule = []
	inv.flags.ignore_permissions = True
	inv.insert(ignore_permissions=True)
	inv.submit()
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	pe = get_payment_entry("Sales Invoice", inv.name)
	pe.posting_date = _date(8, 30)
	pe.reference_no = "TRF-0001"
	pe.reference_date = _date(8, 30)
	pe.flags.ignore_permissions = True
	pe.insert(ignore_permissions=True)
	pe.submit()


def unload():
	frappe.set_user("Administrator")
	names = frappe.get_all("Project", filters={"project_name": PROJECT_NAME, "customer": CUSTOMER}, pluck="name")
	for p in names:
		for dt in ("Portal Comment", "Portal Evidence", "Portal Notification", "Portal Audit Log"):
			frappe.db.delete(dt, {"project": p})
		for dt in ("Portal Signoff", "Portal Meeting Note", "Portal Weekly Update"):
			for n in frappe.get_all(dt, filters={"project": p}, pluck="name"):
				d = frappe.get_doc(dt, n)
				if d.docstatus == 1:
					d.flags.portal_decision = True
					d.cancel()
				d.delete(ignore_permissions=True)
		for inv in frappe.get_all("Sales Invoice", filters={"project": p}, pluck="name"):
			d = frappe.get_doc("Sales Invoice", inv)
			for pe in frappe.get_all("Payment Entry Reference", filters={"reference_name": inv}, pluck="parent"):
				x = frappe.get_doc("Payment Entry", pe)
				if x.docstatus == 1:
					x.cancel()
				x.delete()
			if d.docstatus == 1:
				d.cancel()
			d.delete()
		for so in frappe.get_all("Sales Order", filters={"project": p}, pluck="name"):
			d = frappe.get_doc("Sales Order", so)
			if d.docstatus == 1:
				d.cancel()
			d.delete()
		frappe.db.delete("ToDo", {"reference_type": "Task", "reference_name": ["in", frappe.get_all("Task", filters={"project": p}, pluck="name") or [""]]})
		for t in frappe.get_all("Task", filters={"project": p, "is_group": 0}, pluck="name"):
			frappe.delete_doc("Task", t, ignore_permissions=True, force=True)
		for t in frappe.get_all("Task", filters={"project": p}, pluck="name"):
			frappe.delete_doc("Task", t, ignore_permissions=True, force=True)
		frappe.delete_doc("Project", p, ignore_permissions=True, force=True)
	frappe.db.commit()
	print(f"Demo dihapus: {names}")
