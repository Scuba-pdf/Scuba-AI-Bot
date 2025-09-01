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
from database import db, DatabaseManager

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

# === Configuration ===
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

# AI Setup
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
        user_chat_sessions = {}
        AI_READY = True
        print("✅ Gemini AI initialized successfully")
    except Exception as e:
        AI_READY = False
        print(f"⚠️ Failed to initialize Gemini: {e}")
else:
    AI_READY = False


# === Utility Functions ===
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
    pattern = r'^[\$]?\d+[kmb]?(\s*(gp|osrs|rs|gold|dollars?))?$'
    return bool(re.match(pattern, price_str.lower().strip().replace(',', '')))


# === Database Wrapper Functions ===
async def get_user_stats(user_id: int, username: str = None) -> dict:
    """Get user trading statistics"""
    return await db.get_user_stats(user_id, username)


async def update_user_stats(user_id: int, action: str, rating: int = None, username: str = None):
    """Update user statistics"""
    await db.update_user_stats(user_id, action, rating, username)


async def get_average_rating(user_id: int) -> float:
    """Calculate user's average rating"""
    return await db.get_average_rating(user_id)


# === UI Components ===
def build_listing_embed(sale, message=None):
    """Build the listing embed with improved formatting"""
    embed = discord.Embed(
        title=f"💼 {sale['account_type']}",
        description=sale['description'][:1024] if len(sale['description']) > 1024 else sale['description'],
        color=discord.Color.from_rgb(255, 204, 0)
    )

    embed.add_field(name="💰 Price", value=sale["price"], inline=True)

    # Handle user object vs user_id
    if hasattr(sale.get("user"), "mention"):
        user_mention = sale["user"].mention
        user_avatar = sale["user"].display_avatar.url if sale["user"].display_avatar else None
        user_name = sale["user"].display_name
    else:
        # If we only have user_id from database, we'll need to fetch the user
        user_mention = f"<@{sale.get('user_id', 'Unknown')}>"
        user_avatar = None
        user_name = "Unknown User"

    embed.add_field(name="🧑 Seller", value=user_mention, inline=True)

    embed.set_footer(
        text=f"Click 'Trade' to start a secure trade with {user_name}",
        icon_url=user_avatar
    )

    # Handle images
    if "image_url" in sale and sale["image_url"]:
        embed.set_image(url=sale["image_url"])
    elif message and message.attachments:
        embed.set_image(url=message.attachments[0].url)

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
        stats = await get_user_stats(interaction.user.id, interaction.user.display_name)
        rating = await get_average_rating(interaction.user.id)
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
        if not validate_price_format(self.price.value):
            await interaction.response.send_message(
                "❌ Invalid price format. Please use formats like: $150, 250m GP, 100k OSRS",
                ephemeral=True
            )
            return

        # Check for active listings using database
        user_active_listings = await db.get_user_active_listings(interaction.user.id)
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

            # Save to database instead of memory
            sale_data = {
                "account_type": f"{self.account_type_prefix} - {self.account_type.value}",
                "price": self.price.value,
                "description": self.description.value,
                "user": interaction.user,
                "created_at": datetime.utcnow(),
                "expires_at": datetime.utcnow() + timedelta(minutes=10)
            }

            await db.save_temp_sale(interaction.user.id, sale_data)

            # Auto-cleanup expired listings
            await asyncio.sleep(600)  # 10 minutes
            temp_sale = await db.get_temp_sale(interaction.user.id)
            if temp_sale:
                await db.delete_temp_sale(interaction.user.id)
                try:
                    await dm.send("❌ Your listing expired. Please start over if you still want to list your account.")
                except discord.Forbidden:
                    pass

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I can't send you a DM. Please enable DMs from server members and try again.",
                ephemeral=True
            )

async def finalize_listing(bot, user: discord.User, sale_data: dict, images: list[str]):
    """Finalize a listing once screenshots are received"""
    guild = bot.guilds[0]  # If multiple guilds, adjust accordingly
    # Pick correct channel based on account type
    channel_id = OSRS_MAIN_CHANNEL_ID if "Main" in sale_data["account_type"] else OSRS_IRON_CHANNEL_ID
    channel = guild.get_channel(channel_id)

    if not channel:
        logger.error("Listing channel not found.")
        return

    # Attach first image to embed
    if images:
        sale_data["image_url"] = images[0]

    # Send embed + buttons
    embed = build_listing_embed(sale_data)
    message = await channel.send(embed=embed, view=TradeView(bot, user, sale_data))

    # Add identifiers for DB
    sale_data["listing_id"] = str(uuid.uuid4())
    sale_data["listing_channel_id"] = channel.id
    sale_data["listing_message_id"] = message.id
    sale_data["user_id"] = user.id
    sale_data["extra_message_ids"] = []

    # Save to DB
    await db.create_active_listing(sale_data)
    await db.delete_temp_sale(user.id)  # cleanup temp record

    logger.info(f"✅ Listing finalized for {user} ({sale_data['listing_id']})")

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

        if buyer.id == seller.id:
            await interaction.response.send_message("❌ You can't trade with yourself!", ephemeral=True)
            return

        guild = interaction.guild
        category = guild.get_channel(TRADE_CATEGORY_ID)
        staff_role = guild.get_role(STAFF_ROLE_ID)

        if not category or not staff_role:
            await interaction.response.send_message("❌ Trade system not properly configured. Contact staff.",
                                                    ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            buyer: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            seller: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }

        try:
            trade_channel = await guild.create_text_channel(
                name=f"trade-{buyer.display_name[:10].lower()}",
                category=category,
                overwrites=overwrites,
                topic=f"Trade between {buyer.display_name} and {seller.display_name}"
            )

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

        except Exception as e:
            await interaction.response.send_message(
                "❌ Failed to create trade channel. Please try again or contact staff.",
                ephemeral=True
            )
            logger.error(f"Error creating trade channel: {e}")

    @discord.ui.button(label="❌ Cancel Listing", style=discord.ButtonStyle.danger, custom_id="cancel_listing")
    async def cancel_listing(self, interaction: discord.Interaction, button: discord.ui.Button):
        sale = self.sale_data
        if not sale or interaction.user.id != sale.get("user_id", sale.get("user", {}).id if hasattr(sale.get("user"),
                                                                                                     "id") else None):
            await interaction.response.send_message("❌ Only the seller can cancel this listing.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            # Delete from database
            if sale.get("listing_id"):
                await db.delete_active_listing(sale["listing_id"])

            # Delete listing messages
            await self.delete_listing_messages(interaction.guild, sale)
            await interaction.followup.send("✅ Your listing has been canceled and deleted.", ephemeral=True)

        except Exception as e:
            logger.error(f"Error canceling listing: {e}")
            await interaction.followup.send("❌ Error canceling listing. Contact staff if needed.", ephemeral=True)


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
            except (discord.NotFound, discord.Forbidden):
                pass

        # Delete extra image messages
        for extra_id in sale.get("extra_message_ids", []):
            try:
                msg = await listing_channel.fetch_message(extra_id)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

    @discord.ui.button(label="✏️ Edit Listing", style=discord.ButtonStyle.secondary, custom_id="edit_listing")
    async def edit_listing(self, interaction: discord.Interaction, button: discord.ui.Button):
        sale = self.sale_data

        if not sale or interaction.user.id != sale.get("user_id", sale.get("user", {}).id if hasattr(sale.get("user"),
                                                                                                     "id") else None):
            await interaction.response.send_message("❌ Only the seller can edit this listing.", ephemeral=True)
            return

        await interaction.response.send_modal(EditSaleModal(sale, self))


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

        try:
            # Update the sale data
            account_prefix = self.sale_data["account_type"].split(" - ")[0] if " - " in self.sale_data[
                "account_type"] else "Account"
            self.sale_data["account_type"] = f"{account_prefix} - {self.account_type.value}"
            self.sale_data["price"] = self.price.value
            self.sale_data["description"] = self.description.value

            # Update in database if it's an active listing
            if self.sale_data.get("listing_id"):
                try:
                    # Update the database directly with SQL
                    async with db.pool.acquire() as conn:
                        await conn.execute('''
                            UPDATE active_listings 
                            SET account_type = $1, price = $2, description = $3, updated_at = $4
                            WHERE listing_id = $5
                        ''',
                                           self.sale_data["account_type"],
                                           self.sale_data["price"],
                                           self.sale_data["description"],
                                           datetime.utcnow(),
                                           self.sale_data["listing_id"]
                                           )
                    logger.info(f"Updated listing in database: {self.sale_data['listing_id']}")
                except Exception as db_error:
                    logger.error(f"Database update error: {db_error}")
                    # Continue anyway - the message update might still work

            # Update the embed message
            channel = interaction.guild.get_channel(self.sale_data["listing_channel_id"])
            if not channel:
                await interaction.response.send_message("❌ Listing channel not found.", ephemeral=True)
                return

            try:
                message = await channel.fetch_message(self.sale_data["listing_message_id"])
                new_embed = build_listing_embed(self.sale_data, message)
                await message.edit(embed=new_embed)

                await interaction.response.send_message("✅ Listing updated successfully!", ephemeral=True)
                logger.info(f"Listing edited by {interaction.user}")

            except discord.NotFound:
                await interaction.response.send_message("❌ Original listing message not found.", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message("❌ I don't have permission to edit that message.",
                                                        ephemeral=True)

        except discord.HTTPException as http_error:
            logger.error(f"Discord API error updating listing: {http_error}")
            await interaction.response.send_message(f"❌ Discord error: {str(http_error)}", ephemeral=True)
        except Exception as e:
            logger.error(f"Unexpected error updating listing: {e}")
            await interaction.response.send_message("❌ An unexpected error occurred. Please try again.", ephemeral=True)


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

        try:
            # Update user statistics in database
            await update_user_stats(self.seller.id, "sale", username=self.seller.display_name)
            await update_user_stats(self.buyer.id, "purchase", username=self.buyer.display_name)

            # Save trade history
            await db.save_trade_history(
                self.trade_id,
                self.buyer.id,
                self.seller.id,
                self.sale_data.get("account_type", "Unknown"),
                self.sale_data.get("price", "Unknown"),
                self.sale_data.get("description", ""),
                interaction.channel.id
            )

            # Log the completed trade
            await self.log_completed_trade(interaction)

            # Delete listing from database and messages
            if self.sale_data.get("listing_id"):
                await db.delete_active_listing(self.sale_data["listing_id"])
            await self.delete_original_listing(interaction.guild)

            # Send vouch requests
            await self.send_vouch_requests()

            # Close the trade channel
            await self.end_trade(interaction, completed=True)

        except Exception as e:
            logger.error(f"Error finalizing trade: {e}")
            await interaction.followup.send("❌ Error completing trade. Please contact staff.", ephemeral=True)

    async def log_completed_trade(self, interaction):
        """Log the trade to the completed sales channel"""
        log_channel = interaction.guild.get_channel(COMPLETED_SALES_CHANNEL_ID)
        if not log_channel:
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
                    try:
                        msg = await listing_channel.fetch_message(listing_message_id)
                        await msg.delete()
                    except (discord.NotFound, discord.Forbidden):
                        pass

                    for extra_id in self.sale_data.get("extra_message_ids", []):
                        try:
                            extra_msg = await listing_channel.fetch_message(extra_id)
                            await extra_msg.delete()
                        except (discord.NotFound, discord.Forbidden):
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
            async for message in channel.history(limit=10):
                if message.author == interaction.client.user and message.embeds:
                    embed = message.embeds[0]
                    embed.color = discord.Color.red() if not completed else discord.Color.green()
                    embed.title = f"🔒 Trade {status.capitalize()}"
                    await message.edit(embed=embed, view=self)
                    break
        except Exception as e:
            logger.warning(f"Failed to update trade message: {e}")

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

        for i in range(1, 6):
            self.add_item(self.StarButton(i, self))

    class StarButton(discord.ui.Button):
        def __init__(self, stars: int, parent_view: "StarRatingView"):
            super().__init__(
                style=discord.ButtonStyle.primary,
                label="⭐" * stars,
                custom_id=f"rate_{stars}_{parent_view.trade_id[:8]}"  # Unique per trade
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
        try:
            # Save pending vouch to database
            await db.save_pending_vouch(
                self.trade_id,
                self.role,
                self.rater.id,
                self.other_party.id,
                self.stars,
                self.comment.value or "No comment provided."
            )

            # Update user stats with the rating
            await update_user_stats(self.other_party.id, "rating", self.stars, self.other_party.display_name)

            await interaction.response.send_message(
                f"✅ **Thank you for your {self.stars}⭐ rating!**\n"
                "Your review helps build trust in our trading community.",
                ephemeral=True
            )

            # Check if both parties have submitted vouches
            pending_vouches = await db.get_pending_vouches(self.trade_id)
            if len(pending_vouches) == 2:
                await self.post_complete_vouch(interaction, pending_vouches)

        except Exception as e:
            logger.error(f"Error submitting vouch: {e}")
            await interaction.response.send_message("❌ Error submitting review. Please try again.", ephemeral=True)

    async def post_complete_vouch(self, interaction, pending_vouches):
        """Post the complete vouch when both parties have rated"""
        try:
            buyer_vouch = pending_vouches.get("buyer")
            seller_vouch = pending_vouches.get("seller")

            if not buyer_vouch or not seller_vouch:
                return

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
                value=f"<@{buyer_vouch['rater_id']}>: {buyer_vouch['comment']}",
                inline=False
            )

            embed.add_field(
                name=f"💼 Seller Review - {'⭐' * seller_vouch['rating']} ({seller_vouch['rating']}/5)",
                value=f"<@{seller_vouch['rater_id']}>: {seller_vouch['comment']}",
                inline=False
            )

            await channel.send(embed=embed)

            # Complete the vouch in database
            await db.complete_vouch(self.trade_id)
            logger.info(f"Complete vouch posted for trade {self.trade_id}")

        except Exception as e:
            logger.error(f"Error posting complete vouch: {e}")


# === TICKET SYSTEM VIEWS ===
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Create Ticket', style=discord.ButtonStyle.green, emoji='🎫', custom_id="create_ticket_btn")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
        if not category:
            category = await guild.create_category(TICKET_CATEGORY_NAME)

        # Get ticket number from database
        total_tickets = await db.get_total_tickets_count()
        ticket_number = total_tickets + 1
        channel_name = f"ticket-{user.name}-{ticket_number}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        support_role = discord.utils.get(guild.roles, name=SUPPORT_ROLE_NAME)
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        try:
            ticket_channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites
            )

            # Save ticket to database
            ticket_id = await db.create_ticket(user.id, ticket_channel.id)

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
            embed.set_footer(text=f"Ticket #{ticket_id} - ScubaAI Support System")

            await ticket_channel.send(
                f"Welcome {user.mention}!",
                embed=embed,
                view=CloseTicketView()
            )

            await interaction.response.send_message(
                f"✅ **Ticket #{ticket_id} created successfully!**\nCheck {ticket_channel.mention} for support.",
                ephemeral=True
            )

            logger.info(f"Ticket #{ticket_id} created: {ticket_channel.name} by {user}")

        except Exception as e:
            logger.error(f"Error creating ticket: {e}")
            await interaction.response.send_message(
                "❌ Failed to create ticket. Please try again or contact staff.",
                ephemeral=True
            )


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Close Ticket', style=discord.ButtonStyle.red, emoji='🔒', custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not channel.name.startswith("ticket-"):
            await interaction.response.send_message("❌ This can only be used in ticket channels!", ephemeral=True)
            return

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

        await interaction.response.send_message(embed=embed, view=ConfirmCloseView(), ephemeral=True)


class ConfirmCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label='Yes, Close Ticket', style=discord.ButtonStyle.red, emoji='✅')
    async def confirm_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel

        try:
            # Update ticket status in database
            await db.close_ticket(channel.id, interaction.user.id)

            embed = discord.Embed(
                title="🔒 Ticket Closing",
                description=f"This ticket is being closed by {interaction.user.mention}.\nChannel will be deleted in 5 seconds...",
                color=discord.Color.red()
            )

            await interaction.response.send_message(embed=embed)
            await asyncio.sleep(5)
            await channel.delete(reason=f"Ticket closed by {interaction.user}")

        except Exception as e:
            logger.error(f"Error closing ticket: {e}")
            await interaction.response.send_message("❌ Error closing ticket.", ephemeral=True)

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.gray, emoji='❌')
    async def cancel_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("✅ Ticket close cancelled.", ephemeral=True)


# === Bot Commands ===
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

    view = SaleView()
    await ctx.send(embed=embed, view=view)


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clean_expired(ctx):
    """Clean up expired temporary sales"""
    try:
        expired_count = await db.cleanup_expired_temp_sales()
        await ctx.send(f"✅ Cleaned up {expired_count} expired temporary listings.")
    except Exception as e:
        logger.error(f"Error cleaning expired listings: {e}")
        await ctx.send("❌ Error cleaning expired listings.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def stats(ctx, user: discord.Member = None):
    """View trading statistics for a user"""
    target_user = user or ctx.author
    try:
        stats = await get_user_stats(target_user.id, target_user.display_name)
        rating = await get_average_rating(target_user.id)
        rating_display = f"{'⭐' * int(rating)} ({rating}/5)" if rating > 0 else "No ratings yet"

        embed = discord.Embed(title=f"📊 Trading Statistics", color=discord.Color.blue())
        embed.set_author(name=target_user.display_name, icon_url=target_user.display_avatar.url)

        embed.add_field(name="🛒 Total Sales", value=str(stats['sales']), inline=True)
        embed.add_field(name="💰 Total Purchases", value=str(stats['purchases']), inline=True)
        embed.add_field(name="🔄 Total Trades", value=str(stats['sales'] + stats['purchases']), inline=True)
        embed.add_field(name="⭐ Average Rating", value=rating_display, inline=True)
        embed.add_field(name="📝 Total Reviews", value=str(stats['rating_count']), inline=True)

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

    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        await ctx.send("❌ Error retrieving statistics.")


@bot.command()
@commands.has_permissions(administrator=True)
async def manual_vouch(ctx, rater: discord.Member, rated_user: discord.Member, rating: int, *,
                       comment: str = "Manual vouch"):
    """Manually add a vouch/rating (admin only)"""
    if rating < 1 or rating > 5:
        await ctx.send("❌ Rating must be between 1 and 5.")
        return

    try:
        # Generate a unique trade ID for manual vouches
        manual_trade_id = f"manual-{uuid.uuid4().hex[:8]}"

        # Add the vouch directly to completed vouches
        async with db.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO vouches 
                (trade_id, rater_id, rated_user_id, rating, comment, role)
                VALUES ($1, $2, $3, $4, $5, $6)
            ''', manual_trade_id, rater.id, rated_user.id, rating, comment, "manual")

        # Update user stats
        await update_user_stats(rated_user.id, "rating", rating, rated_user.display_name)

        # Log to vouch channel
        channel = ctx.guild.get_channel(VOUCH_LOG_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="📝 Manual Vouch Added",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(
                name="🔧 Manual Entry",
                value=f"**Trade ID:** {manual_trade_id}\n**Added by:** {ctx.author.mention}",
                inline=False
            )
            embed.add_field(
                name=f"⭐ Rating - {'⭐' * rating} ({rating}/5)",
                value=f"**From:** {rater.mention}\n**To:** {rated_user.mention}\n**Comment:** {comment}",
                inline=False
            )

            await channel.send(embed=embed)

        embed = discord.Embed(
            title="✅ Manual Vouch Added",
            description=f"Added {rating}⭐ rating from {rater.mention} to {rated_user.mention}",
            color=discord.Color.green()
        )
        embed.add_field(name="Comment", value=comment, inline=False)
        embed.add_field(name="Trade ID", value=manual_trade_id, inline=True)

        await ctx.send(embed=embed)
        logger.info(f"Manual vouch added by {ctx.author}: {rater} -> {rated_user} ({rating}⭐)")

    except Exception as e:
        logger.error(f"Error adding manual vouch: {e}")
        await ctx.send("❌ Error adding manual vouch.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def remove_vouch(ctx, vouch_id: str = None, rater: discord.Member = None, rated_user: discord.Member = None):
    """Remove a specific vouch (admin only)"""
    if not vouch_id and not (rater and rated_user):
        embed = discord.Embed(
            title="🗑️ Remove Vouch",
            description=(
                "Remove a vouch by trade ID or user pair:\n\n"
                "**By Trade ID:**\n"
                "`!remove_vouch trade_id_here`\n\n"
                "**By Users:**\n"
                "`!remove_vouch @rater @rated_user`"
            ),
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return

    try:
        async with db.pool.acquire() as conn:
            if vouch_id:
                # Remove by trade ID
                deleted = await conn.execute('''
                    DELETE FROM vouches WHERE trade_id = $1
                ''', vouch_id)

                if "DELETE 0" in deleted:
                    await ctx.send(f"❌ No vouch found with trade ID: {vouch_id}")
                    return

                await ctx.send(f"✅ Removed vouch with trade ID: {vouch_id}")

            elif rater and rated_user:
                # Remove by user pair - show options if multiple found
                vouches = await conn.fetch('''
                    SELECT trade_id, rating, comment, created_at 
                    FROM vouches 
                    WHERE rater_id = $1 AND rated_user_id = $2
                    ORDER BY created_at DESC
                ''', rater.id, rated_user.id)

                if not vouches:
                    await ctx.send(f"❌ No vouches found from {rater.mention} to {rated_user.mention}")
                    return

                if len(vouches) == 1:
                    # Only one vouch, remove it
                    await conn.execute('DELETE FROM vouches WHERE trade_id = $1', vouches[0]['trade_id'])
                    await ctx.send(f"✅ Removed vouch from {rater.mention} to {rated_user.mention}")
                else:
                    # Multiple vouches, show list
                    embed = discord.Embed(
                        title="🔍 Multiple Vouches Found",
                        description=f"Found {len(vouches)} vouches from {rater.mention} to {rated_user.mention}:",
                        color=discord.Color.blue()
                    )

                    for i, vouch in enumerate(vouches[:5], 1):
                        embed.add_field(
                            name=f"#{i} - {'⭐' * vouch['rating']}",
                            value=(
                                f"**Trade ID:** {vouch['trade_id']}\n"
                                f"**Date:** {vouch['created_at'].strftime('%Y-%m-%d')}\n"
                                f"**Comment:** {vouch['comment'][:50]}..."
                            ),
                            inline=True
                        )

                    embed.set_footer(text="Use !remove_vouch <trade_id> to remove a specific one")
                    await ctx.send(embed=embed)

        logger.info(f"Vouch removal by {ctx.author}: {vouch_id or f'{rater} -> {rated_user}'}")

    except Exception as e:
        logger.error(f"Error removing vouch: {e}")
        await ctx.send("❌ Error removing vouch.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def vouch_history(ctx, user: discord.Member, limit: int = 10):
    """View detailed vouch history for a user"""
    if limit > 50:
        limit = 50

    try:
        async with db.pool.acquire() as conn:
            # Get vouches received by user
            received_vouches = await conn.fetch('''
                SELECT v.*, u.username as rater_username
                FROM vouches v
                LEFT JOIN user_stats u ON v.rater_id = u.user_id
                WHERE v.rated_user_id = $1
                ORDER BY v.created_at DESC
                LIMIT $2
            ''', user.id, limit)

            # Get vouches given by user
            given_vouches = await conn.fetch('''
                SELECT v.*, u.username as rated_username
                FROM vouches v
                LEFT JOIN user_stats u ON v.rated_user_id = u.user_id
                WHERE v.rater_id = $1
                ORDER BY v.created_at DESC
                LIMIT $2
            ''', user.id, limit)

        embed = discord.Embed(
            title=f"📜 Vouch History for {user.display_name}",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        # Received vouches
        if received_vouches:
            received_text = []
            for vouch in received_vouches[:5]:
                rater_mention = f"<@{vouch['rater_id']}>"
                date = vouch['created_at'].strftime('%m/%d/%y')
                stars = '⭐' * vouch['rating']
                comment = vouch['comment'][:30] + "..." if len(vouch['comment']) > 30 else vouch['comment']
                received_text.append(f"{stars} {rater_mention} ({date})\n*{comment}*")

            embed.add_field(
                name=f"📨 Received ({len(received_vouches)} total)",
                value="\n\n".join(received_text) or "None",
                inline=False
            )

        # Given vouches
        if given_vouches:
            given_text = []
            for vouch in given_vouches[:5]:
                rated_mention = f"<@{vouch['rated_user_id']}>"
                date = vouch['created_at'].strftime('%m/%d/%y')
                stars = '⭐' * vouch['rating']
                comment = vouch['comment'][:30] + "..." if len(vouch['comment']) > 30 else vouch['comment']
                given_text.append(f"{stars} to {rated_mention} ({date})\n*{comment}*")

            embed.add_field(
                name=f"📤 Given ({len(given_vouches)} total)",
                value="\n\n".join(given_text) or "None",
                inline=False
            )

        if not received_vouches and not given_vouches:
            embed.description = "No vouch history found."

        await ctx.send(embed=embed)

    except Exception as e:
        logger.error(f"Error getting vouch history: {e}")
        await ctx.send("❌ Error retrieving vouch history.")

@bot.command()
@commands.has_permissions(administrator=True)
async def clear_vouches(ctx, user: discord.Member, confirmation: str = None):
    """Clear all vouch history for a user"""
    if confirmation != "CONFIRM":
        embed = discord.Embed(
            title="⚠️ Vouch History Clearing",
            description=(
                f"This will **permanently delete** all vouch history for {user.mention}.\n\n"
                f"To confirm, use: `!clear_vouches {user.mention} CONFIRM`"
            ),
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return

    try:
        old_stats = await get_user_stats(user.id)
        old_rating = await get_average_rating(user.id)

        await db.clear_user_vouches(user.id)

        embed = discord.Embed(
            title="✅ Vouch History Cleared",
            description=f"Successfully cleared vouch history for {user.mention}",
            color=discord.Color.green()
        )
        embed.add_field(
            name="📊 Previous Stats",
            value=f"Rating: {old_rating}/5 ⭐\nReviews: {old_stats['rating_count']}",
            inline=True
        )

        await ctx.send(embed=embed)
        logger.info(f"Vouch history cleared for {user} by {ctx.author}")

    except Exception as e:
        logger.error(f"Error clearing vouches: {e}")
        await ctx.send("❌ Error clearing vouch history.")


@bot.command()
@commands.has_permissions(administrator=True)
async def reset_user_stats(ctx, user: discord.Member, confirmation: str = None):
    """Completely reset all stats for a user"""
    if confirmation != "CONFIRM":
        embed = discord.Embed(
            title="⚠️ Complete User Reset",
            description=(
                f"This will **permanently delete** ALL data for {user.mention}.\n\n"
                f"To confirm, use: `!reset_user_stats {user.mention} CONFIRM`"
            ),
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    try:
        old_stats = await get_user_stats(user.id)
        old_rating = await get_average_rating(user.id)

        await db.reset_user_completely(user.id)

        embed = discord.Embed(
            title="✅ User Data Completely Reset",
            description=f"All data for {user.mention} has been permanently removed",
            color=discord.Color.red()
        )
        embed.add_field(
            name="📊 Previous Stats",
            value=f"Sales: {old_stats['sales']}\nPurchases: {old_stats['purchases']}\nRating: {old_rating}/5",
            inline=True
        )

        await ctx.send(embed=embed)
        logger.info(f"Complete user reset for {user} by {ctx.author}")

    except Exception as e:
        logger.error(f"Error resetting user: {e}")
        await ctx.send("❌ Error resetting user data.")


@bot.command()
@commands.has_permissions(administrator=True)
async def overview(ctx):
    """Get an overview of all bot systems"""
    try:
        # Get database statistics
        user_listings = await db.get_user_active_listings(ctx.author.id)  # Sample call
        open_tickets = await db.get_open_tickets_count()
        total_tickets = await db.get_total_tickets_count()

        embed = discord.Embed(
            title="🤖 Bot System Overview",
            description="Complete status of all bot systems",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )

        embed.add_field(
            name="💼 Trading System",
            value=f"Database: ✅ Connected\nOpen tickets: {open_tickets}",
            inline=True
        )

        embed.add_field(
            name="🎫 Support System",
            value=f"Open tickets: {open_tickets}\nTotal created: {total_tickets}",
            inline=True
        )

        embed.add_field(
            name="🤖 AI System",
            value=f"Status: {'✅ Online' if AI_READY else '❌ Offline'}\nActive sessions: {len(user_chat_sessions) if AI_READY else 0}",
            inline=True
        )

        await ctx.send(embed=embed)

    except Exception as e:
        logger.error(f"Error getting overview: {e}")
        await ctx.send("❌ Error retrieving system overview.")


# === Ticket Commands ===
@bot.tree.command(name="ticket_panel", description="Create the support ticket panel")
@app_commands.default_permissions(administrator=True)
async def ticket_panel_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎫 Support Ticket System",
        description=(
            "Need help with trading, account issues, or have questions?\n"
            "Create a private support ticket using the button below!"
        ),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=TicketView())


@bot.tree.command(name="ticket_stats", description="View support ticket statistics")
@app_commands.default_permissions(administrator=True)
async def ticket_stats_slash(interaction: discord.Interaction):
    try:
        open_tickets = await db.get_open_tickets_count()
        total_tickets = await db.get_total_tickets_count()

        embed = discord.Embed(
            title="📊 Support Ticket Statistics",
            color=discord.Color.green()
        )
        embed.add_field(name="🎫 Open Tickets", value=str(open_tickets), inline=True)
        embed.add_field(name="📈 Total Created", value=str(total_tickets), inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    except Exception as e:
        logger.error(f"Error getting ticket stats: {e}")
        await interaction.response.send_message("❌ Error retrieving ticket statistics.", ephemeral=True)


# === Event Handlers ===
@bot.event
async def on_message(message: discord.Message):
    """Handle DM messages for listing images and AI chat"""
    if message.author.bot:
        return

    # Handle DM image submissions
    if isinstance(message.channel, discord.DMChannel) and message.attachments:
        temp_sale = await db.get_temp_sale(message.author.id)
        if not temp_sale:
            embed = discord.Embed(
                title="❌ No Active Listing",
                description="You don't have an active listing waiting for images.",
                color=discord.Color.red()
            )
            await message.channel.send(embed=embed)
            return

        # Check if expired
        if temp_sale.get("expires_at", datetime.utcnow()) <= datetime.utcnow():
            await db.delete_temp_sale(message.author.id)
            embed = discord.Embed(
                title="⏰ Listing Expired",
                description="Your listing has expired. Please start a new one.",
                color=discord.Color.orange()
            )
            await message.channel.send(embed=embed)
            return

        if len(message.attachments) > 3:
            await message.channel.send("❌ Please send a maximum of 3 screenshots.")
            return

        try:
            # Collect image URLs
            images = [attachment.url for attachment in message.attachments[:3]]

            # Use the existing finalize_listing function
            await finalize_listing(bot, message.author, temp_sale, images)

            # Send success message
            success_embed = discord.Embed(
                title="✅ Listing Posted Successfully!",
                description=f"Your **{temp_sale['account_type']}** listing has been posted!",
                color=discord.Color.green()
            )
            await message.reply(embed=success_embed)

        except Exception as e:
            logger.error(f"Error posting listing: {e}")
            await message.channel.send("❌ Error posting your listing. Please try again.")
            return


    # Handle AI chat
    elif message.channel.id == AI_CHANNEL_ID and AI_READY:
        async with message.channel.typing():
            try:
                if message.author.id not in user_chat_sessions:
                    user_chat_sessions[message.author.id] = gemini_model.start_chat(history=[])

                chat_session = user_chat_sessions[message.author.id]
                prompt = message.content[:2000]
                response = await asyncio.to_thread(chat_session.send_message, prompt)

                ai_reply = response.text
                if len(ai_reply) > 2000:
                    ai_reply = ai_reply[:1997] + "..."

                await message.reply(ai_reply)

            except Exception as e:
                logger.error(f"Gemini error: {e}")
                error_msg = str(e).lower()
                if "quota" in error_msg:
                    await message.reply("⚠️ AI service quota exceeded. Please try again later.")
                else:
                    await message.reply("⚠️ AI error occurred. Please try again later.")

                if message.author.id in user_chat_sessions:
                    del user_chat_sessions[message.author.id]

    await bot.process_commands(message)


@bot.event
async def on_ready():
    """Bot startup event"""
    print("🚀 Starting ScubaAI Bot...")

    try:
        # Initialize database
        await db.initialize()
        print("✅ Database connected successfully")

        # Load existing data from database
        print("📊 Loading data from database...")
        # Note: We don't need to load into memory anymore since we query directly

        # Add persistent views (only those with timeout=None and proper custom_ids)
        bot.add_view(SaleView())
        bot.add_view(TicketView())
        bot.add_view(CloseTicketView())

        # Note: TradeCompleteView and StarRatingView are not persistent (they have timeouts)
        # ConfirmCloseView is not persistent (has timeout=60)

        # Start cleanup task
        if not hasattr(bot, '_cleanup_started'):
            bot.loop.create_task(cleanup_task())
            bot._cleanup_started = True

        print(f"✅ {bot.user} is online and ready!")
        print(f"🤖 AI System: {'✅ Ready' if AI_READY else '❌ Not available'}")

        # Sync slash commands
        try:
            synced = await bot.tree.sync()
            print(f"✅ Successfully synced {len(synced)} slash commands")
        except Exception as e:
            print(f"❌ Failed to sync commands: {e}")

    except Exception as e:
        logger.error(f"Failed to initialize bot: {e}")
        print(f"❌ Failed to start bot: {e}")


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
        return
    else:
        logger.error(f"Command error: {error}")


# === Cleanup Tasks ===
async def cleanup_task():
    """Background cleanup task"""
    while not bot.is_closed():
        try:
            # Clean expired temp sales
            expired_count = await db.cleanup_expired_temp_sales()
            if expired_count > 0:
                logger.info(f"Cleaned up {expired_count} expired temporary listings")

            # Clean old listings (optional - listings older than 3 days)
            old_listings = await db.cleanup_expired_listings(hours=72)
            if old_listings > 0:
                logger.info(f"Cleaned up {old_listings} old listings")

        except Exception as e:
            logger.error(f"Error in cleanup task: {e}")

        await asyncio.sleep(300)  # Run every 5 minutes


# === Bot shutdown handler ===
async def shutdown():
    """Gracefully shutdown the bot and database connections"""
    print("🔄 Shutting down bot...")
    await db.close()
    print("✅ Database connections closed")


# === Run Bot ===
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ERROR: DISCORD_TOKEN environment variable not set!")
        exit(1)

    try:
        import atexit

        atexit.register(lambda: asyncio.create_task(shutdown()))

        print("📋 Systems loading:")
        print("   - Trading System ✅")
        print("   - Support Ticket System ✅")
        print(f"   - AI Chat System {'✅' if AI_READY else '❌'}")
        print("   - PostgreSQL Database ✅")

        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"❌ Failed to start bot: {e}")