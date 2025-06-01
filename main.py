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

# Database setup
DB_PATH = os.environ.get("DB_PATH", "tickets.db")
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS tickets
             (id INTEGER PRIMARY KEY, 
              user_id INTEGER, 
              channel_id INTEGER,
              status TEXT,
              created_at TIMESTAMP,
              ticket_type TEXT,
              assigned_to INTEGER,
              priority TEXT)''')
conn.commit()

# Configuration
SUPPORT_ROLE_ID = int(os.environ.get("SUPPORT_ROLE_ID", 0))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", 0))
CATEGORY_NAME = "Nexus Support Tickets"
PRIORITIES = {"🟢 Low": "low", "🟡 Medium": "medium", "🔴 High": "high", "🚨 Critical": "critical"}

# Ticket types with descriptions
TICKET_TYPES = {
    "player-application": "Apply to join our competitive teams",
    "support-request": "Get help with server issues",
    "report-player": "Report rule violations",
    "partnership": "Business collaboration inquiries",
    "content-creation": "Streamer/creator partnerships"
}

class TicketModal(ui.Modal, title="Create Support Ticket"):
    def __init__(self, ticket_type):
        super().__init__()
        self.ticket_type = ticket_type
        self.title = f"{ticket_type.capitalize()} Ticket"
        
    issue = ui.TextInput(label="Briefly describe your issue", style=discord.TextStyle.short)
    details = ui.TextInput(label="Additional details", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_ticket_channel(interaction, self.ticket_type, str(self.issue), str(self.details))

class TicketTypeView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
        for ticket_type, description in TICKET_TYPES.items():
            self.add_item(TicketTypeButton(ticket_type, description))

class TicketTypeButton(ui.Button):
    def __init__(self, ticket_type, description):
        super().__init__(
            label=ticket_type.replace("-", " ").title(),
            style=discord.ButtonStyle.blurple,
            custom_id=f"ticket_{ticket_type}",
            emoji="📩"
        )
        self.ticket_type = ticket_type
        self.description = description
        
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketModal(self.ticket_type))

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, 
        name="Nexus Support Tickets"
    ))
    bot.add_view(TicketManagementView())
    bot.add_view(TicketTypeView())
    await bot.tree.sync()

async def create_ticket_channel(interaction: discord.Interaction, ticket_type: str, issue: str, details: str):
    # Get or create category
    category = discord.utils.get(interaction.guild.categories, name=CATEGORY_NAME)
    if not category:
        category = await interaction.guild.create_category(CATEGORY_NAME)
    
    # Create channel
    ticket_number = get_next_ticket_number()
    channel_name = f"{ticket_type}-{ticket_number}-{interaction.user.display_name}"
    channel = await category.create_text_channel(channel_name[:99])
    
    # Set permissions
    await channel.set_permissions(interaction.user, read_messages=True, send_messages=True)
    await channel.set_permissions(interaction.guild.default_role, read_messages=False)
    
    # Add support role
    support_role = interaction.guild.get_role(SUPPORT_ROLE_ID)
    if support_role:
        await channel.set_permissions(support_role, read_messages=True, send_messages=True)
    
    # Create embed
    embed = discord.Embed(
        title=f"Nexus {ticket_type.replace('-', ' ').title()} Ticket #{ticket_number}",
        color=discord.Color.blue()
    )
    embed.add_field(name="Player", value=interaction.user.mention, inline=False)
    embed.add_field(name="Type", value=ticket_type, inline=True)
    embed.add_field(name="Status", value="🟢 Open", inline=True)
    embed.add_field(name="Priority", value="🟡 Medium", inline=True)
    embed.add_field(name="Issue", value=issue, inline=False)
    embed.add_field(name="Details", value=details, inline=False)
    embed.set_footer(text=f"Created at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    
    # Send initial message
    message = await channel.send(
        content=f"{interaction.user.mention} {support_role.mention if support_role else ''}",
        embed=embed,
        view=TicketManagementView()
    )
    
    # Pin message
    await message.pin()
    
    # Save to database
    c.execute("INSERT INTO tickets (user_id, channel_id, status, created_at, ticket_type, priority) VALUES (?, ?, ?, ?, ?, ?)",
              (interaction.user.id, channel.id, "open", datetime.datetime.now(), ticket_type, "medium"))
    conn.commit()
    
    # Send confirmation
    await interaction.followup.send(
        f"Ticket created: {channel.mention}",
        ephemeral=True
    )
    
    # Log creation
    await log_action(f"Ticket #{ticket_number} ({ticket_type}) created by {interaction.user}")

class TicketManagementView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @ui.button(label="Claim Ticket", style=discord.ButtonStyle.green, custom_id="claim_ticket", emoji="🙋")
    async def claim_ticket(self, interaction: discord.Interaction, button: ui.Button):
        # Update database
        c.execute("UPDATE tickets SET assigned_to = ?, status = ? WHERE channel_id = ?",
                  (interaction.user.id, "claimed", interaction.channel.id))
        conn.commit()
        
        # Update embed
        embed = interaction.message.embeds[0]
        embed.set_field_at(2, name="Status", value="🟡 Claimed", inline=True)
        embed.set_field_at(3, name="Assigned To", value=interaction.user.mention, inline=True)
        await interaction.message.edit(embed=embed)
        
        await interaction.response.send_message(
            f"{interaction.user.mention} has claimed this ticket",
            allowed_mentions=discord.AllowedMentions.none()
        )
        
        # Log claim
        await log_action(f"Ticket claimed by {interaction.user} in #{interaction.channel.name}")
    
    @ui.button(label="Add User", style=discord.ButtonStyle.blurple, custom_id="add_user", emoji="👥")
    async def add_user(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AddUserModal())
    
    @ui.button(label="Set Priority", style=discord.ButtonStyle.gray, custom_id="set_priority", emoji="⚠️")
    async def set_priority(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "Select ticket priority:",
            view=PriorityView(),
            ephemeral=True
        )
    
    @ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket", emoji="🔒")
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

class AddUserModal(ui.Modal, title="Add User to Ticket"):
    user = ui.TextInput(label="User ID or Mention", style=discord.TextStyle.short)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Try to convert to user ID
            user_id = int(str(self.user).strip().replace("<@", "").replace(">", ""))
            user = interaction.guild.get_member(user_id)
            
            if not user:
                await interaction.response.send_message("User not found!", ephemeral=True)
                return
                
            await interaction.channel.set_permissions(user, read_messages=True, send_messages=True)
            await interaction.response.send_message(
                f"{user.mention} has been added to the ticket",
                allowed_mentions=discord.AllowedMentions.none()
            )
        except ValueError:
            await interaction.response.send_message("Invalid user format! Use ID or mention", ephemeral=True)

class PriorityView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
        for label, priority in PRIORITIES.items():
            self.add_item(PriorityButton(label, priority))

class PriorityButton(ui.Button):
    def __init__(self, label, priority):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.gray,
            custom_id=f"priority_{priority}"
        )
        self.priority = priority
        
    async def callback(self, interaction: discord.Interaction):
        # Update database
        c.execute("UPDATE tickets SET priority = ? WHERE channel_id = ?",
                  (self.priority, interaction.channel.id))
        conn.commit()
        
        # Update embed
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if not embed:
            await interaction.response.send_message("Couldn't find ticket info!", ephemeral=True)
            return
            
        # Find priority field index
        for i, field in enumerate(embed.fields):
            if field.name == "Priority":
                embed.set_field_at(i, name="Priority", value=self.view.children[0].label, inline=True)
                break
        
        await interaction.message.edit(embed=embed)
        await interaction.response.send_message(
            f"Priority set to {self.label}",
            ephemeral=True
        )

async def create_transcript(channel):
    transcript = []
    async for message in channel.history(limit=None, oldest_first=True):
        content = message.content
        if message.embeds:
            content += "\n[Embed Content]"
        transcript.append(f"{message.created_at} - {message.author.display_name}: {content}")
    
    filename = f"{channel.id}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(transcript))
    
    return filename

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
        description="Select ticket type below:",
        color=discord.Color.green()
    )
    for ticket_type, description in TICKET_TYPES.items():
        embed.add_field(
            name=ticket_type.replace("-", " ").title(),
            value=description,
            inline=False
        )
    embed.set_footer(text="Our team will respond within 24 hours")
    
    await interation.response.send_message(
        embed=embed,
        view=TicketTypeView()
    )

@bot.tree.command(name="ticketstats", description="Show ticket statistics")
async def ticket_stats(interaction: discord.Interaction):
    # Get stats from database
    c.execute("SELECT status, COUNT(*) FROM tickets GROUP BY status")
    status_counts = dict(c.fetchall())
    
    c.execute("SELECT ticket_type, COUNT(*) FROM tickets GROUP BY ticket_type")
    type_counts = dict(c.fetchall())
    
    # Create embed
    embed = discord.Embed(
        title="Ticket Statistics",
        color=discord.Color.blue()
    )
    
    # Status summary
    status_text = "\n".join([f"• **{status.capitalize()}**: {count}" 
                            for status, count in status_counts.items()])
    embed.add_field(name="Status Summary", value=status_text, inline=False)
    
    # Type summary
    type_text = "\n".join([f"• **{ttype.replace('-', ' ').title()}**: {count}" 
                          for ttype, count in type_counts.items()])
    embed.add_field(name="Ticket Types", value=type_text, inline=False)
    
    await interaction.response.send_message(embed=embed)

if __name__ == "__main__":
    bot.run(os.environ["BOT_TOKEN"])
