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
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ------------------ local modules ------------------
from config import (
    BOT_TOKEN,
    REQUIRED_CHANNELS,          # list[str]  e.g. ["srdexchange","srdexchangeglobal","srdearning"] or ["-1001780887211", ...]
    WELCOME_REWARD_BEAM,        # int, e.g. 1
    REFERRAL_REWARD_BEAM,       # int
    REFERRALS_PER_WITHDRAWAL,   # int
)
from db import init_db, SessionLocal, User, Referral, Payout
from web3_utils import is_address, checksum, send_tokens

# ========================= logging =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("srd_airdrop_bot")


# ========================= UI TEXT =========================

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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Verify Telegram joins", callback_data="verify")],
        [InlineKeyboardButton("🗒️ View X Tasks (info)", callback_data="x_tasks")],
        [InlineKeyboardButton("📬 Submit BSC Address", callback_data="submit_addr")],
        [InlineKeyboardButton("💰 Balance & Withdraw", callback_data="balance")],
        [InlineKeyboardButton("🔗 Referral Link", callback_data="ref")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ])


# ========================= helpers =========================

def _dm_only(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")


async def _ensure_user(session, tg_id: int, username: Optional[str]) -> User:
    user = session.query(User).filter_by(telegram_id=tg_id).one_or_none()
    if not user:
        user = User(telegram_id=tg_id, username=username or "")
        session.add(user)
        session.commit()
    return user


async def _is_member_of(context: ContextTypes.DEFAULT_TYPE, chat_key: str, user_id: int) -> bool:
    """
    Accepts either @username or numeric -100... chat id.
    Returns True if user is member/admin/creator.
    """
    try:
        # resolve chat
        if str(chat_key).startswith("-100"):
            chat_id = int(chat_key)
            chat = await context.bot.get_chat(chat_id)
        else:
            uname = str(chat_key).strip().lstrip("@")
            chat = await context.bot.get_chat(f"@{uname}")

        member = await context.bot.get_chat_member(chat.id, user_id)
        ok = member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
        logger.info("verify %s → %s (%s)", chat_key, member.status, "OK" if ok else "NO")
        return ok
    except Exception as e:
        logger.warning("verify fail for %s: %s", chat_key, e)
        return False


async def _verify_all_required(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    all_ok = True
    for ch in REQUIRED_CHANNELS:
        if not await _is_member_of(context, ch, user_id):
            all_ok = False
        await asyncio.sleep(0.25)
    return all_ok


async def safe_edit_message(query, text: str, **kwargs):
    """
    Edit message safely; ignore 'message is not modified'.
    """
    try:
        if query.message and getattr(query.message, "text", None) == text:
            return
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        raise


# ========================= handlers =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _dm_only(update):
        return
    session = SessionLocal()
    try:
        user = await _ensure_user(session, update.effective_user.id, update.effective_user.username)

        # handle referral payload /start ref_12345
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
                await safe_edit_message(q, "✅ All channels joined! Now submit your BSC address.", reply_markup=kb_main())
            else:
                txt = "❌ You haven't joined all channels yet. Please join:\n"
                for ch in REQUIRED_CHANNELS:
                    if str(ch).startswith("-100"):
                        txt += f"• (private/ID) {ch}\n"
                    else:
                        txt += f"• https://t.me/{str(ch).lstrip('@')}\n"
                await safe_edit_message(q, txt, reply_markup=kb_main())

        elif data == "x_tasks":
            html = (
                'Follow on X:\n'
                '• <a href="https://x.com/srdaryandubey">@srdaryandubey</a>\n'
                '• <a href="https://x.com/srdexchange">@srdexchange</a>\n\n'
                'Like/Retweet the pinned post and tag 3 friends.\n\n'
                '<i>Note: The bot does NOT verify X tasks.</i>'
            )
            await safe_edit_message(q, html, parse_mode=ParseMode.HTML, reply_markup=kb_main())

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

        # expecting BSC address?
        if context.user_data.get("awaiting_bsc"):
            addr = (update.message.text or "").strip()
            if not is_address(addr):
                await update.message.reply_text("❌ That doesn't look like a valid BSC address. Try again.")
                return

            addr = checksum(addr)
            user.bsc_address = addr
            session.commit()

            ok = await _verify_all_required(context, user.telegram_id)
            if not ok:
                await update.message.reply_text(
                    "You must join all channels first. Tap 'Verify Telegram joins' again.",
                    reply_markup=kb_main()
                )
                return

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


# ========================= debug commands =========================

async def checkverify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Debug membership check with status/errors per channel.
    """
    if not _dm_only(update):
        return

    res = []
    for ch in REQUIRED_CHANNELS:
        try:
            # resolve chat first
            if str(ch).startswith("-100"):
                chat = await context.bot.get_chat(int(ch))
                member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
                status = member.status
                title = chat.title or ""
                uname = f"@{chat.username}" if getattr(chat, "username", None) else "(no username)"
                ok = status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
                res.append(f"{'✅' if ok else '❌'} {chat.id}: {title} {uname} → {status}")
            else:
                uname = str(ch).strip().lstrip("@")
                chat = await context.bot.get_chat(f"@{uname}")
                member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
                status = member.status
                ok = status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
                res.append(f"{'✅' if ok else '❌'} @{uname} → {chat.id} → {status}")
        except Exception as e:
            res.append(f"❌ {ch} → ERROR: {e}")
        await asyncio.sleep(0.2)

    await update.message.reply_text("\n".join(res))


async def channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Show how REQUIRED_CHANNELS resolve to IDs/titles.
    """
    out = []
    for ch in REQUIRED_CHANNELS:
        try:
            if str(ch).startswith("-100"):
                chat = await context.bot.get_chat(int(ch))
                handle = f"@{chat.username}" if getattr(chat, "username", None) else "(no username)"
                out.append(f"✅ {chat.id} → {chat.title} {handle}")
            else:
                uname = str(ch).strip().lstrip("@")
                chat = await context.bot.get_chat(f"@{uname}")
                out.append(f"✅ @{uname} → {chat.id} ({chat.title})")
        except Exception as e:
            out.append(f"❌ {ch} → ERROR: {e}")
        await asyncio.sleep(0.2)

    await update.message.reply_text("\n".join(out) if out else "No REQUIRED_CHANNELS configured.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)


# ========================= entrypoint =========================

def main():
    init_db()
    application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    # commands (DM only)
    application.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("help", help_cmd, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("withdraw", withdraw_cmd, filters=filters.ChatType.PRIVATE))

    # debug commands
    application.add_handler(CommandHandler("checkverify", checkverify, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("channels", channels_cmd, filters=filters.ChatType.PRIVATE))

    # callbacks & text (DM only)
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_text))

    application.add_error_handler(error_handler)

    application.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()

