"""Aturan F1–F6: feedback klien memakai DocType Issue bawaan ERPNext.

F1  Klien membuat satu atau banyak feedback sekaligus (mode tabel); setiap baris jadi Issue dengan
    project, customer, raised_by, issue_type, portal_visible=1, portal_task opsional.
F2  Screenshot/file diunggah lebih dulu ("dititipkan", tanpa dokumen induk), lalu ditautkan ke Issue saat dikirim.
    File titipan hanya bisa dipakai pemiliknya.
F3  Klien hanya boleh mengubah feedback miliknya selama status Open; setelah ditanggapi, perubahan lewat komentar.
F4  Klien menutup siklus: Resolved → Closed ("sudah sesuai") atau Resolved/Closed → Open ("belum sesuai", komentar wajib).
F5  PIC mengubah status / menulis resolution_details di Desk → klien pelapor diberi tahu (events.issue_on_update).
F6  Feedback baru → satu notifikasi ringkas ke PIC internal per kiriman, bukan satu per baris.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, getdate, now_datetime, nowdate
from frappe.utils.file_manager import save_file

from portal_bridge import notify
from portal_bridge.rules import comments as cm
from portal_bridge.setup.install import FEEDBACK_ISSUE_TYPES
from portal_bridge.utils import audit, internal_pics, is_client, is_internal, portal_url, user_display

KINDS = FEEDBACK_ISSUE_TYPES
KIND_BY_TYPE = {v: k for k, v in KINDS.items()}
STATUSES = ("Open", "Replied", "On Hold", "Resolved", "Closed")
IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"}
MAX_BATCH = 50


def upload_policy() -> dict:
	"""Mengikuti System Settings Frappe: Allowed File Extensions (kosong = semua tipe) dan Max File Size (MB)."""
	from frappe.core.api.file import get_max_file_size

	raw = frappe.get_system_settings("allowed_file_extensions") or ""
	exts = sorted({e.strip().lower().lstrip(".") for e in raw.splitlines() if e.strip()})
	return {"extensions": exts or None, "max_bytes": get_max_file_size()}

FIELDS = [
	"name", "subject", "status", "issue_type", "priority", "opening_date", "raised_by", "description", "resolution_details",
	"portal_task", "project", "creation", "modified", "modified_by", "first_responded_on",
]


def link(project: str, name: str | None = None) -> str:
	return f"{portal_url()}/{project}/feedback" + (f"/{name}" if name else "")


# ---------------------------------------------------------------- serialisasi


def _attachments(issue_names: list[str]) -> dict[str, list[dict]]:
	out: dict[str, list[dict]] = {n: [] for n in issue_names}
	if not issue_names:
		return out
	for f in frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Issue", "attached_to_name": ["in", issue_names]},
		fields=["name", "file_name", "file_url", "file_size", "attached_to_name", "is_private"],
		order_by="creation asc",
	):
		out.setdefault(f.attached_to_name, []).append(_file_row(f))
	return out


def _file_row(f) -> dict:
	ext = (f.file_name or "").rsplit(".", 1)[-1].lower() if "." in (f.file_name or "") else ""
	return {"name": f.name, "file_name": f.file_name, "file_url": f.file_url, "file_size": f.file_size, "is_image": ext in IMAGE_EXT}


def _task_labels(task_names: list[str]) -> dict[str, dict]:
	names = [n for n in set(task_names) if n]
	if not names:
		return {}
	return {t.name: {"name": t.name, "subject": t.subject, "status": t.status}
	        for t in frappe.get_all("Task", filters={"name": ["in", names]}, fields=["name", "subject", "status"])}


def _raised_by_display(user: str | None) -> dict:
	"""user_display dengan memo per request (is_client/get_fullname beberapa query per user)."""
	memo = getattr(frappe.local, "portal_user_display_memo", None)
	if memo is None:
		memo = frappe.local.portal_user_display_memo = {}
	if user not in memo:
		if user and frappe.db.exists("User", user):
			memo[user] = user_display(user)
		else:
			memo[user] = {"user": user, "full_name": user, "first_name": user, "side": "klien"}
	return memo[user]


def _activity(names: list[str], user: str) -> dict[str, dict]:
	"""Per Issue: jumlah komentar, utas terbuka, komentar terakhir (siapa/sisi/kapan/kutipan), dan waktu terakhir user membukanya."""
	out: dict[str, dict] = {n: {"total": 0, "open": 0, "last": None, "last_view": None} for n in names}
	if not names:
		return out
	rows = frappe.get_all(
		"Portal Comment", filters={"ref_doctype": "Issue", "ref_name": ["in", names]},
		fields=["name", "ref_name", "author_user", "creation", "body", "parent_comment", "status"], order_by="creation asc",
	)
	root_status = {c.name: c.status for c in rows if not c.parent_comment}
	for c in rows:
		a = out[c.ref_name]
		a["total"] += 1
		if not c.parent_comment and c.status == "Open":
			a["open"] += 1
		a["last"] = c  # terurut naik → yang terakhir menang
		# komentar di utas yang sudah ditandai terjawab tidak dihitung untuk "giliran siapa"
		if root_status.get(c.parent_comment or c.name) == "Open":
			a["last_open"] = c
		if not is_client(c.author_user):
			a["team_replied"] = True
	for v in frappe.get_all(
		"Portal Audit Log", filters={"user": user, "action": "feedback_view", "ref_doctype": "Issue", "ref_name": ["in", names]},
		fields=["ref_name", "max(creation) as last_view"], group_by="ref_name",
	):
		if v.ref_name in out:
			out[v.ref_name]["last_view"] = v.last_view
	return out


def serialize(row, files: list[dict], open_comments: int, user: str | None = None, *, internal: bool | None = None,
              tasks: dict[str, dict] | None = None, activity: dict | None = None) -> dict:
	user = user or frappe.session.user
	mine = row.raised_by == user
	if internal is None:
		internal = is_internal(user)
	if tasks is None:
		tasks = _task_labels([row.portal_task])
	act = activity or {"total": 0, "open": open_comments, "last": None, "last_view": None}
	last = act.get("last")
	last_comment = None
	awaiting = None  # "team" = komentar terakhir dari klien belum dijawab tim; "client" = tim bertanya, menunggu klien
	if last is not None:
		who = _raised_by_display(last.author_user)
		from portal_bridge.rules.comments import plain_text

		last_comment = {"author": who, "created_at": last.creation, "excerpt": (plain_text(last.body) or "📎")[:120]}
	last_open = act.get("last_open")
	if last_open is not None:
		awaiting = "team" if _raised_by_display(last_open.author_user)["side"] == "klien" else "client"
	elif row.status == "Open" and not act.get("team_replied"):
		awaiting = "team"  # belum ada tanggapan tim sama sekali
	# pembaruan yang belum dibuka user ini: perubahan oleh orang lain setelah terakhir kali dibuka
	events = []
	if last is not None and last.author_user != user:
		events.append(last.creation)
	if row.modified_by != user:
		events.append(row.modified)
	latest = max(events) if events else None
	has_update = bool(latest) and (act.get("last_view") is None or latest > act["last_view"])
	return {
		"name": row.name,
		"subject": row.subject,
		"kind": KIND_BY_TYPE.get(row.issue_type),
		"issue_type": row.issue_type,
		"priority": row.priority,
		"date": row.opening_date,
		"status": row.status,
		"task": tasks.get(row.portal_task) if row.portal_task else None,
		"raised_by": _raised_by_display(row.raised_by),
		"description": row.description,
		"resolution": row.resolution_details,
		"first_responded_on": row.first_responded_on,
		"created_at": row.creation,
		"modified": row.modified,
		"attachments": files,
		"open_comments": act.get("open", open_comments),
		"comments_total": act.get("total", 0),
		"last_comment": last_comment,
		"awaiting": awaiting,
		"has_update": has_update,
		"mine": mine,
		"can_edit": internal or (mine and row.status == "Open"),
		"can_close": row.status == "Resolved",
		"can_reopen": row.status in ("Resolved", "Closed"),
	}


def list_rows(project: str, status: str | None = None, user: str | None = None) -> dict:
	user = user or frappe.session.user
	filters: dict = {"project": project, "portal_visible": 1}
	rows = frappe.get_all("Issue", filters=filters, fields=FIELDS, order_by="opening_date desc, creation desc")
	names = [r.name for r in rows]
	files = _attachments(names)
	activity = _activity(names, user)
	internal = is_internal(user)
	tasks = _task_labels([r.portal_task for r in rows])
	items = [serialize(r, files.get(r.name, []), activity[r.name]["open"], user, internal=internal, tasks=tasks, activity=activity[r.name])
	         for r in rows]
	counts = {s: 0 for s in STATUSES}
	for it in items:
		counts[it["status"]] = counts.get(it["status"], 0) + 1
	counts["all"] = len(items)
	counts["open"] = counts["Open"] + counts["Replied"] + counts["On Hold"]
	counts["needs_you"] = counts["Resolved"]
	counts["unanswered"] = sum(1 for i in items if i["awaiting"] == "team" and i["status"] not in ("Resolved", "Closed"))
	counts["updates"] = sum(1 for i in items if i["has_update"])
	if status == "open":
		items = [i for i in items if i["status"] in ("Open", "Replied", "On Hold")]
	elif status == "needs_you":
		items = [i for i in items if i["status"] == "Resolved"]
	elif status == "done":
		items = [i for i in items if i["status"] in ("Resolved", "Closed")]
	return {"items": items, "counts": counts, "kinds": list(KINDS.keys())}


def detail(name: str, user: str | None = None) -> dict:
	row = frappe.db.get_value("Issue", name, FIELDS, as_dict=True)
	if not row:
		frappe.throw(_("Feedback tidak ditemukan."), frappe.DoesNotExistError)
	files = _attachments([name]).get(name, [])
	activity = _activity([name], user or frappe.session.user)[name]
	d = serialize(row, files, activity["open"], user, activity=activity)
	d["project"] = row.project
	d["comments"] = cm.threads("Issue", name)
	# mentionables tidak dikirim di sini: frontend memakai project_members (cache 5 menit)
	return d


def task_options(project: str) -> list[dict]:
	"""Pilihan "task terkait" yang ringan (tanpa pohon/blocks seperti `plan`): task tampil, bukan grup, belum dibatalkan."""
	from portal_bridge.api import _wbs_map

	wbs = _wbs_map(project)
	rows = frappe.get_all(
		"Task", filters={"project": project, "portal_visible": 1, "is_group": 0, "status": ["!=", "Cancelled"]},
		fields=["name", "subject", "status", "exp_end_date"], order_by="exp_end_date asc, idx asc",
	)
	out = [{"name": r.name, "subject": r.subject, "status": r.status, "wbs": wbs.get(r.name)} for r in rows]
	out.sort(key=lambda r: [int(x) if x.isdigit() else 0 for x in (r["wbs"] or "9999").split(".")])
	return out


def needs_action_count(project: str, user: str | None = None) -> int:
	"""Badge tab: klien → feedback yang menunggu konfirmasinya (Resolved); internal → yang belum ditanggapi (Open)."""
	user = user or frappe.session.user
	status = "Resolved" if is_client(user) else "Open"
	return frappe.db.count("Issue", {"project": project, "portal_visible": 1, "status": status})


# ---------------------------------------------------------------- F2: file titipan


def stage_file(filename: str, content: bytes, issue: str | None = None):
	"""Simpan file privat. Tanpa `issue` → dititipkan (attached_to kosong) sampai feedback dikirim."""
	filename = frappe.utils.sanitize_html(filename or "file")
	ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
	policy = upload_policy()
	if policy["extensions"] is not None and ext not in policy["extensions"]:
		frappe.throw(_("Tipe file .{0} tidak diizinkan. Gunakan: {1}.").format(ext, ", ".join(policy["extensions"])), frappe.ValidationError)
	if len(content) > policy["max_bytes"]:
		frappe.throw(_("Ukuran file maksimal {0} MB.").format(policy["max_bytes"] // (1024 * 1024)), frappe.ValidationError)
	if issue:
		row = frappe.db.get_value("Issue", issue, ["project", "raised_by", "status", "portal_visible"], as_dict=True)
		if not row or not row.portal_visible:
			frappe.throw(_("Feedback tidak ditemukan."), frappe.DoesNotExistError)
		user = frappe.session.user
		if is_client(user) and row.raised_by != user:
			frappe.throw(_("Hanya pelapor yang bisa menambah lampiran."), frappe.PermissionError)
		file_doc = save_file(filename, content, "Issue", issue, is_private=1)
		audit("feedback_upload", row.project, "Issue", issue, {"file": file_doc.name, "size": len(content)})
	else:
		file_doc = save_file(filename, content, None, None, is_private=1)
	file_doc.db_set("portal_visible", 1, update_modified=False)
	return file_doc


def _claim_staged(file_names: list[str], issue: str, user: str, doctype: str = "Issue"):
	"""Tautkan file titipan ke dokumen (Issue / Portal Comment); hanya file milik user yang belum punya induk."""
	for fname in file_names or []:
		f = frappe.db.get_value("File", fname, ["owner", "attached_to_doctype", "attached_to_name"], as_dict=True)
		if not f:
			frappe.throw(_("File {0} tidak ditemukan.").format(fname), frappe.DoesNotExistError)
		if f.owner != user or f.attached_to_doctype:
			frappe.throw(_("File {0} bukan milik Anda atau sudah dipakai.").format(fname), frappe.PermissionError)
		frappe.db.set_value("File", fname, {"attached_to_doctype": doctype, "attached_to_name": issue}, update_modified=False)


# ---------------------------------------------------------------- F1: buat


def _validate_task(project: str, task: str | None) -> str | None:
	if not task:
		return None
	t = frappe.db.get_value("Task", task, ["project", "portal_visible"], as_dict=True)
	if not t or t.project != project or not t.portal_visible:
		frappe.throw(_("Task {0} tidak ada di proyek ini.").format(task), frappe.ValidationError)
	return task


def _issue_type(kind: str | None) -> str | None:
	if not kind:
		return None
	if kind in KINDS:
		return KINDS[kind]
	if kind in KIND_BY_TYPE:
		return kind
	frappe.throw(_("Jenis feedback tidak dikenal: {0}.").format(kind), frappe.ValidationError)


def rule_f1_create(project: str, items: list[dict]) -> list[str]:
	user = frappe.session.user
	if not isinstance(items, list) or not items:
		frappe.throw(_("Tidak ada feedback yang dikirim."), frappe.ValidationError)
	if len(items) > MAX_BATCH:
		frappe.throw(_("Maksimal {0} feedback per kiriman.").format(MAX_BATCH), frappe.ValidationError)
	p = frappe.db.get_value("Project", project, ["customer", "company"], as_dict=True)
	created = []
	for i, it in enumerate(items, start=1):
		subject = (it.get("subject") or "").strip()
		if not subject:
			frappe.throw(_("Baris {0}: judul wajib diisi.").format(i), frappe.ValidationError)
		date = getdate(it.get("date")) if it.get("date") else getdate(nowdate())
		doc = frappe.get_doc(
			{
				"doctype": "Issue",
				"subject": subject[:140],
				"status": "Open",
				"raised_by": user,
				"customer": p.customer,
				"company": p.company,
				"project": project,
				"issue_type": _issue_type(it.get("kind")),
				"opening_date": date,
				"description": it.get("description") or None,
				"portal_visible": 1,
				"portal_task": _validate_task(project, it.get("task")),
			}
		)
		doc.flags.portal_client_action = True
		doc.insert(ignore_permissions=True)
		_claim_staged(it.get("files") or [], doc.name, user)
		audit("feedback_create", project, "Issue", doc.name, {"subject": subject, "kind": it.get("kind"), "files": len(it.get("files") or [])})
		created.append(doc.name)
	_notify_new(project, created, user)
	frappe.cache.delete_value(f"portal_summary:{project}")
	return created


def _notify_new(project: str, names: list[str], actor: str):
	if not names:
		return
	first = frappe.db.get_value("Issue", names[0], "subject")
	ctx = {
		"actor": user_display(actor)["full_name"],
		"count": len(names),
		"subject": first,
		"more": f" (+{len(names) - 1} lainnya)" if len(names) > 1 else "",
		"project_name": frappe.db.get_value("Project", project, "project_name"),
		"link": link(project) if len(names) > 1 else link(project, names[0]),
	}
	notify.enqueue_many(internal_pics(project), "feedback_new", ctx, project, dedupe_prefix=f"feedback_new:{names[0]}")


# ---------------------------------------------------------------- F3: ubah


def rule_f3_update(name: str, changes: dict):
	doc = frappe.get_doc("Issue", name)
	user = frappe.session.user
	if is_client(user):
		if doc.raised_by != user:
			frappe.throw(_("Hanya pelapor yang bisa mengubah feedback ini."), frappe.PermissionError)
		if doc.status != "Open":
			frappe.throw(_("Feedback sudah ditanggapi; sampaikan perubahan lewat komentar."), frappe.ValidationError)
	elif not is_internal(user):
		frappe.throw(_("Tidak diizinkan."), frappe.PermissionError)
	if "subject" in changes:
		subject = (changes.get("subject") or "").strip()
		if not subject:
			frappe.throw(_("Judul wajib diisi."), frappe.ValidationError)
		doc.subject = subject[:140]
	if "description" in changes:
		doc.description = changes.get("description") or None
	if "kind" in changes:
		doc.issue_type = _issue_type(changes.get("kind"))
	if "task" in changes:
		doc.portal_task = _validate_task(doc.project, changes.get("task"))
	if "date" in changes and changes.get("date"):
		doc.opening_date = getdate(changes["date"])
	doc.flags.portal_client_action = True
	doc.save(ignore_permissions=True)
	audit("feedback_update", doc.project, "Issue", name, {k: v for k, v in changes.items() if k != "description"})
	return doc


# ---------------------------------------------------------------- F4: tutup / buka kembali


def rule_f4_decide(name: str, decision: str, note: str | None = None):
	doc = frappe.get_doc("Issue", name)
	user = frappe.session.user
	actor = user_display(user)["full_name"]
	pics = internal_pics(doc.project)
	if decision == "close":
		if doc.status != "Resolved":
			frappe.throw(_("Hanya feedback berstatus selesai yang bisa dikonfirmasi."), frappe.ValidationError)
		doc.status = "Closed"
		doc.flags.portal_client_action = True
		doc.save(ignore_permissions=True)
		audit("feedback_close", doc.project, "Issue", name)
		notify.enqueue_many(pics, "feedback_closed", {"actor": actor, "subject": doc.subject, "link": link(doc.project, name)},
		                    doc.project, dedupe_prefix=f"feedback_closed:{name}:{now_datetime():%Y%m%d%H}")
	elif decision == "reopen":
		if doc.status not in ("Resolved", "Closed"):
			frappe.throw(_("Feedback ini belum ditandai selesai."), frappe.ValidationError)
		if not (note or "").strip():
			frappe.throw(_("Tulis alasan mengapa belum sesuai."), frappe.ValidationError)
		doc.status = "Open"
		doc.flags.portal_client_action = True
		doc.save(ignore_permissions=True)
		# alasan tercatat sebagai komentar utas (ikut notifikasi komentar) + notifikasi khusus ke PIC
		cm.rule_16_add(doc.project, "Issue", name, "all", note.strip())
		audit("feedback_reopen", doc.project, "Issue", name, {"note": note.strip()[:200]})
		notify.enqueue_many(pics, "feedback_reopened", {"actor": actor, "subject": doc.subject, "excerpt": note.strip()[:120],
		                                                 "link": link(doc.project, name)},
		                    doc.project, dedupe_prefix=f"feedback_reopened:{name}:{now_datetime():%Y%m%d%H}")
	else:
		frappe.throw(_("Keputusan tidak dikenal."), frappe.ValidationError)
	frappe.cache.delete_value(f"portal_summary:{doc.project}")
	return {"ok": True, "status": doc.status}


# ---------------------------------------------------------------- F5: perubahan dari Desk


STATUS_TEXT = {"Open": "dibuka", "Replied": "ditanggapi", "On Hold": "ditunda", "Resolved": "selesai", "Closed": "ditutup"}


def on_issue_update(doc):
	if doc.flags.get("portal_client_action"):
		return
	before = doc.get_doc_before_save()
	if not before:
		return
	status_changed = before.status != doc.status
	resolution_changed = (before.resolution_details or "") != (doc.resolution_details or "")
	if not status_changed and not resolution_changed:
		return
	to = doc.raised_by
	if not to or not frappe.db.exists("User", to) or not is_client(to):
		return
	hour = now_datetime().strftime("%Y%m%d%H")
	ctx = {"subject": doc.subject, "status_text": STATUS_TEXT.get(doc.status, doc.status), "link": link(doc.project, doc.name)}
	if doc.status == "Resolved" and status_changed:
		notify.enqueue(to, "feedback_resolved", ctx, doc.project, dedupe_key=f"feedback_resolved:{doc.name}:{hour}")
	elif doc.status in ("Replied", "On Hold", "Open") or resolution_changed:
		notify.enqueue(to, "feedback_replied", ctx, doc.project, dedupe_key=f"feedback_replied:{doc.name}:{hour}")
	frappe.cache.delete_value(f"portal_summary:{doc.project}")


def on_thread_resolved(doc):
	"""Utas komentar ditandai terjawab: bila tak ada utas terbuka lagi dan tim pernah menanggapi, status Open (akibat balasan klien)
	kembali ke Replied. Feedback tidak ditutup otomatis; penutupan tetap lewat Resolved (tim) → Sudah sesuai (klien)."""
	row = frappe.db.get_value("Issue", doc.ref_name, ["status", "portal_visible"], as_dict=True)
	if not row or not row.portal_visible or row.status != "Open":
		return
	if frappe.db.exists("Portal Comment", {"ref_doctype": "Issue", "ref_name": doc.ref_name, "parent_comment": ["in", ["", None]], "status": "Open"}):
		return
	authors = frappe.get_all("Portal Comment", filters={"ref_doctype": "Issue", "ref_name": doc.ref_name}, pluck="author_user")
	if any(not is_client(a) for a in set(authors)):
		frappe.db.set_value("Issue", doc.ref_name, "status", "Replied")
		frappe.cache.delete_value(f"portal_summary:{doc.project}")


def on_comment_added(doc):
	"""Status Issue mengikuti percakapan (seperti Communication di ERPNext): tim berkomentar saat Open → Replied;
	klien berkomentar saat Replied → Open. Lewat db.set_value agar tidak memicu notifikasi status ke klien (komentar sudah bernotifikasi)."""
	row = frappe.db.get_value("Issue", doc.ref_name, ["status", "first_responded_on", "portal_visible"], as_dict=True)
	if not row or not row.portal_visible:
		return
	author_internal = not is_client(doc.author_user)
	if author_internal and row.status == "Open":
		values = {"status": "Replied"}
		if not row.first_responded_on:
			values["first_responded_on"] = now_datetime()
		frappe.db.set_value("Issue", doc.ref_name, values)
	elif not author_internal and row.status == "Replied":
		frappe.db.set_value("Issue", doc.ref_name, "status", "Open")
	frappe.cache.delete_value(f"portal_summary:{doc.project}")
