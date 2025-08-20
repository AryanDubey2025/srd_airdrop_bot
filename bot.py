import asyncio
from typing import Optional
from urllib.parse import urlparse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---- local modules (must exist) ----
from config import (
    BOT_TOKEN,
    REQUIRED_CHANNELS,
    WELCOME_REWARD_BEAM,
    REFERRAL_REWARD_BEAM,
    REFERRALS_PER_WITHDRAWAL,
)
from db import init_db, SessionLocal, User, Referral, Payout
from web3_utils import is_address, checksum, send_tokens


# ==================== UI TEXT ====================

WELCOME_TEXT = (
    "Welcome to <b>SRD Exchange Airdrop</b>!\n\n"
    "Complete these tasks:\n"
    "1) Join our Telegram channels:\n"
    f"   • @{REQUIRED_CHANNELS[0]}\n"
    f"   • @{REQUIRED_CHANNELS[1]}\n"
    f"   • @{REQUIRED_CHANNELS[2]}\n"
    "   (the bot will verify).\n"
    "2) X tasks (not verified by bot).\n"
    "3) Submit your <b>BSC address</b> to receive <b>"
    f"{WELCOME_REWARD_BEAM} $BEAM</b> instantly.\n"
    f"4) Referral: each invite gives <b>{REFERRAL_REWARD_BEAM} $BEAM</b>.\n"
    f"   Every <b>{REFERRALS_PER_WITHDRAWAL}</b> referrals triggers an "
    "auto-withdraw from admin wallet.\n"
)

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Verify Telegram joins", callback_data="verify")],
        [InlineKeyboardButton("ℹ️  View X Tasks (info)", callback_data="x_tasks")],
        [InlineKeyboardButton("📬 Submit BSC Address", callback_data="submit_addr")],
        [InlineKeyboardButton("💰 Balance & Withdraw", callback_data="balance")],
        [InlineKeyboardButton("🔗 Referral Link", callback_data="ref")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ])


# ==================== HELPERS ====================

def _dm_only(update: Update) -> bool:
    """Return True if this update is in a private chat; otherwise False."""
    return bool(update.effective_chat and update.effective_chat.type == "private")


async def _ensure_user(session, tg_id: int, username: Optional[str]) -> User:
    user = session.query(User).filter_by(telegram_id=tg_id).one_or_none()
    if not user:
        user = User(telegram_id=tg_id, username=username or "")
        session.add(user)
        session.commit()
    return user


async def _is_member_of(context: ContextTypes.DEFAULT_TYPE, chat_username: str, user_id: int) -> bool:
    # chat_username may be 'srdexchange' (without @)
    if chat_username.startswith("@"):
        chat_username = chat_username[1:]
    try:
        member = await context.bot.get_chat_member(chat_id=f"@{chat_username}", user_id=user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception:
        return False


async def _verify_all_required(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    results = []
    for ch in REQUIRED_CHANNELS:
        ok = await _is_member_of(context, ch, user_id)
        results.append(ok)
        await asyncio.sleep(0.2)  # be gentle with API
    return all(results)


# ==================== HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    session = SessionLocal()
    try:
        user = await _ensure_user(session, update.effective_user.id, update.effective_user.username)
        # handle referral ?start=ref_12345
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
                await q.edit_message_text("✅ All channels joined! Now submit your BSC address.", reply_markup=kb_main())
            else:
                txt = "❌ You haven't joined all channels yet. Please join:\n"
                for ch in REQUIRED_CHANNELS:
                    txt += f"• https://t.me/{ch}\n"
                await q.edit_message_text(txt, reply_markup=kb_main())

       elif data == "x_tasks":
    await q.edit_message_text(
        "Follow on X:\n"
        "• <a href='https://x.com/srdaryandubey'>@srdaryandubey</a>\n"
        "• <a href='https://x.com/srdexchange'>@srdexchange</a>\n\n"
        "Like/Retweet the pinned post and tag 3 friends.\n\n"
        "Note: If You not complete X(Twitter) Task You will be banned soon.",
        parse_mode=ParseMode.HTML,   # 👈 important for clickable links
        reply_markup=kb_main()
    )


        elif data == "submit_addr":
            await q.edit_message_text("Send me your <b>BSC address</b> now:", parse_mode=ParseMode.HTML)

            # next text message from this user is treated as address
            context.user_data["awaiting_bsc"] = True

        elif data == "balance":
            # compute display
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
            link = f"https://t.me/{(await context.bot.get_me()).username}?start=ref_{user.telegram_id}"
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

        # Auto-withdraw same as welcome (example logic; adjust as needed)
        try:
            tx_hash = send_tokens(user.bsc_address, REFERRAL_REWARD_BEAM)
            user.balance_beam = (user.balance_beam or 0) + REFERRAL_REWARD_BEAM
            # reset counter or decrement by threshold depending on your model
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


# ==================== ENTRYPOINT ====================

def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    # --- commands: only in private chats ---
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", help_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd, filters=filters.ChatType.PRIVATE))

    # --- callbacks & messages ---
    app.add_handler(CallbackQueryHandler(button_handler))  # guard inside keeps DMs only
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_text))

    # Ignore channel/group posts entirely
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
