import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import re
import asyncio
import uuid
import logging
import json
from datetime import datetime, timedelta
import google.generativeai as genai
from typing import Dict, Optional

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.dm_messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === Configuration - Replace with your actual IDs ===
# Trading System Configuration
OSRS_MAIN_CHANNEL_ID = 1393681123355394058
OSRS_IRON_CHANNEL_ID = 1393671722636546088
STAFF_ROLE_ID = 1399572599054536866
TRADE_CATEGORY_ID = 1402544026032541768
COMPLETED_SALES_CHANNEL_ID = 1402544168034766850
VOUCH_LOG_CHANNEL_ID = 1399553110841622660
AI_CHANNEL_ID = 1400157457774411916

# Ticket System Configuration
TICKET_CATEGORY_NAME = "Support Tickets"
SUPPORT_ROLE_NAME = "Support"
TICKET_COUNTER = 0

# In-memory storage (consider using a database for production)
bot.temp_sales = {}
bot.active_listings = {}  # {listing_id: sale_data}
bot.pending_vouches = {}
bot.user_stats = {}  # {user_id: {"sales": 0, "purchases": 0, "rating": 0}}

try:
    import google.generativeai as genai

    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    print("⚠️ Warning: google-generativeai module not found. AI features disabled.")

if GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-1.5-flash')
        user_chat_sessions = {}  # Store chat sessions per user
        AI_READY = True
        print("✅ Gemini AI initialized successfully")
    except Exception as e:
        AI_READY = False
        print(f"⚠️ Failed to initialize Gemini: {e}")
else:
    AI_READY = False
    if not GEMINI_AVAILABLE:
        print("⚠️ Gemini library not installed")
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY not found in environment")


# === Utility Functions (Trading) ===
def extract_price_value(price_str: str) -> int:
    """Extract numeric value from price string"""
    if not price_str:
        return 0
    price_str = price_str.lower().replace(",", "").replace("$", "").replace("m", "000000").replace("k", "000")
    match = re.search(r"(\d+)", price_str)
    return int(match.group(1)) if match else 0


def validate_price_format(price_str: str) -> bool:
    """Validate price format"""
    if not price_str:
        return False
    # Allow formats like: $150, 250m GP, 100k, etc.
    pattern = r'^[\$]?\d+[kmb]?(\s*(gp|osrs|rs|gold|dollars?))?$'
    return bool(re.match(pattern, price_str.lower().strip().replace(',', '')))


def get_user_stats(user_id: int) -> dict:
    """Get user trading statistics"""
    return bot.user_stats.get(user_id, {"sales": 0, "purchases": 0, "total_rating": 0, "rating_count": 0})


def update_user_stats(user_id: int, action: str, rating: int = None):
    """Update user statistics"""
    if user_id not in bot.user_stats:
        bot.user_stats[user_id] = {"sales": 0, "purchases": 0, "total_rating": 0, "rating_count": 0}

    if action in ["sale", "sales"]:
        bot.user_stats[user_id]["sales"] += 1
    elif action in ["purchase", "purchases"]:
        bot.user_stats[user_id]["purchases"] += 1

    if rating is not None:
        bot.user_stats[user_id]["total_rating"] += rating
        bot.user_stats[user_id]["rating_count"] += 1


def get_average_rating(user_id: int) -> float:
    """Calculate user's average rating"""
    stats = get_user_stats(user_id)
    if stats["rating_count"] == 0:
        return 0
    return round(stats["total_rating"] / stats["rating_count"], 1)


# === UI Components (Trading) ===
def build_listing_embed(sale, message=None):
    """Build the listing embed with improved formatting"""
    embed = discord.Embed(
        title=f"💼 {sale['account_type']}",
        description=sale['description'][:1024] if len(sale['description']) > 1024 else sale['description'],
        color=discord.Color.from_rgb(255, 204, 0)
    )

    embed.add_field(name="💰 Price", value=sale["price"], inline=True)
    embed.add_field(name="🧑 Seller", value=sale["user"].mention, inline=True)

    # Add seller stats
    stats = get_user_stats(sale["user"].id)
    rating = get_average_rating(sale["user"].id)
    rating_display = f"{'⭐' * int(rating)} ({rating}/5)" if rating > 0 else "No ratings yet"
    embed.add_field(
        name="📊 Seller Stats",
        value=f"Sales: {stats['sales']} | Rating: {rating_display}",
        inline=False
    )

    embed.set_footer(
        text=f"Click 'Trade' to start a secure trade with {sale['user'].display_name}",
        icon_url=sale["user"].display_avatar.url if sale["user"].display_avatar else None
    )

    # Handle images
    attachments = sale.get("attachments") or (message.attachments if message else [])
    if "image_url" in sale:
        embed.set_image(url=sale["image_url"])
    elif attachments:
        embed.set_image(url=attachments[0].url)

    return embed


# === Trading Views ===
class SaleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🏆 Post OSRS Main Account", style=discord.ButtonStyle.green, custom_id="post_osrs_main")
    async def post_main(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SaleModal("Main"))

    @discord.ui.button(label="⚒️ Post OSRS Iron Account", style=discord.ButtonStyle.blurple, custom_id="post_osrs_iron")
    async def post_iron(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SaleModal("Ironman"))

    @discord.ui.button(label="📊 My Stats", style=discord.ButtonStyle.secondary, custom_id="view_stats")
    async def view_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        stats = get_user_stats(interaction.user.id)
        rating = get_average_rating(interaction.user.id)
        rating_display = f"{'⭐' * int(rating)} ({rating}/5)" if rating > 0 else "No ratings yet"

        embed = discord.Embed(
            title=f"📊 {interaction.user.display_name}'s Trading Stats",
            color=discord.Color.blue()
        )
        embed.add_field(name="🛒 Total Sales", value=str(stats['sales']), inline=True)
        embed.add_field(name="💰 Total Purchases", value=str(stats['purchases']), inline=True)
        embed.add_field(name="⭐ Average Rating", value=rating_display, inline=True)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        await interaction.response.send_message(embed=embed, ephemeral=True)


class SaleModal(discord.ui.Modal):
    def __init__(self, account_type_prefix: str):
        super().__init__(title=f"{account_type_prefix} Account Listing")
        self.account_type_prefix = account_type_prefix

        self.account_type = discord.ui.TextInput(
            label="Account Type",
            placeholder="e.g. Maxed Pure, Zerker, Main, etc.",
            max_length=100
        )
        self.price = discord.ui.TextInput(
            label="Price",
            placeholder="e.g. $150, 250m GP, 100k OSRS",
            max_length=50
        )
        self.description = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            placeholder="Describe your account (stats, quests, items, etc.)",
            max_length=1024
        )

        self.add_item(self.account_type)
        self.add_item(self.price)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction):
        # Validate price format
        if not validate_price_format(self.price.value):
            await interaction.response.send_message(
                "❌ Invalid price format. Please use formats like: $150, 250m GP, 100k OSRS",
                ephemeral=True
            )
            return

        # Check for active listings
        user_active_listings = [sale for sale in bot.temp_sales.values() if sale["user"].id == interaction.user.id]
        if len(user_active_listings) >= 3:
            await interaction.response.send_message(
                "❌ You can only have 3 active listings at a time. Cancel or complete existing trades first.",
                ephemeral=True
            )
            return

        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                "✅ **Please send 1-3 screenshots of your account here.**\n"
                "📸 Include combat stats, bank value, or any other relevant screenshots.\n"
                "⏰ You have 10 minutes to send the images."
            )
            await interaction.response.send_message(
                "📩 Check your DMs to complete your listing by sending screenshots!",
                ephemeral=True
            )

            bot.temp_sales[interaction.user.id] = {
                "account_type": f"{self.account_type_prefix} - {self.account_type.value}",
                "price": self.price.value,
                "description": self.description.value,
                "user": interaction.user,
                "created_at": datetime.utcnow(),
                "expires_at": datetime.utcnow() + timedelta(minutes=10)
            }

            # Auto-cleanup expired listings
            await asyncio.sleep(600)  # 10 minutes
            if interaction.user.id in bot.temp_sales:
                expired_sale = bot.temp_sales.pop(interaction.user.id, None)
                if expired_sale:
                    try:
                        await dm.send(
                            "❌ Your listing expired. Please start over if you still want to list your account.")
                    except discord.Forbidden:
                        pass

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I can't send you a DM. Please enable DMs from server members and try again.",
                ephemeral=True
            )


class TradeView(discord.ui.View):
    def __init__(self, bot, seller, sale_data=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.sale_data = sale_data
        self.seller = seller

    @discord.ui.button(label="💱 Trade", style=discord.ButtonStyle.green, custom_id="buy_account")
    async def trade(self, interaction: discord.Interaction, button: discord.ui.Button):
        buyer = interaction.user
        seller = self.seller

        # Prevent self-trading
        if buyer.id == seller.id:
            await interaction.response.send_message("❌ You can't trade with yourself!", ephemeral=True)
            return

        guild = interaction.guild
        category = guild.get_channel(TRADE_CATEGORY_ID)
        staff_role = guild.get_role(STAFF_ROLE_ID)

        if not category:
            await interaction.response.send_message("❌ Trade category not found. Contact staff.", ephemeral=True)
            logger.error(f"Trade category {TRADE_CATEGORY_ID} not found")
            return

        if not staff_role:
            await interaction.response.send_message("❌ Staff role not found. Contact an administrator.", ephemeral=True)
            logger.error(f"Staff role {STAFF_ROLE_ID} not found")
            return

        # Set up permissions
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            buyer: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            seller: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }

        try:
            # Create private trade channel
            trade_channel = await guild.create_text_channel(
                name=f"trade-{buyer.display_name[:10].lower()}",
                category=category,
                overwrites=overwrites,
                topic=f"Trade between {buyer.display_name} and {seller.display_name}"
            )

            # Create initial trade message
            embed = discord.Embed(
                title="🔒 Secure Trade Initiated",
                description=(
                    f"**Buyer:** {buyer.mention}\n"
                    f"**Seller:** {seller.mention}\n"
                    f"**Staff:** {staff_role.mention}\n\n"
                    f"**Account:** {self.sale_data.get('account_type', 'Unknown')}\n"
                    f"**Price:** {self.sale_data.get('price', 'Unknown')}\n\n"
                    "**Instructions:**\n"
                    "1. Discuss trade details\n"
                    "2. When both parties are satisfied, click '✅ Trade Completed'\n"
                    "3. Both parties must confirm before the trade is finalized"
                ),
                color=discord.Color.blue()
            )
            embed.set_footer(text="⚠️ Staff are monitoring this channel for your safety")

            await trade_channel.send(
                embed=embed,
                view=TradeCompleteView(buyer, seller, sale_data=self.sale_data)
            )

            await interaction.response.send_message(
                f"✅ **Trade channel created!** {trade_channel.mention}\n"
                f"Please proceed there to complete your trade safely.",
                ephemeral=True
            )

            logger.info(f"Trade channel created: {trade_channel.name} ({buyer} <-> {seller})")

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to create trade channels. Contact an administrator.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                "❌ Failed to create trade channel. Please try again or contact staff.",
                ephemeral=True
            )
            logger.error(f"Error creating trade channel: {e}")

    @discord.ui.button(label="❌ Cancel Listing", style=discord.ButtonStyle.danger, custom_id="cancel_listing")
    async def cancel_listing(self, interaction: discord.Interaction, button: discord.ui.Button):
        sale = self.sale_data

        if not sale:
            await interaction.response.send_message("❌ Sale data missing.", ephemeral=True)
            return

        if interaction.user.id != sale["user"].id:
            await interaction.response.send_message("❌ Only the seller can cancel this listing.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            # Delete listing messages
            await self.delete_listing_messages(interaction.guild, sale)

            # Clean up storage
            bot.temp_sales.pop(sale["user"].id, None)
            if hasattr(sale, 'listing_id'):
                bot.active_listings.pop(sale.get('listing_id'), None)

            await interaction.followup.send("✅ Your listing has been canceled and deleted.", ephemeral=True)
            logger.info(f"Listing canceled by {interaction.user}")

        except Exception as e:
            logger.error(f"Error canceling listing: {e}")
            await interaction.followup.send("❌ Error canceling listing. Contact staff if needed.", ephemeral=True)

    @discord.ui.button(label="✏️ Edit Listing", style=discord.ButtonStyle.secondary, custom_id="edit_listing")
    async def edit_listing(self, interaction: discord.Interaction, button: discord.ui.Button):
        sale = self.sale_data

        if not sale or interaction.user.id != sale["user"].id:
            await interaction.response.send_message("❌ Only the seller can edit this listing.", ephemeral=True)
            return

        await interaction.response.send_modal(EditSaleModal(sale, self))

    async def delete_listing_messages(self, guild, sale):
        """Helper method to delete all listing messages"""
        listing_channel = guild.get_channel(sale.get("listing_channel_id"))
        if not listing_channel:
            return

        # Delete main listing message
        if sale.get("listing_message_id"):
            try:
                msg = await listing_channel.fetch_message(sale["listing_message_id"])
                await msg.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                logger.warning(f"Failed to delete main listing message: {e}")

        # Delete extra image messages
        for extra_id in sale.get("extra_message_ids", []):
            try:
                msg = await listing_channel.fetch_message(extra_id)
                await msg.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                logger.warning(f"Failed to delete extra image message: {e}")


class EditSaleModal(discord.ui.Modal, title="Edit Your Listing"):
    def __init__(self, sale_data, trade_view: TradeView):
        super().__init__()
        self.sale_data = sale_data
        self.trade_view = trade_view

        # Pre-fill with current values
        current_type = sale_data["account_type"].split(" - ", 1)[-1] if " - " in sale_data["account_type"] else \
            sale_data["account_type"]

        self.account_type = discord.ui.TextInput(
            label="Account Type",
            default=current_type,
            required=True,
            max_length=100
        )
        self.price = discord.ui.TextInput(
            label="Price",
            default=sale_data["price"],
            required=True,
            max_length=50
        )
        self.description = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            default=sale_data["description"],
            required=True,
            max_length=1024
        )

        self.add_item(self.account_type)
        self.add_item(self.price)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction):
        # Validate new price format
        if not validate_price_format(self.price.value):
            await interaction.response.send_message(
                "❌ Invalid price format. Please use formats like: $150, 250m GP, 100k OSRS",
                ephemeral=True
            )
            return

        # Update the sale data
        account_prefix = self.sale_data["account_type"].split(" - ")[0] if " - " in self.sale_data[
            "account_type"] else "Account"
        self.sale_data["account_type"] = f"{account_prefix} - {self.account_type.value}"
        self.sale_data["price"] = self.price.value
        self.sale_data["description"] = self.description.value

        try:
            # Update the embed message
            channel = interaction.guild.get_channel(self.sale_data["listing_channel_id"])
            message = await channel.fetch_message(self.sale_data["listing_message_id"])
            new_embed = build_listing_embed(self.sale_data, message)
            await message.edit(embed=new_embed)

            await interaction.response.send_message("✅ Listing updated successfully!", ephemeral=True)
            logger.info(f"Listing edited by {interaction.user}")

        except discord.NotFound:
            await interaction.response.send_message("❌ Original listing message not found.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error updating listing: {e}")
            await interaction.response.send_message("❌ Failed to update listing. Contact staff.", ephemeral=True)


class TradeCompleteView(discord.ui.View):
    def __init__(self, buyer: discord.Member, seller: discord.Member, sale_data: dict = None):
        super().__init__(timeout=None)
        self.buyer = buyer
        self.seller = seller
        self.sale_data = sale_data or {}
        self.completed_by = set()
        self.trade_id = str(uuid.uuid4())

    @discord.ui.button(label="✅ Trade Completed", style=discord.ButtonStyle.green, custom_id="trade_complete")
    async def complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in {self.buyer.id, self.seller.id}:
            # Allow staff to complete trades
            if not any(role.id == STAFF_ROLE_ID for role in interaction.user.roles):
                await interaction.response.send_message("❌ You're not part of this trade.", ephemeral=True)
                return

        self.completed_by.add(interaction.user.id)
        remaining_user = None

        if self.buyer.id not in self.completed_by:
            remaining_user = self.buyer
        elif self.seller.id not in self.completed_by:
            remaining_user = self.seller

        if remaining_user:
            await interaction.response.send_message(
                f"✅ You marked the trade as complete. Waiting for {remaining_user.mention} to confirm.",
                ephemeral=True
            )
        else:
            await self.finalize_trade(interaction)

    @discord.ui.button(label="❌ Cancel Trade", style=discord.ButtonStyle.danger, custom_id="trade_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in {self.buyer.id, self.seller.id}:
            # Allow staff to cancel trades
            if not any(role.id == STAFF_ROLE_ID for role in interaction.user.roles):
                await interaction.response.send_message("❌ You're not part of this trade.", ephemeral=True)
                return

        await self.end_trade(interaction, completed=False)

    async def finalize_trade(self, interaction: discord.Interaction):
        """Finalize a completed trade"""
        embed = discord.Embed(
            title="🎉 Trade Completed Successfully!",
            description="Both parties have confirmed the trade. Processing completion...",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)

        # Update user statistics
        update_user_stats(self.seller.id, "sale")
        update_user_stats(self.buyer.id, "purchase")

        # Log the completed trade
        await self.log_completed_trade(interaction)

        # Delete listing messages
        await self.delete_original_listing(interaction.guild)

        # Send vouch requests
        await self.send_vouch_requests()

        # Close the trade channel
        await self.end_trade(interaction, completed=True)

    async def log_completed_trade(self, interaction):
        """Log the trade to the completed sales channel"""
        log_channel = interaction.guild.get_channel(COMPLETED_SALES_CHANNEL_ID)
        if not log_channel:
            logger.warning("Completed sales channel not found")
            return

        try:
            embed = discord.Embed(
                title="✅ Trade Completed",
                description=self.sale_data.get("description", "No description provided."),
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Account Type", value=self.sale_data.get("account_type", "Unknown"), inline=True)
            embed.add_field(name="Price", value=self.sale_data.get("price", "Unknown"), inline=True)
            embed.add_field(name="Trade ID", value=self.trade_id[:8], inline=True)
            embed.set_footer(text=f"Buyer: {self.buyer} • Seller: {self.seller}")

            await log_channel.send(embed=embed)
            logger.info(f"Trade completed and logged: {self.trade_id}")

        except Exception as e:
            logger.error(f"Failed to log completed trade: {e}")

    async def delete_original_listing(self, guild):
        """Delete the original listing messages"""
        try:
            listing_channel_id = self.sale_data.get("listing_channel_id")
            listing_message_id = self.sale_data.get("listing_message_id")

            if listing_channel_id and listing_message_id:
                listing_channel = guild.get_channel(listing_channel_id)
                if listing_channel:
                    # Delete main listing message
                    try:
                        msg = await listing_channel.fetch_message(listing_message_id)
                        await msg.delete()
                    except discord.NotFound:
                        pass

                    # Delete extra image messages
                    for extra_id in self.sale_data.get("extra_message_ids", []):
                        try:
                            extra_msg = await listing_channel.fetch_message(extra_id)
                            await extra_msg.delete()
                        except discord.NotFound:
                            pass

        except Exception as e:
            logger.error(f"Failed to delete original listing: {e}")

    async def send_vouch_requests(self):
        """Send vouch requests to both parties"""
        for user, role in [(self.buyer, "buyer"), (self.seller, "seller")]:
            other_party = self.seller if role == "buyer" else self.buyer
            try:
                dm = await user.create_dm()
                embed = discord.Embed(
                    title="📝 Rate Your Trading Experience",
                    description=(
                        f"You recently completed a trade with **{other_party.display_name}**!\n\n"
                        f"**Account:** {self.sale_data.get('account_type', 'Unknown')}\n"
                        f"**Price:** {self.sale_data.get('price', 'Unknown')}\n\n"
                        "Please rate your experience to help build trust in our community:"
                    ),
                    color=discord.Color.blue()
                )
                embed.set_footer(text="Your rating helps other traders make informed decisions")

                await dm.send(
                    embed=embed,
                    view=StarRatingView(
                        rater=user,
                        role=role,
                        trade_id=self.trade_id,
                        other_party=other_party,
                        account_info=self.sale_data
                    )
                )
            except discord.Forbidden:
                logger.warning(f"Couldn't send vouch request to {user}")

    async def end_trade(self, interaction: discord.Interaction, completed: bool):
        """End the trade and clean up"""
        channel = interaction.channel
        status = "completed" if completed else "canceled"

        # Send final message
        if not completed:
            embed = discord.Embed(
                title="❌ Trade Canceled",
                description="This trade has been canceled by one of the parties.",
                color=discord.Color.red()
            )
            await channel.send(embed=embed)

        # Disable all buttons
        for item in self.children:
            item.disabled = True

        try:
            # Edit the original message to show trade is ended
            async for message in channel.history(limit=10):
                if message.author == interaction.client.user and message.embeds:
                    embed = message.embeds[0]
                    embed.color = discord.Color.red() if not completed else discord.Color.green()
                    embed.title = f"🔒 Trade {status.capitalize()}"
                    await message.edit(embed=embed, view=self)
                    break
        except Exception as e:
            logger.warning(f"Failed to update trade message: {e}")

        # Wait before deleting channel
        await asyncio.sleep(10)

        try:
            await channel.delete(reason=f"Trade {status} - auto cleanup")
            logger.info(f"Trade channel deleted: {channel.name} ({status})")
        except Exception as e:
            logger.error(f"Failed to delete trade channel: {e}")


class StarRatingView(discord.ui.View):
    def __init__(self, rater, trade_id, role, other_party, account_info):
        super().__init__(timeout=300)
        self.rater = rater
        self.trade_id = trade_id
        self.role = role
        self.other_party = other_party
        self.account_info = account_info

        # Add star rating buttons
        for i in range(1, 6):
            self.add_item(self.StarButton(i, self))

    class StarButton(discord.ui.Button):
        def __init__(self, stars: int, parent_view: "StarRatingView"):
            super().__init__(
                style=discord.ButtonStyle.primary,
                label="⭐" * stars,
                custom_id=f"rate_{stars}"
            )
            self.stars = stars
            self.parent_view = parent_view

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(
                VouchCommentModal(
                    stars=self.stars,
                    rater=self.parent_view.rater,
                    trade_id=self.parent_view.trade_id,
                    role=self.parent_view.role,
                    other_party=self.parent_view.other_party,
                    account_info=self.parent_view.account_info,
                )
            )


class VouchCommentModal(discord.ui.Modal, title="Leave Your Review"):
    def __init__(self, stars, rater, trade_id, role, other_party, account_info):
        super().__init__()
        self.stars = stars
        self.rater = rater
        self.trade_id = trade_id
        self.role = role
        self.other_party = other_party
        self.account_info = account_info

        self.comment = discord.ui.TextInput(
            label="Your Review (Optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            placeholder="Share details about your trading experience...",
            max_length=500
        )
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction):
        # Store the vouch
        if self.trade_id not in bot.pending_vouches:
            bot.pending_vouches[self.trade_id] = {}

        bot.pending_vouches[self.trade_id][self.role] = {
            "rater": self.rater,
            "rating": self.stars,
            "comment": self.comment.value or "No comment provided.",
        }

        # Update user stats with the rating
        other_party_id = self.other_party.id
        update_user_stats(other_party_id, "rating", self.stars)

        await interaction.response.send_message(
            f"✅ **Thank you for your {self.stars}⭐ rating!**\n"
            "Your review helps build trust in our trading community.",
            ephemeral=True
        )

        # Check if both parties have submitted vouches
        if len(bot.pending_vouches[self.trade_id]) == 2:
            await self.post_complete_vouch(interaction)

    async def post_complete_vouch(self, interaction):
        """Post the complete vouch when both parties have rated"""
        try:
            buyer_vouch = bot.pending_vouches[self.trade_id]["buyer"]
            seller_vouch = bot.pending_vouches[self.trade_id]["seller"]

            channel = interaction.client.get_channel(VOUCH_LOG_CHANNEL_ID)
            if not channel:
                logger.error("Vouch log channel not found")
                return

            embed = discord.Embed(
                title="✅ Trade Review",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )

            embed.add_field(
                name="📦 Trade Details",
                value=(
                    f"**Account:** {self.account_info.get('account_type', 'Unknown')}\n"
                    f"**Price:** {self.account_info.get('price', 'Unknown')}\n"
                    f"**Trade ID:** {self.trade_id[:8]}"
                ),
                inline=False
            )

            embed.add_field(
                name=f"🛒 Buyer Review - {'⭐' * buyer_vouch['rating']} ({buyer_vouch['rating']}/5)",
                value=f"{buyer_vouch['rater'].mention}: {buyer_vouch['comment']}",
                inline=False
            )

            embed.add_field(
                name=f"💼 Seller Review - {'⭐' * seller_vouch['rating']} ({seller_vouch['rating']}/5)",
                value=f"{seller_vouch['rater'].mention}: {seller_vouch['comment']}",
                inline=False
            )

            await channel.send(embed=embed)

            # Clean up the pending vouch
            del bot.pending_vouches[self.trade_id]
            logger.info(f"Complete vouch posted for trade {self.trade_id}")

        except Exception as e:
            logger.error(f"Error posting complete vouch: {e}")


# === TICKET SYSTEM VIEWS ===
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Persistent view

    @discord.ui.button(label='Create Ticket', style=discord.ButtonStyle.green, emoji='🎫', custom_id="create_ticket_btn")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        global TICKET_COUNTER
        TICKET_COUNTER += 1

        guild = interaction.guild
        user = interaction.user

        # Find or create the ticket category
        category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
        if not category:
            category = await guild.create_category(TICKET_CATEGORY_NAME)

        # Create ticket channel
        channel_name = f"ticket-{user.name}-{TICKET_COUNTER}"

        # Set permissions
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        # Add support role permissions if it exists
        support_role = discord.utils.get(guild.roles, name=SUPPORT_ROLE_NAME)
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        # Also add staff role permissions if it exists (for trading staff)
        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        ticket_channel = await guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=overwrites
        )

        # Create welcome embed for the ticket
        embed = discord.Embed(
            title="🎫 Support Ticket",
            description="Support will be with you shortly. To close this ticket, press the close button below.",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="📋 What can we help you with?",
            value=(
                "• Trading issues or disputes\n"
                "• Account verification\n"
                "• Technical support\n"
                "• General questions\n"
                "• Report a problem"
            ),
            inline=False
        )
        embed.set_footer(text="ScubaAI Support System - We're here to help!")

        # Send welcome message with close button
        await ticket_channel.send(
            f"Welcome {user.mention}!",
            embed=embed,
            view=CloseTicketView()
        )

        # Respond to the interaction
        await interaction.response.send_message(
            f"✅ **Ticket created successfully!**\nCheck {ticket_channel.mention} for support.",
            ephemeral=True
        )

        logger.info(f"Ticket created: {ticket_channel.name} by {user}")


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Close Ticket', style=discord.ButtonStyle.red, emoji='🔒', custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user has permission to close (ticket creator, support, or staff)
        channel = interaction.channel
        if not channel.name.startswith("ticket-"):
            await interaction.response.send_message("❌ This can only be used in ticket channels!", ephemeral=True)
            return

        # Create confirmation embed
        embed = discord.Embed(
            title="🔒 Close Ticket",
            description="Are you sure you want to close this support ticket?",
            color=discord.Color.red()
        )
        embed.add_field(
            name="⚠️ Note",
            value="This action cannot be undone. The channel will be permanently deleted.",
            inline=False
        )

        await interaction.response.send_message(
            embed=embed,
            view=ConfirmCloseView(),
            ephemeral=True
        )


class ConfirmCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label='Yes, Close Ticket', style=discord.ButtonStyle.red, emoji='✅')
    async def confirm_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel

        # Send closing message
        embed = discord.Embed(
            title="🔒 Ticket Closing",
            description=f"This ticket is being closed by {interaction.user.mention}.\nChannel will be deleted in 5 seconds...",
            color=discord.Color.red()
        )
        embed.set_footer(text="Thank you for using our support system!")

        await interaction.response.send_message(embed=embed)
        logger.info(f"Ticket closed: {channel.name} by {interaction.user}")

        # Wait 5 seconds then delete
        await asyncio.sleep(5)
        await channel.delete(reason=f"Ticket closed by {interaction.user}")

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.gray, emoji='❌')
    async def cancel_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("✅ Ticket close cancelled.", ephemeral=True)


# === Bot Commands (Trading) ===
@bot.command()
@commands.has_permissions(administrator=True)
async def test_gemini(ctx):
    """Test Gemini AI functionality"""
    if not GEMINI_AVAILABLE:
        await ctx.send("❌ Gemini API is not configured. Please set the GEMINI_API_KEY environment variable.")
        return

    embed = discord.Embed(title="🔧 Gemini AI Test", color=discord.Color.blue())

    try:
        # Test simple prompt
        test_model = genai.GenerativeModel('gemini-1.5-flash')
        test_response = test_model.generate_content(
            "Hello! This is a test message. Please respond with 'Test successful!'")

        if test_response and test_response.text:
            embed.add_field(
                name="✅ Gemini Test Successful",
                value=f"Response: {test_response.text[:200]}{'...' if len(test_response.text) > 200 else ''}",
                inline=False
            )
            embed.color = discord.Color.green()
        else:
            embed.add_field(name="❌ Test Failed", value="No response received", inline=False)
            embed.color = discord.Color.red()

    except Exception as e:
        embed.add_field(name="❌ Error", value=f"{type(e).__name__}: {str(e)}", inline=False)
        embed.color = discord.Color.red()

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def clear_gemini_sessions(ctx):
    """Clear all Gemini chat sessions"""
    if GEMINI_AVAILABLE:
        count = len(user_chat_sessions)
        user_chat_sessions.clear()
        await ctx.send(f"✅ Cleared {count} Gemini chat sessions.")
    else:
        await ctx.send("❌ Gemini AI is not available.")


@bot.command()
@commands.has_permissions(administrator=True)
async def gemini_stats(ctx):
    """Show Gemini usage statistics"""
    if not GEMINI_AVAILABLE:
        await ctx.send("❌ Gemini AI is not configured.")
        return

    embed = discord.Embed(title="📊 Gemini AI Statistics", color=discord.Color.blue())
    embed.add_field(name="Active Chat Sessions", value=str(len(user_chat_sessions)), inline=True)
    embed.add_field(name="Model", value="gemini-1.5-flash", inline=True)
    embed.add_field(name="Status", value="✅ Online" if GEMINI_AVAILABLE else "❌ Offline", inline=True)

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    """Create the main trading panel"""
    embed = discord.Embed(
        title="🏆 OSRS Account Trading Hub",
        description=(
            "**Welcome to the secure OSRS account marketplace!**\n\n"
            "🔸 **Post your account** using the buttons below\n"
            "🔸 **Browse listings** in the marketplace channels\n"
            "🔸 **Trade safely** with our secure system\n"
            "🔸 **Build reputation** through our rating system\n\n"
            "All trades are monitored by staff for your protection."
        ),
        color=discord.Color.from_rgb(255, 204, 0)
    )
    embed.set_footer(text="⚠️ Always trade through our secure system • Powered by ScubaAI")
    embed.set_thumbnail(url="https://oldschool.runescape.wiki/images/Old_school_icon.png")

    view = SaleView()
    await ctx.send(embed=embed, view=view)


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clean_expired(ctx):
    """Clean up expired temporary sales"""
    now = datetime.utcnow()
    expired_count = 0

    for user_id in list(bot.temp_sales.keys()):
        sale = bot.temp_sales[user_id]
        if sale.get("expires_at", now) <= now:
            bot.temp_sales.pop(user_id, None)
            expired_count += 1

    await ctx.send(f"✅ Cleaned up {expired_count} expired temporary listings.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def stats(ctx, user: discord.Member = None):
    """View trading statistics for a user"""
    target_user = user or ctx.author
    stats = get_user_stats(target_user.id)
    rating = get_average_rating(target_user.id)
    rating_display = f"{'⭐' * int(rating)} ({rating}/5)" if rating > 0 else "No ratings yet"

    embed = discord.Embed(
        title=f"📊 Trading Statistics",
        color=discord.Color.blue()
    )
    embed.set_author(name=target_user.display_name, icon_url=target_user.display_avatar.url)

    embed.add_field(name="🛒 Total Sales", value=str(stats['sales']), inline=True)
    embed.add_field(name="💰 Total Purchases", value=str(stats['purchases']), inline=True)
    embed.add_field(name="🔄 Total Trades", value=str(stats['sales'] + stats['purchases']), inline=True)
    embed.add_field(name="⭐ Average Rating", value=rating_display, inline=True)
    embed.add_field(name="📝 Total Reviews", value=str(stats['rating_count']), inline=True)

    # Add reputation level
    total_trades = stats['sales'] + stats['purchases']
    if total_trades >= 50:
        rep_level = "🏆 Elite Trader"
    elif total_trades >= 25:
        rep_level = "🥇 Expert Trader"
    elif total_trades >= 10:
        rep_level = "🥈 Experienced Trader"
    elif total_trades >= 5:
        rep_level = "🥉 Active Trader"
    else:
        rep_level = "🌟 New Trader"

    embed.add_field(name="🏅 Reputation Level", value=rep_level, inline=True)

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def clear_vouches(ctx, user: discord.Member, confirmation: str = None):
    """Clear all vouch history for a user (admin only)"""
    if confirmation != "CONFIRM":
        embed = discord.Embed(
            title="⚠️ Vouch History Clearing",
            description=(
                f"This will **permanently delete** all vouch history for {user.mention}:\n\n"
                f"• All ratings and reviews\n"
                f"• Average rating calculation\n"
                f"• Rating count\n\n"
                f"**This action cannot be undone!**\n\n"
                f"To confirm, use: `!clear_vouches {user.mention} CONFIRM`"
            ),
            color=discord.Color.orange()
        )
        embed.set_footer(text="⚠️ This is a destructive action - use with caution")
        await ctx.send(embed=embed)
        return

    # Get current stats before clearing
    old_stats = get_user_stats(user.id)
    old_rating = get_average_rating(user.id)

    # Clear the user's rating data while preserving sales/purchases
    if user.id in bot.user_stats:
        bot.user_stats[user.id]["total_rating"] = 0
        bot.user_stats[user.id]["rating_count"] = 0

    # Remove any pending vouches for this user
    vouches_removed = 0
    for trade_id in list(bot.pending_vouches.keys()):
        trade_vouches = bot.pending_vouches[trade_id]

        # Remove vouches submitted by this user
        roles_to_remove = []
        for role, vouch_data in trade_vouches.items():
            if vouch_data.get("rater") and vouch_data["rater"].id == user.id:
                roles_to_remove.append(role)
                vouches_removed += 1

        for role in roles_to_remove:
            del trade_vouches[role]

        # Remove empty trade vouch entries
        if not trade_vouches:
            del bot.pending_vouches[trade_id]

    # Success message
    embed = discord.Embed(
        title="✅ Vouch History Cleared",
        description=f"Successfully cleared vouch history for {user.mention}",
        color=discord.Color.green()
    )

    embed.add_field(
        name="📊 Previous Stats",
        value=(
            f"Rating: {old_rating}/5 ⭐\n"
            f"Reviews: {old_stats['rating_count']}\n"
            f"Sales: {old_stats['sales']} (preserved)\n"
            f"Purchases: {old_stats['purchases']} (preserved)"
        ),
        inline=True
    )

    embed.add_field(
        name="🗑️ Removed",
        value=(
            f"Pending vouches: {vouches_removed}\n"
            f"Rating data: ✅ Cleared\n"
            f"Review count: ✅ Reset to 0"
        ),
        inline=True
    )

    embed.set_footer(text=f"Action performed by {ctx.author}")

    await ctx.send(embed=embed)
    logger.info(f"Vouch history cleared for {user} by {ctx.author}")


@bot.command()
@commands.has_permissions(administrator=True)
async def reset_user_stats(ctx, user: discord.Member, confirmation: str = None):
    """Completely reset all stats for a user (admin only)"""
    if confirmation != "CONFIRM":
        embed = discord.Embed(
            title="⚠️ Complete User Reset",
            description=(
                f"This will **permanently delete** ALL data for {user.mention}:\n\n"
                f"• All sales and purchases\n"
                f"• All ratings and reviews\n"
                f"• All trading statistics\n\n"
                f"**This action cannot be undone!**\n\n"
                f"To confirm, use: `!reset_user_stats {user.mention} CONFIRM`"
            ),
            color=discord.Color.red()
        )
        embed.set_footer(text="⚠️ This completely removes the user from the system")
        await ctx.send(embed=embed)
        return

    # Get current stats before clearing
    old_stats = get_user_stats(user.id)
    old_rating = get_average_rating(user.id)

    # Completely remove user from stats
    if user.id in bot.user_stats:
        del bot.user_stats[user.id]

    # Remove pending vouches
    vouches_removed = 0
    for trade_id in list(bot.pending_vouches.keys()):
        trade_vouches = bot.pending_vouches[trade_id]
        roles_to_remove = []

        for role, vouch_data in trade_vouches.items():
            if vouch_data.get("rater") and vouch_data["rater"].id == user.id:
                roles_to_remove.append(role)
                vouches_removed += 1

        for role in roles_to_remove:
            del trade_vouches[role]

        if not trade_vouches:
            del bot.pending_vouches[trade_id]

    # Remove any active listings
    user_listings = [listing_id for listing_id, sale in bot.active_listings.items()
                     if sale["user"].id == user.id]
    for listing_id in user_listings:
        del bot.active_listings[listing_id]

    # Remove temporary sales
    bot.temp_sales.pop(user.id, None)

    embed = discord.Embed(
        title="✅ User Data Completely Reset",
        description=f"All data for {user.mention} has been permanently removed",
        color=discord.Color.red()
    )

    embed.add_field(
        name="📊 Previous Stats",
        value=(
            f"Sales: {old_stats['sales']}\n"
            f"Purchases: {old_stats['purchases']}\n"
            f"Rating: {old_rating}/5 ⭐\n"
            f"Reviews: {old_stats['rating_count']}"
        ),
        inline=True
    )

    embed.add_field(
        name="🗑️ Removed",
        value=(
            f"User statistics: ✅\n"
            f"Pending vouches: {vouches_removed}\n"
            f"Active listings: {len(user_listings)}\n"
            f"Temp sales: {'✅' if user.id in bot.temp_sales else 'None'}"
        ),
        inline=True
    )

    await ctx.send(embed=embed)
    logger.info(f"Complete user reset for {user} by {ctx.author}")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def vouch_info(ctx, user: discord.Member):
    """Display detailed vouch information for a user"""
    stats = get_user_stats(user.id)
    rating = get_average_rating(user.id)

    embed = discord.Embed(
        title=f"📝 Vouch Information",
        color=discord.Color.blue()
    )
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)

    embed.add_field(
        name="📊 Rating Statistics",
        value=(
            f"Average Rating: {rating}/5 {'⭐' * int(rating) if rating > 0 else 'No ratings'}\n"
            f"Total Reviews: {stats['rating_count']}\n"
            f"Total Rating Points: {stats['total_rating']}"
        ),
        inline=False
    )

    embed.add_field(
        name="🔄 Trading Activity",
        value=(
            f"Sales: {stats['sales']}\n"
            f"Purchases: {stats['purchases']}\n"
            f"Total Trades: {stats['sales'] + stats['purchases']}"
        ),
        inline=False
    )

    # Check for pending vouches
    pending_count = 0
    for trade_vouches in bot.pending_vouches.values():
        for vouch_data in trade_vouches.values():
            if vouch_data.get("rater") and vouch_data["rater"].id == user.id:
                pending_count += 1

    if pending_count > 0:
        embed.add_field(
            name="⏳ Pending Vouches",
            value=f"{pending_count} vouch(es) waiting to be completed",
            inline=False
        )

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def active_listings(ctx):
    """View all active listings"""
    temp_count = len(bot.temp_sales)
    active_count = len(bot.active_listings)

    embed = discord.Embed(
        title="📊 Active Listings Overview",
        color=discord.Color.blue()
    )
    embed.add_field(name="⏳ Pending (awaiting images)", value=str(temp_count), inline=True)
    embed.add_field(name="✅ Active Listings", value=str(active_count), inline=True)
    embed.add_field(name="🎯 Total", value=str(temp_count + active_count), inline=True)

    if bot.temp_sales:
        temp_list = []
        for user_id, sale in list(bot.temp_sales.items())[:5]:  # Show first 5
            user = sale["user"]
            expires_in = (sale.get("expires_at", datetime.utcnow()) - datetime.utcnow()).total_seconds()
            expires_in = max(0, int(expires_in // 60))
            temp_list.append(f"{user.display_name} - {expires_in}m left")

        embed.add_field(
            name="⏳ Recent Pending Sales",
            value="\n".join(temp_list) + ("..." if len(bot.temp_sales) > 5 else ""),
            inline=False
        )

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(manage_messages=True)
async def force_complete(ctx, channel_id: int):
    """Force complete a trade (emergency use)"""
    try:
        trade_channel = bot.get_channel(channel_id)
        if not trade_channel or trade_channel.category_id != TRADE_CATEGORY_ID:
            await ctx.send("❌ Invalid trade channel.")
            return

        await trade_channel.send("⚠️ **Trade force-completed by staff.**")
        await asyncio.sleep(5)
        await trade_channel.delete(reason="Force completed by staff")
        await ctx.send("✅ Trade channel force-completed and deleted.")

    except Exception as e:
        await ctx.send(f"❌ Error: {e}")


# === TICKET SYSTEM COMMANDS ===
@bot.tree.command(name="ticket_panel", description="Create the support ticket panel")
@app_commands.default_permissions(administrator=True)
async def ticket_panel_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎫 Support Ticket System",
        description=(
            "Need help with trading, account issues, or have questions?\n"
            "Create a private support ticket using the button below!\n\n"
            "**Our support team can help with:**\n"
            "• Trading disputes or issues\n"
            "• Account verification problems\n"
            "• Technical support\n"
            "• General questions about the marketplace\n"
            "• Reporting problems or violations"
        ),
        color=discord.Color.blue()
    )
    embed.add_field(
        name="📋 How it works:",
        value=(
            "1. Click **Create Ticket** below\n"
            "2. A private channel will be created for you\n"
            "3. Explain your issue to our support team\n"
            "4. We'll help resolve your problem!"
        ),
        inline=False
    )
    embed.set_footer(text="ScubaAI Support - We're here to help 24/7!")

    await interaction.response.send_message(embed=embed, view=TicketView())


@bot.tree.command(name="ticket_stats", description="View support ticket statistics")
@app_commands.default_permissions(administrator=True)
async def ticket_stats_slash(interaction: discord.Interaction):
    guild = interaction.guild
    category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)

    if not category:
        open_tickets = 0
    else:
        open_tickets = len([ch for ch in category.channels if ch.name.startswith("ticket-")])

    embed = discord.Embed(
        title="📊 Support Ticket Statistics",
        color=discord.Color.green()
    )
    embed.add_field(name="🎫 Open Tickets", value=str(open_tickets), inline=True)
    embed.add_field(name="📈 Total Created", value=str(TICKET_COUNTER), inline=True)

    if category:
        embed.add_field(name="📂 Category", value=f"#{category.name}", inline=True)

    embed.set_footer(text="ScubaAI Support System")
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="close_ticket", description="Close the current support ticket")
async def close_ticket_slash(interaction: discord.Interaction):
    channel = interaction.channel

    # Check if this is a ticket channel
    if not channel.name.startswith("ticket-"):
        await interaction.response.send_message("❌ This command can only be used in ticket channels!", ephemeral=True)
        return

    # Create confirmation embed
    embed = discord.Embed(
        title="🔒 Close Support Ticket",
        description="Are you sure you want to close this support ticket?",
        color=discord.Color.red()
    )
    embed.add_field(
        name="⚠️ Warning",
        value="This action cannot be undone. The channel and all messages will be permanently deleted.",
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        view=ConfirmCloseView(),
        ephemeral=True
    )


# Backup prefix commands for tickets
@bot.command(name="ticket_panel")
@commands.has_permissions(administrator=True)
async def create_ticket_panel(ctx):
    """Create the support ticket panel (prefix command)"""
    embed = discord.Embed(
        title="🎫 Support Ticket System",
        description=(
            "Need help with trading, account issues, or have questions?\n"
            "Create a private support ticket using the button below!\n\n"
            "**Our support team can help with:**\n"
            "• Trading disputes or issues\n"
            "• Account verification problems\n"
            "• Technical support\n"
            "• General questions about the marketplace\n"
            "• Reporting problems or violations"
        ),
        color=discord.Color.blue()
    )
    embed.set_footer(text="ScubaAI Support - We're here to help 24/7!")

    await ctx.send(embed=embed, view=TicketView())


@bot.command(name="ticket_stats")
@commands.has_permissions(administrator=True)
async def ticket_stats_prefix(ctx):
    """View support ticket statistics (prefix command)"""
    guild = ctx.guild
    category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)

    if not category:
        open_tickets = 0
    else:
        open_tickets = len([ch for ch in category.channels if ch.name.startswith("ticket-")])

    embed = discord.Embed(
        title="📊 Support Ticket Statistics",
        color=discord.Color.green()
    )
    embed.add_field(name="🎫 Open Tickets", value=str(open_tickets), inline=True)
    embed.add_field(name="📈 Total Created", value=str(TICKET_COUNTER), inline=True)

    if category:
        embed.add_field(name="📂 Category", value=f"#{category.name}", inline=True)

    embed.set_footer(text="ScubaAI Support System")

    await ctx.send(embed=embed)


# === COMBINED OVERVIEW COMMAND ===
@bot.command()
@commands.has_permissions(administrator=True)
async def overview(ctx):
    """Get an overview of all bot systems"""
    guild = ctx.guild

    # Trading stats
    temp_count = len(bot.temp_sales)
    active_count = len(bot.active_listings)
    pending_vouches_count = len(bot.pending_vouches)

    # Ticket stats
    ticket_category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
    open_tickets = 0
    if ticket_category:
        open_tickets = len([ch for ch in ticket_category.channels if ch.name.startswith("ticket-")])

    # AI stats
    ai_sessions = len(user_chat_sessions) if AI_READY else 0

    embed = discord.Embed(
        title="🤖 Bot System Overview",
        description="Complete status of all bot systems",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow()
    )

    # Trading System
    embed.add_field(
        name="💼 Trading System",
        value=(
            f"Pending listings: {temp_count}\n"
            f"Active listings: {active_count}\n"
            f"Pending vouches: {pending_vouches_count}\n"
            f"Total users: {len(bot.user_stats)}"
        ),
        inline=True
    )

    # Support System
    embed.add_field(
        name="🎫 Support System",
        value=(
            f"Open tickets: {open_tickets}\n"
            f"Total created: {TICKET_COUNTER}\n"
            f"Category: {ticket_category.name if ticket_category else 'Not found'}"
        ),
        inline=True
    )

    # AI System
    embed.add_field(
        name="🤖 AI System",
        value=(
            f"Status: {'✅ Online' if AI_READY else '❌ Offline'}\n"
            f"Active sessions: {ai_sessions}\n"
            f"Model: {'gemini-1.5-flash' if AI_READY else 'N/A'}"
        ),
        inline=True
    )

    embed.set_footer(text=f"Requested by {ctx.author}")
    await ctx.send(embed=embed)


# === Event Handlers ===
@bot.event
async def on_message(message: discord.Message):
    """Handle DM messages for listing images and AI chat"""
    if message.author.bot:
        return

    # Handle DM image submissions for trading
    if isinstance(message.channel, discord.DMChannel) and message.attachments:
        sale = bot.temp_sales.get(message.author.id)
        if not sale:
            embed = discord.Embed(
                title="❌ No Active Listing",
                description=(
                    "You don't have an active listing waiting for images.\n"
                    "Please start a new listing in the server using the trading panel."
                ),
                color=discord.Color.red()
            )
            await message.channel.send(embed=embed)
            return

        # Check if listing expired
        if sale.get("expires_at", datetime.utcnow()) <= datetime.utcnow():
            bot.temp_sales.pop(message.author.id, None)
            embed = discord.Embed(
                title="⏰ Listing Expired",
                description=(
                    "Your listing has expired. Please start a new one if you still want to list your account.\n"
                    "Listings expire 10 minutes after creation to keep the marketplace fresh."
                ),
                color=discord.Color.orange()
            )
            await message.channel.send(embed=embed)
            return

        # Validate image count
        if len(message.attachments) > 3:
            await message.channel.send("❌ Please send a maximum of 3 screenshots.")
            return

        # Determine target channel
        target_channel = (
            bot.get_channel(OSRS_MAIN_CHANNEL_ID)
            if sale["account_type"].startswith("Main")
            else bot.get_channel(OSRS_IRON_CHANNEL_ID)
        )

        if not target_channel:
            await message.channel.send("❌ Could not find the appropriate listing channel. Contact staff.")
            logger.error("Target listing channel not found")
            return

        try:
            # Create the listing
            view = TradeView(bot, seller=sale["user"], sale_data=sale)

            # Store the first image URL for the embed
            if message.attachments:
                sale["image_url"] = message.attachments[0].url

            # Build and send the main listing embed
            embed = build_listing_embed(sale, message)
            embed_message = await target_channel.send(embed=embed, view=view)

            # Store message references for future use
            sale["listing_message_id"] = embed_message.id
            sale["listing_channel_id"] = embed_message.channel.id
            sale["extra_message_ids"] = []

            # Create a unique listing ID
            listing_id = str(uuid.uuid4())
            sale["listing_id"] = listing_id
            bot.active_listings[listing_id] = sale

            # Send additional images as separate embeds
            if len(message.attachments) > 1:
                for i, attachment in enumerate(message.attachments[1:3], start=2):
                    img_embed = discord.Embed(
                        title=f"📷 Additional Screenshot #{i}",
                        description=f"**Seller:** {sale['user'].mention}\n**Account:** {sale['account_type']}",
                        color=discord.Color.gold()
                    )
                    img_embed.set_image(url=attachment.url)
                    img_embed.set_footer(text="Part of the listing above")

                    img_msg = await target_channel.send(embed=img_embed)
                    sale["extra_message_ids"].append(img_msg.id)

            # Success message
            success_embed = discord.Embed(
                title="✅ Listing Posted Successfully!",
                description=(
                    f"Your **{sale['account_type']}** listing has been posted in {target_channel.mention}!\n\n"
                    f"**Price:** {sale['price']}\n"
                    f"**Listing ID:** {listing_id[:8]}\n\n"
                    "Buyers can now contact you through the secure trade system.\n"
                    "You can edit or cancel your listing using the buttons on your post."
                ),
                color=discord.Color.green()
            )
            success_embed.set_footer(text="Good luck with your sale!")
            await message.reply(embed=success_embed)

            # Clean up temp storage
            bot.temp_sales.pop(message.author.id, None)

            logger.info(f"Listing posted successfully by {message.author}: {listing_id}")

        except Exception as e:
            logger.error(f"Error posting listing: {e}")
            await message.channel.send(
                "❌ **Error posting your listing.**\n"
                "Please try again or contact staff if the problem persists."
            )
        return

    # Handle AI chat with Gemini
    if message.channel.id == AI_CHANNEL_ID:
        if not AI_READY:
            status_msg = "AI chat is currently unavailable."
            if not GEMINI_AVAILABLE:
                status_msg += " The google-generativeai library is not installed."
            elif not GEMINI_API_KEY:
                status_msg += " The GEMINI_API_KEY is not configured."
            else:
                status_msg += " There was an error initializing the AI service."

            await message.reply(f"⚠️ {status_msg}")
            return

        async with message.channel.typing():
            try:
                # Get or create chat session for user (maintains conversation context)
                if message.author.id not in user_chat_sessions:
                    user_chat_sessions[message.author.id] = gemini_model.start_chat(history=[])
                    logger.info(f"Created new chat session for {message.author}")

                chat_session = user_chat_sessions[message.author.id]

                # Limit message length
                prompt = message.content[:2000] if len(message.content) > 2000 else message.content

                # Send the prompt to Gemini
                response = await asyncio.to_thread(chat_session.send_message, prompt)
                ai_reply = response.text

                # Validate response
                if not ai_reply or ai_reply.strip() == "":
                    raise ValueError("Empty response from Gemini")

                # Limit response length for Discord
                if len(ai_reply) > 2000:
                    ai_reply = ai_reply[:1997] + "..."

                await message.reply(ai_reply)
                logger.info(f"Successfully sent Gemini response to {message.author}")

            except Exception as e:
                logger.error(f"Gemini API error for {message.author.id}: {type(e).__name__}: {e}")

                # Handle specific error types
                error_msg = str(e).lower()
                if "quota" in error_msg or "limit" in error_msg:
                    await message.reply("⚠️ AI service quota exceeded. Please try again later.")
                elif "safety" in error_msg or "blocked" in error_msg:
                    await message.reply("⚠️ Your message was blocked by safety filters. Please try rephrasing.")
                elif "invalid" in error_msg or "api" in error_msg:
                    await message.reply("⚠️ API error occurred. Please try again later.")
                else:
                    await message.reply(f"⚠️ AI error: {str(e)[:100]}...")

                # Clear the problematic chat session
                if message.author.id in user_chat_sessions:
                    del user_chat_sessions[message.author.id]
                    logger.info(f"Cleared chat session for {message.author} due to error")

    await bot.process_commands(message)


@bot.event
async def on_ready():
    """Bot startup event"""
    # Add persistent views for both systems
    bot.add_view(SaleView())
    bot.add_view(TradeView(bot, None))  # For persistent trade buttons
    bot.add_view(TicketView())  # For persistent ticket creation
    bot.add_view(CloseTicketView())  # For persistent ticket closing
    bot.add_view(ConfirmCloseView())  # For persistent close confirmation

    # Start cleanup task
    if not hasattr(bot, '_cleanup_started'):
        bot.loop.create_task(cleanup_expired_listings())
        bot._cleanup_started = True

    print(f"✅ {bot.user} is online and ready!")
    print(f"📊 Trading System:")
    print(f"   - Active temp sales: {len(bot.temp_sales)}")
    print(f"   - Active listings: {len(bot.active_listings)}")
    print(f"   - Pending vouches: {len(bot.pending_vouches)}")
    print(f"🎫 Support System:")
    print(f"   - Total tickets created: {TICKET_COUNTER}")
    print(f"🤖 AI System:")
    print(f"   - Gemini status: {'✅ Ready' if AI_READY else '❌ Not available'}")
    print(f"   - Active sessions: {len(user_chat_sessions) if AI_READY else 0}")

    # Sync slash commands
    try:
        print("🔄 Syncing application commands...")
        synced = await bot.tree.sync()
        print(f"✅ Successfully synced {len(synced)} slash command(s)")
        for command in synced:
            print(f"   - /{command.name}")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")
        logger.error(f"Command sync error: {e}")

    logger.info(f"Bot started successfully as {bot.user}")


@bot.event
async def on_command_error(ctx, error):
    """Handle command errors"""
    if isinstance(error, commands.MissingPermissions):
        embed = discord.Embed(
            title="❌ Missing Permissions",
            description="You don't have permission to use this command.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
    elif isinstance(error, commands.CommandNotFound):
        return  # Ignore unknown commands
    elif isinstance(error, commands.BadArgument):
        embed = discord.Embed(
            title="❌ Invalid Argument",
            description="Please check your command arguments and try again.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
    else:
        logger.error(f"Unexpected command error: {error}")
        embed = discord.Embed(
            title="❌ Unexpected Error",
            description="An unexpected error occurred. Please try again later.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)


@bot.event
async def on_error(event, *args, **kwargs):
    """Handle general bot errors"""
    logger.error(f"Bot error in {event}: {args}, {kwargs}")


# === Error handling for slash commands ===
@ticket_panel_slash.error
async def ticket_panel_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You don't have permission to use this command!", ephemeral=True)
    else:
        logger.error(f"Error in ticket_panel: {error}")
        await interaction.response.send_message("❌ An error occurred. Please try again later.", ephemeral=True)


@ticket_stats_slash.error
async def ticket_stats_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You don't have permission to use this command!", ephemeral=True)
    else:
        logger.error(f"Error in ticket_stats: {error}")
        await interaction.response.send_message("❌ An error occurred. Please try again later.", ephemeral=True)


# Manual sync command (for debugging)
@bot.tree.command(name="sync", description="Manually sync application commands")
async def sync_commands(interaction: discord.Interaction):
    if not (interaction.user.guild_permissions.administrator or interaction.user.id == interaction.guild.owner_id):
        await interaction.response.send_message("❌ You don't have permission to use this command!", ephemeral=True)
        return

    try:
        synced = await bot.tree.sync()
        await interaction.response.send_message(f"✅ Successfully synced {len(synced)} application commands!",
                                                ephemeral=True)
        logger.info(f"Commands manually synced by {interaction.user}")
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to sync commands: {e}", ephemeral=True)
        logger.error(f"Manual sync failed: {e}")


# === Cleanup Tasks ===
async def cleanup_expired_listings():
    """Background task to cleanup expired listings"""
    while not bot.is_closed():
        try:
            now = datetime.utcnow()
            expired_sales = []

            for user_id, sale in bot.temp_sales.items():
                if sale.get("expires_at", now) <= now:
                    expired_sales.append(user_id)

            for user_id in expired_sales:
                expired_sale = bot.temp_sales.pop(user_id, None)
                if expired_sale:
                    try:
                        user = expired_sale["user"]
                        dm = await user.create_dm()
                        embed = discord.Embed(
                            title="⏰ Listing Expired",
                            description=(
                                "Your account listing has expired because no images were submitted within 10 minutes.\n"
                                "Please start a new listing if you still want to sell your account."
                            ),
                            color=discord.Color.orange()
                        )
                        await dm.send(embed=embed)
                    except discord.Forbidden:
                        pass

            if expired_sales:
                logger.info(f"Cleaned up {len(expired_sales)} expired temporary listings")

        except Exception as e:
            logger.error(f"Error in cleanup task: {e}")

        await asyncio.sleep(300)  # Run every 5 minutes


# === Run Bot ===
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ERROR: DISCORD_TOKEN environment variable not set!")
        exit(1)

    try:
        print("🚀 Starting ScubaAI Bot...")
        print("📋 Systems loading:")
        print("   - Trading System ✅")
        print("   - Support Ticket System ✅")
        print(f"   - AI Chat System {'✅' if AI_READY else '❌'}")
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"❌ Failed to start bot: {e}")