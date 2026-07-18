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
