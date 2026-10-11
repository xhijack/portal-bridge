"""Print Format Jinja untuk bukti PDF (sign-off dan notulen). Dibuat/diperbarui oleh setup()."""

_BASE_CSS = """
<style>
 body { font-family: Inter, Arial, sans-serif; color:#1F2937; font-size: 11px; }
 h1 { font-size: 18px; margin: 0 0 4px; color:#0B1220; }
 h2 { font-size: 13px; margin: 18px 0 6px; color:#154693; border-bottom:1px solid #E2E8F0; padding-bottom:3px; }
 .meta { color:#475569; font-size:10px; margin-bottom: 12px; }
 table { width:100%; border-collapse: collapse; margin: 6px 0; }
 th, td { border:1px solid #E2E8F0; padding:4px 6px; text-align:left; vertical-align: top; font-size:10.5px; }
 th { background:#F8FAFC; font-size:9px; letter-spacing:.08em; text-transform: uppercase; color:#475569; }
 .box { border:1px solid #93BDEC; background:#EAF3FC; padding:8px 10px; border-radius:6px; margin-top: 14px; }
 .mono { font-family: "JetBrains Mono", Menlo, monospace; font-size:9.5px; }
 .muted { color:#64748B; }
</style>
"""

SIGNOFF_HTML = (
	_BASE_CSS
	+ """
{% set project = frappe.get_doc("Project", doc.project) %}
<h1>{{ doc.kind }} — {{ doc.title }}</h1>
<div class="meta">
 {{ project.project_name }} · {{ project.customer or "" }} · No. {{ doc.name }} · versi dokumen {{ doc.doc_version }}
 · diajukan {{ frappe.utils.format_datetime(doc.requested_on) if doc.requested_on else "-" }}
</div>

<h2>Ringkasan</h2>
<div>{{ doc.summary or "-" }}</div>

{% if doc.kind == "Change Request" %}
<h2>Dampak</h2>
<table>
 <tr><th>Man-day</th><th>Timeline</th></tr>
 <tr><td>{{ doc.impact_man_days or 0 }}</td><td>{{ doc.impact_timeline_days or 0 }} hari</td></tr>
</table>
{% if doc.impact_invoices %}
<table>
 <tr><th>Termin</th><th>Jumlah</th></tr>
 {% for r in doc.impact_invoices %}<tr><td>{{ r.payment_term }}</td><td>{{ frappe.utils.fmt_money(r.amount, currency="IDR") }}</td></tr>{% endfor %}
</table>
{% endif %}
{% if doc.new_tasks %}
<table>
 <tr><th>Task baru</th><th>Tenggat</th><th>Jenis</th></tr>
 {% for r in doc.new_tasks %}<tr><td>{{ r.subject }}</td><td>{{ frappe.utils.formatdate(r.exp_end_date) if r.exp_end_date else "-" }}</td><td>{{ r.type or "-" }}</td></tr>{% endfor %}
</table>
{% endif %}
{% endif %}

<div class="box">
 <strong>Keputusan: {{ doc.status }}</strong><br>
 {% if doc.decided_name %}
 Oleh <strong>{{ doc.decided_name }}</strong> ({{ doc.decided_by_user }}) atas nama {{ project.customer or "klien" }}<br>
 Waktu {{ frappe.utils.format_datetime(doc.decided_on) }} · IP {{ doc.decided_ip or "-" }}<br>
 {% if doc.client_note %}Catatan: {{ doc.client_note }}<br>{% endif %}
 <span class="muted">Pernyataan: "Saya berwenang menyetujui dokumen ini atas nama {{ project.customer or "klien" }}" telah dicentang.</span>
 {% else %}
 <span class="muted">Belum diputuskan.</span>
 {% endif %}
</div>
<p class="mono muted">Dokumen ini dihasilkan otomatis oleh portal. Hash SHA-256 dicatat di Portal Evidence setelah PDF dibuat.</p>
"""
)

MEETING_NOTE_HTML = (
	_BASE_CSS
	+ """
{% set project = frappe.get_doc("Project", doc.project) %}
<h1>Notulen #{{ doc.seq }} — {{ doc.title }}</h1>
<div class="meta">
 {{ project.project_name }} · {{ project.customer or "" }} · {{ frappe.utils.formatdate(doc.meeting_date) }}
 {% if doc.start_time %}· {{ doc.start_time }}{% if doc.end_time %}–{{ doc.end_time }}{% endif %}{% endif %}
 {% if doc.location %}· {{ doc.location }}{% endif %} · versi {{ doc.version }} · status {{ doc.status }}
</div>

<h2>Hadir</h2>
<table><tr><th>Nama</th><th>Peran</th><th>Sisi</th><th>Hadir</th></tr>
{% for a in doc.attendees %}<tr><td>{{ a.full_name or a.user }}</td><td>{{ a.role or "" }}</td><td>{{ a.side }}</td><td>{{ "Ya" if a.present else "Tidak" }}</td></tr>{% endfor %}
</table>

<h2>Pembahasan</h2>
{% for t in doc.topics %}
<p><strong>{{ t.seq or loop.index }}. {{ t.title }}</strong><br>{{ t.body or "" }}</p>
{% endfor %}

<h2>Keputusan &amp; risiko</h2>
<table><tr><th>Kode</th><th>Isi</th><th>Diusulkan</th><th>Disetujui</th><th>Status</th></tr>
{% for d in doc.decisions %}<tr><td>{{ d.code or "" }}</td><td>{{ d.body }}</td><td>{{ d.proposed_by or "" }}</td><td>{{ d.approved_by or "" }}</td><td>{{ d.status }}</td></tr>{% endfor %}
</table>

<h2>Tindak lanjut</h2>
<table><tr><th>Tindakan</th><th>PIC</th><th>Tenggat</th><th>Task</th></tr>
{% for a in doc.actions %}<tr><td>{{ a.body }}</td><td>{{ frappe.db.get_value("User", a.assignee, "full_name") if a.assignee else "-" }}</td><td>{{ frappe.utils.formatdate(a.due) if a.due else "-" }}</td><td class="mono">{{ a.task or "" }}</td></tr>{% endfor %}
</table>

<div class="box">
 <strong>Konfirmasi klien: {{ doc.status }}</strong><br>
 {% if doc.confirmed_by %}Oleh {{ frappe.db.get_value("User", doc.confirmed_by, "full_name") }} ({{ doc.confirmed_by }}) pada {{ frappe.utils.format_datetime(doc.confirmed_on) }}{% else %}<span class="muted">Batas konfirmasi {{ frappe.utils.formatdate(doc.confirm_due) if doc.confirm_due else "-" }}</span>{% endif %}
</div>
"""
)

PRINT_FORMATS = {
	"Portal Signoff": {"doc_type": "Portal Signoff", "html": SIGNOFF_HTML},
	"Portal Meeting Note": {"doc_type": "Portal Meeting Note", "html": MEETING_NOTE_HTML},
}
