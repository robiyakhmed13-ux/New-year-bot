import os
import re
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------
# Load env
# ---------------------------
load_dotenv()

TZ = ZoneInfo("Asia/Tashkent")
PHONE_RE = re.compile(r"^\+?\d[\d\s()-]{7,}$")

TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or "0")
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()

REG_DEADLINE = (os.getenv("REG_DEADLINE") or "2025-12-25").strip()
CAPACITY_27 = int(os.getenv("CAPACITY_27", "200") or "200")
CAPACITY_28 = int(os.getenv("CAPACITY_28", "200") or "200")

GSHEET_ID = (os.getenv("GSHEET_ID") or "").strip()
GSHEET_TAB = (os.getenv("GSHEET_TAB") or "Sheet1").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()

# Basic validation (keep, but don't crash on Sheets at startup)
if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not ADMIN_CHAT_ID:
    raise RuntimeError("Missing ADMIN_CHAT_ID")
if not PUBLIC_URL:
    raise RuntimeError("Missing PUBLIC_URL")
if not WEBHOOK_SECRET:
    raise RuntimeError("Missing WEBHOOK_SECRET")
if not GSHEET_ID:
    raise RuntimeError("Missing GSHEET_ID")
if not GOOGLE_SERVICE_ACCOUNT_JSON:
    raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")

WEBHOOK_PATH = f"/telegram/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}"

# ---------------------------
# Conversation states
# ---------------------------
CHILD_FULLNAME, PARENT_FULLNAME, CHILD_PHOTO, PARENT_PHONE, CONFIRM = range(5)

WELCOME = (
    "🎄 *Yangi yil bayramiga ro‘yxatdan o‘tish*\n\n"
    "Boshlash: /register\n"
    "Admin ID olish: /whoami"
)

CLOSED = (
    "⛔️ *Ro‘yxatdan o‘tish yopilgan.*\n\n"
    "Agar siz ro‘yxatdan o‘tgan bo‘lsangiz, kelish sanangiz bo‘yicha xabarnoma yuboriladi."
)

NOTIF_27 = (
    "🔔 *27-dekabr kuni keladigan mehmonlar uchun bildirishnoma*\n\n"
    "Hurmatli ota-onalar!\n\n"
    "Siz va farzandingiz Yangi yil bayramiga *27-dekabr* kuni taklif etilgansiz.\n"
    "Bayram Markaziy bankning *B-binosida* bo‘lib o‘tadi.\n\n"
    "🕘 Yig‘ilish vaqti: *soat 9:30 dan*\n"
    "(shu vaqtda ro‘yxatdan o‘tish ishlari amalga oshiriladi)\n\n"
    "Iltimos, belgilangan vaqtda yetib kelishingizni so‘raymiz.\n"
    "Sizni bayramona muhit va quvonchli lahzalar kutmoqda! 🎄✨"
)

NOTIF_28 = (
    "🔔 *28-dekabr kuni keladigan mehmonlar uchun bildirishnoma*\n\n"
    "Hurmatli ota-onalar!\n\n"
    "Siz va farzandingiz Yangi yil bayramiga *28-dekabr* kuni taklif etilgansiz.\n"
    "Bayram Markaziy bankning *B-binosida* bo‘lib o‘tadi.\n\n"
    "🕘 Yig‘ilish vaqti: *soat 9:30 dan*\n"
    "(shu vaqtda ro‘yxatdan o‘tish ishlari amalga oshiriladi)\n\n"
    "Iltimos, belgilangan vaqtda yetib kelishingizni so‘raymiz.\n"
    "Farzandlaringiz uchun unutilmas Yangi yil bayrami tayyorlab qo‘yilgan! 🎅🎁"
)

# ---------------------------
# Google Sheets helpers
# ---------------------------
SHEETS = None  # init later

def _sheets_service():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def tab_range(a1: str) -> str:
    return f"{GSHEET_TAB}!{a1}"

def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def deadline_passed() -> bool:
    y, m, d = [int(x) for x in REG_DEADLINE.split("-")]
    dl = date(y, m, d)
    return datetime.now(TZ).date() > dl

def ensure_headers():
    """
    Creates header row if missing.
    IMPORTANT: Called in try/except on startup so it never kills the app.
    """
    global SHEETS
    if SHEETS is None:
        SHEETS = _sheets_service()

    resp = SHEETS.spreadsheets().values().get(
        spreadsheetId=GSHEET_ID, range=tab_range("A1:J1")
    ).execute()
    vals = resp.get("values", [])

    if vals and len(vals[0]) >= 3:
        return

    headers = [[
        "created_at", "chat_id", "user_id", "username",
        "child_fullname", "parent_fullname", "parent_phone",
        "photo_file_id", "assigned_day", "notified_at"
    ]]
    SHEETS.spreadsheets().values().update(
        spreadsheetId=GSHEET_ID,
        range=tab_range("A1:J1"),
        valueInputOption="RAW",
        body={"values": headers},
    ).execute()

def get_all_rows() -> List[List[str]]:
    global SHEETS
    if SHEETS is None:
        SHEETS = _sheets_service()

    resp = SHEETS.spreadsheets().values().get(
        spreadsheetId=GSHEET_ID, range=tab_range("A2:J")
    ).execute()
    return resp.get("values", [])

def count_assigned(day: int) -> int:
    rows = get_all_rows()
    c = 0
    for r in rows:
        if len(r) >= 9 and str(r[8]).strip() == str(day):
            c += 1
    return c

def choose_day() -> int:
    d27 = count_assigned(27)
    d28 = count_assigned(28)
    if d27 < CAPACITY_27:
        return 27
    if d28 < CAPACITY_28:
        return 28
    return 27 if d27 <= d28 else 28

def upsert_registration_row(
    chat_id: int,
    user_id: int,
    username: str,
    child_fullname: str,
    parent_fullname: str,
    parent_phone: str,
    photo_file_id: str,
    assigned_day: int,
):
    """
    If chat_id exists, update that row; else append new row.
    """
    global SHEETS
    if SHEETS is None:
        SHEETS = _sheets_service()

    rows = get_all_rows()
    target_row_index = None  # 0-based in rows (A2=0)
    for idx, r in enumerate(rows):
        if len(r) >= 2 and str(r[1]).strip() == str(chat_id):
            target_row_index = idx
            break

    values = [[
        now_str(), str(chat_id), str(user_id), username or "",
        child_fullname, parent_fullname, parent_phone,
        photo_file_id, str(assigned_day), ""  # notified_at empty
    ]]

    if target_row_index is None:
        SHEETS.spreadsheets().values().append(
            spreadsheetId=GSHEET_ID,
            range=tab_range("A2:J"),
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
    else:
        row_num = 2 + target_row_index
        SHEETS.spreadsheets().values().update(
            spreadsheetId=GSHEET_ID,
            range=tab_range(f"A{row_num}:J{row_num}"),
            valueInputOption="RAW",
            body={"values": values},
        ).execute()

def get_chat_ids_to_notify(day: int) -> List[int]:
    rows = get_all_rows()
    out: List[int] = []
    for r in rows:
        # need chat_id, parent_fullname, notified_at
        if len(r) < 10:
            continue

        chat_id_str = str(r[1]).strip()
        parent_fullname = str(r[5]).strip()  # parent_fullname column
        notified = str(r[9]).strip()

        if notified != "":
            continue

        computed_day = assign_day_by_surname(parent_fullname)
        if computed_day != day:
            continue

        try:
            out.append(int(chat_id_str))
        except Exception:
            continue

    return out
def mark_notified(chat_id: int):
    global SHEETS
    if SHEETS is None:
        SHEETS = _sheets_service()

    rows = get_all_rows()
    for idx, r in enumerate(rows):
        if len(r) >= 2 and str(r[1]).strip() == str(chat_id):
            row_num = 2 + idx
            SHEETS.spreadsheets().values().update(
                spreadsheetId=GSHEET_ID,
                range=tab_range(f"J{row_num}"),
                valueInputOption="RAW",
                body={"values": [[now_str()]]},
            ).execute()
            return

# ---------------------------
# ✅ ADDED (minimal): Assign day by surname
# ---------------------------
def _extract_surname(fullname: str) -> str:
    parts = [p for p in (fullname or "").strip().split() if p]
    if not parts:
        return ""
    return parts[0]  # surname = first word
def assign_day_by_surname(fullname_for_grouping: str) -> int:
    """
    Uzbek (lotin) familiya bo‘yicha:
    - A dan O gacha  -> 27-dekabr
    - P dan CH gacha -> 28-dekabr
    Eslatma: CH (digraph) har doim 28.
    """
    surname = _extract_surname(fullname_for_grouping)
    s = (surname or "").strip().upper()
    s = s.replace("’", "").replace("'", "").replace("-", "")

    if not s:
        return 27

    # Special Uzbek digraphs
    if s.startswith("CH"):
        return 28

    # Uzbek letters like O‘, G‘ start with O / G anyway after cleaning above
    first = s[0]

    # A..O => 27
    if "A" <= first <= "O":
        return 27

    # P..Z => 28
    if "P" <= first <= "Z":
        return 28

    return 27
27

# ---------------------------
# Telegram handlers
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    c = update.effective_chat
    await update.message.reply_text(
        f"👤 username: @{u.username if u.username else '—'}\n"
        f"🆔 user_id: {u.id}\n"
        f"💬 chat_id: {c.id}",
        parse_mode=ParseMode.MARKDOWN,
    )

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if deadline_passed():
        await update.message.reply_text(CLOSED, parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "1) Farzandning *ism va familiyasi*ni yuboring (to‘liq).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    return CHILD_FULLNAME

async def child_fullname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if len(text.split()) < 2:
        await update.message.reply_text("Iltimos, *to‘liq F.I.Sh* yuboring.", parse_mode=ParseMode.MARKDOWN)
        return CHILD_FULLNAME
    context.user_data["child_fullname"] = text
    await update.message.reply_text("2) Kuzatuvchi ota-onaning *ism va familiyasi*ni yuboring.", parse_mode=ParseMode.MARKDOWN)
    return PARENT_FULLNAME

async def parent_fullname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if len(text.split()) < 2:
        await update.message.reply_text("Iltimos, *to‘liq F.I.Sh* yuboring.", parse_mode=ParseMode.MARKDOWN)
        return PARENT_FULLNAME
    context.user_data["parent_fullname"] = text
    await update.message.reply_text("3) Farzandning *fotosurati*ni yuboring (foto/selfi).", parse_mode=ParseMode.MARKDOWN)
    return CHILD_PHOTO

async def child_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Iltimos, rasmni *foto* ko‘rinishida yuboring.", parse_mode=ParseMode.MARKDOWN)
        return CHILD_PHOTO
    context.user_data["photo_file_id"] = update.message.photo[-1].file_id
    await update.message.reply_text("4) Telefon raqamingizni yuboring. Masalan: +99890xxxxxxx", parse_mode=ParseMode.MARKDOWN)
    return PARENT_PHONE

async def parent_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if deadline_passed():
        await update.message.reply_text(CLOSED, parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    phone = (update.message.text or "").strip()
    if not PHONE_RE.match(phone):
        await update.message.reply_text("Telefon raqam noto‘g‘ri. Masalan: +99890xxxxxxx", parse_mode=ParseMode.MARKDOWN)
        return PARENT_PHONE

    context.user_data["parent_phone"] = phone
    await update.message.reply_text(
        "✅ *Tekshiring:*\n\n"
        f"👧🧒 Farzand: *{context.user_data['child_fullname']}*\n"
        f"👤 Ota-ona: *{context.user_data['parent_fullname']}*\n"
        f"📞 Telefon: *{context.user_data['parent_phone']}*\n\n"
        "Tasdiqlash uchun: *Ha* (yozing)\nBekor qilish: *Yo‘q*",
        parse_mode=ParseMode.MARKDOWN,
    )
    return CONFIRM

async def send_to_admin(context: ContextTypes.DEFAULT_TYPE, user, payload: Dict[str, Any]):
    caption = (
        "🆕 *Yangi ro‘yxatdan o‘tish*\n\n"
        f"👧🧒 Farzand: *{payload['child_fullname']}*\n"
        f"👤 Ota-ona: *{payload['parent_fullname']}*\n"
        f"📞 Telefon: *{payload['parent_phone']}*\n\n"
        f"👤 Username: @{user.username if user.username else '—'}\n"
        f"🆔 user_id: `{user.id}`\n"
        f"💬 chat_id: `{payload['chat_id']}`\n"
        f"🕒 Vaqt: {now_str()}"
    )
    await context.bot.send_photo(
        chat_id=ADMIN_CHAT_ID,
        photo=payload["photo_file_id"],
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
    )
async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = (update.message.text or "").strip().lower()
    if ans in {"yo‘q", "yoq", "no", "cancel"}:
        await update.message.reply_text("Bekor qilindi. /register orqali qayta boshlang.")
        return ConversationHandler.END
    if ans not in {"ha", "xa", "yes", "ok"}:
        await update.message.reply_text("Iltimos, *Ha* yoki *Yo‘q* deb javob bering.", parse_mode=ParseMode.MARKDOWN)
        return CONFIRM

    user = update.effective_user
    chat_id = update.effective_chat.id

    # ✅ ONLY CHANGE: assign day by surname rule (NOT capacity)
    assigned_day = assign_day_by_surname(context.user_data.get("parent_fullname", ""))

    # Write to Sheets (try, but don't crash)
    try:
        upsert_registration_row(
            chat_id=chat_id,
            user_id=user.id,
            username=user.username or "",
            child_fullname=context.user_data["child_fullname"],
            parent_fullname=context.user_data["parent_fullname"],
            parent_phone=context.user_data["parent_phone"],
            photo_file_id=context.user_data["photo_file_id"],
            assigned_day=assigned_day,
        )
    except Exception as e:
        print("Sheets upsert failed:", e)

    # Always send to admin (with photo)
    payload = {
        "chat_id": chat_id,
        "child_fullname": context.user_data["child_fullname"],
        "parent_fullname": context.user_data["parent_fullname"],
        "parent_phone": context.user_data["parent_phone"],
        "photo_file_id": context.user_data["photo_file_id"],
        "assigned_day": assigned_day,
    }
    await send_to_admin(context, user, payload)

    await update.message.reply_text(
        "✨ *Ro‘yxatdan o‘tganingiz uchun rahmat!*\n\n"
        "🧸 *Guruhlar bo‘yicha tashrif tartibi:*\n"
        "- A dan O gacha bo‘lgan familiyalar — 27-dekabr\n"
        "- P dan CH gacha bo‘lgan familiyalar — 28-dekabr\n\n"
        "📩 Ro‘yxat yopilgach, kelish sanangiz bo‘yicha xabarnoma yuboriladi.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END

# ---------------------------
# Admin push commands
# ---------------------------
async def notify_day(update: Update, context: ContextTypes.DEFAULT_TYPE, day: int):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Bu buyruq faqat admin uchun.")
        return

    try:
        chat_ids = get_chat_ids_to_notify(day)
    except Exception as e:
        await update.message.reply_text(f"Sheets xatolik: {e}")
        return

    if not chat_ids:
        await update.message.reply_text(f"{day}-dekabr uchun yuboriladigan (yangi) ro‘yxat yo‘q.")
        return

    msg = NOTIF_27 if day == 27 else NOTIF_28
    sent, failed = 0, 0
    for cid in chat_ids:
        try:
            await context.bot.send_message(chat_id=cid, text=msg, parse_mode=ParseMode.MARKDOWN)
            try:
                mark_notified(cid)
            except Exception:
                pass
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ Yuborildi: {sent}\n⚠️ Xatolik: {failed}")

async def notify27(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await notify_day(update, context, 27)

async def notify28(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await notify_day(update, context, 28)

async def export_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Bu buyruq faqat admin uchun.")
        return
    try:
        c27 = count_assigned(27)
        c28 = count_assigned(28)
        await update.message.reply_text(f"📊 Assigned:\n27-dekabr: {c27}\n28-dekabr: {c28}")
    except Exception as e:
        await update.message.reply_text(f"Sheets xatolik: {e}")

# ---------------------------
# FastAPI + PTB wiring
# ---------------------------
api = FastAPI(title="CBU NY Bot (Sheets + Push)")
ptb_app = Application.builder().token(TOKEN).build()

def setup_handlers():
    conv = ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            CHILD_FULLNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, child_fullname)],
            PARENT_FULLNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, parent_fullname)],
            CHILD_PHOTO: [MessageHandler(filters.PHOTO, child_photo)],
            PARENT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, parent_phone)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("whoami", whoami))
    ptb_app.add_handler(CommandHandler("notify27", notify27))
    ptb_app.add_handler(CommandHandler("notify28", notify28))
    ptb_app.add_handler(CommandHandler("export", export_stats))
    ptb_app.add_handler(conv)

@api.on_event("startup")
async def on_startup():
    # ✅ 1) handlers first
    setup_handlers()

    # ✅ 2) initialize PTB (required for v21+)
    await ptb_app.initialize()
    await ptb_app.start()

    # ✅ 3) set webhook
    await ptb_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)

    # ✅ 4) Sheets setup should NEVER block app start
    try:
        ensure_headers()
    except HttpError as e:
        print("⚠️ Sheets HttpError:", e)
    except Exception as e:
        print("⚠️ Sheets ensure_headers failed:", e)

@api.on_event("shutdown")
async def on_shutdown():
    try:
        await ptb_app.bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass
    await ptb_app.stop()
    await ptb_app.shutdown()

@api.get("/")
async def root():
    return {"ok": True, "webhook": WEBHOOK_URL}

@api.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return {"ok": True}
