# bot.py
import asyncio
import logging
from typing import Optional, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError, BadRequest

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- local modules (must exist) ----------
from config import (
    BOT_TOKEN,
    REQUIRED_CHANNELS,            # e.g. ["srdexchange", "srdexchangeglobal", "srdearning"] (no @)
    WELCOME_REWARD_BEAM,          # int
    REFERRAL_REWARD_BEAM,         # int
    REFERRALS_PER_WITHDRAWAL,     # int
)
from db import init_db, SessionLocal, User, Referral, Payout
from web3_utils import is_address, checksum, send_tokens


# ========================= UI / KEYBOARDS =========================

def _channels_bullets(chs: List[str]) -> str:
    return "\n".join([f"   • @{c.lstrip('@')}" for c in chs])

WELCOME_TEXT = (
    "Welcome to <b>SRD Exchange Airdrop</b>!\n\n"
    "Complete these tasks:\n"
    "1) Join our Telegram channels:\n"
    f"{_channels_bullets(REQUIRED_CHANNELS)}\n"
    "   (the bot will verify).\n"
    "2) X tasks (not verified by bot).\n"
    "3) Submit your <b>BSC address</b> to receive "
    f"<b>{WELCOME_REWARD_BEAM} $BEAM</b> instantly.\n"
    "4) Referral: share your link. Each invite gives "
    f"<b>{REFERRAL_REWARD_BEAM} $BEAM</b>.\n"
    f"   Every <b>{REFERRALS_PER_WITHDRAWAL}</b> referrals triggers an auto-withdraw.\n"
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


# ========================= HELPERS =========================

def _dm_only(update: Update) -> bool:
    """True if update is in a private chat (DM)."""
    return bool(update.effective_chat and update.effective_chat.type == "private")


async def _ensure_user(session, tg_id: int, username: Optional[str]) -> User:
    user = session.query(User).filter_by(telegram_id=tg_id).one_or_none()
    if not user:
        user = User(telegram_id=tg_id, username=username or "")
        session.add(user)
        session.commit()
    return user


async def _is_member_of(context: ContextTypes.DEFAULT_TYPE, chat_username: str, user_id: int) -> bool:
    """Resolve @username to chat.id, then check membership. Handles spaces/@ and logs errors."""
    uname = (chat_username or "").strip().lstrip("@")
    try:
        chat = await context.bot.get_chat(f"@{uname}")
        member = await context.bot.get_chat_member(chat.id, user_id)
        ok = member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
        print(f"[DEBUG] verify @{uname}: {member.status} ({'OK' if ok else 'NO'})")
        return ok
    except Exception as e:
        print(f"[DEBUG] verify fail @{uname}: {e}")
        return False


async def _verify_all_required(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Return True only if ALL required channels pass; logs per-channel results."""
    all_ok = True
    for ch in REQUIRED_CHANNELS:
        if not await _is_member_of(context, ch, user_id):
            all_ok = False
        await asyncio.sleep(0.25)  # gentle with Telegram API
    return all_ok



# ========================= HANDLERS =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    logging.info("start from %s", update.effective_user.id if update.effective_user else "?")
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
                    logging.info("referral parse error: %s", e)

        await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb_main())
    finally:
        session.close()


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    await update.message.reply_text("Use /start to open the menu.", reply_markup=kb_main())


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    await update.message.reply_text("pong")


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
                await q.edit_message_text(
                    "✅ All channels joined! Now submit your BSC address.",
                    reply_markup=kb_main(),
                )
            else:
                txt = "❌ You haven't joined all channels yet. Please join:\n"
                for ch in REQUIRED_CHANNELS:
                    txt += f"• https://t.me/{ch}\n"
                await q.edit_message_text(txt, reply_markup=kb_main())

        elif data == "x_tasks":
            # Clickable X profiles
            await q.edit_message_text(
                "Follow on X:\n"
                "• <a href='https://x.com/srdaryandubey'>@srdaryandubey</a>\n"
                "• <a href='https://x.com/srdexchange'>@srdexchange</a>\n\n"
                "Like/Retweet the pinned post and tag 3 friends.\n\n"
                "Note: The bot does NOT verify X tasks.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_main(),
            )

        elif data == "submit_addr":
            await q.edit_message_text(
                "Send me your <b>BSC address</b> now:",
                parse_mode=ParseMode.HTML,
            )
            context.user_data["awaiting_bsc"] = True  # next text is treated as address

        elif data == "balance":
            bal = user.balance_beam or 0
            refs = user.referrals_count or 0
            txt = (
                f"💰 Balance: <b>{bal} BEAM</b>\n"
                f"👥 Referrals: <b>{refs}</b>\n\n"
                "Withdrawals are auto-triggered every "
                f"<b>{REFERRALS_PER_WITHDRAWAL}</b> referrals."
            )
            await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb_main())

        elif data == "ref":
            me = await context.bot.get_me()
            link = f"https://t.me/{me.username}?start=ref_{user.telegram_id}"
            await q.edit_message_text(f"Your referral link:\n{link}", reply_markup=kb_main())

        elif data == "help":
            await q.edit_message_text("Use /start to open the menu.", reply_markup=kb_main())

    finally:
        session.close()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return

    session = SessionLocal()
    try:
        user = await _ensure_user(session, update.effective_user.id, update.effective_user.username)

        # Waiting for BSC address?
        if context.user_data.get("awaiting_bsc"):
            addr = (update.message.text or "").strip()
            if not is_address(addr):
                await update.message.reply_text("❌ That doesn't look like a valid BSC address. Try again.")
                return

            addr = checksum(addr)
            user.bsc_address = addr
            session.commit()

            # Must be joined to all channels before reward
            if not await _verify_all_required(context, user.telegram_id):
                await update.message.reply_text(
                    "You must join all channels first. Tap 'Verify Telegram joins' again.",
                    reply_markup=kb_main(),
                )
                return

            # Send welcome reward
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

        # Any other text in DM
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
            user.referrals_count = refs - REFERRALS_PER_WITHDRAWAL  # basic rollover model
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


# ========================= DEBUG / DIAGNOSTICS =========================

async def checkverify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports what the bot sees for each REQUIRED_CHANNELS membership."""
    if not _dm_only(update):
        return
    res = []
    for ch in REQUIRED_CHANNELS:
        uname = ch.lstrip("@")
        try:
            chat = await context.bot.get_chat(f"@{uname}")
            member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
            res.append(f"@{uname}: {member.status} (chat_id={chat.id})")
        except TelegramError as e:
            res.append(f"@{uname}: ERROR ({e.message})")
        except Exception as e:
            res.append(f"@{uname}: ERROR ({e})")
        await asyncio.sleep(0.2)
    await update.message.reply_text("\n".join(res))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled error", exc_info=context.error)


# ========================= ENTRYPOINT =========================

def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    # Commands (DMs only)
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", help_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("ping", ping, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("checkverify", checkverify, filters=filters.ChatType.PRIVATE))

    # Callbacks & text (DMs only)
    app.add_handler(CallbackQueryHandler(button_handler))  # guarded inside
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_text))

    # Errors
    app.add_error_handler(error_handler)

    logging.info("Starting long polling…")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
