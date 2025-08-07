
import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
load_dotenv()
import re
import asyncio
import uuid

TOKEN = os.getenv("DISCORD_TOKEN")  # Recommended: .env file

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.dm_messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === Replace with your actual channel IDs ===
OSRS_MAIN_CHANNEL_ID = 1393681123355394058
OSRS_IRON_CHANNEL_ID = 1393671722636546088
STAFF_ROLE_ID = 1399572599054536866  # Replace it with your real staff role ID
TRADE_CATEGORY_ID = 1402544026032541768  # Replace it with your private category ID
COMPLETED_SALES_CHANNEL_ID = 1402544168034766850  # Replace it with your real log channel ID
VOUCH_LOG_CHANNEL_ID = 1399553110841622660  # Replace it with your vouch log channel ID

# Temporary in-memory storage
bot.temp_sales = {}
bot.pending_sales = {}
bot.pending_vouches = {}  # {trade_id: {"buyer": {...}, "seller": {...}}}


# === Utility Functions ===
def extract_price_value(price_str: str) -> int:
    if not price_str:
        return 0
    price_str = price_str.lower().replace(",", "")
    match = re.search(r"(\d+)", price_str)
    return int(match.group(1)) if match else 0

# === UI Views ===
def build_listing_embed(sale, message):
    embed = discord.Embed(
        title=f"💼 {sale['account_type']} Listing",
        description=(
            "  📜 **Description**\n"
            f"  {sale['description'][:1024]}\n"

        ),
        color=discord.Color.from_rgb(255, 204, 0)  # OSRS gold/yellow
    )

    embed.add_field(name="💰 Listing Price", value=sale["price"], inline=True)
    embed.add_field(name="🧑 Seller", value=sale["user"].mention, inline=True)

    # Footer with profile picture
    embed.set_footer(
        text=f"Press the button below to initiate a trade with {sale['user'].display_name}.",
        icon_url=sale["user"].display_avatar.url if sale["user"].display_avatar else None
    )

    # Prefer attachments from sale["attachments"], fallback to message.attachments
    attachments = sale.get("attachments") or (message.attachments if message else [])
    if "image_url" in sale:
        embed.set_image(url=sale["image_url"])
    elif attachments:
        embed.set_image(url=attachments[0].url)

    return embed

class BuyView(discord.ui.View):
    def __init__(self, seller):
        super().__init__(timeout=None)
        self.seller = seller

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.green, custom_id="buy_account")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"{interaction.user.mention}, your interest has been sent to {self.seller.mention}!", ephemeral=True
        )
class SaleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # ✅ Mark it persistent

    @discord.ui.button(label="Post OSRS Main Account", style=discord.ButtonStyle.green, custom_id="post_osrs_main")
    async def post_main(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SaleModal("Main"))

    @discord.ui.button(label="Post OSRS Iron Account", style=discord.ButtonStyle.blurple, custom_id="post_osrs_iron")
    async def post_iron(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SaleModal("Ironman"))

# === Modal ===
class SaleModal(discord.ui.Modal):
    def __init__(self, account_type_prefix: str):
        super().__init__(title=f"{account_type_prefix} Account Sale Listing")
        self.account_type_prefix = account_type_prefix

        self.account_type = discord.ui.TextInput(
            label="Account Type", placeholder="e.g. Maxed Pure, Ironman"
        )
        self.price = discord.ui.TextInput(
            label="Price", placeholder="e.g. $150 or 250m OSRS GP"
        )
        self.description = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph
        )

        self.add_item(self.account_type)
        self.add_item(self.price)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            dm = await interaction.user.create_dm()
            await dm.send("✅ Please send 1–3 screenshots of the account here.")
            await interaction.response.send_message("📩 Check your DMs to complete your listing.", ephemeral=True)

            bot.temp_sales[interaction.user.id] = {
                "account_type": f"{self.account_type_prefix} - {self.account_type.value}",
                "price": self.price.value,
                "description": self.description.value,
                "user": interaction.user,
            }

        except discord.Forbidden:
            await interaction.response.send_message("❌ I can't DM you. Please enable DMs from server members.", ephemeral=True)

class TradeView(discord.ui.View):
    def __init__(self, bot, seller, sale_data=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.sale_data = sale_data
        self.seller = seller
        self.message = None

    @discord.ui.button(label="Trade", style=discord.ButtonStyle.green, custom_id="buy_account")
    async def trade(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        buyer = interaction.user
        seller = self.seller
        category = guild.get_channel(TRADE_CATEGORY_ID)
        staff_role = guild.get_role(STAFF_ROLE_ID)

        if not category or not staff_role:
            await interaction.response.send_message("❌ Config error. Contact staff.", ephemeral=True)
            return

        # Set up permissions
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            buyer: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            seller: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }

        # Create private trade channel
        trade_channel = await guild.create_text_channel(
            name=f"trade-{buyer.name}",
            category=category,
            overwrites=overwrites
        )

        await trade_channel.send(
            f"🔒 **Trade Started**\nBuyer: {buyer.mention}\nSeller: {seller.mention}\nStaff: {staff_role.mention}",
            view=TradeCompleteView(buyer, seller, sale_data=self.sale_data)
        )

        await interaction.response.send_message(
            f"✅ Trade channel created: {trade_channel.mention}", ephemeral=True
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel_listing(self, interaction: discord.Interaction, button: discord.ui.Button):
        sale = self.sale_data

        # Safety check
        if not sale:
            await interaction.response.send_message("❌ Sale data missing.", ephemeral=True)
            return

        if interaction.user.id != sale["user"].id:
            await interaction.response.send_message("❌ Only the seller can cancel this listing.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)  # Prevent "interaction failed"

        try:
            guild = interaction.guild
            listing_channel = guild.get_channel(sale.get("listing_channel_id"))
            listing_message_id = sale.get("listing_message_id")

            # Delete main listing message
            if listing_channel and listing_message_id:
                try:
                    msg = await listing_channel.fetch_message(listing_message_id)
                    await msg.delete()
                except discord.NotFound:
                    pass

            # Delete extra image messages
            for extra_id in sale.get("extra_message_ids", []):
                try:
                    msg = await listing_channel.fetch_message(extra_id)
                    await msg.delete()
                except discord.NotFound:
                    pass

        except Exception as e:
            print(f"❌ Error while cancelling listing: {e}")

        # Clean up temp storage
        bot.temp_sales.pop(sale["user"].id, None)

        try:
            await interaction.followup.send("✅ Listing has been canceled and deleted.", ephemeral=True)
        except Exception as e:
            print(f"❌ Failed to send confirmation: {e}")

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.secondary)
    async def edit_listing(self, interaction: discord.Interaction, button: discord.ui.Button):
        sale = self.sale_data

        if not sale or interaction.user.id != sale["user"].id:
            await interaction.response.send_message("❌ Only the seller can edit this listing.", ephemeral=True)
            return

        # Show modal with current values pre-filled
        await interaction.response.send_modal(EditSaleModal(sale, self))

class EditSaleModal(discord.ui.Modal, title="Edit Your Listing"):
    def __init__(self, sale_data, trade_view: TradeView):
        super().__init__()

        self.sale_data = sale_data
        self.trade_view = trade_view

        self.account_type = discord.ui.TextInput(
            label="Account Type",
            default=sale_data["account_type"],
            required=True
        )
        self.price = discord.ui.TextInput(
            label="Price",
            default=sale_data["price"],
            required=True
        )
        self.description = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            default=sale_data["description"],
            required=True
        )

        self.add_item(self.account_type)
        self.add_item(self.price)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction):
        # Update the sale data
        self.sale_data["account_type"] = self.account_type.value
        self.sale_data["price"] = self.price.value
        self.sale_data["description"] = self.description.value

        # Update the embed message
        try:
            channel = interaction.guild.get_channel(self.sale_data["listing_channel_id"])
            message = await channel.fetch_message(self.sale_data["listing_message_id"])
            new_embed = build_listing_embed(self.sale_data, message)
            await message.edit(embed=new_embed)
            await interaction.response.send_message("✅ Listing updated successfully!", ephemeral=True)
        except Exception as e:
            print(f"❌ Error updating listing: {e}")
            await interaction.response.send_message("❌ Failed to update listing. Contact staff.", ephemeral=True)

class TradeCompleteView(discord.ui.View):
    def __init__(self, buyer: discord.Member, seller: discord.Member, sale_data: dict = None):
        super().__init__(timeout=None)
        self.buyer = buyer
        self.seller = seller
        self.sale_data = sale_data or {}  # sale info: price, desc, type, etc.
        self.completed_by = set()

    @discord.ui.button(label="✅ Trade Completed", style=discord.ButtonStyle.green, custom_id="trade_complete")
    async def complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in {self.buyer.id, self.seller.id}:
            await interaction.response.send_message("❌ You're not part of this trade.", ephemeral=True)
            return

        self.completed_by.add(interaction.user.id)
        await interaction.response.send_message("✅ Marked as complete.", ephemeral=True)

        if self.buyer.id in self.completed_by and self.seller.id in self.completed_by:
            await self.finalize_trade(interaction)

    @discord.ui.button(label="❌ Trade Canceled", style=discord.ButtonStyle.danger, custom_id="trade_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.end_trade(interaction, completed=False)

    async def finalize_trade(self, interaction: discord.Interaction):
        await interaction.channel.send("🎉 Both parties marked the trade as complete! Archiving channel...")

        # Log the sale
        log_channel = interaction.guild.get_channel(COMPLETED_SALES_CHANNEL_ID)
        if log_channel:
            embed = discord.Embed(
                title="✅ Trade Completed",
                description=self.sale_data.get("description", "No description provided."),
                color=discord.Color.green()
            )
            embed.add_field(name="Account Type", value=self.sale_data.get("account_type", "Unknown"))
            embed.add_field(name="Price", value=self.sale_data.get("price", "Unknown"))
            embed.set_footer(text=f"Buyer: {self.buyer} • Seller: {self.seller}")
            await log_channel.send(embed=embed)

            # Delete main listing message and extra images
            try:
                listing_channel_id = self.sale_data.get("listing_channel_id")
                listing_message_id = self.sale_data.get("listing_message_id")
                listing_channel = interaction.guild.get_channel(listing_channel_id)
                if listing_channel:
                    # Delete main listing message
                    if listing_message_id:
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
                print(f"Failed to delete listing messages: {e}")

            await self.end_trade(interaction, completed=True)

    async def end_trade(self, interaction: discord.Interaction, completed: bool):
        channel = interaction.channel
        status = "completed" if completed else "canceled"

        # Lock the channel (optional — you can comment this out to test)
        try:
            await channel.edit(overwrites={
                role: discord.PermissionOverwrite(view_channel=False)
                for role in channel.overwrites
            })
        except Exception as e:
            print(f"⚠️ Failed to lock channel: {e}")

        # DM both users
        if completed:
            trade_id = str(uuid.uuid4())

            for user, role in [(self.buyer, "buyer"), (self.seller, "seller")]:
                try:
                    dm = await user.create_dm()
                    await dm.send(
                        f"📝 Please leave a vouch for your recent trade with "
                        f"{self.seller.display_name if role == 'buyer' else self.buyer.display_name}:",
                        view=StarRatingView(
                            rater=user,
                            role=role,
                            trade_id=trade_id,
                            other_party=self.seller if role == "buyer" else self.buyer,
                            account_info=self.sale_data
                        )
                    )
                except discord.Forbidden:
                    print(f"⚠️ Couldn't DM {user} for vouch request.")
        else:
            for user in (self.buyer, self.seller):
                try:
                    dm = await user.create_dm()
                    await dm.send("❌ Your trade was canceled. No vouch request will be sent.")
                except discord.Forbidden:
                    pass

        await asyncio.sleep(5)

        # Delete the channel (with proper error handling)
        try:
            await channel.delete(reason=f"Trade {status} closed and channel auto-deleted")
            print(f"✅ Channel {channel.name} deleted.")
        except Exception as e:
            print(f"❌ Failed to delete channel: {e}")

class VouchRequestView(discord.ui.View):
    def __init__(self, buyer, seller, sale_data):
        super().__init__(timeout=None)
        self.buyer = buyer
        self.seller = seller
        self.sale_data = sale_data

    @discord.ui.button(label="Leave a Vouch", style=discord.ButtonStyle.green, custom_id="leave_vouch_button")
    async def leave_vouch(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        if user.id not in (self.buyer.id, self.seller.id):
            await interaction.response.send_message("❌ You weren't in this trade.", ephemeral=True)
            return

        await interaction.response.send_modal(
            VouchModal(
                buyer=self.buyer,
                seller=self.seller,
                sale_data=self.sale_data,
                vouching_user=user
            )
        )

class VouchModal(discord.ui.Modal):
    def __init__(self, buyer, seller, sale_data, vouching_user):
        super().__init__(title="Leave your vouch")

        self.buyer = buyer
        self.seller = seller
        self.sale_data = sale_data
        self.vouching_user = vouching_user

        self.comment = discord.ui.TextInput(
            label="Your comment",
            style=discord.TextStyle.paragraph,
            required=False,
            placeholder="Write your feedback here..."
        )

        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction):
        # Handle submitted vouch here, e.g., save it or send to a channel
        await interaction.response.send_message("Thanks for your vouch!", ephemeral=True)

class StarRatingView(discord.ui.View):
    def __init__(self, rater, trade_id, role, other_party, account_info):
        super().__init__(timeout=300)
        self.rater = rater
        self.trade_id = trade_id
        self.role = role
        self.other_party = other_party
        self.account_info = account_info

        for i in range(1, 6):
            self.add_item(self.StarButton(i, self))  # Add buttons correctly

    class StarButton(discord.ui.Button):
        def __init__(self, stars: int, parent_view: "StarRatingView"):
            super().__init__(style=discord.ButtonStyle.primary, label="⭐" * stars, custom_id=f"rate_{stars}")
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

class VouchCommentModal(discord.ui.Modal, title="Leave a Vouch"):
    def __init__(self, stars, rater, trade_id, role, other_party, account_info):
        super().__init__()
        self.stars = stars
        self.rater = rater
        self.trade_id = trade_id
        self.role = role
        self.other_party = other_party
        self.account_info = account_info

        self.comment = discord.ui.TextInput(label="Comments", style=discord.TextStyle.paragraph, required=False, placeholder="What was your experience?")
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction):
        if self.trade_id not in bot.pending_vouches:
            bot.pending_vouches[self.trade_id] = {}

        bot.pending_vouches[self.trade_id][self.role] = {
            "rater": self.rater,
            "rating": self.stars,
            "comment": self.comment.value,
        }

        await interaction.response.send_message("✅ Thanks! Your vouch has been submitted.", ephemeral=True)

        if len(bot.pending_vouches[self.trade_id]) == 2:
            buyer = bot.pending_vouches[self.trade_id]["buyer"]
            seller = bot.pending_vouches[self.trade_id]["seller"]

            channel = interaction.client.get_channel(VOUCH_LOG_CHANNEL_ID)

            embed = discord.Embed(
                title="✅ Trade Vouch",
                color=discord.Color.green(),
                description=f"**Account:** {self.account_info['account_type']}\n**Price:** {self.account_info['price']}\n"
            )
            embed.add_field(
                name=f"Buyer - {'⭐' * buyer['rating']}",
                value=f"{buyer['rater'].mention}\n{buyer['comment'] or 'No comment provided.'}",
                inline=False
            )
            embed.add_field(
                name=f"Seller - {'⭐' * seller['rating']}",
                value=f"{seller['rater'].mention}\n{seller['comment'] or 'No comment provided.'}",
                inline=False
            )
            await channel.send(embed=embed)
            del bot.pending_vouches[self.trade_id]

# === Admin Setup Command ===
@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    embed = discord.Embed(
        title="Create a trade",
        description="Post your account using the buttons below.",
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Powered by ScubaAI")

    view = SaleView()  # ← Use the correct view for this panel

    await ctx.send(embed=embed, view=view)


# === DM Message Handler ===
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if isinstance(message.channel, discord.DMChannel) and message.attachments:
        sale = bot.temp_sales.get(message.author.id)
        if not sale:
            await message.channel.send("❌ You don't have an active listing. Please start one using the market button in the server.")
            return

        # Determine which channel to post in
        view_channel = (
            bot.get_channel(OSRS_MAIN_CHANNEL_ID)
            if sale["account_type"].startswith("Main")
            else bot.get_channel(OSRS_IRON_CHANNEL_ID)
        )

        if not view_channel:
            await message.channel.send("❌ Could not find the appropriate listing channel.")
            return

        view = TradeView(bot, seller=sale["user"], sale_data=sale)

        # Build the listing embed (with image if available)
        embed = build_listing_embed(sale, message)
        # Store image URL
        if message.attachments:
            sale["image_url"] = message.attachments[0].url
        embed_message = await view_channel.send(embed=embed, view=view)

        # Set message reference for later use
        view.message = embed_message
        attachments = message.attachments

        # Store message metadata for future deletion/editing
        sale["listing_message_id"] = embed_message.id
        sale["listing_channel_id"] = embed_message.channel.id

        sale["extra_message_ids"] = []

        if len(message.attachments) > 1:
            for i, attachment in enumerate(message.attachments[1:3], start=2):
                embed = discord.Embed(
                    title=f"📷 Additional Screenshot #{i}",
                    description="Provided by seller",
                    color=discord.Color.gold()
                )
                embed.set_image(url=attachment.url)
                img_msg = await view_channel.send(embed=embed)
                sale["extra_message_ids"].append(img_msg.id)

        await message.reply("✅ Your listing has been posted!")
        return

    await bot.process_commands(message)

@bot.event
async def on_ready():
    bot.add_view(SaleView())
    bot.add_view(TradeView(None, None))  # For persistent buttons
    print(f"Logged in as {bot.user}")

# === Start Bot ===
bot.run(TOKEN)  # Replace it with your bot token
