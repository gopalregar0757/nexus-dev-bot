import discord
from discord import ui, app_commands
from discord.ext import commands
import sqlite3
import datetime
import asyncio
import os


# Initialize bot
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, application_id="YOUR_APP_ID")

DB_PATH = os.environ.get("DB_PATH", "tickets.db")
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS tickets
             (id INTEGER PRIMARY KEY, 
              user_id INTEGER, 
              channel_id INTEGER,
              status TEXT,
              created_at TIMESTAMP,
              ticket_type TEXT)''')
conn.commit()

# Configuration
SUPPORT_ROLE_ID = 000000000000000000  # Replace with your support role ID
LOG_CHANNEL_ID = 000000000000000000   # Replace with your log channel ID
CATEGORY_NAME = "Nexus Support Tickets"

# Ticket types with descriptions
TICKET_TYPES = {
    "player-application": "Apply to join our competitive teams",
    "support-request": "Get help with server issues",
    "report-player": "Report rule violations",
    "partnership": "Business collaboration inquiries",
    "content-creation": "Streamer/creator partnerships"
}

class TicketModal(ui.Modal, title="Create Support Ticket"):
    issue = ui.TextInput(label="Briefly describe your issue", style=discord.TextStyle.short)
    details = ui.TextInput(label="Additional details", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_ticket_channel(interaction, str(self.issue), str(self.details))

class TicketView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(TicketModal())

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, 
        name="Nexus Support Tickets"
    ))
    bot.add_view(TicketView())
    await bot.tree.sync()

async def create_ticket_channel(interaction: discord.Interaction, issue: str, details: str):
    # Get or create category
    category = discord.utils.get(interaction.guild.categories, name=CATEGORY_NAME)
    if not category:
        category = await interaction.guild.create_category(CATEGORY_NAME)
    
    # Create channel
    ticket_type = "general"
    ticket_number = get_next_ticket_number()
    channel_name = f"ticket-{ticket_number}-{interaction.user.display_name}"
    channel = await category.create_text_channel(channel_name)
    
    # Set permissions
    await channel.set_permissions(interaction.user, read_messages=True, send_messages=True)
    await channel.set_permissions(interaction.guild.default_role, read_messages=False)
    
    # Add support role
    support_role = interaction.guild.get_role(SUPPORT_ROLE_ID)
    if support_role:
        await channel.set_permissions(support_role, read_messages=True, send_messages=True)
    
    # Create embed
    embed = discord.Embed(
        title=f"Nexus Support Ticket #{ticket_number}",
        color=discord.Color.blue()
    )
    embed.add_field(name="Player", value=interaction.user.mention, inline=False)
    embed.add_field(name="Issue", value=issue, inline=False)
    embed.add_field(name="Details", value=details, inline=False)
    embed.set_footer(text=f"Created at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    
    # Send initial message
    message = await channel.send(
        content=f"{interaction.user.mention} {support_role.mention if support_role else ''}",
        embed=embed,
        view=TicketCloseView()
    )
    
    # Pin message
    await message.pin()
    
    # Save to database
    c.execute("INSERT INTO tickets (user_id, channel_id, status, created_at, ticket_type) VALUES (?, ?, ?, ?, ?)",
              (interaction.user.id, channel.id, "open", datetime.datetime.now(), ticket_type))
    conn.commit()
    
    # Send confirmation
    await interaction.followup.send(
        f"Ticket created: {channel.mention}",
        ephemeral=True
    )
    
    # Log creation
    await log_action(f"Ticket #{ticket_number} created by {interaction.user}")

class TicketCloseView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        # Update database
        c.execute("UPDATE tickets SET status = ? WHERE channel_id = ?",
                  ("closed", interaction.channel.id))
        conn.commit()
        
        # Create transcript
        transcript = await create_transcript(interaction.channel)
        
        # Send to log channel
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(
                f"Ticket closed by {interaction.user.mention}",
                file=discord.File(transcript, filename=f"transcript-{interaction.channel.name}.txt")
            )
        
        # Notify user
        await interaction.response.send_message("Closing ticket in 10 seconds...")
        await asyncio.sleep(10)
        await interaction.channel.delete(reason="Ticket closed")
        
        # Log closure
        await log_action(f"Ticket closed by {interaction.user} in #{interaction.channel.name}")

async def create_transcript(channel):
    transcript = []
    async for message in channel.history(limit=None, oldest_first=True):
        transcript.append(f"{message.created_at} - {message.author.display_name}: {message.content}")
    
    with open(f"{channel.id}.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(transcript))
    
    return f"{channel.id}.txt"

async def log_action(message):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        embed = discord.Embed(
            description=message,
            color=discord.Color.gold(),
            timestamp=datetime.datetime.now()
        )
        await channel.send(embed=embed)

def get_next_ticket_number():
    c.execute("SELECT COUNT(*) FROM tickets")
    count = c.fetchone()[0]
    return count + 1

@bot.tree.command(name="ticketpanel", description="Setup ticket creation panel (Admin only)")
@commands.has_permissions(administrator=True)
async def ticket_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Nexus Esports Support",
        description="Click below to create a support ticket",
        color=discord.Color.green()
    )
    embed.add_field(
        name="Ticket Types",
        value="\n".join([f"• **{k}**: {v}" for k, v in TICKET_TYPES.items()]),
        inline=False
    )
    embed.set_footer(text="Our team will respond within 24 hours")
    
    await interaction.response.send_message(
        embed=embed,
        view=TicketView()
    )

@bot.tree.command(name="adduser", description="Add user to current ticket")
async def add_user(interaction: discord.Interaction, user: discord.Member):
    if "ticket" not in interaction.channel.name:
        await interaction.response.send_message("This is not a ticket channel!", ephemeral=True)
        return
    
    await interaction.channel.set_permissions(user, read_messages=True, send_messages=True)
    await interaction.response.send_message(f"{user.mention} has been added to the ticket")

@bot.tree.command(name="close", description="Close current ticket")
async def close_ticket(interaction: discord.Interaction):
    if "ticket" not in interaction.channel.name:
        await interaction.response.send_message("This is not a ticket channel!", ephemeral=True)
        return
    
    # Trigger close button functionality
    view = TicketCloseView()
    await view.close_ticket(interaction, None)


if __name__ == "__main__":
    bot.run(os.environ["BOT_TOKEN"]) 
