import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, HTTPException
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

load_dotenv()

TZ = ZoneInfo("Asia/Tashkent")
PHONE_RE = re.compile(r"^\+?\d[\d\s()-]{7,}$")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or "0")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
REG_DEADLINE = os.getenv("REG_DEADLINE", "2025-12-25").strip()

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
if not ADMIN_CHAT_ID:
    raise RuntimeError("ADMIN_CHAT_ID is missing")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is missing")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL is missing")

WEBHOOK_PATH = f"/telegram/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}"

# Conversation states
CHILD_FULLNAME, PARENT_FULLNAME, CHILD_PHOTO, PARENT_PHONE, CONFIRM = range(5)

app = FastAPI(title="CBU New Year Registration Bot (No SQL)")

ptb_app = Application.builder().token(TOKEN).build()


def deadline_passed() -> bool:
    # closes AFTER the deadline date (inclusive deadline day)
    y, m, d = [int(x) for x in REG_DEADLINE.split("-")]
    dl = date(y, m, d)
    return datetime.now(TZ).date() > dl


WELCOME = (
    "🎄 *Yangi yil bayramiga ro‘yxatdan o‘tish*\n\n"
    "Boshlash: /register\n"
    "Admin ID olish: /whoami"
)

CLOSED = (
    "⛔️ *Ro‘yxatdan o‘tish yopilgan.*\n\n"
    "Agar siz ro‘yxatdan o‘tgan bo‘lsangiz, kelish sanangiz bo‘yicha xabarnoma yuboriladi."
)


async def send_to_admin(context: ContextTypes.DEFAULT_TYPE, user, payload: dict):
    """
    Sends summary + photo to ADMIN_CHAT_ID
    """
    caption = (
        "🆕 *Yangi ro‘yxat*\n\n"
        f"👧🧒 Farzand: *{payload['child_fullname']}*\n"
        f"👤 Ota-ona: *{payload['parent_fullname']}*\n"
        f"📞 Telefon: *{payload['parent_phone']}*\n\n"
        f"👤 User: @{user.username if user.username else '—'}\n"
        f"🆔 user_id: `{user.id}`\n"
        f"💬 chat_id: `{payload['chat_id']}`\n"
        f"🕒 Vaqt: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}"
    )

    # Send photo with caption
    await context.bot.send_photo(
        chat_id=ADMIN_CHAT_ID,
        photo=payload["photo_file_id"],
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    c = update.effective_chat
    await update.message.reply_text(
        f"👤 username: @{u.username if u.username else '—'}\n"
        f"🆔 user_id: {u.id}\n"
        f"💬 chat_id: {c.id}\n\n"
        "✅ Admin qilish uchun",
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
        await update.message.reply_text("Iltimos, *to‘liq F.I.Sh* yuboring (kamida 2 ta so‘z).", parse_mode=ParseMode.MARKDOWN)
        return CHILD_FULLNAME

    context.user_data["child_fullname"] = text
    await update.message.reply_text(
        "2) Kuzatuvchi ota-onaning *ism va familiyasi*ni yuboring (Markaziy bank xodimi).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return PARENT_FULLNAME


async def parent_fullname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if len(text.split()) < 2:
        await update.message.reply_text("Iltimos, *to‘liq F.I.Sh* yuboring (kamida 2 ta so‘z).", parse_mode=ParseMode.MARKDOWN)
        return PARENT_FULLNAME

    context.user_data["parent_fullname"] = text
    await update.message.reply_text(
        "3) Farzandning *fotosurati*ni yuboring (oddiy foto/selfi).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return CHILD_PHOTO


async def child_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Iltimos, rasmni *foto* ko‘rinishida yuboring.", parse_mode=ParseMode.MARKDOWN)
        return CHILD_PHOTO

    context.user_data["photo_file_id"] = update.message.photo[-1].file_id

    await update.message.reply_text(
        "4) Kuzatuvchi ota-onaning *telefon raqami*ni yuboring.\n"
        "Masalan: +99890xxxxxxx",
        parse_mode=ParseMode.MARKDOWN,
    )
    return PARENT_PHONE


async def parent_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if deadline_passed():
        await update.message.reply_text(CLOSED, parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    phone = (update.message.text or "").strip()
    if not PHONE_RE.match(phone):
        await update.message.reply_text("Telefon raqam formati noto‘g‘ri. Masalan: +99890xxxxxxx")
        return PARENT_PHONE

    context.user_data["parent_phone"] = phone

    summary = (
        "✅ *Tekshiring:*\n\n"
        f"👧🧒 Farzand: *{context.user_data['child_fullname']}*\n"
        f"👤 Ota-ona: *{context.user_data['parent_fullname']}*\n"
        f"📞 Telefon: *{context.user_data['parent_phone']}*\n\n"
        "Tasdiqlash uchun: *Ha* deb yozing.\n"
        "Bekor qilish uchun: *Yo‘q* deb yozing."
    )
    await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = (update.message.text or "").strip().lower()
    if ans in ["yo‘q", "yoq", "no", "cancel"]:
        await update.message.reply_text("Bekor qilindi. /register orqali qayta boshlashingiz mumkin.")
        return ConversationHandler.END

    if ans not in ["ha", "xa", "yes", "ok"]:
        await update.message.reply_text("Iltimos, *Ha* yoki *Yo‘q* deb javob bering.", parse_mode=ParseMode.MARKDOWN)
        return CONFIRM

    chat_id = update.effective_chat.id
    user = update.effective_user

    payload = {
        "chat_id": chat_id,
        "child_fullname": context.user_data["child_fullname"],
        "parent_fullname": context.user_data["parent_fullname"],
        "parent_phone": context.user_data["parent_phone"],
        "photo_file_id": context.user_data["photo_file_id"],
    }

    # Send to admin (photo + full details)
    await send_to_admin(context, user, payload)

    await update.message.reply_text(
        "✨ *Ro‘yxatdan o‘tganingiz uchun rahmat!*\n\n"
        "📩 Ro‘yxatdan o‘tish 25-dekabr kuni yopilgach, bayramga kelish sanangiz ko‘rsatilgan xabarnoma sizga yuboriladi.",
        parse_mode=ParseMode.MARKDOWN,
    )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def build_handlers():
    conv = ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            CHILD_FULLNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, child_fullname)],
            PARENT_FULLNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, parent_fullname)],
            CHILD_PHOTO: [MessageHandler(filters.PHOTO, child_photo)],
            PARENT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, parent_phone)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("whoami", whoami))
    ptb_app.add_handler(conv)


@app.on_event("startup")
async def on_startup():
    build_handlers()
    # Set webhook
    await ptb_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)


@app.on_event("shutdown")
async def on_shutdown():
    await ptb_app.bot.delete_webhook(drop_pending_updates=False)


@app.get("/")
async def root():
    return {"ok": True, "webhook": WEBHOOK_PATH}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return {"ok": True}
