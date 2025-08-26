import os
import re
import logging
import random
import string
import asyncio
from datetime import datetime

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone
import dotenv
import json


# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Google Sheets
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
BOT_TOKEN = os.getenv("BOT_TOKEN")

credentials_json = os.getenv("GOOGLE_CREDENTIALS")
CREDS = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(credentials_json), SCOPE)
CLIENT = gspread.authorize(CREDS)
SHEET_USERS = CLIENT.open_by_key(SPREADSHEET_ID).worksheet("Users")
SHEET_EXPENSES = CLIENT.open_by_key(SPREADSHEET_ID).worksheet("Expenses")
SHEET_SPLITS = CLIENT.open_by_key(SPREADSHEET_ID).worksheet("Splits")

BOT_TOKEN = "7492423599:AAGFo_h7t0_P7o5cw9BtZ26pyfFNFKQq09s"

async def back(update, context):
    context.user_data.clear()
    await update.message.reply_text("🔙 Okay, cancelled. You're back at the main menu.")
    return ConversationHandler.END


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = (update.message.from_user.username or "").strip().lower()
    chat_id = update.message.chat_id

    # Greet the user
    await update.message.reply_text(
        f"👋 Hello @{username}!\nYour chat ID is: `{chat_id}`\n\n"
        "Use /register to join the expense split system.",
        parse_mode="Markdown"
    )

    # Optional: auto-register if not already in sheet
    try:
        users = await asyncio.to_thread(SHEET_USERS.get_all_records)
        if not any(u["username"].lower() == username for u in users):
            await asyncio.to_thread(SHEET_USERS.append_row, [username, chat_id, "TRUE"])
            await update.message.reply_text("✅ You've been auto-registered.")
    except Exception as e:
        logger.error(f"Error in /start auto-registration: {e}", exc_info=True)


async def register(update, context):
    try:
        username = (update.message.from_user.username or "").strip().lower()
        chat_id = update.message.chat_id

        if not username:
            await update.message.reply_text("❌ You must set a Telegram username to register.")
            return

        users = await asyncio.to_thread(SHEET_USERS.get_all_records)
        if any(u["username"].lower() == username for u in users):
            await update.message.reply_text(f"👋 You're already registered as @{username}.")
            return

        await asyncio.to_thread(SHEET_USERS.append_row, [username, chat_id, "TRUE"])
        await update.message.reply_text(f"✅ Registered successfully!\nUsername: @{username}\nChat ID: `{chat_id}`", parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error in /register: {e}", exc_info=True)
        await update.message.reply_text("❌ Something went wrong. Please try again later.")

def generate_id(prefix):
    return f"{prefix}-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

async def get_active_users():
    users = await asyncio.to_thread(SHEET_USERS.get_all_records)
    return [u for u in users if u["active"] == "TRUE" and u.get("chat_id")]

def build_user_buttons(users):
    buttons = [[InlineKeyboardButton(f"@{u['username']}", callback_data=u["username"])] for u in users]
    return InlineKeyboardMarkup(buttons)

def build_status_board(expense_id):
    splits = SHEET_SPLITS.get_all_records()
    relevant = [s for s in splits if s["expense_id"] == expense_id]
    lines = []
    for s in relevant:
        status = "✅" if s["status"] == "PAID" else "❌"
        lines.append(f"{status} @{s['participant_username']}")
    return "\n".join(lines)


SELECTING, ENTERING_AMOUNT = range(2)

async def start_add(update, context):
    context.user_data["selected_users"] = []
    context.user_data["all_users"] = await asyncio.to_thread(SHEET_USERS.get_all_records)

    return await show_selection_menu(update, context)

async def show_selection_menu(update_or_query, context):
    selected = context.user_data.get("selected_users", [])
    all_users = context.user_data.get("all_users", [])

    buttons = []
    for u in all_users:
        if u["active"] == "TRUE":
            uname = u["username"]
            label = f"✅ @{uname}" if uname in selected else f"@{uname}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"SELECT:{uname}")])

    buttons.append([InlineKeyboardButton("✅ Done", callback_data="DONE")])
    markup = InlineKeyboardMarkup(buttons)

    text = "Select participants:\n" + (", ".join(f"@{u}" for u in selected) or "None selected")

    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text, reply_markup=markup)
    else:
        await update_or_query.edit_message_text(text, reply_markup=markup)

    return SELECTING

async def select_user(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("SELECT:"):
        uname = data.split(":")[1]
        selected = context.user_data.get("selected_users", [])
        if uname in selected:
            selected.remove(uname)
        else:
            selected.append(uname)
        context.user_data["selected_users"] = selected
        return await show_selection_menu(query, context)

    elif data == "DONE":
        if not context.user_data.get("selected_users"):
            await query.edit_message_text("❌ You must select at least one participant.")
            return SELECTING
        await query.edit_message_text("Great! Now send the total amount.")
        return ENTERING_AMOUNT



async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        total = float(update.message.text.strip())
        usernames = context.user_data["selected_users"]
        payer_username = update.message.from_user.username.lower()
        payer_chat_id = update.message.chat_id
        timestamp = datetime.utcnow().isoformat()
        expense_id = generate_id("EXP")
        per_share = round(total / len(usernames), 2)

        # Save expense
        expense_row = [expense_id, timestamp[:10], payer_username, payer_chat_id,
                       total, ','.join(usernames), per_share, timestamp]
        await asyncio.to_thread(SHEET_EXPENSES.append_row, expense_row)

        # Save splits and notify
        users = await get_active_users()
        for u in users:
            if u["username"] in usernames:
                split_id = generate_id("SPL")
                split_row = [split_id, expense_id, u["username"], u["chat_id"],
                             per_share, "PENDING", "", timestamp]
                await asyncio.to_thread(SHEET_SPLITS.append_row, split_row)

                button = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Mark as Paid", callback_data=f"PAID:{split_id}")]
                ])
                await context.bot.send_message(
                    chat_id=u["chat_id"],
                    text=f"🍱 Expense: {expense_id}\nAmount: {per_share}",
                    reply_markup=button
                )

        await update.message.reply_text(
            f"✅ Expense recorded\nTotal: {total}\nEach owes: {per_share}"
        )
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error in enter_amount: {e}")
        await update.message.reply_text("Invalid amount. Try again.")
        return ENTERING_AMOUNT
    
    
    
    
async def mark_paid(update, context):
    query = update.callback_query
    await query.answer()
    split_id = query.data.split(":")[1]
    timestamp = datetime.utcnow().isoformat()

    all_rows = await asyncio.to_thread(SHEET_SPLITS.get_all_values)
    header = all_rows[0]
    for i, row in enumerate(all_rows[1:], 1):
        if row[header.index("split_id")] == split_id:
            await asyncio.to_thread(SHEET_SPLITS.update_cell, i+1, header.index("status")+1, "PAID")
            await asyncio.to_thread(SHEET_SPLITS.update_cell, i+1, header.index("settled_at")+1, timestamp)
            expense_id = row[header.index("expense_id")]
            splits = await asyncio.to_thread(SHEET_SPLITS.get_all_records)
            board = "\n".join(
                f"{'✅' if s['status']=='PAID' else '❌'} @{s['participant_username']}"
                for s in splits if s["expense_id"] == expense_id
            )
            await query.edit_message_text(f"✅ Marked as paid.\n\nSplit Status:\n{board}")
            break 
        

async def daily_reminder(app):
    splits = await asyncio.to_thread(SHEET_SPLITS.get_all_records)
    pending = {}
    for s in splits:
        if s["status"] == "PENDING":
            pending.setdefault(s["participant_chat_id"], []).append(s)

    for chat_id, items in pending.items():
        for s in items:
            board = build_status_board(s["expense_id"])
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ Reminder: You still owe for {s['expense_id']}\n\nSplit Status:\n{board}"
            )
            
def main():
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except:
        pass

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", start_add)],
        states={
            SELECTING: [CallbackQueryHandler(select_user)],
            ENTERING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount)],
        },
            fallbacks=[CommandHandler("back", back)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(mark_paid, pattern=r"^PAID:"))
    app.add_handler(CommandHandler("back", back))

    scheduler = AsyncIOScheduler(timezone=timezone("Asia/Colombo"))
    scheduler.add_job(daily_reminder, "cron", hour=20, minute=0, args=[app])
    scheduler.start()

    app.run_polling()

if __name__ == "__main__":
    main() 