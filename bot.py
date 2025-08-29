# bot.py — BLASTER (local polling, date-columns format)
# -------------------------------------------------------------------

from __future__ import annotations
import os
import traceback
from datetime import datetime
from typing import Tuple, Optional
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from dotenv import load_dotenv
load_dotenv()  # reads .env when running locally



# =========================
# CONFIG (edit these)
# =========================
# Prefer env vars in deployment; fall back allowed for local testing.
import json

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
GSHEET_ID: str = os.getenv("GSHEET_ID", "")
WORKSHEET_TITLE: str = os.getenv("WORKSHEET_TITLE", "Sheet1")

# Auth: prefer SERVICE_ACCOUNT_JSON; fall back to SERVICE_ACCOUNT_FILE
SERVICE_ACCOUNT_JSON: str | None = os.getenv("SERVICE_ACCOUNT_JSON")
SERVICE_ACCOUNT_FILE: str | None = os.getenv("SERVICE_ACCOUNT_FILE")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# =========================
# editable messages
# =========================

USEFUL_LINKS = {
    "training calendar": "https://docs.google.com/spreadsheets/d/1hdZcShRccqVyegUG07WjWBy-rWC51HVDlttACU7vNh4/edit?usp=sharing",
    # "club handbook": "https://your-domain.tld/handbook.pdf",
}

ATTENDANCE_INSTRUCTIONS = (
    "🙆🏼‍♂️🙆🏼‍♀️ this is how you log your attendance:\n"
    "/attendance [DD/MM/YYYY] [absent/late/leave early] [Name] [Reason] [NA/HH:MM (24h format)]\n\n"
    "examples:\n"
    "• /attendance 03/09/2025 absent marvell sick NA\n"
    "• /attendance 03/09/2025 late marvell night class 19:15\n"
    "• /attendance 03/09/2025 leave early marvell family matter 20:30"
)

HELP_MESSAGE = (
    "❓ are you confused?\n\n"
    "if you’re unsure how to use me 👀 or something isn’t working, "
    "please pm my boss @tamlongkang for help."
)

def start_message(first_name: str) -> str:
    return (
        f"👋 hello {first_name}, i’m ✨blaster✨, your favourite blastard :)\n\n"
        "use the menu below or type commands directly:\n"
        "• /attendance — log attendance\n"
        "• /usefullinks — view important links\n"
        "• /help — report anything"
    )

# Slash command menu shown in Telegram’s “/” list
COMMAND_MENU = [
    BotCommand("start", "say hi to the bot 🖖"),
    BotCommand("attendance", "report when you are absent/late/leave early for training (valid reasons) ✉️"),
    BotCommand("usefullinks", "view important links 💃🏼🕺🏼"),
    BotCommand("help", "contact the bossman 🧘🏼"),
]


# =========================
# sheets helpers
# =========================
def get_ws():
    # Build credentials from ENV
    if SERVICE_ACCOUNT_JSON:
        info = json.loads(SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    elif SERVICE_ACCOUNT_FILE:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    else:
        raise RuntimeError("Set SERVICE_ACCOUNT_JSON or SERVICE_ACCOUNT_FILE in env")

    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet(WORKSHEET_TITLE)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET_TITLE, rows=2000, cols=100)
    return ws


def ensure_header_row(ws):
    """Ensure row 1 exists (date headers live here)."""
    if not ws.row_values(1):
        ws.update("A1:A1", [[""]])

def get_or_create_date_column(ws, date_str: str) -> int:
    """Return 1-based column index for date header; create header if missing."""
    headers = ws.row_values(1)
    try:
        return headers.index(date_str) + 1
    except ValueError:
        # Append as next header
        target_col = max(len(headers), 1) + 1 if headers else 1
        if target_col > ws.col_count:
            ws.add_cols(target_col - ws.col_count)
        ws.update_cell(1, target_col, date_str)
        return target_col

def append_record_under_column(ws, col_idx: int, record_line: str):
    """Append a multi-line record into the first empty row of a date column."""
    first_empty_row = len(ws.col_values(col_idx)) + 1
    if first_empty_row == 1:
        first_empty_row = 2
    ws.update_cell(first_empty_row, col_idx, record_line)


# =========================
# BUSINESS LOGIC
# =========================
def parse_attendance_args(text: str) -> Tuple[str, str, str, str, str]:
    """
    /attendance [DD/MM/YYYY] [absent/late/leave early] [Name] [Reason] [NA/HH:MM]
    Name must be one token (use underscores for spaces). Reason may be multi-word.
    """
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        raise ValueError("Missing parameters.")
    tokens = parts[1].strip().split()
    if len(tokens) < 5:
        raise ValueError("Please provide all 5 fields.")

    date_str = tokens[0]
    status_raw = tokens[1].lower().strip()
    time_detail = tokens[-1]
    name = tokens[2]
    reason = " ".join(tokens[3:-1]).strip()

    # Validate values
    datetime.strptime(date_str, "%d/%m/%Y")  # raises if bad
    if status_raw in {"leave", "leaveearly", "leave_early"}:
        status = "leave early"
    elif status_raw in {"absent", "late", "leave early"}:
        status = status_raw
    else:
        raise ValueError("Status must be one of: absent, late, leave early.")
    if time_detail.upper() != "NA":
        datetime.strptime(time_detail, "%H:%M")
    if not name:
        raise ValueError("Name cannot be empty.")
    if not reason:
        raise ValueError("Reason cannot be empty. Use 'Reason NA' if none.")

    return date_str, status, name, reason, time_detail

def format_record_line(
    name: str,
    tele_username: Optional[str],
    status: str,
    reason: str,
    submitted_dt_sgt: str,
    time_detail: str,
) -> str:
    handle = f"@{tele_username}" if tele_username else "N/A"
    time_line = f"\nTime: {time_detail}" if time_detail.upper() != "NA" else "\nTime: NA"
    return (
        f"Name: {name}\n"
        f"Telegram Handle: {handle}\n"
        f"Status: {status}\n"
        f"Reason: {reason}\n"
        f"Submitted: {submitted_dt_sgt}"
        f"{time_line}"
    )


# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    first_name = update.effective_user.first_name or "dancer"
    await update.message.reply_text(start_message(first_name))

async def attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()

    # If user typed only "/attendance" (no args), just show instructions
    if msg == "/attendance":
        await update.message.reply_text(ATTENDANCE_INSTRUCTIONS)
        return

    try:
        date_str, status, name, reason, time_detail = parse_attendance_args(msg)
    except Exception as e:
        await update.message.reply_text(
            "⚠️ " + str(e) + "\n\n"
            "Format:\n/attendance [DD/MM/YYYY] [absent/late/leave early] [Name] [Reason] [NA/HH:MM (24h format)]"
        )
        return

    try:
        ws = get_ws()
        ensure_header_row(ws)
        col_idx = get_or_create_date_column(ws, date_str)

        ts_sgt = datetime.now(ZoneInfo("Asia/Singapore")).strftime("%Y-%m-%d %H:%M:%S")
        record_line = format_record_line(
            name=name,
            tele_username=update.effective_user.username,
            status=status,
            reason=reason,
            submitted_dt_sgt=ts_sgt,
            time_detail=time_detail,
        )
        append_record_under_column(ws, col_idx, record_line)

        time_line = f"Time (if applicable): {time_detail}" if time_detail.upper() != "NA" else "Time (if applicable): NA"
        await update.message.reply_text(
            f"✅ thank you {name}! your submission has been recorded.\n\n"
            f"Type: {status}\n"
            f"Date: {date_str}\n"
            f"Reason: {reason}\n"
            f"{time_line}\n"
            f"Submitted at: {ts_sgt}"
        )

    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(
            "❌ Could not write to Google Sheets.\n"
            "• Check GSHEET_ID\n"
            "• Ensure the Sheet is shared with the service account email (Editor)\n"
            "• Confirm SERVICE_ACCOUNT_FILE path\n"
            f"Error: {e}\nMore: {tb}"
        )

async def usefullinks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(title, url=url)] for title, url in USEFUL_LINKS.items()]
    await update.message.reply_text(
        "okay, here are some important links that you might be looking for 🤝:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_MESSAGE)

async def post_init(application):
    await application.bot.set_my_commands(COMMAND_MENU)


# =========================
# APP ENTRY
# =========================
def main():
    if not BOT_TOKEN or "REPLACE" in BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN (env var recommended).")
    if not GSHEET_ID or "REPLACE" in GSHEET_ID:
        raise RuntimeError("Set GSHEET_ID (from Google Sheets URL).")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("attendance", attendance))
    app.add_handler(CommandHandler("usefullinks", usefullinks))
    app.add_handler(CommandHandler("help", help_command))

    app.post_init = post_init  # sets the "/" command menu in Telegram

    print("BLAST bot is running locally with polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
