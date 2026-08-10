import os

# Compact top-level categories. "Other" is the fallback.
# Bills absorbs utilities/housing/subscriptions/insurance/fees;
# Health absorbs personal care/fitness; Entertainment absorbs fun (not travel);
# Travel is overseas + travelling spend (bookings via keywords, the rest via /trip).
CATEGORIES = [
    "Food", "Groceries", "Transport", "Shopping",
    "Bills", "Health", "Entertainment", "Travel", "Other",
]
DEFAULT_CATEGORY = "Other"

# Budget alert thresholds (% of a category's monthly budget).
ALERT_THRESHOLDS = (80, 100)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "0") or "0")
DB_PATH = os.environ.get("DB_PATH", "/app/data/expenses.db")

# Transaction-email ingestion — Trust Bank / Grab / PayLah
# (empty GMAIL_APP_PASSWORD disables the poll entirely).
GMAIL_USER = os.environ.get("GMAIL_USER", "")
# Google shows the 16-char app password in 4 space-separated groups for
# readability; the spaces aren't part of it. Strip them so a paste-with-spaces
# still authenticates (Compose passes env values literally, spaces included).
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
EMAIL_POLL_MINUTES = int(os.environ.get("EMAIL_POLL_MINUTES", "15") or "15")
EMAIL_CURSOR_PATH = os.environ.get("EMAIL_CURSOR_PATH", "/app/data/trust_last_uid")

# --- Tiger Brokers Open API (portfolio page) ---
# Empty TIGER_ID disables the whole feature: the scheduler job is not
# registered and the web routes 503, which hides the tab. Same gating style as
# GMAIL_APP_PASSWORD for email ingestion.
TIGER_ID = os.environ.get("TIGER_ID", "").strip()
TIGER_ACCOUNT = os.environ.get("TIGER_ACCOUNT", "").strip()
TIGER_PRIVATE_KEY_PATH = os.environ.get(
    "TIGER_PRIVATE_KEY_PATH", "/app/data/tiger_private_key.pem")
PORTFOLIO_POLL_MINUTES = int(os.environ.get("PORTFOLIO_POLL_MINUTES", "30") or "30")

# --- Payslip archive ---
# Password your payroll provider puts on the payslip PDF. Empty disables the
# feature entirely: the email branch never runs, no PDF is downloaded or
# stored, and the web tab hides itself. Same gating style as TIGER_ID and
# GMAIL_APP_PASSWORD.
PAYSLIP_PDF_PASSWORD = os.environ.get("PAYSLIP_PDF_PASSWORD", "").strip()
# Address the payslip mail arrives from. No default -- set it to your own
# payroll sender, or leave it empty and the feature stays off. Matching is on
# sender + a PDF attachment, never on subject: payroll subjects tend to carry
# a per-request number ("Payslip Request 12345678"), not the pay month.
PAYSLIP_SENDER = os.environ.get("PAYSLIP_SENDER", "").strip().lower()
PAYSLIP_DIR = os.environ.get("PAYSLIP_DIR", "/app/data/payslips")

# --- Web dashboard authentication ---
# Set to reopen signup after the first account exists. When set, the code is
# required for EVERY signup, including the first.
SIGNUP_CODE = os.environ.get("SIGNUP_CODE", "").strip()
# Comma-separated peer IPs whose X-Forwarded-For header is trusted. Empty (the
# default) means the socket peer address is used. Requests reach this app via
# Nginx Proxy Manager, so without this every visitor shares one throttle
# window; trusting XFF unconditionally would instead let anyone hitting port
# 8085 directly spoof their way around it.
TRUSTED_PROXIES = tuple(
    p.strip() for p in os.environ.get("TRUSTED_PROXIES", "").split(",") if p.strip()
)
