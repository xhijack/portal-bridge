"""Kanal pengiriman: WhatsApp (gateway HTTP, mis. Sopwer Inbox) dan email (Email Account site)."""

from __future__ import annotations

import re

import frappe
import requests

from portal_bridge.utils import settings


def normalize_phone(phone: str) -> str:
	digits = re.sub(r"\D", "", phone or "")
	if digits.startswith("0"):
		digits = "62" + digits[1:]
	return digits


def send_whatsapp(phone: str, text: str):
	s = settings()
	if not s.whatsapp_enabled or not s.whatsapp_base_url:
		raise RuntimeError("WhatsApp belum dikonfigurasi di Portal Settings")
	url = s.whatsapp_base_url.rstrip("/") + (s.whatsapp_send_path or "/api/send")
	token = s.get_password("whatsapp_token", raise_exception=False) if s.whatsapp_token else None
	headers = {"Content-Type": "application/json"}
	if token:
		headers["Authorization"] = f"Bearer {token}"
	payload = {
		"to": normalize_phone(phone),
		"text": text,
		"sender_label": s.whatsapp_sender_label or "Portal",
	}
	resp = requests.post(url, json=payload, headers=headers, timeout=15)
	if resp.status_code >= 300:
		raise RuntimeError(f"WA gateway {resp.status_code}: {resp.text[:200]}")
	return resp.json() if resp.content else {}


def send_email(email: str, subject: str, text: str):
	s = settings()
	frappe.sendmail(
		recipients=[email],
		sender=s.email_sender or None,
		subject=subject,
		message=text.replace("\n", "<br>"),
		delayed=False,
		retry=2,
	)
