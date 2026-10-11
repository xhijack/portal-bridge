"""Bukti PDF + hash SHA-256 untuk sign-off dan notulen (prinsip 4: persetujuan tercatat, tak bisa diubah)."""

from __future__ import annotations

import hashlib

import frappe
from frappe.utils.file_manager import save_file


def make_evidence(doc, print_format: str, version: int = 1, decided_by: str | None = None) -> tuple[str, str]:
	"""Render PDF lewat Print Format, simpan sebagai File privat (portal_visible=1), catat Portal Evidence.

	Mengembalikan (file_url, sha256).
	"""
	# tanpa doc=doc: get_print menyetel doc.flags.in_print yang membuat save()/submit() berikutnya diabaikan
	pdf = frappe.get_print(doc.doctype, doc.name, print_format, as_pdf=True)
	sha = hashlib.sha256(pdf).hexdigest()
	fname = f"{doc.name.replace('/', '-')}-v{version}.pdf"
	file_doc = save_file(fname, pdf, doc.doctype, doc.name, is_private=1)
	file_doc.db_set("portal_visible", 1, update_modified=False)
	frappe.get_doc(
		{
			"doctype": "Portal Evidence",
			"ref_doctype": doc.doctype,
			"ref_name": doc.name,
			"project": doc.get("project"),
			"version": version,
			"sha256": sha,
			"pdf_file": file_doc.file_url,
			"decided_by": decided_by,
			"decided_ip": getattr(frappe.local, "request_ip", None),
		}
	).insert(ignore_permissions=True)
	return file_doc.file_url, sha


def verify(file_url: str, expected_sha: str) -> bool:
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode()
	return hashlib.sha256(content).hexdigest() == expected_sha
