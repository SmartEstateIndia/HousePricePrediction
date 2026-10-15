import os
import time
import random
import urllib.parse
import requests
import streamlit.components.v1 as components
import json
import html
from pathlib import Path
import joblib
import pandas as pd
import numpy as np
import streamlit as st
import matplotlib.pyplot as plt
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timezone, timedelta
import math
import re
from io import BytesIO
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ===== APP PAGE CONFIG =====
st.set_page_config(
    page_title="SmartEstate | Premium Property Intelligence",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ==========================================
# 🔐 LOGIN / SIGN UP + GMAIL OTP
# ==========================================
# Local account storage is kept in users.json.
# Signup requires: Full Name + Mobile Number + Gmail + Password,
# then a one-time OTP is sent to the Gmail address before the account is created.
#
# Gmail sender configuration (Windows PowerShell):
#   $env:SMART_ESTATE_GMAIL="yourgmail@gmail.com"
#   $env:SMART_ESTATE_GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
#
# Use a Gmail App Password for SMTP (with 2-Step Verification enabled).
import hashlib
import secrets
import smtplib
import socket
import threading
from email.message import EmailMessage


class _SmartEstateSMTP(smtplib.SMTP):
    """SMTP client that forces IPv4 for networks where IPv6 SMTP is closed."""
    def _get_socket(self, host, port, timeout):
        if timeout is not None and timeout == 0:
            raise ValueError("Non-blocking SMTP connection is not supported")
        last_error = None
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        for family, socktype, proto, canonname, sockaddr in infos:
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(timeout)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                last_error = exc
                try:
                    sock.close()
                except Exception:
                    pass
        raise last_error or OSError("Unable to connect to SMTP server over IPv4")


class _SmartEstateSMTP_SSL(smtplib.SMTP_SSL):
    """SMTP-over-SSL client that forces IPv4 while preserving Gmail TLS SNI."""
    def _get_socket(self, host, port, timeout):
        if timeout is not None and timeout == 0:
            raise ValueError("Non-blocking SMTP connection is not supported")
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        last_error = None
        for family, socktype, proto, canonname, sockaddr in infos:
            try:
                raw = socket.socket(family, socktype, proto)
                raw.settimeout(timeout)
                raw.connect(sockaddr)
                return self.context.wrap_socket(raw, server_hostname=host)
            except OSError as exc:
                last_error = exc
                try:
                    raw.close()
                except Exception:
                    pass
        raise last_error or OSError("Unable to connect to Gmail SMTP over IPv4")


AUTH_FILE = Path(__file__).resolve().parent / "users.json"
AUTH_ITERATIONS = 220_000
OTP_TTL_SECONDS = 5 * 60
OTP_RESEND_COOLDOWN = 45
OTP_MAX_ATTEMPTS = 5
LOGIN_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
LOGIN_SESSION_MAX_PER_USER = 5
GMAIL_SMTP_SERVER = os.environ.get("SMART_ESTATE_SMTP_SERVER", "smtp.gmail.com")
GMAIL_SMTP_PORT = int(os.environ.get("SMART_ESTATE_SMTP_PORT", "465"))
GMAIL_SENDER = os.environ.get("SMART_ESTATE_GMAIL", "darjishailesh093@gmail.com").strip()
GMAIL_APP_PASSWORD = os.environ.get("SMART_ESTATE_GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

# Google Apps Script HTTPS email relay. This bypasses blocked Gmail SMTP ports.
SMART_ESTATE_APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbxH8aJ0aqDcAs945437p022EG--WOtUAiIl32X9suTPubHQo2HrefGlMO5j6GYXob_-jw/exec"
SMART_ESTATE_APPS_SCRIPT_TOKEN = "SmartEstate-Email-2026-10-05-7f4c9a2d"

def _send_via_apps_script(recipient, subject, html_body, text_body, delay_seconds=0):
    """Send mail through the user's Google Apps Script HTTPS relay."""
    recipient = str(recipient or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient):
        return False, "Invalid recipient email address."
    try:
        payload = {
            "token": SMART_ESTATE_APPS_SCRIPT_TOKEN,
            "to": recipient,
            "subject": subject,
            "html": html_body,
            "text": text_body,
            "delay_seconds": max(0, min(int(delay_seconds or 0), 30)),
        }
        response = requests.post(SMART_ESTATE_APPS_SCRIPT_URL, json=payload, timeout=45)
        data = response.json() if response.text else {}
        if response.ok and data.get("ok"):
            return True, str(data.get("message", "Email sent successfully."))
        err = data.get("error") or response.text[:500] or f"HTTP {response.status_code}"
        return False, str(err)
    except Exception as exc:
        return False, f"Apps Script connection failed: {type(exc).__name__}: {exc}"

def _gmail_runtime_config():
    """Read Gmail credentials from session, process env, .env, and Windows user env.
    Never prints or exposes the App Password.
    """
    sender = st.session_state.get("smartestate_gmail_sender") or os.environ.get("SMART_ESTATE_GMAIL", GMAIL_SENDER)
    app_password = st.session_state.get("smartestate_gmail_app_password") or os.environ.get("SMART_ESTATE_GMAIL_APP_PASSWORD", GMAIL_APP_PASSWORD)

    # Fallback for Windows `setx`: the current Streamlit process may have been
    # started before the variable was written to HKCU\Environment.
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            if not sender:
                sender = winreg.QueryValueEx(key, "SMART_ESTATE_GMAIL")[0]
            if not app_password:
                app_password = winreg.QueryValueEx(key, "SMART_ESTATE_GMAIL_APP_PASSWORD")[0]
    except Exception:
        pass

    # Fallback for a simple project-local .env file; no python-dotenv required.
    env_path = Path(__file__).resolve().parent / ".env"
    try:
        if env_path.exists():
            vals = {}
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('\"').strip("'")
            sender = sender or vals.get("SMART_ESTATE_GMAIL", "")
            app_password = app_password or vals.get("SMART_ESTATE_GMAIL_APP_PASSWORD", "")
    except Exception:
        pass

    sender = str(sender or "").strip().strip('\"').strip("'")
    app_password = str(app_password or "").replace(" ", "").strip().strip('\"').strip("'")
    return sender, app_password

# Premium residential photo used in branded OTP emails.
OTP_EMAIL_IMAGE_URL = "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?q=85&w=1200&auto=format&fit=crop"

# Real Gmail OTP sender settings.
# Configure these BEFORE starting Streamlit:
#   PowerShell:
#   $env:SMART_ESTATE_GMAIL="yourgmail@gmail.com"
#   $env:SMART_ESTATE_GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
#
# IMPORTANT: use a Google App Password, NOT the normal Gmail password.

def _gmail_sender_status():
    sender, app_password = _gmail_runtime_config()
    if not sender:
        return False, "Sender Gmail is not configured."
    if not sender.lower().endswith("@gmail.com"):
        return False, "SMART_ESTATE_GMAIL must be a Gmail address."
    if not app_password:
        return False, "Gmail App Password is not configured."
    if len(app_password) != 16:
        return False, f"Gmail App Password should contain 16 characters (currently {len(app_password)})."
    return True, sender


def _brevo_runtime_config():
    """Read Brevo HTTPS email configuration without exposing the API key."""
    api_key = os.environ.get("SMART_ESTATE_BREVO_API_KEY", "").strip()
    sender = os.environ.get("SMART_ESTATE_BREVO_SENDER", "").strip()
    sender_name = os.environ.get("SMART_ESTATE_BREVO_SENDER_NAME", "SmartEstate").strip() or "SmartEstate"
    try:
        env_path = Path(__file__).resolve().parent / ".env"
        if env_path.exists():
            vals = {}
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
            api_key = api_key or vals.get("SMART_ESTATE_BREVO_API_KEY", "")
            sender = sender or vals.get("SMART_ESTATE_BREVO_SENDER", "")
            sender_name = vals.get("SMART_ESTATE_BREVO_SENDER_NAME", sender_name) or sender_name
    except Exception:
        pass
    return api_key.strip(), sender.strip(), sender_name.strip()


def _send_inquiry_via_brevo(recipient, name, subject, html_body, text_body, delay_seconds=25):
    """Queue the email through Brevo's HTTPS API, avoiding blocked SMTP ports."""
    api_key, sender, sender_name = _brevo_runtime_config()
    if not api_key:
        return False, ""
    if not sender:
        sender = str(os.environ.get("SMART_ESTATE_GMAIL", GMAIL_SENDER) or "").strip()
    if not sender or "@" not in sender:
        return False, "Brevo sender email is not configured. Set SMART_ESTATE_BREVO_SENDER."
    try:
        delay = max(20, min(int(delay_seconds), 30))
    except Exception:
        delay = 25
    scheduled_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    payload = {
        "sender": {"name": sender_name, "email": sender},
        "to": [{"email": str(recipient).strip(), "name": str(name or "Customer").strip() or "Customer"}],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": text_body,
        "scheduledAt": scheduled_at,
        "tags": ["SmartEstate", "property-inquiry", "thank-you"],
    }
    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"accept": "application/json", "api-key": api_key, "content-type": "application/json"},
            json=payload,
            timeout=25,
        )
        if 200 <= response.status_code < 300:
            try:
                data = response.json()
                message_id = data.get("messageId", "")
            except Exception:
                message_id = ""
            return True, f"HTTPS email queued for about {delay} seconds." + (f" Message ID: {message_id}" if message_id else "")
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:500]
        error_text = f"Brevo HTTP {response.status_code}: {detail}"
        _write_inquiry_email_log(f"BREVO FAILED | {recipient} | {error_text}")
        return False, error_text
    except Exception as exc:
        error_text = f"Brevo HTTPS error: {type(exc).__name__}: {exc}"
        _write_inquiry_email_log(f"BREVO FAILED | {recipient} | {error_text}")
        return False, error_text

def _build_inquiry_email_content(recipient, name, phone, city, area_name, bhk, property_area, price, bathrooms=None, amenities=None, property_title=None):
    recipient = str(recipient or "").strip()
    name = str(name or "Customer").strip() or "Customer"
    safe_name = html.escape(name)
    safe_email = html.escape(recipient)
    safe_city = html.escape(str(city or ""))
    safe_area = html.escape(str(area_name or ""))
    safe_phone = html.escape(str(phone or "Not provided"))
    try:
        safe_bhk = html.escape(str(int(float(bhk))))
    except Exception:
        safe_bhk = html.escape(str(bhk or "-"))
    try:
        safe_property_area = f"{float(property_area):,.0f} sq.ft."
    except Exception:
        safe_property_area = f"{html.escape(str(property_area or '-'))} sq.ft."
    try:
        safe_price = format_price(float(price))
    except Exception:
        safe_price = html.escape(str(price or "Contact us"))
    safe_price = html.escape(str(safe_price))
    _brevo_api_key, _brevo_sender, _brevo_sender_name = _brevo_runtime_config()
    _display_sender = _brevo_sender or str(os.environ.get("SMART_ESTATE_GMAIL", GMAIL_SENDER) or "SmartEstate")
    safe_sender = html.escape(_display_sender)
    try:
        safe_bathrooms = html.escape(str(int(float(bathrooms)))) if bathrooms not in (None, "", "-") else "-"
    except Exception:
        safe_bathrooms = html.escape(str(bathrooms or "-"))
    amenity_labels = []
    if isinstance(amenities, dict):
        amenity_map = {
            "Gym": "🏋️ Gym",
            "Swimming Pool": "🏊 Swimming Pool",
            "Cinema Hall": "🎬 Cinema Hall",
            "Gaming Zone": "🎮 Gaming Zone",
            "Solar Balcony": "☀️ Solar Balcony",
        }
        for key, label in amenity_map.items():
            if bool(amenities.get(key)):
                amenity_labels.append(label)
    elif isinstance(amenities, (list, tuple, set)):
        amenity_labels = [html.escape(str(x)) for x in amenities if str(x).strip()]
    safe_property_title = html.escape(str(property_title or f"{safe_bhk} BHK Property • {area_name}, {city}"))
    amenities_text = ", ".join(amenity_labels) if amenity_labels else "Standard property configuration"
    amenities_html = "".join(f'<span style="display:inline-block;margin:4px 5px 0 0;padding:6px 9px;border-radius:999px;background:#fffdf8;border:1px solid #ead9bd;color:#72583b;font-size:11px;">{a}</span>' for a in amenity_labels) or '<span style="font-size:12px;color:#806f60;">Standard property configuration</span>'


    text_body = f"""Hello {name},

Thank you for your interest in SmartEstate!

We have successfully received your property inquiry. Our team will review your requirements and get back to you shortly with the relevant property details and assistance.

Property: {safe_property_title}
Location: {safe_area}, {safe_city}
BHK: {safe_bhk}
Bathrooms: {safe_bathrooms}
Area: {safe_property_area}
Estimated Price: {safe_price}
Amenities: {amenities_text}

Thank you for choosing SmartEstate for your home search.

SmartEstate Team
{_display_sender}
"""

    inquiry_id = f"SE-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
    html_body = f"""<!doctype html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f1eadf;font-family:Arial,Helvetica,sans-serif;color:#33271d;">
<div style="padding:32px 12px;background:linear-gradient(180deg,#f7f1e7 0%,#eee3d3 100%);">
  <div style="max-width:700px;margin:0 auto;background:#fffdf9;border:1px solid #d9c19c;border-radius:28px;overflow:hidden;box-shadow:0 24px 70px rgba(70,48,28,.18);">

    <!-- VIP HEADER -->
    <div style="background:linear-gradient(135deg,#2d1e15 0%,#4d3020 48%,#704a2b 100%);padding:34px 38px 30px;color:#fff;position:relative;">
      <div style="font-size:12px;font-weight:900;letter-spacing:4px;color:#e9bd6b;text-transform:uppercase;">SMART<span style="color:#fff4d9;">ESTATE</span></div>
      <div style="margin-top:22px;display:inline-block;padding:7px 13px;border:1px solid #dcb36a;border-radius:999px;color:#f2d18f;font-size:10px;font-weight:900;letter-spacing:2px;text-transform:uppercase;">VIP PROPERTY INQUIRY</div>
      <div style="font-size:34px;font-weight:900;line-height:1.12;margin-top:15px;color:#fffaf1;">Thank you for your inquiry.</div>
      <div style="font-size:14px;line-height:1.7;margin-top:11px;color:#eadcc8;max-width:590px;">Your request has been received successfully. Your selected property details are reserved in our inquiry record.</div>
    </div>

    <!-- GREETING -->
    <div style="padding:34px 38px 10px;">
      <div style="font-size:14px;color:#8a7159;">Hello <b style="color:#4b3524;">{safe_name}</b>,</div>
      <div style="font-size:26px;font-weight:900;color:#3e2c20;margin-top:8px;">Your property inquiry is confirmed.</div>
      <div style="font-size:14px;line-height:1.8;color:#756453;margin-top:10px;">Thank you for choosing SmartEstate. We have received your interest and our team can use the details below to assist you with the next step.</div>
    </div>

    <!-- PROPERTY CARD -->
    <div style="margin:24px 38px 0;border:1px solid #dfc69e;border-radius:22px;background:linear-gradient(145deg,#fffaf0,#f7ead6);overflow:hidden;">
      <div style="padding:15px 20px;background:linear-gradient(90deg,#7a512b,#b67b2d);color:#fff;font-size:10px;font-weight:900;letter-spacing:2px;text-transform:uppercase;">&#11088; Your selected property</div>
      <div style="padding:24px 22px;">
        <div style="font-size:22px;font-weight:900;color:#3f2c20;line-height:1.3;">{safe_property_title}</div>
        <div style="margin-top:9px;font-size:13px;color:#725d48;">&#128205; {safe_area}, {safe_city}</div>

        <div style="margin-top:18px;display:flex;flex-wrap:wrap;gap:8px;">
          <span style="display:inline-block;padding:9px 12px;border-radius:12px;background:#fffdf8;border:1px solid #e4d2b5;font-size:12px;color:#624a35;font-weight:700;">&#128105; {safe_bhk} BHK</span>
          <span style="display:inline-block;padding:9px 12px;border-radius:12px;background:#fffdf8;border:1px solid #e4d2b5;font-size:12px;color:#624a35;font-weight:700;">&#128703; {safe_bathrooms} Bathrooms</span>
          <span style="display:inline-block;padding:9px 12px;border-radius:12px;background:#fffdf8;border:1px solid #e4d2b5;font-size:12px;color:#624a35;font-weight:700;">&#128208; {safe_property_area}</span>
        </div>

        <div style="margin-top:22px;padding:17px 18px;border-radius:16px;background:#3f2b1f;">
          <div style="font-size:10px;font-weight:800;letter-spacing:1.7px;color:#dcb46d;text-transform:uppercase;">Estimated property value</div>
          <div style="font-size:30px;font-weight:900;color:#f0c66f;margin-top:4px;">{safe_price}</div>
        </div>

        <div style="margin-top:20px;">
          <div style="font-size:10px;font-weight:900;letter-spacing:1.5px;color:#9a6b2e;text-transform:uppercase;margin-bottom:9px;">Premium amenities</div>
          {amenities_html}
        </div>
      </div>
    </div>

    <!-- CUSTOMER DETAILS -->
    <div style="margin:20px 38px 0;padding:20px 22px;border-radius:18px;background:#f7f0e5;border:1px solid #eadbc5;">
      <div style="font-size:10px;font-weight:900;letter-spacing:1.6px;color:#9a6b2e;text-transform:uppercase;">Inquiry details</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;font-size:13px;color:#5f4d3b;">
        <tr><td style="padding:6px 0;font-weight:700;width:110px;">Name</td><td style="padding:6px 0;">{safe_name}</td></tr>
        <tr><td style="padding:6px 0;font-weight:700;">Email</td><td style="padding:6px 0;">{safe_email}</td></tr>
        <tr><td style="padding:6px 0;font-weight:700;">Mobile</td><td style="padding:6px 0;">{safe_phone}</td></tr>
        <tr><td style="padding:6px 0;font-weight:700;">Inquiry ID</td><td style="padding:6px 0;font-weight:800;color:#8b5d25;">{inquiry_id}</td></tr>
      </table>
    </div>

    <!-- NEXT STEP -->
    <div style="margin:20px 38px 30px;padding:20px 22px;border-radius:18px;background:linear-gradient(135deg,#fff8e9,#f5e4c7);border:1px solid #dfc18d;">
      <div style="font-size:16px;font-weight:900;color:#4b3524;">&#10003; What happens next?</div>
      <div style="font-size:13px;line-height:1.8;color:#75604c;margin-top:7px;">Our SmartEstate team will review your inquiry and contact you using the details provided. Keep this email for your property reference.</div>
    </div>

    <!-- FOOTER -->
    <div style="padding:22px 30px;background:#2f2118;text-align:center;color:#d9c6aa;">
      <div style="font-size:12px;font-weight:900;letter-spacing:2px;color:#e6bd6e;">SMART<span style="color:#fff1d4;">ESTATE</span></div>
      <div style="font-size:10px;margin-top:7px;color:#a9957e;">Premium Property Intelligence Platform</div>
      <div style="font-size:9px;margin-top:8px;color:#8e7b66;">This is an automated confirmation of your property inquiry.</div>
    </div>
  </div>
</div>
</body></html>"""
    return f"SmartEstate • Thank you for your inquiry, {name}", html_body, text_body

def _send_inquiry_thank_you_email(recipient, name, phone, city, area_name, bhk, property_area, price, bathrooms=None, amenities=None, delay_seconds=25, smtp_sender=None, smtp_password=None, property_title=None):
    recipient = str(recipient or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient):
        return False
    try:
        gmail_sender, gmail_app_password = (str(smtp_sender or "").strip(), str(smtp_password or "").replace(" ", "").strip()) if smtp_sender and smtp_password else _gmail_runtime_config()
        if not gmail_sender or not gmail_app_password:
            return False
        delay = max(20, min(int(delay_seconds), 30))
        time.sleep(delay)
        subject, html_body, text_body = _build_inquiry_email_content(recipient, name, phone, city, area_name, bhk, property_area, price, bathrooms, amenities, property_title)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = gmail_sender
        msg["To"] = recipient
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        last_exc = None
        for port_mode in ("ssl", "starttls"):
            try:
                if port_mode == "ssl":
                    with _SmartEstateSMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
                        server.ehlo(); server.login(gmail_sender, gmail_app_password); server.send_message(msg)
                else:
                    with _SmartEstateSMTP("smtp.gmail.com", 587, timeout=30) as server:
                        server.ehlo(); server.starttls(); server.ehlo(); server.login(gmail_sender, gmail_app_password); server.send_message(msg)
                return True
            except Exception as exc:
                last_exc = exc
        _write_inquiry_email_log(f"SMTP ERROR | {recipient} | {type(last_exc).__name__}: {last_exc}")
        return False
    except Exception as exc:
        _write_inquiry_email_log(f"SMTP ERROR | {recipient} | {type(exc).__name__}: {exc}")
        return False



def _schedule_inquiry_thank_you_email(recipient, name, phone, city, area_name, bhk, property_area, price, bathrooms=None, amenities=None, property_title=None):
    """Send the inquiry email through Google Apps Script over HTTPS."""
    try:
        subject, html_body, text_body = _build_inquiry_email_content(
            recipient, name, phone, city, area_name, bhk, property_area, price,
            bathrooms=bathrooms, amenities=amenities, property_title=property_title,
        )
        ok, status = _send_via_apps_script(
            recipient, subject, html_body, text_body, delay_seconds=25
        )
        if ok:
            return True, "Thank-you email queued. It will be sent in about 25 seconds."
        _write_inquiry_email_log(f"APPS SCRIPT INQUIRY FAILED | {recipient} | {status}")
        return False, f"Email service failed: {status}"
    except Exception as exc:
        status = f"{type(exc).__name__}: {exc}"
        _write_inquiry_email_log(f"APPS SCRIPT INQUIRY ERROR | {recipient} | {status}")
        return False, f"Email service failed: {status}"


def _write_inquiry_email_log(message):
    try:
        log_path = Path(__file__).resolve().parent / "inquiry_email_errors.log"
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now().isoformat()} | {message}\n")
    except Exception:
        pass


def _load_users():
    """Load accounts safely; recover from the backup if the main JSON is damaged."""
    candidates = [AUTH_FILE, AUTH_FILE.with_suffix(AUTH_FILE.suffix + ".bak")]
    for path in candidates:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            continue
    return {}


def _save_users(users):
    """Persist account data safely on Windows/OneDrive without breaking the app.

    OneDrive/Windows can temporarily lock users.json, which makes Path.replace()
    raise WinError 5. We retry the atomic replace and then fall back to a direct
    write. Account-history failures must never block PDF downloads or other UI.
    """
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(users, indent=2, ensure_ascii=False)
    tmp_path = AUTH_FILE.with_suffix(AUTH_FILE.suffix + ".tmp")
    bak_path = AUTH_FILE.with_suffix(AUTH_FILE.suffix + ".bak")

    try:
        tmp_path.write_text(payload, encoding="utf-8")
    except Exception:
        # If the temporary file itself is locked, try a unique local temp name.
        tmp_path = AUTH_FILE.with_name(AUTH_FILE.name + f".{os.getpid()}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")

    try:
        if AUTH_FILE.exists():
            import shutil
            try:
                shutil.copy2(AUTH_FILE, bak_path)
            except Exception:
                pass
    except Exception:
        pass

    # OneDrive may release the lock after a short delay.
    last_error = None
    for _attempt in range(4):
        try:
            tmp_path.replace(AUTH_FILE)
            return True
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.35)
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)

    # Final fallback: direct write to the existing file. This avoids crashing
    # report downloads when OneDrive briefly blocks an atomic rename.
    try:
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            f.write(payload)
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return True
    except Exception:
        # Do not let account-history persistence break the requested action.
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _normalize_email(email):
    return email.strip().lower()


def _normalize_phone(phone):
    raw = re.sub(r"\D", "", phone or "")
    if raw.startswith("91") and len(raw) == 12:
        raw = raw[2:]
    return raw


def _valid_indian_mobile(phone):
    return bool(re.fullmatch(r"[6-9]\d{9}", _normalize_phone(phone)))


def _account_record(email):
    email_key = _normalize_email(email or "")
    users = _load_users()
    record = users.get(email_key)
    return record if isinstance(record, dict) else None


def _token_digest(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _issue_login_session(email):
    """Create a persistent login session and store only a hash of its token."""
    email_key = _normalize_email(email)
    users = _load_users()
    record = users.get(email_key)
    if not isinstance(record, dict):
        raise RuntimeError("Account was not found.")

    tokens = record.get("login_tokens", {})
    if not isinstance(tokens, dict):
        tokens = {}

    now = time.time()
    clean = {}
    for token_hash, expiry in tokens.items():
        try:
            if float(expiry) > now:
                clean[str(token_hash)] = float(expiry)
        except Exception:
            continue

    raw_token = secrets.token_urlsafe(32)
    clean[_token_digest(raw_token)] = now + LOGIN_SESSION_TTL_SECONDS
    newest = sorted(clean.items(), key=lambda kv: kv[1], reverse=True)[:LOGIN_SESSION_MAX_PER_USER]
    record["login_tokens"] = dict(newest)
    users[email_key] = record
    _save_users(users)
    return raw_token


def _set_auth_query_token(raw_token):
    """Keep a persistent login token in the app URL so refresh does not log out."""
    try:
        st.query_params["se"] = raw_token
    except Exception:
        try:
            st.experimental_set_query_params(se=raw_token)
        except Exception:
            pass


def _get_auth_query_token():
    try:
        return str(st.query_params.get("se", "") or "").strip()
    except Exception:
        try:
            return str((st.experimental_get_query_params().get("se") or [""])[0] or "").strip()
        except Exception:
            return ""


def _clear_auth_query_token():
    try:
        if "se" in st.query_params:
            del st.query_params["se"]
    except Exception:
        try:
            params = st.experimental_get_query_params()
            params.pop("se", None)
            st.experimental_set_query_params(**params)
        except Exception:
            pass


def _restore_login_from_query():
    """Restore the logged-in account after a browser refresh."""
    if st.session_state.get("auth_user"):
        return True

    token = _get_auth_query_token()
    if not token:
        return False

    digest = _token_digest(token)
    users = _load_users()
    now = time.time()
    changed = False

    for email_key, record in users.items():
        if not isinstance(record, dict):
            continue
        tokens = record.get("login_tokens", {})
        if not isinstance(tokens, dict):
            continue

        clean = {}
        for token_hash, expiry in tokens.items():
            try:
                if float(expiry) > now:
                    clean[str(token_hash)] = float(expiry)
                else:
                    changed = True
            except Exception:
                changed = True

        if clean != tokens:
            record["login_tokens"] = clean
            users[email_key] = record

        if digest in clean:
            st.session_state.auth_user = {
                "email": _normalize_email(email_key),
                "name": record.get("name", "User"),
                "phone": record.get("phone", ""),
            }
            if changed:
                _save_users(users)
            return True

    if changed:
        _save_users(users)
    _clear_auth_query_token()
    return False


def _revoke_current_login_session():
    """Revoke the current browser token when the user explicitly logs out."""
    token = _get_auth_query_token()
    auth_user = st.session_state.get("auth_user") or {}
    email_key = _normalize_email(auth_user.get("email", ""))

    if token and email_key:
        users = _load_users()
        record = users.get(email_key)
        if isinstance(record, dict):
            tokens = record.get("login_tokens", {})
            if isinstance(tokens, dict):
                tokens.pop(_token_digest(token), None)
                record["login_tokens"] = tokens
                users[email_key] = record
                _save_users(users)

    _clear_auth_query_token()


def _ensure_account_collections(record):
    if not isinstance(record.get("favorites"), list):
        record["favorites"] = []
    if not isinstance(record.get("saved_searches"), list):
        record["saved_searches"] = []
    if not isinstance(record.get("recently_viewed"), list):
        record["recently_viewed"] = []
    if not isinstance(record.get("valuation_history"), list):
        record["valuation_history"] = []
    if not isinstance(record.get("comparison_history"), list):
        record["comparison_history"] = []
    if not isinstance(record.get("report_history"), list):
        record["report_history"] = []
    return record


def _persist_account_data(favorites=None, saved_searches=None, recently_viewed=None, valuation_history=None, comparison_history=None, report_history=None):
    """Persist current user's personal workspace into users.json immediately."""
    auth_user = st.session_state.get("auth_user") or {}
    email_key = _normalize_email(auth_user.get("email", ""))
    if not email_key:
        return

    users = _load_users()
    record = users.get(email_key)
    if not isinstance(record, dict):
        return
    record = _ensure_account_collections(record)
    if favorites is not None:
        record["favorites"] = favorites
    if saved_searches is not None:
        record["saved_searches"] = saved_searches
    if recently_viewed is not None:
        record["recently_viewed"] = recently_viewed
    if valuation_history is not None:
        record["valuation_history"] = valuation_history
    if comparison_history is not None:
        record["comparison_history"] = comparison_history
    if report_history is not None:
        record["report_history"] = report_history
    users[email_key] = record
    _save_users(users)


# ==========================================
# 🏠 PROPERTY INTELLIGENCE HELPERS
# ==========================================
def format_price(value_in_lakhs, decimals=2):
    mode = st.session_state.get("currency_mode", "Lakhs")
    try:
        v = float(value_in_lakhs)
    except (TypeError, ValueError):
        return "₹ 0"
    if mode == "Crores":
        return f"₹ {v/100:,.{decimals}f} Cr"
    if mode == "Full":
        return f"₹ {v*100000:,.0f}"
    return f"₹ {v:,.{decimals}f} L"

def _prop_key(p):
    if not isinstance(p, dict):
        return ""
    return f"{p.get('City','')}|{p.get('Area_Name','')}|{p.get('BHK','')}|{p.get('Area','')}"

def is_favorited(prop):
    key = _prop_key(prop)
    return any(_prop_key(f) == key for f in st.session_state.get("favorites", []) if isinstance(f, dict))

def toggle_favorite(prop):
    if not st.session_state.get("auth_user"):
        return _require_auth("save this property")
    st.session_state.setdefault("favorites", [])
    key = _prop_key(prop)
    if is_favorited(prop):
        st.session_state.favorites = [f for f in st.session_state.favorites if _prop_key(f) != key]
    else:
        prop_copy = dict(prop)
        prop_copy["saved_at"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
        st.session_state.favorites.append(prop_copy)
    _persist_account_data(favorites=[dict(x) for x in st.session_state.favorites])

def add_to_recent(prop):
    if not isinstance(prop, dict):
        return
    st.session_state.setdefault("recently_viewed", [])
    key = _prop_key(prop)
    st.session_state.recently_viewed = [
        r for r in st.session_state.recently_viewed
        if isinstance(r, dict) and _prop_key(r) != key
    ]
    prop_copy = dict(prop)
    prop_copy["viewed_at"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
    st.session_state.recently_viewed.insert(0, prop_copy)
    st.session_state.recently_viewed = st.session_state.recently_viewed[:10]
    if st.session_state.get("auth_user"):
        _persist_account_data(recently_viewed=[dict(x) for x in st.session_state.recently_viewed])

def get_similar_properties(df, city, area_name, bhk, price, limit=3):
    """Return a few comparable properties without external API calls."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    work = df.copy()
    if "City" in work.columns:
        same_city = work[work["City"].astype(str).str.lower() == str(city).lower()]
        if not same_city.empty:
            work = same_city
    if "Area_Name" in work.columns:
        same_area = work[work["Area_Name"].astype(str).str.lower() == str(area_name).lower()]
        if not same_area.empty:
            work = same_area
    if "BHK" in work.columns:
        bhk_match = work[pd.to_numeric(work["BHK"], errors="coerce") == float(bhk)]
        if not bhk_match.empty:
            work = bhk_match
    price_col = "Price" if "Price" in work.columns else ("Historical_Price" if "Historical_Price" in work.columns else None)
    if price_col is not None:
        work = work.copy()
        work["_price_diff"] = (pd.to_numeric(work[price_col], errors="coerce") - float(price)).abs()
        work = work.sort_values("_price_diff").drop(columns=["_price_diff"])
    return work.head(limit).copy()

def calculate_property_score(df, city, area_name, bhk, area, price, amenities=None, safety_score=8.2):
    """Calculate a transparent 0-100 property score from available app data."""
    amenities = amenities or {}
    try:
        area = max(float(area), 1.0)
        price = max(float(price), 0.0)
        bhk = max(float(bhk), 1.0)
    except (TypeError, ValueError):
        area, price, bhk = 1.0, 0.0, 1.0

    # Value: compare price/sqft with same-city data when available.
    value = 70.0
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "Price" in df.columns and "Area" in df.columns:
            work = df.copy()
            if "City" in work.columns:
                city_work = work[work["City"].astype(str).str.lower() == str(city).lower()]
                if not city_work.empty:
                    work = city_work
            p = pd.to_numeric(work["Price"], errors="coerce")
            a = pd.to_numeric(work["Area"], errors="coerce")
            ppsf = (p / a.replace(0, np.nan)).dropna()
            if len(ppsf):
                current_ppsf = price / area
                median_ppsf = float(ppsf.median())
                ratio = current_ppsf / median_ppsf if median_ppsf > 0 else 1.0
                value = float(np.clip(75 - (ratio - 1.0) * 45, 35, 95))
    except Exception:
        pass

    space = float(np.clip(45 + area / max(1.0, bhk) / 10.0, 45, 95))
    amenity_count = sum(1 for v in amenities.values() if bool(v))
    amenities_score = float(np.clip(45 + amenity_count * 11, 45, 100))
    safety = float(np.clip(float(safety_score), 0, 10) * 10)
    overall = float(np.clip(0.30 * safety + 0.30 * value + 0.20 * space + 0.20 * amenities_score, 0, 100))
    return {"overall": overall, "safety": safety, "value": value, "space": space, "amenities": amenities_score}

def calculate_smart_match_score(row, params, dataset=None):
    """Transparent SmartEstate match score (0-100) for Budget-First Search."""
    try:
        price = float(row.get("Price", 0))
        area = float(row.get("Area", 0))
        bhk = int(float(row.get("BHK", 0)))
    except Exception:
        return 0, {}

    target = max(float(params.get("target_budget", price) or price), 1.0)
    min_b = float(params.get("min_b", 0) or 0)
    max_b = float(params.get("max_b", 10**9) or 10**9)
    req_bhk = params.get("user_req_bhk", "Any")
    req_area = float(params.get("user_area", 0) or 0)
    req_bath = params.get("user_bath", "Any")

    # 30% budget fit: full score inside range, gradually lower near the edges.
    if min_b <= price <= max_b:
        budget_score = 100 - min(abs(price-target) / target * 45, 45)
    else:
        budget_score = max(0, 70 - min(abs(price-target) / target * 100, 70))

    bhk_score = 100 if req_bhk == "Any" else (100 if bhk == int(req_bhk) else max(0, 60 - abs(bhk-int(req_bhk))*25))

    if req_area <= 0:
        area_score = 100
    elif area >= req_area:
        area_score = min(100, 88 + min((area-req_area)/max(req_area,1)*12, 12))
    else:
        area_score = max(0, area/req_area*88)

    city_score = 100 if params.get("user_city", "All Cities") == "All Cities" else (100 if str(row.get("City")) == str(params.get("user_city")) else 0)
    locality_score = 100 if params.get("user_locality", "All Localities") == "All Localities" else (100 if str(row.get("Area_Name")) == str(params.get("user_locality")) else 0)
    location_score = 0.65 * city_score + 0.35 * locality_score

    requested_amenities = {
        "Gym": bool(params.get("gym")),
        "Swimming_Pool": bool(params.get("pool")),
        "Cinema_Hall": bool(params.get("cinema")),
        "Gaming_Zone": bool(params.get("gaming")),
        "Solar_Balcony": bool(params.get("solar")),
    }
    amen_scores = []
    for col, wanted in requested_amenities.items():
        if wanted:
            amen_scores.append(100 if int(float(row.get(col, 0) or 0)) == 1 else 0)
    amenity_score = sum(amen_scores)/len(amen_scores) if amen_scores else 100

    safety = float(SAFETY_SCORES.get(str(row.get("Area_Name", "")), 8.2)) * 10
    value_score = 75.0
    try:
        if isinstance(dataset, pd.DataFrame) and not dataset.empty and "Price" in dataset.columns and "Area" in dataset.columns:
            work = dataset
            if "City" in work.columns:
                cw = work[work["City"].astype(str) == str(row.get("City"))]
                if not cw.empty: work = cw
            ppsf = (pd.to_numeric(work["Price"], errors="coerce") / pd.to_numeric(work["Area"], errors="coerce").replace(0, np.nan)).dropna()
            if len(ppsf):
                current = price/max(area,1)
                median = float(ppsf.median())
                value_score = float(np.clip(75 - ((current/median)-1)*45, 35, 95)) if median > 0 else 75
    except Exception:
        pass

    weights = {"budget":.30,"bhk":.20,"area":.12,"location":.15,"amenities":.10,"safety":.05,"value":.08}
    total = (budget_score*weights["budget"] + bhk_score*weights["bhk"] + area_score*weights["area"] +
             location_score*weights["location"] + amenity_score*weights["amenities"] + safety*weights["safety"] + value_score*weights["value"])
    details = {"Budget":budget_score,"BHK":bhk_score,"Area":area_score,"Location":location_score,"Amenities":amenity_score,"Safety":safety,"Value":value_score}
    return int(np.clip(round(total), 0, 100)), details

def build_property_advisor(row, params, score, details):
    """Generate concise, explainable advisor reasons without an external AI/API call."""
    reasons, cautions = [], []
    price = float(row.get("Price", 0)); area = float(row.get("Area", 0)); bhk = int(float(row.get("BHK", 0)))
    target = float(params.get("target_budget", price) or price)
    if price <= target: reasons.append("Budget ke andar hai")
    else: cautions.append(f"Target budget se ₹{price-target:.1f}L upar hai")
    if params.get("user_req_bhk", "Any") == "Any" or bhk == int(params.get("user_req_bhk", bhk)): reasons.append(f"{bhk} BHK requirement match")
    if float(params.get("user_area", 0) or 0) <= 0 or area >= float(params.get("user_area", 0)): reasons.append(f"{area:,.0f} sq.ft. area requirement meet")
    if details.get("Amenities",100) >= 99 and any(params.get(k) for k in ["gym","pool","cinema","gaming","solar"]): reasons.append("Selected amenities available")
    if details.get("Safety",0) >= 82: reasons.append("Area safety profile strong")
    if details.get("Value",0) >= 75: reasons.append("Price/value profile balanced")
    if not reasons: reasons.append("Multiple selected preferences ke saath compatible")
    return reasons[:4], cautions[:2]

def _load_account_data_into_session(email=None):
    """Load persistent per-user data exactly once after login/signup."""
    auth_user = st.session_state.get("auth_user") or {}
    email_key = _normalize_email(email or auth_user.get("email", ""))
    if not email_key:
        return

    loaded_for = st.session_state.get("account_data_loaded_for")
    if loaded_for == email_key:
        return

    record = _account_record(email_key) or {}
    record = _ensure_account_collections(record)
    st.session_state.favorites = [dict(x) for x in record.get("favorites", []) if isinstance(x, dict)]
    st.session_state.saved_searches = [dict(x) for x in record.get("saved_searches", []) if isinstance(x, dict)]
    st.session_state.recently_viewed = [dict(x) for x in record.get("recently_viewed", []) if isinstance(x, dict)][:10]
    st.session_state.valuation_history = [dict(x) for x in record.get("valuation_history", []) if isinstance(x, dict)][:20]
    st.session_state.comparison_history = [dict(x) for x in record.get("comparison_history", []) if isinstance(x, dict)][:10]
    st.session_state.report_history = [dict(x) for x in record.get("report_history", []) if isinstance(x, dict)][:20]
    st.session_state.account_data_loaded_for = email_key


def _clear_account_data_from_session():
    st.session_state.account_data_loaded_for = None
    st.session_state.favorites = []
    st.session_state.saved_searches = []
    st.session_state.recently_viewed = []
    st.session_state.valuation_history = []
    st.session_state.comparison_history = []
    st.session_state.report_history = []


def _append_account_history(field_name, item, max_items=20):
    """Append a personal account event and persist it for the logged-in user."""
    if not st.session_state.get("auth_user"):
        return False
    collection = [dict(x) for x in st.session_state.get(field_name, []) if isinstance(x, dict)][-50:]
    signature = str(item.get("signature", ""))
    if signature and any(str(x.get("signature", "")) == signature for x in collection[:5]):
        return False
    collection.insert(0, dict(item))
    collection = collection[:max_items]
    st.session_state[field_name] = collection
    _persist_account_data(**{field_name: collection})
    return True


def record_valuation_history(city, area_name, bhk, area, price, confidence=None):
    item = {
        "City": city, "Area_Name": area_name, "BHK": int(bhk), "Area": float(area),
        "price": float(price), "confidence": float(confidence) if confidence is not None else None,
        "created_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
    }
    item["signature"] = f"{city}|{area_name}|{int(bhk)}|{round(float(area),0)}|{round(float(price),2)}"
    return _append_account_history("valuation_history", item, max_items=20)


def record_comparison_history(prop_a, prop_b):
    a = dict(prop_a); b = dict(prop_b)
    item = {
        "property_a": a, "property_b": b,
        "created_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
    }
    item["signature"] = f"{a.get('City','')}|{a.get('Area_Name','')}|{a.get('BHK','')}|{a.get('Area','')}||{b.get('City','')}|{b.get('Area_Name','')}|{b.get('BHK','')}|{b.get('Area','')}"
    return _append_account_history("comparison_history", item, max_items=10)


def record_report_history(title, file_name, property_info=None):
    property_info = dict(property_info or {})
    item = {
        "title": title, "file_name": file_name,
        "property": property_info,
        "created_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
    }
    item["signature"] = f"{file_name}|{property_info.get('Area_Name','')}|{datetime.now().strftime('%Y%m%d%H%M')}"
    return _append_account_history("report_history", item, max_items=20)


def _password_hash(password, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, AUTH_ITERATIONS)
    return salt.hex(), digest.hex()


def _verify_password(password, salt_hex, expected_hash):
    try:
        _, actual = _password_hash(password, salt_hex)
        return secrets.compare_digest(actual, expected_hash)
    except Exception:
        return False


def _gmail_otp_configured():
    # OTP delivery now uses the Google Apps Script HTTPS relay, not Gmail SMTP.
    return bool(SMART_ESTATE_APPS_SCRIPT_URL)


def _development_otp_allowed():
    return os.environ.get("SMART_ESTATE_DEV_OTP", "").strip().lower() in {"1", "true", "yes"}


def _send_gmail_otp(recipient, otp, name="User"):
    """Send a premium, branded signup OTP email to the recipient's Gmail."""
    recipient = _normalize_email(recipient)
    if not recipient.endswith("@gmail.com"):
        raise RuntimeError("OTP recipient must be a Gmail address ending with @gmail.com.")
    safe_name = html.escape((name or "User").strip())
    safe_otp = html.escape(str(otp))
    safe_sender = html.escape("housepriceprediction3@gmail.com")
    minutes = OTP_TTL_SECONDS // 60
    text_body = (
        f"Hello {name or 'User'},\n\n"
        f"Your SmartEstate verification OTP is: {otp}\n\n"
        f"This code expires in {minutes} minutes.\n"
        "If you did not request this code, you can ignore this email.\n\n"
        "SmartEstate"
    )
    html_body = f"""<!doctype html>
<html><body style="margin:0;background:#f6f1e8;font-family:Arial,Helvetica,sans-serif;color:#3f3328;">
<div style="padding:24px 10px;">
  <div style="max-width:640px;margin:0 auto;background:#fffdf9;border:1px solid #e6d8c4;border-radius:24px;overflow:hidden;box-shadow:0 18px 45px rgba(78,59,40,.12);">
    <div style="height:190px;background-image:linear-gradient(180deg,rgba(54,38,23,.08),rgba(54,38,23,.58)),url('{OTP_EMAIL_IMAGE_URL}');background-size:cover;background-position:center;">
      <div style="padding:24px 28px;color:#fff;">
        <div style="font-size:12px;font-weight:800;letter-spacing:2px;text-transform:uppercase;opacity:.92;">SMARTESTATE</div>
        <div style="font-size:30px;font-weight:900;margin-top:52px;">Secure your new home journey.</div>
      </div>
    </div>
    <div style="padding:30px 30px 24px;">
      <div style="font-size:13px;color:#8d7a65;">Hello {safe_name},</div>
      <div style="font-size:24px;font-weight:900;margin-top:7px;color:#4b392b;">Verify your email</div>
      <div style="font-size:14px;line-height:1.6;color:#7c6a58;margin-top:8px;">Use the one-time code below to complete your SmartEstate account verification.</div>
      <div style="margin:24px 0 18px;padding:18px 14px;border-radius:18px;background:#f4e7d2;border:1px solid #dfc8a5;text-align:center;">
        <div style="font-size:11px;font-weight:800;letter-spacing:2px;color:#8a6230;text-transform:uppercase;">YOUR OTP</div>
        <div style="font-size:38px;letter-spacing:9px;font-weight:900;color:#5b4028;margin-top:7px;">{safe_otp}</div>
      </div>
      <div style="font-size:12px;color:#8b7867;">This code expires in <b>{minutes} minutes</b>.</div>
      <div style="margin-top:24px;padding-top:18px;border-top:1px solid #eee4d6;font-size:11px;line-height:1.6;color:#9a8877;">If you did not request this verification, simply ignore this email.<br>Sent securely by {safe_sender}.</div>
    </div>
    <div style="padding:15px 30px;background:#f8f2e8;font-size:10px;color:#9b8976;text-align:center;">SmartEstate • Property Intelligence Platform</div>
  </div>
</div>
</body></html>"""
    ok, status = _send_via_apps_script(recipient, "SmartEstate • Your verification code", html_body, text_body, delay_seconds=0)
    if not ok:
        raise RuntimeError(f"Could not send verification OTP: {status}")


def _create_and_store_signup_otp(name, phone, email, password):
    now = time.time()
    existing = st.session_state.get("pending_signup_otp") or {}
    if existing.get("email") == email and now - float(existing.get("last_sent_at", 0)) < OTP_RESEND_COOLDOWN:
        remaining = max(1, int(OTP_RESEND_COOLDOWN - (now - float(existing.get("last_sent_at", 0)))))
        raise RuntimeError(f"Please wait {remaining} seconds before requesting another OTP.")

    otp = f"{secrets.randbelow(1_000_000):06d}"
    salt, digest = _password_hash(password)
    pending = {
        "name": name,
        "phone": _normalize_phone(phone),
        "email": email,
        "password_hash": digest,
        "salt": salt,
        "otp": otp,
        "expires_at": now + OTP_TTL_SECONDS,
        "last_sent_at": now,
        "attempts": 0,
    }
    if _gmail_otp_configured():
        _send_gmail_otp(email, otp, name)
        pending["delivery"] = "gmail"
    elif _development_otp_allowed():
        pending["delivery"] = "development"
    else:
        _, status = _gmail_sender_status()
        raise RuntimeError(f"Gmail OTP is not configured: {status} Set SMART_ESTATE_GMAIL and SMART_ESTATE_GMAIL_APP_PASSWORD, or set SMART_ESTATE_DEV_OTP=1 for local testing only.")
    st.session_state.pending_signup_otp = pending


def _resend_signup_otp():
    """Send a fresh OTP while preserving the pending signup password hash."""
    pending = st.session_state.get("pending_signup_otp") or {}
    email = _normalize_email(pending.get("email", ""))
    if not email:
        raise RuntimeError("No signup verification is currently pending.")

    now = time.time()
    last_sent = float(pending.get("last_sent_at", 0))
    if now - last_sent < OTP_RESEND_COOLDOWN:
        remaining = max(1, int(OTP_RESEND_COOLDOWN - (now - last_sent)))
        raise RuntimeError(f"Please wait {remaining} seconds before resending OTP.")

    otp = f"{secrets.randbelow(1_000_000):06d}"
    if _gmail_otp_configured():
        _send_gmail_otp(email, otp, pending.get("name", "User"))
        pending["delivery"] = "gmail"
    elif _development_otp_allowed():
        pending["delivery"] = "development"
    else:
        raise RuntimeError("Gmail OTP is not configured. Configure Gmail App Password first.")
    pending["otp"] = otp
    pending["expires_at"] = now + OTP_TTL_SECONDS
    pending["last_sent_at"] = now
    pending["attempts"] = 0
    st.session_state.pending_signup_otp = pending


def _verify_signup_otp(otp_input):
    pending = st.session_state.get("pending_signup_otp") or {}
    if not pending:
        return False, "Please request an OTP first."

    if time.time() > float(pending.get("expires_at", 0)):
        st.session_state.pop("pending_signup_otp", None)
        return False, "This OTP has expired. Please request a new OTP."

    attempts = int(pending.get("attempts", 0))
    if attempts >= OTP_MAX_ATTEMPTS:
        st.session_state.pop("pending_signup_otp", None)
        return False, "Too many incorrect attempts. Please request a new OTP."

    otp = re.sub(r"\D", "", otp_input or "")
    if not secrets.compare_digest(otp, str(pending.get("otp", ""))):
        pending["attempts"] = attempts + 1
        st.session_state.pending_signup_otp = pending
        remaining = max(0, OTP_MAX_ATTEMPTS - int(pending["attempts"]))
        return False, f"Incorrect OTP. {remaining} attempt(s) remaining."

    return True, "OTP verified."


def _send_password_reset_otp(recipient, otp, name="User"):
    """Send a premium password-reset OTP email to the registered Gmail."""
    recipient = _normalize_email(recipient)
    if not recipient.endswith("@gmail.com"):
        raise RuntimeError("Password reset requires the registered Gmail address.")
    safe_name = html.escape((name or "User").strip())
    safe_otp = html.escape(str(otp))
    safe_sender = html.escape("housepriceprediction3@gmail.com")
    minutes = OTP_TTL_SECONDS // 60
    text_body = (
        f"Hello {name or 'User'},\n\n"
        f"Your SmartEstate password-reset OTP is: {otp}\n\n"
        f"This code expires in {minutes} minutes.\n"
        "If you did not request a password reset, you can safely ignore this email.\n\n"
        "SmartEstate"
    )
    html_body = f"""<!doctype html>
<html><body style="margin:0;background:#f6f1e8;font-family:Arial,Helvetica,sans-serif;color:#3f3328;">
<div style="padding:24px 10px;">
  <div style="max-width:640px;margin:0 auto;background:#fffdf9;border:1px solid #e6d8c4;border-radius:24px;overflow:hidden;box-shadow:0 18px 45px rgba(78,59,40,.12);">
    <div style="height:190px;background-image:linear-gradient(180deg,rgba(54,38,23,.08),rgba(54,38,23,.58)),url('{OTP_EMAIL_IMAGE_URL}');background-size:cover;background-position:center;">
      <div style="padding:24px 28px;color:#fff;">
        <div style="font-size:12px;font-weight:800;letter-spacing:2px;text-transform:uppercase;opacity:.92;">SMARTESTATE</div>
        <div style="font-size:30px;font-weight:900;margin-top:52px;">Reset your account securely.</div>
      </div>
    </div>
    <div style="padding:30px 30px 24px;">
      <div style="font-size:13px;color:#8d7a65;">Hello {safe_name},</div>
      <div style="font-size:24px;font-weight:900;margin-top:7px;color:#4b392b;">Password reset requested</div>
      <div style="font-size:14px;line-height:1.6;color:#7c6a58;margin-top:8px;">Use this one-time code to create a new password for your SmartEstate account.</div>
      <div style="margin:24px 0 18px;padding:18px 14px;border-radius:18px;background:#f4e7d2;border:1px solid #dfc8a5;text-align:center;">
        <div style="font-size:11px;font-weight:800;letter-spacing:2px;color:#8a6230;text-transform:uppercase;">RESET OTP</div>
        <div style="font-size:38px;letter-spacing:9px;font-weight:900;color:#5b4028;margin-top:7px;">{safe_otp}</div>
      </div>
      <div style="font-size:12px;color:#8b7867;">This code expires in <b>{minutes} minutes</b>.</div>
      <div style="margin-top:24px;padding-top:18px;border-top:1px solid #eee4d6;font-size:11px;line-height:1.6;color:#9a8877;">If you did not request this reset, no action is required.<br>Sent securely by {safe_sender}.</div>
    </div>
    <div style="padding:15px 30px;background:#f8f2e8;font-size:10px;color:#9b8976;text-align:center;">SmartEstate • Property Intelligence Platform</div>
  </div>
</div>
</body></html>"""
    ok, status = _send_via_apps_script(recipient, "SmartEstate • Password reset OTP", html_body, text_body, delay_seconds=0)
    if not ok:
        raise RuntimeError(f"Could not send reset OTP: {status}")


def _create_and_store_password_reset_otp(email):
    """Create and email a password-reset OTP for an existing account."""
    email_key = _normalize_email(email)
    record = _account_record(email_key)
    if not record:
        st.session_state.pop("pending_password_reset_otp", None)
        return False, "If an account exists for that Gmail, a reset OTP has been sent."

    now = time.time()
    existing = st.session_state.get("pending_password_reset_otp") or {}
    if existing.get("email") == email_key and now - float(existing.get("last_sent_at", 0)) < OTP_RESEND_COOLDOWN:
        remaining = max(1, int(OTP_RESEND_COOLDOWN - (now - float(existing.get("last_sent_at", 0)))))
        return False, f"Please wait {remaining} seconds before requesting another OTP."

    otp = f"{secrets.randbelow(1_000_000):06d}"
    if _gmail_otp_configured():
        _send_password_reset_otp(email_key, otp, record.get("name", "User"))
        delivery = "gmail"
    elif _development_otp_allowed():
        delivery = "development"
    else:
        _, status = _gmail_sender_status()
        raise RuntimeError(f"Gmail OTP is not configured: {status} Set SMART_ESTATE_GMAIL and SMART_ESTATE_GMAIL_APP_PASSWORD, or set SMART_ESTATE_DEV_OTP=1 for local testing only.")
    st.session_state.pending_password_reset_otp = {
        "email": email_key,
        "otp": otp,
        "expires_at": now + OTP_TTL_SECONDS,
        "last_sent_at": now,
        "attempts": 0,
        "delivery": delivery,
    }
    if delivery == "development":
        return True, f"Development OTP generated: {otp}. Local testing only."
    return True, f"OTP sent to {email_key}. Check your Gmail inbox/spam folder."


def _resend_password_reset_otp():
    pending = st.session_state.get("pending_password_reset_otp") or {}
    email_key = _normalize_email(pending.get("email", ""))
    if not email_key:
        raise RuntimeError("No password-reset request is currently pending.")

    record = _account_record(email_key)
    if not record:
        st.session_state.pop("pending_password_reset_otp", None)
        raise RuntimeError("This reset request is no longer valid. Please start again.")

    now = time.time()
    last_sent = float(pending.get("last_sent_at", 0))
    if now - last_sent < OTP_RESEND_COOLDOWN:
        remaining = max(1, int(OTP_RESEND_COOLDOWN - (now - last_sent)))
        raise RuntimeError(f"Please wait {remaining} seconds before resending OTP.")

    otp = f"{secrets.randbelow(1_000_000):06d}"
    if _gmail_otp_configured():
        _send_password_reset_otp(email_key, otp, record.get("name", "User"))
        pending["delivery"] = "gmail"
    elif _development_otp_allowed():
        pending["delivery"] = "development"
    else:
        raise RuntimeError("Gmail OTP is not configured. Configure Gmail App Password first.")
    pending.update({
        "otp": otp,
        "expires_at": now + OTP_TTL_SECONDS,
        "last_sent_at": now,
        "attempts": 0,
    })
    st.session_state.pending_password_reset_otp = pending


def _verify_password_reset_otp(otp_input):
    pending = st.session_state.get("pending_password_reset_otp") or {}
    if not pending:
        return False, "Please request a password-reset OTP first."

    if time.time() > float(pending.get("expires_at", 0)):
        st.session_state.pop("pending_password_reset_otp", None)
        return False, "This OTP has expired. Please request a new one."

    attempts = int(pending.get("attempts", 0))
    if attempts >= OTP_MAX_ATTEMPTS:
        st.session_state.pop("pending_password_reset_otp", None)
        return False, "Too many incorrect attempts. Please request a new OTP."

    otp = re.sub(r"\D", "", otp_input or "")
    if not secrets.compare_digest(otp, str(pending.get("otp", ""))):
        pending["attempts"] = attempts + 1
        st.session_state.pending_password_reset_otp = pending
        remaining = max(0, OTP_MAX_ATTEMPTS - int(pending["attempts"]))
        return False, f"Incorrect OTP. {remaining} attempt(s) remaining."

    return True, "OTP verified."


def _set_account_password(email, new_password):
    email_key = _normalize_email(email)
    users = _load_users()
    record = users.get(email_key)
    if not isinstance(record, dict):
        raise RuntimeError("Account was not found. Please start the reset process again.")
    salt, digest = _password_hash(new_password)
    record["salt"] = salt
    record["password_hash"] = digest
    record["password_updated_at"] = datetime.now().isoformat(timespec="seconds")
    record["login_tokens"] = {}
    users[email_key] = record
    _save_users(users)


def _update_account_profile(email, name, phone):
    """Update editable profile fields while keeping Gmail as the account identifier."""
    email_key = _normalize_email(email)
    users = _load_users()
    record = users.get(email_key)
    if not isinstance(record, dict):
        raise RuntimeError("Account was not found.")

    clean_name = str(name or "").strip()
    clean_phone = _normalize_phone(phone)
    if len(clean_name) < 2:
        raise ValueError("Please enter a valid full name.")
    if not _valid_indian_mobile(clean_phone):
        raise ValueError("Please enter a valid 10-digit Indian mobile number.")

    for other_email, other_record in users.items():
        if other_email != email_key and isinstance(other_record, dict):
            if _normalize_phone(other_record.get("phone", "")) == clean_phone:
                raise ValueError("This mobile number is already registered to another account.")

    record["name"] = clean_name
    record["phone"] = clean_phone
    record["profile_updated_at"] = datetime.now().isoformat(timespec="seconds")
    users[email_key] = record
    _save_users(users)
    st.session_state.auth_user = {
        "email": email_key,
        "name": clean_name,
        "phone": clean_phone,
    }


def _auth_css():
    st.markdown("""
    <style>
      [data-testid="stAppViewContainer"] { background:linear-gradient(145deg,#fbf7ef 0%,#f4eadb 58%,#eee0cc 100%) !important; }
      [data-testid="stSidebar"] { display:none !important; }
      .auth-wrap { min-height:78vh; display:flex; align-items:center; justify-content:center; padding:42px 0 24px; }
      .auth-card { width:min(520px,94vw); background:rgba(255,255,255,.96); border:1px solid rgba(138,90,24,.16); border-radius:28px; padding:30px 34px; box-shadow:0 28px 70px rgba(92,65,37,.16); backdrop-filter:blur(10px); }
      .auth-brand { display:flex; align-items:center; gap:12px; margin-bottom:18px; }
      .auth-logo { width:46px; height:46px; border-radius:14px; display:grid; place-items:center; background:linear-gradient(135deg,#e4bd73,#b87922); color:#fff; font-size:24px; box-shadow:0 10px 20px rgba(184,121,34,.23); }
      .auth-brand-name { font-size:23px; font-weight:900; letter-spacing:-.3px; color:#4e3424; }
      .auth-brand-name span { color:#b67925; }
      .auth-kicker { font-size:11px; letter-spacing:2.2px; text-transform:uppercase; font-weight:900; color:#a27334; margin-bottom:7px; }
      .auth-title { font-size:30px; line-height:1.08; font-weight:900; color:#3f2b1f; margin:0 0 8px; }
      .auth-sub { color:#7d6b5b; font-size:14px; line-height:1.6; margin-bottom:18px; }
      .auth-note { text-align:center; color:#8c7967; font-size:11px; margin-top:16px; }
      .otp-chip { display:inline-block; background:#f6ead6; color:#7b531e; border:1px solid #e0c79b; border-radius:999px; padding:7px 12px; font-size:12px; font-weight:800; margin:3px 0 10px; }
      div[data-testid="stForm"] { border:0 !important; padding:0 !important; }
      .stButton > button, .stFormSubmitButton > button { border-radius:14px !important; border:1px solid #b87922 !important; background:linear-gradient(135deg,#d9a756,#bd8230) !important; color:#fff !important; font-weight:800 !important; min-height:46px !important; box-shadow:0 10px 22px rgba(189,130,48,.18); }
      .stTextInput input { border-radius:12px !important; background:#fffdf9 !important; border:1px solid rgba(113,80,46,.17) !important; }
    </style>
    """, unsafe_allow_html=True)


def _render_gmail_setup_box():
    with st.expander("🧪 Gmail OTP Setup / Test", expanded=True):
        st.caption("Windows environment variables are not being detected. Enter your Gmail App Password here for this session only. Do not send the password in chat.")
        sender = st.text_input("Gmail", value=st.session_state.get("smartestate_gmail_sender", "darjishailesh093@gmail.com"), key="smartestate_gmail_sender_input")
        app_pw = st.text_input("Google App Password", type="password", key="smartestate_gmail_app_password_input", placeholder="16-character App Password")
        if st.button("Save & Test Gmail", key="smartestate_save_test_gmail", use_container_width=True):
            st.session_state.smartestate_gmail_sender = sender.strip()
            st.session_state.smartestate_gmail_app_password = app_pw.replace(" ", "").strip()
            ok, status = _gmail_sender_status()
            if not ok:
                st.error(status)
            else:
                try:
                    gmail_sender, gmail_app_password = _gmail_runtime_config()
                    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
                        server.login(gmail_sender, gmail_app_password)
                    st.success("✅ Gmail SMTP connected. Now request OTP.")
                except Exception as exc:
                    st.error(f"❌ Gmail login failed: {exc}")


def _render_auth_screen():
    _render_gmail_setup_box()
    _auth_css()
    users = _load_users()
    tab_login, tab_signup = st.tabs(["🔐 Login", "✨ Sign Up"] )

    with tab_login:
        st.markdown("""
        <div class="auth-card">
          <div class="auth-brand"><div class="auth-logo">🏠</div><div class="auth-brand-name">Smart<span>Estate</span></div></div>
          <div class="auth-kicker">Welcome back</div>
          <div class="auth-title">Login to your account</div>
          <div class="auth-sub">Use your Gmail and password to access your saved favorites, searches and valuations.</div>
        </div>
        """, unsafe_allow_html=True)
        with st.form("login_form", clear_on_submit=False):
            email = st.text_input("Gmail Address", placeholder="you@gmail.com", key="auth_login_email")
            password = st.text_input("Password", type="password", placeholder="Enter your password", key="auth_login_password")
            submitted = st.form_submit_button("🔓 Login", use_container_width=True)
        if st.button("Forgot Password?", key="fallback_forgot_password_button", use_container_width=True):
            st.session_state.auth_forgot_open = True
            st.rerun()

        if submitted:
            email_key = _normalize_email(email)
            record = users.get(email_key)
            if record and _verify_password(password, record.get("salt", ""), record.get("password_hash", "")):
                st.session_state.auth_user = {
                    "email": email_key,
                    "name": record.get("name", "User"),
                    "phone": record.get("phone", ""),
                }
                _set_auth_query_token(_issue_login_session(email_key))
                st.session_state.auth_notice = ""
                st.session_state.pop("pending_signup_otp", None)
                _load_account_data_into_session(email_key)
                st.rerun()
            else:
                st.error("Invalid Gmail or password.")

    if st.session_state.get("auth_forgot_open"):
        st.markdown("### 🔑 Forgot Password")
        st.caption("Enter your registered Gmail to receive a reset OTP.")
        with st.form("fallback_forgot_request_form", clear_on_submit=False):
            reset_email = st.text_input("Registered Gmail", placeholder="you@gmail.com", key="fallback_forgot_email")
            request_reset = st.form_submit_button("📩 Send Reset OTP", use_container_width=True)
        if request_reset:
            email_key = _normalize_email(reset_email)
            if not email_key.endswith("@gmail.com"):
                st.error("Please enter your registered Gmail address.")
            else:
                try:
                    sent, msg = _create_and_store_password_reset_otp(email_key)
                    (st.success if sent else st.info)(msg)
                except Exception as exc:
                    st.error(str(exc))
        pending_reset = st.session_state.get("pending_password_reset_otp") or {}
        if pending_reset and time.time() <= float(pending_reset.get("expires_at", 0)):
            if pending_reset.get("delivery") == "development":
                st.warning(f"Development OTP: {pending_reset.get('otp')} (local testing only)")
            with st.form("fallback_forgot_verify_form", clear_on_submit=False):
                ro = st.text_input("6-digit OTP", max_chars=6, key="fallback_forgot_otp")
                np = st.text_input("New Password", type="password", key="fallback_new_password")
                cp = st.text_input("Confirm New Password", type="password", key="fallback_confirm_password")
                reset_ok = st.form_submit_button("✅ Verify & Reset Password", use_container_width=True)
            if reset_ok:
                if len(np) < 6:
                    st.error("New password must be at least 6 characters.")
                elif np != cp:
                    st.error("Passwords do not match.")
                else:
                    ok, msg = _verify_password_reset_otp(ro)
                    if ok:
                        _set_account_password(pending_reset.get("email", ""), np)
                        st.session_state.pop("pending_password_reset_otp", None)
                        st.session_state.auth_forgot_open = False
                        st.success("Password reset successfully. Please login again.")
                    else:
                        st.error(msg)
        if st.button("← Back to Login", key="fallback_back_login", use_container_width=True):
            st.session_state.auth_forgot_open = False
            st.rerun()

    with tab_signup:
        st.markdown("""
        <div class="auth-card">
          <div class="auth-brand"><div class="auth-logo">🏠</div><div class="auth-brand-name">Smart<span>Estate</span></div></div>
          <div class="auth-kicker">Create account</div>
          <div class="auth-title">Start your home journey</div>
          <div class="auth-sub">Enter your mobile number and Gmail. We will send a 6-digit OTP to your Gmail before creating the account.</div>
        </div>
        """, unsafe_allow_html=True)

        with st.form("signup_send_otp_form", clear_on_submit=False):
            s1, s2 = st.columns(2)
            with s1:
                name = st.text_input("Full Name", placeholder="Your name", key="auth_signup_name")
                phone = st.text_input("Mobile Number", placeholder="9876543210", key="auth_signup_phone")
            with s2:
                email = st.text_input("Gmail Address", placeholder="you@gmail.com", key="auth_signup_email")
                password = st.text_input("Password", type="password", placeholder="Minimum 6 characters", key="auth_signup_password")
            confirm = st.text_input("Confirm Password", type="password", placeholder="Re-enter password", key="auth_signup_confirm")
            send_otp = st.form_submit_button("📩 Send OTP to Gmail", use_container_width=True)

        if send_otp:
            name = name.strip()
            email_key = _normalize_email(email)
            phone_key = _normalize_phone(phone)
            if not name:
                st.error("Please enter your name.")
            elif not _valid_indian_mobile(phone):
                st.error("Please enter a valid 10-digit Indian mobile number.")
            elif not email_key.endswith("@gmail.com"):
                st.error("Please use a Gmail address ending with @gmail.com.")
            elif len(password) < 6:
                st.error("Password must be at least 6 characters.")
            elif password != confirm:
                st.error("Passwords do not match.")
            elif email_key in users:
                st.error("An account with this Gmail already exists. Please log in.")
            elif any(isinstance(r, dict) and _normalize_phone(r.get("phone", "")) == phone_key for r in users.values()):
                st.error("This mobile number is already registered.")
            elif not _gmail_otp_configured():
                _, gmail_status = _gmail_sender_status()
                st.error(f"Real Gmail OTP is not ready: {gmail_status} Set SMART_ESTATE_GMAIL and SMART_ESTATE_GMAIL_APP_PASSWORD, then restart Streamlit.")
            else:
                try:
                    _create_and_store_signup_otp(name, phone_key, email_key, password)
                    pending_now = st.session_state.get("pending_signup_otp") or {}
                    if pending_now.get("delivery") == "development":
                        st.warning(f"Development OTP: {pending_now.get('otp')} (local testing only)")
                    else:
                        st.success(f"OTP sent to {email_key}. Check your Gmail inbox/spam folder.")
                except Exception as exc:
                    st.error(f"Could not send OTP: {exc}")

        pending = st.session_state.get("pending_signup_otp") or {}
        if pending:
            if time.time() <= float(pending.get("expires_at", 0)):
                st.markdown(f'<div class="otp-chip">📬 OTP sent to {html.escape(pending.get("email", ""))}</div>', unsafe_allow_html=True)
                if pending.get("delivery") == "development":
                    st.warning(f"Development OTP: {pending.get('otp')} (local testing only)")
                with st.form("signup_verify_otp_form", clear_on_submit=False):
                    otp_value = st.text_input("Enter 6-digit OTP", placeholder="123456", max_chars=6, key="auth_signup_otp")
                    verify_otp = st.form_submit_button("✅ Verify OTP & Create Account", use_container_width=True)
                if verify_otp:
                    ok, msg = _verify_signup_otp(otp_value)
                    if ok:
                        p = st.session_state.get("pending_signup_otp", {})
                        users = _load_users()
                        email_key = _normalize_email(p.get("email", ""))
                        users[email_key] = {
                            "name": p.get("name", "User"),
                            "phone": p.get("phone", ""),
                            "salt": p.get("salt", ""),
                            "password_hash": p.get("password_hash", ""),
                            "created_at": datetime.now().isoformat(timespec="seconds"),
                            "email_verified": True,
                            "favorites": [],
                            "saved_searches": [],
                            "recently_viewed": [],
                            "valuation_history": [],
                            "comparison_history": [],
                            "report_history": [],
                        }
                        _save_users(users)
                        st.session_state.auth_user = {
                            "email": email_key,
                            "name": p.get("name", "User"),
                            "phone": p.get("phone", ""),
                        }
                        _set_auth_query_token(_issue_login_session(email_key))
                        st.session_state.auth_notice = "Email verified and account created successfully."
                        st.session_state.pop("pending_signup_otp", None)
                        _load_account_data_into_session(email_key)
                        st.rerun()
                    else:
                        st.error(msg)

                # Resend is intentionally outside the verify form so it can issue a fresh code.
                if st.button("🔁 Resend OTP", key="resend_signup_otp", use_container_width=True):
                    try:
                        _resend_signup_otp()
                        st.success("A new OTP has been sent to your Gmail.")
                    except Exception as exc:
                        st.error(str(exc))
            else:
                st.session_state.pop("pending_signup_otp", None)
                st.warning("OTP expired. Please fill the signup form and request a new OTP.")

    st.markdown('<div class="auth-note">Gmail verification is required for new accounts. OTPs are sent from your configured Gmail account to the Gmail entered by the user. Account data is stored locally in this Streamlit project.</div>', unsafe_allow_html=True)


if "auth_user" not in st.session_state:
    st.session_state.auth_user = None
if "pending_signup_otp" not in st.session_state:
    st.session_state.pending_signup_otp = None
if "pending_password_reset_otp" not in st.session_state:
    st.session_state.pending_password_reset_otp = None
if "auth_modal_open" not in st.session_state:
    st.session_state.auth_modal_open = False
if "auth_prompt" not in st.session_state:
    st.session_state.auth_prompt = "continue"
if "auth_mode" not in st.session_state:
    st.session_state.auth_mode = "Login"
if "auth_forgot_open" not in st.session_state:
    st.session_state.auth_forgot_open = False
if "account_edit_mode" not in st.session_state:
    st.session_state.account_edit_mode = False
if "auth_restored_from_query" not in st.session_state:
    st.session_state.auth_restored_from_query = False

# Restore persistent login before protected data/account UI is rendered.
_restore_login_from_query()


def _open_auth_prompt(action="continue"):
    st.session_state.auth_modal_open = True
    st.session_state.auth_prompt = action


def _require_auth(action="continue"):
    if st.session_state.get("auth_user"):
        return True
    _open_auth_prompt(action)
    st.rerun()
    return False


def _render_auth_dialog():
    # Mobile/older-Streamlit fallback: render the same working auth UI as a
    # full page instead of failing when st.dialog is unavailable.
    if not hasattr(st, "dialog"):
        _render_auth_screen()
        return

    @st.dialog("SmartEstate • Account", width="small")
    def _dialog():
        prompt = html.escape(str(st.session_state.get("auth_prompt", "continue")))
        st.markdown(
            f'''<div style="text-align:center;padding:0 4px 8px;">
                <div style="font-size:34px;line-height:1;">🏠</div>
                <div style="font-size:22px;font-weight:900;color:#5D432C;margin-top:7px;">Smart<span style="color:#DAA752;">Estate</span></div>
                <div style="font-size:12px;color:#8B7765;margin-top:4px;">Login only when you save a property or download a PDF.</div>
            </div>''',
            unsafe_allow_html=True,
        )
        if prompt and prompt != "continue":
            st.info(f"🔐 Please login or sign up to {prompt}.")

        if st.session_state.get("auth_forgot_open"):
            st.markdown(
                '<div style="padding:4px 0 10px;">'
                '<div style="font-size:20px;font-weight:900;color:#5D432C;">Reset your password</div>'
                '<div style="font-size:12px;color:#8B7765;margin-top:4px;">'
                'Enter your registered Gmail. A one-time OTP will be sent there.</div>'
                '</div>',
                unsafe_allow_html=True,
            )

            if st.button("← Back to Login", key="forgot_back_login", use_container_width=True):
                st.session_state.auth_forgot_open = False
                st.session_state.pop("pending_password_reset_otp", None)
                st.rerun()

            with st.form("forgot_password_request_form", clear_on_submit=False):
                reset_email = st.text_input("Registered Gmail", placeholder="you@gmail.com", key="forgot_password_email")
                request_reset = st.form_submit_button("📩 Send Reset OTP", use_container_width=True)

            if request_reset:
                reset_email_key = _normalize_email(reset_email)
                if not reset_email_key.endswith("@gmail.com"):
                    st.error("Please enter your registered Gmail address.")
                elif not _gmail_otp_configured():
                    _, gmail_status = _gmail_sender_status()
                    st.error(f"Gmail OTP is not ready: {gmail_status}")
                else:
                    try:
                        sent, message = _create_and_store_password_reset_otp(reset_email_key)
                        if sent:
                            st.success(message)
                        else:
                            st.info(message)
                    except Exception as exc:
                        st.error(f"Could not send reset OTP: {exc}")

            pending_reset = st.session_state.get("pending_password_reset_otp") or {}
            if pending_reset:
                if time.time() <= float(pending_reset.get("expires_at", 0)):
                    st.markdown(
                        f'<div style="margin:10px 0 8px;padding:9px 12px;border-radius:12px;background:#F8EEDB;color:#795422;font-size:12px;font-weight:800;">📬 Reset OTP sent to {html.escape(pending_reset.get("email", ""))}</div>',
                        unsafe_allow_html=True,
                    )
                    if pending_reset.get("delivery") == "development":
                        st.warning(f"Development OTP: {pending_reset.get('otp')} (local testing only)")
                    with st.form("forgot_password_verify_form", clear_on_submit=False):
                        reset_otp = st.text_input("6-digit OTP", placeholder="123456", max_chars=6, key="forgot_password_otp")
                        new_password = st.text_input("New Password", type="password", placeholder="Minimum 6 characters", key="forgot_new_password")
                        confirm_password = st.text_input("Confirm New Password", type="password", placeholder="Re-enter new password", key="forgot_confirm_password")
                        reset_submit = st.form_submit_button("✅ Verify OTP & Reset Password", use_container_width=True)

                    if reset_submit:
                        if len(new_password) < 6:
                            st.error("New password must be at least 6 characters.")
                        elif new_password != confirm_password:
                            st.error("Passwords do not match.")
                        else:
                            ok, msg = _verify_password_reset_otp(reset_otp)
                            if ok:
                                try:
                                    _set_account_password(pending_reset.get("email", ""), new_password)
                                    st.session_state.pop("pending_password_reset_otp", None)
                                    st.session_state.auth_forgot_open = False
                                    st.session_state.auth_mode = "Login"
                                    st.session_state.auth_modal_open = True
                                    st.success("✅ Password reset successfully. Please log in with your new password.")
                                except Exception as exc:
                                    st.error(str(exc))
                            else:
                                st.error(msg)

                    if st.button("🔁 Resend Reset OTP", key="resend_password_reset_otp", use_container_width=True):
                        try:
                            _resend_password_reset_otp()
                            st.success("A new reset OTP has been sent to your Gmail.")
                        except Exception as exc:
                            st.error(str(exc))
                else:
                    st.session_state.pop("pending_password_reset_otp", None)
                    st.warning("Reset OTP expired. Please request a new one.")

        else:
            st.radio("Account action", ["Login", "Sign Up"], horizontal=True, label_visibility="collapsed", key="auth_mode")
            users = _load_users()

            if st.session_state.auth_mode == "Login":
                with st.form("compact_login_form", clear_on_submit=False):
                    email = st.text_input("Gmail Address", placeholder="you@gmail.com", key="modal_login_email")
                    password = st.text_input("Password", type="password", placeholder="Your password", key="modal_login_password")
                    submit = st.form_submit_button("🔓 Login", use_container_width=True)
                if submit:
                    email_key = _normalize_email(email)
                    record = users.get(email_key)
                    if record and _verify_password(password, record.get("salt", ""), record.get("password_hash", "")):
                        st.session_state.auth_user = {"email": email_key, "name": record.get("name", "User"), "phone": record.get("phone", "")}
                        _set_auth_query_token(_issue_login_session(email_key))
                        st.session_state.auth_modal_open = False
                        st.session_state.auth_prompt = "continue"
                        st.session_state.auth_forgot_open = False
                        _load_account_data_into_session(email_key)
                        st.rerun()
                    else:
                        st.error("Invalid Gmail or password.")
                if st.button("Forgot Password?", key="forgot_password_button", use_container_width=True):
                    st.session_state.auth_forgot_open = True
                    st.rerun()
            else:
                with st.form("compact_signup_form", clear_on_submit=False):
                    n1, n2 = st.columns(2)
                    with n1:
                        name = st.text_input("Full Name", placeholder="Your name", key="modal_signup_name")
                    with n2:
                        phone = st.text_input("Mobile Number", placeholder="9876543210", key="modal_signup_phone")
                    email = st.text_input("Gmail Address", placeholder="you@gmail.com", key="modal_signup_email")
                    q1, q2 = st.columns(2)
                    with q1:
                        password = st.text_input("Password", type="password", placeholder="Minimum 6 characters", key="modal_signup_password")
                    with q2:
                        confirm = st.text_input("Confirm Password", type="password", placeholder="Repeat password", key="modal_signup_confirm")
                    send_otp = st.form_submit_button("📩 Send OTP", use_container_width=True)

                if send_otp:
                    name = name.strip()
                    email_key = _normalize_email(email)
                    phone_key = _normalize_phone(phone)
                    if not name:
                        st.error("Please enter your name.")
                    elif not _valid_indian_mobile(phone):
                        st.error("Please enter a valid 10-digit Indian mobile number.")
                    elif not email_key.endswith("@gmail.com"):
                        st.error("Please use a Gmail address ending with @gmail.com.")
                    elif len(password) < 6:
                        st.error("Password must be at least 6 characters.")
                    elif password != confirm:
                        st.error("Passwords do not match.")
                    elif email_key in users:
                        st.error("This Gmail is already registered. Please use Login.")
                    elif any(isinstance(r, dict) and _normalize_phone(r.get("phone", "")) == phone_key for r in users.values()):
                        st.error("This mobile number is already registered.")
                    elif not _gmail_otp_configured():
                        _, gmail_status = _gmail_sender_status()
                        st.error(f"Gmail OTP is not ready: {gmail_status}")
                    else:
                        try:
                            _create_and_store_signup_otp(name, phone_key, email_key, password)
                            pending_now = st.session_state.get("pending_signup_otp") or {}
                            if pending_now.get("delivery") == "development":
                                st.warning(f"Development OTP: {pending_now.get('otp')} (local testing only)")
                            else:
                                st.success(f"OTP sent to {email_key}. Check Inbox/Spam.")
                        except Exception as exc:
                            st.error(f"Could not send OTP: {exc}")

                pending = st.session_state.get("pending_signup_otp") or {}
                if pending and time.time() <= float(pending.get("expires_at", 0)):
                    st.markdown(
                        f'''<div style="margin:10px 0 8px;padding:9px 12px;border-radius:12px;background:#F8EEDB;color:#795422;font-size:12px;font-weight:800;">📬 OTP sent to {html.escape(pending.get("email", ""))}</div>''',
                        unsafe_allow_html=True,
                    )
                    with st.form("compact_verify_otp_form", clear_on_submit=False):
                        otp_value = st.text_input("6-digit OTP", placeholder="123456", max_chars=6, key="modal_signup_otp")
                        verify = st.form_submit_button("✅ Verify & Create Account", use_container_width=True)
                    if verify:
                        ok, msg = _verify_signup_otp(otp_value)
                        if ok:
                            pending_data = st.session_state.get("pending_signup_otp", {})
                            users = _load_users()
                            email_key = _normalize_email(pending_data.get("email", ""))
                            users[email_key] = {
                                "name": pending_data.get("name", "User"),
                                "phone": pending_data.get("phone", ""),
                                "salt": pending_data.get("salt", ""),
                                "password_hash": pending_data.get("password_hash", ""),
                                "created_at": datetime.now().isoformat(timespec="seconds"),
                                "email_verified": True,
                                "favorites": [],
                                "saved_searches": [],
                                "recently_viewed": [],
                                "valuation_history": [],
                                "comparison_history": [],
                                "report_history": [],
                            }
                            _save_users(users)
                            st.session_state.auth_user = {"email": email_key, "name": pending_data.get("name", "User"), "phone": pending_data.get("phone", "")}
                            _set_auth_query_token(_issue_login_session(email_key))
                            st.session_state.auth_modal_open = False
                            st.session_state.auth_prompt = "continue"
                            st.session_state.pop("pending_signup_otp", None)
                            _load_account_data_into_session(email_key)
                            st.rerun()
                        else:
                            st.error(msg)
                    if st.button("🔁 Resend OTP", key="modal_resend_otp", use_container_width=True):
                        try:
                            _resend_signup_otp()
                            st.success("New OTP sent.")
                        except Exception as exc:
                            st.error(str(exc))
                elif pending:
                    st.session_state.pop("pending_signup_otp", None)
                    st.warning("OTP expired. Please request a new one.")

        st.caption("Browse and search freely. Login unlocks your personal Home Account, saved homes, searches, history and reports.")

    _dialog()


# Public browsing is the default. Authentication appears only for protected actions.
if st.session_state.get("auth_modal_open") and not st.session_state.get("auth_user"):
    _render_auth_dialog()

if st.session_state.get("auth_user"):
    _load_account_data_into_session()
else:
    st.session_state.favorites = []
    st.session_state.saved_searches = []
    st.session_state.recently_viewed = []
    st.session_state.valuation_history = []
    st.session_state.comparison_history = []
    st.session_state.report_history = []


# 📄 PDF Library Compatibility Fix
try:
    from fpdf import FPDF
    HAS_FPDF = True
except ImportError:
    HAS_FPDF = False

# 🤖 GOOGLE GEMINI AI ASSISTANT
# No .streamlit folder is required.
#
# The app automatically retries temporary Gemini 503/429 errors and
# falls back to another currently supported Flash model when possible.
# Never share your API key publicly.

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Put your NEW Gemini API key here when you do not want to use
# an environment variable. The key is NOT shown anywhere in the UI.
if not GEMINI_API_KEY:
    GEMINI_API_KEY = "AQ.Ab8RN6J7aXiobcUqHPFFJCL7fmfd6znE65EsmxadXl9ZchGkVg"


def get_active_gemini_key():
    """Return the configured Gemini key without exposing it in the UI."""
    return GEMINI_API_KEY

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Primary model + automatic fallbacks.
# These are stable/current Gemini Flash endpoints supported by Google.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite").strip()
GEMINI_FALLBACK_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
]

# Temporary service errors that are worth retrying/falling back from.
GEMINI_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
GEMINI_MAX_ATTEMPTS_PER_MODEL = 1


def _gemini_key_configured():
    key = get_active_gemini_key()
    return bool(
        key
        and key != "PASTE_YOUR_NEW_GEMINI_KEY_HERE"
    )


def get_gemini_status():
    """Return (ok, details) without making an AI call."""
    if not _gemini_key_configured():
        return False, "Google Gemini is not configured."

    return True, f"Google Gemini • {GEMINI_MODEL}"


def _gemini_model_candidates():
    """Build a de-duplicated model list, keeping the configured model first."""
    models = [GEMINI_MODEL] + list(GEMINI_FALLBACK_MODELS)
    result = []
    for model in models:
        model = str(model).strip()
        if model and model not in result:
            result.append(model)
    return result


def _gemini_error_detail(response):
    detail = "Unknown Google Gemini API error."
    try:
        error_json = response.json()
        error_obj = error_json.get("error", {})
        if isinstance(error_obj, dict):
            detail = error_obj.get("message") or str(error_obj)
        else:
            detail = str(error_obj)
    except Exception:
        if response.text:
            detail = response.text[:1200]
    return detail


def _gemini_request_once(messages, model, temperature=0.2):
    """Perform one generateContent request for one model."""
    contents = []
    system_instruction = None

    for msg in messages:
        role = str(msg.get("role", "user")).strip().lower()
        content = str(msg.get("content", "")).strip()
        if not content:
            continue

        if role == "system":
            system_instruction = {
                "parts": [{"text": content}]
            }
            continue

        gemini_role = "model" if role in {"assistant", "model"} else "user"
        contents.append({
            "role": gemini_role,
            "parts": [{"text": content}],
        })

    if not contents:
        raise RuntimeError("Gemini request contains no user message.")

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": float(temperature),
            "maxOutputTokens": 512,
        },
    }

    if system_instruction:
        payload["systemInstruction"] = system_instruction

    headers = {
        "x-goog-api-key": get_active_gemini_key(),
        "Content-Type": "application/json",
    }

    endpoint = f"{GEMINI_BASE_URL}/models/{model}:generateContent"

    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=35,
        )
    except requests.exceptions.Timeout as exc:
        raise RuntimeError(
            "Google Gemini request timed out. Check your internet connection and try again."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Could not connect to Google Gemini: {exc}") from exc

    if response.status_code >= 400:
        detail = _gemini_error_detail(response)
        error = RuntimeError(
            f"Google Gemini HTTP {response.status_code}: {detail}"
        )
        error.status_code = response.status_code
        error.model = model
        raise error

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Google Gemini returned an invalid JSON response.") from exc

    candidates = data.get("candidates") or []
    if not candidates:
        prompt_feedback = data.get("promptFeedback") or {}
        block_reason = prompt_feedback.get("blockReason")
        if block_reason:
            raise RuntimeError(
                f"Google Gemini blocked the request: {block_reason}"
            )
        raise RuntimeError("Google Gemini returned no answer.")

    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    answer = "\n".join(
        str(part.get("text", "")) for part in parts if part.get("text")
    ).strip()

    if not answer:
        finish_reason = candidates[0].get("finishReason", "UNKNOWN")
        raise RuntimeError(
            f"Google Gemini returned an empty answer. Finish reason: {finish_reason}"
        )

    return answer


def call_gemini_ai(messages, temperature=0.2):
    """Call Gemini with retry + automatic model fallback."""
    if not _gemini_key_configured():
        raise RuntimeError(
            "GEMINI_API_KEY is not configured. "
            "Set the GEMINI_API_KEY environment variable or paste your key into the GEMINI_API_KEY variable in app.py."
        )

    candidates = _gemini_model_candidates()
    temporary_errors = []

    for model_index, model in enumerate(candidates):
        for attempt in range(1, GEMINI_MAX_ATTEMPTS_PER_MODEL + 1):
            try:
                answer = _gemini_request_once(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                )
                # Save the model that actually worked so the UI can show it.
                st.session_state["gemini_active_model"] = model
                st.session_state["ai_last_error"] = ""
                return answer
            except Exception as exc:
                status_code = getattr(exc, "status_code", None)

                # Authentication/configuration problems should not be hidden by fallback.
                if status_code in {400, 401, 403}:
                    raise

                # Temporary service/rate-limit/model-availability problems:
                # retry once, then move to the next model.
                if status_code in GEMINI_RETRY_STATUS_CODES:
                    temporary_errors.append(
                        f"{model}: HTTP {status_code}"
                    )
                    if attempt < GEMINI_MAX_ATTEMPTS_PER_MODEL:
                        time.sleep(0.25 * attempt)
                        continue
                    break

                # 404 commonly means a model is unavailable for the current API/project.
                if status_code == 404:
                    temporary_errors.append(f"{model}: HTTP 404")
                    break

                # Network timeout/connection errors can be retried, then fall back.
                text = str(exc).lower()
                networkish = any(
                    token in text
                    for token in ["timed out", "could not connect", "connection"]
                )
                if networkish:
                    temporary_errors.append(f"{model}: network error")
                    if attempt < GEMINI_MAX_ATTEMPTS_PER_MODEL:
                        time.sleep(0.25 * attempt)
                        continue
                    break

                raise

    detail = ", ".join(temporary_errors[-6:])
    raise RuntimeError(
        "All configured Gemini models are temporarily unavailable. "
        "Google may be experiencing high demand. "
        f"Tried: {detail or 'Gemini Flash models'}. Please try again in a moment."
    )


def furniture_pdf_section_text():
    rows, subtotal, installation, total = get_furniture_pdf_details()
    if not rows:
        return "FURNITURE & FURNISHING\nNo furniture items selected."
    lines = ["FURNITURE & FURNISHING", "Item | Qty | Unit Price | Subtotal"]
    for name, qty, price, line in rows:
        lines.append(f"{name} | {qty} | ₹{price:,.0f} | ₹{line:,.0f}")
    lines += [
        "",
        f"Furniture Subtotal: ₹{subtotal:,.0f}",
        f"Installation Estimate (8%): ₹{installation:,.0f}",
        f"Total Furnishing Cost: ₹{total:,.0f}",
    ]
    return "\n".join(lines)

def get_furniture_pdf_details():
    """Read the selected furniture cart for PDF/financing calculations."""
    cart = st.session_state.get("furniture_cart", {})
    catalog_map = {}
    for category, items in FURNITURE_CATALOG.items():
        for name, icon, price in items:
            catalog_map[f"{category}|{name}"] = (category, name, icon, float(price))

    rows = []
    subtotal = 0.0
    count = 0
    for iid, qty in cart.items():
        qty = int(qty or 0)
        item = catalog_map.get(iid)
        if qty <= 0 or not item:
            continue
        category, name, icon, unit = item
        line = unit * qty
        subtotal += line
        count += qty
        rows.append((category, name, icon, unit, qty, line))
    installation = round(subtotal * 0.08) if subtotal else 0
    return rows, subtotal, installation, subtotal + installation, count


def smart_rerun():
    if hasattr(st, "rerun"):
        st.rerun()
    else:
        st.experimental_rerun()

# ==========================================
# PATHS & CONSTANTS
# ==========================================
BASE = Path(__file__).resolve().parent
MODEL_PATH = BASE / "model" / "best_model.pkl"
COLUMNS_PATH = BASE / "model" / "model_columns.pkl"
DATASET_PATH = BASE / "dataset" / "house_data.csv"

CITY_AREAS = {
    "Ahmedabad": sorted([
        "Ambawadi", "Ambli", "Anand Nagar", "Ashram Road", "Bapunagar", "Bodakdev", "Bopal", 
        "CG Road", "Chandkheda", "Ellisbridge", "Ghatlodia", "Gota", "Gulbai Tekra", 
        "Iscon Crossroads", "Jodhpur", "Juhapura", "Makarba", "Maninagar", "Memnagar", 
        "Motera", "Naranpura", "Naroda", "Navrangpura", "Nikol", "Odhav", "Paldi", 
        "Prahlad Nagar", "Ranip", "Sabarmati", "Satellite", "Science City", "SG Highway", 
        "Shahibaug", "Shela", "Shilaj", "Sindhu Bhavan Road", "South Bopal", "Thaltej", 
        "Vaishnodevi Circle", "Vastral", "Vastrapur", "Vejalpur"
    ]),
    "Gandhinagar": ["GIFT City", "Kudasan", "Sargasan", "Raysan", "Randesan", "Koba"],
    "Rajkot": ["Raiya", "Yogi Nagar", "Munjka", "Mavdi", "Madhapar", "Sadar"],
}

SAFETY_SCORES = {
    "Bodakdev": 9.5, "Thaltej": 9.2, "Prahlad Nagar": 9.4, "Science City": 9.0, 
    "GIFT City": 9.8, "Satellite": 9.1, "Bopal": 8.5, "Gota": 8.2, "Chandkheda": 8.0, 
    "Maninagar": 8.4, "Sindhu Bhavan Road": 9.6, "SG Highway": 9.0, "CG Road": 9.2,
    "Navrangpura": 9.1, "South Bopal": 8.8, "Shela": 8.7, "Vastrapur": 9.3, "Ambli": 9.4
}

EXACT_COORDS = {
    "Bodakdev": [23.0384, 72.5119], "Thaltej": [23.0475, 72.5120], "Satellite": [23.0284, 72.5236],
    "Vastrapur": [23.0350, 72.5293], "Prahlad Nagar": [23.0119, 72.5054], "Science City": [23.0724, 72.5164],
    "Ambli": [23.0263, 72.4842], "Bopal": [23.0333, 72.4604], "Gota": [23.0900, 72.5358], 
    "Chandkheda": [23.1116, 72.5830], "Maninagar": [22.9961, 72.6033],
    "GIFT City": [23.1593, 72.6841], "Kudasan": [23.1905, 72.6358], "Sargasan": [23.1950, 72.6186],
    "Raiya": [22.3116, 70.7656], "Munjka": [22.2891, 70.7423], "Mavdi": [22.2612, 70.7856]
}
CITY_DEFAULTS = {"Ahmedabad": [23.0225, 72.5714], "Gandhinagar": [23.2156, 72.6369], "Rajkot": [22.3039, 70.8022]}

# Premium property photos.  Keep several backup URLs per BHK so one
# failed/slow CDN request never leaves a blank card image.
FLOOR_PLANS = {
    # Exterior-only house photos — no living-room/bedroom/interior shots.
    1: "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?q=84&w=1400&auto=format&fit=crop",
    2: "https://images.unsplash.com/photo-1512917774080-9991f1c4c750?q=84&w=1400&auto=format&fit=crop",
    3: "https://images.unsplash.com/photo-1600585154526-990dced4db0d?q=84&w=1400&auto=format&fit=crop",
    4: "https://images.unsplash.com/photo-1600047509807-ba8f99d2cdde?q=84&w=1400&auto=format&fit=crop",
    5: "https://images.unsplash.com/photo-1600607688969-a5bfcd646154?q=84&w=1400&auto=format&fit=crop",
    6: "https://images.unsplash.com/photo-1613490908677-caebcd278693?q=84&w=1400&auto=format&fit=crop",
}

# Extra exterior-only alternatives used when a primary photo cannot load.
PROPERTY_IMAGE_FALLBACKS = [
    "https://images.unsplash.com/photo-1600607687920-4e2a09cf159d?q=84&w=1400&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1600566753190-17f0baa2a6c3?q=84&w=1400&auto=format&fit=crop",
]

# Every Find Homes card draws from this exterior-only pool.
PROPERTY_IMAGE_POOL = list(dict.fromkeys(list(FLOOR_PLANS.values()) + PROPERTY_IMAGE_FALLBACKS))

def get_property_image_url(bhk, index=0):
    """Return a different premium property image for each card."""
    try:
        idx = int(index)
    except Exception:
        idx = 0
    return PROPERTY_IMAGE_POOL[idx % len(PROPERTY_IMAGE_POOL)]


def property_image_html(bhk, index=0, height=170, title=None):
    """Premium property thumbnail with robust fallback and no broken-image alt text."""
    try:
        safe_bhk = int(bhk)
    except Exception:
        safe_bhk = 3
    # Each card gets its own primary image. If that image fails, try the remaining
    # premium property images before using the inline SVG fallback.
    selected = get_property_image_url(safe_bhk, index)
    candidates = [selected]
    for u in PROPERTY_IMAGE_POOL:
        if u not in candidates:
            candidates.append(u)
    label = html.escape(str(title or f"Premium {safe_bhk} BHK home"), quote=True)
    primary = html.escape(candidates[0], quote=True)
    # Final fallback is inline SVG, so a blocked CDN never exposes a broken icon.
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="700" viewBox="0 0 1200 700"><defs><linearGradient id="g" x1="0" x2="1" y1="0" y2="1"><stop stop-color="#8d6b45"/><stop offset="1" stop-color="#3e2c20"/></linearGradient></defs><rect width="1200" height="700" fill="url(#g)"/><circle cx="1030" cy="130" r="180" fill="#d9ad5b" opacity=".14"/><path d="M300 420 L600 190 L900 420 V570 H300Z" fill="#f7efe2" opacity=".95"/><path d="M245 430 L600 150 L955 430 L920 470 L600 220 L280 470Z" fill="#d9ad5b"/><rect x="520" y="405" width="160" height="165" rx="6" fill="#5a4330"/><rect x="350" y="385" width="110" height="95" fill="#c9a86d"/><rect x="740" y="385" width="110" height="95" fill="#c9a86d"/><text x="600" y="625" text-anchor="middle" font-family="Arial" font-size="34" fill="#fff8ed">' + label + '</text></svg>'
    import base64
    placeholder = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
    js_urls = candidates + [placeholder]
    fallback_js = "[" + ",".join("'" + u.replace("'", "\\'") + "'" for u in js_urls) + "]"
    img_id = f"smartestate_prop_img_{int(index) if str(index).isdigit() else 0}_{abs(hash(primary)) % 100000}"
    return f"""<img id="{img_id}" src="{primary}" alt="" aria-label="{label}" loading="eager" decoding="async" style="width:100%;height:{int(height)}px;object-fit:cover;display:block;background:#eadcc8;" onerror="(function(img){{var fs={fallback_js};var n=parseInt(img.dataset.fallbackIndex||'0',10)+1;img.dataset.fallbackIndex=n;if(n<fs.length){{img.src=fs[n];}}else{{img.onerror=null;}}}})(this)">"""


BG_IMAGE_URL = "https://images.unsplash.com/photo-1600607687920-4e2a09cf159d?q=80&w=2075&auto=format&fit=crop"

# ==========================================
# HELPER FUNCTIONS 
# ==========================================
@st.cache_data
def load_dataset():
    if DATASET_PATH.exists(): return pd.read_csv(DATASET_PATH)
    return pd.DataFrame()

df = load_dataset()

@st.cache_resource(show_spinner=False)
def load_model_bundle():
    """Load the trained model once per Streamlit process instead of every rerun."""
    if MODEL_PATH.exists() and COLUMNS_PATH.exists():
        return joblib.load(MODEL_PATH), joblib.load(COLUMNS_PATH)
    return None, None

model, model_columns = load_model_bundle()

@st.cache_data(ttl=3600, show_spinner=False)
def get_prediction(c, a_n, a, b, ba, g, p, ch, gz, solar):
    """Predict property value in Lakhs for a complete property configuration."""
    if model is None:
        return 0.0
    row = pd.DataFrame([{
        "Area": float(a),
        "BHK": int(b),
        "Bathrooms": int(ba),
        "City": c,
        "Area_Name": a_n,
        "Gym": int(bool(g)),
        "Swimming_Pool": int(bool(p)),
        "Cinema_Hall": int(bool(ch)),
        "Gaming_Zone": int(bool(gz)),
        "Solar_Balcony": int(bool(solar)),
    }])
    row = pd.get_dummies(row, columns=["City", "Area_Name"])
    row = row.reindex(columns=model_columns, fill_value=0)
    try:
        pred = float(model.predict(row)[0])
    except Exception:
        return 0.0
    # Keep the existing SmartEstate solar fallback used when the trained
    # model was created before Solar_Balcony was added as a feature.
    if bool(solar) and "Solar_Balcony" not in model_columns:
        pred += 1.2
    return pred

# ==========================================
# FURNITURE
# ==========================================

FURNITURE_CATALOG = {
    "Living Room": [("Sofa Set", "🛋️", 65000), ("TV Unit", "📺", 40000), ("Center Table", "☕", 15000), ("Curtains", "🪟", 18000)],
    "Bedrooms": [("King Bed", "🛏️", 45000), ("Queen Bed", "🛏️", 35000), ("Wardrobe", "🚪", 55000), ("Bedside Tables", "🗄️", 12000)],
    "Kitchen": [("Modular Kitchen", "🍳", 200000), ("Kitchen Appliances", "⚡", 65000), ("Dining Table", "🍽️", 45000)],
    "Lighting & Decor": [("Designer Lighting", "💡", 35000), ("Wall Decor", "🖼️", 20000), ("Rugs", "🧶", 18000)],
}

if "furniture_cart" not in st.session_state: st.session_state.furniture_cart = {}

# ============================================================
# FURNITURE CART HELPERS — unique widget keys + live quantities
# ============================================================
def furniture_unique_key(prefix, iid, bhk, final_price, instance="main"):
    """Create a globally unique Streamlit widget key for furniture controls."""
    safe_id = re.sub(r"[^a-zA-Z0-9_]+", "_", str(iid))
    price_key = int(round(float(final_price or 0) * 100))
    return f"furniture_{prefix}_{instance}_{safe_id}_{int(bhk)}_{price_key}"

def furniture_cart_summary(cart):
    total = 0.0
    count = 0
    rows = []
    for iid, qty in cart.items():
        try:
            q = max(0, int(qty))
        except Exception:
            q = 0
        if q <= 0:
            continue
        item = FURNITURE_ITEMS.get(iid, {})
        unit = float(item.get("price", 0))
        subtotal = unit * q
        total += subtotal
        count += q
        rows.append((iid, q, unit, subtotal, item.get("category", "")))
    return total, count, rows

def furniture_estimator(bhk, property_price=None, compact=False, instance_id="valuation"):
    """Smart furniture cart used by the AI Valuation and Budget-First View Details pages."""
    bhk = int(bhk)
    if "furniture_cart" not in st.session_state or not isinstance(st.session_state.furniture_cart, dict):
        st.session_state.furniture_cart = {}

    catalog = []
    for category, items in FURNITURE_CATALOG.items():
        for name, icon, price in items:
            qty_allowed = 1
            if name == "Queen Bed":
                qty_allowed = max(0, bhk - 1)
            elif name in ("Wardrobe", "Bedside Tables"):
                qty_allowed = bhk
            if qty_allowed:
                catalog.append((category, name, icon, float(price)))

    groups = {}
    for item in catalog:
        groups.setdefault(item[0], []).append(item)

    st.markdown("""
    <style>
    .furn-head{padding:14px 16px;border:1px solid rgba(220,176,101,.25);border-radius:16px;
    background:linear-gradient(135deg,rgba(218,167,82,.12),rgba(255,255,255,.04));margin:8px 0 14px}
    .furn-head b{font-size:18px}.furn-head span{color:#64748b;font-size:12px}
    .furn-item{padding:9px 0;border-bottom:1px solid rgba(100,116,139,.14)}
    .furn-name{font-weight:700;font-size:14px}.furn-price{color:#64748b;font-size:11px;margin-top:3px}
    .furn-added{color:#a18359;font-size:11px;font-weight:800;margin-top:3px}
    .furn-total{padding:16px;border-radius:16px;background:linear-gradient(135deg,#0f172a,#1e293b);color:white;margin-top:14px}
    .furn-total .small{font-size:11px;color:#cbd5e1}.furn-total .big{font-size:23px;font-weight:900;color:#f1d08a}
    
/* Premium valuation share card */
.share-card-premium{background:linear-gradient(135deg,#fffaf2,#f6ead4);border:1px solid rgba(218,167,82,.26);border-radius:16px;padding:10px 12px;margin:4px 0 8px;box-shadow:0 8px 20px rgba(91,64,33,.06)}
.share-card-compact{display:flex;align-items:center;justify-content:space-between;gap:10px}
.share-compact-left{min-width:0}.share-compact-left .share-heading{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.share-card-top{display:flex;align-items:flex-start;justify-content:space-between;gap:14px}
.share-eyebrow{font-size:8px;letter-spacing:1.6px;font-weight:900;color:#b47b26;margin-bottom:2px}
.share-heading{font-size:15px;font-weight:850;color:#3e2b1b}
.share-subtitle{font-size:11px;color:#8b7862;margin-top:2px}
.share-badge{font-size:9px;letter-spacing:1px;font-weight:900;color:#8a5a18;background:#f4dfb8;border:1px solid #e5c589;border-radius:999px;padding:7px 10px;white-space:nowrap}
.share-preview{display:flex;align-items:center;gap:12px;background:rgba(255,255,255,.78);border:1px solid rgba(218,167,82,.16);border-radius:15px;padding:12px;margin-top:14px}
.share-preview-icon{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;background:linear-gradient(135deg,#daa752,#bc7e24);box-shadow:0 7px 16px rgba(166,113,37,.2);font-size:20px}
.share-preview-main{flex:1;min-width:0}.share-preview-title{font-weight:800;color:#43301f;font-size:13px}.share-preview-meta{font-size:11px;color:#8d7b66;margin-top:2px}.share-preview-price{font-size:19px;font-weight:900;color:#daa752;white-space:nowrap}
.share-action{display:flex;align-items:center;justify-content:center;gap:7px;text-decoration:none!important;border:1px solid #ead9bd;border-radius:11px;padding:8px 9px;background:#fffaf2;color:#5b4127;box-shadow:0 4px 12px rgba(92,58,25,.05);transition:all .18s ease;min-height:42px}
.share-action-compact{font-size:13px}
.share-mini-btn{width:100%;height:52px;border:1px solid #ead9bd;background:#fffaf2;border-radius:11px;padding:7px 8px;cursor:pointer;color:#5b4127;font-size:13px;font-weight:800;box-shadow:0 4px 12px rgba(92,58,25,.05);display:flex;align-items:center;justify-content:center;gap:6px}
.share-mini-btn:hover,.share-action-compact:hover{transform:translateY(-1px);border-color:#d6b16f}
.share-mini-whatsapp{background:#f7fbf4;color:#4d5b3f}.share-mini-copy{background:#fffaf2}
.share-action:hover{transform:translateY(-2px);border-color:#d6b16f;box-shadow:0 10px 22px rgba(92,58,25,.11)}
.share-action span{display:flex;flex-direction:column;line-height:1.05}.share-action b{font-size:12px}.share-action small{font-size:10px;color:#9a8466;margin-top:3px}.share-action.whatsapp b{color:#198754}.share-action.email b{color:#8a5a18}
@media(max-width:800px){.share-card-top{flex-direction:column}.share-badge{align-self:flex-start}.share-preview-price{font-size:16px}}
</style>
    """, unsafe_allow_html=True)

    st.markdown(
        f'<div class="furn-head"><b>🛋️ Smart Furniture Cost Estimator</b><br>'
        f'<span>Furniture planner for {bhk} BHK • Add items and see the total update instantly.</span></div>',
        unsafe_allow_html=True
    )

    cols = st.columns(2 if compact else 3)
    for ci, (category, items) in enumerate(groups.items()):
        with cols[ci % len(cols)]:
            st.markdown(f"**{category}**")
            for idx, (_, name, icon, price) in enumerate(items):
                iid = f"{category}|{name}"
                cur = int(st.session_state.furniture_cart.get(iid, 0) or 0)
                safe_id = re.sub(r"[^a-zA-Z0-9_]+", "_", iid)
                base_key = f"{instance_id}_{safe_id}_{bhk}_{idx}"

                st.markdown(
                    f'<div class="furn-item"><div class="furn-name">{icon} {name}</div>'
                    f'<div class="furn-price">₹{price:,.0f} / item</div>'
                    + (f'<div class="furn-added">🛒 In cart: {cur}</div>' if cur else '')
                    + '</div>',
                    unsafe_allow_html=True
                )

                c_minus, c_qty, c_add = st.columns([0.75, 0.8, 1.65])
                with c_minus:
                    if st.button("−", key=f"furn_minus_{base_key}", use_container_width=True, help="Remove one"):
                        if cur > 0:
                            st.session_state.furniture_cart[iid] = cur - 1
                            smart_rerun()
                with c_qty:
                    st.markdown(
                        f'<div style="text-align:center;padding:7px 2px;border-radius:9px;'
                        f'background:rgba(218,167,82,.10);font-weight:900;">{cur}</div>',
                        unsafe_allow_html=True
                    )
                with c_add:
                    if st.button(
                        f"＋ Add {cur + 1}",
                        key=f"furn_add_{base_key}",
                        use_container_width=True,
                        help=f"Add {name}"
                    ):
                        st.session_state.furniture_cart[iid] = cur + 1
                        smart_rerun()

    subtotal = 0.0
    selected = []
    for category, name, icon, price in catalog:
        iid = f"{category}|{name}"
        qty = int(st.session_state.furniture_cart.get(iid, 0) or 0)
        if qty > 0:
            line = price * qty
            subtotal += line
            selected.append((category, name, icon, price, qty, line))

    installation = round(subtotal * 0.08) if subtotal else 0
    furnishing_total = subtotal + installation
    cart_count = sum(int(v or 0) for v in st.session_state.furniture_cart.values())

    st.markdown("---")
    if selected:
        st.markdown("### 🛒 Your Furniture Cart")
        for category, name, icon, price, qty, line in selected:
            st.markdown(f"**{icon} {name}** × {qty} — ₹{price:,.0f} × {qty} = **₹{line:,.0f}**")

        st.markdown(
            f'<div class="furn-total"><div class="small">🛒 {cart_count} item(s) selected</div>'
            f'<div style="margin-top:5px;">Furniture Subtotal: <b>₹{subtotal:,.0f}</b></div>'
            f'<div class="small" style="margin-top:7px;">Installation estimate (8%)</div>'
            f'<div>₹{installation:,.0f}</div>'
            f'<div class="small" style="margin-top:8px;">TOTAL FURNISHING COST</div>'
            f'<div class="big">₹{furnishing_total:,.0f}</div></div>',
            unsafe_allow_html=True
        )

        if property_price is not None:
            combined_l = float(property_price) + furnishing_total / 100000
            st.markdown(
                f'<div style="margin-top:12px;padding:15px;border-radius:14px;border:1px solid rgba(220,176,101,.35);'
                f'background:rgba(220,176,101,.08);">'
                f'<b>🏠 Property ₹{float(property_price):,.2f} L</b> + '
                f'<b>🛋️ Furniture ₹{furnishing_total/100000:,.2f} L</b><br>'
                f'<span style="font-size:20px;font-weight:900;">💰 TOTAL PROJECT COST: ₹{combined_l:,.2f} L</span></div>',
                unsafe_allow_html=True
            )

        if st.button("🗑️ Clear Furniture Cart", key=f"clear_furniture_{instance_id}_{bhk}", use_container_width=True):
            st.session_state.furniture_cart = {}
            smart_rerun()
    else:
        st.info("🛒 Cart is empty — use the ＋ Add buttons to select furniture.")

    return {"subtotal": subtotal, "installation": installation, "total": furnishing_total,
            "count": cart_count, "selected": selected}


def calculate_emi(principal, rate, tenure_years):
    r = rate / (12 * 100)
    n = tenure_years * 12
    if r == 0: return principal / n
    emi = principal * r * ((1 + r)**n) / (((1 + r)**n) - 1)
    return emi

def calculate_investment_analysis(price_lakhs, monthly_rent, annual_appreciation=6.5, annual_rent_growth=5.0, years=10):
    price_rs=max(0.0,float(price_lakhs))*100000.0
    rent=max(0.0,float(monthly_rent))
    app=max(-20.0,min(30.0,float(annual_appreciation)))/100.0
    rg=max(-20.0,min(30.0,float(annual_rent_growth)))/100.0
    y=max(1,int(years))
    annual_rent=rent*12.0
    yield_pct=(annual_rent/price_rs*100.0) if price_rs else 0.0
    f5=price_rs*((1+app)**5); f10=price_rs*((1+app)**10)
    cumulative=0.0; r=rent
    for _ in range(y):
        cumulative += r*12.0; r *= (1+rg)
    future=price_rs*((1+app)**y); gain=max(0.0,future-price_rs)
    roi=((gain+cumulative)/price_rs*100.0) if price_rs else 0.0
    return {"price_rs":price_rs,"monthly_rent":rent,"annual_rent":annual_rent,"rental_yield":yield_pct,"future_5":f5,"future_10":f10,"cumulative_rent":cumulative,"future_value":future,"capital_gain":gain,"roi":roi}


def calculate_total_purchase_cost(final_price_lakhs, downpayment_pct, interest_rate, tenure_years, stamp_pct, registration_pct, other_rs=0):
    """Calculate the property's estimated acquisition cost and financing summary.

    final_price_lakhs is the property price in lakh rupees. Percentage inputs are
    applied to the property price; other_rs is an absolute rupee amount.
    """
    try:
        price_rs = max(0.0, float(final_price_lakhs)) * 100000.0
        dp_pct = max(0.0, min(100.0, float(downpayment_pct)))
        rate = max(0.0, float(interest_rate))
        years = max(1, int(tenure_years))
        stamp = price_rs * max(0.0, float(stamp_pct)) / 100.0
        registration = price_rs * max(0.0, float(registration_pct)) / 100.0
        other = max(0.0, float(other_rs))
        downpayment = price_rs * dp_pct / 100.0
        loan_amount = max(0.0, price_rs - downpayment)
        emi = calculate_emi(loan_amount, rate, years) if loan_amount > 0 else 0.0
        months = years * 12
        total_loan_payment = emi * months
        total_interest = max(0.0, total_loan_payment - loan_amount)
        upfront_cash = downpayment + stamp + registration + other
        total_acquisition = price_rs + stamp + registration + other + total_interest
        return {
            "property_price": price_rs,
            "downpayment": downpayment,
            "loan_amount": loan_amount,
            "stamp_duty": stamp,
            "registration": registration,
            "other_charges": other,
            "emi": emi,
            "total_interest": total_interest,
            "upfront_cash": upfront_cash,
            "total_loan_payment": total_loan_payment,
            "total_acquisition": total_acquisition,
        }
    except (TypeError, ValueError, ZeroDivisionError):
        return {
            "property_price": 0.0, "downpayment": 0.0, "loan_amount": 0.0,
            "stamp_duty": 0.0, "registration": 0.0, "other_charges": 0.0,
            "emi": 0.0, "total_interest": 0.0, "upfront_cash": 0.0,
            "total_loan_payment": 0.0, "total_acquisition": 0.0,
        }

def render_3d_architectural_view(bhk, area, gym=False, pool=False, cinema=False, gaming=False, solar=False, design_style="Modern Luxury", view_mode="Exterior"):
    """Premium all-in-one interactive 3D property experience based on Modern Timeless Style.
    Fully dynamic: Bedrooms and Bathrooms scale from 1 to 6 precisely as requested.
    """
    import streamlit.components.v1 as components
    import json
    bhk = int(bhk)
    area = float(area or 0)
    expected_area = {1: 650, 2: 1100, 3: 1500, 4: 2500, 5: 3500, 6: 4500}.get(bhk, 1500)
    scale = max(0.78, min(1.65, (area / max(expected_area, 1)) ** 0.5))
    floors = 3 if bhk >= 6 else (2 if bhk >= 4 else 1)
    
    # Updated Palettes to match the "Timeless Style" image (warmer greys, wood, beige)
    palettes = {
     "Modern Luxury": {"wall":"#b9b5ad","dark":"#292c30","accent":"#a18359","glass":"#6f8e9d","wood":"#5b3d2b","floor":"#aaa49a","sofa":"#aa9d8c"},
        "Contemporary": {"wall":"#dfe4e7","dark":"#293138","accent":"#b38a55","glass":"#6d9db3","wood":"#785b45","floor":"#aeb5b8","sofa":"#cbd0d1"},
        "Classic Premium": {"wall":"#eee5d4","dark":"#493f35","accent":"#bd9250","glass":"#87adbd","wood":"#5c3a21","floor":"#c5b18f","sofa":"#ddd0bd"}
    }
    pal = palettes.get(design_style, palettes["Modern Luxury"])
    amenities=[]
    if gym: amenities.append("🏋️ Private Gym")
    if pool: amenities.append("🏊 Swimming Pool")
    if cinema: amenities.append("🎬 Home Cinema")
    if gaming: amenities.append("🎮 Gaming Zone")
    if solar: amenities.append("☀️ Solar Balcony")
    amenities += ["🛋️ Designer Living", "🍳 Modular Kitchen", "🛏️ Premium Bedrooms", "🛁 Premium Bathrooms", "🌿 Landscaped Balcony"]
    amenities=list(dict.fromkeys(amenities))
    data = {"bhk":bhk,"area":area,"scale":scale,"floors":floors,"style":design_style,
            **pal,"pool":bool(pool),"gym":bool(gym),"cinema":bool(cinema),"gaming":bool(gaming),"solar":bool(solar)}
    payload=json.dumps(data, ensure_ascii=False)
    amen_html=''.join(f'<div class="amen-item">{x}</div>' for x in amenities)
    photos = [
        ("Exterior", FLOOR_PLANS.get(max(1,min(6,bhk)), BG_IMAGE_URL)),
        ("Living Room", "https://images.unsplash.com/photo-1600210492486-724fe5c67fb0?q=80&w=1200&auto=format&fit=crop"),
        ("Master Bedroom", "https://images.unsplash.com/photo-1616486338812-3dadae4b4ace?q=80&w=1200&auto=format&fit=crop"),
        ("Kitchen", "https://images.unsplash.com/photo-1600607687920-4e2a09cf159d?q=80&w=1200&auto=format&fit=crop"),
        ("Bathroom", "https://images.unsplash.com/photo-1600566753086-00f18fb6b3ea?q=80&w=1200&auto=format&fit=crop"),
        ("Balcony", "https://images.unsplash.com/photo-1600607687939-ce8a6c25118c?q=80&w=1200&auto=format&fit=crop"),
    ]
    gallery_html=''.join(f'<button class="thumb" data-photo="{url}" data-label="{label}"><img src="{url}" loading="lazy"><span>{label}</span></button>' for label,url in photos)
    html=r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}html,body{margin:0;height:100%;overflow:hidden;background:#080d12;font-family:Inter,Arial,sans-serif}#app{position:relative;height:760px;overflow:hidden;background:linear-gradient(180deg,#16212b,#070b0f 70%)}#scene{position:absolute;inset:0}.hud{position:absolute;z-index:10;background:rgba(8,12,16,.86);backdrop-filter:blur(14px);color:#fff;border:1px solid rgba(255,255,255,.12);border-radius:16px;box-shadow:0 16px 45px #0008}.top{left:18px;top:16px;padding:13px 16px}.top b{font-size:16px}.sub{color:#aeb8c3;font-size:11px;margin-top:5px}.gold{color:#daa752}.right{right:16px;top:16px;width:230px;padding:14px}.right h3{margin:0 0 8px;font-size:14px}.amen-item{font-size:12px;padding:6px 0;border-bottom:1px solid #fff1;color:#e8edf1}.controls{position:absolute;z-index:11;top:16px;left:50%;transform:translateX(-50%);display:flex;gap:5px;background:#070b0fdd;border:1px solid #fff2;border-radius:999px;padding:5px}.controls button,.modebar button{border:0;background:transparent;color:#c8d0d8;padding:8px 13px;border-radius:999px;cursor:pointer;font-weight:600;font-size:11px}.controls button.active,.modebar button.active{background:#f3eee4;color:#10151a}.modebar{position:absolute;z-index:11;left:50%;bottom:92px;transform:translateX(-50%);display:flex;gap:4px;background:#070b0fcc;border:1px solid #fff2;border-radius:999px;padding:5px}.side-tools{position:absolute;z-index:12;right:260px;top:150px;display:flex;flex-direction:column;gap:5px}.side-tools button{width:42px;height:42px;border-radius:12px;border:1px solid #fff2;background:#070b0fdd;color:#fff;cursor:pointer;font-size:15px}.gallery{position:absolute;z-index:12;left:16px;right:250px;bottom:10px;height:72px;display:flex;gap:7px;overflow-x:auto}.thumb{position:relative;min-width:120px;height:70px;padding:0;border:1px solid #fff2;border-radius:10px;overflow:hidden;background:#111820;cursor:pointer}.thumb img{width:100%;height:100%;object-fit:cover;display:block}.thumb span{position:absolute;bottom:0;left:0;right:0;padding:5px;background:#000b;color:#fff;font-size:10px;text-align:left}.photo{position:absolute;z-index:13;left:22px;bottom:96px;width:300px;background:#070b0fcc;border:1px solid #fff2;border-radius:14px;padding:7px;display:none}.photo img{width:100%;height:175px;object-fit:cover;border-radius:9px}.photo div{color:#fff;font-size:12px;padding:6px 3px}.badge{display:inline-block;margin-top:7px;padding:4px 8px;border-radius:999px;background:#ffffff12;color:#daa752;font-size:10px}.bottom{position:absolute;z-index:10;bottom:82px;left:50%;transform:translateX(-50%);background:#000a;color:#dce4ea;border:1px solid #fff2;border-radius:999px;padding:7px 13px;font-size:10px;white-space:nowrap}.walk{position:absolute;z-index:13;left:50%;top:50%;transform:translate(-50%,-50%);background:#070b0fee;border:1px solid #daa75288;color:#fff;border-radius:18px;padding:18px 22px;display:none;text-align:center;box-shadow:0 20px 70px #000a}.walk button{margin-top:10px;background:#daa752;border:0;border-radius:9px;padding:9px 16px;font-weight:800;cursor:pointer}.loading{position:absolute;z-index:20;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;background:#070b0ff0;font-size:14px}.hidden{display:none!important}
.roomlabel-el{position:absolute;transform:translate(-50%,-100%);background:#000c;color:#fff;padding:4px 9px;border-radius:8px;font-size:11px;font-weight:700;pointer-events:none;white-space:nowrap;border:1px solid #daa75299;box-shadow:0 4px 12px #0008;z-index:8}
</style></head><body><div id="app"><div id="scene"></div><div id="load" class="loading">Loading premium 3D property…</div>
<div class="hud top"><b>🏠 __BHK__ BHK • __STYLE__</b><div class="sub">__AREA__ sq.ft • __FLOORS__ floor(s) • <span class="gold" id="currentMode">Exterior</span></div><span class="badge">Interactive 3D Concept</span></div>
<div class="controls"><button class="active" data-view="Exterior">Exterior</button><button data-view="Interior">Interior</button><button data-view="Floor Plan">Floor Plan</button><button data-view="Amenities">Amenities</button><button data-view="Walkthrough">Walkthrough</button></div>
<div class="hud right"><h3>✨ Premium Amenities</h3>__AMEN__</div>
<div class="side-tools"><button id="reset">↻</button><button id="zoomIn">＋</button><button id="zoomOut">－</button><button id="day">☀</button><button id="night">☾</button></div>
<div class="bottom">🖱️ Drag = rotate • Wheel = zoom • Right-drag = pan • Select a view above</div>
<div class="gallery">__GALLERY__</div><div class="photo" id="photo"><img id="photoImg"><div id="photoLabel"></div></div>
<div class="walk" id="walk"><b>🚶 Virtual Walkthrough</b><div style="font-size:11px;color:#aeb8c3;margin-top:5px">Move through the home room-by-room.</div><button id="startWalk">Start Walkthrough</button></div>
<div id="labelLayer" style="position:absolute;inset:0;pointer-events:none;z-index:8;"></div>
</div>
<script type="importmap">
{
  "imports": {
    "three": "https://cdn.jsdelivr.net/npm/three@0.149.0/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.149.0/examples/jsm/"
  }
}
</script>
 <script type="module">
const loadEl = document.getElementById('load');
const safetyTimer = setTimeout(()=>{
  loadEl.textContent = '⚠️ 3D view is taking too long. Your network may be blocking cdn.jsdelivr.net. Check your internet/firewall and reload.';
  loadEl.classList.remove('hidden');
},8000);
try {
const THREE = await import('three');
const { OrbitControls } = await import('three/addons/controls/OrbitControls.js');
 const P=__DATA__, initialView='__VIEW__', mount=document.getElementById('scene'), labelLayer=document.getElementById('labelLayer');
 const polish=document.createElement('style'); polish.textContent=`
   #scene canvas{display:block;width:100%;height:100%;filter:saturate(.92) contrast(1.04)}
   #app:after{content:"";position:absolute;inset:0;pointer-events:none;z-index:4;background:linear-gradient(180deg,rgba(6,10,15,.10),transparent 38%,rgba(4,7,11,.18));}
   .hud,.controls,.side-tools,.gallery,.photo,.walk{z-index:12}
   .top{max-width:270px}
   .right{top:86px}
   .controls{white-space:nowrap}
   .photo{left:auto;right:16px;bottom:96px}
   .photo button{position:absolute;right:14px;top:14px;width:28px;height:28px;border:1px solid #fff5;border-radius:50%;background:#070b0fdd;color:#fff;font-size:18px;line-height:20px;cursor:pointer}
   @media (max-width:760px){
     #app{height:700px}
     .top{left:12px;top:12px;max-width:235px;padding:10px 12px}
     .top b{font-size:13px}
     .top .sub{font-size:10px}
     .right{right:12px;top:92px;width:205px;padding:10px}
     .right h3{font-size:12px;margin-bottom:4px}
     .amen-item{font-size:10px;padding:4px 0}
     .controls{left:12px;right:12px;top:16px;transform:none;justify-content:center;gap:1px;padding:4px;overflow:hidden}
     .controls button{padding:7px 7px;font-size:10px}
     .side-tools{right:12px;top:205px}
     .side-tools button{width:38px;height:38px}
     .gallery{left:12px;right:12px;height:60px}
     .thumb{min-width:96px;height:58px}
     .photo{left:12px;right:12px;bottom:78px;width:auto}
     .photo img{height:145px}
     .bottom{left:12px;right:12px;transform:none;text-align:center;overflow:hidden}
   }
 `; document.head.appendChild(polish);
 const S=new THREE.Scene(); S.background=new THREE.Color('#10171d'); S.fog=new THREE.Fog('#10171d',34,105);
const C=new THREE.PerspectiveCamera(43,mount.clientWidth/mount.clientHeight,.1,300); C.position.set(19,13,23);
 const R=new THREE.WebGLRenderer({antialias:true,alpha:true,preserveDrawingBuffer:true}); R.setPixelRatio(Math.min(devicePixelRatio,2)); R.setSize(mount.clientWidth,mount.clientHeight); R.shadowMap.enabled=true; R.shadowMap.type=THREE.PCFSoftShadowMap; R.outputEncoding=THREE.sRGBEncoding; R.toneMapping=THREE.ACESFilmicToneMapping; R.toneMappingExposure=.82; mount.appendChild(R.domElement);
 const ctl=new OrbitControls(C,R.domElement); ctl.enableDamping=true; ctl.dampingFactor=.065; ctl.enablePan=true; ctl.target.set(0,2.5,0); ctl.minDistance=5; ctl.maxDistance=55; ctl.maxPolarAngle=Math.PI*.49; ctl.autoRotate=true; ctl.autoRotateSpeed=.32;
 const hemi=new THREE.HemisphereLight(0xdce7ef,0x17222b,1.18); S.add(hemi); const sun=new THREE.DirectionalLight(0xffe0ac,1.75); sun.position.set(18,26,14); sun.castShadow=true; sun.shadow.mapSize.set(2048,2048); sun.shadow.camera.left=-35; sun.shadow.camera.right=35; sun.shadow.camera.top=35; sun.shadow.camera.bottom=-35; S.add(sun);
 const moon=new THREE.DirectionalLight(0x7898c7,.12); moon.position.set(-20,18,-18); S.add(moon);
const M=(c,r=.6,m=0)=>new THREE.MeshStandardMaterial({color:c,roughness:r,metalness:m});
 const wall=M(P.wall,.72),dark=M(P.dark,.42),accent=M(P.accent,.25,.25),wood=M(P.wood,.52),floor=M(P.floor,.72),sofa=M(P.sofa,.78); const glass=new THREE.MeshPhysicalMaterial({color:P.glass,roughness:.05,metalness:.05,transmission:.35,transparent:true,opacity:.62,clearcoat:1,clearcoatRoughness:.08});
 const windowGlow=new THREE.MeshStandardMaterial({color:'#f4c879',emissive:'#f4a83e',emissiveIntensity:.48,roughness:.32});
 const leaf=M('#395747',.95),leafLight=M('#718b67',.9),black=M('#111820',.35,.6);
const exterior=[],interior=[],amenity=[];
 const roomLabels=[]; const nightOnly=[]; const animatedLights=[];
function add(o,group){o.castShadow=o.receiveShadow=true;group.push(o);S.add(o);return o}
function box(w,h,d,ma,x,y,z,group=exterior,rot=0){const o=new THREE.Mesh(new THREE.BoxGeometry(w,h,d),ma);o.position.set(x,y,z);o.rotation.y=rot;return add(o,group)}
function cyl(r,h,ma,x,y,z,group=exterior){const o=new THREE.Mesh(new THREE.CylinderGeometry(r,r,h,32),ma);o.position.set(x,y,z);return add(o,group)}
function win(x,y,z,w,h,group=exterior){box(w,h,.08,glass,x,y,z,group);box(w+.14,.1,.12,dark,x,y-h/2,z,group);box(.08,h,.12,dark,x-w/2,y,z,group);box(.08,h,.12,dark,x+w/2,y,z,group);box(.06,h,.1,dark,x,y,z,group)}

const W=12*P.scale,D=9*P.scale,H=3.65;
 box(48,.3,48,M('#30373b',.9),0,-.18,0,exterior); box(25,.06,25,M('#59615f',.95),0,.02,0,exterior); box(4.5,.08,14,M('#53595d',.95),0,.08,12,exterior);
 // A landscaped arrival court gives the building scale and makes the camera feel cinematic.
 box(3.4,.04,18,M('#77766f',.98),0,.08,14,exterior);
 for(let i=0;i<9;i++){let px=-13+(i%3)*13,pz=-12+Math.floor(i/3)*9; cyl(.42,1.55,leaf,px,.82,pz,exterior); cyl(.95,1.05,leafLight,px,2.05,pz,exterior); cyl(.62,.75,leaf,px-.35,2.45,pz+.22,exterior)}
 for(let i=0;i<16;i++){let px=-19+(i%8)*5.4,pz=-20+Math.floor(i/8)*13; cyl(.12,.36,accent,px,.28,pz,exterior); cyl(.24,.16,windowGlow,px,.5,pz,exterior); animatedLights.push(exterior[exterior.length-1])}
for(let f=0;f<P.floors;f++){let y=f*H; box(W+.5,.25,D+.5,dark,0,y,0); box(W,H,D,wall,0,y+H/2,0); box(W,.16,.2,dark,0,y+H-.08,D/2+.03); box(.2,H,D,dark,-W/2,y+H/2,0); box(.2,H,D,dark,W/2,y+H/2,0); win(-W*.28,y+2,D/2+.08,W*.27,1.8); win(W*.27,y+2,D/2+.08,W*.25,1.8); box(2.2,2.7,.12,wood,0,y+1.35,D/2+.1); box(2.5,2.9,.08,dark,0,y+1.43,D/2+.16); box(W*.74,.18,.8,accent,0,y+.25,D/2+.42); win(-W/2-.06,y+2,0,1.8,1.7); win(W/2+.06,y+2,0,1.8,1.7)}
 // Fine facade fins, balcony rails and warm window bays add an architectural-rendering finish.
 for(let f=0;f<P.floors;f++){let y=f*H; for(let q=-3;q<=3;q++){box(.055,1.72,.12,black,q*1.16*P.scale,y+2,D/2+.17,exterior); box(.055,1.62,.12,black,q*1.16*P.scale,y+2,-D/2-.17,exterior)} for(let q=-2;q<=2;q++){box(.82,.045,.05,windowGlow,q*1.55*P.scale,y+1.32,D/2+.2,nightOnly); box(.82,.045,.05,windowGlow,q*1.55*P.scale,y+1.32,-D/2-.2,nightOnly)} box(W*.75,.06,.06,accent,0,y+2.35,D/2+.32,exterior); box(W*.75,.06,.06,accent,0,y+2.35,-D/2-.32,exterior)}
 // Vertical entry fins and a floating canopy create a strong hero elevation.
 for(let q=-2;q<=2;q++){box(.18,3.15,.18,wood,q*.62,1.62,D/2+.28,exterior)} box(4.6,.12,2.8,glass,0,3.0,D/2+1.05,exterior); box(4.8,.12,.12,accent,0,3.05,D/2+2.42,exterior);
box(W+.65,.35,D+.65,dark,0,P.floors*H+.18,0); box(W*.45,.18,D*.28,accent,0,P.floors*H+.48,-D*.15);
box(5,.22,2.2,accent,0,2.9,D/2+1.15); box(.18,2.8,2.2,dark,-2.25,1.4,D/2+1.1); box(.18,2.8,2.2,dark,2.25,1.4,D/2+1.1);
for(let i=0;i<12;i++){let x=-17+(i%6)*6.8,z=-15+Math.floor(i/6)*11;cyl(.32,1.7,M('#4f6250'),x,.85,z);cyl(1.0,1.35,M('#6f806b'),x,2.25,z)}
if(P.pool){box(5.2,.16,3.4,M('#3f90b8',.12),4.3,.12,3.1,amenity);box(5.8,.12,4,accent,4.3,.03,3.1,amenity);roomLabels.push({text:'🏊 Swimming Pool',pos:[4.3,1.1,3.1],group:'amenity'})}

// Solar Balcony (Mounted on Railing like reference)
if(P.solar){
    for(let i=0;i<6;i++){
        box(1.15,.07,.72,M('#243b52',.18,.35),-3+i*1.15,P.floors*H+.62,D/2+.65,amenity,-.12);
        box(1.05, .85, .05, M('#1a2a3a',.1,.5), -2.85+i*1.15, 1.5, D/2+.45, amenity, 0); // Vertical mount
    }
    roomLabels.push({text:'☀️ Solar Balcony',pos:[0,2.0,D/2+.65],group:'amenity'})
}

// Interior floor
box(W*.94,.12,D*.94,floor,0,.10,0,interior);

// === LIVING ROOM (Modern Timeless Style) ===
const sc=P.scale;
box(4.2*sc,.38,2.1*sc,wood,-2.7*sc,.65,-.9*sc,interior); // Rug/base
box(4.0*sc,.12,2.0*sc,sofa,-2.7*sc,1.0,-.9*sc,interior); // L-Sofa
box(3.8*sc,1.8,.10,wood,-2.7*sc,1.85,-3.7*sc,interior); // Wooden Wall Panel
box(2.2*sc,1.2,.08,dark,-2.7*sc,1.85,-3.6*sc,interior); // Large TV
box(2.4*sc,.65,.55,wood,-2.7*sc,.48,-2.3*sc,interior); // TV Unit
cyl(.6, .1, glass, -2.7*sc, .8, -.9*sc, interior); // Round Center Table
cyl(.05, .8, accent, -2.7*sc, 2.5, -1.4*sc, interior); cyl(.6, .1, glass, -2.7*sc, 2.1, -1.4*sc, interior); // Chandelier
roomLabels.push({text:'🛋️ Living & Dining',pos:[-2.7*sc,2.1,-1.4*sc],group:'interior'});

// === KITCHEN (Modern Grey/Brown Cabinets) ===
box(4.2*sc,.45,.95,dark,3.0*sc,.75,.35,interior); // Lower Cabinets
box(4.0*sc,.10,.75,accent,3.0*sc,1.02,.35,interior); // Countertop
box(4.2*sc,1.0,.6,dark,3.0*sc,2.5,.25,interior); // Upper Cabinets
box(4.3*sc,2.3,.35,wood,3.0*sc,1.25,3.45*sc,interior); // Island/Divider
box(2.5*sc,.06,.55,glass,3.0*sc,2.0,3.23*sc,interior);
roomLabels.push({text:'🍳 Kitchen',pos:[3.0*sc,1.6,.35],group:'interior'});

// === DYNAMIC BEDROOMS (1 to 6 scaling based on user input) ===
const rooms=Math.max(1,Math.min(P.bhk,6));
for(let i=0;i<rooms;i++){
    let x=(i%2===0?-3.1:3.1)*sc, z=(i<2?-3.0:2.65)*sc; 
    box(2.5*sc,.35,2.9*sc,sofa,x,.38,z,interior); // Bed base
    box(2.45*sc,.16,.7,accent,x,.67,z-1.05*sc,interior); // Pillows/Throws
    box(2.2*sc,.07,2.65*sc,wood,x,.20,z,interior); // Rug
    
    // Wooden Wardrobe (Almira)
    box(1.5*sc, 2.4, .7, wood, x+1.2*sc, 1.3, z+1.0*sc, interior);
    
    // Detailed Headboard with Wall Paneling
    box(2.8*sc, 1.2, .15, wood, x, 0.6, z-1.4*sc, interior);
    
    // Nightstands & Lamps
    box(.6, .4, .5, wood, x-1.5*sc, 0.2, z-1.2*sc, interior);
    cyl(.15, .3, M('#ffffff',.2), x-1.5*sc, 0.55, z-1.2*sc, interior);
    box(.6, .4, .5, wood, x+1.5*sc, 0.2, z-1.2*sc, interior);
    cyl(.15, .3, M('#ffffff',.2), x+1.5*sc, 0.55, z-1.2*sc, interior);
    
    // Ceiling Lighting
    cyl(.05, .5, accent, x, 2.5, z, interior);
    cyl(.4, .1, glass, x, 2.25, z, interior);

    roomLabels.push({text:'🛏️ Bedroom '+(i+1),pos:[x,1.0,z],group:'interior'})
}

// === DYNAMIC BATHROOMS (Scales strictly with Bedrooms 1 to 6) ===
for(let i=0;i<rooms;i++){
    let x=(-5.0+i*3.5)*sc,z=4.0*sc; // Spread them along the back
    box(1.5*sc,.08,1.7*sc,M('#cfd3d2',.5),x,.18,z,interior); // Floor
    
    // Floating Vanity & Sink
    box(1.2*sc, .2, .6*sc, wood, x, .7, z+.5*sc, interior); 
    box(.8*sc, .1, .4*sc, M('#ffffff', .1), x, .85, z+.5*sc, interior); 
    // Large Wall Mirror
    box(1.0*sc, 1.0, .05, glass, x, 1.6, z+.75*sc, interior);
    // Modern Toilet
    box(.35, .35, .45, M('#ffffff', .1), x-0.5*sc, .18, z-.5*sc, interior);
    box(.35, .45, .2, M('#ffffff', .1), x-0.5*sc, .55, z-.65*sc, interior);
    
    // Glass Shower Partition
    box(.05, 2.0, 1.5*sc, glass, x+.6*sc, 1.1, z-.1*sc, interior);
    roomLabels.push({text:'🛁 Bath '+(i+1),pos:[x,.9,z],group:'interior'})
}

if(P.gym){box(2.8,.08,1.9,dark,-4,.2,6,amenity);cyl(.5,1.1,accent,-4,.78,6,amenity);box(1.8,.5,.35,dark,-4,.55,5.1,amenity);roomLabels.push({text:'🏋️ Private Gym',pos:[-4,1.4,6],group:'amenity'})}
if(P.cinema){box(3.6,.12,2.4,dark,4.2,.85,-6,amenity);box(3,.3,.9,wood,4.2,.55,-4.75,amenity);box(3,1.8,.08,M('#161a1e'),4.2,1.8,-7.15,amenity);roomLabels.push({text:'🎬 Home Cinema',pos:[4.2,2.2,-6],group:'amenity'})}
if(P.gaming){box(2.2,.8,.8,dark,5,.65,-.2,amenity);box(1.5,.08,.75,glass,5,1.1,-.2,amenity);roomLabels.push({text:'🎮 Gaming Zone',pos:[5,1.5,-.2],group:'amenity'})}

 // Soft architectural uplights make the dusk mode feel like a real presentation render.
 const uplightGroup=[];
 [[-4.5,1.1,4.7],[4.5,1.1,4.7],[-4.5,1.1,-4.7],[4.5,1.1,-4.7]].forEach(p=>{const l=new THREE.PointLight(0xffbd68,0,7);l.position.set(p[0],p[1],p[2]);S.add(l);uplightGroup.push(l)});
 const starGeo=new THREE.BufferGeometry(), starPts=[]; for(let i=0;i<180;i++) starPts.push((Math.random()-.5)*90,18+Math.random()*30,(Math.random()-.5)*90); starGeo.setAttribute('position',new THREE.Float32BufferAttribute(starPts,3)); const stars=new THREE.Points(starGeo,new THREE.PointsMaterial({color:'#d8e6ff',size:.12,transparent:true,opacity:.0})); S.add(stars);

function setGroup(group,visible){group.forEach(o=>o.visible=visible)}
let activeLabelGroups=['exterior'];
 function setView(v){document.getElementById('currentMode').textContent=v; document.querySelectorAll('.controls button').forEach(b=>b.classList.toggle('active',b.dataset.view===v));
  setGroup(exterior,true); setGroup(interior,false); setGroup(amenity,false); ctl.maxPolarAngle=Math.PI*.49; document.getElementById('walk').style.display='none'; activeLabelGroups=[];
   ctl.autoRotate=(v==='Exterior'||v==='Amenities'); ctl.autoRotateSpeed=v==='Amenities'?.22:.32;
   if(v==='Exterior'){C.position.set(19,13,23);ctl.target.set(0,3,0)}
   if(v==='Interior'){setGroup(exterior,false);setGroup(interior,true);C.position.set(13,5.0,14);ctl.target.set(0,1.4,0);activeLabelGroups=['interior']}
   if(v==='Floor Plan'){setGroup(exterior,false);setGroup(interior,true);C.position.set(0,29,.01);ctl.target.set(0,0,0);ctl.maxPolarAngle=.08;activeLabelGroups=['interior']}
   if(v==='Amenities'){setGroup(exterior,true);setGroup(interior,true);setGroup(amenity,true);C.position.set(22,15,25);ctl.target.set(0,2.5,1);activeLabelGroups=['amenity']}
   if(v==='Walkthrough'){setGroup(exterior,false);setGroup(interior,true);setGroup(amenity,true);C.position.set(-10,2.1,9);ctl.target.set(0,1.5,0);document.getElementById('walk').style.display='block';activeLabelGroups=['interior','amenity']}
}
document.querySelectorAll('.controls button').forEach(b=>b.onclick=()=>setView(b.dataset.view));
document.getElementById('reset').onclick=()=>setView(document.querySelector('.controls button.active').dataset.view);
document.getElementById('zoomIn').onclick=()=>{C.position.multiplyScalar(.88)}; document.getElementById('zoomOut').onclick=()=>{C.position.multiplyScalar(1.12)};
 function setLighting(mode){const night=mode==='night'; sun.intensity=night?.42:3.2; hemi.intensity=night?.68:2.2; moon.intensity=night?.72:.18; S.background=new THREE.Color(night?'#05070d':'#10171d'); S.fog.color=new THREE.Color(night?'#05070d':'#10171d'); stars.material.opacity=night?.82:0; nightOnly.forEach(o=>o.visible=night); uplightGroup.forEach(l=>l.intensity=night?2.6:0)}
 document.getElementById('day').onclick=()=>setLighting('day'); document.getElementById('night').onclick=()=>setLighting('night');
document.querySelectorAll('.thumb').forEach(t=>t.onclick=()=>{document.getElementById('photoImg').src=t.dataset.photo;document.getElementById('photoLabel').textContent=t.dataset.label+' • Architectural reference';document.getElementById('photo').style.display='block'});
document.getElementById('startWalk').onclick=()=>{document.getElementById('walk').style.display='none'; const points=[[-10,2.1,9,0,1.5,0],[-5,2.1,3,0,1.5,0],[0,2.1,0,3,1.5,0],[5,2.1,3,0,1.5,2],[-3,2.1,-3,0,1.5,-3]];let i=0;function next(){if(i>=points.length)return;let p=points[i++];C.position.set(p[0],p[1],p[2]);ctl.target.set(p[3],p[4],p[5]);setTimeout(next,1700)}next()};
function rs(){C.aspect=mount.clientWidth/mount.clientHeight;C.updateProjectionMatrix();R.setSize(mount.clientWidth,mount.clientHeight)}addEventListener('resize',rs);
// Floating room-name labels
const _v=new THREE.Vector3();
function updateLabels(){
  labelLayer.innerHTML='';
  roomLabels.forEach(l=>{
    if(!activeLabelGroups.includes(l.group)) return;
    _v.set(l.pos[0],l.pos[1],l.pos[2]).project(C);
    if(_v.z>1||_v.z<-1) return;
    const x=(_v.x*0.5+0.5)*mount.clientWidth, y=(-(_v.y*0.5)+0.5)*mount.clientHeight;
    if(x<0||x>mount.clientWidth||y<0||y>mount.clientHeight) return;
    const el=document.createElement('div'); el.className='roomlabel-el'; el.textContent=l.text; el.style.left=x+'px'; el.style.top=y+'px';
    labelLayer.appendChild(el);
  });
}
 setView(initialView||'Exterior'); setLighting('day');
clearTimeout(safetyTimer);
loadEl.classList.add('hidden');
 const t0=performance.now(); function loop(now){requestAnimationFrame(loop); const pulse=1+Math.sin((now-t0)*.002)*.12; animatedLights.forEach((o,i)=>{if(o.material&&o.material.emissiveIntensity!==undefined)o.material.emissiveIntensity=.28+pulse*.18}); ctl.update();R.render(S,C);updateLabels()}loop(t0);
} catch (err) {
  clearTimeout(safetyTimer);
  console.error('3D view failed to load:', err);
  loadEl.textContent = '⚠️ 3D view failed to load: ' + (err && err.message ? err.message : err) + ' — check internet access to cdn.jsdelivr.net or open browser console (F12) for details.';
  loadEl.classList.remove('hidden');
}
</script></body></html>'''
    html=html.replace('__BHK__',str(bhk)).replace('__STYLE__',design_style).replace('__AREA__',f'{area:,.0f}').replace('__FLOORS__',str(floors)).replace('__AMEN__',amen_html).replace('__GALLERY__',gallery_html).replace('__DATA__',payload).replace('__VIEW__',json.dumps(view_mode))
    components.html(html,height=765,scrolling=False)


def render_architectural_presentation_board(bhk, area, gym=False, pool=False, cinema=False, gaming=False, solar=False, design_style="Modern Luxury", view_mode="Exterior"):
    """Premium offline architectural board with both a furnished 3D cutaway and a 2D floor plan."""
    import html as html_lib
    import json
    bhk = max(1, min(6, int(bhk or 1)))
    area = float(area or 0)
    baths = max(1, min(6, bhk))

    rooms = [
        {"id":"living","name":"LIVING ROOM","kind":"living","x":0,"y":0,"w":6.2,"d":4.2},
        {"id":"dining","name":"DINING","kind":"dining","x":6.2,"y":0,"w":3.2,"d":4.2},
        {"id":"kitchen","name":"KITCHEN","kind":"kitchen","x":9.4,"y":0,"w":3.4,"d":4.2},
        {"id":"master","name":"MASTER BEDROOM","kind":"master","x":0,"y":4.2,"w":5.2,"d":4.4},
    ]
    # Remaining bedrooms in a clean second row.
    for i in range(1, bhk):
        col = (i - 1) % 3
        row = (i - 1) // 3
        rooms.append({"id":f"bed{i}","name":f"BEDROOM {i}","kind":"bed","x":5.2 + col*2.55,"y":4.2 + row*4.0,"w":2.55,"d":4.0})

    bath_y = 8.2 if bhk <= 3 else 12.2
    for i in range(1, baths + 1):
        col = (i - 1) % 3
        row = (i - 1) // 3
        rooms.append({"id":f"bath{i}","name":f"BATHROOM {i}","kind":"bath","x":5.2 + col*2.55,"y":bath_y + row*2.3,"w":2.55,"d":2.2})

    rooms.append({"id":"balcony","name":"BALCONY","kind":"balcony","x":0,"y":-1.35,"w":9.4,"d":1.35})
    rooms.append({"id":"entry","name":"ENTRY","kind":"entry","x":0.7,"y":-0.35,"w":1.7,"d":0.35})

    amenities = []
    if gym: amenities.append("Private Gym")
    if pool: amenities.append("Swimming Pool")
    if cinema: amenities.append("Cinema Hall")
    if gaming: amenities.append("Gaming Zone")
    if solar: amenities.append("Solar Balcony")

    rooms_json = json.dumps(rooms, separators=(",", ":"))
    initial_view = json.dumps(str(view_mode))
    style_json = json.dumps(str(design_style))
    amen_json = json.dumps(amenities)
    safe_style = html_lib.escape(str(design_style))
    room_pills = "".join(f'<span class="room-pill">{html_lib.escape(r["name"])}</span>' for r in rooms)

    html = f'''<!doctype html>
<html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f5ecdf;color:#2e2118;font-family:Arial,Helvetica,sans-serif}}
.board{{max-width:1240px;margin:auto;background:#f7efe4;border:1px solid #d7b77c;border-radius:22px;overflow:hidden;box-shadow:0 18px 55px rgba(72,48,27,.16)}}
.head{{padding:20px 25px;background:linear-gradient(135deg,#3b2416,#78502f);color:#fff;display:flex;justify-content:space-between;align-items:center;gap:18px}}
.brand{{font-size:25px;font-weight:900;letter-spacing:3px}} .brand span{{color:#e4ad45}}
.sub{{font-size:10px;letter-spacing:2px;color:#f1dec0;margin-top:7px}}
.badge{{border:1px solid #e1aa3e;border-radius:999px;padding:10px 15px;color:#f3c15d;font-size:10px;font-weight:900;white-space:nowrap}}
.toolbar{{padding:12px 16px;background:#fffaf3;border-bottom:1px solid #e3d2b7;display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.mode{{border:1px solid #b98a42;background:#fffaf2;color:#62452d;border-radius:10px;padding:9px 14px;font-weight:900;cursor:pointer}}
.mode.active,.mode:hover{{background:#c99236;color:#fff}}
.toolbar .hint{{margin-left:auto;font-size:11px;color:#806b56}}
.stage{{height:650px;position:relative;background:#17100b;overflow:hidden;touch-action:none}}
canvas{{position:absolute;inset:0;width:100%;height:100%;cursor:grab}}
canvas.drag{{cursor:grabbing}}
.info,.hintbox,.legend{{position:absolute;z-index:5;background:rgba(19,13,9,.92);border:1px solid #8f642e;border-radius:13px;color:#fff;padding:10px 13px}}
.info{{left:15px;top:15px}} .info small{{display:block;color:#cdb99c;margin-top:4px;font-size:10px}}
.hintbox{{right:15px;top:15px;color:#e2c9a3;font-size:10px}}
.legend{{right:15px;bottom:18px;max-width:260px;color:#ead7bb;font-size:10px;line-height:1.5}}
.controls{{position:absolute;left:15px;bottom:16px;display:flex;gap:7px;flex-wrap:wrap;z-index:6}}
.btn{{border:1px solid #a9762b;background:#21150d;color:#ead9bc;border-radius:10px;padding:9px 12px;font-weight:800;cursor:pointer}}
.btn.active,.btn:hover{{background:#d49a35;color:#fff}}
.details{{padding:22px 25px}} .eyebrow{{color:#9a6829;font-weight:900;letter-spacing:4px;font-size:10px}}
h2{{font-family:Georgia,serif;font-size:30px;margin:7px 0}} .meta{{color:#705a45;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:9px;margin-top:16px}}
.card{{background:#fffaf2;border:1px solid #e0cba4;border-radius:13px;padding:11px}} .card b{{display:block;font-size:16px;color:#5a3b22}} .card span{{font-size:8px;color:#9a7a59;letter-spacing:1px}}
.room-list{{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}} .room-pill{{background:#fffaf2;border:1px solid #ddc59f;border-radius:999px;padding:6px 9px;font-size:9px;color:#60452e;font-weight:800}}
.amen{{margin-top:12px;border-top:1px solid #ddc49a;padding-top:10px;font-size:10px;color:#74563b}}
@media(max-width:760px){{.head{{padding:15px}}.badge{{display:none}}.toolbar .hint{{width:100%;margin-left:0}}.stage{{height:600px}}.grid{{grid-template-columns:repeat(2,1fr)}}.legend{{display:none}}}}
</style></head><body>
<div class="board">
<header class="head"><div><div class="brand">SMART<span>ESTATE</span></div><div class="sub">PREMIUM ARCHITECTURAL PRESENTATION · FURNISHED HOME PLAN · {safe_style.upper()}</div></div><div class="badge">2D PLAN + 3D CUTAWAY</div></header>
<div class="toolbar">
<button class="mode active" data-mode="3D">🏠 3D Structure</button><button class="mode" data-mode="2D">📐 2D Floor Plan</button>
<button class="mode" data-mode="Exterior">🏡 Exterior</button><button class="mode" data-mode="Interior">🛋️ Interior</button><button class="mode" data-mode="Top">⬆️ Top View</button><button class="mode" data-mode="Night">🌙 Night</button><button class="mode" data-mode="Reset">↺ Reset</button>
<span class="hint">3D: drag rotate · wheel zoom · click any room to focus &nbsp;|&nbsp; 2D: click room to open it</span>
</div>
<section class="stage" id="stage"><div class="info"><b>{bhk} BHK · {area:,.0f} sq.ft</b><small>Living · Dining · Kitchen · Master Bedroom · Bedroom 1–{max(1,bhk-1)} · Bathrooms · Balcony</small></div><div class="hintbox" id="hint">Drag to rotate · Wheel to zoom</div><canvas id="c"></canvas><div class="legend" id="legend"><b>3D CUTAWAY</b><br>Open roof/walls reveal the furnished rooms, partitions, doors, windows and furniture. Select a room to highlight it.</div></section>
<section class="details"><div class="eyebrow">COMPLETE HOME VISUALIZATION</div><h2>{bhk} BHK · FURNISHED ARCHITECTURAL MODEL</h2><div class="meta">{area:,.0f} sq.ft · {safe_style} · offline interactive 2D + 3D structure</div><div class="grid"><div class="card"><b>{bhk}</b><span>BEDROOMS</span></div><div class="card"><b>{baths}</b><span>BATHROOMS</span></div><div class="card"><b>{area:,.0f}</b><span>SQ.FT</span></div><div class="card"><b>1</b><span>LIVING</span></div><div class="card"><b>1</b><span>KITCHEN</span></div></div><div class="room-list">{room_pills}</div><div class="amen"><b>Premium amenities:</b> {html_lib.escape(" • ".join(amenities) if amenities else "Premium standard configuration")}</div></section>
</div>
<script>
const ROOMS={rooms_json};
const AMEN={amen_json};
const INITIAL={initial_view};
const STYLE={style_json};
const canvas=document.getElementById('c'),ctx=canvas.getContext('2d'),stage=document.getElementById('stage');
let W=0,H=0,dpr=1,mode='3D',view='Exterior',night=false,yaw=-0.68,pitch=0.52,zoom=1.0,drag=false,px=0,py=0,selected=null;
const palette={{living:'#c99758',dining:'#d8ad70',kitchen:'#b77d3d',master:'#d1aa72',bed:'#d9b986',bath:'#8ebdc0',balcony:'#7e9d78',entry:'#b8905c'}};
function resize(){{const r=stage.getBoundingClientRect();W=r.width;H=r.height;dpr=Math.min(2,devicePixelRatio||1);canvas.width=W*dpr;canvas.height=H*dpr;canvas.style.width=W+'px';canvas.style.height=H+'px';ctx.setTransform(dpr,0,0,dpr,0,0);draw();}}
function rotate(p){{const cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);let x=p[0]*cy-p[1]*sy,y=p[0]*sy+p[1]*cy,z=p[2];let yy=y*cp-z*sp,zz=y*sp+z*cp;return[x,yy,zz];}}
function project(p){{const q=rotate(p),s=43*zoom;return[W/2+q[0]*s,H/2+q[1]*s-q[2]*s*0.82,q[2]];}}
function poly(ps,fill,stroke='#5a4028',lw=1){{ctx.beginPath();ps.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));ctx.closePath();ctx.fillStyle=fill;ctx.fill();ctx.strokeStyle=stroke;ctx.lineWidth=lw;ctx.stroke();}}
function box(x,y,z,w,d,h,top,side,front,alpha=1){{const a=project([x,y,z]),b=project([x+w,y,z]),c=project([x+w,y+d,z]),dd=project([x,y+d,z]),e=project([x,y,z+h]),f=project([x+w,y,z+h]),g=project([x+w,y+d,z+h]),hh=project([x,y+d,z+h]);ctx.globalAlpha=alpha;poly([e,f,g,hh],top);poly([b,c,g,f],side);poly([a,b,f,e],front);poly([a,dd,hh,e],front);ctx.globalAlpha=1;}}
function text3(t,x,y,z,size=11,color='#fff1d0'){{const p=project([x,y,z]);ctx.font='800 '+size+'px Arial';ctx.textAlign='center';ctx.fillStyle='rgba(0,0,0,.72)';ctx.fillText(t,p[0]+1,p[1]+2);ctx.fillStyle=color;ctx.fillText(t,p[0],p[1]);}}
function line3(a,b,color='#a97b3e',width=1){{const p=project(a),q=project(b);ctx.beginPath();ctx.moveTo(p[0],p[1]);ctx.lineTo(q[0],q[1]);ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke();}}
function roomColor(r){{return palette[r.kind]||'#c6a16b';}}
function furniture(r){{let x=r.x+.25,y=r.y+.25,w=r.w-.5,d=r.d-.5,z=.26;if(r.kind==='living'){{box(x+1,y+1,z,2.7,.65,.45,'#ad8459','#76553a','#8f6847');box(x+1.1,y+2,z,2.3,.55,.35,'#c19a6b','#806044','#a37b54');box(x+4.35,y+1,z,.85,.25,1.0,'#3f3026','#2b211b','#564236');}}else if(r.kind==='dining'){{box(x+.55,y+1.0,z,1.9,1.05,.45,'#9a6c42','#65472f','#7a5638');for(let i=0;i<4;i++)box(x+.25+(i%2)*2.05,y+.2+Math.floor(i/2)*1.6,z,.32,.32,.42,'#a77b4e','#67482f','#855f3e');}}else if(r.kind==='kitchen'){{box(x+.15,y+.25,z,.55,d-.5,1.05,'#6c513c','#46362a','#806145');box(x+.9,y+.25,z,w-1.05,.55,.82,'#76563b','#4f392a','#8b6848');}}else if(r.kind==='master'||r.kind==='bed'){{let bw=Math.min(2.05,w-1),bd=Math.min(2.35,d-1);box(x+.55,y+.8,z,bw,bd,.3,'#bda27d','#80654a','#a48663');box(x+.75,y+.28,z+.3,1.0,.35,.5,'#efe1c4','#a58c70','#d4bea0');}}else if(r.kind==='bath'){{box(x+.25,y+.25,z,.85,.5,.25,'#e8f2ef','#94aaa7','#c4d8d4');box(x+1.45,y+.75,z,.6,.6,.35,'#e8f2ef','#94aaa7','#c4d8d4');}}else if(r.kind==='balcony'){{for(let i=0;i<9;i++)box(x+.12+i*(w-.25)/9,y+d-.08,z+.08,.05,.05,.95,'#d9a13b','#855d27','#b47d2c');}}else if(r.kind==='entry'){{box(x+.15,y+.03,z,.85,.15,1.8,'#7c4b2b','#4c2f1d','#9c653b');}}}}
function draw3D(){{
  ctx.fillStyle=night?'#090705':'#17100b';ctx.fillRect(0,0,W,H);
  const maxX=Math.max(...ROOMS.map(r=>r.x+r.w))+0.6,maxY=Math.max(...ROOMS.map(r=>r.y+r.d))+0.6;
  // ground and garden
  box(-1.0,-1.8,0,maxX+1.6,maxY+2.1,.12,'#4d3827','#2e2119','#3b2a20');
  if({str(bool(pool)).lower()}){{box(maxX-3.2,-1.25,.13,2.6,1.1,.05,'#4aa8b8','#1e6571','#2e8d9b');}}
  // floor slabs / rooms
  ROOMS.forEach(r=>{{let base=r.kind==='balcony'?'#6f8c6d':roomColor(r);box(r.x,r.y,.16,r.w,r.d,.12,base,'#705036','#8f6b49',0.98);}});
  // furniture first so room labels remain on top
  ROOMS.forEach(r=>furniture(r));
  // low partitions around each room; roof intentionally omitted for cutaway
  ROOMS.forEach(r=>{{const c=selected===r.id?'#f1c45c':'#d9c19a';const a=selected===r.id?.98:.88;const h=r.kind==='balcony'?.75:1.7;box(r.x,r.y,.28,r.w,.08,h,c,'#9b6c3a','#b78751',a);box(r.x,r.y+r.d-.08,.28,r.w,.08,h,c,'#9b6c3a','#b78751',a);box(r.x,r.y,.28,.08,r.d,h,c,'#9b6c3a','#b78751',a);box(r.x+r.w-.08,r.y,.28,.08,r.d,h,c,'#9b6c3a','#b78751',a);}});
  // open front facade and small roof canopy for premium exterior feel
  box(-.1,-.2,2.05,maxX+.2,.15,.18,'#704526','#3d2618','#5b371f',.8);
  ROOMS.forEach(r=>{{let cx=r.x+r.w/2,cy=r.y+r.d/2;text3(r.name,cx,cy,.5,Math.max(8,Math.min(12,10/zoom)),selected===r.id?'#ffd66d':'#fff1d0');}});
  // entrance path
  for(let i=0;i<5;i++)box(.9,-2.35+i*.25,.13,1.6,.18,.03,'#8c7560','#59483a','#6d5948');
  if(night){{ctx.fillStyle='rgba(7,3,1,.18)';ctx.fillRect(0,0,W,H);}}
}}
function draw2D(){{
  ctx.fillStyle='#f8f4ed';ctx.fillRect(0,0,W,H);
  const pad=48, maxX=Math.max(...ROOMS.map(r=>r.x+r.w)), maxY=Math.max(...ROOMS.map(r=>r.y+r.d));
  const scale=Math.min((W-pad*2)/maxX,(H-pad*2)/maxY); const ox=(W-maxX*scale)/2, oy=(H-maxY*scale)/2+10;
  // dimension frame
  ctx.strokeStyle='#b48b51';ctx.lineWidth=1;ctx.strokeRect(ox,oy,maxX*scale,maxY*scale);
  ROOMS.forEach(r=>{{let x=ox+r.x*scale,y=oy+r.y*scale,w=r.w*scale,d=r.d*scale;ctx.fillStyle=selected===r.id?'#e8c16b':roomColor(r);ctx.globalAlpha=.78;ctx.fillRect(x,y,w,d);ctx.globalAlpha=1;ctx.strokeStyle='#6d5743';ctx.lineWidth=1.5;ctx.strokeRect(x,y,w,d);drawFurniture2D(r,x,y,w,d);ctx.fillStyle='#3e3026';ctx.font='700 '+Math.max(9,Math.min(15,w/9))+'px Arial';ctx.textAlign='center';ctx.fillText(r.name,x+w/2,y+d/2);}});
  // balcony
  ctx.fillStyle='#78916f';ctx.globalAlpha=.7;ctx.fillRect(ox,oy-1.35*scale,9.4*scale,1.35*scale);ctx.globalAlpha=1;ctx.strokeStyle='#6d5743';ctx.strokeRect(ox,oy-1.35*scale,9.4*scale,1.35*scale);ctx.fillStyle='#3e3026';ctx.font='700 11px Arial';ctx.fillText('BALCONY',ox+4.7*scale,oy-.7*scale);
  // dimension lines
  ctx.fillStyle='#60472f';ctx.font='11px Arial';ctx.textAlign='center';ctx.fillText(maxX.toFixed(1)+' m',W/2,oy+maxY*scale+24);ctx.save();ctx.translate(ox-24,oy+maxY*scale/2);ctx.rotate(-Math.PI/2);ctx.fillText(maxY.toFixed(1)+' m',0,0);ctx.restore();
  ctx.textAlign='left';ctx.fillText('2D FURNISHED FLOOR PLAN',18,25);ctx.font='10px Arial';ctx.fillText('Room labels • furniture • bathrooms • balcony',18,42);
}}
function drawFurniture2D(r,x,y,w,d){{ctx.save();ctx.strokeStyle='#5d4633';ctx.fillStyle='rgba(255,255,255,.32)';ctx.lineWidth=1;if(r.kind==='living'){{ctx.fillRect(x+w*.2,y+d*.35,w*.5,d*.16);ctx.strokeRect(x+w*.2,y+d*.35,w*.5,d*.16);ctx.fillRect(x+w*.68,y+d*.58,w*.12,d*.22);}}else if(r.kind==='dining'){{ctx.beginPath();ctx.ellipse(x+w*.5,y+d*.5,w*.22,d*.18,0,0,Math.PI*2);ctx.fill();ctx.stroke();}}else if(r.kind==='kitchen'){{ctx.fillRect(x+w*.08,y+d*.08,w*.84,d*.18);ctx.strokeRect(x+w*.08,y+d*.08,w*.84,d*.18);}}else if(r.kind==='master'||r.kind==='bed'){{ctx.fillRect(x+w*.2,y+d*.25,w*.55,d*.5);ctx.strokeRect(x+w*.2,y+d*.25,w*.55,d*.5);ctx.fillStyle='#eadcc5';ctx.fillRect(x+w*.23,y+d*.27,w*.49,d*.12);}}else if(r.kind==='bath'){{ctx.beginPath();ctx.arc(x+w*.35,y+d*.42,Math.min(w,d)*.11,0,Math.PI*2);ctx.stroke();ctx.strokeRect(x+w*.55,y+d*.25,w*.22,d*.35);}}ctx.restore();}}
function draw(){{mode==='2D'?draw2D():draw3D();}}
function setMode(m){{if(m==='Reset'){{yaw=-.68;pitch=.52;zoom=1;selected=null;mode='3D';view='Exterior';night=false;}}else if(m==='2D'){{mode='2D';}}else if(m==='3D'){{mode='3D';view='Exterior';night=false;}}else if(m==='Top'){{mode='3D';view='Top';night=false;yaw=-.18;pitch=1.22;zoom=1.08;}}else if(m==='Night'){{mode='3D';night=true;view='Night';}}else{{mode='3D';view=m;night=false;}}document.querySelectorAll('.mode').forEach(b=>b.classList.toggle('active',b.dataset.mode===m));document.getElementById('hint').textContent=mode==='2D'?'2D top view · click a room to highlight':'Drag to rotate · Wheel to zoom · Click a room';document.getElementById('legend').innerHTML=mode==='2D'?'<b>2D FLOOR PLAN</b><br>Top-view furnished layout with room names, furniture, bathrooms and balcony.':'<b>3D CUTAWAY</b><br>Open-roof furnished structure with room partitions, doors, windows and furniture.';draw();}}
document.querySelectorAll('.mode').forEach(b=>b.addEventListener('click',()=>setMode(b.dataset.mode)));
canvas.addEventListener('pointerdown',e=>{{drag=true;px=e.clientX;py=e.clientY;canvas.classList.add('drag');canvas.setPointerCapture(e.pointerId);}});
canvas.addEventListener('pointermove',e=>{{if(!drag||mode!=='3D')return;const dx=e.clientX-px,dy=e.clientY-py;px=e.clientX;py=e.clientY;yaw+=dx*.009;pitch=Math.max(.12,Math.min(1.25,pitch+dy*.006));draw();}});
canvas.addEventListener('pointerup',e=>{{drag=false;canvas.classList.remove('drag');}});
canvas.addEventListener('wheel',e=>{{e.preventDefault();zoom=Math.max(.55,Math.min(1.8,zoom*(e.deltaY<0?1.08:.92)));draw();}},{{passive:false}});
function focusRoom(r){{
  selected=r.id;
  const cx=r.x+r.w/2, cy=r.y+r.d/2;
  // Rotate the selected room toward the viewer, then zoom in so the
  // user feels like the camera has travelled to that room.
  yaw=Math.atan2(cx,cy);
  pitch=.68;
  zoom=1.42;
  mode='3D';
  view='Interior';
  document.querySelectorAll('.mode').forEach(b=>b.classList.toggle('active',b.dataset.mode==='Interior'));
  document.getElementById('hint').textContent='📍 '+r.name+' selected · drag to explore · wheel to zoom';
  document.getElementById('legend').innerHTML='<b>ROOM FOCUS</b><br>'+r.name+' is highlighted. The camera is focused on this room — drag to explore the surrounding spaces.';
  draw();
}}
function hitTest3D(mx,my){{
  let hit=null,best=Infinity;
  ROOMS.forEach(r=>{{
    const pts=[project([r.x,r.y,.2]),project([r.x+r.w,r.y,.2]),project([r.x+r.w,r.y+r.d,.2]),project([r.x,r.y+r.d,.2])];
    const minX=Math.min(...pts.map(p=>p[0]))-8,maxX=Math.max(...pts.map(p=>p[0]))+8;
    const minY=Math.min(...pts.map(p=>p[1]))-8,maxY=Math.max(...pts.map(p=>p[1]))+8;
    if(mx>=minX&&mx<=maxX&&my>=minY&&my<=maxY){{
      const cx=(minX+maxX)/2,cy=(minY+maxY)/2,d=(mx-cx)*(mx-cx)+(my-cy)*(my-cy);
      if(d<best){{best=d;hit=r;}}
    }}
  }});
  return hit;
}}
canvas.addEventListener('click',e=>{{
  if(drag)return;
  const rect=canvas.getBoundingClientRect(),mx=e.clientX-rect.left,my=e.clientY-rect.top;
  if(mode==='2D'){{
    const pad=48,maxX=Math.max(...ROOMS.map(r=>r.x+r.w)),maxY=Math.max(...ROOMS.map(r=>r.y+r.d)),scale=Math.min((W-pad*2)/maxX,(H-pad*2)/maxY),ox=(W-maxX*scale)/2,oy=(H-maxY*scale)/2+10;
    selected=null;
    ROOMS.forEach(r=>{{if(mx>=ox+r.x*scale&&mx<=ox+(r.x+r.w)*scale&&my>=oy+r.y*scale&&my<=oy+(r.y+r.d)*scale)selected=r.id;}});
    const picked=ROOMS.find(r=>r.id===selected);
    if(picked){{mode='3D';focusRoom(picked);}} else draw();
  }} else {{
    const picked=hitTest3D(mx,my);
    if(picked) focusRoom(picked);
  }}
}});
window.addEventListener('resize',resize);resize();setMode(INITIAL==='Floor Plan'?'2D':'3D');
</script></body></html>'''
    components.html(html, height=930, scrolling=False)


AMENITY_PRICE_KEYS = (
    ("Gym", "gym"),
    ("Swimming Pool", "pool"),
    ("Cinema Hall", "cinema"),
    ("Gaming Zone", "gaming"),
    ("Solar Balcony", "solar"),
)

def get_amenity_price_breakdown(city, area_name, area, bhk, bathrooms, gym=False, pool=False, cinema=False, gaming=False, solar=False):
    """Return model-derived premium for each amenity in Lakhs.

    Each premium is calculated against the same property configuration with
    only that single amenity enabled. Negative deltas are clamped to zero.
    """
    selected = {
        "gym": bool(gym),
        "pool": bool(pool),
        "cinema": bool(cinema),
        "gaming": bool(gaming),
        "solar": bool(solar),
    }
    base = float(get_prediction(city, area_name, area, bhk, bathrooms, False, False, False, False, False) or 0.0)
    rows = []
    total = 0.0
    for label, key in AMENITY_PRICE_KEYS:
        flags = {k: False for _, k in AMENITY_PRICE_KEYS}
        flags[key] = True
        one = float(get_prediction(
            city, area_name, area, bhk, bathrooms,
            flags["gym"], flags["pool"], flags["cinema"],
            flags["gaming"], flags["solar"]
        ) or 0.0)
        premium = max(0.0, one - base)
        rows.append({
            "name": label,
            "key": key,
            "premium": premium,
            "selected": selected[key],
        })
        if selected[key]:
            total += premium
    return {
        "base": base,
        "selected_total": float(total),
        "items": rows,
    }


def render_amenity_price_card(city, area_name, area, bhk, bathrooms, gym=False, pool=False, cinema=False, gaming=False, solar=False, compact=False):
    """Render the shared amenity-pricing breakdown used by both valuation flows."""
    if not model:
        return
    info = get_amenity_price_breakdown(city, area_name, area, bhk, bathrooms, gym, pool, cinema, gaming, solar)
    selected = [x for x in info["items"] if x["selected"]]
    if not selected:
        return
    cards = "".join(
        f'<div style="padding:10px 12px;border:1px solid rgba(184,137,69,.18);border-radius:12px;background:#fffaf1;min-width:130px;flex:1;">'
        f'<div style="font-size:11px;color:#806d5a;font-weight:800;">{html.escape(x["name"])}</div>'
        f'<div style="font-size:18px;color:#a26f2d;font-weight:900;margin-top:3px;">+₹{x["premium"]:.2f} L</div>'
        f'</div>'
        for x in selected
    )
    st.markdown(
        f'<div class="intel-box" style="margin-top:{8 if compact else 14}px;padding:14px;">'
        f'<div class="intel-title">💎 Amenity Price Impact</div>'
        f'<div style="font-size:11px;color:#806d5a;margin:3px 0 10px;">Same AI model-derived premium in AI Valuation and Budget-First Search.</div>'
        f'<div style="display:flex;gap:8px;flex-wrap:wrap;">{cards}</div>'
        f'<div style="margin-top:10px;font-size:13px;color:#4b3526;font-weight:800;">Selected amenities contribution: +₹{info["selected_total"]:.2f} L</div>'
        f'</div>', unsafe_allow_html=True
    )

# NEIGHBORHOOD GENERATOR
def _haversine_km(lat1, lon1, lat2, lon2):
    """Straight-line distance between two latitude/longitude points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@st.cache_data(ttl=900, show_spinner=False)
@st.cache_data(ttl=1800, show_spinner=False)
def get_real_pois(area_name, city):
    """Fast server-side POI fallback.
    Live Neighborhood Highlights are loaded client-side with Leaflet/Overpass
    so the Streamlit rerun is not blocked by external map APIs.
    """
    coords = EXACT_COORDS.get(area_name, CITY_DEFAULTS.get(city))
    if not coords:
        return []

    lat, lon = coords
    query = f"""[out:json][timeout:6];
    nwr(around:2200,{lat},{lon})[name][amenity~"school|hospital|clinic|cafe|restaurant"];
    nwr(around:2200,{lat},{lon})[name][shop~"mall|supermarket|shopping_centre"];
    nwr(around:2200,{lat},{lon})[name][leisure~"park|garden"];
    nwr(around:2200,{lat},{lon})[name][public_transport];
    nwr(around:2200,{lat},{lon})[name][railway~"station|subway_entrance"];
    out center tags;"""

    # Only one short request here. The interactive UI below has its own
    # browser-side retry/fallback endpoints.
    try:
        r = requests.post(
            "https://overpass-api.de/api/interpreter",
            data=query,
            timeout=7,
            headers={"User-Agent": "HousePricePrediction/2.0"},
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    rules = [
        ("🏫", "School", lambda t: t.get("amenity") == "school"),
        ("🏥", "Hospital", lambda t: t.get("amenity") in ("hospital", "clinic")),
        ("🛍️", "Shopping", lambda t: t.get("shop") in ("mall", "supermarket", "shopping_centre")),
        ("🚇", "Transit", lambda t: bool(t.get("public_transport")) or t.get("railway") in ("station", "subway_entrance")),
        ("🌳", "Park", lambda t: t.get("leisure") in ("park", "garden")),
        ("☕", "Cafe", lambda t: t.get("amenity") in ("cafe", "restaurant")),
    ]

    found = {}
    for el in data.get("elements", []):
        tags = el.get("tags", {}) or {}
        name = tags.get("name") or tags.get("name:en")
        plat = el.get("lat", el.get("center", {}).get("lat"))
        plon = el.get("lon", el.get("center", {}).get("lon"))
        if not name or plat is None or plon is None:
            continue

        dist = _haversine_km(lat, lon, float(plat), float(plon))
        for icon, category, matcher in rules:
            if matcher(tags):
                key = (category, str(name).strip().lower())
                item = {
                    "icon": icon, "category": category, "name": str(name).strip(),
                    "lat": float(plat), "lon": float(plon), "distance": dist
                }
                if key not in found or dist < found[key]["distance"]:
                    found[key] = item
                break

    result = []
    for category in ["School", "Hospital", "Shopping", "Transit", "Park", "Cafe"]:
        items = sorted(
            [x for x in found.values() if x["category"] == category],
            key=lambda x: x["distance"]
        )
        result.extend(items[:2])
    return result



def get_neighborhood_facilities(area_name, city, radius_m=2000):
    """Fast and resilient real nearby-place lookup within 2 km."""
    coords = EXACT_COORDS.get(area_name, CITY_DEFAULTS.get(city))
    if not coords:
        return []
    lat, lon = float(coords[0]), float(coords[1])
    radius_m = min(int(radius_m or 2000), 2000)
    query = f"""[out:json][timeout:8];
    (
      nwr(around:{radius_m},{lat},{lon})[name][amenity~\"school|college|university|hospital|clinic|doctors|dentist|pharmacy|bank|atm|restaurant|fast_food|cafe|cinema|fuel|police|post_office|library|place_of_worship\"];
      nwr(around:{radius_m},{lat},{lon})[name][shop~\"mall|supermarket|department_store|shopping_centre\"];
      nwr(around:{radius_m},{lat},{lon})[name][leisure~\"park|garden|playground|fitness_centre\"];
      nwr(around:{radius_m},{lat},{lon})[name][railway~\"station|halt|tram_stop\"];
      nwr(around:{radius_m},{lat},{lon})[name][highway=\"bus_stop\"];
    ); out center tags;"""
    endpoints = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
    headers = {"User-Agent":"SmartEstate/NeighborhoodHighlights/6.0", "Accept":"application/json"}
    data = None
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _post(ep):
        r=requests.post(ep,data=query,timeout=(2.0,7.5),headers=headers); r.raise_for_status(); return r.json()
    def _get(ep):
        r=requests.get(ep,params={"data":query},timeout=(2.0,7.5),headers=headers); r.raise_for_status(); return r.json()
    for fn in (_post, _get):
        if data is not None: break
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures=[pool.submit(fn,ep) for ep in endpoints]
                for f in as_completed(futures):
                    try:
                        candidate=f.result()
                        if isinstance(candidate,dict) and candidate.get("elements") is not None:
                            data=candidate; break
                    except Exception: pass
        except Exception: pass
    rules=[
        ("🏫","School",lambda t:t.get("amenity")=="school"),("🎓","College / University",lambda t:t.get("amenity") in ("college","university")),
        ("🏥","Hospital",lambda t:t.get("amenity")=="hospital"),("🩺","Healthcare / Doctor",lambda t:t.get("amenity") in ("clinic","dentist","doctors")),
        ("💊","Pharmacy",lambda t:t.get("amenity")=="pharmacy"),("🏬","Shopping Mall",lambda t:t.get("shop") in ("mall","department_store","shopping_centre")),
        ("🛒","Shop / Supermarket",lambda t:t.get("shop")=="supermarket"),("🏦","Bank / ATM",lambda t:t.get("amenity") in ("bank","atm")),
        ("🍽️","Restaurant",lambda t:t.get("amenity") in ("restaurant","fast_food")),("☕","Cafe",lambda t:t.get("amenity")=="cafe"),
        ("🌳","Park / Garden",lambda t:t.get("leisure") in ("park","garden","playground")),("🚉","Metro / Railway",lambda t:t.get("railway") in ("station","halt","tram_stop")),
        ("🚌","Bus / Transit",lambda t:t.get("highway")=="bus_stop"),("⛽","Petrol Pump",lambda t:t.get("amenity")=="fuel"),
        ("👮","Police",lambda t:t.get("amenity")=="police"),("🛕","Worship",lambda t:t.get("amenity")=="place_of_worship"),
        ("🏋️","Gym / Fitness",lambda t:t.get("leisure")=="fitness_centre"),("🎬","Cinema",lambda t:t.get("amenity")=="cinema"),
        ("📮","Post Office",lambda t:t.get("amenity")=="post_office"),("📚","Library",lambda t:t.get("amenity")=="library")]
    found={}
    if data:
        for element in data.get("elements",[]):
            tags=element.get("tags") or {}; name=tags.get("name") or tags.get("name:en"); point=element.get("center") or {}
            plat=element.get("lat",point.get("lat")); plon=element.get("lon",point.get("lon"))
            if not name or plat is None or plon is None: continue
            matched=None
            for icon,cat,matcher in rules:
                try:
                    if matcher(tags): matched=(icon,cat); break
                except Exception: pass
            if not matched: continue
            try: dist=_haversine_km(lat,lon,float(plat),float(plon))
            except Exception: continue
            if dist > radius_m/1000.0 + 0.05: continue
            key=(matched[1],str(name).strip().lower()); item={"icon":matched[0],"category":matched[1],"name":str(name).strip(),"lat":float(plat),"lon":float(plon),"distance":float(dist)}
            if key not in found or dist < found[key]["distance"]: found[key]=item
    if not found:
        categories=[("School","school","🏫"),("Hospital","hospital","🏥"),("Pharmacy","pharmacy","💊"),("Shopping Mall","shopping mall","🏬"),("Restaurant","restaurant","🍽️"),("Cafe","cafe","☕"),("Park / Garden","park","🌳"),("Gym / Fitness","gym","🏋️")]
        def _nom(q):
            r=requests.get("https://nominatim.openstreetmap.org/search",params={"q":f"{q[1]}, {area_name}, {city}","format":"jsonv2","limit":3,"viewbox":f"{lon-0.025},{lat+0.025},{lon+0.025},{lat-0.025}","bounded":1},headers={"User-Agent":"SmartEstate/NeighborhoodHighlights/6.0"},timeout=(2.0,5.0)); r.raise_for_status(); return q,r.json()
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                for q,items in pool.map(_nom,categories):
                    for item in items or []:
                        try:
                            plat,plon=float(item["lat"]),float(item["lon"]); dist=_haversine_km(lat,lon,plat,plon)
                            if dist <= radius_m/1000.0 and item.get("display_name"):
                                name=str(item["display_name"]).split(",")[0].strip(); found[(q[0],name.lower())]={"icon":q[2],"category":q[0],"name":name,"lat":plat,"lon":plon,"distance":dist}
                        except Exception: pass
        except Exception: pass
    if not found: return []
    order={c:i for i,c in enumerate(["School","College / University","Hospital","Healthcare / Doctor","Pharmacy","Shopping Mall","Shop / Supermarket","Bank / ATM","Restaurant","Cafe","Park / Garden","Metro / Railway","Bus / Transit","Petrol Pump","Police","Worship","Gym / Fitness","Cinema","Post Office","Library"])}
    return sorted(found.values(),key=lambda x:(float(x.get("distance",999)),order.get(x.get("category"),99),x["name"].lower()))[:10]

def render_advanced_neighborhood(area_name, city):
    """Fast version of the original Neighborhood Highlights UI.

    Keeps the original named-facility cards/filters, but uses the cached live
    OSM result so revisiting an area is instant. The first lookup is kept
    deliberately small and short so it does not hold the whole app for long.
    """
    cache_key = f"nh_facilities_{city}_{area_name}"
    facilities = st.session_state.get(cache_key)

    if not facilities:
        with st.spinner("Loading real nearby facilities within 2 km..."):
            fresh = get_neighborhood_facilities(area_name, city, radius_m=2000)
        # Only cache a successful result. Network/API failures must remain retryable.
        if fresh:
            facilities = fresh
            st.session_state[cache_key] = fresh
        else:
            facilities = []

    if not facilities:
        st.warning("OpenStreetMap is temporarily unavailable. Click the button again to retry the real 2 km search.")
        if st.button("🔄 Retry Nearby Facilities", key=f"nh_retry_{city}_{area_name}", use_container_width=True):
            st.session_state.pop(cache_key, None)
            smart_rerun()
        return

    categories = ["All"] + sorted({str(x.get("category", "Facility")) for x in facilities})
    ncol1, ncol2, ncol3 = st.columns([1.3, 1.0, 1.0])
    with ncol1:
        selected_cat = st.selectbox("Category", categories, key=f"nh_cat_{city}_{area_name}")
    with ncol2:
        sort_mode = st.selectbox("Sort by", ["Nearest", "Category", "Name"], key=f"nh_sort_{city}_{area_name}")
    with ncol3:
        max_show = st.selectbox("Show", [10, 15, 20, 30], index=1, key=f"nh_count_{city}_{area_name}")

    visible = facilities if selected_cat == "All" else [x for x in facilities if x.get("category") == selected_cat]
    if sort_mode == "Category":
        visible = sorted(visible, key=lambda x: (x.get("category", ""), float(x.get("distance", 999))))
    elif sort_mode == "Name":
        visible = sorted(visible, key=lambda x: str(x.get("name", "")).lower())
    else:
        visible = sorted(visible, key=lambda x: float(x.get("distance", 999)))
    visible = visible[:int(max_show)]

    nearest = min(facilities, key=lambda x: float(x.get("distance", 999)))
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("📍 Places", len(facilities))
    s2.metric("🗂️ Categories", len({x.get("category") for x in facilities}))
    s3.metric("🚶 Nearest", f"{nearest.get('distance',0)*1000:.0f} m")
    s4.metric("🏙️ Area", area_name)

    for row_start in range(0, len(visible), 5):
        row = visible[row_start:row_start + 5]
        cols = st.columns(5, gap="small")
        for col, item in zip(cols, row):
            with col:
                d = float(item.get("distance", 0))
                dist = f"{d*1000:.0f} m" if d < 1 else f"{d:.1f} km"
                with st.container(border=True):
                    st.markdown(f"<div style='font-size:23px'>{html.escape(str(item.get('icon','📍')))}</div>", unsafe_allow_html=True)
                    st.markdown(f"**{html.escape(str(item.get('name','Nearby Place')))}**")
                    st.caption(str(item.get("category", "Facility")))
                    st.markdown(f"<div style='font-weight:800;color:#c28f37'>📍 {dist}</div>", unsafe_allow_html=True)




def render_area_price_trend(dataset, city, area_name, current_price=None):
    """Show area pricing profile; use historical year data when available,
    otherwise clearly label a modelled trend."""
    if dataset is None or dataset.empty or "Price" not in dataset.columns:
        st.info("No dataset is available for area price trend.")
        return

    chart_font_color = "#f8fafc" if st.session_state.get("dark_mode", False) else "#0f172a"
    grid_color = (
        "rgba(255,255,255,0.1)"
        if st.session_state.get("dark_mode", False)
        else "rgba(0,0,0,0.1)"
    )

    area_df = dataset.copy()
    if "Area_Name" in area_df.columns:
        area_df = area_df[
            area_df["Area_Name"].astype(str).str.strip().str.lower()
            == str(area_name).strip().lower()
        ].copy()

    if area_df.empty and "City" in dataset.columns:
        area_df = dataset[
            dataset["City"].astype(str).str.strip().str.lower()
            == str(city).strip().lower()
        ].copy()

    if area_df.empty:
        st.info(f"No price data found for {area_name}.")
        return

    # Use a real time-series field only when the dataset actually contains one.
    year_col = next(
        (
            c for c in area_df.columns
            if str(c).lower() in {
                "year", "listing_year", "sale_year", "date", "listed_date"
            }
        ),
        None,
    )

    if year_col is not None and str(year_col).lower() not in {"date", "listed_date"}:
        try:
            trend = (
                area_df.groupby(year_col)["Price"]
                .mean()
                .reset_index()
                .sort_values(year_col)
            )
            if len(trend) >= 2:
                fig = px.line(
                    trend,
                    x=year_col,
                    y="Price",
                    markers=True,
                    title=f"{area_name} Price Trend",
                )
                fig.update_traces(
                    line=dict(color="#daa752", width=3),
                    marker=dict(size=8),
                )
                fig.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=chart_font_color),
                    yaxis=dict(
                        gridcolor=grid_color,
                        title="Average Price (Lakhs)",
                    ),
                    xaxis=dict(showgrid=False),
                    margin=dict(t=45, b=20, l=0, r=0),
                )
                st.plotly_chart(
                    fig,
                    use_container_width=True,
                    config={"displayModeBar": False},
                )
                st.caption("Historical trend from the dataset.")
                return
        except Exception:
            pass

    if "BHK" not in area_df.columns:
        st.info("BHK-wise price data is not available for this area.")
        return

    profile = (
        area_df.assign(
            BHK=pd.to_numeric(area_df["BHK"], errors="coerce"),
            Price=pd.to_numeric(area_df["Price"], errors="coerce"),
        )
        .dropna(subset=["BHK", "Price"])
        .groupby("BHK")["Price"]
        .mean()
        .reset_index()
        .sort_values("BHK")
    )

    if profile.empty:
        st.info("No usable price records were found for this area.")
        return

    try:
        base = float(
            current_price
            if current_price is not None
            else profile["Price"].mean()
        )
    except Exception:
        base = float(profile["Price"].mean())

    base = max(base, 0.0)
    start_year = datetime.now().year
    years = list(range(start_year, start_year + 6))
    modelled = [base * (1.065 ** i) for i in range(6)]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=[f"{int(x)} BHK" for x in profile["BHK"]],
            y=profile["Price"],
            name="Current area profile",
            marker_color="#9A6A46",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=[str(y) for y in years],
            y=modelled,
            mode="lines+markers",
            name="5-year modelled trend",
            line=dict(color="#daa752", width=3),
            marker=dict(size=8),
        )
    )
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=chart_font_color),
        yaxis=dict(gridcolor=grid_color, title="Price (Lakhs)"),
        xaxis=dict(showgrid=False),
        margin=dict(t=25, b=20, l=0, r=0),
        legend=dict(orientation="h", y=1.08),
    )
    st.plotly_chart(
        fig,
        use_container_width=True,
        config={"displayModeBar": False},
    )
    st.caption(
        "The dataset has no time-series field, so the line is a modelled trend "
        "using a 6.5% annual growth assumption; it is not historical data."
    )


def build_property_pdf_report(city, area_name, bhk, bathrooms, area, price_lakhs, score, financial, amenities, facilities, trend_note="", customer=None):
    """Build a polished warm-residential PDF with customer identity details."""
    if not HAS_FPDF:
        return None
    customer = customer or {}
    customer_name = str(customer.get("name", "Guest"))
    customer_email = str(customer.get("email", "Not provided"))
    customer_phone = str(customer.get("phone", "Not provided"))

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    C_BROWN = (91,64,40)
    C_BROWN_DARK = (63,47,35)
    C_BEIGE = (244,231,210)
    C_GRAY = (112,98,84)

    pdf.set_fill_color(*C_BROWN_DARK)
    pdf.rect(0,0,210,42,'F')
    pdf.set_text_color(255,255,255)
    pdf.set_xy(14,10)
    pdf.set_font("Arial","B",20)
    pdf.cell(182,9,txt="SMARTESTATE",ln=True,align="L")
    pdf.set_font("Arial","",9)
    pdf.set_text_color(*C_BEIGE)
    pdf.cell(182,6,txt="PROPERTY VALUATION • HOME BUYING REPORT",ln=True,align="L")

    pdf.set_y(52)
    pdf.set_text_color(*C_BROWN_DARK)
    pdf.set_font("Arial","B",19)
    pdf.cell(0,9,txt=f"{area_name}, {city}",ln=True)
    pdf.set_font("Arial","",10)
    pdf.set_text_color(*C_GRAY)
    pdf.cell(0,6,txt=f"{bhk} BHK | {bathrooms} Bathrooms | {area} sq.ft",ln=True)
    pdf.cell(0,6,txt=f"Report Date: {datetime.now().strftime('%d %B %Y')}",ln=True)

    def section(title):
        pdf.ln(4)
        pdf.set_fill_color(*C_BROWN)
        pdf.set_text_color(255,255,255)
        pdf.set_font("Arial","B",11)
        pdf.cell(0,8,txt="  "+title,ln=True,fill=True)
        pdf.ln(2)
        pdf.set_text_color(*C_BROWN_DARK)
    def row(label,value):
        pdf.set_font("Arial","",9)
        pdf.set_text_color(*C_GRAY)
        pdf.cell(58,6,txt=label,ln=0)
        pdf.set_font("Arial","B",9)
        pdf.set_text_color(*C_BROWN_DARK)
        pdf.multi_cell(0,6,txt=str(value))

    section("CUSTOMER DETAILS")
    row("Name:", customer_name)
    row("Mobile:", customer_phone)
    row("Gmail:", customer_email)

    section("PROPERTY DETAILS")
    row("Location:",f"{area_name}, {city}")
    row("Configuration:",f"{bhk} BHK | {bathrooms} Bathrooms | {area} sq.ft")
    row("Estimated Value:",f"Rs. {price_lakhs:,.2f} Lakhs")
    row("Property Score:",f"{score.get('overall',0):.1f} / 100")
    row("Score Breakdown:",f"Safety {score.get('safety',0):.1f}, Value {score.get('value',0):.1f}, Space {score.get('space',0):.1f}, Amenities {score.get('amenities',0):.1f}, Confidence {score.get('confidence',0):.1f}")
    row("Amenities:",", ".join(amenities) if amenities else "None selected")

    pdf.ln(2)
    pdf.set_fill_color(*C_BEIGE)
    pdf.set_text_color(*C_BROWN_DARK)
    pdf.set_font("Arial","B",14)
    pdf.cell(0,12,txt=f"Estimated Market Value: Rs. {price_lakhs:,.2f} Lakhs",ln=True,fill=True,align="C")

    section("EMI & TOTAL PURCHASE COST")
    row("Property Cost:",f"Rs. {financial.get('property_value', financial.get('property_price', 0)):,.0f}")
    row("Down Payment:",f"Rs. {financial['downpayment']:,.0f}")
    row("Loan Amount:",f"Rs. {financial['loan_amount']:,.0f}")
    row("Stamp Duty Estimate:",f"Rs. {financial['stamp_duty']:,.0f}")
    row("Registration Estimate:",f"Rs. {financial['registration']:,.0f}")
    row("Other Charges:",f"Rs. {financial['other_charges']:,.0f}")
    row("Monthly EMI:",f"Rs. {financial['emi']:,.0f}")
    row("Total Interest:",f"Rs. {financial['total_interest']:,.0f}")
    row("Upfront Cash Needed:",f"Rs. {financial['upfront_cash']:,.0f}")
    row("Total Acquisition Cost:",f"Rs. {financial['total_acquisition']:,.0f}")

    # SMART FURNITURE & FURNISHING
    # Pull the currently selected furniture items from the same cart used by
    # the Smart Furniture Cost Estimator, so the downloaded report matches
    # what the user selected in View Details.
    try:
        furniture_rows, furniture_subtotal, furniture_installation, furniture_total, furniture_count = get_furniture_pdf_details()
    except Exception:
        furniture_rows, furniture_subtotal, furniture_installation, furniture_total, furniture_count = [], 0.0, 0.0, 0.0, 0

    section("SMART FURNITURE & FURNISHING")
    if furniture_rows:
        for category, name, icon, unit_price, qty, line_total in furniture_rows:
            row(f"{category}:", f"{name} | Qty {qty} | Rs. {unit_price:,.0f} each | Rs. {line_total:,.0f}")
        row("Furniture Items:", f"{furniture_count} items")
        row("Furniture Subtotal:", f"Rs. {furniture_subtotal:,.0f}")
        row("Installation Estimate:", f"Rs. {furniture_installation:,.0f} (8%)")
        row("Total Furnishing Cost:", f"Rs. {furniture_total:,.0f}")
        row("Property + Furnishing:", f"Rs. {float(financial.get('total_acquisition', 0)) + furniture_total:,.0f}")
    else:
        row("Selected Furniture:", "No furniture items selected")
        row("Furnishing Cost:", "Rs. 0")

    section("NEIGHBORHOOD HIGHLIGHTS")
    for item in facilities[:12]:
        d=float(item.get('distance',0)); dist=f"{d*1000:.0f} m" if d < 1 else f"{d:.1f} km"
        row(f"{item.get('category','Facility')}:",f"{item.get('name','Nearby Place')} - {dist}")

    section("AREA PRICE TREND")
    row("Trend Note:", trend_note or "See the interactive chart inside the app.")
    row("Report Prepared For:", customer_name)

    pdf.ln(6)
    pdf.set_text_color(*C_GRAY)
    pdf.set_font("Arial","I",8)
    pdf.multi_cell(0,4,txt="Disclaimer: Financial charges are user-configurable estimates. Property values and forecasts are model outputs and may differ from actual market prices or lender terms.",align="C")
    pdf.set_y(-14)
    pdf.set_text_color(*C_GRAY)
    pdf.set_font("Arial","",8)
    pdf.cell(0,5,txt="SmartEstate • Property Intelligence Platform",align="C")
    sanitize_pdf_for_fpdf(pdf)
    output = pdf.output(dest="S")
    return output.encode("latin-1") if isinstance(output,str) else bytes(output)


def build_compare_pdf_report(city_a, area_a, bhk_a, bath_a, sqft_a, val_a, score_a, financial_a,
                             city_b, area_b, bhk_b, bath_b, sqft_b, val_b, score_b, financial_b,
                             customer=None, investment_a=None, investment_b=None, match_a=None, match_b=None):
    """Build a clean side-by-side comparison PDF matching the requested reference layout."""
    if not HAS_FPDF:
        return None
    customer = customer or {}
    customer_name = str(customer.get("name", "Guest"))
    customer_email = str(customer.get("email", "Not provided"))
    customer_phone = str(customer.get("phone", "Not provided"))
    investment_a = investment_a or {}
    investment_b = investment_b or {}

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    BROWN=(91,64,40); BROWN_DARK=(68,48,34); BEIGE=(244,231,210); TEXT=(72,62,54); WHITE=(255,255,255)

    def safe(v): return str(v).replace("₹", "Rs.").replace("–", "-").replace("—", "-")
    def num(v):
        try: return float(v)
        except Exception: return 0.0
    def money(v): return f"Rs.{num(v):,.0f}"
    def lakhs(v): return f"Rs.{num(v):.2f} L"
    def pct(v,d=2): return f"{num(v):.{d}f}%"
    def getv(d,k,default=0):
        try: return d.get(k,default)
        except Exception: return default

    def section_bar(title):
        pdf.set_fill_color(*BROWN); pdf.set_text_color(*WHITE); pdf.set_font("Arial","B",11)
        pdf.cell(0,9,safe(title),border=0,ln=True,fill=True); pdf.ln(2)
    def table_header():
        pdf.set_fill_color(*BEIGE); pdf.set_text_color(*BROWN_DARK); pdf.set_font("Arial","B",8.5)
        pdf.cell(55,9,"METRIC",border=1,fill=True,align="C")
        pdf.cell(67.5,9,"PROPERTY A",border=1,fill=True,align="C")
        pdf.cell(67.5,9,"PROPERTY B",border=1,fill=True,align="C"); pdf.ln(9)
    def row(label,a,b,bold=True):
        pdf.set_text_color(*TEXT); pdf.set_font("Arial","",8.3); pdf.cell(55,7.7,safe(label),border=1)
        pdf.set_font("Arial","B" if bold else "",8.3)
        pdf.cell(67.5,7.7,safe(a),border=1,align="C"); pdf.cell(67.5,7.7,safe(b),border=1,align="C"); pdf.ln(7.7)

    pdf.add_page()
    section_bar("DETAILED COMPARISON")
    table_header()
    ca=financial_a or {}; cb=financial_b or {}
    rows=[
      ("Estimated Property Price",lakhs(val_a),lakhs(val_b)),
      ("Area",f"{num(sqft_a):,.0f} sq.ft",f"{num(sqft_b):,.0f} sq.ft"),
      ("BHK",str(bhk_a),str(bhk_b)),
      ("Bathrooms",str(bath_a),str(bath_b)),
      ("Safety Profile",f"{num(getv(score_a,'safety'))/10:.1f}/10",f"{num(getv(score_b,'safety'))/10:.1f}/10"),
      ("Smart Match",pct(match_a if match_a is not None else getv(score_a,'match'),1),pct(match_b if match_b is not None else getv(score_b,'match'),1)),
      ("Rental Yield",pct(getv(investment_a,'rental_yield'),2),pct(getv(investment_b,'rental_yield'),2)),
      ("Down Payment",money(getv(ca,'downpayment')),money(getv(cb,'downpayment'))),
      ("Loan Amount",money(getv(ca,'loan_amount')),money(getv(cb,'loan_amount'))),
      ("Monthly EMI",money(getv(ca,'emi')),money(getv(cb,'emi'))),
      ("Upfront Cash",money(getv(ca,'upfront_cash')),money(getv(cb,'upfront_cash'))),
      ("Total Interest",money(getv(ca,'total_interest')),money(getv(cb,'total_interest'))),
      ("Total Acquisition Cost",money(getv(ca,'total_acquisition')),money(getv(cb,'total_acquisition'))),
      ("5-Year Value",money(getv(investment_a,'future_5')),money(getv(investment_b,'future_5'))),
      ("10-Year Value",money(getv(investment_a,'future_10')),money(getv(investment_b,'future_10'))),
      ("10-Year Rental Income",money(getv(investment_a,'cumulative_rent')),money(getv(investment_b,'cumulative_rent'))),
      ("Modelled 10-Year ROI",pct(getv(investment_a,'roi'),1),pct(getv(investment_b,'roi'),1)),
    ]
    for r in rows: row(*r)

    pdf.ln(4); section_bar("INVESTMENT ASSUMPTIONS")
    assumptions=[
      ("Expected Monthly Rent",money(getv(investment_a,'monthly_rent',getv(investment_a,'rent'))),money(getv(investment_b,'monthly_rent',getv(investment_b,'rent')))),
      ("Annual Appreciation",pct(getv(investment_a,'appreciation'),1),pct(getv(investment_b,'appreciation'),1)),
      ("Annual Rent Growth",pct(getv(investment_a,'rent_growth'),1),pct(getv(investment_b,'rent_growth'),1)),
    ]
    for label,a,b in assumptions:
        pdf.set_text_color(*TEXT); pdf.set_font("Arial","",8.5); pdf.cell(55,7.5,safe(label),border=0)
        pdf.set_font("Arial","B",8.5); pdf.cell(67.5,7.5,safe(a),border=0,align="C"); pdf.cell(67.5,7.5,safe(b),border=0,align="C"); pdf.ln(7.5)

    pdf.ln(3); section_bar("PROPERTY DETAILS")
    row("Property Location",f"{area_a}, {city_a}",f"{area_b}, {city_b}",False)
    row("Configuration",f"{bhk_a} BHK / {bath_a} Bath",f"{bhk_b} BHK / {bath_b} Bath",False)

    pdf.ln(4); section_bar("CUSTOMER DETAILS")
    for label,value in [("Name",customer_name),("Mobile",customer_phone),("Gmail",customer_email)]:
        pdf.set_text_color(*TEXT); pdf.set_font("Arial","",8.5); pdf.cell(35,6.5,safe(label),border=0)
        pdf.set_font("Arial","B",8.5); pdf.cell(0,6.5,safe(value),border=0,ln=True)

    pdf.ln(4); pdf.set_text_color(105,92,80); pdf.set_font("Arial","I",7.5)
    pdf.multi_cell(0,4,safe("Investment projections are modelled estimates based on the selected assumptions. They are not guaranteed returns. Property values, lender terms, taxes and transaction costs may vary."),align="C")
    pdf.set_y(-10); pdf.set_text_color(120,105,90); pdf.set_font("Arial","",7); pdf.cell(0,5,"SmartEstate - Property Intelligence Platform",align="C")
    sanitize_pdf_for_fpdf(pdf)
    output = pdf.output(dest="S")
    # PyFPDF returns a latin-1 string while newer fpdf2 versions may return bytes.
    # Always hand Streamlit a real PDF byte payload so the combined download
    # cannot appear blank/corrupt.
    return output.encode("latin-1") if isinstance(output, str) else bytes(output)
# ==========================================
# 🎯 PRICE CONFIDENCE SCORE
# ==========================================
def get_price_confidence(dataset, area_name):
    """Rough confidence heuristic based on how consistent historical
    per-sq.ft rates are for that locality in the dataset."""
    if dataset is None or dataset.empty or "Area_Name" not in dataset.columns:
        return 82, "Moderate"
    local = dataset[dataset["Area_Name"] == area_name]
    if len(local) < 3:
        return 78, "Moderate (limited local data)"
    rate = (local["Price"] * 100000) / local["Area"]
    cov = rate.std() / rate.mean() if rate.mean() else 0.3
    score = max(60, min(97, int(97 - (cov * 100))))
    label = "High" if score >= 88 else ("Moderate" if score >= 75 else "Low")
    return score, label

def _norm_text(value):
    return str(value).strip().lower().replace("-", " ").replace("_", " ")


def _find_area_mentions(query, dataset):
    """Find locality names mentioned in the user's natural-language question."""
    q = _norm_text(query)
    if dataset is None or dataset.empty or "Area_Name" not in dataset.columns:
        return []
    areas = sorted({str(x) for x in dataset["Area_Name"].dropna().unique()}, key=len, reverse=True)
    found = []
    for area in areas:
        if _norm_text(area) in q:
            found.append(area)
    return found


def _extract_bhk(query):
    import re
    q = _norm_text(query)
    m = re.search(r"(?:bhk|bedroom|bedrooms)\s*(?:is|=|:)?\s*([1-6])\b", q)
    if not m:
        m = re.search(r"\b([1-6])\s*(?:bhk|bedroom|bedrooms)\b", q)
    return int(m.group(1)) if m else None


@st.cache_data(ttl=3600, show_spinner=False)
@st.cache_data(ttl=3600, show_spinner=False)
def _dataset_summary_for_ai(dataset):
    """Compact, factual dataset context for the LLM; cached for fast repeated AI questions."""
    if dataset is None or dataset.empty:
        return "Dataset is empty. Do not invent property statistics."

    parts = []
    parts.append(f"Rows: {len(dataset)}")
    if "City" in dataset.columns:
        parts.append("Cities: " + ", ".join(map(str, sorted(dataset["City"].dropna().unique()))))
    if "Area_Name" in dataset.columns:
        parts.append("Areas: " + ", ".join(map(str, sorted(dataset["Area_Name"].dropna().unique()))))
    if "BHK" in dataset.columns:
        parts.append("BHK values: " + ", ".join(map(str, sorted(dataset["BHK"].dropna().unique()))))
    if "Price" in dataset.columns:
        parts.append(f"Overall price range: {dataset['Price'].min():.2f} to {dataset['Price'].max():.2f} Lakhs")

    # Area-level statistics, useful for cheap/expensive and locality questions.
    if all(c in dataset.columns for c in ["Area_Name", "Price"]):
        g = dataset.groupby("Area_Name")["Price"].agg(["count", "mean", "min", "max"]).reset_index()
        g = g.sort_values("mean")
        parts.append("Area average-price table:\n" + g.to_string(index=False, formatters={
            "mean": "{:.2f}".format, "min": "{:.2f}".format, "max": "{:.2f}".format
        }))

    # Area + BHK statistics, so the assistant can answer questions like
    # '3 BHK in Bodakdev vs Bopal'.
    if all(c in dataset.columns for c in ["Area_Name", "BHK", "Price"]):
        gb = dataset.groupby(["Area_Name", "BHK"])["Price"].agg(["count", "mean", "min", "max"]).reset_index()
        parts.append("Area+BHK price table:\n" + gb.to_string(index=False, formatters={
            "mean": "{:.2f}".format, "min": "{:.2f}".format, "max": "{:.2f}".format
        }))

    if all(c in dataset.columns for c in ["Area_Name", "BHK", "Area"]):
        ga = dataset.groupby(["Area_Name", "BHK"])["Area"].mean().reset_index(name="avg_sqft")
        parts.append("Area+BHK average size table:\n" + ga.to_string(index=False, formatters={"avg_sqft": "{:.0f}".format}))

    return "\n\n".join(parts)


def get_rule_based_response(query, dataset):
    """Dataset-grounded fallback assistant. It answers common natural-language
    property questions without an external API and never fabricates missing stats."""
    q = _norm_text(query)
    areas_mentioned = _find_area_mentions(query, dataset)
    bhk = _extract_bhk(query)

    if q in ["hi", "hello", "hey", "hi ai", "hello ai", "namaste", "नमस्ते"]:
        return "Hello! 👋 Main aapke project ke property data ke basis par area, BHK, price, size, budget aur investment ke questions answer kar sakta hoon."

    if "who" in q and ("made" in q or "created" in q or "developer" in q or "built" in q):
        return "I am the Smart Real Estate AI Assistant, engineered and developed by **Darji Shailesh**. 👨‍💻"

    if dataset is None or dataset.empty:
        return "Mujhe abhi project ka property dataset nahi mil raha, isliye main reliable area/BHK price answer nahi de sakta."

    # Work on the requested BHK first, then requested area(s).
    work = dataset.copy()
    if bhk is not None and "BHK" in work.columns:
        work = work[work["BHK"] == bhk]

    # Cheapest / most expensive questions.
    cheap_words = ["cheap", "cheapest", "affordable", "lowest", "low price", "sasta", "sasti", "kam price", "budget", "sabse kam"]
    expensive_words = ["expensive", "costly", "highest", "premium", "posh", "luxury", "mehnga", "mehngi", "sabse mehnga", "sabse mahanga"]
    if any(w in q for w in cheap_words) or any(w in q for w in expensive_words):
        if "Price" not in work.columns or "Area_Name" not in work.columns or work.empty:
            return f"{bhk} BHK ke liye dataset me enough price data available nahi hai." if bhk else "Is question ke liye dataset me enough price data available nahi hai."
        g = work.groupby("Area_Name")["Price"].mean().sort_values()
        if any(w in q for w in cheap_words):
            area = g.index[0]
            return f"{area} is the most affordable area in the available dataset" + (f" for {bhk} BHK" if bhk else "") + f", with an average price of **{format_price(g.iloc[0])}**. 💰"
        area = g.index[-1]
        return f"{area} is the most expensive/premium area in the available dataset" + (f" for {bhk} BHK" if bhk else "") + f", with an average price of **{format_price(g.iloc[-1])}**. 💎"

    # Compare two explicitly mentioned areas.
    if len(areas_mentioned) >= 2 and "Price" in work.columns:
        rows = []
        for a in areas_mentioned[:5]:
            sub = work[work["Area_Name"].astype(str).str.lower() == a.lower()]
            if not sub.empty:
                rows.append((a, float(sub["Price"].mean()), len(sub)))
        if len(rows) >= 2:
            rows.sort(key=lambda x: x[1])
            detail = " | ".join(f"{a}: {format_price(v)} avg" for a, v, _ in rows)
            return f"Comparison" + (f" for {bhk} BHK" if bhk else "") + f": {detail}. **{rows[0][0]}** is cheaper and **{rows[-1][0]}** is more expensive in the project dataset."

    # Specific area questions: price, BHK, size, range, amenities, safety.
    if areas_mentioned:
        area = areas_mentioned[0]
        sub = work[work["Area_Name"].astype(str).str.lower() == area.lower()]
        if sub.empty:
            return f"{area} ke liye current dataset me matching records nahi mile."
        avg_price = sub["Price"].mean() if "Price" in sub.columns else None
        avg_area = sub["Area"].mean() if "Area" in sub.columns else None
        price_rate = ((sub["Price"] * 100000) / sub["Area"]).mean() if all(c in sub.columns for c in ["Price", "Area"]) else None
        if any(w in q for w in ["area", "size", "sqft", "square feet", "kitna bada"]):
            return f"**{area}**" + (f" ({bhk} BHK)" if bhk else "") + f" has an average property size of **{avg_area:,.0f} sq.ft** in the dataset."
        if any(w in q for w in ["price", "value", "cost", "rate", "kitne ka", "kitna price", "bhav"]):
            ans = f"**{area}**" + (f" ({bhk} BHK)" if bhk else "") + f" has an average price of **{format_price(avg_price)}**"
            if price_rate is not None:
                ans += f" and an average rate of about **₹{price_rate:,.0f}/sq.ft**."
            else:
                ans += "."
            return ans
        if any(w in q for w in ["range", "minimum", "maximum", "min", "max", "lowest", "highest"]):
            return f"For **{area}**" + (f" ({bhk} BHK)" if bhk else "") + f", the dataset price range is **{format_price(sub['Price'].min())} – {format_price(sub['Price'].max())}**."
        if any(w in q for w in ["safe", "security", "women"]):
            return f"The project's configured Safety Index for **{area}** is **{SAFETY_SCORES.get(area, 8.2)}/10**. This is a project-provided index, not a live crime statistic."

        # General area summary.
        bhks = sorted(sub["BHK"].dropna().unique().tolist()) if "BHK" in sub.columns else []
        return f"**{area}** has {len(sub)} matching records" + (f" for {bhk} BHK" if bhk else "") + (f", average price **{format_price(avg_price)}** and average size **{avg_area:,.0f} sq.ft**." if avg_price is not None and avg_area is not None else ".") + (f" Available BHKs: {', '.join(map(str, bhks))}." if bhks and bhk is None else "")

    # City-level and BHK-level questions.
    if bhk is not None and "Price" in work.columns and not work.empty:
        avg = work["Price"].mean()
        cheapest = work.groupby("Area_Name")["Price"].mean().idxmin()
        expensive = work.groupby("Area_Name")["Price"].mean().idxmax()
        return f"For **{bhk} BHK**, the dataset average price is **{format_price(avg)}**. Among areas with {bhk}-BHK data, **{cheapest}** is cheapest on average and **{expensive}** is most expensive."

    if any(w in q for w in ["area", "locality", "location", "which area", "best area"]):
        g = dataset.groupby("Area_Name")["Price"].mean().sort_values()
        return f"The dataset covers **{len(g)} areas**. Average-price-wise, **{g.index[0]}** is the most affordable and **{g.index[-1]}** is the most expensive. You can ask me for a specific BHK, e.g. '3 BHK me kaunsa area sasta hai?'"

    return "Main project dataset se area, BHK, price, sq.ft, price/sq.ft, cheapest, expensive, comparisons, safety aur budget-related questions answer kar sakta hoon. Example: **'Ahmedabad me 3 BHK ke liye sasta area kaunsa hai?'**"

def get_smart_ai_response(query, dataset):
    """Google Gemini-powered general AI assistant with grounded property context."""
    query = str(query or "").strip()
    if not query:
        return "Please type a question."

    # Fast local path: common greetings and project-data questions do not need a network round-trip.
    quick_text = get_rule_based_response(query, dataset)
    quick_q = _norm_text(query)
    property_markers = [
        "price", "price/sq.ft", "sqft", "sq ft", "square feet", "bhk", "area",
        "locality", "location", "cheap", "cheapest", "affordable", "expensive",
        "costly", "budget", "safety", "safe", "security", "rate", "value",
        "kitne ka", "kitna price", "bhav", "sasta", "sasti", "mehnga", "mehngi",
        "kaunsa area", "which area", "compare", "comparison"
    ]
    simple_greeting = quick_q in {"hi", "hello", "hey", "hi ai", "hello ai", "namaste", "नमस्ते"}
    simple_social = {
        "thanks": "You're welcome! 😊",
        "thank you": "You're welcome! 😊",
        "thx": "You're welcome! 😊",
        "bye": "Goodbye! 🏠 Have a great day.",
        "good morning": "Good morning! ☀️ How can I help with your home search today?",
        "good evening": "Good evening! 🌇 How can I help with your property search?",
    }
    if quick_q in simple_social:
        st.session_state["ai_last_error"] = ""
        return simple_social[quick_q]
    looks_like_property_question = simple_greeting or any(marker in quick_q for marker in property_markers)
    if looks_like_property_question and quick_text and not quick_text.startswith("Main project dataset se"):
        st.session_state["ai_last_error"] = ""
        return quick_text

    ok, status = get_gemini_status()
    if not ok:
        st.session_state["ai_last_error"] = status
        return (
            "⚠️ **Google Gemini AI is not configured.**\n\n"
            f"{status}\n\n"
            "Set `GEMINI_API_KEY` and restart Streamlit. No `.streamlit` folder is required."
        )

    try:
        data_context = _dataset_summary_for_ai(dataset)
        if len(data_context) > 9000:
            data_context = data_context[:9000] + "\n[Project data context shortened for faster response.]"

        current_language = st.session_state.get("language", "English")
        last_context = ""
        if st.session_state.get("last_pred", 0) > 0:
            last_context = (
                f"Recent property valuation: {format_price(st.session_state.last_pred)} "
                f"at {st.session_state.get('last_loc', '')}."
            )

        history = st.session_state.get("chat_msgs", [])[-6:]
        messages = [{
            "role": "system",
            "content": f"""
You are the Smart Real Estate AI Assistant. Answer the user's actual question clearly and quickly.
You are a general-purpose AI, not a property-only bot.

LANGUAGE:
- Understand English, Hindi/Hinglish, and Gujarati.
- Reply in the language used by the user whenever possible.

GENERAL CAPABILITIES:
- Answer general knowledge, education, mathematics, programming, coding, debugging,
  writing, rewriting, translation, definitions, explanations, planning, and everyday questions.
- Explain clearly and concisely.
- For code requests, provide practical runnable code.
- Remember recent chat context and answer follow-up questions.
- Do not say you can only answer real-estate questions.

PROPERTY DATA RULES:
- When the question is about this project, use PROJECT DATA below for numeric facts.
- Never invent project prices, BHK counts, areas, dataset statistics, or safety values.
- Prices in the project dataset are in Lakhs unless explicitly stated otherwise.
- If the requested project fact is not in the data, say so clearly.
- Safety Index values are project-provided, not live crime statistics.

CURRENT INFORMATION:
- Do not claim live web access. For latest/current facts, explain that live verification may be needed.

APP LANGUAGE: {current_language}
{last_context}

PROJECT DATA:
{data_context}
"""
        }]

        for msg in history:
            role = "assistant" if msg.get("role") == "bot" else "user"
            content = str(msg.get("content", "")).strip()
            if content:
                messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": query})
        answer = call_gemini_ai(messages)
        st.session_state["ai_last_error"] = ""
        return answer

    except Exception as exc:
        error_text = str(exc)
        st.session_state["ai_last_error"] = error_text
        if "HTTP 503" in error_text or "temporarily unavailable" in error_text or "high demand" in error_text:
            tip = "Google Gemini is temporarily busy. The app already retries and switches models automatically. Please try the question again in a moment."
        elif "HTTP 429" in error_text:
            tip = "Gemini rate limit reached. The app already retries and switches models automatically. Please try again shortly."
        elif "HTTP 402" in error_text:
            tip = "Check your Google AI/API billing or available quota."
        else:
            tip = "Check your Google Gemini API key, API access, quota, and internet connection."

        return (
            "⚠️ **Google Gemini AI request failed.**\n\n"
            f"`{error_text}`\n\n"
            f"{tip}"
        )

# ==========================================
# PAGES & NAVIGATION SETUP
# ==========================================
PAGES = {
    "prediction": "🏠 AI Valuation",
    "budget": "🔄 Budget-First Search",
    "compare": "⚖️ Compare Properties",
    "analytics": "📊 Analytics Dashboard",
    "favorites": "❤️ My Favorites",
    "saved_searches": "🔖 Saved Searches",
    "account": "👤 My Home Account",
}

# ==========================================
# 🌐 MULTI-LANGUAGE (EN / HI / GU) — core UI strings
# ==========================================
TRANSLATIONS = {
    "English": {
        "app_title":"House Price Prediction", "find_home_eyebrow":"Find your perfect home", "ai_powered":"AI Powered", "property_intelligence":"Property Intelligence", "hero_sub":"Get Smart Valuations, Neighborhood Details, and Auto-Generated PDF Reports instantly.", "market_analytics":"Market Analytics", "live_data":"Live Data", "insights":"Insights", "app_sub":"Intelligent Real Estate System",
        "nav_prediction":"🏠 AI Valuation", "nav_budget":"🔄 Budget-First Search", "nav_compare":"⚖️ Compare Properties", "nav_analytics":"📊 Analytics Dashboard", "nav_favorites":"❤️ My Favorites", "nav_saved_searches":"🔖 Saved Searches", "nav_account":"👤 My Home Account",
        "dark_toggle":"🌙 Toggle Dark/Light Mode", "ask_ai":"💬 Ask AI Assistant", "recently_viewed":"🕐 Recently Viewed",
        "save_fav":"❤️ Save to Favorites", "saved_fav":"✅ Saved to Favorites", "remove_fav":"🗑️ Remove",
        "no_fav":"You haven't saved any properties yet. Explore and tap ❤️ to save one!", "share_valuation":"📤 Share This Valuation", "share_whatsapp":"📱 Share on WhatsApp", "share_email":"📧 Share via Email",
        "currency_mode":"💱 Currency Display", "language":"🌐 Language", "similar_props":"🏘️ Similar Properties You May Like", "confidence":"🎯 Prediction Confidence",
        "navigation":"Navigation", "send":"Send", "type_here":"Type here...", "thinking":"Thinking...",
        "select_city":"Select City", "select_area":"Select Area / Locality", "property_area":"Property Area (sq.ft.)", "bathrooms":"Bathrooms",
        "gym":"🏋️ Gym", "pool":"🏊 Pool", "cinema":"🎬 Cinema", "gaming":"🎮 Gaming", "solar":"☀️ Solar Balcony",
        "generate_valuation":"📊 Generate Valuation & Insights →", "modify_details":"⬅️ Modify Details",
        "valuation_discount":"💰 Valuation & Discount", "neighborhood":"📍 Neighborhood", "lead_inquiry":"📧 Lead Gen & Inquiry", "future_roi":"📈 Future ROI & PDF", "three_d_map":"📐 3D Map",
        "apply_discount":"🎁 Click to Apply {max_disc}% AI Discount", "full_name":"Full Name *", "email":"Email Address *", "phone":"Phone Number",
        "downpayment":"Downpayment (%)", "loan_tenure":"Loan Tenure (Yrs)", "interest_rate":"Interest Rate (%)", "property_design":"🏛️ Property Design", "view":"👁️ View",
        "search_method":"Search Method", "total_budget":"Total Budget", "monthly_emi":"Monthly EMI", "min_budget":"Minimum Budget (₹ Lakhs)", "max_budget":"Maximum Budget (₹ Lakhs)",
        "min_emi":"Minimum EMI (₹)", "max_emi":"Maximum EMI (₹)", "preferred_city":"Preferred City", "preferred_locality":"Preferred Locality", "required_bhk":"Required BHK", "minimum_bathrooms":"Minimum Bathrooms", "minimum_area":"Minimum Area (sq.ft.)",
        "find_homes":"🔍 Find Homes", "modify_search":"⬅️ Modify Search", "view_details":"View Details ➔", "back_search":"⬅️ Back to Search Results",
        "deep_compare":"⚖️ Run Deep AI Comparison", "back_homes":"⬅️ Back to Homes Results", "modify_comparison":"⬅️ Modify Comparison",
        "city_a":"City A", "locality_a":"Locality A", "bhk_a":"BHK A", "area_a":"Area A (sq.ft)", "city_b":"City B", "locality_b":"Locality B", "bhk_b":"BHK B", "area_b":"Area B (sq.ft)",
        "analytics_city":"🌍 1. Select City", "analytics_areas":"📍 2. Select Areas", "analytics_bhk":"🛏️ 3. Select BHK", "generate_analytics":"📊 Generate Analytics Dashboard", "modify_filters":"⬅️ Modify Filters",
        "revalue":"🔍 Re-Value", "nearby_title":"Nearby Facilities", "nearby_sub":"Top {count} nearest real places around your selected area.", "chat_welcome":"Hello! I am your Smart Real Estate AI 🤖. Ask me about property prices, investments, or safety.", "places":"places", "all_cities":"All Cities", "all_localities":"All Localities", "any":"Any",
        "nearby_eyebrow":"NEIGHBORHOOD HIGHLIGHTS", "nearby_category":"Nearby Facilities", "no_live_pois":"No live named facilities were returned by OpenStreetMap right now. Please try again in a moment.",
        "nearby_caption":"Real nearby facilities around the selected area — names, categories and distance.",
        "ai_help":"🤖 Ask about any project area, BHK, price, size, cheapest/expensive locality, comparison, budget, ROI or safety. Ask AI also handles general questions, coding, maths, education, translation and explanations using Google Gemini.",
        "saved_properties":"properties saved to favorites", "automated_email":"📧 Automated Email Sent!", "email_sent_suffix":"A detailed property brochure for {area} has been successfully dispatched to {email}. Our team will contact you soon.", "fill_name_email":"⚠️ Please fill in Name and Email.",
        "dataset_required":"⚠️ Dataset required for Budget Search.", "affordability":"💡 Affordability Estimate:", "configs_found":"🎉 AI found {count} premium configurations matching your strict criteria!",
        "max_compare":"You can compare a maximum of 2 properties at a time. Remove one first.", "no_matching":"No properties found matching all these filters. Try relaxing your budget or amenities.",
        "reference_style":"Reference-style presentation — floor plan, labeled spaces and realistic interior views.", "model_missing":"Model not found!", "no_dataset_analytics":"⚠️ No dataset found to display analytics.", "not_enough_amenity":"Not enough diverse data to calculate amenity premiums.",
        "properties_analyzed":"Properties Analyzed", "average_price":"Average Price", "peak_valuation":"Peak Valuation", "avg_sqft":"Avg Price / Sq.Ft", "wealth_forecast":"🔮 10-Year Wealth Forecast (Market Trend)",
        "amenity_impact":"💎 Amenity Premium Impact", "top_markets":"📊 Top 5 Premium Markets", "area_price":"📈 Area vs Price Distribution (Market Spread)",
        "your_shortlist":"Your Shortlist", "favorites_title":"My Favorites", "favorites_sub":"All the properties you've saved for later, in one place.",
    },
    "Hindi": {
        "app_title":"हाउस प्राइस प्रेडिक्शन", "find_home_eyebrow":"अपना सही घर खोजें", "ai_powered":"एआई संचालित", "property_intelligence":"प्रॉपर्टी इंटेलिजेंस", "hero_sub":"स्मार्ट वैल्यूएशन, आसपास की जानकारी और ऑटो-जनरेटेड PDF रिपोर्ट तुरंत पाएँ।", "market_analytics":"मार्केट एनालिटिक्स", "live_data":"लाइव डेटा", "insights":"इनसाइट्स", "app_sub":"स्मार्ट रियल एस्टेट सिस्टम",
        "nav_prediction":"🏠 एआई वैल्यूएशन", "nav_budget":"🔄 बजट-फर्स्ट सर्च", "nav_compare":"⚖️ प्रॉपर्टी तुलना", "nav_analytics":"📊 एनालिटिक्स डैशबोर्ड", "nav_favorites":"❤️ मेरे पसंदीदा", "nav_saved_searches":"🔖 सेव्ड सर्च", "nav_account":"👤 मेरा होम अकाउंट",
        "dark_toggle":"🌙 डार्क/लाइट मोड बदलें", "ask_ai":"💬 एआई असिस्टेंट से पूछें", "recently_viewed":"🕐 हाल ही में देखे गए", "save_fav":"❤️ पसंदीदा में सेव करें", "saved_fav":"✅ पसंदीदा में सेव हो गया", "remove_fav":"🗑️ हटाएं",
        "no_fav":"आपने अभी तक कोई प्रॉपर्टी सेव नहीं की है। ❤️ पर टैप करके सेव करें!", "share_valuation":"📤 यह वैल्यूएशन शेयर करें", "share_whatsapp":"📱 व्हाट्सएप पर शेयर करें", "share_email":"📧 ईमेल से शेयर करें", "currency_mode":"💱 मुद्रा प्रदर्शन", "language":"🌐 भाषा", "similar_props":"🏘️ मिलती-जुलती प्रॉपर्टीज़", "confidence":"🎯 प्रेडिक्शन कॉन्फिडेंस",
        "navigation":"नेविगेशन", "send":"भेजें", "type_here":"यहाँ लिखें...", "thinking":"सोच रहा हूँ...", "select_city":"शहर चुनें", "select_area":"क्षेत्र / लोकैलिटी चुनें", "property_area":"प्रॉपर्टी क्षेत्र (वर्ग फुट)", "bathrooms":"बाथरूम", "gym":"🏋️ जिम", "pool":"🏊 पूल", "cinema":"🎬 सिनेमा", "gaming":"🎮 गेमिंग", "solar":"☀️ सोलर बालकनी",
        "generate_valuation":"📊 वैल्यूएशन और इनसाइट्स बनाएं →", "modify_details":"⬅️ विवरण बदलें", "valuation_discount":"💰 वैल्यूएशन और डिस्काउंट", "neighborhood":"📍 आसपास का क्षेत्र", "lead_inquiry":"📧 संपर्क और पूछताछ", "future_roi":"📈 भविष्य ROI और PDF", "three_d_map":"📐 3D मैप", "apply_discount":"🎁 {max_disc}% एआई डिस्काउंट लागू करें", "full_name":"पूरा नाम *", "email":"ईमेल पता *", "phone":"फोन नंबर",
        "downpayment":"डाउन पेमेंट (%)", "loan_tenure":"लोन अवधि (वर्ष)", "interest_rate":"ब्याज दर (%)", "property_design":"🏛️ प्रॉपर्टी डिज़ाइन", "view":"👁️ दृश्य", "search_method":"सर्च तरीका", "total_budget":"कुल बजट", "monthly_emi":"मासिक EMI", "min_budget":"न्यूनतम बजट (₹ लाख)", "max_budget":"अधिकतम बजट (₹ लाख)", "min_emi":"न्यूनतम EMI (₹)", "max_emi":"अधिकतम EMI (₹)", "preferred_city":"पसंदीदा शहर", "preferred_locality":"पसंदीदा लोकैलिटी", "required_bhk":"आवश्यक BHK", "minimum_bathrooms":"न्यूनतम बाथरूम", "minimum_area":"न्यूनतम क्षेत्र (वर्ग फुट)",
        "find_homes":"🔍 घर खोजें", "modify_search":"⬅️ सर्च बदलें", "view_details":"विवरण देखें ➔", "back_search":"⬅️ सर्च परिणामों पर वापस जाएँ", "deep_compare":"⚖️ गहरी एआई तुलना चलाएँ", "back_homes":"⬅️ घरों के परिणामों पर वापस जाएँ", "modify_comparison":"⬅️ तुलना बदलें", "city_a":"शहर A", "locality_a":"लोकैलिटी A", "bhk_a":"BHK A", "area_a":"क्षेत्र A (वर्ग फुट)", "city_b":"शहर B", "locality_b":"लोकैलिटी B", "bhk_b":"BHK B", "area_b":"क्षेत्र B (वर्ग फुट)",
        "analytics_city":"🌍 1. शहर चुनें", "analytics_areas":"📍 2. क्षेत्र चुनें", "analytics_bhk":"🛏️ 3. BHK चुनें", "generate_analytics":"📊 एनालिटिक्स डैशबोर्ड बनाएं", "modify_filters":"⬅️ फ़िल्टर बदलें", "revalue":"🔍 फिर से वैल्यू करें", "nearby_title":"आसपास की सुविधाएँ", "nearby_sub":"आपके चुने हुए क्षेत्र के आसपास की {count} सबसे नज़दीकी वास्तविक जगहें।", "chat_welcome":"नमस्ते! मैं आपका स्मार्ट रियल एस्टेट एआई 🤖 हूँ। प्रॉपर्टी कीमत, निवेश या सुरक्षा के बारे में पूछें।", "places":"जगहें", "all_cities":"सभी शहर", "all_localities":"सभी लोकैलिटी", "any":"कोई भी", "nearby_eyebrow":"आसपास की प्रमुख सुविधाएँ", "nearby_category":"आसपास की सुविधाएँ", "no_live_pois":"OpenStreetMap से अभी कोई वास्तविक नाम वाली सुविधा नहीं मिली। कृपया थोड़ी देर बाद फिर कोशिश करें।", "nearby_caption":"चुने हुए क्षेत्र के आसपास की वास्तविक सुविधाएँ — नाम, श्रेणी और दूरी।",
        "ai_help":"🤖 क्षेत्र, BHK, कीमत, आकार, सस्ता/महंगा इलाका, तुलना, बजट, ROI या सुरक्षा के बारे में पूछें।", "saved_properties":"प्रॉपर्टीज़ पसंदीदा में सेव हैं", "automated_email":"📧 ईमेल सफलतापूर्वक भेजा गया!", "email_sent_suffix":"{area} की विस्तृत प्रॉपर्टी ब्रोशर {email} पर भेज दी गई है। हमारी टीम जल्द संपर्क करेगी।", "fill_name_email":"⚠️ कृपया नाम और ईमेल भरें।", "dataset_required":"⚠️ बजट सर्च के लिए डेटासेट आवश्यक है।", "affordability":"💡 आपकी वहन क्षमता का अनुमान:", "configs_found":"🎉 आपकी शर्तों से मेल खाने वाली {count} प्रीमियम कॉन्फ़िगरेशन मिलीं!", "max_compare":"आप एक समय में अधिकतम 2 प्रॉपर्टी की तुलना कर सकते हैं। पहले एक हटाएँ।", "no_matching":"इन सभी फ़िल्टर से मेल खाने वाली प्रॉपर्टी नहीं मिली। बजट या सुविधाएँ थोड़ी ढीली करें।", "reference_style":"रेफरेंस-स्टाइल प्रस्तुति — फ्लोर प्लान, लेबल किए गए स्थान और वास्तविक इंटीरियर दृश्य।", "model_missing":"मॉडल नहीं मिला!", "no_dataset_analytics":"⚠️ एनालिटिक्स दिखाने के लिए डेटासेट नहीं मिला।", "not_enough_amenity":"सुविधाओं के प्रीमियम की गणना के लिए पर्याप्त विविध डेटा नहीं है।", "properties_analyzed":"विश्लेषित प्रॉपर्टीज़", "average_price":"औसत कीमत", "peak_valuation":"सबसे अधिक वैल्यूएशन", "avg_sqft":"औसत कीमत / वर्ग फुट", "wealth_forecast":"🔮 10-वर्ष संपत्ति वृद्धि अनुमान", "amenity_impact":"💎 सुविधा से मिलने वाला अतिरिक्त मूल्य", "top_markets":"📊 शीर्ष 5 प्रीमियम मार्केट", "area_price":"📈 क्षेत्र बनाम कीमत वितरण", "your_shortlist":"आपकी शॉर्टलिस्ट", "favorites_title":"मेरे पसंदीदा", "favorites_sub":"बाद में देखने के लिए सेव की गई सभी प्रॉपर्टीज़ यहाँ हैं।"
    },
    "Gujarati": {
        "app_title":"હાઉસ પ્રાઈસ પ્રેડિક્શન", "find_home_eyebrow":"તમારું યોગ્ય ઘર શોધો", "ai_powered":"એઆઈ સંચાલિત", "property_intelligence":"પ્રોપર્ટી ઇન્ટેલિજન્સ", "hero_sub":"સ્માર્ટ વેલ્યુએશન, આસપાસની માહિતી અને ઓટો-જનરેટેડ PDF રિપોર્ટ તરત મેળવો.", "market_analytics":"માર્કેટ એનાલિટિક્સ", "live_data":"લાઇવ ડેટા", "insights":"ઇનસાઇટ્સ", "app_sub":"સ્માર્ટ રિયલ એસ્ટેટ સિસ્ટમ",
        "nav_prediction":"🏠 એઆઈ વેલ્યુએશન", "nav_budget":"🔄 બજેટ-ફર્સ્ટ સર્ચ", "nav_compare":"⚖️ પ્રોપર્ટી સરખામણી", "nav_analytics":"📊 એનાલિટિક્સ ડેશબોર્ડ", "nav_favorites":"❤️ મારા મનપસંદ", "nav_saved_searches":"🔖 સેવ્ડ સર્ચ", "nav_account":"👤 મારું હોમ અકાઉન્ટ",
        "dark_toggle":"🌙 ડાર્ક/લાઈટ મોડ બદલો", "ask_ai":"💬 એઆઈ સહાયકને પૂછો", "recently_viewed":"🕐 તાજેતરમાં જોયેલ", "save_fav":"❤️ મનપસંદમાં સેવ કરો", "saved_fav":"✅ મનપસંદમાં સેવ થયું", "remove_fav":"🗑️ કાઢી નાખો",
        "no_fav":"તમે હજુ સુધી કોઈ પ્રોપર્ટી સેવ કરી નથી. ❤️ પર ટેપ કરીને સેવ કરો!", "share_valuation":"📤 આ વેલ્યુએશન શેર કરો", "share_whatsapp":"📱 વોટ્સએપ પર શેર કરો", "share_email":"📧 ઈમેલથી શેર કરો", "currency_mode":"💱 કરન્સી ડિસ્પ્લે", "language":"🌐 ભાષા", "similar_props":"🏘️ મળતી આવતી પ્રોપર્ટીઝ", "confidence":"🎯 પ્રેડિક્શન કોન્ફિડન્સ",
        "navigation":"નેવિગેશન", "send":"મોકલો", "type_here":"અહીં લખો...", "thinking":"વિચાર કરી રહ્યો છું...", "select_city":"શહેર પસંદ કરો", "select_area":"વિસ્તાર / લોકેલિટી પસંદ કરો", "property_area":"પ્રોપર્ટી વિસ્તાર (ચોરસ ફૂટ)", "bathrooms":"બાથરૂમ", "gym":"🏋️ જિમ", "pool":"🏊 પૂલ", "cinema":"🎬 સિનેમા", "gaming":"🎮 ગેમિંગ", "solar":"☀️ સોલર બાલ્કની",
        "generate_valuation":"📊 વેલ્યુએશન અને ઇનસાઇટ્સ બનાવો →", "modify_details":"⬅️ વિગતો બદલો", "valuation_discount":"💰 વેલ્યુએશન અને ડિસ્કાઉન્ટ", "neighborhood":"📍 આસપાસનો વિસ્તાર", "lead_inquiry":"📧 સંપર્ક અને પૂછપરછ", "future_roi":"📈 ભવિષ્ય ROI અને PDF", "three_d_map":"📐 3D મેપ", "apply_discount":"🎁 {max_disc}% AI ડિસ્કાઉન્ટ લાગુ કરો", "full_name":"પૂરું નામ *", "email":"ઈમેલ સરનામું *", "phone":"ફોન નંબર",
        "downpayment":"ડાઉન પેમેન્ટ (%)", "loan_tenure":"લોન સમયગાળો (વર્ષ)", "interest_rate":"વ્યાજ દર (%)", "property_design":"🏛️ પ્રોપર્ટી ડિઝાઇન", "view":"👁️ દૃશ્ય", "search_method":"સર્ચ પદ્ધતિ", "total_budget":"કુલ બજેટ", "monthly_emi":"માસિક EMI", "min_budget":"ન્યૂનતમ બજેટ (₹ લાખ)", "max_budget":"મહત્તમ બજેટ (₹ લાખ)", "min_emi":"ન્યૂનતમ EMI (₹)", "max_emi":"મહત્તમ EMI (₹)", "preferred_city":"પસંદનું શહેર", "preferred_locality":"પસંદની લોકેલિટી", "required_bhk":"જરૂરી BHK", "minimum_bathrooms":"ન્યૂનતમ બાથરૂમ", "minimum_area":"ન્યૂનતમ વિસ્તાર (ચોરસ ફૂટ)",
        "find_homes":"🔍 ઘર શોધો", "modify_search":"⬅️ સર્ચ બદલો", "view_details":"વિગતો જુઓ ➔", "back_search":"⬅️ સર્ચ પરિણામો પર પાછા જાઓ", "deep_compare":"⚖️ ડીપ AI સરખામણી ચલાવો", "back_homes":"⬅️ ઘરના પરિણામો પર પાછા જાઓ", "modify_comparison":"⬅️ સરખામણી બદલો", "city_a":"શહેર A", "locality_a":"લોકેલિટી A", "bhk_a":"BHK A", "area_a":"વિસ્તાર A (ચોરસ ફૂટ)", "city_b":"શહેર B", "locality_b":"લોકેલિટી B", "bhk_b":"BHK B", "area_b":"વિસ્તાર B (ચોરસ ફૂટ)",
        "analytics_city":"🌍 1. શહેર પસંદ કરો", "analytics_areas":"📍 2. વિસ્તારો પસંદ કરો", "analytics_bhk":"🛏️ 3. BHK પસંદ કરો", "generate_analytics":"📊 એનાલિટિક્સ ડેશબોર્ડ બનાવો", "modify_filters":"⬅️ ફિલ્ટર્સ બદલો", "revalue":"🔍 ફરી વેલ્યુએશન", "nearby_title":"આસપાસની સુવિધાઓ", "nearby_sub":"તમારા પસંદ કરેલા વિસ્તારની આસપાસની {count} સૌથી નજીકની વાસ્તવિક જગ્યાઓ.", "chat_welcome":"નમસ્તે! હું તમારો સ્માર્ટ રિયલ એસ્ટેટ AI 🤖 છું. પ્રોપર્ટીની કિંમત, રોકાણ અથવા સલામતી વિશે પૂછો.", "places":"જગ્યાઓ", "all_cities":"બધા શહેરો", "all_localities":"બધી લોકેલિટી", "any":"કોઈપણ", "nearby_eyebrow":"આસપાસની મુખ્ય સુવિધાઓ", "nearby_category":"આસપાસની સુવિધાઓ", "no_live_pois":"OpenStreetMap પરથી હાલમાં કોઈ વાસ્તવિક નામવાળી સુવિધા મળી નથી. થોડી વાર પછી ફરી પ્રયાસ કરો.", "nearby_caption":"પસંદ કરેલા વિસ્તારની આસપાસની વાસ્તવિક સુવિધાઓ — નામ, શ્રેણી અને અંતર.",
        "ai_help":"🤖 વિસ્તાર, BHK, કિંમત, કદ, સસ્તો/મોંઘો વિસ્તાર, સરખામણી, બજેટ, ROI અથવા સલામતી વિશે પૂછો.", "saved_properties":"પ્રોપર્ટીઝ મનપસંદમાં સેવ છે", "automated_email":"📧 ઈમેલ સફળતાપૂર્વક મોકલાયો!", "email_sent_suffix":"{area}ની વિગતવાર પ્રોપર્ટી બ્રોશર {email} પર મોકલવામાં આવી છે. અમારી ટીમ જલ્દી સંપર્ક કરશે.", "fill_name_email":"⚠️ કૃપા કરીને નામ અને ઈમેલ ભરો.", "dataset_required":"⚠️ બજેટ સર્ચ માટે ડેટાસેટ જરૂરી છે.", "affordability":"💡 પોષાય તેવી કિંમતનો અંદાજ:", "configs_found":"🎉 તમારી શરતો સાથે મેળ ખાતી {count} પ્રીમિયમ કન્ફિગરેશન મળી!", "max_compare":"તમે એક સમયે મહત્તમ 2 પ્રોપર્ટીની સરખામણી કરી શકો છો. પહેલા એક દૂર કરો.", "no_matching":"આ બધા ફિલ્ટર્સ સાથે મેળ ખાતી પ્રોપર્ટી મળી નથી. બજેટ અથવા સુવિધાઓમાં થોડી છૂટ આપો.", "reference_style":"રેફરન્સ-સ્ટાઇલ પ્રેઝન્ટેશન — ફ્લોર પ્લાન, લેબલ કરેલી જગ્યાઓ અને વાસ્તવિક ઇન્ટિરિયર દૃશ્યો.", "model_missing":"મોડલ મળ્યું નથી!", "no_dataset_analytics":"⚠️ એનાલિટિક્સ બતાવવા માટે ડેટાસેટ મળ્યું નથી.", "not_enough_amenity":"સુવિધાઓના વધારાના મૂલ્યની ગણતરી માટે પૂરતો વિવિધ ડેટા નથી.", "properties_analyzed":"વિશ્લેષિત પ્રોપર્ટીઝ", "average_price":"સરેરાશ કિંમત", "peak_valuation":"ટોચનું વેલ્યુએશન", "avg_sqft":"સરેરાશ કિંમત / ચોરસ ફૂટ", "wealth_forecast":"🔮 10 વર્ષનું સંપત્તિ વૃદ્ધિ અનુમાન", "amenity_impact":"💎 સુવિધાથી વધતું મૂલ્ય", "top_markets":"📊 ટોચના 5 પ્રીમિયમ માર્કેટ", "area_price":"📈 વિસ્તાર વિ. કિંમત વિતરણ", "your_shortlist":"તમારી શોર્ટલિસ્ટ", "favorites_title":"મારા મનપસંદ", "favorites_sub":"પછી જોવા માટે સેવ કરેલી તમામ પ્રોપર્ટીઝ અહીં છે."
    },
}

# ===== Separate feature-page translations =====
TRANSLATIONS["English"].update({
    "nav_recommendations":"✨ Smart Recommendations", "nav_price_intelligence":"📈 Price Intelligence",
    "nav_home_buying_cost":"🧮 Home-Buying Cost", "nav_smart_compare":"⚖️ Smart Compare",
    "nav_connectivity":"📍 Connectivity Score", "nav_property_ai":"🤖 Property AI",
    "nav_gallery":"🖼️ Gallery & Tour", "nav_dashboard":"❤️ Personal Dashboard",
    "nav_smart_alerts":"🔔 Smart Alerts", "nav_property_report":"📄 Property Report",
})
TRANSLATIONS["Hindi"].update({
    "nav_recommendations":"✨ स्मार्ट रिकमेंडेशन", "nav_price_intelligence":"📈 प्राइस इंटेलिजेंस",
    "nav_home_buying_cost":"🧮 घर खरीदने की लागत", "nav_smart_compare":"⚖️ स्मार्ट तुलना",
    "nav_connectivity":"📍 कनेक्टिविटी स्कोर", "nav_property_ai":"🤖 प्रॉपर्टी AI",
    "nav_gallery":"🖼️ गैलरी और टूर", "nav_dashboard":"❤️ पर्सनल डैशबोर्ड",
    "nav_smart_alerts":"🔔 स्मार्ट अलर्ट", "nav_property_report":"📄 प्रॉपर्टी रिपोर्ट",
})
TRANSLATIONS["Gujarati"].update({
    "nav_recommendations":"✨ સ્માર્ટ ભલામણો", "nav_price_intelligence":"📈 પ્રાઇસ ઇન્ટેલિજન્સ",
    "nav_home_buying_cost":"🧮 ઘર ખરીદી ખર્ચ", "nav_smart_compare":"⚖️ સ્માર્ટ સરખામણી",
    "nav_connectivity":"📍 કનેક્ટિવિટી સ્કોર", "nav_property_ai":"🤖 પ્રોપર્ટી AI",
    "nav_gallery":"🖼️ ગેલેરી અને ટૂર", "nav_dashboard":"❤️ પર્સનલ ડેશબોર્ડ",
    "nav_smart_alerts":"🔔 સ્માર્ટ અલર્ટ", "nav_property_report":"📄 પ્રોપર્ટી રિપોર્ટ",
})

def t(key, **kwargs):
    lang = st.session_state.get("language", "English")
    value = TRANSLATIONS.get(lang, TRANSLATIONS["English"]).get(key, TRANSLATIONS["English"].get(key, key))
    try:
        return value.format(**kwargs)
    except Exception:
        return value

CATEGORY_TRANSLATIONS = {
    "Hindi": {
        "School":"स्कूल", "College / University":"कॉलेज / विश्वविद्यालय", "Hospital":"अस्पताल", "Pharmacy":"फार्मेसी", "Shopping Mall":"शॉपिंग मॉल", "Shop / Supermarket":"दुकान / सुपरमार्केट", "Bank / ATM":"बैंक / एटीएम", "Restaurant":"रेस्टोरेंट", "Cafe":"कैफे", "Park / Garden":"पार्क / गार्डन", "Metro / Railway":"मेट्रो / रेलवे", "Bus / Transit":"बस / ट्रांज़िट", "Petrol Pump":"पेट्रोल पंप", "Police":"पुलिस", "Worship":"पूजा स्थल", "Gym / Fitness":"जिम / फिटनेस", "Cinema":"सिनेमा", "Hotel":"होटल", "Post Office":"डाकघर", "Library":"पुस्तकालय", "Healthcare / Doctor":"स्वास्थ्य / डॉक्टर"
    },
    "Gujarati": {
        "School":"શાળા", "College / University":"કોલેજ / યુનિવર્સિટી", "Hospital":"હોસ્પિટલ", "Pharmacy":"ફાર્મસી", "Shopping Mall":"શોપિંગ મોલ", "Shop / Supermarket":"દુકાન / સુપરમાર્કેટ", "Bank / ATM":"બેંક / એટીએમ", "Restaurant":"રેસ્ટોરન્ટ", "Cafe":"કેફે", "Park / Garden":"પાર્ક / ગાર્ડન", "Metro / Railway":"મેટ્રો / રેલવે", "Bus / Transit":"બસ / ટ્રાન્ઝિટ", "Petrol Pump":"પેટ્રોલ પંપ", "Police":"પોલીસ", "Worship":"પૂજા સ્થળ", "Gym / Fitness":"જિમ / ફિટનેસ", "Cinema":"સિનેમા", "Hotel":"હોટેલ", "Post Office":"પોસ્ટ ઓફિસ", "Library":"લાઇબ્રેરી", "Healthcare / Doctor":"હેલ્થકેર / ડૉક્ટર"
    }
}

def tc(category):
    lang = st.session_state.get("language", "English")
    return CATEGORY_TRANSLATIONS.get(lang, {}).get(category, category)

# --- Initialize Memory (Session States) ---
if 'main_nav' not in st.session_state: st.session_state.main_nav = PAGES["prediction"]
if 'pending_nav' not in st.session_state: st.session_state.pending_nav = None
if 'compare_return_page' not in st.session_state: st.session_state.compare_return_page = None
if 'history' not in st.session_state: st.session_state.history = []
if 'chat_msgs' not in st.session_state: st.session_state.chat_msgs = [{"role": "bot", "content": t("chat_welcome")}]
if 'last_pred' not in st.session_state: st.session_state.last_pred = 0
if 'last_loc' not in st.session_state: st.session_state.last_loc = ""
# Theme state: light by default, but user can switch from the sidebar.
if 'dark_mode' not in st.session_state:
    st.session_state.dark_mode = False
if 'dark_mode_ui' not in st.session_state:
    st.session_state.dark_mode_ui = st.session_state.dark_mode

# --- New: Favorites / Recently Viewed / Currency / Language / Cross-page compare ---
if 'favorites' not in st.session_state: st.session_state.favorites = []          # list of property dicts
if 'recently_viewed' not in st.session_state: st.session_state.recently_viewed = []  # list of property dicts (max 5)
if 'currency_mode' not in st.session_state: st.session_state.currency_mode = "Lakhs"  # Lakhs / Crores / Full
if 'language' not in st.session_state: st.session_state.language = "English"      # English / Hindi / Gujarati
if 'compare_selection' not in st.session_state: st.session_state.compare_selection = []  # up to 2 props picked from search results
if 'saved_searches' not in st.session_state: st.session_state.saved_searches = []  # saved budget-search presets
if 'valuation_history' not in st.session_state: st.session_state.valuation_history = []
if 'comparison_history' not in st.session_state: st.session_state.comparison_history = []
if 'report_history' not in st.session_state: st.session_state.report_history = []
if 'smart_alerts' not in st.session_state: st.session_state.smart_alerts = []  # in-app smart alert rules
if 'feature_selected_property' not in st.session_state: st.session_state.feature_selected_property = None

# 1. AI Valuation States
if 'p_executed' not in st.session_state: st.session_state.p_executed = False
if 'req_city' not in st.session_state: st.session_state.req_city = "Ahmedabad"
if 'req_area' not in st.session_state: st.session_state.req_area = "Bodakdev"
if 'req_bhk' not in st.session_state: st.session_state.req_bhk = 3
if 'req_sqft' not in st.session_state: st.session_state.req_sqft = 1500
if 'req_bath' not in st.session_state: st.session_state.req_bath = 3
if 'ui_bhk' not in st.session_state: st.session_state.ui_bhk = st.session_state.req_bhk
if 'ui_area' not in st.session_state: st.session_state.ui_area = st.session_state.req_sqft
if 'ui_bath' not in st.session_state: st.session_state.ui_bath = st.session_state.req_bath
if 'p_gym' not in st.session_state: st.session_state.p_gym = False
if 'p_pool' not in st.session_state: st.session_state.p_pool = False
if 'p_cinema' not in st.session_state: st.session_state.p_cinema = False
if 'p_gaming' not in st.session_state: st.session_state.p_gaming = False
if 'p_solar' not in st.session_state: st.session_state.p_solar = False

# 2. Budget Search States
if 'search_executed' not in st.session_state: st.session_state.search_executed = False
if 'b_search_params' not in st.session_state: st.session_state.b_search_params = {}
if 'viewing_property' not in st.session_state: st.session_state.viewing_property = None
if 'property_ai_answer' not in st.session_state: st.session_state.property_ai_answer = ''
if 'property_ai_question' not in st.session_state: st.session_state.property_ai_question = ''
if 'b_ui_bhk' not in st.session_state: st.session_state.b_ui_bhk = "Any"
if 'b_ui_area' not in st.session_state: st.session_state.b_ui_area = 0
if 'b_ui_bath' not in st.session_state: st.session_state.b_ui_bath = "Any"
if 'b_gym' not in st.session_state: st.session_state.b_gym = False
if 'b_pool' not in st.session_state: st.session_state.b_pool = False
if 'b_cinema' not in st.session_state: st.session_state.b_cinema = False
if 'b_gaming' not in st.session_state: st.session_state.b_gaming = False
if 'b_solar' not in st.session_state: st.session_state.b_solar = False

# 3. Compare Properties States
if 'c_compare_executed' not in st.session_state: st.session_state.c_compare_executed = False
if 'c_params' not in st.session_state: st.session_state.c_params = {}
if 'ui_ca_bhk' not in st.session_state: st.session_state.ui_ca_bhk = 3
if 'ui_ca_sqft' not in st.session_state: st.session_state.ui_ca_sqft = 1500
if 'ui_cb_bhk' not in st.session_state: st.session_state.ui_cb_bhk = 3
if 'ui_cb_sqft' not in st.session_state: st.session_state.ui_cb_sqft = 1500

# 4. Analytics Dashboard States
if 'a_executed' not in st.session_state: st.session_state.a_executed = False
if 'a_sel_city' not in st.session_state: st.session_state.a_sel_city = "All Cities"
if 'a_sel_areas' not in st.session_state: st.session_state.a_sel_areas = []
if 'a_sel_bhk' not in st.session_state: st.session_state.a_sel_bhk = []

# EMI Defaults
if 'emi_dp' not in st.session_state: st.session_state.emi_dp = 20
if 'emi_tenure' not in st.session_state: st.session_state.emi_tenure = 20
if 'emi_interest' not in st.session_state: st.session_state.emi_interest = 8.5

# Navigation Callbacks
def open_property_view(city, area, bhk, sqft, price):
    st.session_state.viewing_property = {
        "City": city, "Area_Name": area, "BHK": bhk, "Area": sqft, "Historical_Price": price
    }
    st.session_state.property_ai_answer = ''
    st.session_state.property_ai_question = ''

def close_property_view():
    st.session_state.viewing_property = None
    st.session_state.property_ai_answer = ''
    st.session_state.property_ai_question = ''

def reset_views():
    st.session_state.viewing_property = None
    st.session_state.search_executed = False
    st.session_state.c_compare_executed = False
    st.session_state.p_executed = False
    st.session_state.a_executed = False

def save_budget_search(params, name="My Property Search"):
    """Save the current Budget-First Search preset and persist it for logged-in users."""
    if not isinstance(params, dict):
        return False

    item = dict(params)
    item["name"] = str(name or "My Property Search").strip()[:80]
    item["saved_at"] = datetime.now().strftime("%d %b %Y, %I:%M %p")

    # Stable signature prevents accidental duplicate saves of the same filters.
    item["signature"] = "|".join([
        str(item.get("min_b", 0)),
        str(item.get("max_b", 0)),
        str(item.get("target_budget", 0)),
        str(item.get("user_city", "All Cities")),
        str(item.get("user_locality", "All Localities")),
        str(item.get("user_req_bhk", "Any")),
        str(item.get("user_bath", "Any")),
        str(item.get("user_area", 0)),
    ])

    existing = [x for x in st.session_state.get("saved_searches", []) if isinstance(x, dict)]
    # Replace an existing identical search instead of creating duplicates.
    existing = [x for x in existing if x.get("signature") != item["signature"]]
    existing.insert(0, item)
    st.session_state.saved_searches = existing[:20]

    if st.session_state.get("auth_user"):
        _persist_account_data(saved_searches=[dict(x) for x in st.session_state.saved_searches])

    return True


def modify_prediction(): st.session_state.p_executed = False
def modify_search():
    st.session_state.search_executed = False
    st.session_state.pop('budget_inline_detail_key', None)
def modify_compare(): st.session_state.c_compare_executed = False
def modify_analytics(): st.session_state.a_executed = False


# ==========================================

def _protected_download_button(label, data, file_name, mime, key, action="download this PDF"):
    if st.session_state.get("auth_user"):
        clicked = st.download_button(label, data=data, file_name=file_name, mime=mime, use_container_width=True, key=key)
        if clicked:
            record_report_history(
                title=action.replace("download ", "").title(),
                file_name=file_name,
                property_info={"Area_Name": st.session_state.get("req_area", ""), "Location": st.session_state.get("last_loc", "")},
            )
        return clicked
    if st.button("🔐 Login to Download PDF", use_container_width=True, key=f"{key}_login"):
        _require_auth(action)
    st.caption("Login is required only when you download the PDF.")
    return False


def _share_valuation_payload(area_name, city, bhk, area, final_price, confidence=None):
    conf_line = f"🎯 Prediction confidence: {confidence}%" if confidence is not None else ""
    return (
        "🏡 SmartEstate Property Valuation\n"
        f"📍 {area_name}, {city}\n"
        f"🛏️ {bhk} BHK  •  📐 {area:,.0f} sq.ft\n"
        f"💰 Estimated value: {format_price(final_price)}\n"
        f"{conf_line}\n"
        "✨ Generated by SmartEstate AI"
    )


def _get_card_font(size, bold=False):
    """Best-effort font loader for generated share-card PNG."""
    if not HAS_PIL:
        return None
    candidates = []
    if os.name == "nt":
        candidates += [
            r"C:\\Windows\\Fonts\\segoeui.ttf",
            r"C:\\Windows\\Fonts\\segoeuib.ttf",
        ]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _build_valuation_share_card(area_name, city, bhk, area, final_price, confidence=None):
    """Create a WhatsApp-friendly 1200x630 PNG property card."""
    if not HAS_PIL:
        return None

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), "#FBF7EF")
    d = ImageDraw.Draw(img)

    # Warm premium background blocks.
    d.rounded_rectangle((28, 28, W - 28, H - 28), radius=34, fill="#FFFDF9", outline="#E6D3AE", width=3)
    d.rounded_rectangle((28, 28, 430, H - 28), radius=34, fill="#F1E2C9")
    d.rectangle((394, 28, 430, H - 28), fill="#F1E2C9")

    # Decorative building silhouette.
    for x, h in [(72, 180), (128, 245), (184, 205), (240, 280), (296, 225)]:
        y0 = H - 74 - h
        d.rounded_rectangle((x, y0, x + 38, H - 74), radius=7, fill="#DFC49A")
        for wy in range(int(y0 + 18), H - 92, 28):
            d.rounded_rectangle((x + 8, wy, x + 16, wy + 10), radius=2, fill="#FFF8EA")
            d.rounded_rectangle((x + 22, wy, x + 30, wy + 10), radius=2, fill="#FFF8EA")

    gold = "#DAA752"
    brown = "#5D432C"
    muted = "#8F7960"

    title_font = _get_card_font(30, bold=True)
    sub_font = _get_card_font(17, bold=False)
    eyebrow_font = _get_card_font(13, bold=True)
    price_font = _get_card_font(52, bold=True)
    meta_font = _get_card_font(19, bold=True)
    small_font = _get_card_font(15, bold=False)
    score_font = _get_card_font(22, bold=True)

    d.text((72, 56), "SMARTESTATE", font=eyebrow_font, fill=gold)
    d.text((72, 94), "Property Valuation", font=title_font, fill=brown)
    d.text((72, 138), "AI-powered estimated market value", font=sub_font, fill=muted)

    # Main content panel.
    panel_x, panel_y = 470, 62
    d.rounded_rectangle((panel_x, panel_y, 1140, 568), radius=28, fill="#FFFCF7", outline="#EADABB", width=2)
    d.text((520, 102), "ESTIMATED MARKET VALUE", font=eyebrow_font, fill=gold)
    d.text((520, 150), format_price(final_price), font=price_font, fill=gold)

    # Property details.
    display_area = f"{area:,.0f} sq.ft"
    d.text((520, 244), f"{bhk} BHK  •  {display_area}", font=meta_font, fill=brown)
    d.text((520, 286), f"{area_name}, {city}", font=sub_font, fill=muted)

    # Confidence badge.
    if confidence is not None:
        try:
            conf_text = f"Prediction confidence: {float(confidence):.0f}%"
        except Exception:
            conf_text = f"Prediction confidence: {confidence}%"
    else:
        conf_text = "AI valuation estimate"
    d.rounded_rectangle((520, 334, 890, 382), radius=18, fill="#F8EBCF", outline="#E7C982", width=1)
    d.text((540, 346), "🎯  " + conf_text, font=small_font, fill="#7C5A24")

    d.text((520, 424), "SmartEstate AI", font=score_font, fill=brown)
    d.text((520, 458), "Premium property insight card", font=small_font, fill=muted)

    # Gold strip + footer.
    d.rounded_rectangle((930, 96, 1090, 156), radius=18, fill=gold)
    d.text((956, 112), "FLAT", font=eyebrow_font, fill="#FFFDF8")
    d.line((520, 510, 1090, 510), fill="#E9D9BD", width=2)
    d.text((520, 526), "Shared from SmartEstate • Property Intelligence", font=small_font, fill=muted)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_share_valuation(area_name, city, bhk, area, final_price, confidence=None, key_suffix="main"):
    """Compact, responsive valuation-share UI with a small preview card."""
    share_text = _share_valuation_payload(area_name, city, bhk, area, final_price, confidence)
    encoded_text = urllib.parse.quote(share_text)
    encoded_subject = urllib.parse.quote(f"SmartEstate Valuation – {area_name}, {city}")
    encoded_mail = urllib.parse.quote(share_text)
    whatsapp_url = f"https://wa.me/?text={encoded_text}"
    email_url = f"mailto:?subject={encoded_subject}&body={encoded_mail}"
    share_json = json.dumps(share_text)
    card_bytes = _build_valuation_share_card(area_name, city, bhk, area, final_price, confidence)

    st.markdown(
        """
        <div class="share-card-premium share-card-compact">
          <div class="share-compact-left">
            <div class="share-eyebrow">PROPERTY SHARE CARD</div>
            <div class="share-heading">📤 Share This Valuation</div>
          </div>
          <div class="share-badge">GOLD</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Small preview instead of a full-width giant image.
    if card_bytes:
        st.image(card_bytes, width=320)

    # Two compact columns keep labels readable even when this block is in a narrow column/sidebar.
    c1, c2 = st.columns(2, gap="small")
    with c1:
        whatsapp_id = f"share_whatsapp_card_{key_suffix}"
        components.html(
            f"""
            <button id='{whatsapp_id}' class='share-mini-btn share-mini-whatsapp'>
              <span>🟢</span><b>WhatsApp</b>
            </button>
            <script>
            (async()=>{{
              const btn=document.getElementById('{whatsapp_id}');
              btn.addEventListener('click', async()=>{{
                try {{
                  const canvas=document.createElement('canvas');
                  canvas.width=1200; canvas.height=630;
                  const ctx=canvas.getContext('2d');
                  ctx.fillStyle='#FBF7EF'; ctx.fillRect(0,0,1200,630);
                  ctx.fillStyle='#DAA752'; ctx.fillRect(0,0,18,630);
                  ctx.fillStyle='#5D432C'; ctx.font='bold 36px Arial'; ctx.fillText('SmartEstate Property Valuation',52,78);
                  ctx.fillStyle='#8F7960'; ctx.font='24px Arial'; ctx.fillText('{html.escape(area_name)}, {html.escape(city)}',52,124);
                  ctx.fillStyle='#DAA752'; ctx.font='bold 64px Arial'; ctx.fillText('{html.escape(format_price(final_price))}',52,224);
                  ctx.fillStyle='#5D432C'; ctx.font='bold 28px Arial'; ctx.fillText('{html.escape(str(bhk))} BHK  •  {html.escape(f'{area:,.0f} sq.ft')}',52,278);
                  ctx.fillStyle='#8F7960'; ctx.font='22px Arial'; ctx.fillText('AI-powered estimate • Shared from SmartEstate',52,338);
                  canvas.toBlob(async(blob)=>{{
                    const file=new File([blob], 'SmartEstate_Valuation_Card.png', {{type:'image/png'}});
                    if (navigator.canShare && navigator.canShare({{files:[file]}}) && navigator.share) {{
                      await navigator.share({{title:'SmartEstate Valuation',text:{share_json},files:[file]}});
                      btn.innerHTML='<span>✅</span><b>Shared</b>';
                    }} else {{
                      const a=document.createElement('a');
                      a.href=URL.createObjectURL(blob);
                      a.download='SmartEstate_Valuation_Card.png';
                      document.body.appendChild(a); a.click(); a.remove();
                      setTimeout(()=>window.open('{whatsapp_url}', '_blank'), 350);
                      btn.innerHTML='<span>✅</span><b>Downloaded</b>';
                    }}
                  }},'image/png');
                }} catch(e) {{
                  window.open('{whatsapp_url}', '_blank');
                }}
              }});
            }})();
            </script>
            """,
            height=52,
            scrolling=False,
        )
    with c2:
        st.download_button(
            "🖼️ Download",
            data=card_bytes if card_bytes else share_text,
            file_name=f"smartestate_{re.sub(r'[^a-zA-Z0-9]+','_',area_name).strip('_').lower()}_valuation_card.png" if card_bytes else f"smartestate_{re.sub(r'[^a-zA-Z0-9]+','_',area_name).strip('_').lower()}_valuation.txt",
            mime="image/png" if card_bytes else "text/plain",
            use_container_width=True,
            key=f"share_card_download_{key_suffix}",
        )

    c3, c4 = st.columns(2, gap="small")
    with c3:
        st.markdown(
            f'<a class="share-action share-action-compact email" href="{email_url}" target="_blank">✉️ <b>Email</b></a>',
            unsafe_allow_html=True,
        )
    with c4:
        copy_id = f"share_copy_{key_suffix}"
        components.html(
            f"""
            <button id='{copy_id}' class='share-mini-btn share-mini-copy'>📋 <b>Copy Summary</b></button>
            <script>
              const btn=document.getElementById('{copy_id}');
              const txt={share_json};
              btn.addEventListener('click', async()=>{{
                try{{ await navigator.clipboard.writeText(txt); btn.innerHTML='✅ <b>Copied</b>'; }}
                catch(e){{ btn.innerHTML='⚠️ <b>Copy unavailable</b>'; }}
              }});
            </script>
            """,
            height=52,
            scrolling=False,
        )

    with st.expander("ℹ️ Sharing help", expanded=False):
        st.caption("On supported phones, WhatsApp uses the native share sheet and can attach the PNG card. On desktop, the card is downloaded first so you can attach it in WhatsApp Web.")

# ==========================================
# CUSTOM CSS
# ==========================================
if st.session_state.dark_mode:
    app_bg = "linear-gradient(-45deg, #020617, #0f172a, #1e293b, #0f172a)"
    card_bg = "rgba(15, 23, 42, 0.65)"
    sidebar_bg = "rgba(2, 6, 23, 0.95)"
    text_col = "#f8fafc"
    sub_text = "#94a3b8"
    border_col = "rgba(255, 255, 255, 0.1)"
    glow_col = "rgba(220, 176, 101, 0.15)"
    mpl_grid_col = "#ffffff"
else:
    # Premium light apartment / flat marketplace palette — warm golden theme
    app_bg = "linear-gradient(-45deg, #fbf7ef, #f4ead7, #fffaf2, #f7eddc)"
    card_bg = "rgba(255, 255, 255, 0.94)"
    sidebar_bg = "rgba(255, 252, 246, 0.98)"
    text_col = "#33261b"
    sub_text = "#7b6a58"
    border_col = "rgba(218, 167, 82, 0.22)"
    glow_col = "rgba(218, 167, 82, 0.18)"
    mpl_grid_col = "#dfd1b7"

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
html, body, [class*="css"] {{ font-family: 'Inter', sans-serif; }}

.stMarkdown a, .stMarkdown a:hover, a {{ text-decoration: none !important; border-bottom: none !important; outline: none !important; box-shadow: none !important; }}

@keyframes gradientBG {{ 0% {{background-position: 0% 50%;}} 50% {{background-position: 100% 50%;}} 100% {{background-position: 0% 50%;}} }}
@keyframes fadeInUp {{ from {{opacity: 0; transform: translateY(30px);}} to {{opacity: 1; transform: translateY(0);}} }}
@keyframes slideInLeft {{ from {{opacity: 0; transform: translateX(-20px);}} to {{opacity: 1; transform: translateX(0);}} }}

.stApp {{ background: {app_bg}; background-size: 400% 400%; animation: gradientBG 24s ease infinite; }}
.animate-up {{ animation: fadeInUp 0.7s cubic-bezier(0.16, 1, 0.3, 1) forwards; opacity: 0; }}
.delay-1 {{ animation-delay: 0.1s; }}
.delay-2 {{ animation-delay: 0.2s; }}
.delay-3 {{ animation-delay: 0.3s; }}

header[data-testid="stHeader"] {{ background: transparent !important; }}
[data-testid="stSidebarNav"] {{ display: none; }}
[data-testid="stSidebar"] {{ background-color: {sidebar_bg} !important; backdrop-filter: blur(20px); border-right: 1px solid {border_col}; transition: all 0.3s ease; }}
[data-testid="stSidebar"] * {{ color: {text_col} !important; }}
.sidebar-logo {{ padding: 10px 0 20px; animation: slideInLeft 0.6s ease forwards; }}
.sidebar-logo h2 {{ color: {text_col} !important; font-size: 24px; font-weight: 800; margin: 0; border:none !important; }}
.sidebar-logo p {{ color: #daa752 !important; font-size: 11px; margin-top: 2px; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; }}

[data-testid="stSidebar"] div[role="radiogroup"] > label > div:first-child {{ display: none !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label {{ background-color: transparent; padding: 12px 15px; border-radius: 12px; margin-bottom: 10px; transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); border: 1px solid transparent; cursor: pointer; width: 100%; display: flex; align-items: center; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {{ background-color: {border_col}; transform: translateX(5px); border-color: rgba(218, 167, 82, 0.22); }}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] {{ background-color: rgba(218, 167, 82, 0.11) !important; border-left: 4px solid #daa752 !important; box-shadow: inset 2px 0 10px rgba(218, 167, 82, 0.06); }}
[data-testid="stSidebar"] div[role="radiogroup"] p {{ color: {text_col} !important; font-size: 15px !important; font-weight: 600 !important; margin: 0 !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] p {{ color: #daa752 !important; }}

.hero-banner {{ background-image: url('{BG_IMAGE_URL}'); background-size: cover; background-position: center; border-radius: 24px; padding: 60px 40px; position: relative; margin-bottom: 30px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 15px 35px rgba(0,0,0,0.1); overflow: hidden; }}
.hero-banner::before {{ content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, {sidebar_bg} 0%, rgba(255,255,255,0) 80%, transparent 100%); }}
.hero-content {{ position: relative; z-index: 1; max-width: 600px; animation: slideInLeft 0.8s ease forwards; }}
.hero-eyebrow {{ color: #b9842f; font-size: 12px; font-weight: 800; letter-spacing: 3px; text-transform: uppercase; margin-bottom: 10px; }}
.hero-title {{ font-size: 52px; font-weight: 800; color: {text_col}; line-height: 1.1; margin: 0; }}
.gradient-text {{ background: linear-gradient(90deg, #8a5a18, #d9a84f); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
.hero-sub {{ color: {text_col}; font-size: 16px; margin-top: 15px; font-weight: 500; opacity: 0.85; line-height: 1.5; }}

div[data-testid="stHorizontalBlock"] {{ gap: 1.5rem !important; }}
.card-header {{ font-size: 18px; font-weight: 700; color: {text_col}; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }}

label {{ color: {text_col} !important; font-weight: 600 !important; font-size: 13px !important; letter-spacing: 0.3px; }}
.stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"] > div {{ background-color: {card_bg} !important; border: 1px solid {border_col} !important; border-radius: 10px !important; color: {text_col} !important; transition: all 0.3s ease !important; }}
.stTextInput input:focus, .stSelectbox div[data-baseweb="select"] > div:focus-within {{ border-color: #daa752 !important; box-shadow: 0 0 0 2px rgba(218,167,82,0.22) !important; transform: translateY(-1px); }}

.stButton > button {{ background: linear-gradient(90deg, #daa752, #c7953e); color: #ffffff !important; border: none; border-radius: 12px; width: 100%; min-height: 54px; font-size: 16px; font-weight: 700; margin-top: 5px; box-shadow: 0 4px 15px rgba(218, 167, 82, 0.4); transition: all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275); text-shadow: 0 1px 2px rgba(0,0,0,0.2); }}
.stButton > button:hover {{ transform: translateY(-4px) scale(1.01); box-shadow: 0 10px 25px rgba(218, 167, 82, 0.55); }}

.intel-box {{ background: {card_bg}; backdrop-filter: blur(20px); border: 1px solid {border_col}; border-radius: 16px; padding: 22px; margin-bottom: 15px; height: 100%; transition: all 0.4s cubic-bezier(0.165, 0.84, 0.44, 1); box-shadow: 0 8px 30px rgba(0,0,0,0.04); }}
.intel-box:hover {{ transform: translateY(-5px); box-shadow: 0 15px 35px {glow_col}; border-color: rgba(220, 176, 101, 0.4); }}
.intel-title {{ font-size: 14px; font-weight: 700; color: {text_col}; margin-bottom: 10px; opacity: 0.9; }}
.intel-stat {{ font-size: 26px; font-weight: 800; color: #b47b22; letter-spacing: -0.5px; }}

.metric-card {{ background: {card_bg}; backdrop-filter: blur(20px); border: 1px solid {border_col}; border-radius: 16px; padding: 25px; text-align: center; box-shadow: 0 8px 30px rgba(0,0,0,0.04); transition: all 0.3s ease; }}
.metric-card:hover {{ transform: translateY(-5px); border-color: #daa752; box-shadow: 0 15px 30px {glow_col}; }}
.metric-card .num {{ color: {text_col}; font-size: 34px; font-weight: 800; text-shadow: 0 2px 5px rgba(0,0,0,0.05); }}
.metric-card .lbl {{ color: #b47b22; font-size: 13px; font-weight: 700; text-transform: uppercase; margin-top: 5px; letter-spacing: 1.5px; }}

.val-box {{ background: {card_bg}; backdrop-filter: blur(20px); border: 2px solid #daa752; border-radius: 20px; padding: 35px 25px; text-align: center; margin-bottom: 20px; box-shadow: 0 10px 40px {glow_col}; position: relative; overflow: hidden; }}
.val-price {{ font-size: 48px; font-weight: 800; margin: 15px 0; color: #b47b22; display: inline-block; }}
.val-range {{ color: {sub_text}; font-size: 14px; font-weight: 600; padding: 8px 15px; background: rgba(0,0,0,0.05); border-radius: 20px; display: inline-block; }}

.property-image {{ border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.15); margin-bottom: 15px; width: 100%; object-fit: cover; height: 320px; border: 2px solid transparent; transition: all 0.5s cubic-bezier(0.25, 0.8, 0.25, 1); }}
.property-image:hover {{ transform: scale(1.04) rotate(-1deg); box-shadow: 0 20px 40px rgba(0,0,0,0.3); border-color: #daa752; cursor: crosshair; }}

.ai-engine-status {{
  display:flex; align-items:center; gap:9px; margin:4px 0 12px; padding:10px 12px;
  border:1px solid rgba(16,185,129,.20); background:rgba(16,185,129,.08);
  border-radius:12px; font-size:13px; font-weight:600;
}}
.ai-status-dot {{ width:9px; height:9px; border-radius:50%; background:#34d399; box-shadow:0 0 10px rgba(52,211,153,.55); flex:0 0 auto; }}
.ai-chat-stack {{ display:flex; flex-direction:column; gap:10px; margin-top:12px; max-height:300px; overflow-y:auto; padding:3px 2px 8px; }}
.ai-chat-row {{ display:flex; width:100%; }}
.ai-chat-row.user {{ justify-content:flex-end; }}
.ai-chat-row.bot {{ justify-content:flex-start; }}
.ai-chat-bubble {{ padding:12px 15px; border-radius:16px; font-size:13px; line-height:1.55; font-weight:500; max-width:88%; box-shadow:0 4px 15px rgba(0,0,0,.05); word-break:break-word; overflow-wrap:anywhere; }}
.ai-chat-bubble.user {{ background:linear-gradient(135deg,#daa752,#b88a44); color:#fff !important; border-bottom-right-radius:5px; }}
.ai-chat-bubble.bot {{ background:{card_bg}; color:{text_col} !important; border:1px solid {border_col}; border-bottom-left-radius:5px; }}

.btn-back > button {{ background: transparent !important; color: {text_col} !important; border: 1px solid {border_col} !important; box-shadow: none !important; min-height: 40px !important; margin-bottom: 20px; width: auto !important; padding: 0 20px !important; }}
.btn-back > button:hover {{ border-color: #daa752 !important; color: #daa752 !important; }}

/* Premium Compare Layout */
.compare-card {{ background: {card_bg}; border: 1px solid {border_col}; border-radius: 16px; padding: 25px; box-shadow: 0 8px 30px rgba(0,0,0,0.05); backdrop-filter: blur(20px); height: 100%; }}
.compare-row {{ display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid {border_col}; }}
.compare-row:last-child {{ border-bottom: none; }}
.c-label {{ color: {sub_text}; font-size: 14px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-right: 10px; }}
.c-val {{ color: {text_col}; font-size: 15px; font-weight: 700; text-align: right; white-space: nowrap; }}
.c-win {{ color: #10b981; font-weight: 800; font-size: 16px; }}
.dual-verdict-box {{ background: linear-gradient(135deg, rgba(218, 167, 82, 0.11), rgba(16, 185, 129, 0.1)); border: 1px solid rgba(220, 176, 101, 0.3); border-radius: 16px; padding: 25px; margin-top: 20px; display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}

/* POI Cards */
.poi-card {{ background: {card_bg}; border: 1px solid {border_col}; border-radius: 12px; padding: 15px; margin-bottom: 15px; display: flex; align-items: center; justify-content: space-between; transition: all 0.3s ease; backdrop-filter: blur(10px); }}
.poi-card:hover {{ border-color: #daa752; transform: translateX(5px); box-shadow: 0 5px 15px rgba(0,0,0,0.05); }}
.poi-icon {{ font-size: 24px; margin-right: 15px; }}
.poi-details {{ flex-grow: 1; }}
.poi-title {{ color: {text_col}; font-weight: 700; font-size: 14px; margin-bottom: 2px; }}
.poi-dist {{ color: #10b981; font-weight: 800; font-size: 16px; }}


/* ✦ PREMIUM UI V2 — lightweight CSS-only motion */
@keyframes premiumGlow {{
  0%,100% {{ box-shadow: 0 0 0 rgba(220,176,101,0), 0 14px 38px rgba(0,0,0,.06); }}
  50% {{ box-shadow: 0 0 28px rgba(218,167,82,.12), 0 18px 44px rgba(0,0,0,.10); }}
}}
@keyframes shimmerSweep {{
  0% {{ transform: translateX(-130%) skewX(-18deg); opacity: 0; }}
  25% {{ opacity: .55; }}
  65%,100% {{ transform: translateX(170%) skewX(-18deg); opacity: 0; }}
}}
@keyframes floatSoft {{
  0%,100% {{ transform: translateY(0); }}
  50% {{ transform: translateY(-4px); }}
}}
@keyframes pulseRing {{
  0% {{ box-shadow: 0 0 0 0 rgba(220,176,101,.25); }}
  70% {{ box-shadow: 0 0 0 10px rgba(220,176,101,0); }}
  100% {{ box-shadow: 0 0 0 0 rgba(220,176,101,0); }}
}}
.hero-banner {{
  animation: premiumGlow 5s ease-in-out infinite;
}}
.hero-banner::after {{
  content:""; position:absolute; top:0; bottom:0; left:-35%; width:22%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.14),transparent);
  transform:skewX(-18deg); animation:shimmerSweep 7s ease-in-out infinite;
  pointer-events:none;
}}
.intel-box,.metric-card,.val-box,.compare-card {{
  position:relative; overflow:hidden;
}}
.intel-box::after,.metric-card::after,.val-box::after {{
  content:""; position:absolute; inset:0; pointer-events:none;
  background:linear-gradient(120deg,transparent 0%,rgba(255,255,255,.055) 48%,transparent 55%);
  transform:translateX(-120%); transition:transform .65s ease;
}}
.intel-box:hover::after,.metric-card:hover::after,.val-box:hover::after {{
  transform:translateX(120%);
}}
.poi-card {{
  min-height:86px;
  border-radius:16px !important;
  animation:floatSoft 4.8s ease-in-out infinite;
}}
.poi-card:nth-child(2n) {{ animation-delay:.7s; }}
.poi-card:nth-child(3n) {{ animation-delay:1.2s; }}
.poi-card:hover {{
  transform:translateY(-5px) scale(1.015) !important;
  box-shadow:0 16px 34px rgba(220,176,101,.14) !important;
}}
.stButton > button:focus-visible,
.stTextInput input:focus-visible,
.stSelectbox div[data-baseweb="select"] > div:focus-visible {{
  outline:2px solid rgba(220,176,101,.75) !important;
  outline-offset:2px;
}}
.stButton > button {{
  position:relative; overflow:hidden;
}}
.stButton > button::after {{
  content:""; position:absolute; top:-40%; left:-25%; width:20%; height:180%;
  background:rgba(255,255,255,.16); transform:skewX(-20deg) translateX(-160%);
  transition:transform .55s ease;
}}
.stButton > button:hover::after {{ transform:skewX(-20deg) translateX(700%); }}
@media (prefers-reduced-motion: reduce) {{
  *,*::before,*::after {{ animation-duration:.001ms !important; transition-duration:.001ms !important; }}
}}

/* Custom Plotly overrides */
.js-plotly-plot .plotly .hoverlayer .hovertext rect {{ rx: 8px !important; ry: 8px !important; }}
</style>
""", unsafe_allow_html=True)

# Sidebar scrolling: target Streamlit's actual scroll container only.
st.markdown("""
<style>
/* CRITICAL SMART ESTATE GOLD THEME - loaded before all other UI CSS */
html, body, [data-testid="stAppViewContainer"], .stApp { background:#FBF7EF !important; }
[data-testid="stSidebar"] { background:#F7EFE3 !important; }
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] { background:rgba(218,167,82,.14) !important; border-color:rgba(218,167,82,.28) !important; box-shadow:inset 3px 0 #DAA752 !important; }
.stButton > button, .stFormSubmitButton > button { background:linear-gradient(135deg,#DAA752,#B87925) !important; border-color:#B87925 !important; color:#fffdf8 !important; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
section[data-testid="stSidebar"] {
    height: 100vh !important;
    max-height: 100vh !important;
    min-height: 100vh !important;
}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {
    height: 100% !important;
    max-height: 100% !important;
    overflow-y: auto !important;
    overflow-x: hidden !important;
    overscroll-behavior: contain !important;
    -webkit-overflow-scrolling: touch !important;
    scrollbar-gutter: stable both-edges !important;
    padding-bottom: 32px !important;
}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
    padding-bottom: 20px !important;
}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"]::-webkit-scrollbar {
    width: 8px;
}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"]::-webkit-scrollbar-thumb {
    background: linear-gradient(180deg, rgba(190,143,67,.92), rgba(126,84,38,.92));
    border-radius: 999px;
}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"]::-webkit-scrollbar-track {
    background: transparent;
}
</style>
""", unsafe_allow_html=True)

# ==========================================
# SMART ESTATE PREMIUM EDITION — UI POLISH
# ==========================================
st.markdown("""
<style>
[data-testid="stAppViewContainer"]{background:linear-gradient(145deg,#fcfaf5 0%,#f7efe3 54%,#f3e5d2 100%) !important;}
[data-testid="stHeader"]{background:rgba(252,250,245,.72) !important;}
.block-container{padding-top:1.35rem !important;padding-bottom:3rem !important;max-width:1500px !important;}
.se-topbar{display:flex;align-items:center;justify-content:space-between;gap:18px;margin:2px 0 20px;padding:13px 17px;border:1px solid rgba(201,151,67,.22);border-radius:18px;background:rgba(255,253,248,.84);box-shadow:0 10px 30px rgba(78,54,35,.07);backdrop-filter:blur(12px);}
.se-topbar-left{display:flex;align-items:center;gap:11px;min-width:0;}
.se-live-dot{width:9px;height:9px;border-radius:50%;background:#b98a42;box-shadow:0 0 0 5px rgba(185,138,66,.12);animation:sePulse 2.2s infinite;}
.se-top-title{font-size:13px;font-weight:900;letter-spacing:.55px;color:#4d3424;text-transform:uppercase;}
.se-top-sub{font-size:11px;color:#8a7764;white-space:nowrap;}
.se-top-pill{padding:7px 11px;border-radius:999px;background:#f8edd9;border:1px solid #ead3aa;color:#765222;font-size:11px;font-weight:900;}
section[data-testid="stSidebar"]{background:linear-gradient(180deg,#fbf6ed 0%,#f4e9d9 100%) !important;border-right:1px solid rgba(111,75,40,.10);}
section[data-testid="stSidebar"] .stRadio label{border-radius:13px !important;color:#624936 !important;}
section[data-testid="stSidebar"] .stRadio label:hover{background:rgba(201,151,67,.09) !important;}
.stTextInput input,.stNumberInput input,.stSelectbox div[data-baseweb="select"]>div,.stMultiSelect div[data-baseweb="select"]>div{border-radius:12px !important;border-color:rgba(112,78,44,.18) !important;background:#fffdf9 !important;}
.stTextInput input:focus,.stNumberInput input:focus{border-color:#c99743 !important;box-shadow:0 0 0 3px rgba(201,151,67,.10) !important;}
.stButton>button,.stFormSubmitButton>button{border-radius:13px !important;border:1px solid #b77e2e !important;background:linear-gradient(135deg,#d5a355,#b97d2c) !important;color:#fffdf8 !important;font-weight:900 !important;box-shadow:0 9px 22px rgba(169,112,39,.16) !important;transition:transform .18s ease,box-shadow .18s ease,filter .18s ease !important;}
.stButton>button:hover,.stFormSubmitButton>button:hover{transform:translateY(-2px);filter:saturate(1.06);box-shadow:0 14px 28px rgba(169,112,39,.23) !important;}
.stDownloadButton>button{border-radius:13px !important;border:1px solid #c99743 !important;background:#fffaf0 !important;color:#6b4a32 !important;font-weight:900 !important;}
.valuation-hero-v2{position:relative;overflow:hidden;border:1px solid rgba(201,151,67,.20) !important;box-shadow:0 22px 60px rgba(78,54,35,.10) !important;}
.valuation-hero-v2:after{content:"";position:absolute;right:-90px;bottom:-120px;width:310px;height:310px;border-radius:50%;background:radial-gradient(circle,rgba(226,191,122,.22),transparent 68%);pointer-events:none;}
.valuation-kicker{letter-spacing:2.4px !important;color:#9b6b2d !important;}
.valuation-hero-copy h1 span{background:linear-gradient(90deg,#b67b2c,#e1bd78,#a66d27) !important;-webkit-background-clip:text !important;background-clip:text !important;color:transparent !important;}
.valuation-panel-v2,.v2-card,.nh-card,.account-card{box-shadow:0 18px 55px rgba(78,54,35,.11) !important;}
.v2-section-head{border-bottom:1px solid rgba(201,151,67,.13) !important;padding-bottom:13px !important;}
.v2-step{background:linear-gradient(135deg,#d7a65b,#b77927) !important;box-shadow:0 8px 18px rgba(183,121,39,.18) !important;}
.se-metric-row{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:0 0 18px;}
.se-metric{background:rgba(255,253,248,.90);border:1px solid rgba(201,151,67,.18);border-radius:16px;padding:14px 15px;box-shadow:0 10px 28px rgba(78,54,35,.06);}
.se-metric-label{font-size:10px;font-weight:900;letter-spacing:1.2px;text-transform:uppercase;color:#9a846c;}
.se-metric-value{font-size:20px;font-weight:950;color:#4d3424;margin-top:4px;}
.se-metric-note{font-size:10px;color:#9b8977;margin-top:3px;}
.stTabs [data-baseweb="tab-list"]{gap:5px;background:rgba(244,232,215,.56);padding:5px;border-radius:14px;}
.stTabs [data-baseweb="tab"]{border-radius:10px !important;color:#72543b !important;font-weight:800 !important;}
.stTabs [aria-selected="true"]{background:#fffaf1 !important;color:#9a692b !important;box-shadow:0 5px 15px rgba(78,54,35,.08);}
hr{border-color:rgba(111,75,40,.10) !important;}
@keyframes sePulse{0%,100%{transform:scale(1);opacity:.9}50%{transform:scale(1.22);opacity:.62}}
@keyframes seRise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.valuation-hero-v2,.valuation-panel-v2,.se-topbar{animation:seRise .42s ease both;}
@media(max-width:900px){.se-metric-row{grid-template-columns:repeat(2,minmax(0,1fr));}.se-top-sub{display:none;}.block-container{padding-left:1rem !important;padding-right:1rem !important;}}
@media(max-width:520px){.se-metric-row{grid-template-columns:1fr 1fr;gap:8px;}.se-metric{padding:11px;}.se-metric-value{font-size:17px;}}
</style>
""", unsafe_allow_html=True)

# ==========================================
# SIDEBAR NAVIGATION
# ==========================================
# Apply deferred navigation BEFORE the main_nav radio widget is created.
if st.session_state.pending_nav in PAGES.values():
    st.session_state.main_nav = st.session_state.pending_nav
    st.session_state.pending_nav = None

with st.sidebar:
    st.markdown(f"""
    <div class="sidebar-logo neo-brand">
        <div class="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 48 48" role="img">
                <path d="M8 22.5 24 9l16 13.5"/>
                <path d="M12 20v19h24V20"/>
                <path d="M19 39V27h10v12"/>
                <path d="m35 8 5 5"/>
            </svg>
        </div>
        <div class="brand-copy">
            <h2>Smart<span>Estate</span></h2>
            <p>{t("app_sub")}</p>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    page_labels = {
        "prediction": t("nav_prediction"),
        "budget": t("nav_budget"),
        "compare": t("nav_compare"),
        "analytics": t("nav_analytics"),
        "favorites": t("nav_favorites"),
        "saved_searches": t("nav_saved_searches"),
    }
    if st.session_state.get("auth_user"):
        page_labels["account"] = t("nav_account")
    # Keep navigation stable across language changes.
    stored_nav = st.session_state.get("main_nav")
    nav_key = next((k for k, v in PAGES.items() if stored_nav == v), None)
    if nav_key is None:
        nav_key = next((k for k, v in page_labels.items() if stored_nav == v), None)
    # After logout, the Account page is removed from page_labels.
    # Never allow a stale "account" navigation value to cause KeyError.
    if nav_key is None or nav_key not in page_labels:
        nav_key = "prediction"
    st.session_state.main_nav = page_labels[nav_key]

    if st.session_state.pending_nav is not None:
        pending_key = next((k for k, v in PAGES.items() if st.session_state.pending_nav == v), None)
        if pending_key is not None:
            st.session_state.main_nav = page_labels[pending_key]
        st.session_state.pending_nav = None

    st.radio(
        t("navigation"),
        options=list(page_labels.values()),
        label_visibility="collapsed",
        key="main_nav",
        on_change=reset_views
    )

    current_page = next((k for k, v in page_labels.items() if v == st.session_state.main_nav), "prediction")

    # ===================== ACCOUNT =====================
    auth_user = st.session_state.get("auth_user") or {}
    if auth_user:
        auth_name = html.escape(str(auth_user.get("name", "User")))
        auth_email = html.escape(str(auth_user.get("email", "")))
        st.markdown(f"""
        <div class="account-card">
          <div class="account-avatar">{auth_name[:1].upper()}</div>
          <div class="account-copy">
            <div class="account-name">{auth_name}</div>
            <div class="account-email">{auth_email}</div>
          </div>
        </div>
        """, unsafe_allow_html=True)
        ac1, ac2 = st.columns(2, gap="small")
        with ac1:
            if st.button(t("nav_account"), use_container_width=True, key="my_account_btn"):
                st.session_state.pending_nav = PAGES["account"]
                st.rerun()
        with ac2:
            if st.button("↪ Logout", use_container_width=True, key="logout_btn"):
                _revoke_current_login_session()
                _clear_account_data_from_session()

                # IMPORTANT: main_nav is already instantiated above.
                # Never modify it directly in this button callback.
                # Defer navigation to the next rerun, where it is applied
                # before the main_nav widget is created.
                st.session_state.auth_user = None
                st.session_state.auth_modal_open = False
                st.session_state.pending_nav = PAGES["prediction"]

                st.rerun()
    else:
        st.markdown("""
        <div class="account-card">
          <div class="account-avatar">G</div>
          <div class="account-copy">
            <div class="account-name">Guest Mode</div>
            <div class="account-email">Browse freely • Login only to save/download</div>
          </div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("🔐 Login / Sign Up", use_container_width=True, key="guest_login_btn"):
            _open_auth_prompt("save properties and download PDFs")
            st.rerun()

    # ===================== SMART AI PANEL =====================
    st.markdown("""
    <div class="side-section-title">
        <span class="side-section-kicker">AI COMMAND CENTER</span>
        <span class="side-live"><span class="live-dot"></span> LIVE</span>
    </div>
    """, unsafe_allow_html=True)

    ai_ok, ai_status = get_gemini_status()
    status_class = "online" if ai_ok else "offline"
    status_text = "Gemini AI Ready" if ai_ok else "Gemini AI Offline"
    status_sub = "Property, finance and general questions." if ai_ok else "Connect Gemini to activate AI."

    st.markdown(f"""
    <div class="ai-command-card">
        <div class="ai-command-top">
            <div class="ai-avatar-wrap">
                <div class="ai-avatar"><span class="material-symbols-rounded">smart_toy</span></div>
                <span class="ai-avatar-ring"></span>
            </div>
            <div class="ai-command-copy">
                <div class="ai-command-title">Ask AI Assistant</div>
                <div class="ai-command-sub">Real-estate intelligence, instantly.</div>
            </div>
        </div>
        <div class="ai-status-pill {status_class}">
            <span class="live-dot"></span>
            <span>{html.escape(status_text)}</span>
            <span class="ai-status-mini">{html.escape(status_sub)}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    def _clear_ai_chat():
        st.session_state.chat_msgs = [{"role": "bot", "content": t("chat_welcome")}]
        st.session_state.ai_last_error = ""

    chat_box = st.empty()

    def _render_ai_chat(target):
        chat_html = ['<div class="ai-chat-panel">']
        for msg in st.session_state.chat_msgs[-10:]:
            role = msg.get("role", "bot")
            content = html.escape(str(msg.get("content", ""))).replace("\n", "<br>")
            if role == "user":
                chat_html.append(
                    f'<div class="ai-chat-message user"><div class="ai-chat-avatar user"><span class="material-symbols-rounded">person</span></div><div class="ai-chat-bubble user">{content}</div></div>'
                )
            else:
                chat_html.append(
                    f'<div class="ai-chat-message bot"><div class="ai-chat-avatar bot"><span class="material-symbols-rounded">smart_toy</span></div><div class="ai-chat-bubble bot">{content}</div></div>'
                )
        chat_html.append('</div>')
        target.markdown("".join(chat_html), unsafe_allow_html=True)

    _render_ai_chat(chat_box)

    with st.form("ask_ai_form", clear_on_submit=True, enter_to_submit=True):
        bot_q = st.text_input(
            "Ask anything",
            placeholder="Ask about price, EMI, locality, BHK…",
            label_visibility="collapsed",
            key="ai_question",
        )
        ask_clicked = st.form_submit_button("✨ Ask AI", use_container_width=True, type="primary")

    aib1, aib2 = st.columns([1.35, 1])
    with aib1:
        st.caption("↵ Enter to send")
    with aib2:
        clear_clicked = st.button("⌫ Clear", use_container_width=True, key="ai_clear_btn")

    if clear_clicked:
        _clear_ai_chat()

    if ask_clicked:
        question = (bot_q or "").strip()
        if question:
            st.session_state.chat_msgs.append({"role": "user", "content": question})
            temp_df = load_dataset()
            with st.spinner("🤖 Gemini is thinking..."):
                ans = get_smart_ai_response(question, temp_df)
            st.session_state.chat_msgs.append({"role": "bot", "content": ans})
            st.session_state.ai_last_error = ""
        else:
            st.session_state.ai_last_error = "Please type a question first."

    if st.session_state.get("ai_last_error"):
        st.markdown(
            f'<div class="ai-error-chip">⚠ {html.escape(st.session_state["ai_last_error"])}</div>',
            unsafe_allow_html=True,
        )

    _render_ai_chat(chat_box)

    # ===== Appearance + Language controls restored =====
    st.markdown("""
    <div class="sidebar-tools-title">
      <span>APP CONTROLS</span>
      <span class="sidebar-tools-line"></span>
    </div>
    """, unsafe_allow_html=True)

    def _apply_dark_mode():
        st.session_state.dark_mode = bool(st.session_state.get("dark_mode_ui", False))

    def _apply_language():
        lang_value = st.session_state.get("language_ui", "English")
        st.session_state.language = lang_value
        # Keep current page selected; its translated label is rebuilt on rerun.

    ctl1, ctl2 = st.columns(2, gap="small")
    with ctl1:
        st.toggle(
            "🌙 " + ("Dark" if st.session_state.get("language", "English") == "English" else "Dark / " + st.session_state.get("language", "English")),
            key="dark_mode_ui",
            on_change=_apply_dark_mode,
        )
    with ctl2:
        lang_options = ["English", "Hindi", "Gujarati"]
        lang_index = lang_options.index(st.session_state.get("language", "English")) if st.session_state.get("language", "English") in lang_options else 0
        st.selectbox(
            t("language"),
            lang_options,
            index=lang_index,
            key="language_ui",
            on_change=_apply_language,
            label_visibility="collapsed",
        )

    # Currency control stays available beside the language control.
    currency_options = ["Lakhs", "Crores", "Full"]
    cur_index = currency_options.index(st.session_state.get("currency_mode", "Lakhs")) if st.session_state.get("currency_mode", "Lakhs") in currency_options else 0
    st.selectbox(
        t("currency_mode"),
        currency_options,
        index=cur_index,
        key="currency_mode_ui",
        label_visibility="collapsed",
    )
    st.session_state.currency_mode = st.session_state.currency_mode_ui

    st.divider()
    st.markdown("""
    <div class="animate-up delay-3" style="text-align:center; padding: 10px; color:#94a3b8; font-size:12px;">
        System Engineered & Developed by<br>
        <b style="color:#daa752; font-size:14px; letter-spacing:0.5px;">Darji Shailesh</b>
    </div>
    """, unsafe_allow_html=True)

# ==========================================

# ==========================================
# ✦ V13 SIDEBAR — EXECUTIVE AI DOCK
# ==========================================
st.markdown(f"""
<style>
[data-testid="stSidebar"] {{
    width:320px !important; min-width:320px !important;
    background:
      radial-gradient(circle at 14% 8%, rgba(124,58,237,.18), transparent 24%),
      radial-gradient(circle at 86% 38%, rgba(34,211,238,.10), transparent 25%),
      linear-gradient(180deg, {sidebar_bg}, {sidebar_bg}) !important;
    border-right:1px solid rgba(124,58,237,.16) !important;
    box-shadow:14px 0 55px rgba(2,6,23,.10) !important;
}}
[data-testid="stSidebar"] > div:first-child {{ width:320px !important; }}
[data-testid="stSidebarContent"] {{ padding:14px 14px 28px !important; }}
[data-testid="stSidebarUserContent"] {{ padding:0 2px 30px !important; }}
.sidebar-logo.neo-brand {{ margin:3px 2px 14px !important; padding:10px 8px 15px !important; border-bottom:1px solid rgba(148,163,184,.13) !important; }}
.sidebar-logo.neo-brand .brand-mark {{ width:48px !important; height:48px !important; border-radius:16px !important; background:linear-gradient(145deg,#B87925,#D9A756) !important; box-shadow:0 16px 34px rgba(99,102,241,.30), inset 0 1px 0 rgba(255,255,255,.45) !important; }}
.sidebar-logo.neo-brand .brand-copy h2 {{ font-size:22px !important; font-weight:900 !important; letter-spacing:-.8px !important; }}
.sidebar-logo.neo-brand .brand-copy p {{ font-size:9px !important; letter-spacing:1.45px !important; opacity:.65 !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] {{ gap:6px !important; margin-top:4px !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label {{ min-height:49px !important; padding:9px 12px !important; border-radius:15px !important; margin:0 !important; border:1px solid transparent !important; background:rgba(255,255,255,.025) !important; transition:transform .25s ease, background .25s ease, border-color .25s ease, box-shadow .25s ease !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {{ transform:translateX(4px) !important; background:linear-gradient(90deg, rgba(124,58,237,.12), rgba(34,211,238,.05)) !important; border-color:rgba(124,58,237,.18) !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] {{ transform:translateX(3px) !important; background:linear-gradient(100deg, rgba(124,58,237,.19), rgba(34,211,238,.07)) !important; border-color:rgba(124,58,237,.24) !important; box-shadow:inset 3px 0 0 #C9974A, 0 10px 28px rgba(76,29,149,.10) !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] p {{ font-size:13px !important; font-weight:750 !important; letter-spacing:-.05px !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] p {{ color:#c4b5fd !important; }}
.side-section-title {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin:16px 4px 9px; color:{sub_text}; }}
.side-section-kicker {{ font-size:9px; font-weight:900; letter-spacing:1.65px; color:{sub_text}; }}
.side-live {{ display:inline-flex; align-items:center; gap:6px; font-size:8px; letter-spacing:1px; font-weight:900; color:#F2D99B; }}
.live-dot {{ width:7px; height:7px; border-radius:50%; background:#E6C27A; box-shadow:0 0 0 4px rgba(34,211,238,.08), 0 0 14px rgba(34,211,238,.40); animation:sidePulse 2s ease-in-out infinite; display:inline-block; }}
@keyframes sidePulse {{ 0%,100%{{transform:scale(.92);opacity:.75}} 50%{{transform:scale(1.14);opacity:1}} }}
.ai-command-card {{ padding:14px; border-radius:20px; background:linear-gradient(145deg, rgba(124,58,237,.13), rgba(34,211,238,.055) 58%, rgba(255,255,255,.025)); border:1px solid rgba(124,58,237,.20); box-shadow:0 18px 46px rgba(30,41,59,.12), inset 0 1px 0 rgba(255,255,255,.08); position:relative; overflow:hidden; }}
.ai-command-card::after {{ content:""; position:absolute; width:110px; height:110px; right:-42px; top:-42px; border-radius:50%; background:radial-gradient(circle, rgba(34,211,238,.22), transparent 68%); pointer-events:none; }}
.ai-command-top {{ display:flex; align-items:center; gap:11px; position:relative; z-index:1; }}
.ai-avatar-wrap {{ position:relative; width:44px; height:44px; flex:0 0 44px; }}
.ai-avatar {{ width:44px; height:44px; border-radius:14px; display:grid; place-items:center; color:#fff; background:linear-gradient(135deg,#B87925,#0891b2); box-shadow:0 12px 28px rgba(79,70,229,.28); }}
.ai-avatar .material-symbols-rounded {{ font-size:23px; }}
.ai-avatar-ring {{ position:absolute; inset:-6px; border-radius:18px; border:1px solid rgba(103,232,249,.18); animation:aiRing 2.8s ease-out infinite; pointer-events:none; }}
@keyframes aiRing {{ 0%{{transform:scale(.88);opacity:.8}} 75%,100%{{transform:scale(1.22);opacity:0}} }}
.ai-command-title {{ font-size:14px; font-weight:900; letter-spacing:-.2px; color:{text_col}; }}
.ai-command-sub {{ margin-top:3px; font-size:9px; color:{sub_text}; font-weight:700; }}
.ai-status-pill {{ margin-top:11px; padding:8px 9px; border-radius:12px; display:flex; align-items:center; gap:7px; border:1px solid rgba(34,211,238,.14); background:rgba(34,211,238,.05); font-size:9px; font-weight:800; color:{text_col}; }}
.ai-status-pill.offline {{ border-color:rgba(245,158,11,.18); background:rgba(245,158,11,.06); }}
.ai-status-pill.offline .live-dot {{ background:#f59e0b; box-shadow:0 0 0 4px rgba(245,158,11,.08); }}
.ai-status-mini {{ color:{sub_text}; font-weight:600; margin-left:auto; text-align:right; }}
.ai-chat-panel {{ max-height:250px; overflow-y:auto; margin:10px 0 9px; padding:8px 3px; scroll-behavior:smooth; }}
.ai-chat-panel::-webkit-scrollbar {{ width:5px; }}
.ai-chat-panel::-webkit-scrollbar-thumb {{ background:linear-gradient(#B87925,#E6C27A); border-radius:99px; }}
.ai-chat-message {{ display:flex; align-items:flex-start; gap:7px; margin:8px 0; animation:chatIn .32s ease both; }}
.ai-chat-message.user {{ justify-content:flex-end; }}
.ai-chat-message.user .ai-chat-avatar {{ order:2; }}
.ai-chat-message.user .ai-chat-bubble {{ order:1; }}
.ai-chat-avatar {{ width:24px; height:24px; border-radius:8px; display:grid; place-items:center; flex:0 0 24px; margin-top:2px; }}
.ai-chat-avatar .material-symbols-rounded {{ font-size:15px; }}
.ai-chat-avatar.bot {{ color:#fff; background:linear-gradient(135deg,#B87925,#0891b2); }}
.ai-chat-avatar.user {{ color:{text_col}; background:rgba(148,163,184,.13); border:1px solid rgba(148,163,184,.12); }}
.ai-chat-bubble {{ max-width:82%; padding:9px 11px; border-radius:13px; font-size:11px; line-height:1.48; font-weight:650; word-break:break-word; overflow-wrap:anywhere; }}
.ai-chat-bubble.bot {{ color:{text_col} !important; background:rgba(255,255,255,.045); border:1px solid rgba(148,163,184,.10); border-top-left-radius:5px; }}
.ai-chat-bubble.user {{ color:#fff !important; background:linear-gradient(135deg,#B87925,#0891b2); border-bottom-right-radius:5px; box-shadow:0 8px 20px rgba(79,70,229,.20); }}
@keyframes chatIn {{ from{{opacity:0; transform:translateY(5px)}} to{{opacity:1; transform:none}} }}
[data-testid="stSidebar"] .stTextInput input {{ min-height:43px !important; border-radius:13px !important; background:rgba(255,255,255,.045) !important; border:1px solid rgba(148,163,184,.13) !important; font-size:11px !important; padding:0 12px !important; }}
[data-testid="stSidebar"] .stTextInput input:focus {{ border-color:rgba(124,58,237,.55) !important; box-shadow:0 0 0 3px rgba(124,58,237,.10) !important; }}
[data-testid="stSidebar"] .stFormSubmitButton button {{ min-height:42px !important; border-radius:12px !important; font-size:11px !important; font-weight:900 !important; background:linear-gradient(100deg,#B87925,#D9A756) !important; border:0 !important; box-shadow:0 12px 24px rgba(79,70,229,.20) !important; }}
[data-testid="stSidebar"] .stButton > button {{ min-height:40px !important; border-radius:12px !important; font-size:10px !important; font-weight:800 !important; background:rgba(255,255,255,.04) !important; border:1px solid rgba(148,163,184,.12) !important; box-shadow:none !important; }}
[data-testid="stSidebar"] .stButton > button:hover {{ transform:translateY(-1px) !important; border-color:rgba(34,211,238,.25) !important; background:rgba(34,211,238,.06) !important; }}
.ai-error-chip {{ margin-top:7px; padding:8px 10px; border-radius:11px; font-size:10px; font-weight:700; color:#fecaca; background:rgba(239,68,68,.08); border:1px solid rgba(239,68,68,.15); }}
[data-testid="stSidebar"] .stSelectbox > div > div {{ border-radius:12px !important; min-height:40px !important; }}
[data-testid="stSidebar"] .stCaption {{ font-size:9px !important; color:{sub_text} !important; }}
[data-testid="stSidebar"] hr {{ margin:12px 0 !important; border-color:rgba(148,163,184,.10) !important; }}
@media (max-width:1100px) {{ [data-testid="stSidebar"], [data-testid="stSidebar"] > div:first-child {{ width:290px !important; min-width:290px !important; }} }}
@media (prefers-reduced-motion:reduce) {{ .ai-avatar-ring,.live-dot,.ai-chat-message {{ animation:none !important; }} }}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
/* ===== V10: WIDE PREMIUM APP LAYOUT ===== */

[data-testid="stAppViewContainer"] {
    overflow-x: hidden;
}

[data-testid="stSidebar"] {
    width: 280px !important;
    min-width: 280px !important;
}

[data-testid="stSidebar"] > div:first-child {
    width: 280px !important;
}

.block-container {
    max-width: 1520px !important;
    width: calc(100% - 28px) !important;
    padding-top: 1.2rem !important;
    padding-left: 18px !important;
    padding-right: 18px !important;
    padding-bottom: 2rem !important;
    margin-left: auto !important;
    margin-right: auto !important;
}

/* Use almost the full desktop width */
@media (min-width: 1200px) {
    .block-container {
        max-width: 1520px !important;
        width: calc(100% - 24px) !important;
    }
}

/* Extra-wide monitors */
@media (min-width: 1700px) {
    .block-container {
        max-width: 1650px !important;
        width: calc(100% - 30px) !important;
    }
}

/* Laptop/tablet */
@media (max-width: 1100px) {
    [data-testid="stSidebar"] {
        width: 250px !important;
        min-width: 250px !important;
    }

    [data-testid="stSidebar"] > div:first-child {
        width: 250px !important;
    }

    .block-container {
        width: calc(100% - 18px) !important;
        padding-left: 10px !important;
        padding-right: 10px !important;
    }
}

/* Mobile */
@media (max-width: 700px) {
    .block-container {
        width: 100% !important;
        padding-left: 8px !important;
        padding-right: 8px !important;
    }
}
</style>
""", unsafe_allow_html=True)



# ==========================================
# ✦ V11 PREMIUM REAL ESTATE DESIGN SYSTEM
# ==========================================
st.markdown(f"""
<style>
/* ---------- Global shell ---------- */
:root {{
    --brand:#daa752;
    --brand-2:#f0cf8a;
    --ink:{text_col};
    --muted:{sub_text};
    --line:{border_col};
    --panel:{card_bg};
}}

[data-testid="stAppViewContainer"] {{
    background:
        radial-gradient(circle at 10% 5%, rgba(218,167,82,.10), transparent 24%),
        radial-gradient(circle at 88% 12%, rgba(59,130,246,.07), transparent 22%),
        {app_bg};
}}

.block-container {{
    max-width: 1540px !important;
    padding-top: 1.05rem !important;
    padding-bottom: 3rem !important;
}}

/* ---------- Sidebar ---------- */
[data-testid="stSidebar"] {{
    background:
        radial-gradient(circle at 20% 0%, rgba(218,167,82,.12), transparent 24%),
        linear-gradient(180deg, {sidebar_bg}, {sidebar_bg});
    border-right: 1px solid rgba(220,176,101,.16) !important;
    box-shadow: 16px 0 45px rgba(0,0,0,.08);
}}
[data-testid="stSidebar"] > div:first-child {{
    padding-top: 1rem !important;
}}
.sidebar-logo {{
    padding: 8px 8px 18px !important;
    margin-bottom: 10px;
    border-bottom: 1px solid rgba(220,176,101,.15);
}}
.sidebar-logo h2 {{
    font-size: 23px !important;
    letter-spacing: -.4px;
}}
.sidebar-logo p {{
    color: var(--muted) !important;
    opacity: .9;
    letter-spacing: 1.2px !important;
}}
[data-testid="stSidebar"] .stDivider {{
    border-color: rgba(218,167,82,.12) !important;
}}

/* ---------- Navigation ---------- */
[data-testid="stSidebar"] div[role="radiogroup"] {{
    gap: 7px !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label {{
    min-height: 47px;
    padding: 11px 13px !important;
    margin-bottom: 0 !important;
    border-radius: 14px !important;
    background: transparent !important;
    border: 1px solid transparent !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {{
    background: rgba(220,176,101,.08) !important;
    border-color: rgba(220,176,101,.16) !important;
    transform: translateX(3px) !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] {{
    background: linear-gradient(90deg, rgba(220,176,101,.17), rgba(220,176,101,.05)) !important;
    border: 1px solid rgba(220,176,101,.24) !important;
    box-shadow: inset 4px 0 0 var(--brand), 0 8px 25px rgba(220,176,101,.07) !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] p {{
    color: var(--brand-2) !important;
}}

/* ---------- Top command bar ---------- */
.premium-commandbar {{
    position:relative;
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:16px;
    padding:13px 17px;
    margin:0 0 18px;
    border:1px solid rgba(220,176,101,.16);
    border-radius:18px;
    background:linear-gradient(135deg, rgba(255,255,255,.68), rgba(255,255,255,.30));
    backdrop-filter:blur(18px);
    box-shadow:0 16px 45px rgba(15,23,42,.06);
    overflow:hidden;
}}
.premium-commandbar::after {{
    content:"";
    position:absolute;
    inset:auto -10% -55% auto;
    width:260px;
    height:120px;
    background:radial-gradient(circle, rgba(220,176,101,.16), transparent 70%);
    pointer-events:none;
}}
.premium-commandbar-title {{
    font-size:13px;
    font-weight:800;
    letter-spacing:.7px;
    color:var(--ink);
}}
.premium-commandbar-title span {{
    color:var(--brand);
}}
.premium-commandbar-meta {{
    display:flex;
    align-items:center;
    gap:8px;
    color:var(--muted);
    font-size:11px;
    font-weight:700;
}}
.premium-chip {{
    display:inline-flex;
    align-items:center;
    gap:6px;
    padding:6px 9px;
    border-radius:999px;
    background:rgba(16,185,129,.10);
    border:1px solid rgba(16,185,129,.15);
    color:#059669;
}}
.premium-dot {{
    width:7px;height:7px;border-radius:999px;background:#10b981;
    box-shadow:0 0 0 5px rgba(16,185,129,.09);
}}

/* ---------- Hero ---------- */
.hero-banner {{
    min-height:265px !important;
    padding:48px 42px !important;
    border-radius:28px !important;
    margin-bottom:22px !important;
    border:1px solid rgba(255,255,255,.16);
    box-shadow:0 22px 55px rgba(15,23,42,.12) !important;
}}
.hero-banner::before {{
    background:linear-gradient(90deg, rgba(2,6,23,.83) 0%, rgba(2,6,23,.56) 45%, rgba(2,6,23,.08) 100%) !important;
}}
.hero-content {{ max-width:760px !important; }}
.hero-eyebrow {{
    font-size:11px !important;
    letter-spacing:2.6px !important;
    margin-bottom:8px !important;
}}
.hero-title {{
    font-size:clamp(38px, 4.3vw, 64px) !important;
    line-height:1.02 !important;
    letter-spacing:-1.8px !important;
}}
.hero-sub {{
    max-width:660px;
    font-size:15px !important;
    line-height:1.65 !important;
}}

/* ---------- Cards / panels ---------- */
.intel-box, .metric-card, .val-box, .compare-card, .poi-card {{
    border-radius:20px !important;
    border:1px solid rgba(148,163,184,.16) !important;
    box-shadow:0 12px 35px rgba(15,23,42,.055) !important;
}}
.intel-box {{
    padding:23px !important;
    background:linear-gradient(145deg, rgba(255,255,255,.78), rgba(255,255,255,.43)) !important;
}}
.intel-box:hover, .metric-card:hover, .compare-card:hover {{
    transform:translateY(-4px) !important;
    border-color:rgba(220,176,101,.28) !important;
    box-shadow:0 20px 44px rgba(15,23,42,.09), 0 0 0 1px rgba(220,176,101,.05) !important;
}}
.card-header {{
    font-size:17px !important;
    letter-spacing:-.15px;
}}
.metric-card {{
    padding:21px 16px !important;
    min-height:125px;
}}
.metric-card .num {{ font-size:31px !important; }}
.metric-card .lbl {{ letter-spacing:1.1px !important; }}
.val-box {{
    padding:28px !important;
    border-radius:24px !important;
    background:linear-gradient(145deg, rgba(220,176,101,.15), rgba(255,255,255,.55)) !important;
}}
.val-price {{
    font-size:clamp(38px, 4vw, 58px) !important;
    letter-spacing:-2px !important;
}}

/* ---------- Inputs ---------- */
.stTextInput input, .stNumberInput input, textarea,
.stSelectbox div[data-baseweb="select"] > div,
.stMultiSelect div[data-baseweb="select"] > div {{
    min-height:46px !important;
    border-radius:13px !important;
    background:rgba(255,255,255,.68) !important;
    border:1px solid rgba(148,163,184,.18) !important;
}}
.stTextInput input::placeholder, textarea::placeholder {{ opacity:.52; }}
[data-baseweb="select"] * {{ font-size:13px !important; }}

/* ---------- Buttons ---------- */
.stButton > button, .stFormSubmitButton > button {{
    min-height:48px !important;
    border-radius:14px !important;
    border:1px solid rgba(220,176,101,.18) !important;
    background:linear-gradient(135deg, #daa752, #c7963d) !important;
    box-shadow:0 10px 25px rgba(220,176,101,.23) !important;
    font-size:14px !important;
    letter-spacing:.1px;
}}
.stButton > button:hover, .stFormSubmitButton > button:hover {{
    transform:translateY(-2px) !important;
    box-shadow:0 15px 32px rgba(220,176,101,.30) !important;
}}
.stButton > button[kind="secondary"], .stFormSubmitButton > button[kind="secondary"] {{
    background:rgba(255,255,255,.65) !important;
    color:var(--ink) !important;
    box-shadow:none !important;
}}

/* ---------- Tabs ---------- */
.stTabs [data-baseweb="tab-list"] {{
    gap:7px;
    padding:5px;
    border-radius:16px;
    background:rgba(148,163,184,.075);
}}
.stTabs [data-baseweb="tab"] {{
    min-height:42px;
    padding:0 15px;
    border-radius:12px;
    font-weight:700;
    color:var(--muted);
}}
.stTabs [aria-selected="true"] {{
    background:rgba(220,176,101,.16) !important;
    color:var(--ink) !important;
}}
.stTabs [data-baseweb="tab-highlight"] {{ display:none; }}

/* ---------- Expanders / alerts ---------- */
div[data-testid="stExpander"] {{
    border:1px solid rgba(148,163,184,.15) !important;
    border-radius:16px !important;
    overflow:hidden;
    background:rgba(255,255,255,.42);
}}
div[data-testid="stAlert"] {{ border-radius:15px !important; }}

/* ---------- Dataframes ---------- */
[data-testid="stDataFrame"] {{
    border-radius:16px !important;
    overflow:hidden !important;
    border:1px solid rgba(148,163,184,.14) !important;
}}

/* ---------- Premium chat ---------- */
.ai-chat-stack {{
    display:flex;
    flex-direction:column;
    gap:10px;
    max-height:330px;
    overflow-y:auto;
    padding:4px 2px 9px;
    scroll-behavior:smooth;
}}
.ai-chat-row {{ display:flex; width:100%; }}
.ai-chat-row.user {{ justify-content:flex-end; }}
.ai-chat-row.bot {{ justify-content:flex-start; }}
.ai-chat-bubble {{
    max-width:86%;
    padding:11px 13px;
    border-radius:16px;
    font-size:12px;
    line-height:1.5;
    box-shadow:0 8px 20px rgba(15,23,42,.05);
}}
.ai-chat-bubble.user {{
    background:linear-gradient(135deg, #daa752, #bd8b3c);
    color:#fff;
    border-bottom-right-radius:6px;
}}
.ai-chat-bubble.bot {{
    background:rgba(148,163,184,.10);
    color:var(--ink);
    border:1px solid rgba(148,163,184,.12);
    border-bottom-left-radius:6px;
}}
.ai-engine-status {{
    display:flex;
    align-items:center;
    gap:8px;
    padding:8px 10px;
    margin-bottom:8px;
    border-radius:12px;
    background:rgba(16,185,129,.07);
    border:1px solid rgba(16,185,129,.10);
    font-size:11px;
    font-weight:700;
    color:#059669;
}}
.ai-status-dot {{ width:7px;height:7px;border-radius:999px;background:#10b981;box-shadow:0 0 0 5px rgba(16,185,129,.10); }}

/* ---------- Small property cards / summary cards ---------- */
.property-summary-card {{
    position:relative;
    padding:17px;
    border-radius:18px;
    border:1px solid rgba(148,163,184,.15);
    background:linear-gradient(145deg, rgba(255,255,255,.78), rgba(255,255,255,.46));
    box-shadow:0 12px 32px rgba(15,23,42,.05);
    overflow:hidden;
}}
.property-summary-card::before {{
    content:"";
    position:absolute;
    inset:0 auto 0 0;
    width:3px;
    background:linear-gradient(180deg, #daa752, #8b5e1f);
}}
.score-pill {{
    display:inline-flex;
    align-items:center;
    gap:5px;
    padding:5px 9px;
    border-radius:999px;
    background:rgba(16,185,129,.09);
    border:1px solid rgba(16,185,129,.12);
    color:#059669;
    font-size:11px;
    font-weight:800;
}}

/* ---------- Mobile ---------- */
@media (max-width: 900px) {{
    .premium-commandbar {{ flex-direction:column; align-items:flex-start; }}
    .hero-banner {{ padding:34px 25px !important; min-height:235px !important; }}
    .hero-title {{ font-size:40px !important; }}
    .stTabs [data-baseweb="tab"] {{ padding:0 9px; font-size:11px; }}
}}
@media (max-width: 700px) {{
    .block-container {{ padding-left:8px !important; padding-right:8px !important; }}
    .hero-title {{ font-size:33px !important; }}
    .hero-sub {{ font-size:13px !important; }}
    .premium-commandbar-meta {{ flex-wrap:wrap; }}
}}

/* Respect reduced-motion preference */
@media (prefers-reduced-motion: reduce) {{
    *,*::before,*::after {{ animation:none !important; transition:none !important; }}
}}
</style>
""", unsafe_allow_html=True)

# ==========================================
# ✦ V12 NEO-LUXURY MOTION + ICON DESIGN LAYER
# ==========================================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,500,0,0');

:root{
  --neo-violet:#B87925;
  --neo-violet-2:#E6C27A;
  --neo-cyan:#E6C27A;
  --neo-cyan-2:#F2D99B;
  --neo-amber:#E6C27A;
  --neo-ink:#3B2A1E;
}

.material-symbols-rounded{
  font-family:'Material Symbols Rounded';
  font-weight:500;
  font-style:normal;
  font-size:20px;
  line-height:1;
  letter-spacing:normal;
  text-transform:none;
  display:inline-block;
  white-space:nowrap;
  word-wrap:normal;
  direction:ltr;
  -webkit-font-feature-settings:'liga';
  -webkit-font-smoothing:antialiased;
  font-feature-settings:'liga';
}

/* Ambient animated light field */
[data-testid="stAppViewContainer"]{
  position:relative;
  isolation:isolate;
  font-family:'Manrope','Inter',sans-serif !important;
}
[data-testid="stAppViewContainer"]::before,
[data-testid="stAppViewContainer"]::after{
  content:"";
  position:fixed;
  inset:auto;
  width:460px;
  height:460px;
  border-radius:50%;
  pointer-events:none;
  z-index:-1;
  filter:blur(12px);
  opacity:.32;
  mix-blend-mode:screen;
}
[data-testid="stAppViewContainer"]::before{
  top:4vh; left:-160px;
  background:radial-gradient(circle, rgba(124,58,237,.42), transparent 65%);
  animation:neoFloatA 13s ease-in-out infinite;
}
[data-testid="stAppViewContainer"]::after{
  right:-180px; bottom:-120px;
  background:radial-gradient(circle, rgba(34,211,238,.32), transparent 64%);
  animation:neoFloatB 16s ease-in-out infinite;
}

/* Subtle animated grid */
.main .block-container{
  position:relative;
}
.main .block-container::before{
  content:"";
  position:fixed;
  inset:0;
  pointer-events:none;
  z-index:-1;
  opacity:.20;
  background-image:linear-gradient(rgba(124,58,237,.06) 1px,transparent 1px),linear-gradient(90deg,rgba(34,211,238,.05) 1px,transparent 1px);
  background-size:54px 54px;
  mask-image:linear-gradient(to bottom,black,transparent 78%);
}

@keyframes neoFloatA{0%,100%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(120px,55px,0) scale(1.12)}}
@keyframes neoFloatB{0%,100%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(-100px,-70px,0) scale(1.10)}}
@keyframes neoShimmer{0%{transform:translateX(-140%) skewX(-18deg);opacity:0}15%{opacity:.55}55%{opacity:.08}100%{transform:translateX(180%) skewX(-18deg);opacity:0}}
@keyframes neoPulse{0%,100%{box-shadow:0 0 0 0 rgba(34,211,238,.0)}50%{box-shadow:0 0 0 9px rgba(34,211,238,.06)}}
@keyframes neoRing{0%{transform:scale(.86);opacity:.7}70%{transform:scale(1.28);opacity:0}100%{opacity:0}}
@keyframes neoRise{0%{opacity:0;transform:translateY(18px) scale(.985)}100%{opacity:1;transform:translateY(0) scale(1)}}

/* Top command bar */
.neo-topbar{
  min-height:70px !important;
  padding:11px 15px !important;
  border:1px solid rgba(124,58,237,.18) !important;
  border-radius:22px !important;
  background:
    linear-gradient(135deg,rgba(255,255,255,.82),rgba(255,255,255,.42)),
    radial-gradient(circle at 5% 0%,rgba(167,139,250,.13),transparent 38%),
    radial-gradient(circle at 100% 100%,rgba(34,211,238,.12),transparent 36%) !important;
  box-shadow:0 18px 55px rgba(15,23,42,.08),inset 0 1px 0 rgba(255,255,255,.75) !important;
  backdrop-filter:blur(24px) saturate(150%) !important;
}
.neo-topbar::before{
  content:"";
  position:absolute;inset:0 auto 0 -34%;width:22%;
  background:linear-gradient(100deg,transparent,rgba(255,255,255,.48),transparent);
  transform:skewX(-18deg);
  animation:neoShimmer 6.8s ease-in-out infinite;
  pointer-events:none;
}
.neo-topbar-left{display:flex;align-items:center;gap:12px;position:relative;z-index:1}
.neo-topbar-mark,.brand-mark{
  width:44px;height:44px;border-radius:15px;display:grid;place-items:center;
  background:linear-gradient(145deg,#C9974A,#D9A756);
  box-shadow:0 12px 26px rgba(99,102,241,.28),inset 0 1px 0 rgba(255,255,255,.42);
  position:relative;overflow:hidden;
}
.neo-topbar-mark::after,.brand-mark::after{
  content:"";position:absolute;inset:-8px;border-radius:18px;border:1px solid rgba(255,255,255,.26);animation:neoRing 2.8s ease-out infinite;
}
.neo-topbar-mark svg,.brand-mark svg{width:24px;height:24px;fill:none;stroke:#fff;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;position:relative;z-index:1}
.neo-topbar-sub{font-size:10px;letter-spacing:.55px;text-transform:uppercase;color:var(--muted);font-weight:800;margin-top:2px}
.premium-commandbar-title{font-size:16px !important;letter-spacing:-.4px !important;font-weight:900 !important;display:flex;align-items:center;gap:8px}
.premium-commandbar-title i{width:7px;height:7px;border-radius:50%;background:linear-gradient(135deg,#E6C27A,#C9974A);box-shadow:0 0 0 5px rgba(34,211,238,.08),0 0 16px rgba(34,211,238,.28);animation:neoPulse 2.6s ease-in-out infinite}
.premium-commandbar-title span{color:var(--neo-violet) !important;background:linear-gradient(90deg,#B87925,#0891b2);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.neo-topbar-right{gap:8px !important;position:relative;z-index:1}
.neo-mini-pill{display:inline-flex;align-items:center;gap:5px;padding:7px 10px;border-radius:999px;background:rgba(15,23,42,.035);border:1px solid rgba(148,163,184,.16);font-size:10px;font-weight:800;color:var(--muted);white-space:nowrap}
.neo-mini-pill .material-symbols-rounded{font-size:15px;color:#B87925}

/* Sidebar brand */
.sidebar-logo.neo-brand{display:flex;align-items:center;gap:11px;padding:8px 8px 18px !important}
.sidebar-logo.neo-brand .brand-mark{width:43px;height:43px;border-radius:14px;flex:0 0 auto}
.sidebar-logo.neo-brand .brand-copy h2{font-size:21px !important;letter-spacing:-.7px !important;line-height:1;margin:0 !important}
.sidebar-logo.neo-brand .brand-copy h2 span{background:linear-gradient(90deg,#B87925,#0891b2);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sidebar-logo.neo-brand .brand-copy p{margin:5px 0 0 !important;font-size:9px !important;letter-spacing:1.1px !important}

/* Navigation gets a modern icon-rail feel without changing labels */
[data-testid="stSidebar"] div[role="radiogroup"] > label{
  position:relative !important;
  overflow:hidden;
  background:linear-gradient(90deg,rgba(255,255,255,.18),rgba(255,255,255,.03)) !important;
  box-shadow:inset 0 0 0 1px rgba(148,163,184,.06);
}
[data-testid="stSidebar"] div[role="radiogroup"] > label::after{
  content:"";position:absolute;top:0;bottom:0;left:-120%;width:70%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.16),transparent);
  transform:skewX(-18deg);transition:left .8s ease;
}
[data-testid="stSidebar"] div[role="radiogroup"] > label:hover::after{left:150%}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"]{
  box-shadow:inset 3px 0 0 #E6C27A,0 10px 24px rgba(99,102,241,.10) !important;
}

/* Hero: more cinematic, layered, and animated */
.hero-banner{
  min-height:300px !important;
  border-radius:32px !important;
  border:1px solid rgba(255,255,255,.16) !important;
  box-shadow:0 30px 80px rgba(15,23,42,.14),inset 0 1px 0 rgba(255,255,255,.20) !important;
  animation:neoRise .8s cubic-bezier(.16,1,.3,1) both;
}
.hero-banner::after{
  content:"";position:absolute;right:6%;top:13%;width:160px;height:160px;border-radius:50%;
  background:radial-gradient(circle,rgba(34,211,238,.20),rgba(124,58,237,.04) 55%,transparent 72%);
  filter:blur(1px);animation:neoFloatB 7s ease-in-out infinite;pointer-events:none;
}
.hero-eyebrow{display:inline-flex;align-items:center;gap:7px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);padding:7px 10px;border-radius:999px;backdrop-filter:blur(12px)}
.hero-eyebrow::before{content:"✦";color:#F2D99B;animation:neoPulse 2.4s ease-in-out infinite}
.gradient-text{background:linear-gradient(90deg,#f8fafc 0%,#F2D99B 46%,#c4b5fd 100%) !important;-webkit-background-clip:text !important;-webkit-text-fill-color:transparent !important}

/* Card choreography */
.intel-box,.metric-card,.val-box,.compare-card,.poi-card,.property-summary-card{
  position:relative;overflow:hidden;transform:translateZ(0);
  background:
    linear-gradient(145deg,rgba(255,255,255,.78),rgba(255,255,255,.40)),
    radial-gradient(circle at 100% 0%,rgba(124,58,237,.07),transparent 38%) !important;
  box-shadow:0 16px 42px rgba(15,23,42,.065),inset 0 1px 0 rgba(255,255,255,.75) !important;
}
.intel-box::after,.metric-card::after,.val-box::after,.compare-card::after,.poi-card::after,.property-summary-card::after{
  content:"";position:absolute;inset:0 auto 0 -140%;width:55%;
  background:linear-gradient(100deg,transparent,rgba(255,255,255,.24),transparent);
  transform:skewX(-16deg);pointer-events:none;transition:none;
}
.intel-box:hover::after,.metric-card:hover::after,.val-box:hover::after,.compare-card:hover::after,.poi-card:hover::after,.property-summary-card:hover::after{animation:neoShimmer 1.05s ease-out 1}
.intel-box:hover,.metric-card:hover,.compare-card:hover,.poi-card:hover,.property-summary-card:hover{
  transform:translateY(-7px) scale(1.008) !important;
  border-color:rgba(34,211,238,.26) !important;
  box-shadow:0 24px 55px rgba(79,70,229,.11),0 0 0 1px rgba(34,211,238,.04) !important;
}

/* Primary buttons: neon luxury */
.stButton > button,.stFormSubmitButton > button{
  position:relative;overflow:hidden;isolation:isolate;
  background:linear-gradient(135deg,#B87925 0%,#4f46e5 48%,#0891b2 100%) !important;
  border:1px solid rgba(255,255,255,.16) !important;
  box-shadow:0 13px 28px rgba(79,70,229,.28),inset 0 1px 0 rgba(255,255,255,.20) !important;
}
.stButton > button::after,.stFormSubmitButton > button::after{
  content:"";position:absolute;top:-20%;left:-70%;width:38%;height:140%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.40),transparent);
  transform:skewX(-18deg);transition:left .75s ease;pointer-events:none;
}
.stButton > button:hover::after,.stFormSubmitButton > button:hover::after{left:140%}
.stButton > button:hover,.stFormSubmitButton > button:hover{box-shadow:0 18px 38px rgba(79,70,229,.34),0 0 0 1px rgba(34,211,238,.08) !important}

/* Icon-like metric labels */
.metric-card .lbl::before{content:"✦";margin-right:5px;color:#D9A756}
.score-pill::before{content:"verified";font-family:'Material Symbols Rounded';font-size:14px;line-height:1}

/* AI chat feels like a native assistant */
.ai-chat-stack{scrollbar-width:thin;scrollbar-color:rgba(124,58,237,.35) transparent}
.ai-chat-bubble{position:relative;border-radius:18px !important;box-shadow:0 12px 28px rgba(15,23,42,.06) !important}
.ai-chat-bubble.user{background:linear-gradient(135deg,#B87925,#0891b2) !important;border-bottom-right-radius:7px !important}
.ai-chat-bubble.bot{background:linear-gradient(145deg,rgba(255,255,255,.74),rgba(241,245,249,.55)) !important;border:1px solid rgba(124,58,237,.12) !important;border-bottom-left-radius:7px !important}
.ai-chat-row.user .ai-chat-bubble::before{content:"person";font-family:'Material Symbols Rounded';position:absolute;top:7px;left:-25px;font-size:16px;color:#B87925;opacity:.7}
.ai-chat-row.bot .ai-chat-bubble::before{content:"smart_toy";font-family:'Material Symbols Rounded';position:absolute;top:7px;right:-25px;font-size:16px;color:#0891b2;opacity:.7}

/* Form controls */
.stTextInput input:focus,.stNumberInput input:focus,textarea:focus-within,[data-baseweb="select"] > div:focus-within{border-color:rgba(124,58,237,.48) !important;box-shadow:0 0 0 4px rgba(124,58,237,.08),0 8px 24px rgba(79,70,229,.08) !important}

/* Page-load stagger for common Streamlit sections */
[data-testid="stVerticalBlock"] > div:nth-child(1) .intel-box{animation:neoRise .55s .04s both}
[data-testid="stVerticalBlock"] > div:nth-child(2) .intel-box{animation:neoRise .55s .09s both}
[data-testid="stVerticalBlock"] > div:nth-child(3) .intel-box{animation:neoRise .55s .14s both}

@media (max-width:900px){
  .neo-topbar{padding:10px 12px !important}
  .neo-topbar-right{width:100%;flex-wrap:wrap}
  .neo-mini-pill{font-size:9px}
  .hero-banner{min-height:255px !important}
}

@media (prefers-reduced-motion: reduce){
  [data-testid="stAppViewContainer"]::before,[data-testid="stAppViewContainer"]::after,.neo-topbar::before,.neo-topbar-mark::after,.brand-mark::after,.hero-banner,.hero-banner::after{animation:none !important}
}
</style>
""", unsafe_allow_html=True)

# Top command bar for every screen.
st.markdown(f"""
<div class="premium-commandbar neo-topbar editorial-topbar">
  <div class="neo-topbar-left">
    <div class="neo-topbar-mark" aria-hidden="true">
      <svg viewBox="0 0 44 44"><path d="M7 20 22 8l15 12"/><path d="M11 18v19h22V18"/><path d="M18 37V25h8v12"/></svg>
    </div>
    <div>
      <div class="premium-commandbar-title">SMART<span>ESTATE</span><i></i></div>
      <div class="neo-topbar-sub">Property intelligence · home discovery · valuation</div>
    </div>
  </div>
  <div class="premium-commandbar-meta neo-topbar-right">
    <span class="premium-chip"><span class="premium-dot"></span> AI LIVE</span>
    <span class="neo-mini-pill"><span class="material-symbols-rounded">location_on</span> Gujarat</span>
    <span class="neo-mini-pill"><span class="material-symbols-rounded">auto_awesome</span> Smart Search</span>
  </div>
</div>
""", unsafe_allow_html=True)

# ==========================================
# ✦ V20 EDITORIAL LUXURY REDESIGN
# A clean, premium real-estate visual system that intentionally replaces
# the previous neon-heavy look without changing application logic.
# ==========================================
st.markdown(f"""
<style>
:root {{
  --edg:{'#3B2A1E' if st.session_state.dark_mode else '#111827'};
  --edm:{'#a8b4c8' if st.session_state.dark_mode else '#667085'};
  --edbg:{'#08111f' if st.session_state.dark_mode else '#f4f6f8'};
  --edsurface:{'#0f1b2d' if st.session_state.dark_mode else '#ffffff'};
  --edline:{'rgba(255,255,255,.10)' if st.session_state.dark_mode else 'rgba(16,24,40,.09)'};
  --edblue:#B87925;
  --edcyan:#A8754F;
  --edwarm:#C9974A;
  --edshadow:{'0 18px 50px rgba(0,0,0,.24)' if st.session_state.dark_mode else '0 18px 50px rgba(15,23,42,.07)'};
}}

/* ---------- Global shell ---------- */
.stApp {{
  background: var(--edbg) !important;
  color: var(--edg) !important;
}}
[data-testid="stAppViewContainer"] {{
  background:
    radial-gradient(circle at 7% 0%, rgba(37,99,235,.08), transparent 22%),
    radial-gradient(circle at 93% 12%, rgba(15,159,154,.06), transparent 20%),
    var(--edbg) !important;
  overflow-x:hidden !important;
}}
[data-testid="stHeader"] {{ background: transparent !important; }}
footer {{ visibility:hidden; }}
.block-container {{
  max-width: 1480px !important;
  padding: 0 22px 46px !important;
  margin: 0 auto !important;
}}

/* subtle moving light, not neon */
[data-testid="stAppViewContainer"]::before {{
  content:""; position:fixed; inset:auto -7% 5% auto; width:420px; height:420px;
  background:radial-gradient(circle, rgba(37,99,235,.07), transparent 66%);
  filter:blur(12px); pointer-events:none; z-index:0;
  animation: edFloat 14s ease-in-out infinite;
}}
@keyframes edFloat {{ 0%,100%{{transform:translate3d(0,0,0)}} 50%{{transform:translate3d(-20px,-28px,0)}} }}
@keyframes edRise {{ from{{opacity:0;transform:translateY(16px)}} to{{opacity:1;transform:none}} }}
@keyframes edReveal {{ from{{opacity:0;clip-path:inset(0 0 16% 0)}} to{{opacity:1;clip-path:inset(0 0 0 0)}} }}
@keyframes edShine {{ from{{transform:translateX(-140%) skewX(-16deg)}} to{{transform:translateX(250%) skewX(-16deg)}} }}

/* ---------- Header ---------- */
.editorial-topbar {{
  position:relative !important; z-index:5;
  display:flex !important; align-items:center !important; justify-content:space-between !important;
  gap:18px; padding:14px 16px !important; margin:4px 0 24px !important;
  border:1px solid var(--edline) !important;
  border-radius:20px !important;
  background:color-mix(in srgb, var(--edsurface) 88%, transparent) !important;
  box-shadow:var(--edshadow) !important;
  backdrop-filter:blur(18px) !important;
  overflow:hidden !important;
}}
.editorial-topbar::before {{
  content:""; position:absolute; inset:0 auto 0 0; width:5px;
  background:linear-gradient(180deg,var(--edblue),var(--edcyan));
}}
.editorial-topbar .neo-topbar-mark {{
  width:46px !important; height:46px !important; border-radius:14px !important;
  background:linear-gradient(145deg,var(--edblue),#8A5A18) !important;
  box-shadow:0 12px 26px rgba(37,99,235,.22) !important;
}}
.editorial-topbar .neo-topbar-mark::after {{ display:none !important; }}
.editorial-topbar .premium-commandbar-title {{
  font-size:17px !important; font-weight:900 !important; letter-spacing:1.2px !important;
  color:var(--edg) !important;
}}
.editorial-topbar .premium-commandbar-title span {{
  color:var(--edblue) !important; background:none !important; -webkit-text-fill-color:initial !important;
}}
.editorial-topbar .neo-topbar-sub {{
  color:var(--edm) !important; font-size:9px !important; letter-spacing:1.2px !important;
}}
.editorial-topbar .premium-chip {{
  background:rgba(16,185,129,.08) !important; color:#047857 !important;
  border:1px solid rgba(16,185,129,.16) !important;
}}
.editorial-topbar .neo-mini-pill {{
  background:rgba(148,163,184,.07) !important; color:var(--edm) !important;
  border:1px solid var(--edline) !important;
}}

/* ---------- Sidebar ---------- */
[data-testid="stSidebar"] {{
  width:296px !important; min-width:296px !important;
  background:
    linear-gradient(180deg, rgba(9,19,35,.99), rgba(10,18,32,.99)) !important;
  border-right:1px solid rgba(255,255,255,.07) !important;
  box-shadow:16px 0 50px rgba(2,8,23,.16) !important;
}}
[data-testid="stSidebar"] > div:first-child {{ width:296px !important; }}
[data-testid="stSidebarContent"] {{
  padding:16px 14px 40px !important;
  overflow-y:auto !important; overflow-x:hidden !important;
  scrollbar-width:thin; scrollbar-color:rgba(148,163,184,.35) transparent;
}}
[data-testid="stSidebarContent"]::-webkit-scrollbar {{ width:6px; }}
[data-testid="stSidebarContent"]::-webkit-scrollbar-track {{ background:transparent; }}
[data-testid="stSidebarContent"]::-webkit-scrollbar-thumb {{ background:rgba(148,163,184,.28); border-radius:99px; }}
[data-testid="stSidebar"] * {{ color:#e7edf7; }}
.sidebar-logo.neo-brand {{
  margin:0 0 16px !important; padding:10px 8px 18px !important;
  border-bottom:1px solid rgba(255,255,255,.08) !important;
}}
.sidebar-logo.neo-brand .brand-mark {{
  width:45px !important; height:45px !important; border-radius:14px !important;
  background:linear-gradient(145deg,var(--edblue),#123a91) !important;
  box-shadow:0 12px 25px rgba(37,99,235,.22) !important;
}}
.sidebar-logo.neo-brand .brand-copy h2 {{
  color:#f8fafc !important; font-size:20px !important; letter-spacing:.15px !important;
}}
.sidebar-logo.neo-brand .brand-copy h2 span {{ color:#72a1ff !important; -webkit-text-fill-color:initial !important; background:none !important; }}
.sidebar-logo.neo-brand .brand-copy p {{ color:#8ea0b8 !important; letter-spacing:1.35px !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] {{ gap:5px !important; margin-top:3px !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label {{
  min-height:48px !important; padding:10px 12px !important; margin:0 !important;
  background:transparent !important; border:1px solid transparent !important; border-radius:13px !important;
  transition:all .22s ease !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {{
  background:rgba(255,255,255,.045) !important; border-color:rgba(255,255,255,.07) !important;
  transform:translateX(2px) !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] {{
  background:linear-gradient(90deg,rgba(37,99,235,.19),rgba(37,99,235,.055)) !important;
  border-color:rgba(104,147,255,.22) !important;
  box-shadow:inset 3px 0 0 #4d7df0 !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] p {{
  color:#d9e3f2 !important; font-size:12px !important; font-weight:750 !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] p {{ color:#fff !important; }}

/* Ask AI: premium but understated */
.ai-command-card {{
  margin-top:14px !important; padding:15px !important; border-radius:17px !important;
  background:linear-gradient(145deg,rgba(37,99,235,.17),rgba(15,159,154,.07)) !important;
  border:1px solid rgba(123,160,255,.20) !important;
  box-shadow:0 18px 38px rgba(2,8,23,.22) !important;
}}
.ai-avatar {{ background:linear-gradient(145deg,var(--edblue),var(--edcyan)) !important; box-shadow:0 10px 22px rgba(37,99,235,.24) !important; }}
.ai-avatar-ring {{ border-color:rgba(113,163,255,.28) !important; }}
.ai-status-pill {{ background:rgba(255,255,255,.045) !important; border-color:rgba(255,255,255,.08) !important; }}
.ai-chat-panel {{ max-height:220px !important; }}
.ai-chat-bubble.bot {{ background:rgba(255,255,255,.055) !important; border:1px solid rgba(255,255,255,.08) !important; color:#e7edf7 !important; }}
.ai-chat-bubble.user {{ background:linear-gradient(135deg,var(--edblue),#174aa7) !important; color:#fff !important; }}
[data-testid="stSidebar"] .stTextInput input {{
  background:rgba(255,255,255,.045) !important; border:1px solid rgba(255,255,255,.08) !important;
  color:#f8fafc !important; border-radius:12px !important;
}}
[data-testid="stSidebar"] .stTextInput input::placeholder {{ color:#7f90a8 !important; }}
[data-testid="stSidebar"] .stFormSubmitButton button {{
  background:linear-gradient(135deg,var(--edblue),#174aa7) !important;
  box-shadow:0 10px 22px rgba(37,99,235,.22) !important;
}}
[data-testid="stSidebar"] .stButton > button {{
  background:rgba(255,255,255,.035) !important; border:1px solid rgba(255,255,255,.08) !important;
}}

/* ---------- Hero ---------- */
.hero-banner {{
  position:relative !important; min-height:360px !important; border-radius:28px !important;
  padding:62px 52px !important; margin:0 0 22px !important;
  background-size:cover !important; background-position:center !important;
  box-shadow:0 28px 70px rgba(15,23,42,.14) !important;
  overflow:hidden !important; animation:edReveal .65s ease both !important;
}}
.hero-banner::before {{
  background:linear-gradient(90deg,rgba(7,16,30,.83) 0%,rgba(7,16,30,.58) 44%,rgba(7,16,30,.10) 85%,transparent 100%) !important;
}}
.hero-banner::after {{
  content:"" !important; position:absolute !important; inset:auto -5% -50% auto !important;
  width:300px !important; height:300px !important; border-radius:50% !important;
  background:radial-gradient(circle,rgba(37,99,235,.20),transparent 68%) !important;
  animation:edFloat 9s ease-in-out infinite !important;
}}
.hero-content {{ position:relative !important; z-index:2 !important; max-width:680px !important; }}
.hero-eyebrow {{
  padding:8px 11px !important; border-radius:999px !important;
  background:rgba(255,255,255,.10) !important; border:1px solid rgba(255,255,255,.18) !important;
  color:#e8edf7 !important; backdrop-filter:blur(8px) !important;
}}
.hero-title {{
  font-family:Georgia,'Times New Roman',serif !important;
  font-size:clamp(42px,5vw,66px) !important; line-height:.98 !important;
  letter-spacing:-2px !important; color:#fff !important;
  margin:18px 0 12px !important;
}}
.gradient-text {{
  background:none !important; -webkit-text-fill-color:#9fc2ff !important; color:#9fc2ff !important;
}}
.hero-sub {{ color:rgba(255,255,255,.84) !important; max-width:590px !important; font-size:15px !important; line-height:1.65 !important; }}

/* ---------- Cards / sections ---------- */
.intel-box,.metric-card,.val-box,.compare-card,.poi-card,.property-summary-card {{
  background:var(--edsurface) !important; color:var(--edg) !important;
  border:1px solid var(--edline) !important; border-radius:20px !important;
  box-shadow:var(--edshadow) !important; backdrop-filter:blur(12px) !important;
  transition:transform .25s ease, box-shadow .25s ease, border-color .25s ease !important;
}}
.intel-box {{ padding:24px !important; margin-bottom:18px !important; }}
.intel-box:hover,.metric-card:hover,.compare-card:hover,.property-summary-card:hover {{
  transform:translateY(-4px) !important; border-color:rgba(37,99,235,.22) !important;
  box-shadow:0 24px 58px rgba(15,23,42,.10) !important;
}}
.intel-box::after,.metric-card::after,.val-box::after,.compare-card::after,.poi-card::after,.property-summary-card::after {{
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.22),transparent) !important;
}}
.intel-title,.card-header {{
  color:var(--edg) !important; font-weight:850 !important;
}}
.card-header {{ font-size:18px !important; padding-bottom:12px !important; border-bottom:1px solid var(--edline) !important; }}
.metric-card {{ padding:22px 18px !important; text-align:left !important; }}
.metric-card .num {{ color:var(--edg) !important; font-size:34px !important; letter-spacing:-1.5px !important; }}
.metric-card .lbl {{ color:var(--edm) !important; font-size:10px !important; letter-spacing:1.2px !important; }}
.metric-card .lbl::before {{ content:none !important; }}
.val-box {{
  border:1px solid rgba(37,99,235,.20) !important;
  background:linear-gradient(145deg,var(--edsurface),rgba(37,99,235,.05)) !important;
  padding:30px 24px !important;
}}
.val-price {{ color:var(--edblue) !important; font-size:46px !important; letter-spacing:-2px !important; }}
.premium-accent,.score-pill {{ border-color:rgba(37,99,235,.20) !important; }}
.poi-card {{ padding:14px 16px !important; border-radius:16px !important; }}
.poi-card:hover {{ transform:translateY(-2px) !important; border-color:rgba(15,159,154,.24) !important; }}

/* ---------- Controls ---------- */
.stTextInput input,.stNumberInput input,.stTextArea textarea,[data-baseweb="select"] > div {{
  background:var(--edsurface) !important; color:var(--edg) !important;
  border:1px solid var(--edline) !important; border-radius:13px !important;
  box-shadow:0 4px 18px rgba(15,23,42,.025) !important;
}}
.stTextInput input:focus,.stNumberInput input:focus,.stTextArea textarea:focus,[data-baseweb="select"] > div:focus-within {{
  border-color:rgba(37,99,235,.42) !important;
  box-shadow:0 0 0 4px rgba(37,99,235,.08) !important;
}}
.stCheckbox label span,.stRadio label span,.stSelectbox label,.stMultiSelect label,.stSlider label,.stNumberInput label,.stTextInput label {{
  color:var(--edm) !important; font-weight:700 !important;
}}

/* ---------- Buttons ---------- */
.stButton > button,.stFormSubmitButton > button {{
  border-radius:12px !important; border:1px solid transparent !important;
  background:linear-gradient(135deg,var(--edblue),#1649a6) !important;
  box-shadow:0 10px 24px rgba(37,99,235,.18) !important;
  min-height:43px !important; font-weight:800 !important;
  transition:transform .2s ease, box-shadow .2s ease !important;
}}
.stButton > button:hover,.stFormSubmitButton > button:hover {{
  transform:translateY(-2px) !important; box-shadow:0 16px 34px rgba(37,99,235,.23) !important;
}}
.stButton > button::after,.stFormSubmitButton > button::after {{
  content:""; position:absolute; top:-20%; left:-80%; width:32%; height:140%;
  background:rgba(255,255,255,.26); transform:skewX(-18deg); pointer-events:none;
}}
.stButton > button:hover::after,.stFormSubmitButton > button:hover::after {{ animation:edShine .75s ease 1; }}

/* ---------- Tabs / expanders / status ---------- */
.stTabs [data-baseweb="tab-list"] {{ gap:6px !important; border-bottom:1px solid var(--edline) !important; }}
.stTabs [data-baseweb="tab"] {{
  color:var(--edm) !important; font-weight:800 !important; border-radius:10px 10px 0 0 !important;
}}
.stTabs [aria-selected="true"] {{ color:var(--edblue) !important; }}
.streamlit-expanderHeader {{
  background:var(--edsurface) !important; border:1px solid var(--edline) !important;
  border-radius:14px !important; color:var(--edg) !important; font-weight:800 !important;
}}
[data-testid="stAlert"] {{ border-radius:14px !important; border:1px solid var(--edline) !important; }}

/* ---------- Dataframe / plots ---------- */
[data-testid="stDataFrame"] {{
  border:1px solid var(--edline) !important; border-radius:16px !important; overflow:hidden !important;
  box-shadow:0 10px 30px rgba(15,23,42,.05) !important;
}}

/* ---------- AI chat in main content (where present) ---------- */
.ai-chat-row {{ animation:edRise .35s ease both; }}
.ai-chat-bubble {{ border-radius:16px !important; }}

/* ---------- Responsive ---------- */
@media (max-width:1100px) {{
  .block-container {{ padding-left:14px !important; padding-right:14px !important; }}
  [data-testid="stSidebar"],[data-testid="stSidebar"] > div:first-child {{ width:276px !important; min-width:276px !important; }}
  .editorial-topbar {{ align-items:flex-start !important; flex-direction:column !important; }}
  .neo-topbar-right {{ width:100%; flex-wrap:wrap !important; }}
}}
@media (max-width:700px) {{
  .block-container {{ padding-left:8px !important; padding-right:8px !important; }}
  .hero-banner {{ min-height:300px !important; padding:38px 24px !important; border-radius:22px !important; }}
  .hero-title {{ font-size:42px !important; }}
  .neo-mini-pill {{ font-size:9px !important; }}
}}
@media (prefers-reduced-motion:reduce) {{
  * {{ animation-duration:.001ms !important; transition-duration:.001ms !important; }}
}}
</style>
""", unsafe_allow_html=True)

def sanitize_pdf_for_fpdf(pdf_obj):
    """Make classic PyFPDF page content safe for its latin-1 encoder."""
    replacements = {
        "—": "-",
        "–": "-",
        "−": "-",
        "₹": "Rs.",
        "•": "-",
        "·": "-",
        "×": "x",
        "→": "->",
        "←": "<-",
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "…": "...",
        "©": "(c)",
        "™": "(TM)",
        "®": "(R)",
        "✅": "[OK]",
        "⚠️": "[!]",
        "⚠": "[!]",
        "🏠": "[HOME]",
        "🛋️": "[FURNITURE]",
        "🛋": "[FURNITURE]",
        "📄": "[PDF]",
        "🛒": "[CART]",
        "💰": "[TOTAL]",
    }
    for page_no, page_text in list(pdf_obj.pages.items()):
        if not isinstance(page_text, str):
            continue
        for old, new in replacements.items():
            page_text = page_text.replace(old, new)
        # Final safety net: classic FPDF writes latin-1 page strings.
        page_text = page_text.encode("latin-1", errors="replace").decode("latin-1")
        pdf_obj.pages[page_no] = page_text
    return pdf_obj

# ==========================================
# ✨ SMART HOME HUB — NEW ALL-IN-ONE EXPERIENCE
# ==========================================
def _hub_property_options(dataset):
    if dataset is None or dataset.empty:
        return []
    cols = [c for c in ["City", "Area_Name", "BHK", "Area", "Price"] if c in dataset.columns]
    if len(cols) < 5:
        return []
    tmp = dataset[cols].copy()
    tmp = tmp.dropna(subset=["City", "Area_Name", "BHK", "Area", "Price"])
    tmp["BHK"] = pd.to_numeric(tmp["BHK"], errors="coerce")
    tmp["Area"] = pd.to_numeric(tmp["Area"], errors="coerce")
    tmp["Price"] = pd.to_numeric(tmp["Price"], errors="coerce")
    tmp = tmp.dropna()
    tmp = tmp.groupby(["City", "Area_Name", "BHK"], as_index=False).agg({"Area":"mean", "Price":"mean"})
    return tmp.sort_values(["City", "Area_Name", "BHK"]).to_dict("records")


def build_smart_recommendations(dataset, min_budget, max_budget, city="All Cities", locality="All Localities",
                                bhk="Any", min_area=0, top_n=6):
    """Return explainable recommendation matches using only dataset/project fields."""
    if dataset is None or dataset.empty:
        return pd.DataFrame()
    req_cols = {"City", "Area_Name", "BHK", "Area", "Price"}
    if not req_cols.issubset(dataset.columns):
        return pd.DataFrame()
    pool = dataset[list(req_cols)].copy()
    for c in ["BHK", "Area", "Price"]:
        pool[c] = pd.to_numeric(pool[c], errors="coerce")
    pool = pool.dropna()
    if city != "All Cities":
        pool = pool[pool["City"] == city]
    if locality != "All Localities":
        pool = pool[pool["Area_Name"] == locality]
    if bhk != "Any":
        pool = pool[pool["BHK"] == int(bhk)]
    if min_area and float(min_area) > 0:
        pool = pool[pool["Area"] >= float(min_area)]
    if pool.empty:
        return pd.DataFrame()

    grouped = pool.groupby(["City", "Area_Name", "BHK"], as_index=False).agg({"Area":"mean", "Price":"mean"})
    low = float(min_budget); high = max(float(max_budget), low + 0.01)
    grouped["PriceSqft"] = (grouped["Price"] * 100000) / grouped["Area"].replace(0, np.nan)
    grouped["BudgetFit"] = grouped["Price"].apply(lambda p: 100.0 if low <= p <= high else max(0.0, 100.0 - min(abs(p-low), abs(p-high)) / high * 120.0))
    grouped["BhkFit"] = grouped["BHK"].apply(lambda b: 100.0 if bhk == "Any" or int(b) == int(bhk) else max(0.0, 100.0 - abs(float(b)-int(bhk))*30.0))
    area_target = max(float(min_area or 0), 1.0)
    grouped["AreaFit"] = grouped["Area"].apply(lambda a: 100.0 if area_target <= 1 else min(100.0, max(0.0, float(a) / area_target * 100.0)))
    med_rate = float(grouped["PriceSqft"].median()) if not grouped["PriceSqft"].dropna().empty else 0.0
    grouped["ValueFit"] = grouped["PriceSqft"].apply(lambda r: 100.0 if med_rate <= 0 else max(0.0, min(100.0, 100.0 - ((float(r)/med_rate)-1.0)*85.0)))
    grouped["Safety"] = grouped["Area_Name"].map(lambda a: float(SAFETY_SCORES.get(a, 8.2))*10.0)
    grouped["MatchScore"] = (grouped["BudgetFit"]*0.38 + grouped["BhkFit"]*0.24 + grouped["AreaFit"]*0.16 + grouped["ValueFit"]*0.14 + grouped["Safety"]*0.08).round(1)
    return grouped.sort_values(["MatchScore", "BudgetFit", "ValueFit"], ascending=False).head(int(top_n)).reset_index(drop=True)


def get_price_benchmark(dataset, city, area_name, bhk, current_price, sqft):
    """Return current-vs-market benchmark plus clearly labelled modelled projection."""
    result = {"area_median": None, "city_median": None, "area_rate": None, "city_rate": None, "future_5y": None, "note": ""}
    if dataset is None or dataset.empty or "Price" not in dataset.columns:
        return result
    area_df = dataset[dataset["Area_Name"] == area_name].copy() if "Area_Name" in dataset.columns else pd.DataFrame()
    city_df = dataset[dataset["City"] == city].copy() if "City" in dataset.columns else pd.DataFrame()
    if "BHK" in area_df.columns:
        bhk_num = pd.to_numeric(area_df["BHK"], errors="coerce")
        bhk_area = area_df[bhk_num == int(bhk)]
        if not bhk_area.empty:
            area_df = bhk_area
    if not area_df.empty:
        result["area_median"] = float(pd.to_numeric(area_df["Price"], errors="coerce").median())
    if not city_df.empty:
        result["city_median"] = float(pd.to_numeric(city_df["Price"], errors="coerce").median())
    try:
        result["area_rate"] = (float(current_price)*100000)/max(float(sqft),1.0)
    except Exception:
        result["area_rate"] = None
    try:
        if "Area" in city_df.columns:
            rates = (pd.to_numeric(city_df["Price"], errors="coerce")*100000)/pd.to_numeric(city_df["Area"], errors="coerce").replace(0,np.nan)
            rates = rates.replace([np.inf,-np.inf],np.nan).dropna()
            if not rates.empty: result["city_rate"] = float(rates.median())
    except Exception:
        pass
    result["future_5y"] = float(current_price) * (1.065 ** 5)
    result["note"] = "5-year figure is a modelled 6.5% annual-growth scenario, not a guaranteed forecast."
    return result


def calculate_connectivity_score(facilities):
    """Project-based connectivity score from returned nearby facilities; not live travel time."""
    if not facilities:
        return {"overall": 0.0, "details": {}, "label": "No nearby data"}
    weights = {
        "transit": (25, {"Metro / Railway", "Bus / Transit"}),
        "health": (20, {"Hospital", "Pharmacy", "Healthcare / Doctor"}),
        "education": (20, {"School", "College / University", "Library"}),
        "shopping": (15, {"Shopping Mall", "Shop / Supermarket", "Restaurant", "Cafe"}),
        "finance": (10, {"Bank / ATM", "Post Office"}),
        "recreation": (10, {"Park / Garden", "Gym / Fitness", "Cinema"}),
    }
    details = {}; total = 0.0
    for key, (weight, cats) in weights.items():
        matches = [x for x in facilities if str(x.get("category")) in cats]
        if not matches:
            details[key] = 0.0
            continue
        nearest = min(float(x.get("distance", 99)) for x in matches)
        score = max(0.0, min(100.0, 100.0 - nearest*28.0))
        details[key] = round(score,1)
        total += score * weight / 100.0
    return {"overall": round(total,1), "details": details, "label": "Nearby-facility index"}


def _hub_save_alert(name, city, locality, min_budget, max_budget, bhk):
    item = {
        "name": (name or "My Home Alert").strip()[:50],
        "city": city,
        "locality": locality,
        "min_budget": float(min_budget),
        "max_budget": float(max_budget),
        "bhk": bhk,
        "saved_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
    }
    sig = f"{city}|{locality}|{min_budget}|{max_budget}|{bhk}"
    item["signature"] = sig
    st.session_state.smart_alerts = [x for x in st.session_state.smart_alerts if x.get("signature") != sig]
    st.session_state.smart_alerts.insert(0, item)
    st.session_state.smart_alerts = st.session_state.smart_alerts[:12]


def _hub_find_alert_matches(dataset, alert):
    recs = build_smart_recommendations(
        dataset,
        alert.get("min_budget",0), alert.get("max_budget",0),
        alert.get("city","All Cities"), alert.get("locality","All Localities"),
        alert.get("bhk","Any"), 0, 20
    )
    return recs


def _hub_property_key(row):
    return f"{row.get('City','')}|{row.get('Area_Name','')}|{int(row.get('BHK',0))}|{int(round(float(row.get('Area',0))))}"


def render_smart_property_card(row, index=0):
    city = str(row["City"]); area_name = str(row["Area_Name"]); bhk = int(row["BHK"]); sqft = float(row["Area"]); price = float(row["Price"])
    score = float(row.get("MatchScore",0)); rate = float(row.get("PriceSqft",0)); safety = float(SAFETY_SCORES.get(area_name,8.2))
    st.markdown(f"""
    <div class="hub-prop-card" style="animation-delay:{index*70}ms">
      <div class="hub-prop-image">{property_image_html(bhk, index=index, height=180)}</div>
      <div class="hub-prop-body">
        <div class="hub-prop-top"><span class="hub-badge">{score:.0f}% Match</span><span class="hub-score">{safety:.1f}/10 safety</span></div>
        <div class="hub-prop-title">{html.escape(area_name)}</div>
        <div class="hub-prop-meta">{html.escape(city)} · {bhk} BHK · {sqft:,.0f} sq.ft</div>
        <div class="hub-prop-price">₹{price:,.2f} L</div>
        <div class="hub-prop-rate">₹{rate:,.0f}/sq.ft · dataset benchmark</div>
      </div>
    </div>
    """, unsafe_allow_html=True)


# ==========================================
# SHARED FEATURE-PAGE HELPERS
# ==========================================
def _feature_default_property(dataset):
    """Return a useful default property from the last valuation/recent context."""
    candidate = st.session_state.get("feature_selected_property")
    if candidate:
        return candidate
    recent = st.session_state.get("recently_viewed") or []
    if recent:
        return recent[0]
    if st.session_state.get("last_pred", 0) and st.session_state.get("last_loc"):
        loc = str(st.session_state.last_loc)
        if ", " in loc:
            area_name, city = loc.split(", ", 1)
            bhk = int(st.session_state.get("req_bhk", 3))
            area = float(st.session_state.get("req_sqft", 1500))
            return {"City": city, "Area_Name": area_name, "BHK": bhk, "Area": area,
                    "Historical_Price": float(st.session_state.last_pred)}
    if dataset is not None and not dataset.empty:
        try:
            row = dataset.groupby(["City", "Area_Name", "BHK"], as_index=False).agg({"Area":"mean","Price":"mean"}).sort_values("Price").iloc[0]
            return {"City": str(row["City"]), "Area_Name": str(row["Area_Name"]), "BHK": int(row["BHK"]),
                    "Area": float(row["Area"]), "Historical_Price": float(row["Price"])}
        except Exception:
            pass
    return None


def render_feature_property_picker(prefix, dataset):
    """Compact property context picker shared by the new standalone pages."""
    current = _feature_default_property(dataset) or {
        "City": list(CITY_AREAS.keys())[0], "Area_Name": list(CITY_AREAS.values())[0][0],
        "BHK": 3, "Area": 1500.0, "Historical_Price": 0.0
    }
    city_list = list(CITY_AREAS.keys())
    city_default = current.get("City") if current.get("City") in city_list else city_list[0]
    c1, c2, c3, c4 = st.columns([1.15, 1.35, .8, 1.0])
    with c1:
        city = st.selectbox("City", city_list, index=city_list.index(city_default), key=f"{prefix}_city")
    area_list = CITY_AREAS.get(city, [])
    area_default = current.get("Area_Name") if current.get("Area_Name") in area_list else area_list[0]
    with c2:
        area_name = st.selectbox("Locality", area_list, index=area_list.index(area_default), key=f"{prefix}_area")
    bhk_default = int(current.get("BHK", 3))
    bhk_default = bhk_default if bhk_default in [1,2,3,4,5,6] else 3
    with c3:
        bhk = st.selectbox("BHK", [1,2,3,4,5,6], index=bhk_default-1, key=f"{prefix}_bhk")
    with c4:
        area_default_sqft = float(current.get("Area", {1:650,2:1100,3:1500,4:2500,5:3500,6:4500}.get(bhk,1500)))
        sqft = st.number_input("Area (sq.ft.)", min_value=300.0, max_value=15000.0, value=area_default_sqft, step=50.0, key=f"{prefix}_sqft")

    if st.button("Use This Property", key=f"{prefix}_use", use_container_width=True):
        bathrooms = {1:1,2:2,3:3,4:4,5:5,6:6}.get(int(bhk),3)
        try:
            if model:
                price = float(get_prediction(city, area_name, float(sqft), int(bhk), bathrooms, 0, 0, 0, 0, 0))
            else:
                mask = (
                    (dataset["City"] == city) &
                    (dataset["Area_Name"] == area_name) &
                    (pd.to_numeric(dataset["BHK"], errors="coerce") == int(bhk))
                )
                price = float(pd.to_numeric(dataset.loc[mask, "Price"], errors="coerce").mean())
            if not np.isfinite(price):
                price = float(current.get("Historical_Price", 0.0))
        except Exception:
            price = float(current.get("Historical_Price", 0.0))
        st.session_state.feature_selected_property = {
            "City": city, "Area_Name": area_name, "BHK": int(bhk), "Area": float(sqft), "Historical_Price": price
        }
        add_to_recent(st.session_state.feature_selected_property)
        st.rerun()

    prop = st.session_state.get("feature_selected_property") or current
    st.markdown(
        f"<div class='intel-box' style='padding:14px 18px; margin-top:10px;'><b>Selected:</b> {html.escape(str(prop.get('Area_Name','')))} · {html.escape(str(prop.get('City','')))} · {int(prop.get('BHK',3))} BHK · {float(prop.get('Area',0)):,.0f} sq.ft · <span style='color:#DAA752;font-weight:800;'>₹{float(prop.get('Historical_Price',0)):,.2f} L</span></div>",
        unsafe_allow_html=True,
    )
    return prop


def _feature_title(eyebrow, title, subtitle):
    st.markdown(
        f"<div class='hero-banner'><div class='hero-content'><div class='hero-eyebrow'>{html.escape(eyebrow)}</div><h1 class='hero-title'>{html.escape(title)}</h1><div class='hero-sub'>{html.escape(subtitle)}</div></div></div>",
        unsafe_allow_html=True,
    )




# REAL MAP RENDERER
def render_real_map(area_name, city, height=500):
    """Fast property-only map. No neighborhood API call and no nearby markers."""
    coords = EXACT_COORDS.get(area_name, CITY_DEFAULTS.get(city, [23.0225, 72.5714]))
    center_json = json.dumps({"lat": float(coords[0]), "lon": float(coords[1])})
    map_html = """
    <style>
      .se-map-wrap{border-radius:20px;overflow:hidden;border:1px solid #ead9c5;box-shadow:0 12px 30px rgba(90,55,25,.12)}
    </style>
    <div class="se-map-wrap"><div id="smartestate-map" style="width:100%;height:__HEIGHT__px"></div></div>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
      const center=__CENTER__;
      const map=L.map('smartestate-map',{zoomControl:true,scrollWheelZoom:true}).setView([center.lat,center.lon],14);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap contributors'}).addTo(map);
      const home=L.divIcon({className:'',html:'<div style="font-size:32px;filter:drop-shadow(0 2px 3px #777)">🏠</div>',iconSize:[36,36],iconAnchor:[18,32]});
      L.marker([center.lat,center.lon],{icon:home}).addTo(map).bindPopup('<b>🏠 Selected Property</b><br>__AREA__, __CITY__').openPopup();
    </script>
    """
    map_html = map_html.replace("__HEIGHT__", str(int(height)))
    map_html = map_html.replace("__CENTER__", center_json)
    map_html = map_html.replace("__AREA__", str(area_name).replace("'", "\\'"))
    map_html = map_html.replace("__CITY__", str(city).replace("'", "\\'"))
    components.html(map_html, height=int(height)+10, scrolling=False)


# ==========================================
# PAGE 1: PRICE PREDICTION (SPA FLOW + POIs)
# ==========================================
if current_page == "prediction":
    
    def sync_bhk():
        b = st.session_state.ui_bhk
        st.session_state.ui_area = {1: 650, 2: 1100, 3: 1500, 4: 2500, 5: 3500, 6: 4500}.get(b, 1500)
        st.session_state.ui_bath = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}.get(b, 3)
        st.session_state.req_bhk = b
        st.session_state.req_sqft = st.session_state.ui_area
        st.session_state.req_bath = st.session_state.ui_bath

    def sync_area(): st.session_state.req_sqft = st.session_state.ui_area
    def sync_bath(): st.session_state.req_bath = st.session_state.ui_bath

    # Render the valuation editor inside a replaceable placeholder.
    # The placeholder keeps Generate Valuation in the same run and avoids any old-page flash.
    prediction_stage = st.empty()
    show_results = bool(st.session_state.p_executed)
    predict_btn = False

    if not show_results:
        with prediction_stage.container():
            try:
                _rows = int(len(df)) if df is not None else 0
                _cities = int(df["City"].nunique()) if df is not None and "City" in df.columns else 0
                _avg_price = float(df["Price"].mean()) if df is not None and "Price" in df.columns and len(df) else 0.0
                _avg_area = float(df["Area"].mean()) if df is not None and "Area" in df.columns and len(df) else 0.0
                _metric_html = f"""<div class=\"se-metric-row\"><div class=\"se-metric\"><div class=\"se-metric-label\">Properties</div><div class=\"se-metric-value\">{_rows:,}</div><div class=\"se-metric-note\">Dataset inventory</div></div><div class=\"se-metric\"><div class=\"se-metric-label\">Markets</div><div class=\"se-metric-value\">{_cities}</div><div class=\"se-metric-note\">Cities covered</div></div><div class=\"se-metric\"><div class=\"se-metric-label\">Avg. Value</div><div class=\"se-metric-value\">₹{_avg_price:.1f}L</div><div class=\"se-metric-note\">Model dataset</div></div><div class=\"se-metric\"><div class=\"se-metric-label\">Avg. Space</div><div class=\"se-metric-value\">{_avg_area:,.0f}</div><div class=\"se-metric-note\">sq.ft average</div></div></div>"""
                st.markdown(_metric_html, unsafe_allow_html=True)
            except Exception:
                pass
            st.markdown(f"""
            <div class="valuation-hero-v2">
                <div class="valuation-hero-copy">
                    <div class="valuation-kicker">SMART HOME VALUATION</div>
                    <h1>Know Your Flat's<br><span>True Market Value.</span></h1>
                    <p>Choose your apartment configuration, add amenities, and get an AI-powered estimate in seconds.</p>
                    <div class="valuation-hero-badges">
                        <span>🏢 Apartment focused</span>
                        <span>🛏️ 1–6 BHK</span>
                        <span>⚡ Instant estimate</span>
                    </div>
                </div>
                <div class="valuation-hero-orbit">
                    <div class="orbit-ring orbit-ring-1"></div>
                    <div class="orbit-ring orbit-ring-2"></div>
                    <div class="orbit-house">⌂</div>
                    <div class="orbit-caption"><b>AI</b><span>Property Intelligence</span></div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            left, right = st.columns([1.02, 0.98], gap="large")

            with left:
                st.markdown('<div class="valuation-panel-v2">', unsafe_allow_html=True)
                st.markdown("""
                <div class="v2-section-head">
                    <div class="v2-step">01</div>
                    <div><div class="v2-section-title">Property Details</div><div class="v2-section-sub">Tell us about the flat you are evaluating.</div></div>
                </div>
                """, unsafe_allow_html=True)

                rc = st.session_state.req_city
                ra = st.session_state.req_area

                c_idx = list(CITY_AREAS.keys()).index(rc) if rc in CITY_AREAS else 0
                city = st.selectbox(t("select_city"), list(CITY_AREAS.keys()), index=c_idx)

                a_list = CITY_AREAS[city]
                a_idx = a_list.index(ra) if ra in a_list else 0
                area_name = st.selectbox(t("select_area"), a_list, index=a_idx)

                c1, c2 = st.columns(2)
                with c1:
                    bhk = st.selectbox("BHK", [1, 2, 3, 4, 5, 6], key="ui_bhk", on_change=sync_bhk)
                with c2:
                    area = st.number_input(t("property_area"), min_value=300, max_value=15000, step=50, key="ui_area", on_change=sync_area)

                bathrooms = st.selectbox(t("bathrooms"), [1, 2, 3, 4, 5, 6], key="ui_bath", on_change=sync_bath)

                st.markdown('<div class="v2-mini-note">💡 Tip: More built-up area and premium amenities can change the estimated value significantly.</div>', unsafe_allow_html=True)
                st.markdown('</div>', unsafe_allow_html=True)

            with right:
                st.markdown('<div class="valuation-panel-v2 preview-panel-v2">', unsafe_allow_html=True)
                st.markdown("""
                <div class="v2-section-head">
                    <div class="v2-step">02</div>
                    <div><div class="v2-section-title">Live Flat Preview</div><div class="v2-section-sub">Your selections update this preview.</div></div>
                </div>
                """, unsafe_allow_html=True)

                preview_img = FLOOR_PLANS.get(int(bhk), BG_IMAGE_URL)
                st.markdown(f"""
                <div class="flat-preview-v2">
                    <img src="{preview_img}" alt="Apartment preview">
                    <div class="flat-preview-overlay-v2">
                        <span class="preview-pill">{city}</span>
                        <span class="preview-pill">{area_name}</span>
                    </div>
                    <div class="flat-preview-title-v2">
                        <b>{bhk} BHK Apartment</b>
                        <span>~{area:,} sq.ft • {bathrooms} Bathroom{'s' if bathrooms != 1 else ''}</span>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                # 💎 Premium Amenities — keep these in the main AI Valuation preview panel.
                # Show each model-derived premium beside the amenity and update the live
                # AI property price immediately whenever a selection changes.
                ai_amenity_info = get_amenity_price_breakdown(
                    city, area_name, area, bhk, bathrooms,
                    False, False, False, False, False
                )
                ai_amenity_premiums = {
                    item["key"]: float(item["premium"])
                    for item in ai_amenity_info.get("items", [])
                }

                st.markdown('<div class="v2-amenity-title">Premium amenities</div>', unsafe_allow_html=True)
                a1, a2, a3 = st.columns(3)
                with a1:
                    gym = st.checkbox(
                        f'{t("gym")}  (+₹{ai_amenity_premiums.get("gym", 0.0):.2f} L)',
                        value=st.session_state.p_gym, key="ai_amenity_gym"
                    )
                with a2:
                    pool = st.checkbox(
                        f'{t("pool")}  (+₹{ai_amenity_premiums.get("pool", 0.0):.2f} L)',
                        value=st.session_state.p_pool, key="ai_amenity_pool"
                    )
                with a3:
                    cinema = st.checkbox(
                        f'{t("cinema")}  (+₹{ai_amenity_premiums.get("cinema", 0.0):.2f} L)',
                        value=st.session_state.p_cinema, key="ai_amenity_cinema"
                    )

                a4, a5, a6 = st.columns(3)
                with a4:
                    gaming = st.checkbox(
                        f'{t("gaming")}  (+₹{ai_amenity_premiums.get("gaming", 0.0):.2f} L)',
                        value=st.session_state.p_gaming, key="ai_amenity_gaming"
                    )
                with a5:
                    solar = st.checkbox(
                        f'☀️ Solar  (+₹{ai_amenity_premiums.get("solar", 0.0):.2f} L)',
                        value=st.session_state.p_solar, key="ai_amenity_solar"
                    )
                with a6:
                    selected_count = sum([bool(gym), bool(pool), bool(cinema), bool(gaming), bool(solar)])
                    st.markdown(
                        f'<div class="amenity-counter-v2"><b>{selected_count}/5</b><span>amenities selected</span></div>',
                        unsafe_allow_html=True,
                    )

                st.markdown('<div class="generate-wrap-v2">', unsafe_allow_html=True)
                predict_btn = st.button(t("generate_valuation"), use_container_width=True)
                st.markdown('<div class="generate-hint-v2">⚡ AI estimate • Neighborhood context • Confidence • Shareable report</div>', unsafe_allow_html=True)
                st.markdown('</div></div>', unsafe_allow_html=True)

        if predict_btn:
            st.session_state.req_city = city
            st.session_state.req_area = area_name
            st.session_state.p_gym = gym
            st.session_state.p_pool = pool
            st.session_state.p_cinema = cinema
            st.session_state.p_gaming = gaming
            st.session_state.p_solar = solar
            st.session_state.p_executed = True
            show_results = True
            prediction_stage.empty()

    if show_results:
            st.markdown('<div class="btn-back">', unsafe_allow_html=True)
            st.button(t("modify_details"), on_click=modify_prediction)
            st.markdown('</div>', unsafe_allow_html=True)
        
            city = st.session_state.req_city
            area_name = st.session_state.req_area
            bhk = st.session_state.ui_bhk
            area = st.session_state.ui_area
            bathrooms = st.session_state.ui_bath
            gym = st.session_state.p_gym
            pool = st.session_state.p_pool
            cinema = st.session_state.p_cinema
            gaming = st.session_state.p_gaming
            solar = st.session_state.p_solar

            # Compatibility aliases used by the PDF/email/report sections.
            # Keep these synchronized with the current AI Valuation amenity state.
            g, p, c, gam, s = bool(gym), bool(pool), bool(cinema), bool(gaming), bool(solar)
        
            st.markdown(f"""
            <div class="hero-banner" style="padding: 40px; margin-bottom: 20px;">
                <div class="hero-content">
                    <div class="hero-eyebrow">AI Valuation Results</div>
                    <h1 class="hero-title">{area_name}, <span class="gradient-text">{city}</span></h1>
                    <div class="hero-sub">{bhk} BHK Premium Configuration &nbsp;•&nbsp; ~{area} Sq.ft</div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            if model:
                pred = get_prediction(city, area_name, area, bhk, bathrooms, gym, pool, cinema, gaming, solar)
                price_sqft = (pred * 100000) / area
                add_to_recent({"City": city, "Area_Name": area_name, "BHK": bhk, "Area": area, "Historical_Price": pred})

                st.markdown(f"""
                <div class="results-topline-v2">
                    <div class="result-fact-v2 primary"><div class="rf-label">Estimated Value</div><div class="rf-value">{format_price(pred)}</div></div>
                    <div class="result-fact-v2"><div class="rf-label">Rate / sq.ft</div><div class="rf-value">₹{price_sqft:,.0f}</div></div>
                    <div class="result-fact-v2"><div class="rf-label">Configuration</div><div class="rf-value">{bhk} BHK</div></div>
                    <div class="result-fact-v2"><div class="rf-label">Built-up Area</div><div class="rf-value">{area:,} sq.ft</div></div>
                </div>
                """, unsafe_allow_html=True)
            
                if price_sqft > 6500: neg_text, neg_col, neg_desc, max_disc = "Low (Seller's Market 📉)", "#ef4444", "High demand area.", 2
                elif price_sqft < 4500: neg_text, neg_col, neg_desc, max_disc = "High (Buyer's Market 📈)", "#10b981", "Good inventory. You can negotiate more!", 8
                else: neg_text, neg_col, neg_desc, max_disc = "Medium (Balanced ⚖️)", "#f59e0b", "Standard market.", 4
                
            t1, t2, t3, t4, t5 = st.tabs([t("valuation_discount"), t("neighborhood"), t("lead_inquiry"), t("future_roi"), t("three_d_map")])
            
            with t1:
                col_a, col_b = st.columns([1.2, 1])
                with col_b:
                    st.markdown(f'<div class="intel-box animate-up delay-1"><div class="intel-title">🤝 Negotiation Power</div><div style="font-size:18px; font-weight:800; color:{neg_col}; margin-bottom:5px;">{neg_text}</div><p style="color:{sub_text}; font-size:13px; margin:0; margin-bottom:15px;">{neg_desc}</p>', unsafe_allow_html=True)
                    apply_discount = st.checkbox(f"🎁 Click to Apply {max_disc}% AI Discount")
                    final_price = pred * (1 - max_disc/100) if apply_discount else pred
                    st.markdown('</div>', unsafe_allow_html=True)
                
                    saf_score = SAFETY_SCORES.get(area_name, 8.2)
                    saf_color = "#10b981" if saf_score > 9.0 else ("#f59e0b" if saf_score > 8.0 else "#ef4444")
                    st.markdown(f'<div class="intel-box animate-up delay-2" style="margin-top:10px;"><div class="intel-title">🛡️ Women Safety & Security Index</div><div style="font-size:28px; font-weight:800; color:{saf_color}; text-shadow: 0 2px 10px rgba(0,0,0,0.1);">{saf_score} / 10</div></div>', unsafe_allow_html=True)

                    detail_score = calculate_property_score(df, city, area_name, bhk, area, pred, {"Gym": gym, "Swimming Pool": pool, "Cinema Hall": cinema, "Gaming Zone": gaming, "Solar Balcony": solar}, safety_score=saf_score)
                    st.markdown(f'<div class="intel-box animate-up delay-3" style="margin-top:10px;padding:15px;"><div class="intel-title">🏆 Property Score</div><div style="font-size:34px;font-weight:900;color:#daa752;">{detail_score["overall"]:.0f}<span style="font-size:13px;color:{sub_text};">/100</span></div><div style="font-size:11px;color:{sub_text};margin-top:5px;">Safety {detail_score["safety"]:.0f} • Value {detail_score["value"]:.0f} • Space {detail_score["space"]:.0f} • Amenities {detail_score["amenities"]:.0f}</div></div>', unsafe_allow_html=True)

                with col_a:
                    val_title = "Discounted Market Value" if apply_discount else "Estimated Market Value"
                    conf_score, conf_label = get_price_confidence(df, area_name)
                    conf_color = "#10b981" if conf_score >= 88 else ("#f59e0b" if conf_score >= 75 else "#ef4444")
                    st.markdown(f'<div class="val-box animate-up delay-1"><div style="color:#daa752; font-weight:800; font-size:13px; text-transform:uppercase; letter-spacing:1px;">{val_title}</div><div class="val-price">{format_price(final_price)}</div><div class="val-range">Expected Range: {format_price(final_price * 0.93)} — {format_price(final_price * 1.07)}</div><div style="margin-top:10px;"><span style="background:rgba(16,185,129,0.12); color:{conf_color}; padding:4px 12px; border-radius:20px; font-size:12px; font-weight:700;">{t("confidence")}: {conf_score}% ({conf_label})</span></div></div>', unsafe_allow_html=True)
                    if apply_discount: st.success(f"💸 You saved {format_price(pred - final_price)} by applying negotiation power!")
                    st.session_state.last_pred = final_price
                    st.session_state.last_loc = f"{area_name}, {city}"
                    record_valuation_history(city, area_name, bhk, area, final_price, conf_score)

                    fav_prop = {"City": city, "Area_Name": area_name, "BHK": bhk, "Area": area, "Historical_Price": final_price}
                    fcol1, fcol2 = st.columns(2)
                    with fcol1:
                        fav_label = t("saved_fav") if is_favorited(fav_prop) else t("save_fav")
                        if st.button(fav_label, key="fav_pred_page", use_container_width=True):
                            toggle_favorite(fav_prop)
                            smart_rerun()
                    with fcol2:
                        with st.expander(t("share_valuation")):
                            render_share_valuation(area_name, city, bhk, area, final_price, confidence=conf_score, key_suffix="main")

                # ----------------------------------------------------------
                # 🛋️ SMART FURNITURE COST ESTIMATOR
                # AI Valuation → exact bottom of the Valuation & Discount tab.
                # It is intentionally OUTSIDE both valuation columns so it
                # appears full-width directly underneath the visible section.
                # Closed by default; user can open/close it.
                # ----------------------------------------------------------
                st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)
                with st.expander("🛋️ Smart Furniture Cost Estimator", expanded=False):
                    furniture_result = furniture_estimator(
                        bhk, final_price, compact=False, instance_id="valuation_main"
                    )
                    furniture_subtotal = float(furniture_result.get("subtotal", 0))
                    furniture_installation = float(furniture_result.get("installation", 0))
                    furniture_total = float(furniture_result.get("total", 0))
                    st.session_state.furniture_cart_total = furniture_total

            with t2:
                st.markdown(f"#### 📍 Neighborhood Highlights · {area_name}")
                st.caption(t("nearby_caption"))
                render_advanced_neighborhood(area_name, city)



                similar = get_similar_properties(df, city, area_name, bhk, final_price)
                if not similar.empty:
                    st.markdown(f"#### {t('similar_props')}")
                    sim_cols = st.columns(len(similar))
                    for i, (_, srow) in enumerate(similar.iterrows()):
                        with sim_cols[i]:
                            st.markdown(f'''
                            <div class="intel-box" style="padding:15px; text-align:center;">
                                <div style="font-weight:800; color:{text_col};">{srow['Area_Name']}</div>
                                <div style="color:{sub_text}; font-size:12px; margin-bottom:8px;">{srow['City']} • {int(srow['BHK'])} BHK</div>
                                <div style="color:#daa752; font-size:18px; font-weight:800;">{format_price(srow['Price'])}</div>
                            </div>
                            ''', unsafe_allow_html=True)

            with t3:
                st.markdown(f"#### 📧 Send Automated Email Inquiry")
                st.caption("Your inquiry will be received immediately. A premium thank-you email will be sent to your email after about 25 seconds.")
                with st.form("email_inquiry_form", clear_on_submit=True):
                    e_col1, e_col2 = st.columns(2)
                    with e_col1: cust_name = st.text_input(t("full_name"))
                    with e_col2: cust_email = st.text_input(t("email"))
                    cust_phone = st.text_input(t("phone"))
                    if st.form_submit_button("📩 Send Inquiry", use_container_width=True):
                        if cust_name and cust_email:
                            scheduled, email_status = _schedule_inquiry_thank_you_email(
                                cust_email, cust_name, cust_phone, city, area_name,
                                bhk, area, final_price, bathrooms=bathrooms,
                                amenities={"Gym": bool(g), "Swimming Pool": bool(p), "Cinema Hall": bool(c), "Gaming Zone": bool(gam), "Solar Balcony": bool(s)},
                                property_title=f"{bhk} BHK Premium Property • {area_name}, {city}"
                            )
                            if scheduled:
                                st.success(f"✅ Inquiry received! Thank you, {cust_name}. Gmail verified. Your SmartEstate thank-you email will arrive at {cust_email} in about 25 seconds.")
                            else:
                                st.error(f"❌ Email was NOT scheduled. {email_status}")
                                st.caption("Your inquiry is still received. Keep your Gmail App Password private; do not paste it into chat.")
                        else: st.error(t("fill_name_email"))

            with t4:
                r_col1, r_col2 = st.columns([1.5, 1])
                with r_col1:
                    st.markdown('<div class="intel-box animate-up delay-1"><div class="card-header">📈 10-Year Price Projection (AI Forecast)</div>', unsafe_allow_html=True)
                
                    chart_font_color = "#f8fafc" if st.session_state.dark_mode else "#0f172a"
                    grid_color = "rgba(255,255,255,0.1)" if st.session_state.dark_mode else "rgba(0,0,0,0.1)"
                
                    years = list(range(2026, 2037))
                    future_prices = [final_price * (1.065 ** i) for i in range(11)]
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=years, y=future_prices, fill='tozeroy', mode='lines+markers', line=dict(color='#daa752', width=3), marker=dict(size=8, color='#daa752'), fillcolor='rgba(218, 167, 82, 0.22)'))
                    fig.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', margin=dict(l=0, r=0, t=10, b=0), xaxis=dict(showgrid=False, color=chart_font_color), yaxis=dict(showgrid=True, gridcolor=grid_color, color=chart_font_color, title="Est. Value (Lakhs)"), height=250)
                    st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})
                
                    st.markdown('</div>', unsafe_allow_html=True)
                
                    if HAS_FPDF:
                        pdf = FPDF()
                        pdf.set_auto_page_break(auto=True, margin=18)
                        pdf.add_page()

                        C_DARK = (63, 47, 35)
                        C_BROWN = (91, 64, 40)
                        C_BEIGE = (244, 231, 210)
                        C_GRAY = (112, 98, 84)
                        C_SOFT = (249, 244, 236)
                        C_GREEN = (55, 122, 81)

                        # Premium brochure header
                        pdf.set_fill_color(*C_DARK)
                        pdf.rect(0, 0, 210, 48, 'F')
                        pdf.set_text_color(255, 255, 255)
                        pdf.set_xy(12, 9)
                        pdf.set_font("Arial", 'B', 22)
                        pdf.cell(186, 10, txt="HOUSE PRICE PREDICTION", ln=True, align='C')
                        pdf.set_text_color(*C_BEIGE)
                        pdf.set_font("Arial", 'B', 10)
                        pdf.cell(186, 7, txt="AI VALUATION + FURNISHING REPORT", ln=True, align='C')

                        pdf.set_y(58)
                        pdf.set_text_color(*C_DARK)
                        pdf.set_font("Arial", 'B', 18)
                        pdf.cell(0, 9, txt=f"{area_name}, {city}", ln=True)
                        pdf.set_font("Arial", '', 10)
                        pdf.set_text_color(*C_GRAY)
                        pdf.cell(0, 6, txt=f"{bhk} BHK | {bathrooms} Bathrooms | {area} sq.ft", ln=True)
                        pdf.cell(0, 6, txt=f"Valuation Date: {datetime.now().strftime('%d %B %Y')}", ln=True)
                        _pdf_user = st.session_state.get("auth_user") or {}
                        pdf.set_fill_color(*C_BEIGE)
                        pdf.set_text_color(*C_DARK)
                        pdf.set_font("Arial", 'B', 9)
                        pdf.cell(0, 8, txt=f"Customer: {(_pdf_user.get('name') or 'User')} | {(_pdf_user.get('phone') or 'Not provided')} | {(_pdf_user.get('email') or 'Not provided')}", ln=True, fill=True)
                        pdf.ln(7)

                        def pdf_section(title):
                            pdf.set_fill_color(*C_DARK)
                            pdf.set_text_color(255, 255, 255)
                            pdf.set_font("Arial", 'B', 12)
                            pdf.cell(0, 9, txt=f"  {title}", ln=True, fill=True)
                            pdf.ln(2)
                            pdf.set_text_color(*C_DARK)

                        def pdf_row(label, value):
                            pdf.set_font("Arial", '', 10)
                            pdf.set_text_color(*C_GRAY)
                            pdf.cell(62, 7, txt=label, ln=0)
                            pdf.set_font("Arial", 'B', 10)
                            pdf.set_text_color(*C_DARK)
                            pdf.multi_cell(0, 7, txt=str(value))

                        furniture_rows, furniture_sub_pdf, furniture_install_pdf, furniture_total_pdf, furniture_count_pdf = get_furniture_pdf_details()
                        combined_lakhs_pdf = final_price + furniture_total_pdf / 100000

                        pdf_section("1. PROPERTY DETAILS")
                        pdf_row("Location:", f"{area_name}, {city}")
                        pdf_row("Configuration:", f"{bhk} BHK | {bathrooms} Bathrooms")
                        pdf_row("Total Area:", f"{area} sq.ft")
                        am_flags = {"Gym": gym, "Swimming Pool": pool, "Cinema Hall": cinema, "Gaming Zone": gaming, "Solar Balcony": solar}
                        am_list = [k for k, v in am_flags.items() if v]
                        pdf_row("Premium Amenities:", ", ".join(am_list) if am_list else "None Selected")
                        ai_pdf_amenity_info = get_amenity_price_breakdown(
                            city, area_name, area, bhk, bathrooms, gym, pool, cinema, gaming, solar
                        )
                        selected_impacts = [
                            f'{item["name"]}: +Rs. {item["premium"]:.2f} Lakhs'
                            for item in ai_pdf_amenity_info.get("items", [])
                            if item.get("selected")
                        ]
                        pdf_row(
                            "Amenity Price Impact:",
                            "; ".join(selected_impacts) if selected_impacts else "None"
                        )

                        pdf.ln(4)
                        pdf_section("2. AI VALUATION")
                        pdf_row("Base Estimated Value:", f"Rs. {pred:,.2f} Lakhs")
                        if apply_discount:
                            pdf_row("AI Discount Applied:", f"{max_disc}% | Saved Rs. {pred-final_price:,.2f} Lakhs")
                        pdf_row("Property Value:", f"Rs. {final_price:,.2f} Lakhs")

                        pdf.ln(4)
                        pdf_section("3. SMART FURNITURE & FURNISHING")
                        if furniture_rows:
                            pdf.set_fill_color(*C_SOFT)
                            pdf.set_text_color(*C_DARK)
                            pdf.set_font("Arial", 'B', 9)
                            pdf.cell(72, 8, txt="Furniture Item", border=1, fill=True)
                            pdf.cell(22, 8, txt="Qty", border=1, fill=True, align='C')
                            pdf.cell(43, 8, txt="Unit Price", border=1, fill=True, align='R')
                            pdf.cell(53, 8, txt="Subtotal", border=1, fill=True, align='R')
                            pdf.ln()
                            pdf.set_font("Arial", '', 9)
                            for category, name, icon, unit, qty, line in furniture_rows:
                                pdf.cell(72, 7, txt=name, border=1)
                                pdf.cell(22, 7, txt=str(qty), border=1, align='C')
                                pdf.cell(43, 7, txt=f"Rs. {unit:,.0f}", border=1, align='R')
                                pdf.cell(53, 7, txt=f"Rs. {line:,.0f}", border=1, align='R')
                                pdf.ln()
                            pdf.ln(2)
                            pdf_row("Furniture Items:", str(furniture_count_pdf))
                            pdf_row("Furniture Subtotal:", f"Rs. {furniture_sub_pdf:,.0f}")
                            pdf_row("Installation (8%):", f"Rs. {furniture_install_pdf:,.0f}")
                            pdf_row("Total Furnishing Cost:", f"Rs. {furniture_total_pdf:,.0f}")
                        else:
                            pdf_row("Furniture Selection:", "No furniture items selected")

                        pdf.ln(5)
                        pdf.set_fill_color(*C_BROWN)
                        pdf.set_text_color(255, 255, 255)
                        pdf.set_font("Arial", 'B', 15)
                        pdf.cell(0, 13, txt=f" TOTAL PROPERTY + FURNISHING: Rs. {combined_lakhs_pdf:,.2f} LAKHS ", ln=True, fill=True, align='C')
                        pdf.ln(5)

                        # EMI uses the combined project cost.
                        curr_dp = st.session_state.get('b_emi_dp', 20)
                        curr_tenure = st.session_state.get('b_emi_tenure', 20)
                        curr_interest = st.session_state.get('b_emi_interest', 8.5)
                        combined_loan = combined_lakhs_pdf * 100000 * (1 - curr_dp / 100)
                        combined_emi = calculate_emi(combined_loan, curr_interest, curr_tenure)

                        pdf_section("4. FINANCING — PROPERTY + FURNISHING")
                        pdf_row("Total Project Cost:", f"Rs. {combined_lakhs_pdf:,.2f} Lakhs")
                        pdf_row("Downpayment:", f"{curr_dp}%")
                        pdf_row("Loan Tenure:", f"{curr_tenure} Years")
                        pdf_row("Interest Rate:", f"{curr_interest}%")
                        pdf_row("Estimated EMI:", f"Rs. {combined_emi:,.0f} / month")

                        pdf.ln(4)
                        pdf_section("5. FORECAST & PROJECTIONS")
                        rent = ((final_price * 100000) * 0.035) / 12
                        app_10yr = final_price * (1.065 ** 10)
                        pdf_row("Est. Monthly Rent:", f"Rs. {rent:,.0f} / month")
                        pdf_row("10-Year Property Forecast:", f"Rs. {app_10yr:,.2f} Lakhs")

                        pdf.ln(7)
                        pdf.set_text_color(*C_GRAY)
                        pdf.set_font("Arial", 'I', 8)
                        pdf.multi_cell(
                            0, 4,
                            txt="Disclaimer: Property values, furniture prices, installation charges and EMI are estimates. "
                                "Actual prices, vendor charges and lending terms may vary."
                        )
                        pdf.set_y(-14)
                        pdf.set_text_color(*C_GRAY)
                        pdf.set_font("Arial", '', 8)
                        pdf.cell(0, 5, txt="House Price Prediction AI • Premium Valuation + Furnishing Report", align='C')

                        sanitize_pdf_for_fpdf(pdf)

                        pdf_output = pdf.output(dest="S")
                        try:
                            pdf_bytes = pdf_output.encode('latin-1')
                        except AttributeError:
                            pdf_bytes = bytes(pdf_output)

                        _protected_download_button(
                            "📄 Download Premium Valuation + Furniture PDF",
                            data=pdf_bytes,
                            file_name=f"Premium_Valuation_{area_name}.pdf",
                            mime="application/pdf",
                            key="prediction_premium_pdf",
                            action="download this valuation PDF"
                        )

                with r_col2:
                    st.markdown('<div class="intel-box animate-up delay-2"><div class="card-header">🏦 EMI Calculator</div>', unsafe_allow_html=True)
                    downpayment_pct = st.slider(t("downpayment"), 10, 50, 20, key="emi_dp")
                    total_project_for_emi = final_price * 100000
                    loan_amt = total_project_for_emi * (1 - (downpayment_pct/100))
                    tenure = st.slider(t("loan_tenure"), 5, 30, 20, key="emi_tenure")
                    interest = st.number_input(t("interest_rate"), value=8.5, step=0.1, key="emi_interest")
                    emi = calculate_emi(loan_amt, interest, tenure)
                    st.caption(f"Property project cost: ₹{total_project_for_emi/100000:,.2f} L")
                    st.markdown(f'<div class="intel-stat" style="margin-top:15px; font-size:28px;">₹ {emi:,.0f} <span style="font-size:14px; color:{sub_text};">/ mo</span></div>', unsafe_allow_html=True)
                    st.markdown('</div>', unsafe_allow_html=True)

                st.markdown('<div style="margin-top:18px;"></div>', unsafe_allow_html=True)
                st.markdown('<div class="intel-box animate-up delay-3"><div class="card-header">💰 Total Purchase Cost Planner</div>', unsafe_allow_html=True)
                fc1, fc2, fc3 = st.columns(3)
                with fc1:
                    stamp_pct = st.number_input("Stamp Duty % (estimate)", min_value=0.0, max_value=15.0, value=5.0, step=0.25, key="budget_stamp_pct")
                with fc2:
                    registration_pct = st.number_input("Registration % (estimate)", min_value=0.0, max_value=10.0, value=1.0, step=0.25, key="budget_reg_pct")
                with fc3:
                    other_rs = st.number_input("Other Charges (₹)", min_value=0.0, step=5000.0, value=0.0, key="budget_other_rs")
                financial = calculate_total_purchase_cost(final_price, downpayment_pct, interest, tenure, stamp_pct, registration_pct, other_rs)
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Upfront Cash", f"₹{financial['upfront_cash']:,.0f}")
                m2.metric("Loan", f"₹{financial['loan_amount']:,.0f}")
                m3.metric("Monthly EMI", f"₹{financial['emi']:,.0f}")
                m4.metric("Total Cost", f"₹{financial['total_acquisition']:,.0f}")
                st.caption("Stamp duty, registration and other charges are editable estimates; verify actual charges before purchase.")
                st.markdown('</div>', unsafe_allow_html=True)

                st.markdown('<div style="margin-top:18px;"></div>', unsafe_allow_html=True)
                st.markdown('<div class="intel-box animate-up delay-3"><div class="card-header">📊 Area Price Trend</div>', unsafe_allow_html=True)
                render_area_price_trend(df, city, area_name, current_price=final_price)
                st.markdown('</div>', unsafe_allow_html=True)

                st.markdown('<div style="margin-top:18px;"></div>', unsafe_allow_html=True)
                st.markdown('<div class="intel-box animate-up delay-3"><div class="card-header">📈 Smart Investment Analyzer</div>', unsafe_allow_html=True)
                ia1, ia2, ia3 = st.columns(3)
                with ia1:
                    rent_default=max(5000,min(100000,float(final_price)*100000*0.035/12))
                    monthly_rent=st.number_input("Expected Monthly Rent (₹)",min_value=0.0,value=float(round(rent_default/500)*500),step=500.0,key="investment_monthly_rent")
                with ia2:
                    inv_app=st.number_input("Annual Appreciation %",min_value=-10.0,max_value=20.0,value=6.5,step=0.5,key="investment_appreciation")
                with ia3:
                    inv_rg=st.number_input("Annual Rent Growth %",min_value=-10.0,max_value=20.0,value=5.0,step=0.5,key="investment_rent_growth")
                inv=calculate_investment_analysis(final_price,monthly_rent,inv_app,inv_rg,10)
                im1,im2,im3,im4=st.columns(4)
                im1.metric("Rental Yield",f"{inv['rental_yield']:.2f}%")
                im2.metric("Monthly Rent",f"₹{inv['monthly_rent']:,.0f}")
                im3.metric("5-Year Value",f"₹{inv['future_5']/100000:,.1f} L")
                im4.metric("10-Year Value",f"₹{inv['future_10']/100000:,.1f} L")
                st.markdown(f"<div style='display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:12px;'><div style='padding:12px;border-radius:12px;background:rgba(218,167,82,.08);'><small>10Y Rent Income</small><br><b>₹{inv['cumulative_rent']/100000:,.1f} L</b></div><div style='padding:12px;border-radius:12px;background:rgba(218,167,82,.08);'><small>10Y Capital Gain</small><br><b>₹{inv['capital_gain']/100000:,.1f} L</b></div><div style='padding:12px;border-radius:12px;background:rgba(218,167,82,.08);'><small>Modelled 10Y ROI</small><br><b>{inv['roi']:.1f}%</b></div></div>",unsafe_allow_html=True)
                st.caption("Modelled scenario only — actual rent, appreciation, vacancy, taxes, maintenance and transaction costs can differ. Change the assumptions above to test scenarios.")
                st.markdown('</div>', unsafe_allow_html=True)

                if HAS_FPDF:
                    facilities_for_report = get_neighborhood_facilities(area_name, city, radius_m=2000) or []
                    report_amenities = [k for k, v in {"Gym": gym, "Swimming Pool": pool, "Cinema Hall": cinema, "Gaming Zone": gaming, "Solar Balcony": solar}.items() if v]
                    report_bytes = build_property_pdf_report(city, area_name, bhk, bathrooms, area, final_price, detail_score, financial, report_amenities, facilities_for_report, "Current area profile and modelled 6.5% annual trend", customer=st.session_state.get("auth_user"))
                    _protected_download_button(
                        "📄 Download Full Property Report (PDF)",
                        data=report_bytes,
                        file_name=f"Property_Report_{area_name}.pdf",
                        mime="application/pdf",
                        key="budget_full_pdf_report",
                        action="download this property report"
                    )

            with t5:
                m_col1, m_col2 = st.columns(2)
                with m_col1:
                    st.markdown(f"#### 📐 Architectural Presentation Board ({bhk} BHK)")
                    st.caption(t("reference_style"))
                    d1, d2 = st.columns(2)
                    with d1:
                        design_style = st.selectbox(t("property_design"), ["Modern Luxury", "Contemporary", "Classic Premium"], key="p_3d_style")
                    with d2:
                        view_mode = st.selectbox(t("view"), ["Exterior", "Interior", "Floor Plan"], key="p_3d_view")
                    render_architectural_presentation_board(bhk, area, gym, pool, cinema, gaming, solar, design_style, view_mode)
                with m_col2:
                    st.markdown(f"#### 📍 Exact Location Map")
                    st.markdown('<div class="animate-up delay-2" style="border-radius:16px; overflow:hidden; border:2px solid rgba(255,255,255,0.1);">', unsafe_allow_html=True)
                    render_real_map(area_name, city, height=500)
                    st.markdown('</div>', unsafe_allow_html=True)


# ==========================================
# PAGE 2: 🔄 BUDGET-FIRST SEARCH
# ==========================================
elif current_page == "budget":
    
    def b_sync_bhk():
        b = st.session_state.b_ui_bhk
        if b != "Any":
            val_b = int(b)
            st.session_state.b_ui_area = {1: 650, 2: 1100, 3: 1500, 4: 2500, 5: 3500, 6: 4500}.get(val_b, 0)
            st.session_state.b_ui_bath = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}.get(val_b, "Any")
        else:
            st.session_state.b_ui_area = 0
            st.session_state.b_ui_bath = "Any"

    if not st.session_state.search_executed and st.session_state.viewing_property is None:
        # Keep Budget-First Search visually consistent with the AI Valuation page.
        st.markdown("""
        <div class="valuation-hero-v2">
            <div class="valuation-hero-copy">
                <div class="valuation-kicker">SMART BUDGET SEARCH</div>
                <h1>Find Your Flat<br><span>Within Your Budget.</span></h1>
                <p>Set your budget, choose the right configuration, and discover apartment matches with the same premium experience as AI Valuation.</p>
                <div class="valuation-hero-badges">
                    <span>🏢 Apartment focused</span>
                    <span>🛏️ 1–6 BHK</span>
                    <span>🎯 Smart matching</span>
                </div>
            </div>
            <div class="valuation-hero-orbit">
                <div class="orbit-ring orbit-ring-1"></div>
                <div class="orbit-ring orbit-ring-2"></div>
                <div class="orbit-house">⌂</div>
                <div class="orbit-caption"><b>₹</b><span>Budget Intelligence</span></div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        if df.empty:
            st.warning(t("dataset_required"))
            st.stop()

        left, right = st.columns([1.02, 0.98], gap="large")

        with left:
            st.markdown('<div class="valuation-panel-v2">', unsafe_allow_html=True)
            st.markdown("""
            <div class="v2-section-head">
                <div class="v2-step">01</div>
                <div><div class="v2-section-title">Budget & Property Details</div><div class="v2-section-sub">Tell us what kind of flat you want.</div></div>
            </div>
            """, unsafe_allow_html=True)

            st.markdown('<div class="v2-field-kicker">💰 Search by affordability</div>', unsafe_allow_html=True)
            search_type = st.radio(
                t("search_method"),
                ["Total Budget", "Monthly EMI"],
                format_func=lambda x: t("total_budget") if x == "Total Budget" else t("monthly_emi"),
                horizontal=True,
                label_visibility="collapsed",
                key="b_search_type_v2",
            )

            if search_type == "Total Budget":
                b1, b2 = st.columns(2)
                with b1:
                    min_b = st.number_input(t("min_budget"), min_value=0.0, value=20.0, step=5.0, key="b_min_budget_v2")
                with b2:
                    max_b = st.number_input(t("max_budget"), min_value=0.0, value=60.0, step=5.0, key="b_max_budget_v2")
                target_budget = max_b
            else:
                b1, b2 = st.columns(2)
                with b1:
                    min_emi = st.number_input(t("min_emi"), min_value=0, value=15000, step=5000, key="b_min_emi_v2")
                with b2:
                    max_emi = st.number_input(t("max_emi"), min_value=0, value=50000, step=5000, key="b_max_emi_v2")
                min_b = (min_emi / 867) / 0.8
                max_b = (max_emi / 867) / 0.8
                target_budget = max_b
                st.markdown(
                    f'<div class="v2-mini-note">💡 Estimated affordability range: <b>₹{min_b:,.1f}L – ₹{max_b:,.1f}L</b></div>',
                    unsafe_allow_html=True,
                )

            st.markdown('<div class="v2-divider"></div>', unsafe_allow_html=True)
            st.markdown('<div class="v2-field-kicker">📍 Choose your preferred location</div>', unsafe_allow_html=True)
            loc1, loc2 = st.columns(2)
            with loc1:
                user_city = st.selectbox(
                    t("preferred_city"),
                    ["All Cities"] + list(CITY_AREAS.keys()),
                    format_func=lambda x: t("all_cities") if x == "All Cities" else x,
                    key="b_city_v2",
                )
            with loc2:
                if user_city == "All Cities":
                    loc_options = ["All Localities"]
                else:
                    loc_options = ["All Localities"] + CITY_AREAS[user_city]
                user_locality = st.selectbox(
                    t("preferred_locality"),
                    loc_options,
                    format_func=lambda x: t("all_localities") if x == "All Localities" else x,
                    key="b_locality_v2",
                )

            st.markdown('<div class="v2-divider"></div>', unsafe_allow_html=True)
            st.markdown('<div class="v2-field-kicker">🏠 Apartment configuration</div>', unsafe_allow_html=True)
            user_req_bhk = st.selectbox(
                t("required_bhk"),
                ["Any", 1, 2, 3, 4, 5, 6],
                format_func=lambda x: t("any") if x == "Any" else f"{x} BHK",
                key="b_ui_bhk",
                on_change=b_sync_bhk,
            )
            prop1, prop2 = st.columns(2)
            with prop1:
                user_bath = st.selectbox(
                    t("minimum_bathrooms"),
                    ["Any", 1, 2, 3, 4, 5, 6],
                    format_func=lambda x: t("any") if x == "Any" else f"{x} Bath",
                    key="b_ui_bath",
                )
            with prop2:
                user_area = st.number_input(t("minimum_area"), min_value=0, max_value=15000, step=100, key="b_ui_area")

            current_search_params = {
                'min_b': min_b, 'max_b': max_b, 'target_budget': target_budget,
                'user_city': user_city, 'user_locality': user_locality,
                'user_req_bhk': user_req_bhk, 'user_bath': user_bath,
                'user_area': user_area,
                'gym': bool(st.session_state.b_gym), 'pool': bool(st.session_state.b_pool),
                'cinema': bool(st.session_state.b_cinema), 'gaming': bool(st.session_state.b_gaming),
                'solar': bool(st.session_state.b_solar)
            }

            st.markdown('</div>', unsafe_allow_html=True)

        with right:
            st.markdown('<div class="valuation-panel-v2 preview-panel-v2">', unsafe_allow_html=True)
            st.markdown("""
            <div class="v2-section-head">
                <div class="v2-step">02</div>
                <div><div class="v2-section-title">Live Search Preview</div><div class="v2-section-sub">Your choices update this apartment preview.</div></div>
            </div>
            """, unsafe_allow_html=True)

            preview_bhk = 3 if user_req_bhk == "Any" else int(user_req_bhk)
            preview_img = FLOOR_PLANS.get(preview_bhk, BG_IMAGE_URL)
            preview_locality = user_locality if user_locality != "All Localities" else "Any locality"
            preview_city = user_city if user_city != "All Cities" else "All cities"
            bath_text = "Any bathrooms" if user_bath == "Any" else f"{int(user_bath)} Bathroom{'s' if int(user_bath) != 1 else ''}"
            area_text = "Any size" if user_area <= 0 else f"≥ {user_area:,.0f} sq.ft"
            budget_text = f"₹{min_b:,.0f}L – ₹{max_b:,.0f}L"

            st.markdown(f"""
            <div class="flat-preview-v2">
                <img src="{preview_img}" alt="Apartment search preview">
                <div class="flat-preview-overlay-v2">
                    <span class="preview-pill">{html.escape(str(preview_city))}</span>
                    <span class="preview-pill">{html.escape(str(preview_locality))}</span>
                </div>
                <div class="flat-preview-title-v2">
                    <b>{preview_bhk} BHK Apartment Search</b>
                    <span>{html.escape(area_text)} • {html.escape(bath_text)}</span>
                </div>
            </div>
            """, unsafe_allow_html=True)

            st.markdown('<div class="v2-live-stats">', unsafe_allow_html=True)
            ls1, ls2, ls3 = st.columns(3)
            with ls1:
                st.markdown(f'<div class="v2-live-stat"><span>Budget</span><b>{budget_text}</b></div>', unsafe_allow_html=True)
            with ls2:
                st.markdown(f'<div class="v2-live-stat"><span>Configuration</span><b>{preview_bhk} BHK</b></div>', unsafe_allow_html=True)
            with ls3:
                st.markdown(f'<div class="v2-live-stat"><span>Area</span><b>{html.escape(area_text)}</b></div>', unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)

            st.markdown('<div class="v2-field-kicker" style="margin-top:16px;">🔖 Save this search</div>', unsafe_allow_html=True)
            saved_search_name = st.text_input(
                "",
                placeholder="Example: Ahmedabad 3 BHK under 80L",
                key="saved_search_name_v2",
                label_visibility="collapsed",
            )
            ss1, ss2 = st.columns([1.05, 1.45])
            with ss1:
                save_search_btn = st.button("🔖 Save Search", use_container_width=True, key="save_search_v2")
            with ss2:
                search_btn = st.button(t("find_homes"), use_container_width=True, key="find_homes_v2")

            st.markdown('<div class="generate-hint-v2">⚡ Smart matching • Budget fit • BHK • Location • Amenities</div>', unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)

        if save_search_btn:
            current_search_params = {
                'min_b': min_b, 'max_b': max_b, 'target_budget': target_budget,
                'user_city': user_city, 'user_locality': user_locality,
                'user_req_bhk': user_req_bhk, 'user_bath': user_bath,
                'user_area': user_area,
                'gym': False, 'pool': False, 'cinema': False,
                'gaming': False, 'solar': False
            }
            saved = save_budget_search(current_search_params, saved_search_name or f"{user_city} property search")
            if saved is not False:
                st.success("✅ Search saved. Open 🔖 Saved Searches anytime to run it again.")

        if search_btn:
            # Budget-First Search main filters intentionally have NO amenities.
            # Premium amenities are selected only inside View Details.
            st.session_state.b_gym = False
            st.session_state.b_pool = False
            st.session_state.b_cinema = False
            st.session_state.b_gaming = False
            st.session_state.b_solar = False
            st.session_state.b_search_params = {
                'min_b': min_b, 'max_b': max_b, 'target_budget': target_budget,
                'user_city': user_city, 'user_locality': user_locality,
                'user_req_bhk': user_req_bhk, 'user_bath': user_bath,
                'user_area': user_area,
                'gym': False, 'pool': False, 'cinema': False,
                'gaming': False, 'solar': False
            }
            st.session_state.search_executed = True
            smart_rerun()
            
    elif st.session_state.search_executed and st.session_state.viewing_property is None:
        
        st.markdown('<div class="btn-back">', unsafe_allow_html=True)
        st.button(t("modify_search"), on_click=modify_search)
        st.markdown('</div>', unsafe_allow_html=True)
        
        p = st.session_state.b_search_params
        res_df = df[(df['Price'] >= p['min_b']) & (df['Price'] <= p['max_b'])]
        
        if p['user_city'] != "All Cities": res_df = res_df[res_df['City'] == p['user_city']]
        if p['user_locality'] != "All Localities": res_df = res_df[res_df['Area_Name'] == p['user_locality']]
        if p['user_req_bhk'] != "Any": res_df = res_df[res_df['BHK'] == int(p['user_req_bhk'])]
        if p['user_bath'] != "Any": res_df = res_df[res_df['Bathrooms'] >= int(p['user_bath'])]
        if p['user_area'] > 0: res_df = res_df[res_df['Area'] >= p['user_area']]
        
        # Amenities are selected only inside View Details; do not filter search results by amenities.
        if not res_df.empty:
            recs = res_df.groupby(['City', 'Area_Name', 'BHK']).agg({'Price': 'mean', 'Area': 'mean'}).reset_index()
            recs = recs.sort_values(by='Price', ascending=True) 
                
            st.success(t("configs_found", count=len(recs)))
            
            # SmartEstate AI-style advisor summary — explainable and API-free.
            top_row = recs.iloc[0] if not recs.empty else None
            if top_row is not None:
                top_score, top_details = calculate_smart_match_score(top_row, p, df)
                top_reasons, top_cautions = build_property_advisor(top_row, p, top_score, top_details)
                reason_html = "".join(f"<span>✓ {html.escape(x)}</span>" for x in top_reasons)
                caution_html = "".join(f"<span>• {html.escape(x)}</span>" for x in top_cautions)
                st.markdown(f"""
                <div class="intel-box animate-up" style="margin:18px 0 22px;padding:20px;border:1px solid rgba(218,167,82,.30);">
                    <div style="display:flex;justify-content:space-between;gap:14px;align-items:center;flex-wrap:wrap;">
                        <div><div style="font-size:11px;letter-spacing:1.5px;color:#b88945;font-weight:800;">SMART PROPERTY ADVISOR</div>
                        <div style="font-size:21px;font-weight:900;margin-top:3px;">Your strongest profile match</div>
                        <div style="font-size:12px;opacity:.72;">{html.escape(str(top_row['Area_Name']))}, {html.escape(str(top_row['City']))} • {int(top_row['BHK'])} BHK</div></div>
                        <div style="font-size:30px;font-weight:900;color:#daa752;">{top_score}%<span style="font-size:12px;opacity:.7;"> match</span></div>
                    </div>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;">{reason_html}</div>
                    {f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:7px;opacity:.72;font-size:11px;">{caution_html}</div>' if caution_html else ''}
                </div>
                """, unsafe_allow_html=True)

            cols = st.columns(3)
            for idx, row in recs.iterrows():
                saf_score = SAFETY_SCORES.get(row['Area_Name'], 8.2)
                
                match_score, match_details = calculate_smart_match_score(row, p, df)
                advisor_reasons, advisor_cautions = build_property_advisor(row, p, match_score, match_details)
                
                if row['Price'] <= p['target_budget']:
                    deal_tag = "🔥 Hot Deal"
                    tag_col = "#ef4444"
                    bg_tag = "rgba(239, 68, 68, 0.15)"
                else:
                    deal_tag = "⭐ Premium"
                    tag_col = "#daa752"
                    bg_tag = "rgba(220,176,101,0.15)"
                
                img_url = get_property_image_url(row['BHK'], idx)
                card_score = calculate_property_score(
                    df, row['City'], row['Area_Name'], int(row['BHK']), float(row['Area']), float(row['Price']),
                    {"Gym": bool(p.get('gym')), "Swimming Pool": bool(p.get('pool')), "Cinema Hall": bool(p.get('cinema')), "Gaming Zone": bool(p.get('gaming')), "Solar Balcony": bool(p.get('solar'))},
                    safety_score=saf_score
                )
                
                with cols[idx % 3]:
                    st.markdown(f"""
                    <div class="animate-up" style="background:{card_bg}; backdrop-filter: blur(10px); border:1px solid {border_col}; border-radius:16px 16px 0 0; border-bottom: none; box-shadow: 0 4px 15px rgba(0,0,0,0.05); overflow: hidden; margin-top: 15px;">
                        <div style="position: relative;">
                            {property_image_html(row['BHK'], index=idx, height=180, title=f"Premium {int(row['BHK'])} BHK home")}
                            <div style="position: absolute; top: 10px; right: 10px; background:{bg_tag}; color:{tag_col}; padding:4px 10px; border-radius:20px; font-size:10px; font-weight:800; backdrop-filter: blur(5px);">
                                {deal_tag}
                            </div>
                            <div style="position: absolute; bottom: 10px; left: 10px; background:rgba(0,0,0,0.6); color:#fff; padding:3px 8px; border-radius:6px; font-size:10px; font-weight:700;">
                                🎯 {match_score}% AI Match
                            </div>
                            <div style="position: absolute; bottom: 10px; right: 10px; background:rgba(255,255,255,0.94); color:#0f172a; padding:3px 8px; border-radius:6px; font-size:10px; font-weight:800;">🏆 {card_score['overall']:.0f}/100</div>
                        </div>
                        <div style="padding: 15px;">
                            <div style="margin-bottom: 8px;">
                                <b style="color:{text_col}; font-size:16px;">{row['Area_Name']}</b><br>
                                <span style="color:{sub_text}; font-size:11px; font-weight:600;">{row['City']}</span>
                            </div>
                            <div style="margin-bottom: 12px; color:{sub_text}; font-size:12px; font-weight:600; line-height:1.6;">
                                🛏️ {row['BHK']} BHK &nbsp;•&nbsp; 📐 ~{row['Area']:.0f} sq.ft<br>
                                🛡️ Safety Score: <span style="color:{text_col};">{saf_score}/10</span>
                            </div>
                            <div style="color:#daa752; font-size:22px; font-weight:800; text-shadow: 0 2px 5px rgba(218,167,82,0.22);">
                                {format_price(row['Price'], 1)}
                            </div>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    detail_key = _prop_key({"City": row["City"], "Area_Name": row["Area_Name"], "BHK": row["BHK"], "Area": row["Area"]})
                    if st.button(t("view_details"), key=f"btn_{idx}", use_container_width=True):
                        st.session_state.viewing_property = {"City": row["City"], "Area_Name": row["Area_Name"], "BHK": int(row["BHK"]), "Area": float(row["Area"]), "Historical_Price": float(row["Price"])}
                        st.session_state.budget_inline_detail_key = None
                        # Clear report/AI state from a previously opened property
                        # so the new property's page never performs stale work.
                        st.session_state.pop("property_detail_report_bytes", None)
                        st.session_state.pop("property_ai_answer", None)
                        smart_rerun()

                    card_prop = {"City": row['City'], "Area_Name": row['Area_Name'], "BHK": row['BHK'], "Area": row['Area'], "Historical_Price": row['Price']}
                    fc1, fc2 = st.columns(2)
                    with fc1:
                        fav_lbl = "❤️ Saved" if is_favorited(card_prop) else "🤍 Save"
                        if st.button(fav_lbl, key=f"fav_{idx}", use_container_width=True):
                            toggle_favorite(card_prop)
                            smart_rerun()
                    with fc2:
                        already_selected = any(_prop_key(c) == _prop_key(card_prop) for c in st.session_state.compare_selection)
                        cmp_lbl = "☑️ Selected" if already_selected else "⚖️ Compare"
                        if st.button(cmp_lbl, key=f"cmp_{idx}", use_container_width=True):
                            if already_selected:
                                st.session_state.compare_selection = [c for c in st.session_state.compare_selection if _prop_key(c) != _prop_key(card_prop)]
                            elif len(st.session_state.compare_selection) < 2:
                                st.session_state.compare_selection.append(card_prop)
                                if len(st.session_state.compare_selection) == 2:
                                    a, b = st.session_state.compare_selection
                                    st.session_state.c_params = {
                                        'city_A': a['City'], 'area_A': a['Area_Name'], 'sqft_A': int(a['Area']), 'bhk_A': int(a['BHK']),
                                        'city_B': b['City'], 'area_B': b['Area_Name'], 'sqft_B': int(b['Area']), 'bhk_B': int(b['BHK']),
                                        # Preserve the exact prices shown on Budget-First Search.
                                        # Compare must never re-predict these selected listings.
                                        'listing_price_A': float(a.get('Historical_Price', 0) or 0),
                                        'listing_price_B': float(b.get('Historical_Price', 0) or 0),
                                        'source_A': 'budget_search',
                                        'source_B': 'budget_search',
                                    }
                                    st.session_state.c_compare_executed = True
                                    st.session_state.compare_return_page = "budget_results"
                                    st.session_state.compare_selection = []
                                    st.session_state.pending_nav = PAGES["compare"]
                                    smart_rerun()
                            else:
                                st.warning(t("max_compare"))
                            smart_rerun()
        else: 
            st.error(t("no_matching"))

    else:
        prop = st.session_state.viewing_property
        add_to_recent(prop)
        st.markdown('<div class="btn-back">', unsafe_allow_html=True)
        st.button(t("back_search"), on_click=close_property_view)
        st.markdown('</div>', unsafe_allow_html=True)
        
        st.markdown(f"""
        <div class="hero-banner" style="padding: 40px; margin-bottom: 20px;">
            <div class="hero-content">
                <div class="hero-eyebrow">Property Analysis</div>
                <h1 class="hero-title">{prop['Area_Name']}, <span class="gradient-text">{prop['City']}</span></h1>
                <div class="hero-sub">{prop['BHK']} BHK Premium Configuration &nbsp;•&nbsp; ~{prop['Area']} Sq.ft</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        if model:
            bhk = int(prop['BHK'])
            area = int(prop['Area'])
            city = prop['City']
            area_name = prop['Area_Name']
            bathrooms = {1:1, 2:2, 3:3, 4:4, 5:5, 6:6}.get(bhk, 3)
            
            p_mem = st.session_state.b_search_params
            g, p, c, gam, s = p_mem.get('gym', False), p_mem.get('pool', False), p_mem.get('cinema', False), p_mem.get('gaming', False), p_mem.get('solar', False)
            
            # IMPORTANT: View Details must keep the exact listing price shown on
            # the Budget-First Search card. Do not re-price the property by
            # adding amenity/model premiums a second time.
            listing_price = float(prop.get("Historical_Price", 0) or 0)
            base_price = listing_price if listing_price > 0 else get_prediction(
                city, area_name, area, bhk, bathrooms, False, False, False, False, False
            )

            # ------------------------------------------------------
            # 💎 VIEW DETAILS — OPTIONAL AMENITY ADD-ONS
            # Base listing price stays fixed. Selected amenities add a
            # model-derived premium; removing an amenity removes exactly
            # that premium. This is state-based, so repeated clicks never
            # accumulate the same premium twice.
            # ------------------------------------------------------
            amenity_state_key = f"budget_detail_amenities_{_prop_key(prop)}"
            if amenity_state_key not in st.session_state:
                st.session_state[amenity_state_key] = {
                    "gym": False, "pool": False, "cinema": False,
                    "gaming": False, "solar": False
                }
            detail_am = st.session_state[amenity_state_key]

            # Premium Amenities belong ONLY inside Budget-First Search -> View Details.
            st.markdown(
                '<div class="intel-box" style="margin:10px 0 14px;padding:15px;">'
                '<div class="intel-title">💎 Premium Amenities</div>'
                '<div style="font-size:12px;color:#806d5a;margin:3px 0 10px;">Select or remove amenities to update this property price.</div>'
                '</div>',
                unsafe_allow_html=True,
            )
            am1, am2 = st.columns(2)
            with am1:
                detail_am["gym"] = st.checkbox(
                    "🏋️ Gym",
                    value=bool(detail_am.get("gym", False)),
                    key=f"{amenity_state_key}_gym",
                )
                detail_am["cinema"] = st.checkbox(
                    "🎬 Cinema Hall",
                    value=bool(detail_am.get("cinema", False)),
                    key=f"{amenity_state_key}_cinema",
                )
                detail_am["solar"] = st.checkbox(
                    "☀️ Solar Balcony",
                    value=bool(detail_am.get("solar", False)),
                    key=f"{amenity_state_key}_solar",
                )
            with am2:
                detail_am["pool"] = st.checkbox(
                    "🏊 Swimming Pool",
                    value=bool(detail_am.get("pool", False)),
                    key=f"{amenity_state_key}_pool",
                )
                detail_am["gaming"] = st.checkbox(
                    "🎮 Gaming Zone",
                    value=bool(detail_am.get("gaming", False)),
                    key=f"{amenity_state_key}_gaming",
                )
            st.session_state[amenity_state_key] = detail_am

            d_amen = get_amenity_price_breakdown(
                city, area_name, area, bhk, bathrooms,
                detail_am.get("gym", False), detail_am.get("pool", False),
                detail_am.get("cinema", False), detail_am.get("gaming", False),
                detail_am.get("solar", False)
            )
            amenity_total = float(d_amen.get("selected_total", 0.0))
            pred = float(base_price) + amenity_total
            price_sqft = (pred * 100000) / area

            # Recalculate from the complete current selection after widgets render.
            d_amen = get_amenity_price_breakdown(
                city, area_name, area, bhk, bathrooms,
                detail_am["gym"], detail_am["pool"], detail_am["cinema"],
                detail_am["gaming"], detail_am["solar"]
            )
            amenity_total = float(d_amen.get("selected_total", 0.0))
            pred = float(base_price) + amenity_total
            price_sqft = (pred * 100000) / area
            g, p, c, gam, s = (detail_am["gym"], detail_am["pool"], detail_am["cinema"], detail_am["gaming"], detail_am["solar"])

            # ------------------------------------------------------
            # 🏠 PROPERTY HEALTH + 🤖 PROPERTY-SPECIFIC AI ASSISTANT
            # ------------------------------------------------------
            saf_score = float(SAFETY_SCORES.get(area_name, 8.2))
            property_amenities = {
                "Gym": bool(g),
                "Swimming Pool": bool(p),
                "Cinema Hall": bool(c),
                "Gaming Zone": bool(gam),
                "Solar Balcony": bool(s),
            }
            health = calculate_property_score(
                df, city, area_name, bhk, area, pred, property_amenities, safety_score=saf_score
            )
            health_items = [
                ("💰 Value", health["value"]),
                ("📐 Space", health["space"]),
                ("🛡️ Safety", health["safety"]),
                ("✨ Amenities", health["amenities"]),
            ]
            health_cards = "".join(
                f'<div style=\"background:rgba(255,255,255,.72);border:1px solid rgba(184,137,69,.18);border-radius:14px;padding:13px 14px;min-width:135px;flex:1;\">'
                f'<div style=\"font-size:11px;color:#7d6754;font-weight:700;\">{label}</div>'
                f'<div style=\"font-size:22px;color:#9a6b25;font-weight:900;margin-top:3px;\">{score:.0f}<span style=\"font-size:11px;color:#806d5a;\">/100</span></div></div>'
                for label, score in health_items
            )
            st.markdown(
                f"""
                <div style=\"margin:18px 0 20px;padding:20px;border-radius:20px;background:linear-gradient(135deg,#fffaf1,#f5e8d2);border:1px solid #e2c99d;box-shadow:0 10px 26px rgba(91,61,32,.08);\">
                    <div style=\"display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;\">
                      <div><div style=\"font-size:11px;letter-spacing:2px;color:#a26f2d;font-weight:900;\">PROPERTY HEALTH</div>
                        <div style=\"font-size:23px;color:#4b3526;font-weight:900;margin-top:4px;\">Property Profile</div>
                        <div style=\"font-size:12px;color:#806d5a;margin-top:3px;\">Transparent indicators based on this property's available app data.</div></div>
                      <div style=\"text-align:right;\"><div style=\"font-size:11px;color:#806d5a;font-weight:700;\">Overall profile</div>
                        <div style=\"font-size:32px;color:#b57b25;font-weight:900;\">{health["overall"]:.0f}<span style=\"font-size:12px;color:#806d5a;\">/100</span></div></div>
                    </div>
                    <div style=\"display:flex;gap:10px;flex-wrap:wrap;margin-top:15px;\">{health_cards}</div>
                </div>
                """, unsafe_allow_html=True
            )

            # Compact side Ask SmartEstate AI popover — keeps the property page clean.
            ai_side_left, ai_side_right = st.columns([4.9, 1.35])
            with ai_side_right:
                with st.popover("🤖 Ask SmartEstate AI", use_container_width=True):
                    st.markdown("**Ask anything about this home**")
                    st.caption("Price • EMI • investment • amenities • safety • valuation • comparison")

                    # Enter/Send submits immediately. Clear removes both the question and answer.
                    with st.form("property_smartestate_ai_form", clear_on_submit=True, enter_to_submit=True):
                        ai_q = st.text_input(
                            "Your question",
                            placeholder="Example: Is this property investment ke liye achhi hai?",
                            key="property_ai_question_box",
                            label_visibility="collapsed",
                        )
                        ai_c1, ai_c2 = st.columns(2)
                        with ai_c1:
                            ask_property_ai = st.form_submit_button("✨ Send / Enter", use_container_width=True, type="primary")
                        with ai_c2:
                            clear_property_ai = st.form_submit_button("🗑️ Clear", use_container_width=True)

                    if clear_property_ai:
                        st.session_state.property_ai_answer = ""
                        st.session_state.ai_last_error = ""
                        st.rerun()

                    if ask_property_ai and ai_q.strip():
                        try:
                            data_context = _dataset_summary_for_ai(df)
                            if len(data_context) > 7000:
                                data_context = data_context[:7000]
                            property_context = f"""CURRENT PROPERTY:\nCity: {city}\nLocality: {area_name}\nBHK: {bhk}\nArea: {area} sq.ft\nModelled price: {format_price(pred)}\nPrice per sq.ft: ₹{price_sqft:,.0f}\nSafety profile: {saf_score}/10\nProperty health: overall {health['overall']:.0f}/100, value {health['value']:.0f}/100, space {health['space']:.0f}/100, safety {health['safety']:.0f}/100, amenities {health['amenities']:.0f}/100\nSelected amenities: {', '.join([k for k,v in property_amenities.items() if v]) or 'None selected'}\nReference price shown by the app: {format_price(prop.get('Historical_Price', pred))}"""
                            ai_messages = [{
                                "role": "system",
                                "content": f"""You are SmartEstate AI for an Indian real-estate app. Answer specifically about the CURRENT PROPERTY below. ALWAYS answer in natural Hinglish (Hindi + English written in Roman script), unless the user explicitly asks for another language. Keep the answer concise, clear and practical. Use only supplied project data for numeric claims. Do not invent live market facts. Clearly distinguish modelled estimates from guarantees. For buy/investment questions, explain factors rather than giving a guaranteed recommendation. Use ₹, sq.ft, BHK and Indian terms naturally.\n\n{property_context}\n\nPROJECT DATA SUMMARY:\n{data_context}"""
                            }, {"role": "user", "content": ai_q.strip()}]
                            st.session_state.property_ai_answer = call_gemini_ai(ai_messages, temperature=0.25)
                            st.session_state.ai_last_error = ""
                        except Exception as exc:
                            st.session_state.property_ai_answer = f"⚠️ AI request failed: {exc}"
                            st.session_state.ai_last_error = str(exc)

                    if st.session_state.get("property_ai_answer"):
                        safe_answer = html.escape(str(st.session_state.property_ai_answer)).replace(chr(10), '<br>')
                        st.markdown(
                            f"""<div style=\"margin-top:12px;padding:14px;border-radius:14px;background:#f7eddc;border:1px solid #e1c79b;color:#4b3526;line-height:1.6;\"><div style=\"font-size:10px;letter-spacing:1.3px;color:#a26f2d;font-weight:900;margin-bottom:6px;\">SMART ESTATE AI · HINGLISH</div>{safe_answer}</div>""",
                            unsafe_allow_html=True
                        )

            st.markdown('<div style="height:6px"></div>', unsafe_allow_html=True)

            if price_sqft > 6500: neg_text, neg_col, neg_desc, max_disc = "Low (Seller's Market 📉)", "#ef4444", "High demand area.", 2
            elif price_sqft < 4500: neg_text, neg_col, neg_desc, max_disc = "High (Buyer's Market 📈)", "#10b981", "Good inventory. You can negotiate more!", 8
            else: neg_text, neg_col, neg_desc, max_disc = "Medium (Balanced ⚖️)", "#f59e0b", "Standard market.", 4
                
            # Neighborhood data is fetched only when the user explicitly loads the Neighborhood tab.

            t1, t2, t3, t4, t5 = st.tabs([t("valuation_discount"), t("neighborhood"), t("lead_inquiry"), t("future_roi"), t("three_d_map")])
            
            with t1:
                col_a, col_b = st.columns([1.2, 1])
                with col_b:
                    st.markdown(f'<div class="intel-box animate-up delay-1"><div class="intel-title">🤝 Negotiation Power</div><div style="font-size:18px; font-weight:800; color:{neg_col}; margin-bottom:5px;">{neg_text}</div><p style="color:{sub_text}; font-size:13px; margin:0; margin-bottom:15px;">{neg_desc}</p>', unsafe_allow_html=True)
                    apply_discount = st.checkbox(f"🎁 Click to Apply {max_disc}% AI Discount", key="b_apply_disc")
                    final_price = pred * (1 - max_disc/100) if apply_discount else pred
                    st.markdown('</div>', unsafe_allow_html=True)
                    
                    saf_score = SAFETY_SCORES.get(area_name, 8.2)
                    saf_color = "#10b981" if saf_score > 9.0 else ("#f59e0b" if saf_score > 8.0 else "#ef4444")
                    st.markdown(f'<div class="intel-box animate-up delay-2" style="margin-top:10px;"><div class="intel-title">🛡️ Women Safety & Security Index</div><div style="font-size:28px; font-weight:800; color:{saf_color}; text-shadow: 0 2px 10px rgba(0,0,0,0.1);">{saf_score} / 10</div></div>', unsafe_allow_html=True)

                with col_a:
                    val_title = "Discounted Market Value" if apply_discount else "Estimated Market Value"
                    conf_score, conf_label = get_price_confidence(df, area_name)
                    conf_color = "#10b981" if conf_score >= 88 else ("#f59e0b" if conf_score >= 75 else "#ef4444")
                    st.markdown(f'<div class="val-box animate-up delay-1"><div style="color:#daa752; font-weight:800; font-size:13px; text-transform:uppercase; letter-spacing:1px;">{val_title}</div><div class="val-price">{format_price(final_price)}</div><div class="val-range">Base listing: {format_price(base_price)} &nbsp;•&nbsp; Amenities: +{format_price(amenity_total)}</div><div style="margin-top:10px;"><span style="background:rgba(16,185,129,0.12); color:{conf_color}; padding:4px 12px; border-radius:20px; font-size:12px; font-weight:700;">{t("confidence")}: {conf_score}% ({conf_label})</span></div></div>', unsafe_allow_html=True)
                    if apply_discount: st.success(f"💸 You saved {format_price(pred - final_price)} by applying negotiation power!")

                    fav_prop_b = {"City": city, "Area_Name": area_name, "BHK": bhk, "Area": area, "Historical_Price": final_price}
                    bfcol1, bfcol2 = st.columns(2)
                    with bfcol1:
                        fav_label_b = t("saved_fav") if is_favorited(fav_prop_b) else t("save_fav")
                        if st.button(fav_label_b, key="fav_budget_page", use_container_width=True):
                            toggle_favorite(fav_prop_b)
                            smart_rerun()
                    with bfcol2:
                        with st.expander(t("share_valuation")):
                            render_share_valuation(area_name, city, bhk, area, final_price, confidence=conf_score, key_suffix="budget")

                    # 🛋️ SMART FURNITURE COST ESTIMATOR
                    # Budget-First Search -> Find Home -> View Details -> Valuation & Discount
                    # It appears directly below "Share This Valuation" and nowhere else on this property page.
                    # Closed by default; user can open/close it with the expander.
                    with st.expander("🛋️ Smart Furniture Cost Estimator", expanded=False):
                        detail_furniture_result = furniture_estimator(
                            bhk, pred, compact=False, instance_id="budget_property_detail"
                        )
                        detail_furniture_subtotal = float(detail_furniture_result.get("subtotal", 0))
                        detail_furniture_installation = float(detail_furniture_result.get("installation", 0))
                        detail_furniture_total = float(detail_furniture_result.get("total", 0))
                        st.session_state.furniture_cart_total = detail_furniture_total

            with t2:
                st.markdown(f"#### 📍 Neighborhood Highlights · {area_name}")
                st.caption(t("nearby_caption"))
                # Do not make the 5-km live OSM/Overpass request while the
                # Property Details page is opening. Streamlit executes all
                # st.tabs bodies on every rerun, even when the tab is not
                # selected. Load the live neighborhood data only on demand.
                nh_load_key = f"load_neighborhood_{city}_{area_name}"
                if st.button("📍 Load Live Neighborhood Highlights", key=nh_load_key, use_container_width=True):
                    st.session_state[f"nh_force_load_{city}_{area_name}"] = True
                    smart_rerun()
                if st.session_state.get(f"nh_force_load_{city}_{area_name}", False):
                    render_advanced_neighborhood(area_name, city)
                else:
                    st.info("Neighborhood data is ready to load. Click the button above to fetch real named places within 5 km.")

                similar_b = get_similar_properties(df, city, area_name, bhk, final_price)
                if not similar_b.empty:
                    st.markdown(f"#### {t('similar_props')}")
                    sim_cols_b = st.columns(len(similar_b))
                    for i, (_, srow) in enumerate(similar_b.iterrows()):
                        with sim_cols_b[i]:
                            st.markdown(f'''
                            <div class="intel-box" style="padding:15px; text-align:center;">
                                <div style="font-weight:800; color:{text_col};">{srow['Area_Name']}</div>
                                <div style="color:{sub_text}; font-size:12px; margin-bottom:8px;">{srow['City']} • {int(srow['BHK'])} BHK</div>
                                <div style="color:#daa752; font-size:18px; font-weight:800;">{format_price(srow['Price'])}</div>
                            </div>
                            ''', unsafe_allow_html=True)

            with t3:
                st.markdown(f"#### 📧 Send Automated Email Inquiry")
                st.caption("Your inquiry will be received immediately. A premium thank-you email will be sent to your email after about 25 seconds.")
                with st.form("b_email_inquiry_form", clear_on_submit=True):
                    e_col1, e_col2 = st.columns(2)
                    with e_col1: cust_name = st.text_input(t("full_name"))
                    with e_col2: cust_email = st.text_input(t("email"))
                    cust_phone = st.text_input(t("phone"))
                    if st.form_submit_button("📩 Send Inquiry", use_container_width=True):
                        if cust_name and cust_email:
                            scheduled, email_status = _schedule_inquiry_thank_you_email(
                                cust_email, cust_name, cust_phone, city, area_name,
                                bhk, area, final_price, bathrooms=bathrooms,
                                amenities={"Gym": bool(g), "Swimming Pool": bool(p), "Cinema Hall": bool(c), "Gaming Zone": bool(gam), "Solar Balcony": bool(s)},
                                property_title=f"{bhk} BHK Premium Property • {area_name}, {city}"
                            )
                            if scheduled:
                                st.success(f"✅ Inquiry received! Thank you, {cust_name}. Gmail verified. Your SmartEstate thank-you email will arrive at {cust_email} in about 25 seconds.")
                            else:
                                st.error(f"❌ Email was NOT scheduled. {email_status}")
                                st.caption("Your inquiry is still received. Keep your Gmail App Password private; do not paste it into chat.")
                        else: st.error(t("fill_name_email"))

            with t4:
                roi_left, roi_right = st.columns([1.5, 1])
                with roi_left:
                    st.markdown('<div class="intel-box animate-up delay-1"><div class="card-header">📈 10-Year Price Projection (AI Forecast)</div>', unsafe_allow_html=True)
                    chart_font_color = "#f8fafc" if st.session_state.dark_mode else "#0f172a"
                    grid_color = "rgba(255,255,255,0.1)" if st.session_state.dark_mode else "rgba(0,0,0,0.1)"
                    years = list(range(2026, 2037))
                    future_prices = [final_price * (1.065 ** i) for i in range(11)]
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=years, y=future_prices, fill='tozeroy', mode='lines+markers', line=dict(color='#daa752', width=3), marker=dict(size=8, color='#daa752'), fillcolor='rgba(218, 167, 82, 0.22)'))
                    fig.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', margin=dict(l=0, r=0, t=10, b=0), xaxis=dict(showgrid=False, color=chart_font_color), yaxis=dict(showgrid=True, gridcolor=grid_color, color=chart_font_color, title="Est. Value (Lakhs)"), height=270)
                    st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})
                    st.markdown('</div>', unsafe_allow_html=True)

                with roi_right:
                    st.markdown('<div class="intel-box animate-up delay-2"><div class="card-header">🏦 EMI Calculator</div>', unsafe_allow_html=True)
                    detail_dp = st.slider(t("downpayment"), 10, 50, 20, key="detail_emi_dp")
                    detail_tenure = st.slider(t("loan_tenure"), 5, 30, 20, key="detail_emi_tenure")
                    detail_interest = st.number_input(t("interest_rate"), value=8.5, step=0.1, key="detail_emi_interest")
                    detail_project_cost = final_price * 100000
                    detail_loan = detail_project_cost * (1 - detail_dp / 100)
                    detail_emi = calculate_emi(detail_loan, detail_interest, detail_tenure)
                    st.metric("Monthly EMI", f"₹{detail_emi:,.0f}")
                    st.caption(f"Property project cost: ₹{detail_project_cost/100000:,.2f} L")
                    st.markdown('</div>', unsafe_allow_html=True)

                st.markdown('<div style="margin-top:18px;"></div>', unsafe_allow_html=True)
                st.markdown('<div class="intel-box animate-up delay-3"><div class="card-header">💰 Total Purchase Cost Planner</div>', unsafe_allow_html=True)
                pc1, pc2, pc3 = st.columns(3)
                with pc1:
                    detail_stamp = st.number_input("Stamp Duty % (estimate)", min_value=0.0, max_value=15.0, value=5.0, step=0.25, key="detail_stamp_pct")
                with pc2:
                    detail_reg = st.number_input("Registration % (estimate)", min_value=0.0, max_value=10.0, value=1.0, step=0.25, key="detail_reg_pct")
                with pc3:
                    detail_other = st.number_input("Other Charges (₹)", min_value=0.0, step=5000.0, value=0.0, key="detail_other_rs")
                detail_fin = calculate_total_purchase_cost(final_price, detail_dp, detail_interest, detail_tenure, detail_stamp, detail_reg, detail_other)
                fm1, fm2, fm3, fm4 = st.columns(4)
                fm1.metric("Upfront Cash", f"₹{detail_fin['upfront_cash']:,.0f}")
                fm2.metric("Loan", f"₹{detail_fin['loan_amount']:,.0f}")
                fm3.metric("Monthly EMI", f"₹{detail_fin['emi']:,.0f}")
                fm4.metric("Total Cost", f"₹{detail_fin['total_acquisition']:,.0f}")
                st.caption("Stamp duty, registration and other charges are editable estimates; verify actual charges before purchase.")
                st.markdown('</div>', unsafe_allow_html=True)

                st.markdown('<div style="margin-top:18px;"></div>', unsafe_allow_html=True)
                st.markdown('<div class="intel-box animate-up delay-3"><div class="card-header">📊 Area Price Trend</div>', unsafe_allow_html=True)
                render_area_price_trend(df, city, area_name, current_price=final_price)
                st.markdown('</div>', unsafe_allow_html=True)

                st.markdown('<div style="margin-top:18px;"></div>', unsafe_allow_html=True)
                st.markdown('<div class="intel-box animate-up delay-3"><div class="card-header">💎 Smart Investment Analyzer</div>', unsafe_allow_html=True)
                inv1, inv2, inv3 = st.columns(3)
                with inv1:
                    rent_default = max(5000, min(100000, float(final_price) * 100000 * 0.035 / 12))
                    detail_rent = st.number_input("Expected Monthly Rent (₹)", min_value=0.0, value=float(round(rent_default/500)*500), step=500.0, key="detail_investment_rent")
                with inv2:
                    detail_app = st.number_input("Annual Appreciation %", min_value=-10.0, max_value=20.0, value=6.5, step=0.5, key="detail_investment_app")
                with inv3:
                    detail_rg = st.number_input("Annual Rent Growth %", min_value=-10.0, max_value=20.0, value=5.0, step=0.5, key="detail_investment_rg")
                detail_inv = calculate_investment_analysis(final_price, detail_rent, detail_app, detail_rg, 10)
                iv1, iv2, iv3, iv4 = st.columns(4)
                iv1.metric("Rental Yield", f"{detail_inv['rental_yield']:.2f}%")
                iv2.metric("Monthly Rent", f"₹{detail_inv['monthly_rent']:,.0f}")
                iv3.metric("5-Year Value", f"₹{detail_inv['future_5']/100000:,.1f} L")
                iv4.metric("10-Year Value", f"₹{detail_inv['future_10']/100000:,.1f} L")
                st.markdown(f"<div style='display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:12px;'><div style='padding:12px;border-radius:12px;background:rgba(218,167,82,.08);'><small>10Y Rent Income</small><br><b>₹{detail_inv['cumulative_rent']/100000:,.1f} L</b></div><div style='padding:12px;border-radius:12px;background:rgba(218,167,82,.08);'><small>10Y Capital Gain</small><br><b>₹{detail_inv['capital_gain']/100000:,.1f} L</b></div><div style='padding:12px;border-radius:12px;background:rgba(218,167,82,.08);'><small>Modelled 10Y ROI</small><br><b>{detail_inv['roi']:.1f}%</b></div></div>", unsafe_allow_html=True)
                st.caption("Modelled scenario only — actual rent, appreciation, vacancy, taxes, maintenance and transaction costs can differ.")
                st.markdown('</div>', unsafe_allow_html=True)

                if HAS_FPDF:
                    # PDF creation used to run on every Property Details rerun.
                    # That also triggered a live 5-km Overpass request, making
                    # View Details feel slow even though the user had not asked
                    # for a PDF yet. Generate the report only when requested.
                    if st.button("📄 Prepare Full Property Report (PDF)", key="prepare_property_detail_pdf", use_container_width=True):
                        with st.spinner("Preparing your property report..."):
                            facilities_for_report = st.session_state.get(f"nh_facilities_{city}_{area_name}")
                            if facilities_for_report is None:
                                facilities_for_report = []
                            detail_amenities = [k for k, v in {"Gym": g, "Swimming Pool": p, "Cinema Hall": c, "Gaming Zone": gam, "Solar Balcony": s}.items() if v]
                            detail_score = calculate_property_score(
                                df, city, area_name, bhk, area, final_price,
                                {"Gym": g, "Swimming Pool": p, "Cinema Hall": c, "Gaming Zone": gam, "Solar Balcony": s},
                                safety_score=saf_score
                            )
                            detail_report_bytes = build_property_pdf_report(city, area_name, bhk, bathrooms, area, final_price, detail_score, detail_fin, detail_amenities, facilities_for_report, "Current area profile and modelled 6.5% annual trend", customer=st.session_state.get("auth_user"))
                            st.session_state.property_detail_report_bytes = detail_report_bytes
                    if st.session_state.get("property_detail_report_bytes"):
                        _protected_download_button("📄 Download Full Property Report (PDF)", data=st.session_state.property_detail_report_bytes, file_name=f"Property_Report_{area_name}.pdf", mime="application/pdf", key="property_detail_full_pdf", action="download this property report")

            with t5:
                # Render the complete visual section immediately. The map itself is
                # deliberately property-only, so it no longer triggers a 5-km API call.
                m_col1, m_col2 = st.columns(2)
                with m_col1:
                    st.markdown(f"#### 📐 Architectural Presentation Board ({bhk} BHK)")
                    st.caption(t("reference_style"))
                    d1, d2 = st.columns(2)
                    with d1:
                        design_style = st.selectbox(t("property_design"), ["Modern Luxury", "Contemporary", "Classic Premium"], key="p_3d_style")
                    with d2:
                        view_mode = st.selectbox(t("view"), ["Exterior", "Interior", "Floor Plan"], key="p_3d_view")
                    render_architectural_presentation_board(bhk, area, g, p, c, gam, s, design_style, view_mode)
                with m_col2:
                    st.markdown(f"#### 📍 Exact Location Map")
                    st.markdown('<div class="animate-up delay-2" style="border-radius:16px; overflow:hidden; border:2px solid rgba(255,255,255,0.1);">', unsafe_allow_html=True)
                    render_real_map(area_name, city, height=500)
                    st.markdown('</div>', unsafe_allow_html=True)

# ==========================================
# PAGE 3: 🔖 SAVED SEARCHES
# ==========================================
# ==========================================
# PAGE 3: 🔖 SAVED SEARCHES
# ==========================================
elif current_page == "saved_searches":
    st.markdown('<div class="hero-banner"><div class="hero-content"><div class="hero-eyebrow">Smart Search Library</div><h1 class="hero-title">🔖 Saved<br><span class="gradient-text">Searches</span></h1><div class="hero-sub">Save your budget and location filters and reopen them in one click.</div></div></div>', unsafe_allow_html=True)

    if not st.session_state.get("auth_user"):
        st.info("🔐 Saved Searches are personal. Browse Budget-First Search freely, then login when you want to save a search.")
        if st.button("🔐 Login / Sign Up", key="saved_search_login", use_container_width=True):
            _open_auth_prompt("save your searches")
            st.rerun()
    elif not st.session_state.saved_searches:
        st.info("No saved searches yet. Open Budget-First Search, set your filters, and click 🔖 Save Search.")
    else:
        for idx, saved in enumerate(st.session_state.saved_searches):
            with st.container(border=True):
                left, right = st.columns([2.2, 1.2])
                with left:
                    st.markdown(f"### 🔖 {html.escape(str(saved.get('name','My Search')))}")
                    st.caption(f"Saved {saved.get('saved_at','')} • {saved.get('user_city','All Cities')} • {saved.get('user_locality','All Localities')} • BHK {saved.get('user_req_bhk','Any')}")
                    st.write(f"Budget: ₹{float(saved.get('min_b',0)):.1f}L - ₹{float(saved.get('max_b',0)):.1f}L  •  Minimum Area: {saved.get('user_area',0)} sq.ft")
                with right:
                    r1, r2 = st.columns(2)
                    with r1:
                        if st.button("▶️ Run", key=f"run_saved_search_{idx}", use_container_width=True):
                            st.session_state.b_search_params = {k:v for k,v in saved.items() if k not in {"name","saved_at","signature"}}
                            st.session_state.search_executed = True
                            st.session_state.pending_nav = PAGES["budget"]
                            smart_rerun()
                    with r2:
                        if st.button("🗑️ Delete", key=f"delete_saved_search_{idx}", use_container_width=True):
                            st.session_state.saved_searches.pop(idx)
                            _persist_account_data(saved_searches=[dict(x) for x in st.session_state.saved_searches])
                            smart_rerun()

# ==========================================
# PAGE 4: ⚖️ COMPARE PROPERTIES (SPA FLOW)
# ==========================================
elif current_page == "compare":
    
    def sync_comp_a():
        b = st.session_state.ui_ca_bhk
        st.session_state.ui_ca_sqft = {1: 650, 2: 1100, 3: 1500, 4: 2500, 5: 3500, 6: 4500}.get(b, 1500)

    def sync_comp_b():
        b = st.session_state.ui_cb_bhk
        st.session_state.ui_cb_sqft = {1: 650, 2: 1100, 3: 1500, 4: 2500, 5: 3500, 6: 4500}.get(b, 1500)

    if not st.session_state.c_compare_executed:
        st.markdown('<div class="hero-banner"><div class="hero-content"><h1 class="hero-title">⚖️ Compare<br><span class="gradient-text">Properties</span></h1><div class="hero-sub">Deep AI Analysis to find the best value for your money.</div></div></div>', unsafe_allow_html=True)
        if not model: st.warning(t("model_missing")); st.stop()
        
        st.markdown('<div class="intel-box animate-up delay-1" style="padding: 30px;">', unsafe_allow_html=True)
        
        c_left, c_mid, c_right = st.columns([1, 0.1, 1])
        
        with c_left:
            st.markdown('<div class="card-header" style="color:#daa752; border:none; margin-top:0px; font-size: 20px;">🏠 Property A</div>', unsafe_allow_html=True)
            city_A = st.selectbox(t("city_a"), list(CITY_AREAS.keys()), key="ca_city")
            area_A = st.selectbox(t("locality_a"), CITY_AREAS[city_A], key="ca_area")
            col_s1, col_s2 = st.columns(2)
            with col_s2: bhk_A = st.selectbox(t("bhk_a"), [1,2,3,4,5,6], index=2, key="ui_ca_bhk", on_change=sync_comp_a)
            with col_s1: sqft_A = st.number_input(t("area_a"), value=st.session_state.ui_ca_sqft, step=50, key="ui_ca_sqft")
            
        with c_mid:
            st.markdown('<div style="height: 100%; border-right: 1px solid rgba(218,167,82,0.22); margin-top: 15px;"></div>', unsafe_allow_html=True)
            
        with c_right:
            st.markdown('<div class="card-header" style="color:#daa752; border:none; margin-top:0px; font-size: 20px;">🏠 Property B</div>', unsafe_allow_html=True)
            city_B = st.selectbox(t("city_b"), list(CITY_AREAS.keys()), key="cb_city")
            area_B = st.selectbox(t("locality_b"), CITY_AREAS[city_B], key="cb_area")
            col_s3, col_s4 = st.columns(2)
            with col_s4: bhk_B = st.selectbox(t("bhk_b"), [1,2,3,4,5,6], index=2, key="ui_cb_bhk", on_change=sync_comp_b)
            with col_s3: sqft_B = st.number_input(t("area_b"), value=st.session_state.ui_cb_sqft, step=50, key="ui_cb_sqft")
            
        st.markdown('<div style="margin-top:20px;"></div>', unsafe_allow_html=True)
        st.markdown("### ⚙️ Investment Assumptions")
        ia,ib=st.columns(2)
        with ia:
            rent_A=st.number_input("Property A — Expected Monthly Rent (₹)",min_value=0,value=20000,step=1000,key="compare_rent_a")
            app_A=st.number_input("Property A — Annual Appreciation %",0.0,30.0,6.5,0.5,key="compare_app_a")
            rg_A=st.number_input("Property A — Annual Rent Growth %",0.0,30.0,5.0,0.5,key="compare_rg_a")
        with ib:
            rent_B=st.number_input("Property B — Expected Monthly Rent (₹)",min_value=0,value=20000,step=1000,key="compare_rent_b")
            app_B=st.number_input("Property B — Annual Appreciation %",0.0,30.0,6.5,0.5,key="compare_app_b")
            rg_B=st.number_input("Property B — Annual Rent Growth %",0.0,30.0,5.0,0.5,key="compare_rg_b")
        compare_btn=st.button("⚖️ Compare These Properties",use_container_width=True)
        st.markdown('</div>', unsafe_allow_html=True)
            
        if compare_btn:
            st.session_state.c_params = {
                'city_A': city_A, 'area_A': area_A, 'sqft_A': sqft_A, 'bhk_A': bhk_A, 'rent_A': rent_A, 'app_A': app_A, 'rent_growth_A': rg_A,
                'city_B': city_B, 'area_B': area_B, 'sqft_B': sqft_B, 'bhk_B': bhk_B, 'rent_B': rent_B, 'app_B': app_B, 'rent_growth_B': rg_B
            }
            record_comparison_history(
                {"City": city_A, "Area_Name": area_A, "BHK": bhk_A, "Area": sqft_A},
                {"City": city_B, "Area_Name": area_B, "BHK": bhk_B, "Area": sqft_B},
            )
            st.session_state.c_compare_executed = True
            smart_rerun()

    else:
        # ======================================================
        # BUDGET SEARCH -> COMPARE -> BACK TO SEARCH RESULTS
        # Keep the original Budget-First Search filters/results.
        # ======================================================
        # Show "Back to Search Results" ONLY when this comparison was launched
        # from Budget-First Search. A standalone Compare Properties session must
        # not show a misleading Search Results button.
        from_budget_search = st.session_state.get("compare_return_page") == "budget_results"

        if from_budget_search:
            back_col, modify_col = st.columns([1.25, 1], gap="small")
            with back_col:
                if st.button(
                    "⬅️ Back to Search Results",
                    key="compare_back_to_budget_results",
                    use_container_width=True,
                ):
                    # Return to the exact Budget-First Search result state.
                    # Do NOT clear b_search_params or search_executed.
                    st.session_state.c_compare_executed = False
                    st.session_state.compare_return_page = None
                    st.session_state.compare_selection = []
                    st.session_state.viewing_property = None
                    st.session_state.budget_inline_detail_key = None
                    st.session_state.pending_nav = PAGES["budget"]
                    st.session_state.search_executed = True
                    smart_rerun()

            with modify_col:
                if st.button(
                    "✏️ Modify Comparison",
                    key="compare_modify_2",
                    use_container_width=True,
                ):
                    modify_compare()
                    smart_rerun()
        else:
            # Standalone Compare Properties: only allow editing this comparison.
            if st.button(
                "✏️ Modify Comparison",
                key="compare_modify_standalone",
                use_container_width=True,
            ):
                modify_compare()
                smart_rerun()

        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

        cp=st.session_state.c_params
        city_A,area_A,sqft_A,bhk_A=cp['city_A'],cp['area_A'],cp['sqft_A'],cp['bhk_A']
        city_B,area_B,sqft_B,bhk_B=cp['city_B'],cp['area_B'],cp['sqft_B'],cp['bhk_B']
        bath_A={1:1,2:2,3:3,4:4,5:5,6:6}.get(bhk_A,3); bath_B={1:1,2:2,3:3,4:4,5:5,6:6}.get(bhk_B,3)
        # If comparison came from Budget-First Search, use the exact listing prices
        # selected by the user. Only the standalone Compare form uses prediction.
        listing_A = float(cp.get('listing_price_A', 0) or 0)
        listing_B = float(cp.get('listing_price_B', 0) or 0)
        val_A = listing_A if listing_A > 0 else cp.get('val_A', get_prediction(city_A, area_A, sqft_A, bhk_A, bath_A, 0, 0, 0, 0, 0))
        val_B = listing_B if listing_B > 0 else cp.get('val_B', get_prediction(city_B, area_B, sqft_B, bhk_B, bath_B, 0, 0, 0, 0, 0))
        rent_A,rent_B=cp.get('rent_A',20000),cp.get('rent_B',20000)
        app_A,app_B=cp.get('app_A',6.5),cp.get('app_B',6.5); rg_A,rg_B=cp.get('rent_growth_A',5.0),cp.get('rent_growth_B',5.0)
        inv_A=calculate_investment_analysis(val_A,rent_A,app_A,rg_A,10); inv_B=calculate_investment_analysis(val_B,rent_B,app_B,rg_B,10)
        cost_A=calculate_total_purchase_cost(val_A,20,8.5,20,5.0,1.0,0); cost_B=calculate_total_purchase_cost(val_B,20,8.5,20,5.0,1.0,0)
        safety_A,safety_B=SAFETY_SCORES.get(area_A,8.2),SAFETY_SCORES.get(area_B,8.2)
        def match_score(price,sqft,bhk,safety):
            price_component=max(0,min(10,10-(price/max(val_A,val_B,1))*4)); size_component=max(0,min(10,sqft/200)); config_component=max(0,min(10,bhk*1.6))
            return round(min(100,max(0,price_component*2.5+size_component*2+config_component*1.5+safety*2)),1)
        match_A,match_B=match_score(val_A,sqft_A,bhk_A,safety_A),match_score(val_B,sqft_B,bhk_B,safety_B)
        # =========================================================
        # PREMIUM PROPERTY COMPARE RESULT — card based, responsive
        # =========================================================
        st.markdown("""
        <style>
        .compare-result-hero{
            padding:26px 28px;
            border-radius:26px;
            background:linear-gradient(135deg,#fffaf2 0%,#f6ead9 52%,#efe0ca 100%);
            border:1px solid #e5cfad;
            box-shadow:0 16px 42px rgba(91,63,35,.10);
            margin:12px 0 22px;
        }
        .compare-result-kicker{font-size:11px;letter-spacing:2px;font-weight:900;color:#9b6b32;text-transform:uppercase;margin-bottom:7px}
        .compare-result-title{font-size:31px;line-height:1.1;font-weight:900;color:#3f2d20;margin:0}
        .compare-result-sub{color:#756454;margin-top:8px;font-size:14px}
        .compare-home-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin:0 0 22px}
        .compare-home-card{
            position:relative;padding:22px;border-radius:22px;background:#fffdf9;
            border:1px solid #eadbc7;box-shadow:0 10px 28px rgba(91,63,35,.08);
            overflow:hidden;
        }
        .compare-home-card:before{content:"";position:absolute;left:0;top:0;right:0;height:4px;background:linear-gradient(90deg,#8b5e3c,#d6a04b)}
        .compare-home-tag{font-size:11px;font-weight:900;letter-spacing:1.4px;color:#a17138;text-transform:uppercase}
        .compare-home-name{font-size:23px;font-weight:900;color:#3c2b20;margin:5px 0 2px}
        .compare-home-city{font-size:12px;color:#887564}
        .compare-price{font-size:27px;font-weight:900;color:#b57a25;margin-top:16px}
        .compare-mini-row{display:flex;flex-wrap:wrap;gap:7px;margin-top:13px}
        .compare-chip{padding:7px 10px;border-radius:999px;background:#f8f0e4;color:#5c4939;font-size:12px;font-weight:700;border:1px solid #eadbc7}
        .compare-section-title{font-size:18px;font-weight:900;color:#463326;margin:22px 0 12px}
        .compare-metric-grid{display:grid;grid-template-columns:1fr 1fr;gap:11px}
        .compare-metric{
            display:grid;grid-template-columns:minmax(105px,.85fr) 1.15fr minmax(105px,.85fr);
            align-items:stretch;gap:8px;padding:10px;border-radius:17px;background:#fffdf9;
            border:1px solid #eadfce;box-shadow:0 6px 18px rgba(91,63,35,.055)
        }
        .compare-value{display:flex;align-items:center;justify-content:center;min-height:50px;padding:8px;border-radius:13px;background:#fbf5eb;color:#4c392b;font-weight:850;font-size:14px;text-align:center}
        .compare-value-a{border-left:3px solid #b9894d}
        .compare-value-b{border-right:3px solid #8b6a4c}
        .compare-label{display:flex;align-items:center;justify-content:center;text-align:center;color:#6f5b49;font-size:11px;font-weight:900;line-height:1.25;text-transform:uppercase;letter-spacing:.35px}
        .compare-value-primary{font-size:16px;color:#a66f24;background:#fff7e8}
        .compare-note{margin-top:17px;padding:13px 16px;border-radius:14px;background:#f8f0e5;border:1px solid #ead9c2;color:#715d4a;font-size:12px;line-height:1.5}
        @media(max-width:800px){
            .compare-result-title{font-size:25px}.compare-home-grid{grid-template-columns:1fr}.compare-metric-grid{grid-template-columns:1fr}
            .compare-metric{grid-template-columns:1fr;gap:5px}.compare-label{order:-1;min-height:26px}.compare-value{min-height:43px}
        }
        </style>
        <div class="compare-result-hero">
          <div class="compare-result-kicker">Property Compare 2.0</div>
          <div class="compare-result-title">Your Two Homes. One Clear View.</div>
          <div class="compare-result-sub">A clean side-by-side view of price, space, financing and long-term investment projections.</div>
        </div>
        """, unsafe_allow_html=True)

        money=lambda x:f"₹{x:,.0f}"
        lakh=lambda x:f"₹{x:,.2f} L"
        rows=[
            ("💰 Estimated Property Price",lakh(val_A),lakh(val_B),True),
            ("📐 Area",f"{sqft_A:,.0f} sq.ft",f"{sqft_B:,.0f} sq.ft",False),
            ("🛏️ BHK",str(bhk_A),str(bhk_B),False),
            ("🚿 Bathrooms",str(bath_A),str(bath_B),False),
            ("🛡️ Safety Profile",f"{safety_A:.1f}/10",f"{safety_B:.1f}/10",False),
            ("🎯 Smart Match",f"{match_A:.1f}%",f"{match_B:.1f}%",True),
            ("📈 Rental Yield",f"{inv_A['rental_yield']:.2f}%",f"{inv_B['rental_yield']:.2f}%",False),
            ("💳 Down Payment",money(cost_A['downpayment']),money(cost_B['downpayment']),False),
            ("🏦 Loan Amount",money(cost_A['loan_amount']),money(cost_B['loan_amount']),False),
            ("📅 Monthly EMI",money(cost_A['emi']),money(cost_B['emi']),True),
            ("💵 Upfront Cash",money(cost_A['upfront_cash']),money(cost_B['upfront_cash']),False),
            ("💸 Total Interest",money(cost_A['total_interest']),money(cost_B['total_interest']),False),
            ("🏷️ Total Acquisition Cost",money(cost_A['total_acquisition']),money(cost_B['total_acquisition']),True),
            ("🔮 5-Year Value",money(inv_A['future_5']),money(inv_B['future_5']),False),
            ("🚀 10-Year Value",money(inv_A['future_10']),money(inv_B['future_10']),True),
            ("🏘️ 10-Year Rental Income",money(inv_A['cumulative_rent']),money(inv_B['cumulative_rent']),False),
            ("📊 Modelled 10-Year ROI",f"{inv_A['roi']:.1f}%",f"{inv_B['roi']:.1f}%",True),
        ]

        st.markdown(f"""
        <div class="compare-home-grid">
          <div class="compare-home-card">
            <div class="compare-home-tag">Property A</div>
            <div class="compare-home-name">🏠 {html.escape(str(area_A))}</div>
            <div class="compare-home-city">{html.escape(str(city_A))}</div>
            <div class="compare-price">{html.escape(lakh(val_A))}</div>
            <div class="compare-mini-row">
              <span class="compare-chip">🛏️ {bhk_A} BHK</span>
              <span class="compare-chip">📐 {sqft_A:,.0f} sq.ft</span>
              <span class="compare-chip">🛡️ {safety_A:.1f}/10 safety</span>
              <span class="compare-chip">🎯 {match_A:.1f}% match</span>
            </div>
          </div>
          <div class="compare-home-card">
            <div class="compare-home-tag">Property B</div>
            <div class="compare-home-name">🏠 {html.escape(str(area_B))}</div>
            <div class="compare-home-city">{html.escape(str(city_B))}</div>
            <div class="compare-price">{html.escape(lakh(val_B))}</div>
            <div class="compare-mini-row">
              <span class="compare-chip">🛏️ {bhk_B} BHK</span>
              <span class="compare-chip">📐 {sqft_B:,.0f} sq.ft</span>
              <span class="compare-chip">🛡️ {safety_B:.1f}/10 safety</span>
              <span class="compare-chip">🎯 {match_B:.1f}% match</span>
            </div>
          </div>
        </div>
        <div class="compare-section-title">📊 Detailed Comparison</div>
        <div class="compare-metric-grid">
        """, unsafe_allow_html=True)

        # Render metrics in compact, premium comparison cards.
        for label, a, b, primary in rows:
            primary_cls=" compare-value-primary" if primary else ""
            st.markdown(f"""
            <div class="compare-metric">
              <div class="compare-value compare-value-a{primary_cls}">{html.escape(a)}</div>
              <div class="compare-label">{html.escape(label)}</div>
              <div class="compare-value compare-value-b{primary_cls}">{html.escape(b)}</div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)
        st.markdown("""
        <div class="compare-note">
          ℹ️ Investment projections are modelled estimates based on the selected assumptions. They are not guaranteed returns.
        </div>
        """, unsafe_allow_html=True)

        # =========================================================
        # DOWNLOAD PDF REPORTS — COMBINED COMPARISON + INDIVIDUAL REPORTS
        # Generate only when the user clicks, so Compare opens fast.
        # =========================================================
        if HAS_FPDF:
            st.markdown("### 📄 Property Comparison PDF")
            st.caption("Download one PDF containing Property A + Property B side-by-side comparison and financial comparison.")

            if st.button("📊 Generate & Download Both Properties Comparison PDF", key="compare_prepare_combined_pdf", use_container_width=True):
                score_a = calculate_property_score(
                    df, city_A, area_A, bhk_A, sqft_A, val_A,
                    {"Gym": 0, "Swimming Pool": 0, "Cinema Hall": 0, "Gaming Zone": 0, "Solar Balcony": 0},
                    safety_score=safety_A,
                )
                score_b = calculate_property_score(
                    df, city_B, area_B, bhk_B, sqft_B, val_B,
                    {"Gym": 0, "Swimming Pool": 0, "Cinema Hall": 0, "Gaming Zone": 0, "Solar Balcony": 0},
                    safety_score=safety_B,
                )
                financial_a = calculate_total_purchase_cost(val_A, 20, 8.5, 20, 5.0, 1.0, 0)
                financial_b = calculate_total_purchase_cost(val_B, 20, 8.5, 20, 5.0, 1.0, 0)
                st.session_state.compare_pdf_combined = build_compare_pdf_report(
                    city_A, area_A, bhk_A, bath_A, sqft_A, val_A, score_a, financial_a,
                    city_B, area_B, bhk_B, bath_B, sqft_B, val_B, score_b, financial_b,
                    customer=st.session_state.get("auth_user"),
                    investment_a={**inv_A, "appreciation": app_A, "rent_growth": rg_A},
                    investment_b={**inv_B, "appreciation": app_B, "rent_growth": rg_B},
                    match_a=match_A, match_b=match_B,
                )

            if st.session_state.get("compare_pdf_combined"):
                _protected_download_button(
                    "⬇️ Download Both Properties Comparison PDF",
                    data=st.session_state.compare_pdf_combined,
                    file_name=f"SmartEstate_Property_Comparison_{area_A}_vs_{area_B}.pdf",
                    mime="application/pdf",
                    key="compare_download_combined_pdf",
                    action="download both properties comparison PDF",
                )

            st.markdown("### 📄 Individual Property Reports")
            st.caption("Optional: download a separate detailed report for Property A or Property B.")

            pdf_a_col, pdf_b_col = st.columns(2, gap="medium")

            with pdf_a_col:
                if st.button("📄 Prepare Property A PDF", key="compare_prepare_pdf_a", use_container_width=True):
                    score_a = calculate_property_score(
                        df, city_A, area_A, bhk_A, sqft_A, val_A,
                        {"Gym": 0, "Swimming Pool": 0, "Cinema Hall": 0, "Gaming Zone": 0, "Solar Balcony": 0},
                        safety_score=safety_A,
                    )
                    financial_a = calculate_total_purchase_cost(val_A, 20, 8.5, 20, 5.0, 1.0, 0)
                    st.session_state.compare_pdf_a = build_property_pdf_report(
                        city_A, area_A, bhk_A, bath_A, sqft_A, val_A, score_a,
                        financial_a, [], [],
                        "Current area profile and modelled investment assumptions",
                        customer=st.session_state.get("auth_user"),
                    )

                if st.session_state.get("compare_pdf_a"):
                    _protected_download_button(
                        "⬇️ Download Property A PDF",
                        data=st.session_state.compare_pdf_a,
                        file_name=f"Property_A_Report_{area_A}.pdf",
                        mime="application/pdf",
                        key="compare_download_pdf_a",
                        action="download Property A comparison report",
                    )

            with pdf_b_col:
                if st.button("📄 Prepare Property B PDF", key="compare_prepare_pdf_b", use_container_width=True):
                    score_b = calculate_property_score(
                        df, city_B, area_B, bhk_B, sqft_B, val_B,
                        {"Gym": 0, "Swimming Pool": 0, "Cinema Hall": 0, "Gaming Zone": 0, "Solar Balcony": 0},
                        safety_score=safety_B,
                    )
                    financial_b = calculate_total_purchase_cost(val_B, 20, 8.5, 20, 5.0, 1.0, 0)
                    st.session_state.compare_pdf_b = build_property_pdf_report(
                        city_B, area_B, bhk_B, bath_B, sqft_B, val_B, score_b,
                        financial_b, [], [],
                        "Current area profile and modelled investment assumptions",
                        customer=st.session_state.get("auth_user"),
                    )

                if st.session_state.get("compare_pdf_b"):
                    _protected_download_button(
                        "⬇️ Download Property B PDF",
                        data=st.session_state.compare_pdf_b,
                        file_name=f"Property_B_Report_{area_B}.pdf",
                        mime="application/pdf",
                        key="compare_download_pdf_b",
                        action="download Property B comparison report",
                    )

# ==========================================
# PAGE 4: 📊 ANALYTICS DASHBOARD
# ==========================================
elif current_page == "analytics":

    if not st.session_state.a_executed:
        st.markdown('<div class="hero-banner"><div class="hero-content"><h1 class="hero-title">Market<br><span class="gradient-text">Analytics</span></h1><div class="hero-sub">Generate real-time interactive data insights and market trends.</div></div></div>', unsafe_allow_html=True)
        if df.empty: 
            st.warning(t("no_dataset_analytics"))
            st.stop()
        
        st.markdown('<div class="intel-box animate-up delay-1" style="padding: 30px;">', unsafe_allow_html=True)
        st.markdown('<div class="card-header" style="color:#daa752; border:none; margin-top:0px; font-size: 20px;">📊 Select Market Parameters</div>', unsafe_allow_html=True)
        
        f_col1, f_col2, f_col3 = st.columns(3)
        with f_col1: selected_city = st.selectbox(t("analytics_city"), ["All Cities"] + sorted(df["City"].unique().tolist()))
        dash_df_tmp = df[df["City"] == selected_city] if selected_city != "All Cities" else df.copy()
        with f_col2: selected_areas = st.multiselect(t("analytics_areas"), sorted(dash_df_tmp["Area_Name"].unique().tolist()), default=st.session_state.a_sel_areas)
        with f_col3: selected_bhk = st.multiselect(t("analytics_bhk"), sorted(dash_df_tmp["BHK"].unique().tolist()), default=st.session_state.a_sel_bhk)
        
        st.markdown('<div style="margin-top:20px;"></div>', unsafe_allow_html=True)
        dash_btn = st.button(t("generate_analytics"), use_container_width=True)
        st.markdown('</div>', unsafe_allow_html=True)

        if dash_btn:
            st.session_state.a_sel_city = selected_city
            st.session_state.a_sel_areas = selected_areas
            st.session_state.a_sel_bhk = selected_bhk
            st.session_state.a_executed = True
            smart_rerun()

    else:
        st.markdown('<div class="btn-back">', unsafe_allow_html=True)
        st.button(t("modify_filters"), on_click=modify_analytics)
        st.markdown('</div>', unsafe_allow_html=True)

        dash_df = df[df["City"] == st.session_state.a_sel_city] if st.session_state.a_sel_city != "All Cities" else df.copy()
        if st.session_state.a_sel_areas: dash_df = dash_df[dash_df["Area_Name"].isin(st.session_state.a_sel_areas)]
        if st.session_state.a_sel_bhk: dash_df = dash_df[dash_df["BHK"].isin(st.session_state.a_sel_bhk)]
        
        chart_font_color = "#f8fafc" if st.session_state.dark_mode else "#0f172a"
        grid_color = "rgba(255,255,255,0.1)" if st.session_state.dark_mode else "rgba(0,0,0,0.1)"

        st.markdown(f"""
        <div class="hero-banner" style="padding: 40px; margin-bottom: 20px;">
            <div class="hero-content">
                <div class="hero-eyebrow">{t("market_analytics")}</div>
                <h1 class="hero-title">{t("live_data")} <span class="gradient-text">{t("insights")}</span></h1>
                <div class="hero-sub">Analyzing {len(dash_df)} properties in {st.session_state.a_sel_city}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        if not dash_df.empty:
            exp_area = dash_df.groupby('Area_Name')['Price'].mean().idxmax()
            chp_area = dash_df.groupby('Area_Name')['Price'].mean().idxmin()
            avg_pr = dash_df['Price'].mean()
            st.markdown(f'''
            <div class="intel-box animate-up delay-1" style="background: linear-gradient(135deg, rgba(220,176,101,0.1), rgba(16,185,129,0.05)); border-color: #daa752;">
                <div class="card-header" style="border:none; margin:0; color:#daa752;">🤖 Executive Market Summary</div>
                <p style="color:{text_col}; font-size:15px; margin-top:10px; line-height:1.6;">
                    Based on current filters, the average property price is <b>₹{avg_pr:,.2f} Lakhs</b>. 
                    <b>{exp_area}</b> is currently the most premium real estate market, strictly favoring sellers. 
                    Conversely, <b>{chp_area}</b> offers the highest affordability and acts as a buyer's market. 
                    Investing here yields an estimated <b>6.5% to 8% annual appreciation</b>.
                </p>
            </div>
            ''', unsafe_allow_html=True)

        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f'<div class="metric-card animate-up delay-1"><div class="num">{len(dash_df):,}</div><div class="lbl">Properties Analyzed</div></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="metric-card animate-up delay-2"><div class="num">₹{dash_df["Price"].mean():.1f} L</div><div class="lbl">Average Price</div></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="metric-card animate-up delay-3"><div class="num">₹{dash_df["Price"].max():.1f} L</div><div class="lbl">Peak Valuation</div></div>', unsafe_allow_html=True)
        
        avg_sqft_price = (dash_df["Price"].mean() * 100000) / dash_df["Area"].mean() if not dash_df.empty else 0
        c4.markdown(f'<div class="metric-card animate-up delay-3"><div class="num">₹{avg_sqft_price:,.0f}</div><div class="lbl">Avg Price / Sq.Ft</div></div>', unsafe_allow_html=True)
        
        st.write("---")

        st.markdown('<div class="intel-box animate-up delay-2"><div class="card-header" style="border:none; margin-top:0px;">🔮 10-Year Wealth Forecast (Market Trend)</div>', unsafe_allow_html=True)
        if not dash_df.empty:
            base_price = dash_df['Price'].mean()
            years = list(range(2026, 2037))
            y_opt = [base_price * (1.085**i) for i in range(11)]
            y_exp = [base_price * (1.065**i) for i in range(11)]
            
            fig_line = go.Figure()
            fig_line.add_trace(go.Scatter(x=years, y=y_opt, fill='tozeroy', mode='lines+markers', name='Optimistic (8.5%)',
                                        line=dict(color='#10b981', width=3), fillcolor='rgba(16, 185, 129, 0.1)'))
            fig_line.add_trace(go.Scatter(x=years, y=y_exp, fill='tozeroy', mode='lines+markers', name='Expected (6.5%)',
                                        line=dict(color='#daa752', width=3), fillcolor='rgba(218, 167, 82, 0.22)'))
            
            fig_line.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', 
                                font=dict(color=chart_font_color), hovermode="x unified",
                                xaxis=dict(showgrid=False, title="Year"), 
                                yaxis=dict(showgrid=True, gridcolor=grid_color, title="Average Value (Lakhs ₹)"), margin=dict(t=20, b=20, l=0, r=0))
            st.plotly_chart(fig_line, use_container_width=True, config={'displayModeBar': False})
        st.markdown('</div>', unsafe_allow_html=True)

        col1, col2 = st.columns(2)
        with col1:
            st.markdown('<div class="intel-box animate-up delay-2"><div class="card-header" style="margin-top:0px; border:none;">💎 Amenity Premium Impact</div>', unsafe_allow_html=True)
            if not dash_df.empty:
                amenity_impact = {}
                for am, col in [("Gym", "Gym"), ("Pool", "Swimming_Pool"), ("Cinema", "Cinema_Hall"), ("Solar", "Solar_Balcony")]:
                    if col in dash_df.columns:
                        with_am = dash_df[dash_df[col] == 1]['Price'].mean()
                        without_am = dash_df[dash_df[col] == 0]['Price'].mean()
                        if pd.notna(with_am) and pd.notna(without_am):
                            impact = with_am - without_am
                            amenity_impact[am] = max(0, impact) 
                
                if amenity_impact:
                    am_df = pd.DataFrame(list(amenity_impact.items()), columns=['Amenity', 'Added Value (Lakhs)'])
                    fig_bar1 = px.bar(am_df, x='Amenity', y='Added Value (Lakhs)', color='Amenity', 
                                      color_discrete_sequence=['#daa752', '#10b981', '#9A6A46', '#ef4444'])
                    fig_bar1.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', 
                                           font=dict(color=chart_font_color), xaxis=dict(showgrid=False), yaxis=dict(showgrid=True, gridcolor=grid_color), showlegend=False, margin=dict(t=20, b=0, l=0, r=0))
                    st.plotly_chart(fig_bar1, use_container_width=True, config={'displayModeBar': False})
                else:
                    st.info(t("not_enough_amenity"))
            st.markdown('</div>', unsafe_allow_html=True)
            
        with col2:
            st.markdown('<div class="intel-box animate-up delay-3"><div class="card-header" style="margin-top:0px; border:none;">📊 Top 5 Premium Markets</div>', unsafe_allow_html=True)
            if not dash_df.empty:
                top_locs = dash_df.groupby("Area_Name")["Price"].mean().sort_values(ascending=True).tail(5).reset_index()
                fig_bar2 = px.bar(top_locs, y='Area_Name', x='Price', orientation='h', 
                                  color='Price', color_continuous_scale='Oryel')
                fig_bar2.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', 
                                       font=dict(color=chart_font_color), xaxis=dict(showgrid=True, gridcolor=grid_color, title="Avg Price (Lakhs)"), yaxis=dict(showgrid=False, title=""), coloraxis_showscale=False, margin=dict(t=20, b=0, l=0, r=0))
                st.plotly_chart(fig_bar2, use_container_width=True, config={'displayModeBar': False})
            st.markdown('</div>', unsafe_allow_html=True)

        st.markdown('<div class="intel-box animate-up delay-3" style="margin-top:20px;"><div class="card-header" style="margin-top:0px; border:none;">📈 Area vs Price Distribution (Market Spread)</div>', unsafe_allow_html=True)
        if not dash_df.empty:
            fig_scatter = px.scatter(dash_df, x="Area", y="Price", color="BHK", hover_name="Area_Name", 
                                     color_discrete_sequence=['#10b981', '#daa752', '#9A6A46', '#ef4444', '#C9974A'])
            fig_scatter.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', 
                                      font=dict(color=chart_font_color), xaxis=dict(showgrid=True, gridcolor=grid_color), 
                                      yaxis=dict(showgrid=True, gridcolor=grid_color), margin=dict(t=20, b=0, l=0, r=0))
            st.plotly_chart(fig_scatter, use_container_width=True, config={'displayModeBar': False})
        st.markdown('</div>', unsafe_allow_html=True)

        # Selected area trend panel — uses a real year field when present, otherwise a clearly labelled modelled trend.
        st.markdown('<div class="intel-box animate-up delay-2" style="margin-top:20px;"><div class="card-header" style="margin-top:0px;border:none;">📊 Selected Area Price Trend</div>', unsafe_allow_html=True)
        trend_area = None
        if st.session_state.a_sel_areas:
            trend_area = st.session_state.a_sel_areas[0]
        elif not dash_df.empty and "Area_Name" in dash_df.columns:
            trend_area = str(dash_df["Area_Name"].iloc[0])
        if trend_area:
            trend_city = st.session_state.a_sel_city if st.session_state.a_sel_city != "All Cities" else str(dash_df["City"].iloc[0])
            render_area_price_trend(df, trend_city, trend_area)
        else:
            st.caption("Select an area to view its price trend.")
        st.markdown('</div>', unsafe_allow_html=True)

# ==========================================
# PAGE 5: ❤️ MY FAVORITES
# ==========================================
elif current_page == "favorites":
    st.markdown(f"""
    <div class="hero-banner">
        <div class="hero-content">
            <div class="hero-eyebrow">{t("your_shortlist")}</div>
            <h1 class="hero-title">{t("favorites_title")}</h1>
            <div class="hero-sub">{t("favorites_sub")}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    if not st.session_state.get("auth_user"):
        st.info("🔐 Favorites are personal. Browse and find homes without login; login only when you want to save them.")
        if st.button("🔐 Login / Sign Up", key="favorites_login", use_container_width=True):
            _open_auth_prompt("save your favorite properties")
            st.rerun()
    elif not st.session_state.favorites:
        st.info(t("no_fav"))
    else:
        fcols = st.columns(3)
        for idx, fav in enumerate(st.session_state.favorites):
            img_url = FLOOR_PLANS.get(int(fav.get("BHK", 3)), FLOOR_PLANS[3])
            saf_score = SAFETY_SCORES.get(fav.get("Area_Name"), 8.2)
            with fcols[idx % 3]:
                st.markdown(f"""
                <div class="animate-up" style="background:{card_bg}; backdrop-filter: blur(10px); border:1px solid {border_col}; border-radius:16px 16px 0 0; border-bottom: none; box-shadow: 0 4px 15px rgba(0,0,0,0.05); overflow: hidden; margin-top: 15px;">
                    <div style="position: relative;">
                        <img src="{img_url}" style="width: 100%; height: 140px; object-fit: cover;">
                        <div style="position: absolute; top: 10px; right: 10px; background:rgba(239,68,68,0.15); color:#ef4444; padding:4px 10px; border-radius:20px; font-size:10px; font-weight:800;">❤️ Saved</div>
                    </div>
                    <div style="padding: 15px;">
                        <div style="margin-bottom: 8px;">
                            <b style="color:{text_col}; font-size:16px;">{fav.get('Area_Name')}</b><br>
                            <span style="color:{sub_text}; font-size:11px; font-weight:600;">{fav.get('City')}</span>
                        </div>
                        <div style="margin-bottom: 12px; color:{sub_text}; font-size:12px; font-weight:600; line-height:1.6;">
                            🛏️ {fav.get('BHK')} BHK &nbsp;•&nbsp; 📐 ~{fav.get('Area')} sq.ft<br>
                            🛡️ Safety Score: <span style="color:{text_col};">{saf_score}/10</span>
                        </div>
                        <div style="color:#daa752; font-size:22px; font-weight:800; text-shadow: 0 2px 5px rgba(218,167,82,0.22);">
                            {format_price(fav.get('Historical_Price', 0))}
                        </div>
                        <div style="color:{sub_text}; font-size:10px; margin-top:6px;">Saved on {fav.get('saved_at', '')}</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                vcol, rcol, dcol = st.columns(3)
                with vcol:
                    if st.button(t("revalue"), key=f"reval_{idx}", use_container_width=True):
                        st.session_state.req_city = fav.get("City")
                        st.session_state.req_area = fav.get("Area_Name")
                        st.session_state.ui_bhk = int(fav.get("BHK", 3))
                        st.session_state.ui_area = int(fav.get("Area", 1500))
                        st.session_state.ui_bath = {1:1,2:2,3:3,4:4,5:5,6:6}.get(int(fav.get("BHK", 3)), 3)
                        st.session_state.req_bhk = st.session_state.ui_bhk
                        st.session_state.req_sqft = st.session_state.ui_area
                        st.session_state.req_bath = st.session_state.ui_bath
                        st.session_state.p_executed = True
                        st.session_state.pending_nav = PAGES["prediction"]
                        smart_rerun()
                with rcol:
                    if st.button(t("remove_fav"), key=f"remove_fav_{idx}", use_container_width=True):
                        toggle_favorite(fav)
                        smart_rerun()
                with dcol:
                    if st.button("👁️ View Details", key=f"fav_view_{idx}", use_container_width=True):
                        open_property_view(fav.get("City"), fav.get("Area_Name"), int(fav.get("BHK", 3)), int(fav.get("Area", 1500)), float(fav.get("Historical_Price", 0)))
                        st.session_state.pending_nav = PAGES["budget"]
                        smart_rerun()
# ==========================================
# ==========================================
# PAGE 6: 👤 MY HOME ACCOUNT
# ==========================================
elif current_page == "account":
    # 👤 MY HOME — UNIFIED PREMIUM THEME
    # ==========================================
    st.markdown("""
    <style>
    .account-premium-shell { background:linear-gradient(145deg,#fffdf8 0%,#f7eddf 55%,#efe0ca 100%); border:1px solid rgba(138,90,24,.16); border-radius:28px; padding:28px 30px; box-shadow:0 20px 50px rgba(92,65,37,.10); margin:4px 0 20px; }
    .account-premium-eyebrow { font-size:11px; letter-spacing:2.5px; font-weight:900; color:#9a6a2f; text-transform:uppercase; }
    .account-premium-title { font-size:38px; line-height:1.08; font-weight:900; color:#3f2b1f; margin:7px 0 8px; }
    .account-premium-title span { color:#b87925; }
    .account-premium-sub { color:#7d6b5b; font-size:14px; line-height:1.6; }
    .account-premium-stat { background:rgba(255,253,249,.92); border:1px solid rgba(138,90,24,.14); border-radius:18px; padding:15px 16px; box-shadow:0 8px 22px rgba(92,65,37,.06); }
    .account-premium-stat b { color:#4b3424; font-size:22px; }
    .account-premium-stat small { display:block; color:#8b7867; font-weight:700; margin-top:3px; }
    .account-premium-panel { background:rgba(255,253,249,.92); border:1px solid rgba(138,90,24,.14); border-radius:22px; padding:20px; box-shadow:0 12px 28px rgba(92,65,37,.06); }
    .account-premium-panel h3 { color:#4b3424 !important; }
    .account-premium-panel .muted { color:#8b7867; }
    .account-premium-panel button { border-radius:13px !important; }
    .account-premium-shell + div [data-baseweb="tab-list"] { gap:6px; }
    .account-premium-shell + div [data-baseweb="tab"] { color:#705b49 !important; font-weight:800 !important; }
    .account-premium-shell + div [aria-selected="true"] { color:#8a5a18 !important; }
    </style>
    """, unsafe_allow_html=True)
    
    
    auth_user = st.session_state.get("auth_user") or {}
    if not auth_user:
        st.info("🔐 Please login to open your personal Home Account.")
        if st.button("🔐 Login / Sign Up", use_container_width=True, key="account_login_gate"):
            _open_auth_prompt("open your Home Account")
            st.rerun()
    else:
        name = html.escape(str(auth_user.get("name", "User")))
        email_addr = html.escape(str(auth_user.get("email", "")))
        phone = html.escape(str(auth_user.get("phone", "")))
        favorites = st.session_state.get("favorites", [])
        searches = st.session_state.get("saved_searches", [])
        recents = st.session_state.get("recently_viewed", [])
        valuations = st.session_state.get("valuation_history", [])
        comparisons = st.session_state.get("comparison_history", [])
        reports = st.session_state.get("report_history", [])

        st.markdown(f"""
        <div class="account-premium-shell">
            <div class="account-premium-eyebrow">MY HOME ACCOUNT</div>
            <div class="account-premium-title">Welcome back, <span>{name}</span> 👋</div>
            <div class="account-premium-sub">Your saved homes, searches, valuations and reports — all in one private SmartEstate workspace.</div>
        </div>
        """, unsafe_allow_html=True)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("❤️ Favorites", len(favorites))
        m2.metric("🔖 Saved Searches", len(searches))
        m3.metric("🕐 Recently Viewed", len(recents))
        m4.metric("📊 Valuations", len(valuations))

        tab_overview, tab_profile, tab_activity, tab_reports = st.tabs(["🏠 Overview", "👤 Profile", "🕐 Activity", "📄 Reports"])

        with tab_overview:
            left, right = st.columns([1.15, 0.85])
            with left:
                st.markdown('<div class="intel-box animate-up" style="padding:22px;"><div class="card-header">❤️ Favorite Homes</div>', unsafe_allow_html=True)
                if not favorites:
                    st.info("No favorite homes yet. Save a property and it will appear here.")
                else:
                    for fav in favorites[:5]:
                        st.markdown(
                            f"""<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid rgba(120,90,55,.12);">
                            <div><b style="color:{text_col};">{html.escape(str(fav.get('Area_Name','Property')))}</b><div style="font-size:11px;color:{sub_text};">{html.escape(str(fav.get('City','')))} • {int(fav.get('BHK',0))} BHK • {float(fav.get('Area',0)):.0f} sq.ft</div></div>
                            <div style="font-weight:900;color:#DAA752;">{format_price(fav.get('Historical_Price',0))}</div>
                            </div>""",
                            unsafe_allow_html=True,
                        )
                st.markdown('</div>', unsafe_allow_html=True)

            with right:
                st.markdown('<div class="intel-box animate-up" style="padding:22px;"><div class="card-header">🔖 Saved Searches</div>', unsafe_allow_html=True)
                if not searches:
                    st.info("No saved searches yet. Save a search from Budget-First Search.")
                else:
                    for item in searches[:5]:
                        st.markdown(
                            f"""<div style="padding:11px 0;border-bottom:1px solid rgba(120,90,55,.12);">
                            <b style="color:{text_col};">{html.escape(str(item.get('name','My Search')))}</b>
                            <div style="font-size:11px;color:{sub_text};">{html.escape(str(item.get('city','All Cities')))} • {html.escape(str(item.get('locality','All Localities')))} • BHK {html.escape(str(item.get('bhk','Any')))}</div>
                            </div>""",
                            unsafe_allow_html=True,
                        )
                st.markdown('</div>', unsafe_allow_html=True)

            st.markdown('### 🏡 Recently Viewed')
            if not recents:
                st.info("Your recently viewed properties will appear here after you explore listings while logged in.")
            else:
                rc = st.columns(min(5, len(recents)))
                for i, item in enumerate(recents[:5]):
                    with rc[i]:
                        st.markdown(
                            f"""<div class="intel-box" style="padding:14px;min-height:120px;">
                            <div style="font-weight:850;color:{text_col};">{html.escape(str(item.get('Area_Name','Property')))}</div>
                            <div style="font-size:11px;color:{sub_text};margin-top:4px;">{html.escape(str(item.get('City','')))} • {int(item.get('BHK',0))} BHK</div>
                            <div style="margin-top:12px;font-size:20px;font-weight:900;color:#DAA752;">{format_price(item.get('Historical_Price',0))}</div>
                            <div style="font-size:10px;color:{sub_text};margin-top:5px;">{html.escape(str(item.get('viewed_at','')))}</div>
                            </div>""",
                            unsafe_allow_html=True,
                        )

        with tab_profile:
            p1, p2 = st.columns([1.15, 0.85])
            with p1:
                st.markdown('<div class="intel-box" style="padding:22px;">', unsafe_allow_html=True)
                st.markdown("### 👤 Profile Details")
                if not st.session_state.get("account_edit_mode"):
                    st.markdown(f"**Name:** {name}")
                    st.markdown(f"**Gmail:** {email_addr}")
                    st.markdown(f"**Mobile:** {phone or 'Not available'}")
                    st.caption("Gmail stays linked to the account. You can edit your name and mobile number here.")
                    if st.button("✏️ Edit Profile", key="account_edit_profile", use_container_width=True):
                        st.session_state.account_edit_mode = True
                        st.rerun()
                else:
                    with st.form("account_edit_profile_form", clear_on_submit=False):
                        edit_name = st.text_input("Full Name", value=str(auth_user.get("name", "")), key="account_edit_name")
                        edit_phone = st.text_input("Mobile Number", value=str(auth_user.get("phone", "")), key="account_edit_phone")
                        st.text_input("Gmail Address", value=str(auth_user.get("email", "")), disabled=True, key="account_edit_email_readonly")
                        csave, ccancel = st.columns(2)
                        with csave:
                            save_profile = st.form_submit_button("💾 Save Changes", use_container_width=True)
                        with ccancel:
                            cancel_profile = st.form_submit_button("Cancel", use_container_width=True)
                    if save_profile:
                        try:
                            _update_account_profile(auth_user.get("email", ""), edit_name, edit_phone)
                            st.session_state.account_edit_mode = False
                            st.success("✅ Profile updated successfully.")
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                    if cancel_profile:
                        st.session_state.account_edit_mode = False
                        st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
            with p2:
                st.markdown('<div class="intel-box" style="padding:22px;">', unsafe_allow_html=True)
                st.markdown("### 🔐 Account Security")
                st.write("Password reset is available from the Login → Forgot Password flow.")
                st.write("Your personal favorites, searches and history are separated by account email in the local account store.")
                st.markdown('</div>', unsafe_allow_html=True)

        with tab_activity:
            st.markdown("### 📊 Valuation History")
            if not valuations:
                st.info("Your valuation history will appear here after you generate valuations while logged in.")
            else:
                for idx, item in enumerate(valuations[:12]):
                    c1, c2, c3 = st.columns([1.5, 1, 0.8])
                    with c1:
                        st.markdown(f"**{html.escape(str(item.get('Area_Name','Property')))}, {html.escape(str(item.get('City','')))}**")
                        st.caption(f"{int(item.get('BHK',0))} BHK • {float(item.get('Area',0)):.0f} sq.ft • {item.get('created_at','')}")
                    with c2:
                        st.markdown(f"<div style='font-size:20px;font-weight:900;color:#DAA752;'>{format_price(item.get('price',0))}</div>", unsafe_allow_html=True)
                    with c3:
                        if st.button("🔁 Re-value", key=f"acc_reval_{idx}_{item.get('signature','')}", use_container_width=True):
                            st.session_state.req_city = item.get('City','Ahmedabad')
                            st.session_state.req_area = item.get('Area_Name','Bodakdev')
                            st.session_state.req_bhk = int(item.get('BHK',3))
                            st.session_state.req_sqft = int(float(item.get('Area',1500)))
                            st.session_state.ui_bhk = st.session_state.req_bhk
                            st.session_state.ui_area = st.session_state.req_sqft
                            st.session_state.ui_bath = {1:1,2:2,3:3,4:4,5:5,6:6}.get(st.session_state.req_bhk,3)
                            st.session_state.p_executed = True
                            st.session_state.pending_nav = PAGES['prediction']
                            smart_rerun()
                    st.divider()

            st.markdown("### ⚖️ Comparison History")
            if not comparisons:
                st.info("Your property comparisons will appear here after you compare homes while logged in.")
            else:
                for item in comparisons[:8]:
                    a = item.get('property_a',{}); b = item.get('property_b',{})
                    st.markdown(f"**{html.escape(str(a.get('Area_Name','Property A')))}** vs **{html.escape(str(b.get('Area_Name','Property B')))}** — {html.escape(str(item.get('created_at','')))}")

        with tab_reports:
            if not reports:
                st.info("Your downloaded report history will appear here after you download valuation PDFs while logged in.")
            else:
                for item in reports[:15]:
                    prop = item.get('property',{})
                    location = prop.get('Location') or f"{prop.get('Area_Name','')}, {prop.get('City','')}"
                    st.markdown(f"📄 **{html.escape(str(item.get('title','Property Report')))}**")
                    st.caption(f"{html.escape(str(location))} • {html.escape(str(item.get('file_name','')))} • {html.escape(str(item.get('created_at','')))}")

# ==========================================
# ✦ FINAL SIDEBAR POLISH — V14
# This layer intentionally comes last so it wins over earlier UI themes.
# ==========================================
st.markdown(f"""
<style>
[data-testid="stSidebar"]{{
  width:320px !important; min-width:320px !important;
  background:
    radial-gradient(circle at 8% 4%,rgba(124,58,237,.18),transparent 22%),
    radial-gradient(circle at 94% 34%,rgba(34,211,238,.10),transparent 24%),
    linear-gradient(180deg,{sidebar_bg},{sidebar_bg}) !important;
  border-right:1px solid rgba(124,58,237,.16) !important;
}}
[data-testid="stSidebar"]>div:first-child{{width:320px !important;}}
[data-testid="stSidebarContent"]{{padding:14px 14px 34px !important;}}
[data-testid="stSidebarUserContent"]{{padding-bottom:34px !important;}}

.sidebar-logo.neo-brand{{margin:4px 2px 14px !important;padding:9px 8px 15px !important;border-bottom:1px solid rgba(148,163,184,.12) !important;}}
.sidebar-logo.neo-brand .brand-mark{{width:48px !important;height:48px !important;border-radius:16px !important;background:linear-gradient(145deg,#B87925,#D9A756) !important;box-shadow:0 16px 34px rgba(99,102,241,.3),inset 0 1px 0 rgba(255,255,255,.42) !important;}}
.sidebar-logo.neo-brand .brand-copy h2{{font-size:22px !important;font-weight:900 !important;letter-spacing:-.8px !important;}}
.sidebar-logo.neo-brand .brand-copy p{{font-size:9px !important;letter-spacing:1.45px !important;opacity:.62 !important;}}

[data-testid="stSidebar"] div[role="radiogroup"]{{gap:6px !important;margin-top:4px !important;}}
[data-testid="stSidebar"] div[role="radiogroup"]>label{{min-height:49px !important;margin:0 !important;padding:9px 12px !important;border-radius:15px !important;background:rgba(255,255,255,.02) !important;border:1px solid transparent !important;transition:all .25s ease !important;}}
[data-testid="stSidebar"] div[role="radiogroup"]>label:hover{{transform:translateX(4px) !important;background:linear-gradient(90deg,rgba(124,58,237,.13),rgba(34,211,238,.05)) !important;border-color:rgba(124,58,237,.16) !important;}}
[data-testid="stSidebar"] div[role="radiogroup"]>label[data-checked="true"]{{transform:translateX(3px) !important;background:linear-gradient(100deg,rgba(124,58,237,.20),rgba(34,211,238,.07)) !important;border-color:rgba(124,58,237,.25) !important;box-shadow:inset 3px 0 0 #C9974A,0 10px 28px rgba(76,29,149,.10) !important;}}
[data-testid="stSidebar"] div[role="radiogroup"] p{{font-size:13px !important;font-weight:750 !important;}}
[data-testid="stSidebar"] div[role="radiogroup"]>label[data-checked="true"] p{{color:#c4b5fd !important;}}

.side-section-title{{display:flex;align-items:center;justify-content:space-between;margin:16px 4px 9px;}}
.side-section-kicker{{font-size:9px;font-weight:900;letter-spacing:1.65px;color:{sub_text};}}
.side-live{{display:inline-flex;align-items:center;gap:6px;font-size:8px;font-weight:900;letter-spacing:1px;color:#F2D99B;}}
.live-dot{{width:7px;height:7px;border-radius:50%;display:inline-block;background:#E6C27A;box-shadow:0 0 0 4px rgba(34,211,238,.08),0 0 14px rgba(34,211,238,.40);animation:sidePulse2 2s ease-in-out infinite;}}
@keyframes sidePulse2{{0%,100%{{transform:scale(.92);opacity:.75}}50%{{transform:scale(1.14);opacity:1}}}}

.ai-command-card{{padding:14px;border-radius:20px;background:linear-gradient(145deg,rgba(124,58,237,.15),rgba(34,211,238,.06) 60%,rgba(255,255,255,.02));border:1px solid rgba(124,58,237,.20);box-shadow:0 18px 46px rgba(30,41,59,.12),inset 0 1px 0 rgba(255,255,255,.08);position:relative;overflow:hidden;}}
.ai-command-top{{display:flex;align-items:center;gap:11px;}}
.ai-avatar-wrap{{position:relative;width:44px;height:44px;flex:0 0 44px;}}
.ai-avatar{{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;color:#fff;background:linear-gradient(135deg,#B87925,#0891b2);box-shadow:0 12px 28px rgba(79,70,229,.28);}}
.ai-avatar .material-symbols-rounded{{font-size:23px;}}
.ai-avatar-ring{{position:absolute;inset:-6px;border-radius:18px;border:1px solid rgba(103,232,249,.18);animation:aiRing2 2.8s ease-out infinite;}}
@keyframes aiRing2{{0%{{transform:scale(.88);opacity:.8}}75%,100%{{transform:scale(1.22);opacity:0}}}}
.ai-command-title{{font-size:14px;font-weight:900;letter-spacing:-.2px;color:{text_col};}}
.ai-command-sub{{font-size:9px;color:{sub_text};font-weight:700;margin-top:3px;}}
.ai-status-pill{{margin-top:11px;padding:8px 9px;border-radius:12px;display:flex;align-items:center;gap:7px;border:1px solid rgba(34,211,238,.14);background:rgba(34,211,238,.05);font-size:9px;font-weight:800;color:{text_col};}}
.ai-status-pill.offline{{border-color:rgba(245,158,11,.18);background:rgba(245,158,11,.06);}}
.ai-status-pill.offline .live-dot{{background:#f59e0b;box-shadow:0 0 0 4px rgba(245,158,11,.08);}}
.ai-status-mini{{color:{sub_text};font-weight:600;margin-left:auto;text-align:right;}}

.ai-chat-panel{{max-height:250px;overflow-y:auto;margin:10px 0 9px;padding:8px 3px;scroll-behavior:smooth;}}
.ai-chat-panel::-webkit-scrollbar{{width:5px;}}
.ai-chat-panel::-webkit-scrollbar-thumb{{background:linear-gradient(#B87925,#E6C27A);border-radius:99px;}}
.ai-chat-message{{display:flex;align-items:flex-start;gap:7px;margin:8px 0;animation:chatIn2 .32s ease both;}}
.ai-chat-message.user{{justify-content:flex-end;}}
.ai-chat-message.user .ai-chat-avatar{{order:2;}}
.ai-chat-message.user .ai-chat-bubble{{order:1;}}
.ai-chat-avatar{{width:24px;height:24px;border-radius:8px;display:grid;place-items:center;flex:0 0 24px;margin-top:2px;}}
.ai-chat-avatar .material-symbols-rounded{{font-size:15px;}}
.ai-chat-avatar.bot{{color:#fff;background:linear-gradient(135deg,#B87925,#0891b2);}}
.ai-chat-avatar.user{{color:{text_col};background:rgba(148,163,184,.13);border:1px solid rgba(148,163,184,.12);}}
.ai-chat-bubble{{max-width:82%;padding:9px 11px;border-radius:13px;font-size:11px;line-height:1.48;font-weight:650;word-break:break-word;overflow-wrap:anywhere;}}
.ai-chat-bubble.bot{{color:{text_col} !important;background:rgba(255,255,255,.045);border:1px solid rgba(148,163,184,.10);border-top-left-radius:5px;}}
.ai-chat-bubble.user{{color:#fff !important;background:linear-gradient(135deg,#B87925,#0891b2);border-bottom-right-radius:5px;box-shadow:0 8px 20px rgba(79,70,229,.20);}}
@keyframes chatIn2{{from{{opacity:0;transform:translateY(5px)}}to{{opacity:1;transform:none}}}}

[data-testid="stSidebar"] .stTextInput input{{min-height:43px !important;border-radius:13px !important;background:rgba(255,255,255,.045) !important;border:1px solid rgba(148,163,184,.13) !important;font-size:11px !important;}}
[data-testid="stSidebar"] .stFormSubmitButton button{{min-height:42px !important;border-radius:12px !important;font-size:11px !important;font-weight:900 !important;background:linear-gradient(100deg,#B87925,#D9A756) !important;border:0 !important;box-shadow:0 12px 24px rgba(79,70,229,.20) !important;}}
[data-testid="stSidebar"] .stButton > button{{min-height:40px !important;border-radius:12px !important;font-size:10px !important;font-weight:800 !important;background:rgba(255,255,255,.04) !important;border:1px solid rgba(148,163,184,.12) !important;box-shadow:none !important;}}
[data-testid="stSidebar"] .stButton > button:hover{{transform:translateY(-1px) !important;background:rgba(34,211,238,.06) !important;border-color:rgba(34,211,238,.24) !important;}}
.ai-error-chip{{margin-top:7px;padding:8px 10px;border-radius:11px;font-size:10px;font-weight:700;color:#fecaca;background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.15);}}
[data-testid="stSidebar"] hr{{margin:12px 0 !important;border-color:rgba(148,163,184,.10) !important;}}
[data-testid="stSidebar"] .stSelectbox > div > div{{border-radius:12px !important;min-height:40px !important;}}
@media (max-width:1100px){{[data-testid="stSidebar"],[data-testid="stSidebar"]>div:first-child{{width:290px !important;min-width:290px !important;}}}}
@media (prefers-reduced-motion:reduce){{.ai-avatar-ring,.live-dot,.ai-chat-message{{animation:none !important;}}}}
</style>
""", unsafe_allow_html=True)


# ==========================================
# ✦ V21 AURORA BENTO REDESIGN
# A completely new visual direction: editorial + bento + cinematic motion.
# This layer intentionally overrides the earlier themes without touching logic.
# ==========================================
_theme_dark = st.session_state.dark_mode
_A_BG = "#0A0F16" if _theme_dark else "#F5F3EE"
_A_SURFACE = "#111923" if _theme_dark else "#FFFDF8"
_A_SURFACE2 = "#17212D" if _theme_dark else "#F0EEE8"
_A_TEXT = "#F8FAFC" if _theme_dark else "#17191C"
_A_MUTED = "#98A4B3" if _theme_dark else "#72777D"
_A_LINE = "rgba(255,255,255,.10)" if _theme_dark else "rgba(20,24,28,.10)"
_A_CORAL = "#FF6A57"
_A_INDIGO = "#6573FF"
_A_TEAL = "#23C7B5"
_A_GLOW = "rgba(255,106,87,.22)" if _theme_dark else "rgba(255,106,87,.15)"

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

:root {{
  --a-bg:{_A_BG};
  --a-surface:{_A_SURFACE};
  --a-surface2:{_A_SURFACE2};
  --a-text:{_A_TEXT};
  --a-muted:{_A_MUTED};
  --a-line:{_A_LINE};
  --a-coral:{_A_CORAL};
  --a-indigo:{_A_INDIGO};
  --a-teal:{_A_TEAL};
  --a-glow:{_A_GLOW};
}}

/* ===== shell ===== */
html, body, [class*="css"], .stApp {{
  font-family:'Plus Jakarta Sans',sans-serif !important;
}}
.stApp {{
  background:
    radial-gradient(circle at 12% 8%, rgba(101,115,255,.09), transparent 22%),
    radial-gradient(circle at 88% 12%, rgba(255,106,87,.08), transparent 20%),
    var(--a-bg) !important;
  color:var(--a-text) !important;
}}
[data-testid="stAppViewContainer"] {{
  background:transparent !important;
  overflow-x:hidden !important;
}}
[data-testid="stAppViewContainer"]::before {{
  content:""; position:fixed; inset:0; pointer-events:none; z-index:0; opacity:.28;
  background-image:linear-gradient(var(--a-line) 1px,transparent 1px),linear-gradient(90deg,var(--a-line) 1px,transparent 1px);
  background-size:68px 68px;
  mask-image:linear-gradient(to bottom,rgba(0,0,0,.38),transparent 68%);
}}
.block-container {{ max-width:1500px !important; padding:10px 24px 64px !important; }}
footer {{ display:none !important; }}

/* ===== keyframes ===== */
@keyframes auroraFloat {{
  0%,100%{{transform:translate3d(0,0,0) scale(1)}}
  50%{{transform:translate3d(18px,-12px,0) scale(1.03)}}
}}
@keyframes revealUp {{ from{{opacity:0;transform:translateY(22px)}} to{{opacity:1;transform:translateY(0)}} }}
@keyframes softPulse {{ 0%,100%{{box-shadow:0 0 0 0 rgba(255,106,87,0)}} 50%{{box-shadow:0 0 0 9px rgba(255,106,87,.08)}} }}
@keyframes sweep {{ from{{transform:translateX(-150%) skewX(-18deg)}} to{{transform:translateX(250%) skewX(-18deg)}} }}
@keyframes iconFloat {{ 0%,100%{{transform:translateY(0) rotate(0deg)}} 50%{{transform:translateY(-5px) rotate(2deg)}} }}
@keyframes linePulse {{ 0%,100%{{opacity:.35}} 50%{{opacity:1}} }}
@keyframes navIn {{ from{{opacity:0;transform:translateX(-7px)}} to{{opacity:1;transform:none}} }}

/* ===== top brand bar ===== */
.editorial-topbar, .premium-commandbar {{
  background:color-mix(in srgb,var(--a-surface) 91%,transparent) !important;
  border:1px solid var(--a-line) !important;
  box-shadow:0 24px 60px rgba(0,0,0,.08), inset 0 1px 0 rgba(255,255,255,.05) !important;
  border-radius:26px !important;
  position:relative !important;
  overflow:hidden !important;
  backdrop-filter:blur(22px) saturate(1.15) !important;
}}
.editorial-topbar::after {{
  content:""; position:absolute; top:-40%; left:-15%; width:16%; height:180%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.16),transparent);
  transform:skewX(-18deg); animation:sweep 9s ease-in-out infinite; pointer-events:none;
}}
.editorial-topbar .neo-topbar-mark {{
  background:linear-gradient(145deg,var(--a-coral),#D946EF) !important;
  border-radius:16px !important;
  box-shadow:0 14px 32px rgba(255,106,87,.24) !important;
  animation:iconFloat 4.8s ease-in-out infinite;
}}
.editorial-topbar .premium-commandbar-title {{
  font-family:'Plus Jakarta Sans',sans-serif !important;
  font-size:18px !important; letter-spacing:1.4px !important; color:var(--a-text) !important;
}}
.editorial-topbar .premium-commandbar-title span {{ color:var(--a-coral) !important; -webkit-text-fill-color:initial !important; background:none !important; }}
.editorial-topbar .neo-topbar-sub {{ color:var(--a-muted) !important; letter-spacing:1.25px !important; }}

.premium-chip, .neo-mini-pill {{
  border:1px solid var(--a-line) !important;
  background:color-mix(in srgb,var(--a-surface2) 78%,transparent) !important;
  color:var(--a-text) !important;
  border-radius:999px !important;
  transition:transform .25s ease, box-shadow .25s ease, border-color .25s ease !important;
}}
.premium-chip:hover, .neo-mini-pill:hover {{ transform:translateY(-2px); border-color:rgba(255,106,87,.35) !important; box-shadow:0 10px 20px rgba(0,0,0,.06) !important; }}
.premium-dot {{ background:var(--a-teal) !important; box-shadow:0 0 0 4px rgba(35,199,181,.10),0 0 14px rgba(35,199,181,.5) !important; animation:softPulse 2.5s infinite !important; }}

/* ===== hero ===== */
.hero-banner {{
  min-height:370px !important; padding:46px !important; border-radius:34px !important;
  background-position:center !important;
  box-shadow:0 34px 80px rgba(0,0,0,.13) !important;
  border:1px solid var(--a-line) !important;
  transform:translateZ(0); animation:revealUp .65s ease both !important;
}}
.hero-banner::before {{
  background:
    linear-gradient(90deg,rgba(4,8,14,.84) 0%,rgba(4,8,14,.60) 38%,rgba(4,8,14,.06) 82%),
    linear-gradient(180deg,rgba(0,0,0,.04),rgba(0,0,0,.24)) !important;
}}
.hero-content {{ max-width:720px !important; }}
.hero-eyebrow {{
  display:inline-flex !important; align-items:center !important; gap:8px !important;
  padding:8px 12px !important; border-radius:999px !important;
  background:rgba(255,255,255,.09) !important; border:1px solid rgba(255,255,255,.15) !important;
  backdrop-filter:blur(10px) !important; color:#FFD2CB !important; letter-spacing:1.8px !important;
}}
.hero-title {{
  font-family:'DM Serif Display',serif !important; font-weight:400 !important;
  font-size:64px !important; letter-spacing:-1.8px !important; line-height:.98 !important;
  text-wrap:balance; text-shadow:0 10px 30px rgba(0,0,0,.28) !important;
}}
.gradient-text {{
  background:linear-gradient(90deg,#FFE1D9,var(--a-coral),#DCE0FF) !important;
  -webkit-background-clip:text !important; -webkit-text-fill-color:transparent !important;
}}
.hero-sub {{ color:rgba(255,255,255,.78) !important; font-size:16px !important; max-width:600px !important; }}

/* ===== section labels / headings ===== */
.card-header, h1, h2, h3 {{ color:var(--a-text) !important; }}
h1 {{ letter-spacing:-1.4px !important; }}
h2 {{ letter-spacing:-1px !important; }}
.card-header {{ font-size:19px !important; font-weight:800 !important; }}

/* ===== inputs ===== */
label {{ color:var(--a-muted) !important; font-size:12px !important; font-weight:800 !important; letter-spacing:.5px !important; }}
.stTextInput input, .stNumberInput input,
.stTextArea textarea,
.stSelectbox div[data-baseweb="select"] > div,
.stMultiSelect div[data-baseweb="select"] > div,
.stDateInput div[data-baseweb="input"] > div {{
  background:var(--a-surface) !important; color:var(--a-text) !important;
  border:1px solid var(--a-line) !important; border-radius:15px !important;
  min-height:50px !important; box-shadow:0 9px 24px rgba(0,0,0,.035) inset !important;
  transition:all .25s ease !important;
}}
.stTextInput input:focus, .stTextArea textarea:focus,
.stNumberInput input:focus,
.stSelectbox div[data-baseweb="select"] > div:focus-within,
.stMultiSelect div[data-baseweb="select"] > div:focus-within {{
  border-color:rgba(255,106,87,.55) !important;
  box-shadow:0 0 0 4px rgba(255,106,87,.10),0 10px 22px rgba(0,0,0,.05) !important;
  transform:translateY(-1px) !important;
}}
[data-baseweb="popover"] {{ border:1px solid var(--a-line) !important; border-radius:16px !important; box-shadow:0 25px 60px rgba(0,0,0,.18) !important; }}

/* ===== buttons ===== */
.stButton > button, .stFormSubmitButton button {{
  position:relative !important; overflow:hidden !important;
  min-height:50px !important; border:0 !important; border-radius:15px !important;
  color:#fff !important; font-weight:800 !important; letter-spacing:.1px !important;
  background:linear-gradient(135deg,var(--a-coral),#E04767) !important;
  box-shadow:0 16px 32px rgba(255,106,87,.22) !important;
  transition:transform .22s ease, box-shadow .22s ease, filter .22s ease !important;
}}
.stButton > button::after, .stFormSubmitButton button::after {{
  content:""; position:absolute; inset:-30% auto -30% -25%; width:18%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent);
  transform:skewX(-18deg) translateX(-180%); transition:transform .55s ease;
}}
.stButton > button:hover, .stFormSubmitButton button:hover {{
  transform:translateY(-3px) scale(1.01) !important; filter:saturate(1.06) !important;
  box-shadow:0 22px 42px rgba(255,106,87,.30) !important;
}}
.stButton > button:hover::after, .stFormSubmitButton button:hover::after {{ transform:skewX(-18deg) translateX(720%); }}

/* secondary / ghost buttons */
.btn-back > button, [data-testid="stSidebar"] .stButton > button {{
  background:var(--a-surface) !important; color:var(--a-text) !important;
  border:1px solid var(--a-line) !important; box-shadow:0 10px 24px rgba(0,0,0,.04) !important;
}}
.btn-back > button:hover, [data-testid="stSidebar"] .stButton > button:hover {{
  color:var(--a-coral) !important; border-color:rgba(255,106,87,.35) !important;
}}

/* ===== cards / bento ===== */
.intel-box, .metric-card, .compare-card, .val-box, .poi-card {{
  background:linear-gradient(180deg,color-mix(in srgb,var(--a-surface) 96%,transparent),var(--a-surface2)) !important;
  border:1px solid var(--a-line) !important; border-radius:24px !important;
  box-shadow:0 18px 46px rgba(15,23,42,.075),inset 0 1px 0 rgba(255,255,255,.05) !important;
  overflow:hidden !important; position:relative !important;
  transition:transform .28s cubic-bezier(.2,.8,.2,1), box-shadow .28s ease, border-color .28s ease !important;
}}
.intel-box::before, .metric-card::before, .compare-card::before, .val-box::before, .poi-card::before {{
  content:""; position:absolute; top:0; left:0; right:0; height:2px;
  background:linear-gradient(90deg,var(--a-coral),var(--a-indigo),var(--a-teal));
  opacity:.72;
}}
.intel-box:hover, .metric-card:hover, .compare-card:hover, .val-box:hover, .poi-card:hover {{
  transform:translateY(-6px) !important; border-color:rgba(255,106,87,.24) !important;
  box-shadow:0 28px 58px rgba(15,23,42,.12),0 0 0 1px rgba(255,106,87,.05) inset !important;
}}
.metric-card {{ min-height:150px !important; display:flex; flex-direction:column; justify-content:center; }}
.metric-card .num {{ font-size:39px !important; letter-spacing:-1.6px !important; color:var(--a-text) !important; }}
.metric-card .lbl {{ color:var(--a-muted) !important; font-size:10px !important; letter-spacing:1.7px !important; }}
.intel-title {{ color:var(--a-muted) !important; font-size:11px !important; text-transform:uppercase !important; letter-spacing:1.1px !important; }}
.intel-stat {{ color:var(--a-text) !important; font-size:30px !important; }}

/* ===== valuation hero card ===== */
.val-box {{ padding:34px 28px !important; border-radius:28px !important; }}
.val-box .val-price, .val-price {{
  font-family:'DM Serif Display',serif !important; color:var(--a-coral) !important;
  font-size:60px !important; letter-spacing:-1px !important;
}}
.val-range {{ background:var(--a-surface2) !important; color:var(--a-muted) !important; border:1px solid var(--a-line) !important; }}

/* ===== property image ===== */
.property-image {{
  border-radius:24px !important; height:340px !important; object-fit:cover !important;
  border:1px solid rgba(255,255,255,.18) !important;
  box-shadow:0 30px 60px rgba(0,0,0,.16) !important;
  transition:transform .55s cubic-bezier(.2,.8,.2,1), filter .35s ease, box-shadow .35s ease !important;
}}
.property-image:hover {{ transform:scale(1.025) translateY(-4px) !important; filter:saturate(1.05) contrast(1.02) !important; box-shadow:0 38px 78px rgba(0,0,0,.24) !important; }}

/* ===== comparison / tables ===== */
.compare-row {{ border-bottom:1px solid var(--a-line) !important; padding:15px 0 !important; }}
.c-label {{ color:var(--a-muted) !important; }}
.c-val {{ color:var(--a-text) !important; }}
.dual-verdict-box {{ background:linear-gradient(135deg,rgba(255,106,87,.08),rgba(101,115,255,.08)) !important; border-color:var(--a-line) !important; border-radius:22px !important; }}

/* ===== neighborhood / POI ===== */
.poi-card {{ min-height:92px !important; }}
.poi-icon {{ width:42px !important; height:42px !important; display:grid !important; place-items:center !important; border-radius:14px !important; background:linear-gradient(145deg,rgba(255,106,87,.12),rgba(101,115,255,.10)) !important; margin-right:12px !important; animation:iconFloat 5s ease-in-out infinite; }}
.poi-title {{ color:var(--a-text) !important; }}
.poi-dist {{ color:var(--a-teal) !important; }}

/* ===== AI ===== */
.ai-engine-status {{
  background:linear-gradient(135deg,rgba(35,199,181,.08),rgba(101,115,255,.06)) !important;
  border-color:rgba(35,199,181,.18) !important; border-radius:15px !important;
}}
.ai-chat-stack, .ai-chat-panel {{ scrollbar-width:thin; scrollbar-color:rgba(101,115,255,.6) transparent; }}
.ai-chat-bubble.bot {{
  background:var(--a-surface) !important; border-color:var(--a-line) !important; color:var(--a-text) !important;
}}
.ai-chat-bubble.user {{ background:linear-gradient(135deg,var(--a-indigo),var(--a-coral)) !important; color:#fff !important; }}

/* ===== sidebar ===== */
[data-testid="stSidebar"] {{
  background:var(--a-surface) !important; border-right:1px solid var(--a-line) !important;
}}
[data-testid="stSidebar"] > div:first-child {{ background:transparent !important; }}
[data-testid="stSidebar"] .sidebar-logo.neo-brand {{
  background:linear-gradient(145deg,var(--a-surface2),var(--a-surface)) !important;
  border:1px solid var(--a-line) !important; border-radius:22px !important;
  padding:13px 12px !important; margin:2px 0 15px !important;
  box-shadow:0 18px 40px rgba(0,0,0,.06) !important;
}}
[data-testid="stSidebar"] .sidebar-logo.neo-brand .brand-mark {{
  background:linear-gradient(145deg,var(--a-coral),var(--a-indigo)) !important;
  box-shadow:0 14px 28px rgba(255,106,87,.20) !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label {{
  background:transparent !important; border:1px solid transparent !important;
  border-radius:15px !important; min-height:46px !important; transition:all .22s ease !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {{
  background:var(--a-surface2) !important; transform:translateX(3px) !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] {{
  background:linear-gradient(90deg,rgba(255,106,87,.13),rgba(101,115,255,.07)) !important;
  border-color:rgba(255,106,87,.18) !important; box-shadow:inset 3px 0 0 var(--a-coral),0 10px 26px rgba(0,0,0,.05) !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] p {{ color:var(--a-text) !important; }}

/* AI command card on sidebar */
.ai-command-card {{
  background:linear-gradient(145deg,rgba(255,106,87,.10),rgba(101,115,255,.08) 58%,rgba(35,199,181,.05)) !important;
  border:1px solid rgba(255,106,87,.18) !important; border-radius:22px !important;
  box-shadow:0 20px 44px rgba(0,0,0,.09),inset 0 1px 0 rgba(255,255,255,.04) !important;
}}
.ai-avatar {{ background:linear-gradient(135deg,var(--a-coral),var(--a-indigo)) !important; box-shadow:0 14px 30px rgba(255,106,87,.25) !important; animation:iconFloat 4s ease-in-out infinite !important; }}

/* ===== dividers / markdown ===== */
hr {{ border-color:var(--a-line) !important; }}
.stCaption, [data-testid="stCaptionContainer"] {{ color:var(--a-muted) !important; }}

/* ===== tabs / segmented controls ===== */
.stTabs [data-baseweb="tab-list"] {{ gap:7px !important; background:transparent !important; }}
.stTabs [data-baseweb="tab"] {{
  background:var(--a-surface2) !important; color:var(--a-muted) !important; border:1px solid transparent !important;
  border-radius:999px !important; padding:9px 15px !important; min-height:40px !important;
}}
.stTabs [aria-selected="true"] {{
  color:var(--a-text) !important; background:var(--a-surface) !important; border-color:rgba(255,106,87,.20) !important;
  box-shadow:0 10px 20px rgba(0,0,0,.05) !important;
}}

/* ===== reveal all main blocks ===== */
[data-testid="stVerticalBlock"] > div {{ animation:revealUp .52s ease both; }}

/* ===== small screens ===== */
@media (max-width:900px) {{
  .block-container {{ padding-left:14px !important; padding-right:14px !important; }}
  .hero-banner {{ min-height:320px !important; padding:30px !important; }}
  .hero-title {{ font-size:47px !important; }}
  .metric-card .num {{ font-size:31px !important; }}
}}
@media (max-width:600px) {{
  .editorial-topbar {{ border-radius:18px !important; }}
  .editorial-topbar .neo-topbar-right {{ display:none !important; }}
  .hero-title {{ font-size:40px !important; }}
  .hero-banner {{ border-radius:24px !important; padding:24px !important; }}
}}

@media (prefers-reduced-motion:reduce) {{
  *,*::before,*::after {{ animation-duration:.001ms !important; transition-duration:.001ms !important; }}
}}
</style>
""", unsafe_allow_html=True)


# ==========================================
# ✦ V22 SLATE LUXURY REDESIGN
# Medium-dark visual language: slate, electric blue, emerald, cinematic motion.
# ==========================================
st.markdown(f"""
<style>
:root {{
  --m-bg:#111A24;
  --m-surface:#192633;
  --m-surface2:#223140;
  --m-text:#F4F7FA;
  --m-muted:#A8B6C6;
  --m-line:rgba(168,184,202,.16);
  --m-blue:#4CC9FF;
  --m-blue2:#2F80ED;
  --m-teal:#2DD4BF;
  --m-violet:#8B7CFF;
}}
.stApp {{ background:radial-gradient(900px 500px at 8% -8%,rgba(76,201,255,.12),transparent 56%),radial-gradient(820px 520px at 92% 0%,rgba(139,124,255,.10),transparent 60%),linear-gradient(180deg,var(--m-bg),#141F2B 42%,var(--m-bg)) !important; color:var(--m-text) !important; }}
[data-testid="stAppViewContainer"] {{ background:transparent !important; overflow-x:hidden !important; }}
[data-testid="stAppViewContainer"]::before {{ content:""; position:fixed; inset:0; pointer-events:none; z-index:0; opacity:.16; background-image:linear-gradient(rgba(148,163,184,.055) 1px,transparent 1px),linear-gradient(90deg,rgba(148,163,184,.055) 1px,transparent 1px); background-size:58px 58px; mask-image:linear-gradient(to bottom,rgba(0,0,0,.42),transparent 72%); }}
.block-container {{ max-width:1540px !important; padding:18px 24px 70px !important; }}
footer {{ display:none !important; }}
@keyframes midnightFloat {{ 0%,100%{{transform:translate3d(0,0,0)}} 50%{{transform:translate3d(0,-6px,0)}} }}
@keyframes midnightSweep {{ from{{transform:translateX(-160%) skewX(-18deg)}} to{{transform:translateX(520%) skewX(-18deg)}} }}
@keyframes midnightReveal {{ from{{opacity:0;transform:translateY(12px)}} to{{opacity:1;transform:none}} }}
.editorial-topbar,.premium-commandbar {{ background:linear-gradient(135deg,rgba(25,38,51,.96),rgba(17,28,39,.94)) !important; border:1px solid rgba(76,201,255,.14) !important; border-radius:22px !important; box-shadow:0 24px 70px rgba(0,0,0,.34),inset 0 1px 0 rgba(255,255,255,.035) !important; backdrop-filter:blur(22px) saturate(1.15) !important; }}
.editorial-topbar::after {{ content:""; position:absolute; top:-50%; left:-18%; width:13%; height:200%; background:linear-gradient(90deg,transparent,rgba(76,201,255,.12),transparent); transform:skewX(-18deg); animation:midnightSweep 10s ease-in-out infinite; pointer-events:none; }}
.editorial-topbar .neo-topbar-mark {{ background:linear-gradient(145deg,var(--m-blue),var(--m-violet)) !important; box-shadow:0 16px 34px rgba(76,201,255,.18) !important; animation:midnightFloat 4.8s ease-in-out infinite; }}
.editorial-topbar .premium-commandbar-title,.editorial-topbar .premium-commandbar-title span {{ color:var(--m-text) !important; background:none !important; -webkit-text-fill-color:initial !important; }}
.editorial-topbar .premium-commandbar-title span {{ color:var(--m-blue) !important; }}
.editorial-topbar .neo-topbar-sub,.premium-commandbar-meta {{ color:var(--m-muted) !important; }}
.premium-chip,.neo-mini-pill {{ background:rgba(255,255,255,.035) !important; border-color:var(--m-line) !important; color:var(--m-text) !important; }}
.premium-dot {{ background:var(--m-teal) !important; box-shadow:0 0 0 4px rgba(45,212,191,.08),0 0 18px rgba(45,212,191,.45) !important; }}
.hero-banner {{ min-height:350px !important; padding:42px !important; border-radius:30px !important; border:1px solid rgba(255,255,255,.08) !important; box-shadow:0 34px 90px rgba(0,0,0,.40) !important; animation:midnightReveal .55s ease both !important; }}
.hero-banner::before {{ background:linear-gradient(90deg,rgba(8,14,22,.80) 0%,rgba(8,14,22,.58) 44%,rgba(8,14,22,.12) 84%),linear-gradient(180deg,rgba(0,0,0,.04),rgba(0,0,0,.22)) !important; }}
.hero-eyebrow {{ color:#AEE8FF !important; background:rgba(76,201,255,.07) !important; border-color:rgba(76,201,255,.18) !important; }}
.hero-title {{ font-family:'DM Serif Display',serif !important; font-size:62px !important; color:var(--m-text) !important; text-shadow:0 14px 34px rgba(0,0,0,.45) !important; }}
.gradient-text {{ background:linear-gradient(90deg,#EAF9FF,var(--m-blue),#BDB5FF) !important; -webkit-background-clip:text !important; -webkit-text-fill-color:transparent !important; }}
.hero-sub {{ color:#B8C3CF !important; }}
.intel-box,.metric-card,.compare-card,.val-box,.poi-card {{ background:linear-gradient(180deg,rgba(25,38,51,.98),rgba(20,31,43,.98)) !important; border:1px solid rgba(148,163,184,.11) !important; border-radius:22px !important; box-shadow:0 20px 55px rgba(0,0,0,.30),inset 0 1px 0 rgba(255,255,255,.025) !important; position:relative !important; overflow:hidden !important; }}
.intel-box::before,.metric-card::before,.compare-card::before,.val-box::before,.poi-card::before {{ content:""; position:absolute; left:0; right:0; top:0; height:2px; background:linear-gradient(90deg,var(--m-blue),var(--m-violet),var(--m-teal)); opacity:.74; }}
.intel-box:hover,.metric-card:hover,.compare-card:hover,.val-box:hover,.poi-card:hover {{ transform:translateY(-5px) !important; border-color:rgba(76,201,255,.24) !important; box-shadow:0 30px 70px rgba(0,0,0,.38),0 0 0 1px rgba(76,201,255,.04) inset !important; }}
.card-header,h1,h2,h3,.poi-title {{ color:var(--m-text) !important; }}
.intel-title {{ color:#95A4B5 !important; text-transform:uppercase !important; letter-spacing:1.2px !important; font-size:10px !important; }}
.intel-stat {{ color:var(--m-blue) !important; }}
.metric-card .num {{ color:var(--m-text) !important; }}
.metric-card .lbl {{ color:#8797A9 !important; }}
.val-box {{ border-color:rgba(76,201,255,.26) !important; box-shadow:0 0 0 1px rgba(76,201,255,.04),0 28px 75px rgba(0,0,0,.34) !important; }}
.val-box .val-price,.val-price {{ color:var(--m-blue) !important; text-shadow:0 0 28px rgba(76,201,255,.13) !important; }}
.val-range {{ background:#223140 !important; color:#8FA0B2 !important; border-color:rgba(148,163,184,.10) !important; }}
label {{ color:#8FA0B2 !important; }}
.stTextInput input,.stNumberInput input,.stTextArea textarea,.stSelectbox div[data-baseweb="select"] > div,.stMultiSelect div[data-baseweb="select"] > div,.stDateInput div[data-baseweb="input"] > div {{ background:#172330 !important; color:var(--m-text) !important; border:1px solid rgba(148,163,184,.13) !important; border-radius:14px !important; box-shadow:inset 0 1px 0 rgba(255,255,255,.02) !important; }}
.stTextInput input:focus,.stTextArea textarea:focus,.stNumberInput input:focus,.stSelectbox div[data-baseweb="select"] > div:focus-within,.stMultiSelect div[data-baseweb="select"] > div:focus-within {{ border-color:rgba(76,201,255,.55) !important; box-shadow:0 0 0 4px rgba(76,201,255,.08) !important; }}
[data-baseweb="popover"] {{ background:#1A2836 !important; border:1px solid rgba(148,163,184,.12) !important; color:var(--m-text) !important; }}
[data-baseweb="option"] {{ background:#1A2836 !important; color:var(--m-text) !important; }}
[data-baseweb="option"]:hover {{ background:#24394B !important; }}
.stButton > button,.stFormSubmitButton button {{ background:linear-gradient(135deg,var(--m-blue2),var(--m-blue)) !important; color:#041019 !important; border:0 !important; border-radius:14px !important; min-height:50px !important; font-weight:900 !important; box-shadow:0 14px 28px rgba(47,128,237,.18) !important; }}
.stButton > button:hover,.stFormSubmitButton button:hover {{ transform:translateY(-3px) !important; box-shadow:0 20px 36px rgba(76,201,255,.24) !important; filter:brightness(1.04) !important; }}
.btn-back > button,[data-testid="stSidebar"] .stButton > button {{ background:#1A2836 !important; color:var(--m-text) !important; border:1px solid rgba(148,163,184,.12) !important; box-shadow:none !important; }}
[data-testid="stSidebar"] {{ background:linear-gradient(180deg,#121D28,#162331 65%,#111B26) !important; border-right:1px solid rgba(76,201,255,.12) !important; box-shadow:16px 0 50px rgba(0,0,0,.32) !important; }}
[data-testid="stSidebar"] .sidebar-logo.neo-brand {{ background:linear-gradient(145deg,#1C2B3A,#162432) !important; border-color:rgba(76,201,255,.13) !important; box-shadow:0 20px 45px rgba(0,0,0,.25) !important; }}
[data-testid="stSidebar"] .sidebar-logo.neo-brand .brand-mark {{ background:linear-gradient(145deg,var(--m-blue),var(--m-violet)) !important; box-shadow:0 14px 30px rgba(76,201,255,.18) !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label {{ background:transparent !important; color:var(--m-text) !important; border-color:transparent !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {{ background:#203140 !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] {{ background:linear-gradient(90deg,rgba(76,201,255,.12),rgba(139,124,255,.05)) !important; border-color:rgba(76,201,255,.16) !important; box-shadow:inset 3px 0 0 var(--m-blue),0 10px 26px rgba(0,0,0,.20) !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] p {{ color:#DDF7FF !important; }}
.ai-command-card {{ background:linear-gradient(145deg,rgba(76,201,255,.10),rgba(139,124,255,.08),rgba(45,212,191,.04)) !important; border-color:rgba(76,201,255,.16) !important; box-shadow:0 22px 50px rgba(0,0,0,.28) !important; }}
.ai-avatar {{ background:linear-gradient(135deg,var(--m-blue),var(--m-violet)) !important; box-shadow:0 15px 30px rgba(76,201,255,.20) !important; }}
.ai-chat-bubble.bot {{ background:#1A2836 !important; color:var(--m-text) !important; border-color:rgba(148,163,184,.11) !important; }}
.ai-chat-bubble.user {{ background:linear-gradient(135deg,#2F80ED,#4CC9FF) !important; color:#031018 !important; }}
.stTabs [data-baseweb="tab"] {{ background:#1A2836 !important; color:#8899AA !important; border:1px solid rgba(148,163,184,.08) !important; border-radius:999px !important; }}
.stTabs [aria-selected="true"] {{ color:#EAFBFF !important; background:#223648 !important; border-color:rgba(76,201,255,.20) !important; box-shadow:0 10px 24px rgba(0,0,0,.22) !important; }}
.stCaption,[data-testid="stCaptionContainer"] {{ color:#7F90A3 !important; }} hr {{ border-color:rgba(148,163,184,.10) !important; }}
.property-image {{ filter:saturate(.94) contrast(1.04); box-shadow:0 28px 70px rgba(0,0,0,.34) !important; border-color:rgba(255,255,255,.10) !important; }}
.property-image:hover {{ filter:saturate(1.04) contrast(1.06) brightness(1.02); }}
[data-testid="stVerticalBlock"] > div {{ animation:midnightReveal .42s ease both; }}
@media (max-width:900px) {{ .block-container {{ padding:12px 14px 46px !important; }} .hero-title {{ font-size:46px !important; }} .hero-banner {{ min-height:310px !important; padding:28px !important; border-radius:24px !important; }} }}
@media (prefers-reduced-motion:reduce) {{ *,*::before,*::after {{ animation-duration:.001ms !important; transition-duration:.001ms !important; }} }}
</style>
""", unsafe_allow_html=True)


st.markdown("""<!-- LIGHT APARTMENT MARKETPLACE OVERRIDE -->
<style>
:root{
  --apt-bg:#f5f7f8;
  --apt-surface:#ffffff;
  --apt-surface2:#eef3f3;
  --apt-card:#ffffff;
  --apt-line:rgba(21,45,43,.10);
  --apt-text:#17332f;
  --apt-muted:#6c7a77;
  --apt-accent:#b9842f;
  --apt-accent2:#d29a4a;
  --apt-soft:#e7f2ef;
}
html,body{background:#f5f7f8 !important;color:var(--apt-text) !important;}
.stApp{
  background:
    radial-gradient(circle at 8% 7%,rgba(15,139,120,.10),transparent 24%),
    radial-gradient(circle at 92% 14%,rgba(210,154,74,.10),transparent 22%),
    linear-gradient(180deg,#f8faf9 0%,#f2f6f5 52%,#f7f8f7 100%) !important;
  color:var(--apt-text) !important;
}
[data-testid="stAppViewContainer"]{
  background:transparent !important;
  color:var(--apt-text) !important;
}
[data-testid="stHeader"]{background:transparent !important;}
[data-testid="stToolbar"]{background:transparent !important;}
[data-testid="stSidebar"]{
  background:linear-gradient(180deg,#ffffff 0%,#f4f8f7 100%) !important;
  border-right:1px solid var(--apt-line) !important;
  box-shadow:10px 0 35px rgba(23,51,47,.05) !important;
}
[data-testid="stSidebar"] > div:first-child,
[data-testid="stSidebarContent"]{
  background:transparent !important;
}
[data-testid="stSidebarContent"]{padding:18px 14px 26px !important;}
[data-testid="stSidebar"] *{color:var(--apt-text) !important;}
[data-testid="stSidebar"] .stCaption,
[data-testid="stSidebar"] small{color:var(--apt-muted) !important;}

/* Brand + navigation */
.sidebar-logo.neo-brand{
  background:linear-gradient(145deg,#ffffff,#edf6f3) !important;
  border:1px solid rgba(15,139,120,.13) !important;
  border-radius:22px !important;
  padding:13px 12px !important;
  box-shadow:0 16px 34px rgba(23,51,47,.07) !important;
}
.sidebar-logo.neo-brand .brand-mark{
  background:linear-gradient(145deg,#b9842f,#d29a4a) !important;
  box-shadow:0 12px 28px rgba(15,139,120,.22) !important;
}
[data-testid="stSidebar"] div[role="radiogroup"] > label{
  background:transparent !important;
  border:1px solid transparent !important;
  color:var(--apt-text) !important;
}
[data-testid="stSidebar"] div[role="radiogroup"] > label:hover{
  background:#edf6f3 !important;
  border-color:rgba(15,139,120,.12) !important;
  transform:translateX(3px) !important;
}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"]{
  background:linear-gradient(90deg,rgba(15,139,120,.12),rgba(210,154,74,.06)) !important;
  border-color:rgba(15,139,120,.16) !important;
  box-shadow:inset 3px 0 0 var(--apt-accent),0 8px 22px rgba(23,51,47,.05) !important;
}
[data-testid="stSidebar"] div[role="radiogroup"] p{color:var(--apt-text) !important;}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] p{color:var(--apt-accent) !important;}

/* AI sidebar card */
.ai-command-card{
  background:linear-gradient(145deg,#f0faf7,#f8f4ed) !important;
  border:1px solid rgba(15,139,120,.13) !important;
  box-shadow:0 18px 40px rgba(23,51,47,.07),inset 0 1px 0 rgba(255,255,255,.9) !important;
}
.ai-avatar{background:linear-gradient(135deg,#b9842f,#d29a4a) !important;box-shadow:0 12px 25px rgba(15,139,120,.20) !important;}
.ai-chat-bubble.bot{background:#ffffff !important;color:var(--apt-text) !important;border:1px solid var(--apt-line) !important;}
.ai-chat-bubble.user{background:linear-gradient(135deg,#b9842f,#087163) !important;color:#fff !important;}
[data-testid="stSidebar"] .stTextInput input{
  background:#ffffff !important;color:var(--apt-text) !important;border:1px solid var(--apt-line) !important;
}
[data-testid="stSidebar"] .stTextInput input::placeholder{color:#8b9895 !important;}
[data-testid="stSidebar"] .stFormSubmitButton button{
  background:linear-gradient(135deg,#b9842f,#087163) !important;color:#fff !important;
  box-shadow:0 10px 22px rgba(15,139,120,.18) !important;
}
[data-testid="stSidebar"] .stButton > button{
  background:#ffffff !important;color:var(--apt-text) !important;border:1px solid var(--apt-line) !important;
  box-shadow:0 7px 18px rgba(23,51,47,.04) !important;
}

/* Main container */
.main .block-container{max-width:1440px !important;padding-top:22px !important;padding-bottom:42px !important;}
.block-container{color:var(--apt-text) !important;}
.main h1,.main h2,.main h3,.main h4,.main h5,.main h6{color:var(--apt-text) !important;}
.main p,.main li{color:#55635f;}

/* Hero */
.hero-banner{
  min-height:360px !important;
  border-radius:30px !important;
  overflow:hidden !important;
  border:1px solid rgba(23,51,47,.10) !important;
  box-shadow:0 28px 70px rgba(23,51,47,.12) !important;
  background-position:center !important;
}
.hero-banner::before{
  background:
    linear-gradient(90deg,rgba(247,250,249,.94) 0%,rgba(247,250,249,.78) 34%,rgba(247,250,249,.18) 67%,rgba(247,250,249,0) 100%) !important;
}
.hero-banner::after{
  background:linear-gradient(180deg,transparent,rgba(23,51,47,.08)) !important;
}
.hero-eyebrow{
  background:rgba(255,255,255,.78) !important;
  border:1px solid rgba(15,139,120,.14) !important;
  color:#b9842f !important;
  backdrop-filter:blur(10px) !important;
}
.hero-title{
  color:var(--apt-text) !important;
  text-shadow:none !important;
}
.hero-sub{color:#4f625d !important;}
.gradient-text{
  background:linear-gradient(90deg,#b9842f,#d9a84f) !important;
  -webkit-background-clip:text !important;
  -webkit-text-fill-color:transparent !important;
}

/* Cards */
.intel-box,.metric-card,.compare-card,.val-box,.poi-card,.feature-card,.property-card,.stat-card{
  background:linear-gradient(180deg,#ffffff 0%,#f7faf9 100%) !important;
  border:1px solid rgba(23,51,47,.10) !important;
  color:var(--apt-text) !important;
  box-shadow:0 18px 42px rgba(23,51,47,.07),inset 0 1px 0 rgba(255,255,255,.96) !important;
}
.intel-box:hover,.metric-card:hover,.compare-card:hover,.poi-card:hover{
  border-color:rgba(15,139,120,.24) !important;
  box-shadow:0 24px 48px rgba(23,51,47,.10) !important;
}
.metric-card .num,.intel-stat,.val-price{color:var(--apt-accent) !important;}
.metric-card .lbl,.intel-title{color:#71807b !important;}
.val-box{border:2px solid rgba(210,154,74,.28) !important;}
.val-range{background:#eef4f2 !important;color:#6b7874 !important;border:1px solid rgba(23,51,47,.08) !important;}
.poi-icon{background:linear-gradient(145deg,#e8f5f1,#f7f0e4) !important;}
.poi-title,.c-label,.c-val{color:var(--apt-text) !important;}
.poi-dist{color:var(--apt-accent) !important;}

/* Inputs */
label{color:#566761 !important;}
.stTextInput input,.stNumberInput input,.stTextArea textarea,
.stSelectbox div[data-baseweb="select"] > div,
.stMultiSelect div[data-baseweb="select"] > div,
.stDateInput div[data-baseweb="input"] > div{
  background:#ffffff !important;color:var(--apt-text) !important;
  border:1px solid rgba(23,51,47,.12) !important;border-radius:14px !important;
  box-shadow:0 7px 20px rgba(23,51,47,.035) !important;
}
.stTextInput input::placeholder,.stTextArea textarea::placeholder{color:#95a19e !important;}
[data-baseweb="popover"],[role="listbox"]{
  background:#ffffff !important;color:var(--apt-text) !important;
  border:1px solid rgba(23,51,47,.10) !important;
  box-shadow:0 20px 48px rgba(23,51,47,.12) !important;
}
[data-baseweb="option"]{color:var(--apt-text) !important;}

/* Buttons */
.stButton > button,.stFormSubmitButton button{
  background:linear-gradient(135deg,#b9842f,#087163) !important;
  color:#ffffff !important;border:0 !important;border-radius:14px !important;
  box-shadow:0 12px 26px rgba(15,139,120,.20) !important;
}
.stButton > button:hover,.stFormSubmitButton button:hover{
  transform:translateY(-2px) !important;
  box-shadow:0 18px 34px rgba(15,139,120,.24) !important;
}
.btn-back > button,[data-testid="stSidebar"] .stButton > button{
  background:#ffffff !important;color:var(--apt-text) !important;
  border:1px solid rgba(23,51,47,.10) !important;box-shadow:0 8px 18px rgba(23,51,47,.04) !important;
}

/* Tabs / dividers */
.stTabs [data-baseweb="tab-list"]{background:transparent !important;}
.stTabs [data-baseweb="tab"]{background:#eef4f2 !important;color:#697873 !important;border:1px solid transparent !important;}
.stTabs [aria-selected="true"]{background:#ffffff !important;color:var(--apt-text) !important;border-color:rgba(15,139,120,.16) !important;box-shadow:0 8px 18px rgba(23,51,47,.05) !important;}
hr{border-color:rgba(23,51,47,.08) !important;}
.stCaption,[data-testid="stCaptionContainer"]{color:#72807c !important;}

/* Flat-specific chips */
.apt-pill{background:#eef7f4 !important;border:1px solid rgba(15,139,120,.14) !important;color:#285c53 !important;}
.apt-mini{background:#ffffff !important;border:1px solid rgba(23,51,47,.08) !important;box-shadow:0 8px 20px rgba(23,51,47,.04) !important;}
.apt-mini b{color:var(--apt-text) !important;}
.apt-mini span{color:#77847f !important;}
.apt-title{color:var(--apt-text) !important;}
.apt-muted{color:#71807b !important;}

/* Light building-window texture */
[data-testid="stAppViewContainer"]::before{
  content:"";position:fixed;inset:0;pointer-events:none;opacity:.045;z-index:0;
  background-image:linear-gradient(90deg,transparent 0 94%,rgba(15,139,120,.55) 95% 96%,transparent 97%),linear-gradient(0deg,transparent 0 94%,rgba(210,154,74,.55) 95% 96%,transparent 97%);
  background-size:46px 46px;mask-image:linear-gradient(to bottom,rgba(0,0,0,.7),transparent 75%);
}

/* Remove remaining dark-mode artifacts from older style layers */
.stApp .stAlert,.stApp [data-testid="stExpander"]{color:var(--apt-text) !important;}
.stApp [data-testid="stDataFrame"]{background:#ffffff !important;}
.stApp .ag-root-wrapper,.stApp .ag-theme-streamlit{background:#ffffff !important;color:var(--apt-text) !important;}

@media (max-width:900px){.hero-title{font-size:44px !important;}}
@media (max-width:640px){.hero-title{font-size:34px !important;}}
</style>""", unsafe_allow_html=True)


# ==========================================
# ✦ FINAL WARM BROWN + BISCUIT THEME
# Replaces the remaining cool blue/teal accents with a warm apartment palette.
# ==========================================
st.markdown("""
<style>
:root{
  --warm-espresso:#5B3A29;
  --warm-brown:#8B5E3C;
  --warm-caramel:#A8754F;
  --warm-biscuit:#D7B892;
  --warm-cream:#F4E9DB;
  --warm-ivory:#FBF7F2;
  --warm-line:rgba(91,58,41,.12);
}

/* Main canvas */
.stApp,
[data-testid="stAppViewContainer"]{
  background:linear-gradient(180deg,#FBF8F4 0%,#F6EFE6 100%) !important;
  color:var(--warm-espresso) !important;
}
[data-testid="stAppViewContainer"]{
  background-image:
    radial-gradient(circle at 8% 0%,rgba(215,184,146,.20),transparent 23%),
    radial-gradient(circle at 92% 14%,rgba(139,94,60,.10),transparent 20%),
    linear-gradient(180deg,#FBF8F4 0%,#F5EEE5 100%) !important;
}

/* Top bar / logo */
.premium-commandbar,
.neo-topbar{
  background:rgba(255,252,247,.90) !important;
  border:1px solid rgba(91,58,41,.10) !important;
  box-shadow:0 14px 34px rgba(91,58,41,.07),inset 0 1px 0 rgba(255,255,255,.96) !important;
}
.neo-topbar-mark,.brand-mark,.sidebar-logo.neo-brand .brand-mark{
  background:linear-gradient(145deg,#8B5E3C,#C9A27E) !important;
  box-shadow:0 12px 28px rgba(139,94,60,.22),inset 0 1px 0 rgba(255,255,255,.55) !important;
}
.premium-commandbar-title span,
.sidebar-logo.neo-brand .brand-copy h2 span{
  background:linear-gradient(90deg,#6B4630,#A8754F,#D7B892) !important;
  -webkit-background-clip:text !important;
  -webkit-text-fill-color:transparent !important;
}
.premium-commandbar-title i{background:#A8754F !important;box-shadow:0 0 0 5px rgba(168,117,79,.10),0 0 16px rgba(168,117,79,.20) !important;}
.neo-mini-pill .material-symbols-rounded{color:#8B5E3C !important;}

/* Hero */
.hero-eyebrow{background:rgba(255,248,239,.84) !important;border-color:rgba(139,94,60,.16) !important;color:#80553A !important;}
.hero-eyebrow::before{color:#A8754F !important;}
.gradient-text{background:linear-gradient(90deg,#6B4630 0%,#9A6A46 45%,#C9A27E 100%) !important;-webkit-background-clip:text !important;-webkit-text-fill-color:transparent !important;}
.hero-banner::after{background:radial-gradient(circle,rgba(201,162,126,.22),rgba(168,117,79,.05) 55%,transparent 72%) !important;}

/* Cards */
.intel-box,.metric-card,.val-box,.compare-card,.poi-card,.property-summary-card,.feature-card,.property-card,.stat-card{
  background:linear-gradient(180deg,#FFFFFF 0%,#FBF6F0 100%) !important;
  border:1px solid rgba(91,58,41,.11) !important;
  color:var(--warm-espresso) !important;
  box-shadow:0 16px 38px rgba(91,58,41,.07),inset 0 1px 0 rgba(255,255,255,.96) !important;
}
.intel-box:hover,.metric-card:hover,.compare-card:hover,.poi-card:hover,.property-summary-card:hover,.property-card:hover{
  border-color:rgba(139,94,60,.24) !important;
  box-shadow:0 24px 54px rgba(91,58,41,.11) !important;
}
.metric-card .num,.intel-stat,.val-price,.poi-dist{color:#8B5E3C !important;}
.metric-card .lbl,.intel-title{color:#7B6A5D !important;}
.val-box{border-color:rgba(168,117,79,.30) !important;}
.val-range{background:#F4E9DB !important;color:#755B48 !important;border-color:rgba(139,94,60,.10) !important;}
.poi-icon{background:linear-gradient(145deg,#F0DFCC,#FBF5EE) !important;}

/* Pills / chips */
.apt-pill{background:#F1E4D5 !important;border-color:rgba(139,94,60,.15) !important;color:#6B4630 !important;}
.apt-mini{background:#FFFDFC !important;border-color:rgba(91,58,41,.09) !important;}
.side-live{color:#8B5E3C !important;}
.live-dot{background:#A8754F !important;box-shadow:0 0 0 4px rgba(168,117,79,.10),0 0 14px rgba(168,117,79,.24) !important;}

/* Sidebar */
[data-testid="stSidebar"]{
  background:linear-gradient(180deg,#FAF6F0 0%,#F3E8DC 100%) !important;
  border-right:1px solid rgba(91,58,41,.10) !important;
}
[data-testid="stSidebar"] div[role="radiogroup"]>label{
  background:rgba(255,255,255,.50) !important;
  border-color:transparent !important;
}
[data-testid="stSidebar"] div[role="radiogroup"]>label:hover{
  background:linear-gradient(90deg,rgba(215,184,146,.28),rgba(255,255,255,.62)) !important;
  border-color:rgba(139,94,60,.15) !important;
}
[data-testid="stSidebar"] div[role="radiogroup"]>label[data-checked="true"]{
  background:linear-gradient(100deg,rgba(215,184,146,.42),rgba(255,255,255,.68)) !important;
  border-color:rgba(139,94,60,.20) !important;
  box-shadow:inset 3px 0 0 #8B5E3C,0 10px 24px rgba(91,58,41,.08) !important;
}
[data-testid="stSidebar"] div[role="radiogroup"]>label[data-checked="true"] p{color:#6B4630 !important;}

.ai-command-card{
  background:linear-gradient(145deg,rgba(215,184,146,.34),rgba(255,255,255,.72)) !important;
  border-color:rgba(139,94,60,.18) !important;
  box-shadow:0 18px 42px rgba(91,58,41,.09),inset 0 1px 0 rgba(255,255,255,.90) !important;
}
.ai-avatar,.ai-chat-avatar.bot{
  background:linear-gradient(135deg,#8B5E3C,#C9A27E) !important;
  box-shadow:0 12px 26px rgba(139,94,60,.20) !important;
}
.ai-avatar-ring{border-color:rgba(201,162,126,.24) !important;}
.ai-status-pill{border-color:rgba(139,94,60,.15) !important;background:rgba(215,184,146,.16) !important;}
.ai-chat-bubble.user{background:linear-gradient(135deg,#8B5E3C,#B7835A) !important;box-shadow:0 8px 20px rgba(139,94,60,.18) !important;}
.ai-chat-bubble.bot{background:#FFFDF9 !important;border-color:rgba(139,94,60,.12) !important;}
.ai-chat-avatar.user{background:#F1E4D5 !important;color:#6B4630 !important;border-color:rgba(139,94,60,.10) !important;}
.ai-chat-panel::-webkit-scrollbar-thumb{background:linear-gradient(#A8754F,#D7B892) !important;}

/* Inputs */
.stTextInput input,.stNumberInput input,.stTextArea textarea,
.stSelectbox div[data-baseweb="select"] > div,
.stMultiSelect div[data-baseweb="select"] > div,
.stDateInput div[data-baseweb="input"] > div{
  background:#FFFDFC !important;
  color:var(--warm-espresso) !important;
  border-color:rgba(91,58,41,.13) !important;
  box-shadow:0 7px 20px rgba(91,58,41,.035) !important;
}
.stTextInput input:focus,.stNumberInput input:focus,.stTextArea textarea:focus,
[data-baseweb="select"] > div:focus-within{
  border-color:rgba(139,94,60,.46) !important;
  box-shadow:0 0 0 4px rgba(215,184,146,.18),0 8px 24px rgba(91,58,41,.07) !important;
}

/* Buttons */
.stButton > button,.stFormSubmitButton > button{
  background:linear-gradient(135deg,#7A4F32 0%,#9A6A46 50%,#C9A27E 100%) !important;
  color:#FFFFFF !important;
  border:0 !important;
  box-shadow:0 12px 26px rgba(139,94,60,.20),inset 0 1px 0 rgba(255,255,255,.18) !important;
}
.stButton > button:hover,.stFormSubmitButton > button:hover{
  box-shadow:0 18px 34px rgba(139,94,60,.25) !important;
}
.btn-back > button,[data-testid="stSidebar"] .stButton > button{
  background:#FFFDFC !important;color:#6B4630 !important;border-color:rgba(91,58,41,.11) !important;
}

/* Tabs */
.stTabs [data-baseweb="tab"]{background:#F1E7DC !important;color:#7A6A5D !important;}
.stTabs [aria-selected="true"]{background:#FFFFFF !important;color:#6B4630 !important;border-color:rgba(139,94,60,.16) !important;}

/* Top status chip */
.premium-chip{background:#F1E4D5 !important;border-color:rgba(139,94,60,.14) !important;color:#6B4630 !important;}
.premium-dot{background:#A8754F !important;box-shadow:0 0 0 4px rgba(168,117,79,.08),0 0 13px rgba(168,117,79,.22) !important;}

/* Warm building-window texture */
[data-testid="stAppViewContainer"]::before{
  background-image:
    linear-gradient(90deg,transparent 0 94%,rgba(139,94,60,.32) 95% 96%,transparent 97%),
    linear-gradient(0deg,transparent 0 94%,rgba(215,184,146,.40) 95% 96%,transparent 97%) !important;
}
</style>
""", unsafe_allow_html=True)

# ==========================================
# ✦ V23 WARM BROWN + BISCUIT BALANCED THEME
# Visual balance: ~70% warm ivory/cream, ~20% biscuit/beige, ~10% rich brown.
# ==========================================
st.markdown(r"""
<style>
:root{
  --wb-ivory:#FBF7F2;
  --wb-cream:#F6EBDD;
  --wb-biscuit:#E1C6A8;
  --wb-sand:#D4B18D;
  --wb-brown:#7A4F32;
  --wb-brown2:#916344;
  --wb-espresso:#4A3426;
  --wb-muted:#78695D;
  --wb-line:rgba(122,79,50,.13);
  --wb-shadow:0 18px 42px rgba(74,52,38,.08);
}

html,body{background:var(--wb-ivory)!important;color:var(--wb-espresso)!important}
.stApp,[data-testid="stAppViewContainer"]{
  background:
    radial-gradient(circle at 7% 0%,rgba(225,198,168,.28),transparent 24%),
    radial-gradient(circle at 94% 14%,rgba(122,79,50,.07),transparent 19%),
    linear-gradient(180deg,#FFFDF9 0%,var(--wb-ivory) 52%,#F7EEE4 100%)!important;
  color:var(--wb-espresso)!important;
}
[data-testid="stAppViewContainer"]:before{
  background-image:
    linear-gradient(90deg,transparent 0 95%,rgba(122,79,50,.16) 96%,transparent 97%),
    linear-gradient(0deg,transparent 0 95%,rgba(212,177,141,.22) 96%,transparent 97%)!important;
  background-size:52px 52px!important;
  opacity:.65!important;
}

/* Header */
.editorial-topbar,.premium-commandbar,.neo-topbar{
  background:rgba(255,252,247,.92)!important;
  border:1px solid var(--wb-line)!important;
  box-shadow:0 16px 38px rgba(74,52,38,.07),inset 0 1px 0 rgba(255,255,255,.95)!important;
}
.editorial-topbar:before{background:linear-gradient(180deg,var(--wb-brown),var(--wb-biscuit))!important}
.editorial-topbar:after{background:linear-gradient(90deg,transparent,rgba(255,255,255,.52),transparent)!important}
.editorial-topbar .neo-topbar-mark,.neo-topbar-mark,.brand-mark,.sidebar-logo.neo-brand .brand-mark{
  background:linear-gradient(145deg,var(--wb-brown),var(--wb-sand))!important;
  box-shadow:0 12px 28px rgba(122,79,50,.20),inset 0 1px 0 rgba(255,255,255,.46)!important;
}
.editorial-topbar .premium-commandbar-title,.editorial-topbar .premium-commandbar-title span{color:var(--wb-espresso)!important;background:none!important;-webkit-text-fill-color:initial!important}
.editorial-topbar .premium-commandbar-title span{color:var(--wb-brown)!important}
.editorial-topbar .neo-topbar-sub{color:var(--wb-muted)!important}
.premium-chip,.neo-mini-pill{
  background:#F2E5D7!important;color:var(--wb-brown)!important;border-color:rgba(122,79,50,.14)!important;
}
.premium-dot{background:var(--wb-brown2)!important;box-shadow:0 0 0 4px rgba(145,99,68,.09),0 0 14px rgba(145,99,68,.18)!important}
.neo-mini-pill .material-symbols-rounded{color:var(--wb-brown)!important}

/* Hero: warm daylight apartment mood */
.hero-banner{
  border:1px solid rgba(122,79,50,.12)!important;
  box-shadow:0 28px 68px rgba(74,52,38,.14)!important;
}
.hero-banner::before{
  background:
    linear-gradient(90deg,rgba(54,38,28,.54) 0%,rgba(54,38,28,.28) 42%,rgba(54,38,28,.04) 82%),
    linear-gradient(180deg,rgba(67,48,36,.02),rgba(67,48,36,.14))!important;
}
.hero-eyebrow{background:rgba(255,248,239,.84)!important;color:var(--wb-brown)!important;border-color:rgba(122,79,50,.18)!important}
.hero-eyebrow:before{color:var(--wb-brown2)!important}
.hero-title{color:#FFF9F3!important}
.gradient-text{background:linear-gradient(90deg,#FFF8EF 0%,#E7C8A7 42%,#D6A980 100%)!important;-webkit-background-clip:text!important;-webkit-text-fill-color:transparent!important}
.hero-sub{color:rgba(255,248,239,.88)!important}

/* Main cards: mostly light surface, biscuit as secondary layer */
.intel-box,.metric-card,.val-box,.compare-card,.poi-card,.property-summary-card,.feature-card,.property-card,.stat-card{
  background:linear-gradient(180deg,#FFFFFF 0%,#FCF8F3 100%)!important;
  border:1px solid var(--wb-line)!important;
  color:var(--wb-espresso)!important;
  box-shadow:var(--wb-shadow),inset 0 1px 0 rgba(255,255,255,.96)!important;
}
.intel-box:before,.metric-card:before,.compare-card:before,.val-box:before,.poi-card:before{
  background:linear-gradient(90deg,var(--wb-brown),var(--wb-biscuit),var(--wb-sand))!important;
  opacity:.82!important;
}
.intel-box:hover,.metric-card:hover,.compare-card:hover,.poi-card:hover,.property-summary-card:hover,.property-card:hover{
  border-color:rgba(122,79,50,.24)!important;
  box-shadow:0 25px 54px rgba(74,52,38,.12)!important;
}
.card-header,h1,h2,h3,.poi-title{color:var(--wb-espresso)!important}
.intel-title{color:var(--wb-muted)!important}
.intel-stat,.metric-card .num,.val-price{color:var(--wb-brown)!important}
.metric-card .lbl{color:var(--wb-muted)!important}
.val-box{border-color:rgba(145,99,68,.25)!important}
.val-range{background:var(--wb-cream)!important;color:#6F5847!important;border-color:rgba(122,79,50,.10)!important}

/* Property / flat cards */
.property-card .property-card-badge,
.property-summary-card .property-card-badge,
.apt-pill{
  background:#F3E5D6!important;color:var(--wb-brown)!important;border-color:rgba(122,79,50,.14)!important;
}
.property-card .property-card-price{color:var(--wb-brown)!important}
.property-card .property-card-meta{color:var(--wb-muted)!important}

/* Inputs */
label{color:var(--wb-muted)!important}
.stTextInput input,.stNumberInput input,.stTextArea textarea,
.stSelectbox div[data-baseweb="select"]>div,
.stMultiSelect div[data-baseweb="select"]>div,
.stDateInput div[data-baseweb="input"]>div{
  background:#FFFDFC!important;color:var(--wb-espresso)!important;
  border:1px solid rgba(122,79,50,.13)!important;border-radius:14px!important;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.98),0 7px 20px rgba(74,52,38,.035)!important;
}
.stTextInput input:focus,.stNumberInput input:focus,.stTextArea textarea:focus,
.stSelectbox div[data-baseweb="select"]>div:focus-within,
.stMultiSelect div[data-baseweb="select"]>div:focus-within{
  border-color:rgba(122,79,50,.46)!important;
  box-shadow:0 0 0 4px rgba(225,198,168,.25),0 8px 24px rgba(74,52,38,.07)!important;
}
[data-baseweb="popover"]{background:#FFFDF9!important;border:1px solid rgba(122,79,50,.12)!important}
[data-baseweb="option"]{background:#FFFDF9!important;color:var(--wb-espresso)!important}
[data-baseweb="option"]:hover{background:#F3E5D6!important}

/* Buttons: only the 10% brown accent */
.stButton>button,.stFormSubmitButton>button{
  background:linear-gradient(135deg,#7A4F32 0%,#946246 55%,#B88661 100%)!important;
  color:#fff!important;border:0!important;border-radius:14px!important;
  box-shadow:0 12px 26px rgba(122,79,50,.20),inset 0 1px 0 rgba(255,255,255,.18)!important;
}
.stButton>button:hover,.stFormSubmitButton>button:hover{box-shadow:0 18px 34px rgba(122,79,50,.27)!important;transform:translateY(-2px)!important}
.btn-back>button,[data-testid="stSidebar"] .stButton>button{background:#FFFDFC!important;color:var(--wb-brown)!important;border-color:rgba(122,79,50,.12)!important}

/* Sidebar */
[data-testid="stSidebar"]{
  background:linear-gradient(180deg,#FFFDF9 0%,#F6EBDD 100%)!important;
  border-right:1px solid rgba(122,79,50,.10)!important;
  box-shadow:10px 0 35px rgba(74,52,38,.06)!important;
}
[data-testid="stSidebarContent"]{background:transparent!important}
[data-testid="stSidebar"] *{color:var(--wb-espresso)!important}
[data-testid="stSidebar"] .sidebar-logo.neo-brand{
  background:linear-gradient(145deg,#FFFDF9,#F2E3D3)!important;
  border:1px solid rgba(122,79,50,.12)!important;
  box-shadow:0 15px 34px rgba(74,52,38,.07)!important;
}
[data-testid="stSidebar"] div[role="radiogroup"]>label{background:rgba(255,255,255,.48)!important;border-color:transparent!important}
[data-testid="stSidebar"] div[role="radiogroup"]>label:hover{background:linear-gradient(90deg,rgba(225,198,168,.34),rgba(255,255,255,.68))!important;border-color:rgba(122,79,50,.12)!important}
[data-testid="stSidebar"] div[role="radiogroup"]>label[data-checked="true"]{
  background:linear-gradient(100deg,rgba(225,198,168,.42),rgba(255,255,255,.78))!important;
  border-color:rgba(122,79,50,.18)!important;
  box-shadow:inset 3px 0 0 var(--wb-brown),0 10px 22px rgba(74,52,38,.07)!important;
}
[data-testid="stSidebar"] div[role="radiogroup"]>label[data-checked="true"] p{color:var(--wb-brown)!important}

/* AI */
.ai-command-card{background:linear-gradient(145deg,rgba(225,198,168,.30),rgba(255,255,255,.78))!important;border-color:rgba(122,79,50,.16)!important;box-shadow:0 18px 42px rgba(74,52,38,.09)!important}
.ai-avatar,.ai-chat-avatar.bot{background:linear-gradient(135deg,#7A4F32,#C39A72)!important;box-shadow:0 12px 26px rgba(122,79,50,.20)!important}
.ai-chat-bubble.bot{background:#FFFDF9!important;border-color:rgba(122,79,50,.12)!important;color:var(--wb-espresso)!important}
.ai-chat-bubble.user{background:linear-gradient(135deg,#7A4F32,#A66F4C)!important;color:#fff!important}
.ai-chat-avatar.user{background:#F2E5D7!important;color:var(--wb-brown)!important;border-color:rgba(122,79,50,.10)!important}

/* Neighborhood / icons */
.poi-icon{background:linear-gradient(145deg,#F3E3D2,#FFF9F1)!important;border:1px solid rgba(122,79,50,.08)!important}
.poi-dist{color:var(--wb-brown2)!important}

/* Tabs / dividers */
.stTabs [data-baseweb="tab"]{background:#F2E7DB!important;color:#786A5D!important}
.stTabs [aria-selected="true"]{background:#FFFDFC!important;color:var(--wb-brown)!important;border-color:rgba(122,79,50,.16)!important;box-shadow:0 8px 18px rgba(74,52,38,.06)!important}
hr{border-color:rgba(122,79,50,.09)!important}
.stCaption,[data-testid="stCaptionContainer"]{color:var(--wb-muted)!important}

/* Keep chart labels readable in the warm theme */
.js-plotly-plot text,.plot-container .main-svg text{fill:#5C4A3C!important}

/* Warm scrollbar */
[data-testid="stSidebarContent"]{scrollbar-color:#C9A37B transparent}
[data-testid="stSidebarContent"]::-webkit-scrollbar-thumb{background:linear-gradient(#B88762,#E1C6A8)!important;border-radius:99px}

@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.001ms!important;transition-duration:.001ms!important}}
</style>
""", unsafe_allow_html=True)


# Final warm-gold palette safeguards for any legacy components
st.markdown("""
<style class="gold-theme-fallback">
:root{--brand-gold:#DAA752;--brand-gold-dark:#8A5A18;--brand-cream:#FBF7EF;--brand-biscuit:#F1E2C9;}
.stApp{color:#33261B;}
[data-testid="stSidebar"]{background:rgba(255,252,246,.98)!important;}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"]{border-left-color:#DAA752!important;background:rgba(218,167,82,.10)!important;}
.stButton>button{background:linear-gradient(135deg,#DAA752,#C99543)!important;color:#fff!important;}
</style>
""", unsafe_allow_html=True)

# ============================================================================
# FIRST-DESIGN RESTORE — LIGHT REAL-ESTATE / FLAT MARKETPLACE STYLE
# Matches the original warm-light apartment UI: white sidebar, pale blue-gray
# canvas, large home hero, warm-gold accents, clean cards and spacious forms.
# This final layer intentionally overrides newer experimental themes above.
# ============================================================================
st.markdown("""
<style id="first-design-restore">
:root{
  --fd-gold:#DAA752;
  --fd-gold-2:#C99543;
  --fd-gold-soft:#F7E9C8;
  --fd-gold-pale:#FCF6E9;
  --fd-bg:#EEF3F8;
  --fd-surface:#FFFFFF;
  --fd-surface-2:#F8FAFC;
  --fd-text:#172033;
  --fd-muted:#687386;
  --fd-line:#E4EAF0;
  --fd-shadow:0 12px 32px rgba(31,49,74,.08);
}

/* App shell */
html,body{background:var(--fd-bg)!important;}
.stApp{
  background:linear-gradient(180deg,#F2F6FA 0%,#EAF0F6 100%)!important;
  background-size:auto!important;
  animation:none!important;
  color:var(--fd-text)!important;
}
section.main,[data-testid="stAppViewContainer"]{background:transparent!important;}
[data-testid="stMainBlockContainer"]{
  max-width:none!important;
  width:100%!important;
  padding:18px 36px 48px 36px!important;
}
.main .block-container{max-width:none!important;padding-top:0!important;}
header[data-testid="stHeader"]{background:transparent!important;height:42px!important;}

/* Sidebar — original clean white navigation */
[data-testid="stSidebar"]{
  width:330px!important;
  min-width:330px!important;
  max-width:330px!important;
  background:#FFFFFF!important;
  border-right:1px solid #E7ECF1!important;
  box-shadow:6px 0 26px rgba(40,57,78,.05)!important;
  backdrop-filter:none!important;
}
[data-testid="stSidebarContent"]{
  height:100vh!important;
  overflow-y:auto!important;
  overflow-x:hidden!important;
  padding:18px 18px 28px!important;
  background:#FFFFFF!important;
}
[data-testid="stSidebar"] *{color:var(--fd-text)!important;}
[data-testid="stSidebarNav"]{display:none!important;}
[data-testid="stSidebar"] .sidebar-logo,
[data-testid="stSidebar"] .sidebar-logo.neo-brand{
  background:transparent!important;
  border:0!important;
  box-shadow:none!important;
  padding:8px 0 24px!important;
}
[data-testid="stSidebar"] .sidebar-logo h2{
  font-size:27px!important;
  line-height:1.16!important;
  font-weight:800!important;
  letter-spacing:-.8px!important;
  color:#172033!important;
}
[data-testid="stSidebar"] .sidebar-logo p{
  margin-top:22px!important;
  color:#C99239!important;
  font-size:11px!important;
  font-weight:800!important;
  letter-spacing:1.4px!important;
}

/* Sidebar nav */
[data-testid="stSidebar"] div[role="radiogroup"]{gap:5px!important;}
[data-testid="stSidebar"] div[role="radiogroup"] > label{
  min-height:50px!important;
  padding:12px 14px!important;
  margin:3px 0!important;
  border-radius:12px!important;
  border:1px solid transparent!important;
  background:transparent!important;
  transform:none!important;
  box-shadow:none!important;
}
[data-testid="stSidebar"] div[role="radiogroup"] > label:hover{
  background:#FBF5EA!important;
  border-color:#F0DFC1!important;
  transform:translateX(2px)!important;
}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"]{
  background:#FFF8EA!important;
  border-color:#F0D59F!important;
  border-left:4px solid var(--fd-gold)!important;
  box-shadow:0 6px 18px rgba(218,167,82,.08)!important;
}
[data-testid="stSidebar"] div[role="radiogroup"] p{
  font-size:15px!important;
  font-weight:700!important;
  color:#172033!important;
}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] p{
  color:#1D2432!important;
}

/* Sidebar buttons / controls */
[data-testid="stSidebar"] .stButton>button{
  background:#E0AD52!important;
  color:#1E2330!important;
  border:0!important;
  border-radius:13px!important;
  min-height:54px!important;
  box-shadow:0 8px 16px rgba(218,167,82,.22)!important;
}
[data-testid="stSidebar"] .stButton>button:hover{
  background:#D4A047!important;
  transform:translateY(-1px)!important;
}
[data-testid="stSidebar"] .stSelectbox div[data-baseweb="select"]>div,
[data-testid="stSidebar"] .stTextInput input{
  background:#FFFFFF!important;
  border:1px solid #E2E7ED!important;
  color:#172033!important;
  border-radius:10px!important;
}

/* Hero — original large residential image */
.hero-banner{
  min-height:365px!important;
  height:365px!important;
  padding:44px 48px!important;
  margin:0 0 30px!important;
  border-radius:0 0 34px 34px!important;
  border:1px solid #DCE3EA!important;
  box-shadow:0 16px 36px rgba(51,70,92,.10)!important;
  background-size:cover!important;
  background-position:center center!important;
}
.hero-banner::before{
  background:linear-gradient(90deg,rgba(255,255,255,.93) 0%,rgba(255,255,255,.78) 27%,rgba(255,255,255,.30) 58%,rgba(255,255,255,.04) 100%)!important;
}
.hero-content{max-width:800px!important;}
.hero-eyebrow{
  color:#C9963E!important;
  background:transparent!important;
  border:0!important;
  padding:0!important;
  font-size:12px!important;
  font-weight:800!important;
  letter-spacing:4px!important;
  margin-bottom:24px!important;
}
.hero-title{
  color:#2B3140!important;
  font-size:49px!important;
  font-weight:800!important;
  line-height:1.08!important;
  letter-spacing:-1.8px!important;
  margin:0!important;
}
.gradient-text{
  background:linear-gradient(90deg,#C9973D 0%,#F0C66A 100%)!important;
  -webkit-background-clip:text!important;
  -webkit-text-fill-color:transparent!important;
}
.hero-sub{
  color:#2D394C!important;
  font-size:17px!important;
  line-height:1.55!important;
  margin-top:24px!important;
  font-weight:500!important;
  max-width:850px!important;
}

/* Main cards/sections */
.intel-box,.metric-card,.val-box,.compare-card,.poi-card,.property-summary-card,.feature-card,.property-card,.stat-card{
  background:#FFFFFF!important;
  border:1px solid var(--fd-line)!important;
  color:var(--fd-text)!important;
  border-radius:18px!important;
  box-shadow:var(--fd-shadow)!important;
  backdrop-filter:none!important;
}
.intel-box:hover,.metric-card:hover,.compare-card:hover,.poi-card:hover,.property-summary-card:hover,.property-card:hover{
  transform:translateY(-2px)!important;
  border-color:#E9D8BA!important;
  box-shadow:0 18px 36px rgba(31,49,74,.11)!important;
}
.card-header,h1,h2,h3,.poi-title{color:#172033!important;}
.intel-title,.metric-card .lbl{color:#6B7686!important;}
.intel-stat,.metric-card .num,.val-price{color:#C28D31!important;}

/* Inputs — flat, pale, spacious */
label{color:#283549!important;font-weight:600!important;}
.stTextInput input,.stNumberInput input,.stTextArea textarea,
.stSelectbox div[data-baseweb="select"]>div,
.stMultiSelect div[data-baseweb="select"]>div,
.stDateInput div[data-baseweb="input"]>div{
  background:#F9FBFD!important;
  color:#1B2739!important;
  border:1px solid #DCE4EB!important;
  border-radius:12px!important;
  box-shadow:none!important;
}
.stTextInput input:focus,.stNumberInput input:focus,.stTextArea textarea:focus,
.stSelectbox div[data-baseweb="select"]>div:focus-within,
.stMultiSelect div[data-baseweb="select"]>div:focus-within{
  border-color:#D8AE63!important;
  box-shadow:0 0 0 3px rgba(218,167,82,.14)!important;
}

/* Primary gold action */
.stButton>button,.stFormSubmitButton>button{
  background:linear-gradient(90deg,#E1B054,#D3A047)!important;
  color:#FFFFFF!important;
  border:0!important;
  border-radius:13px!important;
  min-height:54px!important;
  font-weight:800!important;
  box-shadow:0 8px 18px rgba(218,167,82,.22)!important;
  text-shadow:none!important;
}
.stButton>button:hover,.stFormSubmitButton>button:hover{
  background:linear-gradient(90deg,#D9A647,#C9953D)!important;
  transform:translateY(-2px)!important;
  box-shadow:0 12px 24px rgba(218,167,82,.28)!important;
}

/* Valuation result */
.val-box{
  border:1px solid #E7D4AF!important;
  background:linear-gradient(180deg,#FFFDF9 0%,#FFF8EE 100%)!important;
}
.val-price{font-size:46px!important;color:#C69236!important;}
.val-range{background:#F7F0E5!important;color:#7C6A56!important;}

/* AI panel */
.ai-engine-status{background:#F4FAF5!important;border:1px solid #D9EBDD!important;color:#315741!important;}
.ai-status-dot{background:#35B86B!important;box-shadow:none!important;}
.ai-chat-bubble.bot{background:#FFFFFF!important;color:#172033!important;border:1px solid #E6EBF0!important;}
.ai-chat-bubble.user{background:linear-gradient(135deg,#E0AD52,#C8943F)!important;color:#FFFFFF!important;}
.ai-command-card{background:#FFF9EE!important;border:1px solid #EDD7AE!important;box-shadow:0 12px 28px rgba(218,167,82,.08)!important;}

/* Neighborhood */
.poi-card{background:#FFFFFF!important;}
.poi-icon{background:#FBF4E8!important;border:1px solid #F0DFBF!important;}
.poi-dist{color:#B98229!important;}

/* Tabs */
.stTabs [data-baseweb="tab"]{background:#F1F4F7!important;color:#6B7686!important;border-radius:10px!important;}
.stTabs [aria-selected="true"]{background:#FFFFFF!important;color:#B7832C!important;border:1px solid #EED7AD!important;box-shadow:0 4px 12px rgba(30,45,65,.06)!important;}

/* Plots */
.js-plotly-plot text,.plot-container .main-svg text{fill:#4E5A6B!important;}

/* Scrollbars */
[data-testid="stSidebarContent"]::-webkit-scrollbar{width:8px!important;}
[data-testid="stSidebarContent"]::-webkit-scrollbar-thumb{background:#D9B476!important;border-radius:20px!important;}
[data-testid="stSidebarContent"]::-webkit-scrollbar-track{background:transparent!important;}

/* Remove experimental neon/animated chrome */
.editorial-topbar,.premium-commandbar,.neo-topbar{box-shadow:none!important;}
[data-testid="stSidebar"] .neo-brand .brand-mark,
.sidebar-logo.neo-brand .brand-mark{background:linear-gradient(145deg,#E0AD52,#C89238)!important;box-shadow:0 8px 18px rgba(218,167,82,.20)!important;}

@media(max-width:1100px){
  [data-testid="stSidebar"]{width:300px!important;min-width:300px!important;max-width:300px!important;}
  [data-testid="stMainBlockContainer"]{padding:16px 22px 40px!important;}
  .hero-banner{min-height:320px!important;height:320px!important;}
  .hero-title{font-size:42px!important;}
}
@media(max-width:800px){
  [data-testid="stMainBlockContainer"]{padding:12px 14px 28px!important;}
  .hero-banner{min-height:300px!important;height:auto!important;padding:34px 26px!important;border-radius:0 0 24px 24px!important;}
  .hero-title{font-size:34px!important;}
  .hero-sub{font-size:14px!important;}
}
</style>
""", unsafe_allow_html=True)


# FINAL GOLD THEME OVERRIDES
# Keep the original clean apartment design, but make all key branding/value
# elements consistently use the requested warm-gold palette.
st.markdown("""
<style>
:root{
  --brand-gold:#DAA752;
  --brand-gold-dark:#A8754F;
  --brand-gold-soft:#F2E4CD;
  --brand-brown:#6B4630;
  --brand-cream:#FBF7EF;
}

/* SmartEstate brand */
.sidebar-logo.neo-brand .brand-mark,
[data-testid="stSidebar"] .sidebar-logo.neo-brand .brand-mark,
.neo-topbar-mark,
.editorial-topbar .neo-topbar-mark{
  background:linear-gradient(145deg,#DAA752 0%,#C7953E 100%) !important;
  border:1px solid rgba(122,79,50,.16) !important;
  box-shadow:0 10px 24px rgba(218,167,82,.24), inset 0 1px 0 rgba(255,255,255,.55) !important;
}

.sidebar-logo.neo-brand .brand-mark svg,
.neo-topbar-mark svg,
.editorial-topbar .neo-topbar-mark svg{
  stroke:#FFFFFF !important;
  fill:none !important;
}

.sidebar-logo.neo-brand .brand-copy h2{
  color:#3B2A1E !important;
}
.sidebar-logo.neo-brand .brand-copy h2 span{
  color:#DAA752 !important;
  background:none !important;
  -webkit-background-clip:initial !important;
  -webkit-text-fill-color:#DAA752 !important;
}
.sidebar-logo.neo-brand .brand-copy p{
  color:#A7835A !important;
}

/* Gold instead of blue for all major valuation numbers */
.val-price,
.val-box .val-price,
.metric-card .num,
.intel-stat,
.property-summary-card .price,
.property-card .price,
.price-value,
.estimated-price,
.market-price,
.amount-value{
  color:#DAA752 !important;
  -webkit-text-fill-color:#DAA752 !important;
  background:none !important;
  text-shadow:0 2px 10px rgba(218,167,82,.12) !important;
}

.val-box .val-price{
  font-size:48px !important;
  font-weight:900 !important;
}

/* Gold headings/labels connected with valuation */
.val-box > div:first-child,
.metric-card .lbl,
.intel-title{
  color:#A8754F !important;
}

/* Active navigation uses the same gold accent */
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"]{
  background:linear-gradient(90deg,rgba(218,167,82,.15),rgba(218,167,82,.045)) !important;
  border-color:rgba(218,167,82,.25) !important;
  box-shadow:inset 3px 0 0 #DAA752 !important;
}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] p{
  color:#8A5A18 !important;
}

/* Keep the hero/property accent gold, never cyan/blue */
.hero-eyebrow,
.hero-banner .gradient-text{
  color:#DAA752 !important;
}
.hero-banner .gradient-text{
  background:linear-gradient(90deg,#A8754F 0%,#DAA752 52%,#E6C27A 100%) !important;
  -webkit-background-clip:text !important;
  -webkit-text-fill-color:transparent !important;
}

/* Gold focus ring */
.stTextInput input:focus,
.stNumberInput input:focus,
.stTextArea textarea:focus,
.stSelectbox div[data-baseweb="select"] > div:focus-within,
.stMultiSelect div[data-baseweb="select"] > div:focus-within{
  border-color:#DAA752 !important;
  box-shadow:0 0 0 3px rgba(218,167,82,.16) !important;
}

/* Gold tabs */
.stTabs [aria-selected="true"]{
  color:#8A5A18 !important;
  border-color:rgba(218,167,82,.28) !important;
  box-shadow:0 5px 14px rgba(218,167,82,.10) !important;
}

.account-card {
  display:flex; align-items:center; gap:10px; margin:10px 0 12px; padding:10px 11px;
  border:1px solid rgba(138,90,24,.14); border-radius:16px; background:rgba(255,255,255,.72);
  box-shadow:0 8px 18px rgba(92,65,37,.05);
}
.account-avatar {
  width:36px; height:36px; border-radius:12px; display:grid; place-items:center; flex:0 0 auto;
  color:#fff; font-weight:900; background:linear-gradient(135deg,#DAA752,#B87922);
}
.account-copy{min-width:0;}
.account-name{font-size:13px; font-weight:900; color:#4B3324; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.account-email{font-size:10px; color:#8B7867; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}

/* AI user bubble and assistant status stay within the same palette */
.ai-chat-bubble.user{
  background:linear-gradient(135deg,#DAA752,#B9853B) !important;
}
.ai-status-dot{
  background:#DAA752 !important;
  box-shadow:0 0 10px rgba(218,167,82,.28) !important;
}
</style>
""", unsafe_allow_html=True)


# ==========================================
# SMART HOME HUB CSS
# ==========================================
st.markdown("""
<style>
.hub-hero{position:relative;overflow:hidden;border-radius:28px;padding:44px 48px;margin:4px 0 24px;background:linear-gradient(135deg,#fffdf8 0%,#f5e7d2 58%,#ead4b4 100%);border:1px solid rgba(218,167,82,.28);box-shadow:0 20px 50px rgba(92,65,37,.10);min-height:240px}
.hub-hero:after{content:"";position:absolute;width:260px;height:260px;border-radius:50%;right:-50px;top:-70px;background:radial-gradient(circle,rgba(218,167,82,.32),rgba(218,167,82,0) 67%);animation:hubFloat 7s ease-in-out infinite}
.hub-hero-copy{position:relative;z-index:2;max-width:760px}.hub-eyebrow{font-size:12px;letter-spacing:3px;font-weight:900;color:#9A6A2F;text-transform:uppercase;margin-bottom:10px}.hub-hero h1{font-size:42px;line-height:1.05;color:#3B2A1E!important;margin:0 0 14px;font-weight:900}.hub-hero p{max-width:720px;color:#7A6757;font-size:15px;line-height:1.7;margin:0}.hub-hero-orb{position:absolute;right:90px;bottom:36px;font-size:94px;filter:drop-shadow(0 18px 22px rgba(116,79,36,.20));animation:hubFloat 4.8s ease-in-out infinite;z-index:2}
.hub-filter-shell{background:rgba(255,252,247,.92);border:1px solid rgba(120,82,41,.12);border-radius:22px;padding:18px 20px;margin-bottom:20px;box-shadow:0 12px 30px rgba(92,65,37,.06)}
.hub-prop-card{border:1px solid rgba(120,82,41,.12);border-radius:20px;overflow:hidden;background:#fffdf9;box-shadow:0 14px 34px rgba(89,62,35,.08);transition:transform .28s ease,box-shadow .28s ease;margin-bottom:12px}.hub-prop-card:hover{transform:translateY(-6px);box-shadow:0 22px 42px rgba(89,62,35,.15)}.hub-prop-image{height:150px;overflow:hidden;background:#eee}.hub-prop-image img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .5s ease}.hub-prop-card:hover .hub-prop-image img{transform:scale(1.05)}.hub-prop-body{padding:15px}.hub-prop-top{display:flex;justify-content:space-between;gap:8px;align-items:center}.hub-badge{padding:5px 9px;border-radius:999px;background:rgba(218,167,82,.13);color:#8A5A18;font-size:11px;font-weight:900}.hub-score{font-size:11px;color:#8A7869}.hub-prop-title{font-size:19px;font-weight:900;color:#3B2A1E;margin-top:10px}.hub-prop-meta{font-size:12px;color:#7A6757;margin-top:4px}.hub-prop-price{font-size:25px;font-weight:950;color:#DAA752;margin-top:12px}.hub-prop-rate{font-size:11px;color:#9B8A79;margin-top:2px}
.hub-ai-answer{padding:18px 20px;border-radius:18px;background:linear-gradient(135deg,#fffaf0,#f2e2ca);border:1px solid rgba(218,167,82,.22);color:#3B2A1E;line-height:1.7;margin-top:14px;box-shadow:0 10px 25px rgba(94,69,43,.08)}
.hub-gallery-title{font-weight:900;color:#5E4634;margin:0 0 8px 2px}.hub-gallery-title:before{content:"✦ ";color:#DAA752}
@keyframes hubFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-9px)}}
@media (max-width:900px){.hub-hero{padding:32px 26px}.hub-hero h1{font-size:32px}.hub-hero-orb{right:20px;bottom:22px;font-size:64px}.hub-prop-image{height:135px}}
</style>
""", unsafe_allow_html=True)

# ==========================================
# FINAL THEME TOGGLE OVERRIDES
# These rules intentionally come last so the restored Dark/Light switch controls
# the finished visual theme without disturbing the gold brand accent.
if st.session_state.get("dark_mode", False):
    st.markdown("""
    <style>
      :root{
        --theme-bg:#26201A;
        --theme-bg2:#30271F;
        --theme-surface:#3A3027;
        --theme-surface2:#46392E;
        --theme-text:#F8F2E9;
        --theme-muted:#C7B9AA;
        --theme-line:rgba(255,244,226,.12);
      }
      .stApp, [data-testid="stAppViewContainer"]{
        background:linear-gradient(180deg,#241F1B 0%,#2E261F 52%,#241F1B 100%) !important;
        color:var(--theme-text) !important;
      }
      [data-testid="stAppViewContainer"]::before{opacity:.025 !important;}
      [data-testid="stSidebar"]{
        background:linear-gradient(180deg,#211C18 0%,#2A231D 100%) !important;
        border-right:1px solid rgba(218,167,82,.12) !important;
      }
      [data-testid="stSidebar"] .sidebar-logo.neo-brand{
        background:linear-gradient(145deg,#342A22,#2A231D) !important;
        border-color:rgba(218,167,82,.14) !important;
      }
      [data-testid="stSidebar"] div[role="radiogroup"] > label{
        background:rgba(255,255,255,.018) !important;
        color:var(--theme-text) !important;
      }
      [data-testid="stSidebar"] div[role="radiogroup"] > label:hover{
        background:rgba(218,167,82,.08) !important;
        border-color:rgba(218,167,82,.12) !important;
      }
      [data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"]{
        background:linear-gradient(90deg,rgba(218,167,82,.17),rgba(218,167,82,.05)) !important;
        border-color:rgba(218,167,82,.20) !important;
      }
      .intel-box,.metric-card,.val-box,.compare-card,.poi-card,.property-card,.property-summary-card,.feature-card,.stat-card{
        background:linear-gradient(180deg,#3A3027 0%,#332A23 100%) !important;
        border-color:var(--theme-line) !important;
        color:var(--theme-text) !important;
        box-shadow:0 18px 44px rgba(0,0,0,.24) !important;
      }
      .card-header,h1,h2,h3,.poi-title,.apt-title,.c-val{color:var(--theme-text) !important;}
      .intel-title,.metric-card .lbl,.c-label,.apt-muted,label{color:#D3C5B6 !important;}
      .stTextInput input,.stNumberInput input,.stTextArea textarea,
      .stSelectbox div[data-baseweb="select"] > div,
      .stMultiSelect div[data-baseweb="select"] > div,
      .stDateInput div[data-baseweb="input"] > div{
        background:#2E261F !important;
        color:var(--theme-text) !important;
        border-color:rgba(255,244,226,.13) !important;
      }
      .stTextInput input::placeholder,.stTextArea textarea::placeholder{color:#9E9082 !important;}
      [data-baseweb="popover"],[role="listbox"]{background:#332A23 !important;color:var(--theme-text) !important;border-color:rgba(255,244,226,.12) !important;}
      [data-baseweb="option"]{background:#332A23 !important;color:var(--theme-text) !important;}
      [data-baseweb="option"]:hover{background:#46392E !important;}
      .stTabs [data-baseweb="tab"]{background:#332A23 !important;color:#C8B9A8 !important;}
      .stTabs [aria-selected="true"]{background:#3A3027 !important;color:#E5B766 !important;border-color:rgba(218,167,82,.22) !important;}
      .premium-commandbar,.neo-topbar,.editorial-topbar{background:#312820 !important;border-color:rgba(255,244,226,.11) !important;}
      .hero-banner::before{background:linear-gradient(90deg,rgba(16,12,9,.72) 0%,rgba(16,12,9,.50) 44%,rgba(16,12,9,.10) 84%) !important;}
      .hero-sub{color:rgba(255,246,235,.76) !important;}
      .val-box{background:linear-gradient(180deg,#40352B 0%,#372C24 100%) !important;}
      .val-range{background:#4A3C30 !important;color:#D7C5B3 !important;}
      .ai-command-card{background:linear-gradient(145deg,rgba(218,167,82,.12),rgba(255,255,255,.025)) !important;border-color:rgba(218,167,82,.18) !important;}
      .ai-chat-bubble.bot{background:#332A23 !important;color:var(--theme-text) !important;border-color:rgba(255,244,226,.12) !important;}
      .ai-chat-bubble.user{background:linear-gradient(135deg,#8B5E3C,#B7835A) !important;}
      .ai-chat-avatar.user{background:#49392D !important;color:#F2E6D7 !important;}
      hr{border-color:rgba(255,244,226,.11) !important;}
    </style>
    """, unsafe_allow_html=True)
else:
    st.markdown("""
    <style>
      /* Light mode: keep the original first-design palette. */
      .stApp, [data-testid="stAppViewContainer"]{background:linear-gradient(180deg,#FBF8F4 0%,#F5EEE5 100%) !important;color:#5B3A29 !important;}
      [data-testid="stSidebar"]{background:linear-gradient(180deg,#FAF6F0 0%,#F3E8DC 100%) !important;}
      .metric-card .num,.intel-stat,.val-price,.poi-dist{color:#DAA752 !important;}
      .stButton > button,.stFormSubmitButton button{background:linear-gradient(135deg,#DAA752,#C99643) !important;color:#fff !important;}
    </style>
    """, unsafe_allow_html=True)

# ==========================================
# 📱 FINAL MOBILE-FIRST UI OVERRIDES
# Keeps the chosen light/gold apartment design, but reflows the entire site for phones.
# ==========================================
st.markdown("""
<style>
/* Keep mobile layouts inside the viewport. */
html, body, [data-testid="stAppViewContainer"], .main, section.main {
  max-width:100% !important;
  overflow-x:hidden !important;
}

@media (max-width: 768px) {
  /* ---------- App chrome ---------- */
  .block-container {
    width:100% !important;
    max-width:100% !important;
    padding:12px 12px 28px !important;
    margin:0 !important;
  }
  [data-testid="stAppViewContainer"] .main .block-container {
    padding-left:12px !important;
    padding-right:12px !important;
  }

  /* ---------- Sidebar ---------- */
  [data-testid="stSidebar"] {
    width:min(86vw, 330px) !important;
    min-width:min(86vw, 330px) !important;
    max-width:min(86vw, 330px) !important;
  }
  [data-testid="stSidebar"] > div:first-child {
    width:100% !important;
    min-width:0 !important;
  }
  [data-testid="stSidebarContent"] {
    padding:10px 10px 24px !important;
    overflow-y:auto !important;
    overscroll-behavior:contain !important;
    -webkit-overflow-scrolling:touch !important;
  }
  [data-testid="stSidebar"] .sidebar-logo,
  [data-testid="stSidebar"] .neo-brand {
    margin-bottom:10px !important;
  }
  [data-testid="stSidebar"] .account-card,
  [data-testid="stSidebar"] .ai-command-card {
    margin-left:0 !important;
    margin-right:0 !important;
  }
  [data-testid="stSidebar"] div[role="radiogroup"] > label {
    min-height:42px !important;
    padding:10px 11px !important;
    margin:4px 0 !important;
    border-radius:12px !important;
  }
  [data-testid="stSidebar"] .stButton > button,
  [data-testid="stSidebar"] .stFormSubmitButton button {
    width:100% !important;
    min-height:42px !important;
  }

  /* ---------- Typography ---------- */
  h1, .hero-title {
    font-size:clamp(27px, 8vw, 40px) !important;
    line-height:1.06 !important;
    letter-spacing:-0.8px !important;
    word-break:normal !important;
  }
  h2 { font-size:24px !important; line-height:1.15 !important; }
  h3 { font-size:20px !important; line-height:1.2 !important; }
  h4 { font-size:17px !important; }
  p, li, label, .stCaption { font-size:13px !important; }

  /* ---------- Hero ---------- */
  .hero-banner {
    min-height:210px !important;
    height:auto !important;
    padding:26px 20px !important;
    margin:0 0 16px !important;
    border-radius:20px !important;
    background-position:center !important;
  }
  .hero-banner::before {
    background:linear-gradient(90deg,rgba(255,255,255,.94) 0%,rgba(255,255,255,.76) 58%,rgba(255,255,255,.22) 100%) !important;
  }
  .hero-content { max-width:100% !important; }
  .hero-eyebrow {
    font-size:10px !important;
    letter-spacing:1.7px !important;
    margin-bottom:7px !important;
  }
  .hero-sub {
    max-width:100% !important;
    font-size:12px !important;
    line-height:1.5 !important;
  }

  /* ---------- Top bars / headers ---------- */
  .premium-commandbar,
  .neo-topbar,
  .editorial-topbar {
    flex-wrap:wrap !important;
    gap:8px !important;
    padding:10px 12px !important;
    border-radius:14px !important;
  }
  .premium-commandbar > *,
  .neo-topbar > *,
  .editorial-topbar > * {
    min-width:0 !important;
  }

  /* ---------- Streamlit columns ---------- */
  [data-testid="stHorizontalBlock"] {
    width:100% !important;
    max-width:100% !important;
    gap:10px !important;
    flex-wrap:wrap !important;
    overflow:visible !important;
  }

  /* On phones, large content cards stack. Compact input rows may remain 2-up. */
  [data-testid="stHorizontalBlock"] > [data-testid="column"] {
    min-width:0 !important;
  }
  .intel-box, .metric-card, .compare-card, .val-box,
  .poi-card, .property-card, .property-summary-card,
  .feature-card, .stat-card, .hub-prop-card {
    width:100% !important;
    box-sizing:border-box !important;
  }

  /* ---------- Forms / controls ---------- */
  .stButton > button,
  .stFormSubmitButton button {
    min-height:44px !important;
    width:100% !important;
    border-radius:12px !important;
    font-size:13px !important;
    padding:9px 12px !important;
  }
  .stTextInput input,
  .stNumberInput input,
  .stTextArea textarea,
  .stSelectbox div[data-baseweb="select"] > div,
  .stMultiSelect div[data-baseweb="select"] > div,
  .stDateInput div[data-baseweb="input"] > div {
    min-height:44px !important;
    font-size:13px !important;
    border-radius:11px !important;
    box-sizing:border-box !important;
  }
  textarea { min-height:92px !important; }
  .stSlider { padding-top:4px !important; padding-bottom:4px !important; }

  /* Keep paired small controls usable on narrow screens. */
  .stNumberInput, .stTextInput, .stSelectbox, .stMultiSelect {
    max-width:100% !important;
  }

  /* ---------- Tabs ---------- */
  .stTabs [data-baseweb="tab-list"] {
    gap:6px !important;
    overflow-x:auto !important;
    overflow-y:hidden !important;
    flex-wrap:nowrap !important;
    scrollbar-width:none !important;
    -webkit-overflow-scrolling:touch !important;
    padding-bottom:2px !important;
  }
  .stTabs [data-baseweb="tab-list"]::-webkit-scrollbar { display:none !important; }
  .stTabs [data-baseweb="tab"] {
    flex:0 0 auto !important;
    min-width:max-content !important;
    padding:8px 12px !important;
    font-size:12px !important;
    border-radius:10px !important;
  }

  /* ---------- Valuation / price ---------- */
  .val-box {
    padding:22px 18px !important;
    border-radius:18px !important;
  }
  .val-price,
  .val-box .val-price {
    font-size:clamp(34px, 10vw, 50px) !important;
    line-height:1 !important;
    word-break:break-word !important;
  }
  .val-range {
    font-size:11px !important;
    line-height:1.5 !important;
    padding:8px 10px !important;
    border-radius:9px !important;
  }

  /* ---------- Property cards / images ---------- */
  .property-image,
  .hub-prop-image {
    height:190px !important;
    max-height:none !important;
    border-radius:15px !important;
  }
  .property-card img,
  .property-summary-card img,
  .hub-prop-card img {
    max-width:100% !important;
    height:100% !important;
    object-fit:cover !important;
  }
  .property-card-price,
  .hub-prop-price { font-size:23px !important; }

  /* ---------- Metrics ---------- */
  .metric-card, .stat-card {
    min-height:96px !important;
    padding:14px !important;
    border-radius:15px !important;
  }
  .metric-card .num { font-size:26px !important; }

  /* ---------- AI chat ---------- */
  .ai-chat-container,
  .ai-command-card {
    width:100% !important;
    max-width:100% !important;
  }
  .ai-chat-bubble {
    max-width:88% !important;
    font-size:12px !important;
    line-height:1.55 !important;
    padding:9px 11px !important;
  }
  .ai-question-row,
  .ai-input-row {
    gap:6px !important;
  }

  /* ---------- Share card ---------- */
  .share-card-top,
  .share-preview,
  .share-actions {
    width:100% !important;
    max-width:100% !important;
  }
  .share-card-top { flex-direction:column !important; align-items:flex-start !important; }
  .share-actions { grid-template-columns:1fr 1fr !important; }

  /* ---------- Neighborhood cards: remain compact but readable ---------- */
  .poi-card {
    padding:12px !important;
    min-height:0 !important;
    border-radius:13px !important;
  }
  .poi-title { font-size:13px !important; }
  .poi-dist { font-size:14px !important; }

  /* ---------- Tables / dataframe ---------- */
  [data-testid="stDataFrame"],
  [data-testid="stDataEditor"] {
    width:100% !important;
    max-width:100% !important;
    overflow-x:auto !important;
  }

  /* ---------- Images & embedded HTML ---------- */
  img, svg, video, iframe, canvas {
    max-width:100% !important;
  }
  [data-testid="stImage"] img {
    width:100% !important;
    height:auto !important;
    object-fit:cover !important;
  }

  /* ---------- Expander / alerts ---------- */
  [data-testid="stExpander"] {
    border-radius:14px !important;
    overflow:hidden !important;
  }
  [data-testid="stAlert"] {
    border-radius:12px !important;
    padding:10px 12px !important;
    font-size:12px !important;
  }

  /* ---------- Footer / spacing ---------- */
  hr { margin:12px 0 !important; }
  .stMarkdown { margin-bottom:4px !important; }
}

@media (max-width: 480px) {
  .block-container { padding:10px 9px 24px !important; }
  .hero-banner { min-height:195px !important; padding:22px 16px !important; border-radius:17px !important; }
  .hero-title { font-size:30px !important; }
  .hero-sub { font-size:11px !important; }
  .val-price, .val-box .val-price { font-size:34px !important; }
  .property-image, .hub-prop-image { height:165px !important; }
  .share-actions { grid-template-columns:1fr !important; }
  .stButton > button, .stFormSubmitButton button { min-height:42px !important; font-size:12px !important; }
  .stTabs [data-baseweb="tab"] { padding:7px 10px !important; font-size:11px !important; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration:.001ms !important;
    animation-iteration-count:1 !important;
    transition-duration:.001ms !important;
    scroll-behavior:auto !important;
  }
}
</style>
""", unsafe_allow_html=True)

# ==========================================
# ✨ V24 — SIGNATURE GOLDEN FLAT EXPERIENCE
# Final visual polish layer. Logic intentionally untouched.
# ==========================================
_WOW_DARK = bool(st.session_state.get("dark_mode", False))
if _WOW_DARK:
    WOW_BG = "#1b1712"
    WOW_SURFACE = "#241e17"
    WOW_SURFACE2 = "#2e251b"
    WOW_TEXT = "#fff8ec"
    WOW_MUTED = "#c8b99f"
    WOW_BORDER = "rgba(218,167,82,.20)"
    WOW_SHADOW = "rgba(0,0,0,.34)"
    WOW_GOLD = "#daa752"
    WOW_GOLD2 = "#f0c777"
else:
    WOW_BG = "#f7f2e8"
    WOW_SURFACE = "#fffdf8"
    WOW_SURFACE2 = "#f1e6d4"
    WOW_TEXT = "#33261b"
    WOW_MUTED = "#756553"
    WOW_BORDER = "rgba(122,79,50,.13)"
    WOW_SHADOW = "rgba(91,61,31,.12)"
    WOW_GOLD = "#daa752"
    WOW_GOLD2 = "#bc8530"

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&family=Playfair+Display:wght@600;700;800&display=swap');

:root {{
  --wow-bg:{WOW_BG};
  --wow-surface:{WOW_SURFACE};
  --wow-surface2:{WOW_SURFACE2};
  --wow-text:{WOW_TEXT};
  --wow-muted:{WOW_MUTED};
  --wow-border:{WOW_BORDER};
  --wow-shadow:{WOW_SHADOW};
  --wow-gold:{WOW_GOLD};
  --wow-gold2:{WOW_GOLD2};
}}

html, body, .stApp, [data-testid="stAppViewContainer"] {{
  background:var(--wow-bg) !important;
  color:var(--wow-text) !important;
  font-family:'DM Sans',sans-serif !important;
}}

.stApp {{
  background:
    radial-gradient(circle at 10% 8%, rgba(218,167,82,.12), transparent 22%),
    radial-gradient(circle at 92% 16%, rgba(122,79,50,.07), transparent 20%),
    linear-gradient(180deg, var(--wow-bg) 0%, var(--wow-bg) 100%) !important;
  overflow-x:hidden !important;
}}

/* Soft architectural grid — stays subtle so the property cards remain the hero. */
[data-testid="stAppViewContainer"]::before {{
  content:"" !important;
  position:fixed !important;
  inset:0 !important;
  pointer-events:none !important;
  z-index:0 !important;
  opacity:{'0.16' if not _WOW_DARK else '0.10'} !important;
  background-image:
    linear-gradient(rgba(122,79,50,.12) 1px, transparent 1px),
    linear-gradient(90deg, rgba(122,79,50,.12) 1px, transparent 1px) !important;
  background-size:72px 72px !important;
  mask-image:linear-gradient(to bottom, rgba(0,0,0,.45), transparent 65%) !important;
}}

.block-container {{
  max-width:1500px !important;
  padding:12px 26px 72px !important;
}}

/* ===== Brand / top bars ===== */
.premium-commandbar,
.editorial-topbar,
.neo-topbar {{
  background:linear-gradient(135deg, color-mix(in srgb, var(--wow-surface) 95%, transparent), color-mix(in srgb, var(--wow-surface2) 72%, transparent)) !important;
  border:1px solid var(--wow-border) !important;
  border-radius:22px !important;
  box-shadow:0 20px 50px var(--wow-shadow), inset 0 1px 0 rgba(255,255,255,.45) !important;
  backdrop-filter:blur(18px) saturate(1.08) !important;
}}
.premium-commandbar-title,
.premium-commandbar-title span {{
  color:var(--wow-text) !important;
  background:none !important;
  -webkit-text-fill-color:initial !important;
}}
.premium-commandbar-title span {{ color:var(--wow-gold2) !important; }}
.premium-commandbar-title i,
.premium-dot {{
  background:var(--wow-gold) !important;
  box-shadow:0 0 0 5px rgba(218,167,82,.10), 0 0 16px rgba(218,167,82,.35) !important;
}}
.premium-chip,
.neo-mini-pill {{
  background:var(--wow-surface) !important;
  color:var(--wow-text) !important;
  border:1px solid var(--wow-border) !important;
}}

/* ===== Sidebar ===== */
[data-testid="stSidebar"] {{
  background:linear-gradient(180deg, var(--wow-surface) 0%, color-mix(in srgb, var(--wow-surface2) 48%, var(--wow-surface)) 100%) !important;
  border-right:1px solid var(--wow-border) !important;
  box-shadow:10px 0 36px var(--wow-shadow) !important;
}}
[data-testid="stSidebar"] .sidebar-logo.neo-brand {{
  background:transparent !important;
  border-bottom:1px solid var(--wow-border) !important;
  padding:10px 6px 16px !important;
}}
[data-testid="stSidebar"] .brand-mark {{
  background:linear-gradient(145deg,#7a4f32,var(--wow-gold)) !important;
  box-shadow:0 12px 25px rgba(122,79,50,.22) !important;
  border-radius:14px !important;
}}
[data-testid="stSidebar"] .brand-copy h2,
[data-testid="stSidebar"] .brand-copy h2 span {{
  color:var(--wow-text) !important;
  background:none !important;
  -webkit-text-fill-color:initial !important;
}}
[data-testid="stSidebar"] .brand-copy h2 span {{ color:var(--wow-gold2) !important; }}
[data-testid="stSidebar"] .brand-copy p {{ color:var(--wow-muted) !important; }}
[data-testid="stSidebar"] div[role="radiogroup"] > label {{
  color:var(--wow-text) !important;
  background:transparent !important;
  border:1px solid transparent !important;
  border-radius:13px !important;
  transition:transform .2s ease, background .2s ease, border-color .2s ease, box-shadow .2s ease !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {{
  background:rgba(218,167,82,.07) !important;
  transform:translateX(3px) !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] {{
  background:linear-gradient(90deg, rgba(218,167,82,.16), rgba(218,167,82,.04)) !important;
  border-color:rgba(218,167,82,.18) !important;
  box-shadow:inset 3px 0 0 var(--wow-gold), 0 10px 22px rgba(122,79,50,.06) !important;
}}
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"] p {{
  color:var(--wow-text) !important;
  font-weight:800 !important;
}}

/* ===== Main hero ===== */
.hero-banner {{
  min-height:360px !important;
  padding:54px !important;
  border-radius:30px !important;
  border:1px solid rgba(255,255,255,.16) !important;
  box-shadow:0 30px 75px rgba(72,48,26,.18) !important;
  animation:wowHeroIn .62s cubic-bezier(.2,.8,.2,1) both !important;
  overflow:hidden !important;
}}
.hero-banner::after {{
  content:"" !important;
  position:absolute !important;
  top:-30% !important;
  left:-18% !important;
  width:26% !important;
  height:180% !important;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.20),transparent) !important;
  transform:skewX(-16deg) !important;
  animation:wowShine 8.5s ease-in-out infinite !important;
  pointer-events:none !important;
}}
.hero-eyebrow {{
  background:rgba(255,255,255,.11) !important;
  border:1px solid rgba(255,255,255,.20) !important;
  color:#fff1d4 !important;
  border-radius:999px !important;
  padding:8px 13px !important;
  letter-spacing:1.7px !important;
}}
.hero-title {{
  font-family:'Playfair Display',serif !important;
  font-size:62px !important;
  line-height:1.03 !important;
  font-weight:700 !important;
  letter-spacing:-1.5px !important;
  text-shadow:0 12px 28px rgba(0,0,0,.20) !important;
}}
.gradient-text {{
  background:linear-gradient(90deg,#fff1d4,var(--wow-gold),#f7dfb0) !important;
  -webkit-background-clip:text !important;
  background-clip:text !important;
  -webkit-text-fill-color:transparent !important;
}}
.hero-sub {{ max-width:650px !important; line-height:1.7 !important; }}

/* ===== Premium content cards ===== */
.intel-box,
.val-box,
.metric-card,
.stat-card,
.property-card,
.property-summary-card,
.share-card-premium,
.ai-command-card,
.furniture-card,
.login-card,
.account-card {{
  background:linear-gradient(180deg, color-mix(in srgb, var(--wow-surface) 98%, transparent), color-mix(in srgb, var(--wow-surface2) 35%, var(--wow-surface))) !important;
  border:1px solid var(--wow-border) !important;
  box-shadow:0 18px 45px var(--wow-shadow), inset 0 1px 0 rgba(255,255,255,.42) !important;
  border-radius:20px !important;
  color:var(--wow-text) !important;
  transition:transform .28s ease, box-shadow .28s ease, border-color .28s ease !important;
}}
.intel-box:hover,
.metric-card:hover,
.property-card:hover,
.property-summary-card:hover,
.account-card:hover {{
  transform:translateY(-5px) !important;
  border-color:rgba(218,167,82,.28) !important;
  box-shadow:0 28px 60px var(--wow-shadow), inset 0 1px 0 rgba(255,255,255,.45) !important;
}}
.card-header,
.intel-title {{ color:var(--wow-text) !important; }}

/* ===== Price hierarchy ===== */
.val-price,
.property-card-price,
.hub-prop-price,
.price-text,
.big-price {{
  color:var(--wow-gold2) !important;
  text-shadow:0 8px 22px rgba(218,167,82,.14) !important;
  font-weight:900 !important;
}}
.val-box {{
  background:linear-gradient(135deg, rgba(218,167,82,.13), color-mix(in srgb, var(--wow-surface) 94%, transparent)) !important;
  border-color:rgba(218,167,82,.24) !important;
}}
.val-range {{
  background:var(--wow-surface2) !important;
  color:var(--wow-muted) !important;
  border:1px solid var(--wow-border) !important;
}}

/* ===== Buttons ===== */
.stButton > button,
.stFormSubmitButton button,
[data-testid="stDownloadButton"] button {{
  background:linear-gradient(135deg,#7a4f32,var(--wow-gold)) !important;
  color:#fffaf2 !important;
  border:0 !important;
  border-radius:13px !important;
  min-height:46px !important;
  font-weight:800 !important;
  letter-spacing:.1px !important;
  box-shadow:0 14px 28px rgba(122,79,50,.18) !important;
  position:relative !important;
  overflow:hidden !important;
  transition:transform .22s ease, box-shadow .22s ease, filter .22s ease !important;
}}
.stButton > button::after,
.stFormSubmitButton button::after,
[data-testid="stDownloadButton"] button::after {{
  content:"" !important;
  position:absolute !important;
  top:-50% !important;
  left:-70% !important;
  width:42% !important;
  height:200% !important;
  transform:skewX(-18deg) !important;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.28),transparent) !important;
  transition:left .45s ease !important;
  pointer-events:none !important;
}}
.stButton > button:hover,
.stFormSubmitButton button:hover,
[data-testid="stDownloadButton"] button:hover {{
  transform:translateY(-2px) !important;
  box-shadow:0 20px 38px rgba(122,79,50,.24) !important;
  filter:brightness(1.035) !important;
}}
.stButton > button:hover::after,
.stFormSubmitButton button:hover::after,
[data-testid="stDownloadButton"] button:hover::after {{ left:130% !important; }}
.btn-back > button,
[data-testid="stSidebar"] .stButton > button {{
  background:var(--wow-surface) !important;
  color:var(--wow-text) !important;
  border:1px solid var(--wow-border) !important;
  box-shadow:none !important;
}}

/* ===== Inputs / controls ===== */
label {{ color:var(--wow-text) !important; font-weight:700 !important; }}
.stTextInput input,
.stNumberInput input,
.stTextArea textarea,
.stDateInput div[data-baseweb="input"] > div,
.stSelectbox div[data-baseweb="select"] > div,
.stMultiSelect div[data-baseweb="select"] > div {{
  background:var(--wow-surface) !important;
  color:var(--wow-text) !important;
  border:1px solid var(--wow-border) !important;
  border-radius:13px !important;
  min-height:42px !important;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.45) !important;
}}
.stTextInput input:focus,
.stNumberInput input:focus,
.stTextArea textarea:focus,
.stSelectbox div[data-baseweb="select"] > div:focus-within,
.stMultiSelect div[data-baseweb="select"] > div:focus-within {{
  border-color:rgba(218,167,82,.65) !important;
  box-shadow:0 0 0 4px rgba(218,167,82,.10) !important;
}}
[data-baseweb="popover"] {{
  background:var(--wow-surface) !important;
  border:1px solid var(--wow-border) !important;
}}
[data-baseweb="option"] {{ background:var(--wow-surface) !important; color:var(--wow-text) !important; }}
[data-baseweb="option"] [aria-selected="true"],
[data-baseweb="option"]:hover {{ background:var(--wow-surface2) !important; }}

/* ===== Tabs ===== */
.stTabs [data-baseweb="tab-list"] {{ gap:8px !important; border-bottom:0 !important; }}
.stTabs [data-baseweb="tab"] {{
  background:var(--wow-surface) !important;
  color:var(--wow-muted) !important;
  border:1px solid var(--wow-border) !important;
  border-radius:999px !important;
  padding:8px 15px !important;
  transition:all .2s ease !important;
}}
.stTabs [data-baseweb="tab"]:hover {{
  color:var(--wow-text) !important;
  border-color:rgba(218,167,82,.28) !important;
}}
.stTabs [aria-selected="true"] {{
  color:#fff9ef !important;
  background:linear-gradient(135deg,#7a4f32,var(--wow-gold)) !important;
  border-color:transparent !important;
  box-shadow:0 10px 22px rgba(122,79,50,.17) !important;
}}

/* ===== AI ===== */
.ai-command-card {{
  background:linear-gradient(145deg, rgba(218,167,82,.13), color-mix(in srgb, var(--wow-surface) 96%, transparent)) !important;
}}
.ai-avatar,
.ai-chat-avatar.bot {{
  background:linear-gradient(145deg,#7a4f32,var(--wow-gold)) !important;
  color:#fff !important;
  box-shadow:0 10px 22px rgba(122,79,50,.20) !important;
}}
.ai-chat-bubble.bot {{
  background:var(--wow-surface2) !important;
  color:var(--wow-text) !important;
  border-color:var(--wow-border) !important;
}}
.ai-chat-bubble.user {{
  background:linear-gradient(135deg,#7a4f32,var(--wow-gold)) !important;
  color:#fffaf2 !important;
  box-shadow:0 10px 22px rgba(122,79,50,.18) !important;
}}
.ai-chat-panel::-webkit-scrollbar-thumb {{ background:linear-gradient(#7a4f32,var(--wow-gold)) !important; }}

/* ===== Share + report ===== */
.share-card-premium,
.share-card-compact {{
  background:var(--wow-surface) !important;
  border:1px solid var(--wow-border) !important;
}}
.share-badge,
.share-mini-btn,
.share-action {{ border-color:var(--wow-border) !important; color:var(--wow-text) !important; }}

/* ===== Images ===== */
.property-image,
.hub-prop-image,
.property-card img,
.property-summary-card img {{
  border-radius:18px !important;
}}
.property-card img,
.property-summary-card img,
.property-image {{
  transition:transform .5s cubic-bezier(.2,.8,.2,1), filter .5s ease !important;
}}
.property-card:hover img,
.property-summary-card:hover img,
.property-image:hover {{
  transform:scale(1.035) !important;
  filter:saturate(1.05) contrast(1.02) !important;
}}

/* ===== Motion ===== */
@keyframes wowHeroIn {{ from {{ opacity:0; transform:translateY(20px) scale(.985); }} to {{ opacity:1; transform:none; }} }}
@keyframes wowShine {{ 0%,62% {{ transform:translateX(-35%) skewX(-16deg); opacity:0; }} 72% {{ opacity:1; }} 82% {{ transform:translateX(420%) skewX(-16deg); opacity:0; }} 100% {{ opacity:0; }} }}
@keyframes wowFloat {{ 0%,100% {{ transform:translateY(0); }} 50% {{ transform:translateY(-4px); }} }}

.hero-banner .hero-eyebrow {{ animation:wowFloat 4s ease-in-out infinite; }}

/* ===== Mobile: keep it luxurious, not cramped ===== */
@media (max-width:900px) {{
  .block-container {{ padding:10px 14px 52px !important; }}
  .hero-banner {{ min-height:300px !important; padding:30px !important; border-radius:24px !important; }}
  .hero-title {{ font-size:44px !important; }}
  .intel-box,.property-card,.property-summary-card,.metric-card {{ border-radius:17px !important; }}
}}
@media (max-width:560px) {{
  .block-container {{ padding:8px 10px 38px !important; }}
  .hero-banner {{ min-height:230px !important; padding:22px 18px !important; border-radius:18px !important; }}
  .hero-title {{ font-size:31px !important; letter-spacing:-.8px !important; }}
  .hero-sub {{ font-size:12px !important; }}
  .hero-eyebrow {{ padding:7px 10px !important; font-size:9px !important; }}
  .val-price {{ font-size:34px !important; }}
  .stButton > button,.stFormSubmitButton button,[data-testid="stDownloadButton"] button {{ min-height:43px !important; border-radius:12px !important; }}
  .stTabs [data-baseweb="tab"] {{ padding:7px 11px !important; font-size:11px !important; }}
}}

@media (prefers-reduced-motion:reduce) {{
  *,*::before,*::after {{ animation-duration:.001ms !important; animation-iteration-count:1 !important; transition-duration:.001ms !important; scroll-behavior:auto !important; }}
}}


</style>
""", unsafe_allow_html=True)


# =====================================================
# AI VALUATION V2 — premium apartment experience
# =====================================================
st.markdown(r"""
<style>
.valuation-hero-v2{position:relative;overflow:hidden;min-height:300px;margin-bottom:28px;padding:42px 44px;border-radius:28px;display:flex;align-items:center;justify-content:space-between;background:linear-gradient(112deg,rgba(61,39,24,.96) 0%,rgba(92,59,31,.88) 46%,rgba(218,167,82,.78) 100%),url("https://images.unsplash.com/photo-1600607687920-4e2a09cf159d?q=80&w=2075&auto=format&fit=crop") center/cover;border:1px solid rgba(218,167,82,.28);box-shadow:0 24px 70px rgba(91,61,31,.18),inset 0 1px 0 rgba(255,255,255,.20)}
.valuation-hero-v2::before{content:"";position:absolute;inset:-30%;background:radial-gradient(circle,rgba(255,244,214,.16) 0 4%,transparent 5% 100%);background-size:34px 34px;transform:rotate(-8deg);opacity:.18;animation:valuationGridDrift 18s linear infinite;pointer-events:none}
.valuation-hero-v2::after{content:"";position:absolute;top:-30%;left:-16%;width:16%;height:180%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.22),transparent);transform:skewX(-17deg);animation:valuationShine 7.5s ease-in-out infinite;pointer-events:none}
.valuation-hero-copy{position:relative;z-index:2;max-width:690px}.valuation-kicker{display:inline-flex;align-items:center;gap:7px;padding:7px 12px;border-radius:999px;background:rgba(255,247,232,.12);border:1px solid rgba(255,248,236,.24);color:#f9e4bf;font-size:10px;font-weight:800;letter-spacing:2px;text-transform:uppercase;margin-bottom:13px}.valuation-hero-v2 h1{margin:0!important;color:#fffaf1!important;font-family:'Playfair Display',serif!important;font-size:48px!important;line-height:1.05!important;letter-spacing:-1px!important}.valuation-hero-v2 h1 span{color:#f3cf8b}.valuation-hero-v2 p{margin:14px 0 0;color:rgba(255,249,239,.85);font-size:15px;line-height:1.65;max-width:620px}.valuation-hero-badges{display:flex;gap:9px;flex-wrap:wrap;margin-top:18px}.valuation-hero-badges span{padding:8px 11px;border-radius:999px;color:#fff6e7;border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.08);font-size:11px;font-weight:700;backdrop-filter:blur(10px)}
.valuation-hero-orbit{position:relative;width:230px;height:230px;flex:0 0 230px;display:flex;align-items:center;justify-content:center;margin-left:20px;z-index:2}.orbit-ring{position:absolute;border:1px solid rgba(255,245,220,.23);border-radius:50%;animation:orbitSpin 18s linear infinite}.orbit-ring-1{width:188px;height:188px}.orbit-ring-2{width:132px;height:132px;border-color:rgba(244,211,151,.34);animation-direction:reverse;animation-duration:12s}.orbit-house{width:84px;height:84px;border-radius:24px;display:flex;align-items:center;justify-content:center;background:linear-gradient(145deg,#f8d48f,#b67c25);color:#5a3a1f;font-size:52px;font-weight:900;box-shadow:0 20px 50px rgba(0,0,0,.22),0 0 0 10px rgba(255,255,255,.05);animation:orbitFloat 4.5s ease-in-out infinite}.orbit-caption{position:absolute;bottom:-4px;right:-2px;display:flex;flex-direction:column;align-items:center;min-width:130px}.orbit-caption b{color:#fff6e6;font-size:14px;letter-spacing:2px}.orbit-caption span{color:rgba(255,245,226,.75);font-size:9px;letter-spacing:.7px;text-transform:uppercase}
.valuation-panel-v2{background:var(--wow-surface)!important;border:1px solid var(--wow-border)!important;border-radius:24px!important;padding:24px!important;box-shadow:0 18px 45px var(--wow-shadow),inset 0 1px 0 rgba(255,255,255,.52)!important;overflow:hidden}.v2-section-head{display:flex;align-items:center;gap:12px;margin-bottom:20px}.v2-step{width:34px;height:34px;border-radius:12px;display:flex;align-items:center;justify-content:center;background:linear-gradient(145deg,#7a4f32,#daa752);color:#fffaf1;font-size:11px;font-weight:900;box-shadow:0 8px 18px rgba(122,79,50,.16)}.v2-section-title{color:var(--wow-text);font-size:18px;font-weight:850}.v2-section-sub{color:var(--wow-muted);font-size:11px;margin-top:2px}.v2-mini-note{margin-top:18px;padding:11px 13px;background:rgba(218,167,82,.09);border:1px solid rgba(218,167,82,.17);border-radius:13px;color:var(--wow-muted);font-size:11px;line-height:1.45}
.preview-panel-v2{padding-bottom:20px!important}.flat-preview-v2{position:relative;height:230px;border-radius:19px;overflow:hidden;border:1px solid var(--wow-border);background:#e9ddcf;box-shadow:0 14px 28px rgba(91,61,31,.10)}.flat-preview-v2 img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .7s cubic-bezier(.2,.8,.2,1),filter .5s ease}.flat-preview-v2:hover img{transform:scale(1.045);filter:saturate(1.07) contrast(1.02)}.flat-preview-overlay-v2{position:absolute;left:12px;top:12px;right:12px;display:flex;gap:7px;flex-wrap:wrap}.preview-pill{padding:7px 10px;border-radius:999px;background:rgba(45,30,20,.58);color:#fff8ea;border:1px solid rgba(255,255,255,.16);font-size:10px;font-weight:700;backdrop-filter:blur(8px)}.flat-preview-title-v2{position:absolute;left:12px;right:12px;bottom:12px;display:flex;flex-direction:column;gap:4px;padding:12px 13px;border-radius:14px;background:linear-gradient(180deg,rgba(33,22,15,.08),rgba(33,22,15,.76));color:#fffaf1;backdrop-filter:blur(5px)}.flat-preview-title-v2 b{font-size:18px;line-height:1.1}.flat-preview-title-v2 span{font-size:10px;opacity:.86}
.v2-amenity-title{margin:18px 0 10px;color:var(--wow-text);font-size:12px;font-weight:800;letter-spacing:.6px;text-transform:uppercase}.amenity-counter-v2{min-height:36px;display:flex;align-items:center;justify-content:center;gap:7px;border:1px dashed rgba(218,167,82,.34);background:rgba(218,167,82,.06);border-radius:11px;color:var(--wow-muted);font-size:10px}.amenity-counter-v2 b{color:var(--wow-gold2);font-size:12px}.generate-wrap-v2{margin-top:18px;padding-top:14px;border-top:1px solid var(--wow-border)}.generate-wrap-v2 .stButton>button{min-height:56px!important;font-size:16px!important;border-radius:15px!important}.generate-hint-v2{text-align:center;color:var(--wow-muted);font-size:9px;margin-top:8px;letter-spacing:.25px}
.results-topline-v2{display:grid;grid-template-columns:1.5fr repeat(3,1fr);gap:12px;margin:0 0 20px}.result-fact-v2{padding:16px;background:var(--wow-surface);border:1px solid var(--wow-border);border-radius:17px;box-shadow:0 12px 30px var(--wow-shadow)}.result-fact-v2 .rf-label{font-size:9px;letter-spacing:1.2px;text-transform:uppercase;color:var(--wow-muted);font-weight:800}.result-fact-v2 .rf-value{margin-top:6px;font-size:20px;font-weight:900;color:var(--wow-text)}.result-fact-v2.primary{background:linear-gradient(145deg,rgba(218,167,82,.15),var(--wow-surface));border-color:rgba(218,167,82,.25)}.result-fact-v2.primary .rf-value{color:var(--wow-gold2)}
@keyframes valuationGridDrift{from{transform:translate3d(0,0,0) rotate(-8deg)}to{transform:translate3d(36px,18px,0) rotate(-8deg)}}@keyframes valuationShine{0%,55%{left:-16%;opacity:0}67%{opacity:1}79%{left:125%;opacity:0}100%{left:125%;opacity:0}}@keyframes orbitSpin{from{transform:rotate(0)}to{transform:rotate(360deg)}}@keyframes orbitFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
@media (max-width:900px){.valuation-hero-v2{min-height:0;padding:30px 26px}.valuation-hero-orbit{width:175px;height:175px;flex-basis:175px}.orbit-ring-1{width:145px;height:145px}.orbit-ring-2{width:105px;height:105px}.orbit-house{width:68px;height:68px;font-size:42px}.results-topline-v2{grid-template-columns:1fr 1fr}}
@media (max-width:620px){.valuation-hero-v2{padding:24px 19px;display:block}.valuation-hero-v2 h1{font-size:32px!important}.valuation-hero-v2 p{font-size:12px}.valuation-hero-orbit{width:145px;height:145px;margin:18px auto 0}.orbit-ring-1{width:120px;height:120px}.orbit-ring-2{width:88px;height:88px}.orbit-house{width:58px;height:58px;font-size:35px;border-radius:18px}.orbit-caption{bottom:-2px}.valuation-panel-v2{padding:17px!important;border-radius:19px!important}.flat-preview-v2{height:195px}.results-topline-v2{grid-template-columns:1fr 1fr;gap:8px}.result-fact-v2{padding:12px}.result-fact-v2 .rf-value{font-size:16px}.valuation-hero-badges span{font-size:9px;padding:7px 9px}}
</style>
""", unsafe_allow_html=True)

# =====================================================
# ALL PAGES V2 — keep every screen in the same premium
# apartment/flat design language as AI Valuation + Budget Search.
# This is intentionally appended last so it wins over older page CSS.
# =====================================================
st.markdown(r'''
<style>
/* ---------- Shared V2 hero for EVERY page ---------- */
.hero-banner{
  position:relative !important;
  overflow:hidden !important;
  min-height:300px !important;
  margin-bottom:28px !important;
  padding:42px 44px !important;
  border-radius:28px !important;
  display:flex !important;
  align-items:center !important;
  justify-content:space-between !important;
  background:
    linear-gradient(112deg,rgba(61,39,24,.96) 0%,rgba(92,59,31,.88) 46%,rgba(218,167,82,.78) 100%),
    url("https://images.unsplash.com/photo-1600607687920-4e2a09cf159d?q=80&w=2075&auto=format&fit=crop") center/cover !important;
  border:1px solid rgba(218,167,82,.28) !important;
  box-shadow:0 24px 70px rgba(91,61,31,.18),inset 0 1px 0 rgba(255,255,255,.20) !important;
}
.hero-banner::before{
  content:"" !important;
  position:absolute !important;
  inset:-30% !important;
  background:radial-gradient(circle,rgba(255,244,214,.16) 0 4%,transparent 5% 100%) !important;
  background-size:34px 34px !important;
  transform:rotate(-8deg) !important;
  opacity:.18 !important;
  animation:allPagesGrid 18s linear infinite !important;
  pointer-events:none !important;
}
.hero-banner::after{
  content:"" !important;
  position:absolute !important;
  top:-30% !important;
  left:-16% !important;
  width:16% !important;
  height:180% !important;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.22),transparent) !important;
  transform:skewX(-17deg) !important;
  animation:allPagesShine 7.5s ease-in-out infinite !important;
  pointer-events:none !important;
}
.hero-banner .hero-content{
  position:relative !important;
  z-index:2 !important;
  max-width:690px !important;
  animation:allPagesHeroIn .65s cubic-bezier(.2,.8,.2,1) both !important;
}
.hero-banner .hero-eyebrow{
  display:inline-flex !important;
  align-items:center !important;
  gap:7px !important;
  padding:7px 12px !important;
  border-radius:999px !important;
  background:rgba(255,247,232,.12) !important;
  border:1px solid rgba(255,248,236,.24) !important;
  color:#f9e4bf !important;
  font-size:10px !important;
  font-weight:800 !important;
  letter-spacing:2px !important;
  text-transform:uppercase !important;
  margin-bottom:13px !important;
}
.hero-banner .hero-title,
.hero-banner h1{
  margin:0 !important;
  color:#fffaf1 !important;
  font-family:'Playfair Display',Georgia,serif !important;
  font-size:48px !important;
  line-height:1.05 !important;
  letter-spacing:-1px !important;
  font-weight:800 !important;
}
.hero-banner .gradient-text{color:#f3cf8b !important;background:none !important;-webkit-text-fill-color:#f3cf8b !important;}
.hero-banner .hero-sub{
  margin:14px 0 0 !important;
  color:rgba(255,249,239,.85) !important;
  font-size:15px !important;
  line-height:1.65 !important;
  max-width:620px !important;
}
.hero-banner .hero-content::after{
  content:"⌂" !important;
  position:absolute !important;
  right:-265px !important;
  top:50% !important;
  transform:translateY(-50%) !important;
  width:170px !important;
  height:170px !important;
  border:1px solid rgba(255,245,220,.20) !important;
  border-radius:50% !important;
  display:flex !important;
  align-items:center !important;
  justify-content:center !important;
  color:#f3cf8b !important;
  font-family:Georgia,serif !important;
  font-size:74px !important;
  text-shadow:0 12px 35px rgba(0,0,0,.25) !important;
  box-shadow:0 0 0 28px rgba(255,255,255,.025),0 0 0 57px rgba(255,255,255,.015) !important;
  animation:allPagesOrbit 5s ease-in-out infinite !important;
}

/* ---------- Common content surfaces ---------- */
.intel-box,
.compare-card,
.metric-card,
.val-box,
.account-card,
.property-summary-card,
.share-card-premium,
.share-card-compact,
.furniture-card{
  border-radius:22px !important;
  border-color:var(--wow-border) !important;
  box-shadow:0 18px 48px var(--wow-shadow), inset 0 1px 0 rgba(255,255,255,.50) !important;
}
.intel-box:hover,
.compare-card:hover,
.metric-card:hover,
.account-card:hover,
.property-summary-card:hover{
  transform:translateY(-4px) !important;
  border-color:rgba(218,167,82,.32) !important;
  box-shadow:0 26px 58px rgba(122,79,50,.12), inset 0 1px 0 rgba(255,255,255,.56) !important;
}
.card-header,
.intel-title{
  letter-spacing:.1px !important;
}

/* ---------- Consistent stat / metric presentation ---------- */
[data-testid="stMetric"]{
  background:var(--wow-surface) !important;
  border:1px solid var(--wow-border) !important;
  border-radius:18px !important;
  padding:15px 17px !important;
  box-shadow:0 14px 34px var(--wow-shadow) !important;
  transition:transform .25s ease,box-shadow .25s ease,border-color .25s ease !important;
}
[data-testid="stMetric"]:hover{
  transform:translateY(-3px) !important;
  border-color:rgba(218,167,82,.32) !important;
  box-shadow:0 20px 42px rgba(122,79,50,.12) !important;
}
[data-testid="stMetricLabel"]{color:var(--wow-muted) !important;font-weight:700 !important;}
[data-testid="stMetricValue"]{color:var(--wow-gold2) !important;font-weight:900 !important;}

/* ---------- Compare page: same gold visual system ---------- */
.c-win{color:var(--wow-gold2) !important;font-weight:900 !important;}
.dual-verdict-box{
  background:linear-gradient(145deg,rgba(218,167,82,.12),var(--wow-surface)) !important;
  border:1px solid rgba(218,167,82,.25) !important;
  border-radius:22px !important;
  box-shadow:0 18px 45px var(--wow-shadow) !important;
}

/* ---------- Analytics charts / tables ---------- */
[data-testid="stDataFrame"]{
  border:1px solid var(--wow-border) !important;
  border-radius:18px !important;
  overflow:hidden !important;
  box-shadow:0 14px 32px var(--wow-shadow) !important;
}

/* ---------- Sidebar follows the same visual language ---------- */
[data-testid="stSidebar"]{
  border-right:1px solid var(--wow-border) !important;
  box-shadow:8px 0 35px rgba(122,79,50,.05) !important;
}
[data-testid="stSidebar"] .stButton > button{
  border-radius:13px !important;
}

/* ---------- Consistent small action pills ---------- */
.share-action-compact,
.share-mini-btn{
  border-radius:12px !important;
  min-height:44px !important;
}

/* ---------- Mobile: stack exactly like the V2 valuation page ---------- */
@media (max-width:900px){
  .hero-banner{
    min-height:0 !important;
    padding:30px 26px !important;
    border-radius:24px !important;
  }
  .hero-banner .hero-title,
  .hero-banner h1{font-size:40px !important;}
  .hero-banner .hero-content::after{right:-165px;width:130px;height:130px;font-size:54px !important;}
}
@media (max-width:620px){
  .hero-banner{
    padding:24px 19px !important;
    display:block !important;
    min-height:230px !important;
    border-radius:19px !important;
  }
  .hero-banner .hero-title,
  .hero-banner h1{font-size:31px !important;letter-spacing:-.7px !important;}
  .hero-banner .hero-sub{font-size:12px !important;line-height:1.55 !important;}
  .hero-banner .hero-eyebrow{font-size:9px !important;padding:7px 10px !important;}
  .hero-banner .hero-content::after{right:12px;top:auto;bottom:10px;transform:none;width:74px;height:74px;font-size:34px !important;box-shadow:none !important;opacity:.72 !important;}
  .intel-box,.compare-card,.metric-card,.account-card{border-radius:17px !important;}
}

@keyframes allPagesGrid{from{transform:translate3d(0,0,0) rotate(-8deg)}to{transform:translate3d(36px,18px,0) rotate(-8deg)}}
@keyframes allPagesShine{0%,55%{left:-16%;opacity:0}67%{opacity:1}79%{left:125%;opacity:0}100%{left:125%;opacity:0}}
@keyframes allPagesHeroIn{from{opacity:0;transform:translateY(18px) scale(.985)}to{opacity:1;transform:none}}
@keyframes allPagesOrbit{0%,100%{transform:translateY(-50%) rotate(-3deg)}50%{transform:translateY(calc(-50% - 7px)) rotate(3deg)}}
@media (prefers-reduced-motion:reduce){
  .hero-banner::before,.hero-banner::after,.hero-banner .hero-content,.hero-banner .hero-content::after{animation:none !important;}
}
</style>
''', unsafe_allow_html=True)



# ==========================================

# 📱 AUTH MOBILE OVERRIDES
st.markdown("""
<style>
@media (max-width: 600px) {
  .auth-wrap { min-height:auto !important; padding:14px 0 18px !important; }
  .auth-card { width:100% !important; max-width:100% !important; padding:20px 15px !important; border-radius:20px !important; box-sizing:border-box !important; }
  .auth-title { font-size:24px !important; }
  .auth-sub { font-size:12px !important; }
  .auth-brand-name { font-size:20px !important; }
  .auth-logo { width:40px !important; height:40px !important; font-size:20px !important; }
  .auth-wrap [data-testid="stHorizontalBlock"] {
      display:flex !important;
      flex-direction:column !important;
      gap:8px !important;
  }
  .auth-wrap [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
      width:100% !important;
      min-width:100% !important;
      flex:1 1 100% !important;
  }
}
</style>
""", unsafe_allow_html=True)

# 📱 SMART ESTATE — MOBILE RESPONSIVE LAYER
# ==========================================
st.markdown("""
<style>
/* ---------- Mobile foundation ---------- */
html, body, [data-testid="stAppViewContainer"] {
    max-width: 100% !important;
    overflow-x: hidden !important;
}

[data-testid="stAppViewContainer"] {
    overflow-x: hidden !important;
}

.block-container {
    box-sizing: border-box !important;
}

/* ---------- Tablet ---------- */
@media (max-width: 900px) {
    .block-container {
        width: 100% !important;
        max-width: 100% !important;
        padding-left: 12px !important;
        padding-right: 12px !important;
        padding-top: 0.75rem !important;
    }

    /* Make Streamlit column groups wrap cleanly */
    [data-testid="stHorizontalBlock"] {
        flex-wrap: wrap !important;
        gap: 10px !important;
        width: 100% !important;
    }

    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
        min-width: 0 !important;
    }

    /* Touch-friendly controls */
    .stButton > button,
    .stDownloadButton > button,
    .stFormSubmitButton > button {
        min-height: 44px !important;
        border-radius: 13px !important;
    }

    .stTextInput input,
    .stNumberInput input,
    .stSelectbox [role="combobox"],
    .stTextArea textarea {
        min-height: 44px !important;
    }

    /* Tabs can scroll instead of squeezing */
    .stTabs [data-baseweb="tab-list"] {
        overflow-x: auto !important;
        scrollbar-width: none !important;
        flex-wrap: nowrap !important;
        white-space: nowrap !important;
    }

    .stTabs [data-baseweb="tab-list"]::-webkit-scrollbar {
        display: none !important;
    }

    .stTabs [data-baseweb="tab"] {
        flex: 0 0 auto !important;
        min-height: 42px !important;
        padding: 0 13px !important;
    }

    /* Tables should scroll rather than break the page */
    [data-testid="stDataFrame"],
    [data-testid="stTable"] {
        max-width: 100% !important;
        overflow-x: auto !important;
    }

    /* Images never exceed the phone width */
    img, video, iframe {
        max-width: 100% !important;
    }
}

/* ---------- Phone ---------- */
@media (max-width: 600px) {
    .block-container {
        padding-left: 8px !important;
        padding-right: 8px !important;
        padding-bottom: 1.5rem !important;
    }

    /* Every multi-column section becomes a clean vertical stack.
       This prevents clipped forms and tiny buttons on phones. */
    [data-testid="stHorizontalBlock"] {
        display: flex !important;
        flex-direction: column !important;
        align-items: stretch !important;
        gap: 8px !important;
    }

    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
        width: 100% !important;
        min-width: 100% !important;
        max-width: 100% !important;
        flex: 1 1 100% !important;
    }

    /* Compact headings */
    h1 {
        font-size: 1.65rem !important;
        line-height: 1.12 !important;
    }

    h2 {
        font-size: 1.35rem !important;
        line-height: 1.18 !important;
    }

    h3 {
        font-size: 1.08rem !important;
        line-height: 1.25 !important;
    }

    p, li {
        line-height: 1.5 !important;
    }

    /* Full-width touch controls */
    .stButton,
    .stDownloadButton,
    .stFormSubmitButton {
        width: 100% !important;
    }

    .stButton > button,
    .stDownloadButton > button,
    .stFormSubmitButton > button {
        width: 100% !important;
        min-height: 46px !important;
        font-size: 13px !important;
        padding: 9px 12px !important;
    }

    /* Inputs */
    .stTextInput,
    .stNumberInput,
    .stSelectbox,
    .stMultiSelect,
    .stSlider,
    .stTextArea {
        width: 100% !important;
    }

    .stTextInput input,
    .stNumberInput input,
    .stTextArea textarea {
        font-size: 14px !important;
        min-height: 46px !important;
        border-radius: 12px !important;
    }

    .stSelectbox [role="combobox"] {
        min-height: 46px !important;
        border-radius: 12px !important;
    }

    /* Streamlit metric cards */
    [data-testid="stMetric"] {
        width: 100% !important;
        box-sizing: border-box !important;
        padding: 12px !important;
        border-radius: 16px !important;
    }

    [data-testid="stMetricValue"] {
        font-size: 1.25rem !important;
    }

    /* Custom cards */
    .account-card,
    .compare-card,
    .metric-card,
    .intel-box,
    .dual-verdict-box,
    .property-card,
    .nh-card {
        max-width: 100% !important;
        box-sizing: border-box !important;
        border-radius: 16px !important;
    }

    /* Hero */
    .hero-banner {
        width: 100% !important;
        box-sizing: border-box !important;
        min-height: auto !important;
        padding: 22px 17px !important;
        border-radius: 18px !important;
    }

    .hero-banner .hero-title,
    .hero-banner h1 {
        font-size: 29px !important;
        line-height: 1.08 !important;
        max-width: 100% !important;
    }

    .hero-banner .hero-sub {
        font-size: 11px !important;
        line-height: 1.55 !important;
        max-width: 100% !important;
    }

    /* Hide decorative overflow on narrow screens */
    .hero-banner .hero-content::after {
        opacity: .35 !important;
        right: 4px !important;
        bottom: 4px !important;
        width: 58px !important;
        height: 58px !important;
        font-size: 26px !important;
    }

    /* Sidebar becomes a phone-friendly overlay */
    [data-testid="stSidebar"] {
        width: min(86vw, 320px) !important;
        min-width: min(86vw, 320px) !important;
        max-width: min(86vw, 320px) !important;
    }

    [data-testid="stSidebar"] > div:first-child {
        width: min(86vw, 320px) !important;
        min-width: min(86vw, 320px) !important;
        max-width: min(86vw, 320px) !important;
    }

    [data-testid="stSidebar"] .stButton > button {
        min-height: 44px !important;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab"] {
        font-size: 11px !important;
        padding: 0 11px !important;
    }

    /* Expanders */
    [data-testid="stExpander"] {
        border-radius: 15px !important;
        overflow: hidden !important;
    }

    /* Code / long text */
    pre, code {
        max-width: 100% !important;
        overflow-x: auto !important;
    }

    /* Prevent long property names / signatures from breaking layout */
    .stMarkdown, .stCaption, .stAlert, .stInfo, .stSuccess, .stWarning, .stError {
        overflow-wrap: anywhere !important;
        word-break: break-word !important;
    }

    /* Neighborhood cards: one per row on phone */
    .nh-grid,
    .neighborhood-grid {
        grid-template-columns: 1fr !important;
    }

    /* Architectural presentation board */
    .board {
        max-width: 100% !important;
        overflow-x: hidden !important;
    }
}

/* ---------- Small phones ---------- */
@media (max-width: 420px) {
    .block-container {
        padding-left: 6px !important;
        padding-right: 6px !important;
    }

    h1 {
        font-size: 1.45rem !important;
    }

    h2 {
        font-size: 1.2rem !important;
    }

    h3 {
        font-size: 1rem !important;
    }

    .hero-banner {
        padding: 19px 14px !important;
    }

    .hero-banner .hero-title,
    .hero-banner h1 {
        font-size: 25px !important;
    }

    .hero-banner .hero-sub {
        font-size: 10.5px !important;
    }

    [data-testid="stMetricValue"] {
        font-size: 1.1rem !important;
    }

    .stButton > button,
    .stDownloadButton > button,
    .stFormSubmitButton > button {
        font-size: 12px !important;
        min-height: 44px !important;
    }

    .stTextInput input,
    .stNumberInput input,
    .stSelectbox [role="combobox"] {
        font-size: 13px !important;
    }
}

/* ---------- Reduced motion ---------- */
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: .001ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: .001ms !important;
        scroll-behavior: auto !important;
    }
}
</style>
""", unsafe_allow_html=True)
