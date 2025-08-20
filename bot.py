import asyncio
import logging
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------------------- local modules --------------------
from config import (
    BOT_TOKEN,
    REQUIRED_CHANNELS,          # e.g. ["srdexchange", "srdexchangeglobal", "srdearning"]
    WELCOME_REWARD_BEAM,        # e.g. 1
    REFERRAL_REWARD_BEAM,       # e.g. 1 (used for auto withdraw chunk)
    REFERRALS_PER_WITHDRAWAL,   # e.g. 3
)
from db import init_db, SessionLocal, User, Referral, Payout
from web3_utils import is_address, checksum, send_tokens

# ======================================================
# Logging
# ======================================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("srd_airdrop_bot")


# ======================================================
# UI
# ======================================================

WELCOME_TEXT = (
    "Welcome to <b>SRD Exchange Airdrop</b>!\n\n"
    "Complete these tasks:\n"
    "1) Join our Telegram channels:\n"
    f"   • @{REQUIRED_CHANNELS[0]}\n"
    f"   • @{REQUIRED_CHANNELS[1]}\n"
    f"   • @{REQUIRED_CHANNELS[2]}\n"
    "   (the bot will verify).\n"
    "2) X tasks (not verified by bot).\n"
    "3) Submit your <b>BSC address</b> to receive "
    f"<b>{WELCOME_REWARD_BEAM} $BEAM</b> instantly.\n"
    f"4) Referral: each invite gives <b>{REFERRAL_REWARD_BEAM} $BEAM</b>.\n"
    f"   Every <b>{REFERRALS_PER_WITHDRAWAL}</b> referrals triggers an "
    "auto-withdraw from admin wallet.\n"
)


def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Verify Telegram joins", callback_data="verify")],
            [InlineKeyboardButton("ℹ️  View X Tasks (info)", callback_data="x_tasks")],
            [InlineKeyboardButton("📬 Submit BSC Address", callback_data="submit_addr")],
            [InlineKeyboardButton("💰 Balance & Withdraw", callback_data="balance")],
            [InlineKeyboardButton("🔗 Referral Link", callback_data="ref")],
            [InlineKeyboardButton("❓ Help", callback_data="help")],
        ]
    )


# ======================================================
# Helpers
# ======================================================

def _dm_only(update: Update) -> bool:
    """Return True if update is in a private chat (DM)."""
    return bool(update.effective_chat and update.effective_chat.type == "private")


async def _ensure_user(session, tg_id: int, username: Optional[str]) -> User:
    user = session.query(User).filter_by(telegram_id=tg_id).one_or_none()
    if not user:
        user = User(telegram_id=tg_id, username=username or "")
        session.add(user)
        session.commit()
    return user


async def _is_member_of(context: ContextTypes.DEFAULT_TYPE, chat_ref: str, user_id: int) -> bool:
    """
    Check if user is a member of a channel.
    Supports both @username and numeric chat_id (-100...).
    """
    try:
        if chat_ref.startswith("-100"):   # numeric chat_id
            chat_id = int(chat_ref)
        else:  # username
            uname = chat_ref.strip().lstrip("@")
            chat = await context.bot.get_chat(f"@{uname}")
            chat_id = chat.id

        member = await context.bot.get_chat_member(chat_id, user_id)
        ok = member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
        logging.info("verify %s -> %s (%s)", chat_ref, member.status, "OK" if ok else "NO")
        return ok
    except Exception as e:
        logging.warning("verify fail for %s: %s", chat_ref, e)
        return False



async def _verify_all_required(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Return True only if ALL required channels pass; logs per-channel results."""
    all_ok = True
    for ch in REQUIRED_CHANNELS:
        if not await _is_member_of(context, ch, user_id):
            all_ok = False
        await asyncio.sleep(0.25)  # be gentle with API
    return all_ok


async def safe_edit_message(query, text: str, **kwargs):
    """
    Edit message text safely, ignoring 'message is not modified' errors.
    Usage: await safe_edit_message(q, "text", parse_mode=..., reply_markup=...)
    """
    try:
        if query.message and getattr(query.message, "text", None) == text:
            return
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        raise


# ======================================================
# Handlers
# ======================================================

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
                except Exception:
                    pass

        await update.message.reply_text(
            WELCOME_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(),
        )
    finally:
        session.close()


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    await update.message.reply_text("Use /start to open the menu.", reply_markup=kb_main())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all inline keyboard button clicks."""
    if not _dm_only(update):
        return

    q = update.callback_query
    if not q:
        return

    await q.answer()
    data = (q.data or "").strip()

    session = SessionLocal()
    try:
        user = await _ensure_user(session, q.from_user.id, q.from_user.username)

        # ----------------- VERIFY -----------------
        if data == "verify":
            lines = []
            all_ok = True
            user_id = q.from_user.id

            for ch in REQUIRED_CHANNELS:
                uname = (ch or "").strip().lstrip("@")
                try:
                    chat = await context.bot.get_chat(f"@{uname}")
                    m = await context.bot.get_chat_member(chat.id, user_id)
                    status = m.status
                    is_joined = status in (
                        ChatMemberStatus.MEMBER,
                        ChatMemberStatus.ADMINISTRATOR,
                        ChatMemberStatus.CREATOR,
                    )
                    lines.append(f"@{uname}: {status}{' ✅' if is_joined else ' ❌'} (chat_id={chat.id})")
                    if not is_joined:
                        all_ok = False
                except Exception as e:
                    lines.append(f"@{uname}: ERROR ({e}) ❌")
                    all_ok = False
                await asyncio.sleep(0.25)

            if all_ok:
                await safe_edit_message(
                    q,
                    "✅ All channels joined! Now submit your BSC address.",
                    reply_markup=kb_main(),
                )
            else:
                await safe_edit_message(
                    q,
                    "❌ You haven't joined all channels yet.\n\n"
                    + "\n".join(lines)
                    + "\n\nPlease join and tap Verify again.",
                    reply_markup=kb_main(),
                )

        # ----------------- X TASKS -----------------
        elif data == "x_tasks":
            # clickable X/Twitter profile links
            text = (
                "<b>Follow on X:</b>\n"
                "• <a href=\"https://x.com/srdaryandubey\">@srdaryandubey</a>\n"
                "• <a href=\"https://x.com/srdexchange\">@srdexchange</a>\n\n"
                "Like/Retweet the pinned post and tag 3 friends.\n\n"
                "<i>Note: The bot does NOT verify X tasks.</i>"
            )
            await safe_edit_message(
                q,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb_main(),
            )

        # --------------- SUBMIT ADDRESS ------------
        elif data == "submit_addr":
            await safe_edit_message(
                q,
                "Send me your <b>BSC address</b> now:",
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
            context.user_data["awaiting_bsc"] = True

        # ----------------- BALANCE ------------------
        elif data == "balance":
            bal = user.balance_beam or 0
            refs = user.referrals_count or 0
            txt = (
                f"💰 Balance: <b>{bal} BEAM</b>\n"
                f"👥 Referrals: <b>{refs}</b>\n\n"
                "Withdrawals are auto-triggered every "
                f"<b>{REFERRALS_PER_WITHDRAWAL}</b> referrals."
            )
            await safe_edit_message(
                q,
                txt,
                parse_mode=ParseMode.HTML,
                reply_markup=kb_main(),
            )

        # ----------------- REFERRAL -----------------
        elif data == "ref":
            me = await context.bot.get_me()
            link = f"https://t.me/{me.username}?start=ref_{user.telegram_id}"
            await safe_edit_message(
                q,
                f"Your referral link:\n{link}",
                reply_markup=kb_main(),
            )

        # ------------------- HELP -------------------
        elif data == "help":
            await safe_edit_message(
                q,
                "Use /start to open the menu.",
                reply_markup=kb_main(),
            )

        # -------------- FALLBACK / UNKNOWN ----------
        else:
            await safe_edit_message(
                q,
                "Unknown action. Use /start.",
                reply_markup=kb_main(),
            )

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
                    reply_markup=kb_main(),
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
                    reply_markup=kb_main(),
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
                reply_markup=kb_main(),
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
                reply_markup=kb_main(),
            )
        except Exception as e:
            await update.message.reply_text(f"⚠️ Withdrawal failed: {e}", reply_markup=kb_main())

    finally:
        session.close()


# ======================================================
# Diagnostics / Debug
# ======================================================

async def checkverify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report what the bot sees for each REQUIRED_CHANNELS membership (debug)."""
    if not _dm_only(update):
        return

    res = []
    uid = update.effective_user.id
    for ch in REQUIRED_CHANNELS:
        uname = (ch or "").strip().lstrip("@")
        try:
            chat = await context.bot.get_chat(f"@{uname}")
            m = await context.bot.get_chat_member(chat.id, uid)
            res.append(f"@{uname}: {m.status} (chat_id={chat.id})")
        except Exception as e:
            res.append(f"@{uname}: ERROR ({e})")
        await asyncio.sleep(0.25)

    await update.message.reply_text("\n".join(res))


# ======================================================
# Error handler
# ======================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)


# ======================================================
# Entrypoint
# ======================================================

async def _clear_webhook(app):
    # Ensure no lingering webhook/polling conflicts
    await app.bot.delete_webhook(drop_pending_updates=True)


def main():
    init_db()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_clear_webhook)
        .build()
    )

    # commands
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", help_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("checkverify", checkverify, filters=filters.ChatType.PRIVATE))  # debug

    # callbacks & text
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_text))

    # errors
    app.add_error_handler(error_handler)

    # polling
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
