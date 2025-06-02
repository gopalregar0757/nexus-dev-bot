import discord
from discord import ui, app_commands
from discord.ext import commands
import sqlite3
import datetime
import asyncio
import os
import sys
import json
from typing import Optional, List, Literal

# Initialize bot
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Environment variable handling
try:
    APPLICATION_ID = int(os.environ["APPLICATION_ID"])
    BOT_TOKEN = os.environ["BOT_TOKEN"]
    SUPPORT_ROLE_ID = int(os.environ.get("SUPPORT_ROLE_ID", "0") or 0)  # Handle empty strings
    LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "0") or 0)
except (ValueError, KeyError) as e:
    print(f"ERROR: Environment variable issue - {e}")
    print("Required variables: APPLICATION_ID and BOT_TOKEN")
    sys.exit(1)

bot = commands.Bot(command_prefix="!", intents=intents, application_id=APPLICATION_ID)

# Database setup
DB_PATH = os.environ.get("DB_PATH", "tickets.db")
conn = sqlite3.connect(DB_PATH, isolation_level=None)
c = conn.cursor()

# Create tables with improved schema
c.execute('''
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ticket_type TEXT,
    assigned_to INTEGER,
    priority TEXT DEFAULT 'medium',
    custom_data TEXT,
    guild_id INTEGER NOT NULL
)''')

c.execute('''
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    ticket_role_id INTEGER,
    category_id INTEGER,
    ping_role_id INTEGER
)''')

c.execute('''
CREATE TABLE IF NOT EXISTS custom_panels (
    panel_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    title TEXT NOT NULL,
    description TEXT,
    button_label TEXT DEFAULT 'Create Ticket',
    button_emoji TEXT,
    button_style TEXT DEFAULT 'green',
    allowed_roles TEXT,
    embed_color TEXT DEFAULT '#3aa55c'
)''')

c.execute('''
CREATE TABLE IF NOT EXISTS ticket_presets (
    preset_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    fields TEXT,
    button_label TEXT DEFAULT 'Create Ticket',
    button_emoji TEXT,
    button_style TEXT DEFAULT 'green',
    allowed_roles TEXT,
    embed_color TEXT DEFAULT '#3aa55c',
    UNIQUE(guild_id, name)
)''')

conn.commit()

# Configuration
DEFAULT_CATEGORY_NAME = "Support Tickets"
PRIORITIES = {"🟢 Low": "low", "🟡 Medium": "medium", "🔴 High": "high", "🚨 Critical": "critical"}

# Utility functions
async def send_popup(interaction: discord.Interaction, title: str, message: str, is_error: bool = False):
    embed = discord.Embed(
        title=title,
        description=message,
        color=discord.Color.red() if is_error else discord.Color.green()
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)

def get_next_ticket_number(guild_id: int) -> int:
    c.execute("SELECT COUNT(*) FROM tickets WHERE guild_id=?", (guild_id,))
    return c.fetchone()[0] + 1

async def log_action(guild_id: int, message: str):
    if LOG_CHANNEL_ID:
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                description=message,
                color=discord.Color.gold(),
                timestamp=datetime.datetime.now()
            )
            await channel.send(embed=embed)

async def create_transcript(channel: discord.TextChannel) -> str:
    transcript = []
    async for message in channel.history(limit=None, oldest_first=True):
        content = message.content
        if message.embeds:
            content += "\n[Embed Content]"
        if message.attachments:
            content += "\n" + "\n".join([a.url for a in message.attachments])
        transcript.append(f"{message.created_at} - {message.author.display_name}: {content}")
    
    filename = f"transcript-{channel.id}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(transcript))
    return filename

async def get_ticket_category(guild: discord.Guild) -> discord.CategoryChannel:
    c.execute("SELECT category_id FROM guild_config WHERE guild_id=?", (guild.id,))
    result = c.fetchone()
    
    if result and result[0]:
        category = guild.get_channel(result[0])
        if category:
            return category
    
    # Fallback to default category name
    category = discord.utils.get(guild.categories, name=DEFAULT_CATEGORY_NAME)
    if not category:
        category = await guild.create_category(DEFAULT_CATEGORY_NAME)
        c.execute("INSERT OR REPLACE INTO guild_config (guild_id, category_id) VALUES (?, ?)",
                  (guild.id, category.id))
        conn.commit()
    return category

async def has_ticket_permission(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    
    c.execute("SELECT ticket_role_id FROM guild_config WHERE guild_id=?", (interaction.guild.id,))
    result = c.fetchone()
    
    if not result or not result[0]:
        return False
    
    ticket_role = interaction.guild.get_role(result[0])
    return ticket_role in interaction.user.roles if ticket_role else False

async def get_allowed_roles(panel_id: Optional[int] = None, preset_id: Optional[int] = None) -> List[int]:
    if panel_id:
        c.execute("SELECT allowed_roles FROM custom_panels WHERE panel_id=?", (panel_id,))
    elif preset_id:
        c.execute("SELECT allowed_roles FROM ticket_presets WHERE preset_id=?", (preset_id,))
    else:
        return []
    
    result = c.fetchone()
    if not result or not result[0]:
        return []
    
    try:
        return json.loads(result[0])
    except (json.JSONDecodeError, TypeError):
        return []

async def check_panel_permission(interaction: discord.Interaction, panel_id: Optional[int] = None, preset_id: Optional[int] = None) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    
    allowed_roles = await get_allowed_roles(panel_id, preset_id)
    if not allowed_roles:
        return await has_ticket_permission(interaction)
    
    user_roles = [role.id for role in interaction.user.roles]
    return any(role_id in user_roles for role_id in allowed_roles)

# Modal for custom ticket creation
class AdvancedTicketModal(ui.Modal, title="Create Custom Ticket"):
    def __init__(self, panel_id: Optional[int] = None, preset_id: Optional[int] = None):
        super().__init__(timeout=900)
        self.panel_id = panel_id
        self.preset_id = preset_id
        self.fields_data = []
        
        # Load preset if available
        if preset_id:
            c.execute("SELECT title, description, fields FROM ticket_presets WHERE preset_id=?", (preset_id,))
            preset = c.fetchone()
            if preset:
                self.title = preset[0] or "Create Ticket"
                self.description = preset[1] or ""
                try:
                    self.fields_data = json.loads(preset[2]) if preset[2] else []
                except (json.JSONDecodeError, TypeError):
                    self.fields_data = []
        
        # Add fields from preset or default
        if self.fields_data:
            for field in self.fields_data:
                self.add_item(ui.TextInput(
                    label=field.get('name', 'Field'),
                    placeholder=field.get('placeholder', ''),
                    default=field.get('default', ''),
                    style=discord.TextStyle.paragraph if field.get('long', False) else discord.TextStyle.short,
                    required=field.get('required', True)
                ))
        else:
            # Default fields
            self.add_item(ui.TextInput(
                label="Subject",
                placeholder="Briefly describe your issue",
                style=discord.TextStyle.short,
                required=True
            ))
            self.add_item(ui.TextInput(
                label="Description",
                placeholder="Provide detailed information about your issue",
                style=discord.TextStyle.paragraph,
                required=True
            ))
            self.add_item(ui.TextInput(
                label="Additional Information",
                placeholder="Any other relevant details",
                style=discord.TextStyle.paragraph,
                required=False
            ))
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        if not await check_panel_permission(interaction, self.panel_id, self.preset_id):
            await send_popup(
                interaction,
                "❌ Permission Denied",
                "You don't have permission to create tickets!",
                is_error=True
            )
            return
        
        # Prepare custom data
        custom_data = {
            "title": self.title,
            "fields": {},
            "attachments": []
        }
        
        for child in self.children:
            if isinstance(child, ui.TextInput):
                custom_data["fields"][child.label] = child.value
        
        # Create the ticket
        await create_advanced_ticket(interaction, custom_data, self.panel_id, self.preset_id)

async def create_advanced_ticket(interaction: discord.Interaction, custom_data: dict, 
                               panel_id: Optional[int] = None, preset_id: Optional[int] = None):
    guild = interaction.guild
    category = await get_ticket_category(guild)
    ticket_number = get_next_ticket_number(guild.id)
    
    # Determine channel name
    if preset_id:
        c.execute("SELECT name FROM ticket_presets WHERE preset_id=?", (preset_id,))
        preset_name = c.fetchone()[0]
        channel_name = f"{preset_name}-{ticket_number}"
    elif panel_id:
        c.execute("SELECT title FROM custom_panels WHERE panel_id=?", (panel_id,))
        panel_title = c.fetchone()[0]
        channel_name = f"{panel_title.lower().replace(' ', '-')}-{ticket_number}"
    else:
        channel_name = f"ticket-{ticket_number}"
    
    channel_name = channel_name[:99]  # Discord channel name limit
    try:
        channel = await category.create_text_channel(channel_name)
    except discord.Forbidden:
        await send_popup(
            interaction,
            "❌ Permission Error",
            "Bot doesn't have permission to create channels!",
            is_error=True
        )
        return
    
    # Set permissions
    await channel.set_permissions(interaction.user, read_messages=True, send_messages=True)
    await channel.set_permissions(guild.default_role, read_messages=False)
    
    if SUPPORT_ROLE_ID:
        support_role = guild.get_role(SUPPORT_ROLE_ID)
        if support_role:
            await channel.set_permissions(support_role, read_messages=True, send_messages=True)
    
    # Create embed
    embed_color = discord.Color.green()
    color_hex = None
    
    if panel_id:
        c.execute("SELECT embed_color FROM custom_panels WHERE panel_id=?", (panel_id,))
        color_hex = c.fetchone()[0]
    elif preset_id:
        c.execute("SELECT embed_color FROM ticket_presets WHERE preset_id=?", (preset_id,))
        color_hex = c.fetchone()[0]
    
    if color_hex:
        try:
            embed_color = discord.Color.from_str(color_hex)
        except ValueError:
            pass
    
    embed = discord.Embed(
        title=f"Ticket #{ticket_number}: {custom_data.get('title', 'Support Ticket')}",
        color=embed_color,
        timestamp=datetime.datetime.now()
    )
    
    embed.add_field(name="Created by", value=interaction.user.mention, inline=False)
    
    for field_name, field_value in custom_data["fields"].items():
        if field_value:  # Only add non-empty fields
            embed.add_field(name=field_name, value=field_value[:1024], inline=False)
    
    # Mark ticket as claimed automatically
    assigned_to = SUPPORT_ROLE_ID if SUPPORT_ROLE_ID else interaction.user.id
    c.execute("UPDATE tickets SET status = 'claimed', assigned_to = ? WHERE channel_id = ?",
              (assigned_to, channel.id))
    conn.commit()
    
    embed.add_field(name="Status", value="🟡 Claimed", inline=False)
    if SUPPORT_ROLE_ID:
        support_role = guild.get_role(SUPPORT_ROLE_ID)
        if support_role:
            embed.add_field(name="Assigned To", value=support_role.mention, inline=False)
    
    # Create view with management buttons (without claim button)
    view = TicketManagementView()
    
    # Ping support role if available
    ping_content = interaction.user.mention
    if SUPPORT_ROLE_ID:
        support_role = guild.get_role(SUPPORT_ROLE_ID)
        if support_role:
            ping_content += f" {support_role.mention}"
    
    # Send the ticket message
    try:
        message = await channel.send(
            content=ping_content,
            embed=embed,
            view=view
        )
        await message.pin()
    except discord.Forbidden:
        await send_popup(
            interaction,
            "❌ Permission Error",
            "Bot doesn't have permission to send messages or pin!",
            is_error=True
        )
        return
    
    # Store in database
    c.execute('''
    INSERT INTO tickets 
    (user_id, channel_id, status, created_at, ticket_type, priority, custom_data, guild_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        interaction.user.id,
        channel.id,
        "claimed",  # Automatically mark as claimed
        datetime.datetime.now().isoformat(),
        "preset" if preset_id else "custom",
        "medium",
        json.dumps(custom_data),
        guild.id
    ))
    conn.commit()
    
    await interaction.followup.send(f"🎫 Ticket created: {channel.mention}", ephemeral=True)
    await log_action(guild.id, f"Ticket #{ticket_number} created by {interaction.user}")

class PriorityView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for label, value in PRIORITIES.items():
            self.add_item(PriorityButton(label=label, priority=value))


class PriorityButton(ui.Button):
    def __init__(self, label: str, priority: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=f"priority_{priority}")
        self.priority = priority

    async def callback(self, interaction: discord.Interaction):
        # Update priority in DB
        c.execute("UPDATE tickets SET priority = ? WHERE channel_id = ?", (self.priority, interaction.channel.id))
        conn.commit()

        await interaction.response.send_message(
            f"✅ Priority set to **{self.label}**.",
            ephemeral=True
        )
        await log_action(interaction.guild.id, f"Priority of ticket in {interaction.channel.mention} set to {self.label} by {interaction.user.mention}")


# Ticket management view (without claim button)
class TicketManagementView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @ui.button(label="Add User", style=discord.ButtonStyle.blurple, custom_id="ticket_add_user", emoji="👥")
    async def add_user(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AddUserModal())
    
    @ui.button(label="Set Priority", style=discord.ButtonStyle.gray, custom_id="ticket_set_priority", emoji="⚠️")
    async def set_priority(self, interaction: discord.Interaction, button: ui.Button):
        view = PriorityView()
        await interaction.response.send_message("Select ticket priority:", view=view, ephemeral=True)
    
    @ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="ticket_close", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        c.execute("UPDATE tickets SET status = ? WHERE channel_id = ?",
                  ("closed", interaction.channel.id))
        conn.commit()
        
        # Remove the original ticket management view
        pins = await interaction.channel.pins()
        if pins:
            ticket_message = pins[0]
            await ticket_message.edit(view=None)
        
        # Get creator ID for transcript DM
        c.execute("SELECT user_id FROM tickets WHERE channel_id = ?", (interaction.channel.id,))
        creator_id = c.fetchone()[0]
        
        # Send closed ticket panel
        view = ClosedTicketView(interaction.channel, creator_id)
        await interaction.response.send_message(
            "🔒 Ticket closed. Please choose an action:",
            view=view
        )
        await log_action(interaction.guild.id, f"Ticket closed by {interaction.user} in #{interaction.channel.name}")

# View for closed ticket actions
class ClosedTicketView(ui.View):
    def __init__(self, channel: discord.TextChannel, creator_id: int):
        super().__init__(timeout=None)
        self.channel = channel
        self.creator_id = creator_id
    
    @ui.button(label="🗑️ Delete Ticket", style=discord.ButtonStyle.red, custom_id="delete_ticket")
    async def delete_ticket(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        try:
            await self.channel.delete(reason="Ticket deleted via panel")
        except discord.Forbidden:
            await interaction.followup.send("❌ Bot doesn't have permission to delete this channel!", ephemeral=True)
    
    @ui.button(label="📥 Download Transcript", style=discord.ButtonStyle.gray, custom_id="download_transcript")
    async def download_transcript(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            transcript = await create_transcript(self.channel)
            await interaction.user.send(
                f"Transcript for ticket #{self.channel.name}:",
                file=discord.File(transcript, filename=f"transcript-{self.channel.name}.txt")
            )
            os.remove(transcript)
            await interaction.followup.send("✅ Transcript sent to your DMs!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ Couldn't send DM. Please check your privacy settings.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error generating transcript: {str(e)}", ephemeral=True)
    
    @ui.button(label="📩 DM Transcript to User", style=discord.ButtonStyle.green, custom_id="dm_transcript")
    async def dm_transcript_to_user(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        try:
            creator = await interaction.guild.fetch_member(self.creator_id)
            transcript = await create_transcript(self.channel)
            
            await creator.send(
                f"Transcript for your ticket in {interaction.guild.name}:",
                file=discord.File(transcript, filename=f"transcript-{self.channel.name}.txt")
            )
            os.remove(transcript)
            
            await interaction.followup.send(f"✅ Transcript sent to {creator.mention}!")
            await log_action(interaction.guild.id, 
                           f"Transcript for #{self.channel.name} sent to {creator} by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send(f"❌ Couldn't send DM to user.", ephemeral=True)
        except discord.NotFound:
            await interaction.followup.send("❌ User not found in the server.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

# Simple ticket creation (no modal)
class SimpleTicketView(ui.View):
    def __init__(self, panel_id: int):
        super().__init__(timeout=None)
        self.panel_id = panel_id
    
    @ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="simple_ticket_create")
    async def create_simple_ticket(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        if not await check_panel_permission(interaction, panel_id=self.panel_id):
            await send_popup(
                interaction,
                "❌ Permission Denied",
                "You don't have permission to create tickets!",
                is_error=True
            )
            return
        
        # Create simple ticket data
        custom_data = {
            "title": "Support Ticket",
            "fields": {"Description": "Created via simple panel"},
            "attachments": []
        }
        await create_advanced_ticket(interaction, custom_data, panel_id=self.panel_id)

# Command to create a simple ticket panel
@bot.tree.command(name="createpanel_simple", description="Create a simple ticket panel (no modal)")
@app_commands.default_permissions(administrator=True)
async def create_simple_panel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    description: Optional[str] = None,
    button_label: Optional[str] = "Create Ticket",
    button_emoji: Optional[str] = None,
    button_color: Optional[Literal["green", "blue", "red", "gray"]] = "green",
    embed_color: Optional[str] = "#3aa55c",
    allowed_roles: Optional[str] = None
):
    await interaction.response.defer(ephemeral=True)
    
    # Parse allowed roles
    role_ids = []
    if allowed_roles:
        for role_mention in allowed_roles.split():
            try:
                role_id = int(role_mention.strip("<@&>"))
                if interaction.guild.get_role(role_id):
                    role_ids.append(role_id)
            except ValueError:
                pass
    
    # Map button color to style
    color_map = {
        "green": discord.ButtonStyle.green,
        "blue": discord.ButtonStyle.blurple,
        "red": discord.ButtonStyle.red,
        "gray": discord.ButtonStyle.gray
    }
    button_style = color_map.get(button_color, discord.ButtonStyle.green)
    
    # Insert panel into database
    c.execute('''
    INSERT INTO custom_panels 
    (guild_id, channel_id, title, description, button_label, button_emoji, button_style, embed_color, allowed_roles)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        interaction.guild.id,
        channel.id,
        title,
        description,
        button_label,
        button_emoji,
        button_color,
        embed_color,
        json.dumps(role_ids) if role_ids else None
    ))
    panel_id = c.lastrowid
    
    # Create the embed
    try:
        color = discord.Color.from_str(embed_color)
    except ValueError:
        color = discord.Color.green()
    
    embed = discord.Embed(
        title=title,
        description=description,
        color=color
    )
    
    # Create the view with button
    view = SimpleTicketView(panel_id=panel_id)
    button = view.create_simple_ticket
    button.label = button_label
    button.emoji = button_emoji
    button.style = button_style
    
    # Send the panel
    try:
        message = await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        await send_popup(
            interaction,
            "❌ Permission Error",
            "Bot doesn't have permission to send messages in that channel!",
            is_error=True
        )
        return
    
    # Update message ID in database
    c.execute("UPDATE custom_panels SET message_id = ? WHERE panel_id = ?", (message.id, panel_id))
    conn.commit()
    
    await send_popup(
        interaction, 
        "✅ Panel Created", 
        f"Simple ticket panel successfully created in {channel.mention}!"
    )

# Command to create a ticket preset
@bot.tree.command(name="createticketpreset", description="Create a reusable ticket preset")
@app_commands.default_permissions(administrator=True)
async def create_ticket_preset(
    interaction: discord.Interaction,
    name: str,
    title: str,
    description: Optional[str] = None,
    button_label: Optional[str] = "Create Ticket",
    button_emoji: Optional[str] = None,
    button_color: Optional[Literal["green", "blue", "red", "gray"]] = "green",
    embed_color: Optional[str] = "#3aa55c",
    allowed_roles: Optional[str] = None,
    fields: Optional[str] = None
):
    await interaction.response.defer(ephemeral=True)
    
    # Parse allowed roles
    role_ids = []
    if allowed_roles:
        for role_mention in allowed_roles.split():
            try:
                role_id = int(role_mention.strip("<@&>"))
                if interaction.guild.get_role(role_id):
                    role_ids.append(role_id)
            except ValueError:
                pass
    
    # Parse fields if provided
    fields_data = []
    if fields:
        try:
            fields_data = json.loads(fields)
            if not isinstance(fields_data, list):
                fields_data = []
        except json.JSONDecodeError:
            fields_data = []
    
    # Insert preset into database
    c.execute('''
    INSERT INTO ticket_presets 
    (guild_id, name, title, description, button_label, button_emoji, button_style, 
     embed_color, allowed_roles, fields)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(guild_id, name) DO UPDATE SET
        title = excluded.title,
        description = excluded.description,
        button_label = excluded.button_label,
        button_emoji = excluded.button_emoji,
        button_style = excluded.button_style,
        embed_color = excluded.embed_color,
        allowed_roles = excluded.allowed_roles,
        fields = excluded.fields
    ''', (
        interaction.guild.id,
        name.lower(),
        title,
        description,
        button_label,
        button_emoji,
        button_color,
        embed_color,
        json.dumps(role_ids) if role_ids else None,
        json.dumps(fields_data) if fields_data else None
    ))
    conn.commit()
    
    await send_popup(
        interaction,
        "✅ Preset Created",
        f"Ticket preset '{name}' successfully created!"
    )

# Command to create a ticket from a preset
@bot.tree.command(name="ticket", description="Create a ticket from a preset")
async def create_ticket_from_preset(interaction: discord.Interaction, preset: str):
    await interaction.response.defer(ephemeral=True)
    
    c.execute("SELECT preset_id FROM ticket_presets WHERE guild_id = ? AND name = ?", 
            (interaction.guild.id, preset.lower()))
    result = c.fetchone()
    
    if not result:
        await send_popup(
            interaction,
            "❌ Preset Not Found",
            "The specified ticket preset was not found!",
            is_error=True
        )
        return
    
    preset_id = result[0]
    if not await check_panel_permission(interaction, preset_id=preset_id):
        await send_popup(
            interaction,
            "❌ Permission Denied",
            "You don't have permission to create this type of ticket!",
            is_error=True
        )
        return
    
    await interaction.followup.send_modal(AdvancedTicketModal(preset_id=preset_id))

# Command to list available presets
@bot.tree.command(name="listpresets", description="List available ticket presets")
async def list_presets(interaction: discord.Interaction):
    c.execute("SELECT name, description FROM ticket_presets WHERE guild_id = ?", (interaction.guild.id,))
    presets = c.fetchall()
    
    if not presets:
        await send_popup(
            interaction,
            "❌ No Presets",
            "No ticket presets available!",
            is_error=True
        )
        return
    
    embed = discord.Embed(
        title="Available Ticket Presets",
        color=discord.Color.blue()
    )
    
    for name, description in presets:
        embed.add_field(
            name=name.capitalize(),
            value=description or "No description",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Command to set ticket category
@bot.tree.command(name="setticketcategory", description="Set the category for new tickets")
@app_commands.default_permissions(administrator=True)
async def set_ticket_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    c.execute("INSERT OR REPLACE INTO guild_config (guild_id, category_id) VALUES (?, ?)",
            (interaction.guild.id, category.id))
    conn.commit()
    
    await send_popup(
        interaction,
        "✅ Category Set",
        f"Ticket category successfully set to {category.name}"
    )

# Command to set ticket role
@bot.tree.command(name="setticketrole", description="Set which role can create tickets")
@app_commands.default_permissions(administrator=True)
async def set_ticket_role(interaction: discord.Interaction, role: discord.Role):
    c.execute("INSERT OR REPLACE INTO guild_config (guild_id, ticket_role_id) VALUES (?, ?)",
            (interaction.guild.id, role.id))
    conn.commit()
    
    await send_popup(
        interaction,
        "✅ Role Set",
        f"Ticket creation role successfully set to {role.mention}"
    )

# Command to set ping role
@bot.tree.command(name="setpingrole", description="Set which role gets pinged in new tickets")
@app_commands.default_permissions(administrator=True)
async def set_ping_role(interaction: discord.Interaction, role: discord.Role):
    c.execute("INSERT OR REPLACE INTO guild_config (guild_id, ping_role_id) VALUES (?, ?)",
            (interaction.guild.id, role.id))
    conn.commit()
    
    await send_popup(
        interaction,
        "✅ Ping Role Set",
        f"Ticket ping role successfully set to {role.mention}"
    )

# Command to get ticket stats
@bot.tree.command(name="ticketstats", description="Show ticket statistics")
@app_commands.default_permissions(manage_guild=True)
async def ticket_stats(interaction: discord.Interaction):
    c.execute("SELECT status, COUNT(*) FROM tickets WHERE guild_id = ? GROUP BY status", (interaction.guild.id,))
    status_counts = dict(c.fetchall())
    
    c.execute("SELECT ticket_type, COUNT(*) FROM tickets WHERE guild_id = ? GROUP BY ticket_type", (interaction.guild.id,))
    type_counts = dict(c.fetchall())
    
    embed = discord.Embed(
        title="Ticket Statistics",
        color=discord.Color.blue()
    )
    
    status_text = "\n".join([f"• **{status.capitalize()}**: {count}" for status, count in status_counts.items()])
    embed.add_field(name="Status Summary", value=status_text, inline=False)
    
    type_text = "\n".join([f"• **{ttype.replace('-', ' ').title()}**: {count}" for ttype, count in type_counts.items()])
    embed.add_field(name="Ticket Types", value=type_text, inline=False)
    
    c.execute("SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND status = 'open'", (interaction.guild.id,))
    open_count = c.fetchone()[0]
    embed.add_field(name="Open Tickets", value=str(open_count), inline=True)
    
    c.execute("SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND status = 'claimed'", (interaction.guild.id,))
    claimed_count = c.fetchone()[0]
    embed.add_field(name="Claimed Tickets", value=str(claimed_count), inline=True)
    
    await interaction.response.send_message(embed=embed)

# Command to force close a ticket
@bot.tree.command(name="forceclose", description="Force close a ticket")
@app_commands.default_permissions(administrator=True)
async def force_close(interaction: discord.Interaction, reason: str = "Admin closure"):
    c.execute("SELECT * FROM tickets WHERE channel_id = ? AND status != 'closed'", (interaction.channel.id,))
    if not c.fetchone():
        await send_popup(
            interaction,
            "❌ Invalid Channel",
            "This is not an active ticket channel!",
            is_error=True
        )
        return
        
    # Create a confirmation view
    class ConfirmClose(ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.value = None
        
        @ui.button(label="Confirm", style=discord.ButtonStyle.red)
        async def confirm(self, inter: discord.Interaction, button: ui.Button):
            self.value = True
            await inter.response.defer()
            self.stop()
        
        @ui.button(label="Cancel", style=discord.ButtonStyle.gray)
        async def cancel(self, inter: discord.Interaction, button: ui.Button):
            self.value = False
            await inter.response.send_message("Cancelled", ephemeral=True)
            self.stop()
    
    view = ConfirmClose()
    await interaction.response.send_message(
        embed=discord.Embed(
            title="⚠️ Confirm Force Close",
            description=f"Are you sure you want to force close this ticket?\nReason: {reason}",
            color=discord.Color.orange()
        ),
        view=view,
        ephemeral=True
    )
    
    await view.wait()
    if view.value:
        # Proceed with closing
        c.execute("UPDATE tickets SET status = ? WHERE channel_id = ?",
                ("closed", interaction.channel.id))
        conn.commit()
        
        try:
            transcript = await create_transcript(interaction.channel)
        except Exception as e:
            transcript = None
        
        if LOG_CHANNEL_ID and transcript:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                try:
                    await log_channel.send(
                        f"📂 Ticket force-closed by {interaction.user.mention}\nReason: {reason}",
                        file=discord.File(transcript, filename=f"transcript-{interaction.channel.name}.txt")
                    )
                    os.remove(transcript)
                except Exception:
                    pass
        
        try:
            await interaction.channel.delete(reason=f"Force closed by admin: {reason}")
        except discord.Forbidden:
            await interaction.followup.send("❌ Bot doesn't have permission to delete this channel!", ephemeral=True)
    elif view.value is False:
        return  # Cancelled
    else:
        await interaction.followup.send("Timed out", ephemeral=True)

# Event handlers
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    print("------")

    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="for tickets"
    ))

    # Register persistent views
    bot.add_view(TicketManagementView())  # already timeout=None
    bot.add_view(PriorityView())          # now persistent

    # Register views for saved panels
    c.execute("SELECT panel_id FROM custom_panels")
    panels = c.fetchall()
    for (panel_id,) in panels:
        view = ui.View(timeout=None)

        async def panel_callback(i: discord.Interaction, pid: int = panel_id):
            await i.response.send_modal(AdvancedTicketModal(panel_id=pid))

        button = ui.Button(
            custom_id=f"panel_{panel_id}",
            style=discord.ButtonStyle.green
        )
        button.callback = lambda i, pid=panel_id: panel_callback(i, pid)
        view.add_item(button)
        bot.add_view(view)

    try:
        await bot.tree.sync()
        print("Commands synced successfully")
    except Exception as e:
        print(f"Error syncing commands: {e}")


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
