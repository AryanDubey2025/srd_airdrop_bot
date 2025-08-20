import asyncio
from typing import Optional
from urllib.parse import urlparse

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

from config import (
    BOT_TOKEN, REQUIRED_CHANNELS,
    WELCOME_REWARD_BEAM, REFERRAL_REWARD_BEAM, REFERRALS_PER_WITHDRAWAL
)
from db import init_db, SessionLocal, User, Referral, Payout
from web3_utils import is_address, checksum, send_tokens

WELCOME_TEXT = (
    "👋 Welcome to <b>SRD Exchange Airdrop</b>!\n\n"
    "Complete these tasks:\n"
    "1) Join our Telegram channels:\n"
    "   • @srdexchange\n   • @srdexchangeglobal\n   • @srdearning\n"
    "   (The bot will verify.)\n\n"
    "2) X tasks (not verified by bot):\n"
    "   • Follow @srdaryandubey and @srdexchange\n"
    "   • Like, Retweet, and Tag 3 friends on the pinned post of @srdexchange\n\n"
    "3) Submit your <b>BSC address</b> to receive <b>5 $BEAM</b> instantly.\n\n"
    "💎 Referral: Share your link. You earn <b>5 $BEAM</b> per referral.\n"
    "Every <b>3 referrals</b> triggers an <b>auto withdrawal (15 $BEAM)</b> to your wallet.\n"
)

X_TASKS_TEXT = (
    "🐦 <b>X (Twitter) Tasks</b>\n\n"
    "• Follow: @srdaryandubey and @srdexchange\n"
    "• Like, Retweet & Tag 3 friends on the pinned post of @srdexchange\n\n"
    "<i>Bot does not verify X tasks. Complete them manually.</i>"
)

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Verify Telegram Joins", callback_data="verify_joins")],
        [InlineKeyboardButton("🐦 View X Tasks (info)", callback_data="view_x")],
        [InlineKeyboardButton("📬 Submit BSC Address", callback_data="submit_bsc")],
        [InlineKeyboardButton("💰 Balance & Withdraw", callback_data="balance")],
        [InlineKeyboardButton("👥 Referral Link", callback_data="ref")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()  # safe to call multiple times
    session = SessionLocal()
    try:
        tg_id = update.effective_user.id
        # Handle referral payload: /start <referrer_id>
        referred_by = None
        if context.args:
            try:
                payload = context.args[0]
                referrer_tg = int(payload)
                if referrer_tg != tg_id:
                    referred_by = referrer_tg
            except Exception:
                pass

        user = session.query(User).filter_by(tg_user_id=tg_id).one_or_none()
        if user is None:
            user = User(tg_user_id=tg_id, referred_by=referred_by)
            session.add(user)
            session.commit()

            # Record referral if valid and not previously recorded
            if referred_by:
                already = session.query(Referral).filter_by(referee_tg=tg_id).one_or_none()
                if not already:
                    session.add(Referral(referrer_tg=referred_by, referee_tg=tg_id))
                    # increment counters/owed for referrer
                    ref_user = session.query(User).filter_by(tg_user_id=referred_by).one_or_none()
                    if ref_user:
                        ref_user.referrals_count += 1
                        ref_user.owed_beam += REFERRAL_REWARD_BEAM
                    session.commit()
                    # auto-withdraw on each 3rd referral
                    if ref_user and ref_user.bsc_address and (ref_user.referrals_count % REFERRALS_PER_WITHDRAWAL == 0):
                        try:
                            txh = send_tokens(ref_user.bsc_address, REFERRAL_REWARD_BEAM * REFERRALS_PER_WITHDRAWAL)
                            session.add(Payout(tg_user_id=referred_by, tx_hash=txh,
                                               amount_beam=REFERRAL_REWARD_BEAM * REFERRALS_PER_WITHDRAWAL))
                            ref_user.owed_beam -= REFERRAL_REWARD_BEAM * REFERRALS_PER_WITHDRAWAL
                            session.commit()
                            await context.bot.send_message(
                                chat_id=referred_by,
                                text=f"🎉 Auto-withdrawal sent: <b>{REFERRAL_REWARD_BEAM * REFERRALS_PER_WITHDRAWAL} $BEAM</b>\n"
                                     f"Tx: <code>{txh}</code>",
                                parse_mode=ParseMode.HTML
                            )
                        except Exception as e:
                            await context.bot.send_message(chat_id=referred_by, text=f"⚠️ Auto-withdrawal failed: {e}")

        await update.effective_message.reply_text(WELCOME_TEXT, reply_markup=kb_main(), parse_mode=ParseMode.HTML)
        await send_status(update, context)
    finally:
        session.close()

async def send_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    try:
        tg_id = update.effective_user.id
        user = session.query(User).filter_by(tg_user_id=tg_id).one()
        joins = await check_joins(update, context)
        joins_ok = all(joins.values())
        user.joined_verified = joins_ok
        session.commit()

        join_lines = "\n".join([f"• @{ch}: {'✅' if ok else '❌'}" for ch, ok in joins.items()])
        addr = user.bsc_address or "—"
        msg = (
            f"📊 <b>Your Status</b>\n\n"
            f"{join_lines}\n"
            f"Wallet: <code>{addr}</code>\n"
            f"Referrals: {user.referrals_count}\n"
            f"Owed (unpaid): {user.owed_beam} $BEAM\n"
        )
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)
    finally:
        session.close()

async def check_joins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Returns dict[channel] = bool joined?  Bot must be added to each channel."""
    result = {}
    user_id = update.effective_user.id
    for ch in REQUIRED_CHANNELS:
        try:
            member = await context.bot.getChatMember(chat_id=f"@{ch}", user_id=user_id)
            result[ch] = member.status in (
                ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER
            )
        except Exception:
            result[ch] = False
    return result

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "verify_joins":
        joins = await check_joins(update, context)
        join_lines = "\n".join([f"• @{ch}: {'✅' if ok else '❌'}" for ch, ok in joins.items()])
        await q.edit_message_text(f"🔎 Join verification:\n\n{join_lines}", reply_markup=kb_main())
    elif data == "view_x":
        await q.edit_message_text(X_TASKS_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb_main())
    elif data == "submit_bsc":
        await q.edit_message_text("📬 Send your <b>BSC address</b> now.", parse_mode=ParseMode.HTML)
        context.user_data["awaiting_bsc"] = True
    elif data == "balance":
        await send_status(update, context)
    elif data == "ref":
        ref_link = f"https://t.me/{(await context.bot.get_me()).username}?start={update.effective_user.id}"
        await q.edit_message_text(f"👥 Your referral link:\n{ref_link}", reply_markup=kb_main())
    elif data == "help":
        await q.edit_message_text(
            "❓ <b>Help</b>\n\n"
            "• Verify you joined all 3 Telegram channels.\n"
            "• Complete X tasks (not verified by bot).\n"
            "• Submit BSC address to receive 5 $BEAM.\n"
            "• Share referral link; +5 $BEAM per referral. Auto withdrawal every 3 referrals.\n",
            parse_mode=ParseMode.HTML, reply_markup=kb_main()
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_bsc"):
        addr = update.effective_message.text.strip()
        session = SessionLocal()
        try:
            if not is_address(addr):
                await update.message.reply_text("❌ Invalid address. Send a valid BSC address (0x...).")
                return
            addr = checksum(addr)
            # must have joined all channels
            joins_ok = all((await check_joins(update, context)).values())
            if not joins_ok:
                await update.message.reply_text("⚠️ Join all 3 Telegram channels first, then resend your address.")
                return

            tg_id = update.effective_user.id
            user = session.query(User).filter_by(tg_user_id=tg_id).one()
            # Ensure address not previously used by someone else
            exists_addr = session.query(User).filter(User.bsc_address == addr, User.tg_user_id != tg_id).one_or_none()
            if exists_addr:
                await update.message.reply_text("❌ This address is already used by another participant.")
                return

            user.bsc_address = addr
            session.commit()

            # Welcome payout if not yet paid
            if not user.welcomed_paid:
                try:
                    txh = send_tokens(addr, WELCOME_REWARD_BEAM)
                    user.welcomed_paid = True
                    session.add(Payout(tg_user_id=tg_id, tx_hash=txh, amount_beam=WELCOME_REWARD_BEAM))
                    session.commit()
                    await update.message.reply_text(
                        f"🎉 Sent <b>{WELCOME_REWARD_BEAM} $BEAM</b> to {addr}\nTx: <code>{txh}</code>",
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    await update.message.reply_text(f"⚠️ Could not send welcome tokens now: {e}")
            else:
                await update.message.reply_text("✅ Address saved. (Welcome reward already sent earlier.)")

            context.user_data["awaiting_bsc"] = False
        finally:
            session.close()
    else:
        await update.message.reply_text("Use the menu buttons below.", reply_markup=kb_main())

async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual withdrawal of any owed amount (if >= 1 token), otherwise tell them about auto-withdrawals."""
    session = SessionLocal()
    try:
        tg_id = update.effective_user.id
        user = session.query(User).filter_by(tg_user_id=tg_id).one_or_none()
        if not user or not user.bsc_address:
            await update.message.reply_text("📬 Set your BSC address first (Menu → Submit BSC Address).")
            return
        owed = int(user.owed_beam)
        if owed <= 0:
            await update.message.reply_text("You currently have no owed $BEAM. Earn referrals to accumulate rewards.")
            return
        try:
            txh = send_tokens(user.bsc_address, owed)
            session.add(Payout(tg_user_id=tg_id, tx_hash=txh, amount_beam=owed))
            user.owed_beam = 0
            session.commit()
            await update.message.reply_text(
                f"💸 Withdrawal sent: <b>{owed} $BEAM</b>\nTx: <code>{txh}</code>",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            await update.message.reply_text(f"⚠️ Withdrawal failed: {e}")
    finally:
        session.close()

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /start to open the menu.", reply_markup=kb_main())

def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

if update.effective_chat and update.effective_chat.type != "private":
    return

   # --- handlers (DMs only) ---
app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
app.add_handler(CommandHandler("help", help_cmd, filters=filters.ChatType.PRIVATE))
app.add_handler(CommandHandler("withdraw", withdraw_cmd, filters=filters.ChatType.PRIVATE))

# DO NOT pass filters= to CallbackQueryHandler (it doesn't support it)
app.add_handler(CallbackQueryHandler(button_handler))

# Text messages: only private chats
app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_text))

# Only get messages & callback queries; ignore channel/group posts entirely
app.run_polling(
    
    drop_pending_updates=True
)

# Do not receive channel/group posts at all
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    # --- handlers (DMs only) ---
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", help_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd, filters=filters.ChatType.PRIVATE))

    # Callback queries: no filters arg (not supported)
    app.add_handler(CallbackQueryHandler(button_handler))

    # Text messages: only private chats
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_text))

    # Only receive messages & callback queries; ignore channel/group posts entirely
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
