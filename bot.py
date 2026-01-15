import os
import logging
import asyncio
import json
from datetime import datetime
from typing import Dict, Set, List
from collections import defaultdict
import signal
import sys

from telegram import Update, User
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackContext
)
from telegram.error import (
    TelegramError, 
    BadRequest, 
    Forbidden, 
    RetryAfter
)

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8332370833:AAEbnzx1kZIIMudH4jz01GuMtqTUTm55K3I')

# Rate limiting constants
MESSAGES_PER_SECOND = 1
MEMBERS_PER_MESSAGE = 5
DELAY_BETWEEN_MESSAGES = 5
MAX_TAGGING_TIME = 300

# Store active tagging sessions
active_sessions = {}
user_cooldowns = {}

class TaggingSession:
    def __init__(self, chat_id: int, admin_id: int, message: str):
        self.chat_id = chat_id
        self.admin_id = admin_id
        self.message = message
        self.start_time = datetime.now()
        self.tagged_count = 0
        self.total_members = 0
        self.is_active = True
        self.task = None
        self.last_update = datetime.now()
        self.members_list = []

class TaggerBot:
    def __init__(self):
        self.application = None
        self.cleanup_task = None
        
    async def is_user_admin(self, chat_id: int, user_id: int) -> bool:
        """Check if user is admin in the chat."""
        try:
            chat_member = await self.application.bot.get_chat_member(chat_id, user_id)
            return chat_member.status in ['creator', 'administrator']
        except Exception as e:
            logger.error(f"Error checking admin status: {e}")
            return False
            
    async def is_user_in_cooldown(self, user_id: int) -> bool:
        """Check if user is in command cooldown."""
        if user_id not in user_cooldowns:
            return False
            
        last_time = user_cooldowns[user_id]
        cooldown_seconds = 10
        
        if (datetime.now() - last_time).seconds < cooldown_seconds:
            return True
        return False
        
    async def update_cooldown(self, user_id: int):
        """Update user's last command time."""
        user_cooldowns[user_id] = datetime.now()
        
    async def get_chat_members(self, chat_id: int) -> List[User]:
        """Get all chat members with usernames."""
        members = []
        try:
            # Get chat members count first
            chat = await self.application.bot.get_chat(chat_id)
            
            # For large groups, we'll use a different approach
            # Get administrators first (they're usually active)
            admins = await self.application.bot.get_chat_administrators(chat_id)
            
            # Add admins with usernames
            for admin in admins:
                if not admin.user.is_bot and admin.user.username:
                    members.append(admin.user)
            
            # Note: Getting all members in large groups (50k+) is challenging
            # due to Telegram API limitations
            # We'll work with the members we can get
            
        except Exception as e:
            logger.error(f"Error getting chat members: {e}")
            
        return members
        
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a message when the command /start is issued."""
        user = update.effective_user
        
        welcome_text = """
        🤖 **Welcome to Universal Tagging Bot!**
        
        I can help group admins tag members with important announcements.
        
        **Available Commands:**
        /start - Show this welcome message
        /help - Detailed help information
        /qwert [message] - Start tagging members with your message
        /qwerty - Stop ongoing tagging process
        /status - Check current tagging status
        
        **Important Notes:**
        • Only group admins can use tagging commands
        • Tags admins and active members
        • Use responsibly to avoid spam
        
        **Example:**
        `/qwert Important announcement: Meeting at 5 PM`
        """
        
        await update.message.reply_text(welcome_text, parse_mode='Markdown')
        
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a message when the command /help is issued."""
        help_text = """
        📚 **Tagging Bot Help Guide**
        
        **How to use:**
        1. Add me to your group
        2. Make sure I have admin permissions
        3. Use commands below (admin only)
        
        **Admin Commands:**
        `/qwert [your message]` - Start tagging members
        `/qwerty` - Stop ongoing tagging
        `/status` - Check tagging progress
        
        **Parameters:**
        • Members per batch: 5
        • Delay between messages: 5 seconds
        • Max session time: 5 minutes
        
        **Note:** Due to Telegram API limitations, 
        the bot tags administrators and active members.
        """
        
        await update.message.reply_text(help_text, parse_mode='Markdown')
        
    async def start_tagging(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start tagging process - admin only."""
        chat = update.effective_chat
        user = update.effective_user
        
        # Check if in group
        if chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ This command only works in groups!")
            return
            
        # Check cooldown
        if await self.is_user_in_cooldown(user.id):
            await update.message.reply_text("⏳ Please wait 10 seconds between commands!")
            return
            
        # Check if user is admin
        if not await self.is_user_admin(chat.id, user.id):
            await update.message.reply_text(
                "⛔ **Access Denied!**\n"
                "Only group administrators can use this command.",
                parse_mode='Markdown'
            )
            return
            
        # Check if bot is admin
        try:
            bot_member = await self.application.bot.get_chat_member(chat.id, self.application.bot.id)
            if bot_member.status not in ['administrator', 'creator']:
                await update.message.reply_text(
                    "⚠️ **Bot Needs Admin Permissions!**\n\n"
                    "Please promote me to administrator with the following permissions:\n"
                    "• Send Messages\n"
                    "• Mention Users",
                    parse_mode='Markdown'
                )
                return
        except Exception as e:
            logger.error(f"Error checking bot admin status: {e}")
            
        # Check if already tagging in this chat
        if chat.id in active_sessions and active_sessions[chat.id].is_active:
            await update.message.reply_text(
                "⚠️ **Tagging Already Active!**\n"
                "Use `/qwerty` to stop current session first.",
                parse_mode='Markdown'
            )
            return
            
        # Get message from command
        if not context.args:
            await update.message.reply_text(
                "❌ **Please provide a message!**\n\n"
                "Example:\n"
                "`/qwert Important announcement for all members`",
                parse_mode='Markdown'
            )
            return
            
        tag_message = ' '.join(context.args)
        
        # Validate message length
        if len(tag_message) > 200:
            await update.message.reply_text("❌ Message too long! Maximum 200 characters.")
            return
            
        # Update cooldown
        await self.update_cooldown(user.id)
        
        # Create new session
        session = TaggingSession(chat.id, user.id, tag_message)
        
        # Get chat members
        session.members_list = await self.get_chat_members(chat.id)
        session.total_members = len(session.members_list)
        
        if session.total_members == 0:
            await update.message.reply_text(
                "❌ No members with usernames found to tag!\n"
                "Make sure members have usernames set in their profiles."
            )
            return
            
        # Send confirmation
        confirm_msg = await update.message.reply_text(
            f"🚀 **Starting Tagging Session**\n\n"
            f"**Message:** {tag_message}\n"
            f"**Members to tag:** {session.total_members}\n"
            f"**Started by:** {user.mention_html()}\n"
            f"**Status:** Starting...\n\n"
            f"Use `/qwerty` to stop at any time.",
            parse_mode='HTML'
        )
        
        # Start tagging task
        task = asyncio.create_task(
            self.execute_tagging(session, confirm_msg.message_id)
        )
        session.task = task
        active_sessions[chat.id] = session
        
    async def execute_tagging(self, session: TaggingSession, status_message_id: int):
        """Execute the tagging process."""
        try:
            batch_size = MEMBERS_PER_MESSAGE
            
            for i in range(0, session.total_members, batch_size):
                # Check if session is still active
                if not session.is_active or session.chat_id not in active_sessions:
                    break
                    
                # Check time limit
                if (datetime.now() - session.start_time).seconds > MAX_TAGGING_TIME:
                    await self.application.bot.send_message(
                        chat_id=session.chat_id,
                        text="⏰ **Time Limit Reached!**\n"
                             "Maximum tagging time (5 minutes) exceeded.",
                        parse_mode='Markdown'
                    )
                    break
                    
                batch = session.members_list[i:min(i + batch_size, session.total_members)]
                
                # Create mentions
                mentions = []
                for member in batch:
                    if member.username:
                        mentions.append(f"@{member.username}")
                        
                if not mentions:
                    continue
                    
                # Send tagged message
                message_text = f"📢 **{session.message}**\n\n" + "\n".join(mentions)
                
                try:
                    await self.application.bot.send_message(
                        chat_id=session.chat_id,
                        text=message_text,
                        parse_mode='Markdown'
                    )
                    
                    session.tagged_count += len(batch)
                    session.last_update = datetime.now()
                    
                    # Update progress
                    progress = (session.tagged_count / session.total_members) * 100
                    try:
                        await self.application.bot.edit_message_text(
                            chat_id=session.chat_id,
                            message_id=status_message_id,
                            text=f"🚀 **Tagging In Progress**\n\n"
                                 f"**Message:** {session.message}\n"
                                 f"**Progress:** {session.tagged_count}/{session.total_members} ({progress:.1f}%)\n"
                                 f"**Status:** Tagging...",
                            parse_mode='Markdown'
                        )
                    except:
                        pass
                        
                    # Delay between messages
                    await asyncio.sleep(DELAY_BETWEEN_MESSAGES)
                    
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                    continue
                except (BadRequest, Forbidden) as e:
                    logger.error(f"Error sending message: {e}")
                    continue
                    
            # Completion
            if session.is_active and session.chat_id in active_sessions:
                await self.application.bot.send_message(
                    chat_id=session.chat_id,
                    text=f"✅ **Tagging Completed!**\n\n"
                         f"**Message:** {session.message}\n"
                         f"**Tagged:** {session.tagged_count} members\n"
                         f"**Total:** {session.total_members} members\n"
                         f"**Duration:** {(datetime.now() - session.start_time).seconds} seconds",
                    parse_mode='Markdown'
                )
                
                # Clean up
                if session.chat_id in active_sessions:
                    del active_sessions[session.chat_id]
                    
        except Exception as e:
            logger.error(f"Error in execute_tagging: {e}")
            
            # Send error message
            try:
                await self.application.bot.send_message(
                    chat_id=session.chat_id,
                    text=f"❌ **Tagging Error**\n\n"
                         f"An error occurred during tagging.\n"
                         f"Please try again later.",
                    parse_mode='Markdown'
                )
            except:
                pass
                
            # Clean up on error
            if session.chat_id in active_sessions:
                del active_sessions[session.chat_id]
                
    async def stop_tagging(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Stop tagging process."""
        chat = update.effective_chat
        user = update.effective_user
        
        # Check cooldown
        if await self.is_user_in_cooldown(user.id):
            await update.message.reply_text("⏳ Please wait 10 seconds between commands!")
            return
            
        if chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ This command only works in groups!")
            return
            
        if chat.id not in active_sessions:
            await update.message.reply_text("ℹ️ No active tagging session in this group.")
            return
            
        session = active_sessions[chat.id]
        
        # Check if user is admin or the one who started it
        if not await self.is_user_admin(chat.id, user.id) and user.id != session.admin_id:
            await update.message.reply_text("⛔ Only admins or session starter can stop tagging!")
            return
            
        # Stop the session
        session.is_active = False
        if session.task and not session.task.done():
            session.task.cancel()
            
        # Cleanup
        if chat.id in active_sessions:
            del active_sessions[chat.id]
            
        await update.message.reply_text(
            f"🛑 **Tagging Stopped**\n\n"
            f"**Tagged:** {session.tagged_count} members\n"
            f"**Duration:** {(datetime.now() - session.start_time).seconds} seconds\n"
            f"**Stopped by:** {user.mention_html()}",
            parse_mode='HTML'
        )
        
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check tagging status."""
        chat = update.effective_chat
        
        if chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ This command only works in groups!")
            return
            
        if chat.id in active_sessions:
            session = active_sessions[chat.id]
            elapsed = datetime.now() - session.start_time
            progress = (session.tagged_count / max(1, session.total_members)) * 100
            
            status_text = f"""
            📊 **Active Tagging Session**
            
            **Message:** {session.message}
            **Started:** {session.start_time.strftime('%H:%M:%S')}
            **Duration:** {elapsed.seconds} seconds
            **Tagged:** {session.tagged_count}/{session.total_members} members
            **Progress:** {progress:.1f}%
            **Status:** {'Active ✅' if session.is_active else 'Stopped ⏹️'}
            """
        else:
            status_text = "ℹ️ **No active tagging session in this group.**"
            
        await update.message.reply_text(status_text, parse_mode='HTML')
        
    async def cleanup_inactive_sessions(self):
        """Clean up inactive sessions periodically."""
        while True:
            try:
                await asyncio.sleep(60)
                
                current_time = datetime.now()
                inactive_chats = []
                
                for chat_id, session in list(active_sessions.items()):
                    if (current_time - session.last_update).seconds > 600:
                        inactive_chats.append(chat_id)
                        
                for chat_id in inactive_chats:
                    if chat_id in active_sessions:
                        session = active_sessions[chat_id]
                        session.is_active = False
                        if session.task and not session.task.done():
                            session.task.cancel()
                        del active_sessions[chat_id]
                        
            except Exception as e:
                logger.error(f"Error in cleanup_inactive_sessions: {e}")
                await asyncio.sleep(60)
                
    async def on_startup(self, application: Application):
        """Run on bot startup."""
        self.cleanup_task = asyncio.create_task(self.cleanup_inactive_sessions())
        logger.info("Cleanup task started")
        
    async def on_shutdown(self, application: Application):
        """Run on bot shutdown."""
        for chat_id, session in list(active_sessions.items()):
            session.is_active = False
            if session.task and not session.task.done():
                session.task.cancel()
        
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            
        logger.info("Bot shutdown completed")
        
    def run(self):
        """Start the bot."""
        self.application = (
            Application.builder()
            .token(TOKEN)
            .post_init(self.on_startup)
            .post_shutdown(self.on_shutdown)
            .build()
        )
        
        # Add command handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("qwert", self.start_tagging))
        self.application.add_handler(CommandHandler("qwerty", self.stop_tagging))
        self.application.add_handler(CommandHandler("status", self.status_command))
        
        # Start the bot
        logger.info("Starting Universal Tagging Bot...")
        
        # Security warning
        if TOKEN == "8332370833:AAEbnzx1kZIIMudH4jz01GuMtqTUTm55K3I":
            print("=" * 60)
            print("⚠️  WARNING: Using public token!")
            print("Get new token from @BotFather and set TELEGRAM_BOT_TOKEN")
            print("=" * 60)
        
        print("Bot is running! Commands available:")
        print("/start - Welcome message")
        print("/help - Help guide")
        print("/qwert [message] - Start tagging")
        print("/qwerty - Stop tagging")
        print("/status - Check status")
        
        self.application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )

def main():
    """Main function to run the bot."""
    def signal_handler(signum, frame):
        print("\nShutting down bot...")
        sys.exit(0)
        
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    bot = TaggerBot()
    
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
