import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import re
import asyncio
import uuid
import logging
import json
from datetime import datetime, timedelta
try:
    from HuggingChatAPI import SimpleHugChat
    AI_AVAILABLE = True
except ModuleNotFoundError:
    AI_AVAILABLE = False
    print("⚠️ Warning: HuggingChatAPI module not found. AI features disabled.")


load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.dm_messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === Configuration - Replace with your actual IDs ===
OSRS_MAIN_CHANNEL_ID = 1393681123355394058
OSRS_IRON_CHANNEL_ID = 1393671722636546088
STAFF_ROLE_ID = 1399572599054536866
TRADE_CATEGORY_ID = 1402544026032541768
COMPLETED_SALES_CHANNEL_ID = 1402544168034766850
VOUCH_LOG_CHANNEL_ID = 1399553110841622660
AI_CHANNEL_ID = 1400157457774411916

# In-memory storage (consider using a database for production)
bot.temp_sales = {}
bot.active_listings = {}  # {listing_id: sale_data}
bot.pending_vouches = {}
bot.user_stats = {}  # {user_id: {"sales": 0, "purchases": 0, "rating": 0}}
if AI_AVAILABLE:
    user_chatbots = {}


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


# === UI Components ===
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


# === Event Handlers ===
@bot.event
async def on_message(message: discord.Message):
    """Handle DM messages for listing images"""
    if message.author.bot:
        return

    # Handle DM image submissions
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

    await bot.process_commands(message)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.id == AI_CHANNEL_ID:
        if not AI_AVAILABLE:
            await message.reply(
                "⚠️ Sorry, AI chat feature is currently unavailable. The HuggingChatAPI module is not installed.")
            return

        async with message.channel.typing():
            try:
                # Initialize chatbot if needed
                if message.author.id not in user_chatbots:
                    try:
                        user_chatbots[message.author.id] = SimpleHugChat()
                        logger.info(f"Created new chatbot instance for user {message.author.id}")
                    except Exception as init_error:
                        logger.error(f"Failed to initialize chatbot for {message.author.id}: {init_error}")
                        await message.reply("⚠️ Failed to initialize AI chat session. Please try again later.")
                        return

                chatbot = user_chatbots[message.author.id]

                # Add timeout and better error handling
                try:
                    # Limit message length to prevent issues
                    prompt = message.content[:2000] if len(message.content) > 2000 else message.content

                    ai_reply = chatbot.send_prompt(prompt)

                    # More comprehensive response validation
                    if ai_reply is None:
                        raise ValueError("AI returned None response")

                    if not isinstance(ai_reply, str):
                        # Try to convert to string if possible
                        try:
                            ai_reply = str(ai_reply)
                        except:
                            raise ValueError(f"AI returned non-string response of type: {type(ai_reply)}")

                    if ai_reply.strip() == "":
                        raise ValueError("AI returned empty string response")

                    # Limit response length for Discord
                    if len(ai_reply) > 2000:
                        ai_reply = ai_reply[:1997] + "..."

                    await message.reply(ai_reply)
                    logger.info(f"Successfully sent AI response to {message.author}")

                except Exception as api_error:
                    # Log the specific error for debugging
                    logger.error(f"HuggingChatAPI error for {message.author.id}: {api_error}")

                    # Remove the problematic chatbot instance
                    if message.author.id in user_chatbots:
                        del user_chatbots[message.author.id]

                    # Provide more specific error messages
                    if "Expecting value" in str(api_error):
                        await message.reply(
                            "⚠️ The AI service returned an invalid response. This might be a temporary issue. Please try again.")
                    elif "timeout" in str(api_error).lower():
                        await message.reply("⚠️ The AI service timed out. Please try again with a shorter message.")
                    elif "rate limit" in str(api_error).lower():
                        await message.reply(
                            "⚠️ AI service rate limit reached. Please wait a moment before trying again.")
                    else:
                        await message.reply(f"⚠️ AI service error: {str(api_error)[:100]}... Please try again later.")

            except Exception as general_error:
                logger.error(f"General AI chat error for {message.author.id}: {general_error}")
                await message.reply("⚠️ An unexpected error occurred with the AI chat. Please try again later.")

    await bot.process_commands(message)


@bot.event
async def on_ready():
    """Bot startup event"""
    # Add persistent views
    bot.add_view(SaleView())
    bot.add_view(TradeView(bot, None))  # For persistent trade buttons

    # Start cleanup task
    if not hasattr(bot, '_cleanup_started'):
        bot.loop.create_task(cleanup_expired_listings())
        bot._cleanup_started = True

    print(f"✅ {bot.user} is online and ready!")
    print(f"📊 Active temp sales: {len(bot.temp_sales)}")
    print(f"📊 Active listings: {len(bot.active_listings)}")
    print(f"📊 Pending vouches: {len(bot.pending_vouches)}")

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
        await ctx.send(embed=embed, ephemeral=True)
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
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"❌ Failed to start bot: {e}")