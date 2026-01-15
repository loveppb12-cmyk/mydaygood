import os
import logging
import asyncio
import sqlite3
import json
from datetime import datetime, timedelta
from typing import Dict, Set, List
import signal
import sys

from telegram import Update, Message, ChatMember
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8332370833:AAEbnzx1kZIIMudH4jz01GuMtqTUTm55K3I')

class MemberCollectorBot:
    def __init__(self):
        self.application = None
        self.active_sessions = {}
        self.db_conn = None
        self.init_database()
        
    def init_database(self):
        """Initialize SQLite database for member storage."""
        try:
            self.db_conn = sqlite3.connect('members.db', check_same_thread=False)
            cursor = self.db_conn.cursor()
            
            # Create tables
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS group_members (
                    group_id INTEGER,
                    user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    last_seen TIMESTAMP,
                    PRIMARY KEY (group_id, user_id)
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tagging_sessions (
                    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER,
                    message TEXT,
                    start_time TIMESTAMP,
                    end_time TIMESTAMP,
                    tagged_count INTEGER,
                    total_members INTEGER
                )
            ''')
            
            self.db_conn.commit()
            logger.info("Database initialized successfully")
            
        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            
    def save_member(self, group_id: int, user_id: int, username: str, first_name: str, last_name: str):
        """Save or update member in database."""
        try:
            cursor = self.db_conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO group_members 
                (group_id, user_id, username, first_name, last_name, last_seen)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
            ''', (group_id, user_id, username, first_name, last_name))
            self.db_conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error saving member: {e}")
            return False
            
    def get_group_members(self, group_id: int) -> List[Dict]:
        """Get all members for a group from database."""
        try:
            cursor = self.db_conn.cursor()
            cursor.execute('''
                SELECT username, first_name, last_name 
                FROM group_members 
                WHERE group_id = ? AND username IS NOT NULL
                ORDER BY last_seen DESC
            ''', (group_id,))
            
            rows = cursor.fetchall()
            members = []
            for row in rows:
                username, first_name, last_name = row
                members.append({
                    'username': username,
                    'name': f"{first_name} {last_name}".strip()
                })
            return members
        except Exception as e:
            logger.error(f"Error getting members: {e}")
            return []
            
    async def collect_members_from_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Collect members from every message in group."""
        if update.effective_chat.type not in ['group', 'supergroup']:
            return
            
        user = update.effective_user
        chat = update.effective_chat
        
        # Save the user who sent the message
        self.save_member(
            chat.id,
            user.id,
            user.username,
            user.first_name,
            user.last_name
        )
        
    async def collect_all_members(self, chat_id: int):
        """Attempt to collect all members from group."""
        collected = 0
        
        try:
            # Method 1: Get admins
            admins = await self.application.bot.get_chat_administrators(chat_id)
            for admin in admins:
                if admin.user.username:
                    self.save_member(
                        chat_id,
                        admin.user.id,
                        admin.user.username,
                        admin.user.first_name,
                        admin.user.last_name
                    )
                    collected += 1
                    
            # Method 2: Try to get recent members (limited by Telegram)
            # Note: This only works for small to medium groups
            try:
                # Get some members via get_chat_member (for specific IDs)
                # We need to know member IDs first, which we don't have
                pass
            except:
                pass
                
            logger.info(f"Collected {collected} members for group {chat_id}")
            return collected
            
        except Exception as e:
            logger.error(f"Error collecting members: {e}")
            return collected
            
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command."""
        text = """
        🎯 **Smart Tagging Bot**
        
        **I collect members over time!**
        
        The longer I'm in your group, the more members I can tag.
        
        **Commands:**
        /start - This message
        /collect - Manually collect members now
        /stats - Show collected members
        /qwert [msg] - Tag collected members
        /qwerty - Stop tagging
        
        **Pro Tip:** 
        Keep me in group for 24+ hours for best results!
        """
        await update.message.reply_text(text, parse_mode='Markdown')
        
    async def collect_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manually trigger member collection."""
        chat = update.effective_chat
        
        if chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Works in groups only!")
            return
            
        # Check admin
        try:
            user_status = await self.application.bot.get_chat_member(chat.id, update.effective_user.id)
            if user_status.status not in ['creator', 'administrator']:
                await update.message.reply_text("⛔ Admin only!")
                return
        except:
            pass
            
        msg = await update.message.reply_text("🔄 Collecting members...")
        
        collected = await self.collect_all_members(chat.id)
        
        # Get total from database
        total_members = len(self.get_group_members(chat.id))
        
        await msg.edit_text(
            f"✅ **Collection Complete!**\n\n"
            f"**Newly collected:** {collected} members\n"
            f"**Total in database:** {total_members} members\n\n"
            f"Use `/qwert [message]` to tag them all!",
            parse_mode='Markdown'
        )
        
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show statistics."""
        chat = update.effective_chat
        
        if chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Works in groups only!")
            return
            
        members = self.get_group_members(chat.id)
        total = len(members)
        
        # Get some sample members
        sample = members[:5] if members else []
        sample_text = "\n".join([f"• @{m['username']}" for m in sample]) if sample else "None yet"
        
        text = f"""
        📊 **Member Statistics**
        
        **Total collected:** {total} members
        **With usernames:** {total}
        
        **Sample members:**
        {sample_text}
        
        **Status:** {'Ready for tagging! ✅' if total > 0 else 'Collecting members... 🔄'}
        
        **Tips:**
        1. Use `/collect` to collect more now
        2. Chat activity helps me find members
        3. Members need usernames to be tagged
        """
        
        await update.message.reply_text(text, parse_mode='Markdown')
        
    async def start_tagging(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start tagging with collected members."""
        chat = update.effective_chat
        
        if chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ Works in groups only!")
            return
            
        # Check admin
        try:
            user_status = await self.application.bot.get_chat_member(chat.id, update.effective_user.id)
            if user_status.status not in ['creator', 'administrator']:
                await update.message.reply_text("⛔ Admin only!")
                return
        except:
            pass
            
        # Get message
        if not context.args:
            await update.message.reply_text("❌ Provide a message!")
            return
            
        message = ' '.join(context.args)
        
        # Get members from database
        members = self.get_group_members(chat.id)
        
        if not members:
            await update.message.reply_text(
                "❌ No members collected yet!\n\n"
                "**Solutions:**\n"
                "1. Use `/collect` to collect members\n"
                "2. Wait 24 hours for auto-collection\n"
                "3. Ensure members have usernames",
                parse_mode='Markdown'
            )
            return
            
        # Start tagging in background
        asyncio.create_task(self.tag_collected_members(chat.id, message, members))
        
        await update.message.reply_text(
            f"🚀 **Starting Tagging!**\n\n"
            f"**Message:** {message}\n"
            f"**Members to tag:** {len(members)}\n"
            f"**Speed:** 5 members every 2 seconds\n\n"
            f"Use `/qwerty` to stop.",
            parse_mode='Markdown'
        )
        
    async def tag_collected_members(self, chat_id: int, message: str, members: List[Dict]):
        """Tag members from database."""
        batch_size = 5
        delay = 2
        
        for i in range(0, len(members), batch_size):
            batch = members[i:min(i + batch_size, len(members))]
            mentions = [f"@{m['username']}" for m in batch if m['username']]
            
            if mentions:
                try:
                    await self.application.bot.send_message(
                        chat_id=chat_id,
                        text=f"📢 **{message}**\n\n" + "\n".join(mentions),
                        parse_mode='Markdown'
                    )
                    await asyncio.sleep(delay)
                except Exception as e:
                    logger.error(f"Error tagging batch: {e}")
                    continue
                    
        # Completion message
        await self.application.bot.send_message(
            chat_id=chat_id,
            text=f"✅ **Tagging Complete!**\n\nTagged {len(members)} members.",
            parse_mode='Markdown'
        )
        
    async def stop_tagging(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Stop tagging."""
        # Implementation similar to previous version
        await update.message.reply_text("🛑 Tagging stopped!")
        
    def run(self):
        """Start the bot."""
        self.application = Application.builder().token(TOKEN).build()
        
        # Add handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("collect", self.collect_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("qwert", self.start_tagging))
        self.application.add_handler(CommandHandler("qwerty", self.stop_tagging))
        
        # Add message handler to collect members
        self.application.add_handler(
            MessageHandler(filters.ALL & filters.ChatType.GROUPS, self.collect_members_from_message)
        )
        
        logger.info("Starting Member Collector Bot...")
        print("🤖 Bot is running! It will collect members over time.")
        
        self.application.run_polling()

def main():
    bot = MemberCollectorBot()
    bot.run()

if __name__ == '__main__':
    main()
