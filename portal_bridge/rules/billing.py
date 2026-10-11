"""Aturan 25–26: termin dari Sales Order.payment_schedule dicocokkan ke Sales Invoice."""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate

from portal_bridge.utils import today


def rule_25_terms(project: str) -> dict:
	so_name = frappe.db.get_value("Project", project, "sales_order")
	if not so_name:
		return {"contract_value": None, "currency": None, "sales_order": None, "terms": []}
	so = frappe.db.get_value("Sales Order", so_name, ["grand_total", "currency", "docstatus"], as_dict=True)
	schedule = frappe.get_all(
		"Payment Schedule",
		filters={"parent": so_name, "parenttype": "Sales Order"},
		fields=["payment_term", "description", "due_date", "payment_amount", "invoice_portion", "idx"],
		order_by="idx asc",
	)
	invoices = frappe.get_all(
		"Sales Invoice",
		filters=[["Sales Invoice Item", "sales_order", "=", so_name], ["Sales Invoice", "docstatus", "=", 1]],
		fields=["name", "due_date", "outstanding_amount", "grand_total", "status", "posting_date"],
		order_by="posting_date asc",
		distinct=True,
	)
	# cocokkan berurutan: termin ke-i ↔ invoice ke-i (praktik umum invoice per termin)
	terms = []
	for i, s in enumerate(schedule, start=1):
		inv = invoices[i - 1] if i - 1 < len(invoices) else None
		if not inv:
			status = "not_issued"
		elif flt(inv.outstanding_amount) <= 0:
			status = "paid"
		elif inv.due_date and getdate(inv.due_date) < today():
			status = "overdue"
		else:
			status = "unpaid"
		terms.append(
			{
				"no": i,
				"label": s.description or s.payment_term,
				"portion": flt(s.invoice_portion),
				"amount": flt(s.payment_amount),
				"due_date": inv.due_date if inv else s.due_date,
				"invoice": inv.name if inv else None,
				"status": status,
			}
		)
	return {
		"contract_value": flt(so.grand_total) if so else None,
		"currency": so.currency if so else None,
		"sales_order": so_name,
		"terms": terms,
	}
