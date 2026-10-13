# CLAUDE.md — portal_bridge (app Frappe)

## Tujuan
Backend portal klien proyek implementasi. Frontend (repo `~/frontends/sopwer-portal`, Next.js) **tidak punya backend sendiri**: ia membaca REST bawaan Frappe/ERPNext (`/api/resource/*`) dan memanggil endpoint gabungan di app ini (`/api/method/portal_bridge.*`). Semua yang tidak bisa dilakukan REST bawaan ada di sini: magic link, permission hook, DocType portal, aturan bisnis, scheduler, notifikasi, bukti PDF.

Satu site Frappe = satu tenant. Spesifikasi produk: `~/frontends/sopwer-portal/docs/PRD.md` (sumber kebenaran) + `docs/ARSITEKTUR-V2.md` (perubahan dari PRD: tanpa Postgres/FastAPI).

## Perintah (bench di `~/frappe-bench`, site uji `local.sopwer.my.id`, Frappe/ERPNext v15)
- `bench --site local.sopwer.my.id migrate` — sinkron DocType + jalankan `after_migrate` (setup idempoten)
- `bench --site local.sopwer.my.id run-tests --app portal_bridge` — 28 uji (auth, isolasi, aturan bisnis, feedback)
- `bench --site local.sopwer.my.id execute portal_bridge.setup.install.setup` — ulang setup (Custom Field, Role, Task Type, DocPerm, Print Format, default Settings)
- `bench --site local.sopwer.my.id execute portal_bridge.demo.load` / `portal_bridge.demo.unload` — data demo PT Maju Bersama Jaya (28 task)
- `bench --site local.sopwer.my.id serve --port 8001` — server dev (gunicorn produksi di 8000 butuh `sudo supervisorctl restart` setelah ubah Python)
- Setelah mengubah `hooks.py`: `bench --site <site> clear-cache && bench restart`

## Struktur
- `portal_bridge/hooks.py` — `after_install`/`after_migrate` → `setup.install.setup`; `permission_query_conditions` + `has_permission` (Project, Task, File, Issue, Portal *); `doc_events` Task, Customer, Project, Issue; `scheduler_events`
- `setup/install.py` — satu sumber skema Custom Field (`CUSTOM_FIELDS`), role `Portal Klien`, Task Type `Klien`, Issue Type feedback (`FEEDBACK_ISSUE_TYPES`: Bug, Permintaan Perubahan, Pertanyaan, Saran), DocPerm klien (read saja), Print Format (`setup/print_formats.py`), default Settings
- `auth.py` — `request_magic_link` (publik, selalu `{sent:true}`, rate limit 3/jam/identitas, 20/jam/IP), `exchange` (token → sesi Frappe `login_as`, `session_end` 30 hari), `me`, `logout_all`, `preview_link` (internal)
- `permissions.py` — lapis isolasi klien: hanya `portal_visible=1` pada project `portal_enabled` yang bisa diakses user. **Akses klien** (`utils.client_projects`) = gabungan (a) `Customer → Portal Users` (semua project customer itu; cara utama) dan (b) `Project → Users` (per project). `portal_can_approve` ada di keduanya. Website User yang dicantumkan otomatis diberi role `Portal Klien` lewat `doc_events` Customer/Project `on_update`
- `api.py` — endpoint gabungan: `summary`, `plan`, `my_tasks`, `task`, `complete_task`, `upload_task_file`, `weekly_*`, `meeting_note*`, `confirm_meeting_note`, `request_meeting_note_correction`, `comments`/`add_comment`/`resolve_comment`/`edit_comment`/`delete_comment`, `signoffs`/`signoff`/`decide_signoff`, `feedback`/`feedback_item`/`feedback_tasks` (pilihan task ringan, jangan pakai `plan` untuk dropdown)/`create_feedback` (batch)/`upload_feedback_file`/`update_feedback`/`decide_feedback`, `documents`, `billing`, `set_notif_pref`, `projects`. Semua memanggil `utils.require_access(project)` dan menulis `Portal Audit Log` untuk aksi
- `rules/progress.py::load_tree` — pohon task penuh (grup bertingkat); daun hanya `portal_visible=1`, grup tampil bila punya keturunan tampil. `plan` mengirim baris rata (pre-order) dengan `depth`/`parent`/`is_group`. Progres: `Project.percent_complete`, fallback hitung dari daun bila 0. Custom field `Project.portal_show_all_tasks`: saat dicentang semua task project diset tampil (sekali jalan) dan task baru ikut tampil
- `rules/` — aturan bisnis PRD 1–29, satu fungsi per nomor (`progress.py` 1–4, `client_tasks.py` 5–8, `weekly.py` 9–11, `meeting_notes.py` 12–15, `comments.py` 16–19, `signoff.py` 20–24, `billing.py` 25–26) dan `notify.py` 27–29; `feedback.py` F1–F6 (feedback klien = **Issue bawaan** dengan custom field `portal_visible`, `portal_task`; file screenshot diunggah dulu tanpa induk lalu ditautkan saat `create_feedback`; tipe dan ukuran file mengikuti System Settings Frappe (`upload_policy()`: Allowed File Extensions, kosong = semua tipe; Max File Size) dan diekspos di `auth.me.upload`; klien ubah hanya saat Open; klien tutup Resolved→Closed atau buka kembali dengan komentar wajib; PIC ubah status/`resolution_details` di Desk → klien diberi tahu lewat `events.issue_on_update`; komentar `Portal Comment` dengan `ref_doctype=Issue` (tim berkomentar saat Open → status Replied, klien membalas saat Replied → Open, lewat `db.set_value` tanpa notifikasi status); daftar membawa `last_comment`, `awaiting` (team/client), `has_update` (perubahan orang lain setelah `feedback_view` terakhir user di Portal Audit Log), `counts.unanswered/updates`; serialisasi hemat query: task di-batch, `user_display` dimemo per request, `mentionables` tidak dikirim di detail karena frontend memakai `project_members`)
- `notify.py` + `messengers.py` — antrean `Portal Notification`, kanal WhatsApp (gateway HTTP, konfigurasi di Portal Bridge Settings) lalu email (`frappe.sendmail`), template Indonesia (+ Inggris sebagian), preferensi klien (`User.portal_notif_pref`; `PREF_GROUP` menggabungkan template feedback_* ke satu kunci `feedback`)
- `evidence.py` — PDF via Print Format + SHA-256 → File privat `portal_visible=1` + `Portal Evidence`
- `tasks.py` — scheduler: `send_notifications` (*/5 menit), `remind_client_tasks` (tiap jam, kirim pada `reminder_hour`), `draft_weekly` (Jumat 09:00), `close_confirmations`, `signoff_due_reminders` (harian)
- `events.py` — Task `before_save`: tipe Klien / milestone → `portal_visible=1`; Issue `on_update` → notifikasi feedback ke pelapor, `on_trash` → bersihkan komentar/audit tertaut
- `portal_bridge/doctype/` — 17 DocType (`Portal Meeting Topic` punya `resolution` "Solusi / Jawaban": solusi pembahasan ditulis langsung di baris topiknya, tampil di bawah pembahasan di portal dan PDF, bukan dikumpulkan di Keputusan) (`Portal Bridge Settings` [Single], `Portal Magic Link`, `Portal Audit Log`, `Portal Notification`, `Portal Evidence`, `Portal Weekly Update`, `Portal Meeting Note`, `Portal Comment`, `Portal Signoff` [submittable] + child table). JSON dihasilkan sekali dari generator; sekarang **edit JSON langsung** (atau lewat Desk dengan developer_mode lalu export)
- `demo.py` — data pilot sesuai mock `docs/design/Rencana.html`
- `tests/` — `factory.py` (2 tenant-klien × project), `test_auth.py`, `test_permissions.py`, `test_rules.py`, `test_feedback.py`

## Konvensi
- Nama DocType/field Inggris berawalan `Portal `/`portal_`; teks pesan/UI Indonesia.
- Klien **tidak pernah** punya DocPerm write pada DocType bawaan; tulis-menulis klien hanya lewat `api.py` setelah pemeriksaan, dengan `ignore_permissions=True`, dan efek samping sistem (buat Task, update Project, submit) di dalam `utils.elevated()`.
- Jangan teruskan `doc=doc` ke `frappe.get_print` sebelum `save()/submit()` (flag `in_print` membuat save diabaikan).
- Jangan pakai nama DocType yang sudah ada di Frappe core (`Portal Settings` milik Website → kita pakai `Portal Bridge Settings`).
- Query: `frappe.get_all` dengan `fields` eksplisit dan `order_by` eksplisit; filter `portal_visible = 1` ada di query, bukan disaring sesudahnya.
- Bisnis logic berisiko (auth, permission, aturan) test-first; jalankan `run-tests` sebelum PR. Uji tidak boleh `frappe.db.commit()` (dijaga `frappe.flags.in_test`).
- Site uji memakai `host_name = http://127.0.0.1:1113` agar wkhtmltopdf bisa memuat aset (domain publik lewat Cloudflare 502 dari server).

## Larangan
- Query SQL mentah ke tabel bawaan untuk logika bisnis (kecuali agregat sederhana), `override_doctype_class`, Server Script.
- Menyimpan token magic link mentah (hanya SHA-256), API key di repo.
- Mengubah DocType core Frappe/ERPNext.
