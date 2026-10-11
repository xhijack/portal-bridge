"""doc_events untuk DocType bawaan."""

import frappe

from portal_bridge.setup.install import CLIENT_TASK_TYPE


def task_before_save(doc, method=None):
	# Aturan: task bertipe Klien atau milestone otomatis tampil ke klien
	if doc.type == CLIENT_TASK_TYPE or doc.is_milestone:
		doc.portal_visible = 1


def task_on_update(doc, method=None):
	# Invalidasi cache ringkasan project
	if doc.project:
		frappe.cache.delete_value(f"portal_summary:{doc.project}")
