# bot.py
# python-telegram-bot v20.x async bot

import asyncio
import logging
from typing import Optional, Union

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---- local modules you already have ----
from config import (
    BOT_TOKEN,
    REQUIRED_CHANNELS,             # list of @names OR -100.. ids (str or int)
    WELCOME_REWARD_BEAM,
    REFERRAL_REWARD_BEAM,
    REFERRALS_PER_WITHDRAWAL,
)
from db import init_db, SessionLocal, User, Referral, Payout
from web3_utils import is_address, checksum, send_tokens


# =========================== Logging ===========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("srd_airdrop_bot")


# =========================== UI TEXT ===========================
WELCOME_TEXT = (
    "Welcome to <b>SRD Exchange Airdrop</b>!\n\n"
    "Complete these tasks:\n"
    "1) Join our Telegram channels (the bot will verify).\n"
    "2) X tasks.\n"
    "3) Submit your <b>BSC address</b> to receive "
    f"<b>{WELCOME_REWARD_BEAM} $BEAM</b> instantly.\n"
    f"4) Referral: each invite gives <b>{REFERRAL_REWARD_BEAM} $BEAM</b>.\n"
    f"   Every <b>{REFERRALS_PER_WITHDRAWAL}</b> referrals triggers an "
    "auto-withdraw from admin wallet.\n"
)

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Verify Telegram joins", callback_data="verify")],
        [InlineKeyboardButton("🧭 View X Tasks (info)", callback_data="x_tasks")],
        [InlineKeyboardButton("📬 Submit BSC Address", callback_data="submit_addr")],
        [InlineKeyboardButton("💰 Balance & Withdraw", callback_data="balance")],
        [InlineKeyboardButton("🔗 Referral Link", callback_data="ref")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ])


# =========================== Helpers ===========================
def _dm_only(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")


async def _ensure_user(session, tg_id: int, username: Optional[str]) -> User:
    user = session.query(User).filter_by(telegram_id=tg_id).one_or_none()
    if not user:
        user = User(telegram_id=tg_id, username=username or "")
        session.add(user)
        session.commit()
    return user


async def _is_member_of(
    context: ContextTypes.DEFAULT_TYPE,
    channel: Union[int, str],
    user_id: int
) -> bool:
    """
    Accepts either @username (str) or a numeric chat_id (int or '-100…' str).
    Returns True if user is NOT 'left' or 'kicked'.
    """
    # Resolve chat
    if isinstance(channel, int):
        chat_id = channel
        chat = await context.bot.get_chat(chat_id)
    else:
        raw = str(channel).strip()
        if raw.startswith("-100") and raw[4:].isdigit():
            chat_id = int(raw)
            chat = await context.bot.get_chat(chat_id)
        else:
            uname = raw.lstrip("@")
            chat = await context.bot.get_chat(f"@{uname}")

    member = await context.bot.get_chat_member(chat.id, user_id)
    status = str(member.status).lower()
    is_joined = status not in {"left", "kicked"}

    # debug line
    logger.info("verify %s -> status=%s => %s", chat.id, member.status, "OK" if is_joined else "NO")
    return is_joined


async def _verify_all_required(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Return True only if ALL required channels pass."""
    all_ok = True
    for ch in REQUIRED_CHANNELS:
        try:
            ok = await _is_member_of(context, ch, user_id)
            if not ok:
                all_ok = False
        except Exception as e:
            logger.warning("verify failed for %s: %s", ch, e)
            all_ok = False
        await asyncio.sleep(0.2)  # be gentle with API
    return all_ok


async def safe_edit_message(query, text: str, **kwargs):
    """
    Edit message text safely, ignoring 'message is not modified' errors.
    """
    try:
        if query.message and getattr(query.message, "text", None) == text and "reply_markup" not in kwargs:
            return
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        raise


# =========================== Handlers ===========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    session = SessionLocal()
    try:
        user = await _ensure_user(session, update.effective_user.id, update.effective_user.username)

        # Handle referral payload: /start ref_12345
        if update.message and update.message.text:
            parts = update.message.text.split(maxsplit=1)
            if len(parts) > 1 and parts[1].startswith("ref_"):
                try:
                    referrer_id = int(parts[1][4:])
                    if referrer_id != user.telegram_id and user.referred_by is None:
                        ref = session.query(User).filter_by(telegram_id=referrer_id).one_or_none()
                        if ref:
                            user.referred_by = ref.telegram_id
                            session.add(Referral(referrer_id=ref.telegram_id, referee_id=user.telegram_id))
                            session.commit()
                except Exception as e:
                    logger.info("referral parse error: %s", e)

        await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb_main())
    finally:
        session.close()


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    await update.message.reply_text("Use /start to open the menu.", reply_markup=kb_main())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    session = SessionLocal()

    try:
        user = await _ensure_user(session, q.from_user.id, q.from_user.username)

        if data == "verify":
            ok = await _verify_all_required(context, user.telegram_id)
            if ok:
                await safe_edit_message(
                    q,
                    "✅ All channels joined! Now submit your BSC address.",
                    reply_markup=kb_main()
                )
            else:
                # Show the configured channels (as links where possible)
                lines = []
                for ch in REQUIRED_CHANNELS:
                    if isinstance(ch, int) or (isinstance(ch, str) and ch.startswith("-100")):
                        lines.append(f"• {ch}")
                    else:
                        uname = str(ch).lstrip("@")
                        lines.append(f"• https://t.me/{uname}")
                await safe_edit_message(
                    q,
                    "❌ You haven't joined all channels yet. Please join:\n" + "\n".join(lines),
                    reply_markup=kb_main()
                )

        elif data == "x_tasks":
            # Provide buttons that open X (Twitter) profiles directly
            x_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Follow @srdaryandubey", url="https://x.com/srdaryandubey")],
                [InlineKeyboardButton("Follow @srdexchange", url="https://x.com/srdexchange")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
            ])
            await safe_edit_message(
                q,
                "Follow both profiles and do the pinned post task (not verified by bot).",
                reply_markup=x_kb
            )

        elif data == "back_main":
            await safe_edit_message(q, WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb_main())

        elif data == "submit_addr":
            await safe_edit_message(q, "Send me your <b>BSC address</b> now:", parse_mode=ParseMode.HTML)
            context.user_data["awaiting_bsc"] = True

        elif data == "balance":
            bal = user.balance_beam or 0
            refs = user.referrals_count or 0
            txt = (
                f"💰 Balance: <b>{bal} BEAM</b>\n"
                f"👥 Referrals: <b>{refs}</b>\n\n"
                "Withdrawals are auto-triggered every "
                f"<b>{REFERRALS_PER_WITHDRAWAL}</b> referrals."
            )
            await safe_edit_message(q, txt, parse_mode=ParseMode.HTML, reply_markup=kb_main())

        elif data == "ref":
            me = await context.bot.get_me()
            link = f"https://t.me/{me.username}?start=ref_{user.telegram_id}"
            await safe_edit_message(q, f"Your referral link:\n{link}", reply_markup=kb_main())

        elif data == "help":
            await safe_edit_message(q, "Use /start to open the menu.", reply_markup=kb_main())

    finally:
        session.close()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    session = SessionLocal()
    try:
        user = await _ensure_user(session, update.effective_user.id, update.effective_user.username)

        # Expecting BSC address?
        if context.user_data.get("awaiting_bsc"):
            addr = (update.message.text or "").strip()
            if not is_address(addr):
                await update.message.reply_text("❌ That doesn't look like a valid BSC address. Try again.")
                return

            addr = checksum(addr)
            user.bsc_address = addr
            session.commit()

            # verify telegram joins before rewarding
            ok = await _verify_all_required(context, user.telegram_id)
            if not ok:
                await update.message.reply_text(
                    "You must join all channels first. Tap 'Verify Telegram joins' again.",
                    reply_markup=kb_main()
                )
                return

            # send welcome reward
            try:
                tx_hash = send_tokens(addr, WELCOME_REWARD_BEAM)
                user.balance_beam = (user.balance_beam or 0) + WELCOME_REWARD_BEAM
                session.commit()
                await update.message.reply_text(
                    f"✅ Address saved.\nSent <b>{WELCOME_REWARD_BEAM} BEAM</b>.\nTX: {tx_hash}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb_main()
                )
            except Exception as e:
                await update.message.reply_text(f"⚠️ Transfer failed: {e}", reply_markup=kb_main())

            context.user_data["awaiting_bsc"] = False
            return

        # any other text in DM
        await update.message.reply_text("Use /start to open the menu.", reply_markup=kb_main())

    finally:
        session.close()


async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    session = SessionLocal()
    try:
        user = await _ensure_user(session, update.effective_user.id, update.effective_user.username)

        refs = user.referrals_count or 0
        if refs < REFERRALS_PER_WITHDRAWAL:
            need = REFERRALS_PER_WITHDRAWAL - refs
            await update.message.reply_text(
                f"You need {need} more referral(s) to trigger auto-withdraw.",
                reply_markup=kb_main()
            )
            return

        if not user.bsc_address:
            await update.message.reply_text("Submit your BSC address first.", reply_markup=kb_main())
            return

        try:
            tx_hash = send_tokens(user.bsc_address, REFERRAL_REWARD_BEAM)
            user.balance_beam = (user.balance_beam or 0) + REFERRAL_REWARD_BEAM
            user.referrals_count = refs - REFERRALS_PER_WITHDRAWAL
            session.add(Payout(telegram_id=user.telegram_id, amount=REFERRAL_REWARD_BEAM, tx_hash=tx_hash))
            session.commit()
            await update.message.reply_text(
                f"✅ Withdrawn <b>{REFERRAL_REWARD_BEAM} BEAM</b>.\nTX: {tx_hash}",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_main()
            )
        except Exception as e:
            await update.message.reply_text(f"⚠️ Withdrawal failed: {e}", reply_markup=kb_main())

    finally:
        session.close()


# ------------ Debug command to see exactly what the bot sees ------------
async def checkverify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    user_id = update.effective_user.id
    rows = []
    for ch in REQUIRED_CHANNELS:
        try:
            # Resolve for display
            if isinstance(ch, int) or (isinstance(ch, str) and ch.startswith("-100")):
                chat = await context.bot.get_chat(int(ch))
                label = str(chat.id)
            else:
                uname = str(ch).lstrip("@")
                chat = await context.bot.get_chat(f"@{uname}")
                label = f"@{uname}"

            m = await context.bot.get_chat_member(chat.id, user_id)
            status = str(m.status).upper()
            ok = status.lower() not in {"left", "kicked"}
            rows.append(f"{'✅' if ok else '❌'} {label} → {status} ({'OK' if ok else 'ERROR'})")
        except Exception as e:
            rows.append(f"❌ {ch} → ERROR: {e}")
        await asyncio.sleep(0.15)

    await update.message.reply_text("\n".join(rows))


# =========================== Errors / startup ===========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)


async def _clear_webhook(app):
    """Make sure long polling is used (Railway)."""
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass


# =========================== Entrypoint ===========================
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    # Commands
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", help_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd, filters=filters.ChatType.PRIVATE))

    # Debug command
    app.add_handler(CommandHandler("checkverify", checkverify, filters=filters.ChatType.PRIVATE))

    # Callback buttons & DM text
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    # Ensure no webhook and start polling
    app.post_init = _clear_webhook
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
