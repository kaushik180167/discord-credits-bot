import os
import sqlite3
import time

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: makes slash commands appear instantly
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")  # optional: channel for credit change logs

# ---------- Database ----------
db = sqlite3.connect("credits.db")
db.execute("PRAGMA journal_mode=WAL")
db.execute(
    """CREATE TABLE IF NOT EXISTS credits (
        guild_id INTEGER, user_id INTEGER, balance INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id))"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
        change INTEGER, balance_after INTEGER, by_id INTEGER, reason TEXT, ts INTEGER)"""
)
db.commit()


def get_balance(guild_id: int, user_id: int) -> int:
    row = db.execute(
        "SELECT balance FROM credits WHERE guild_id=? AND user_id=?", (guild_id, user_id)
    ).fetchone()
    return row[0] if row else 0


def log_change(guild_id, user_id, change, balance_after, by_id, reason):
    db.execute(
        "INSERT INTO history (guild_id,user_id,change,balance_after,by_id,reason,ts) VALUES (?,?,?,?,?,?,?)",
        (guild_id, user_id, change, balance_after, by_id, reason, int(time.time())),
    )


def add_credits(guild_id, user_id, amount, by_id, reason):
    db.execute(
        """INSERT INTO credits (guild_id,user_id,balance) VALUES (?,?,?)
           ON CONFLICT(guild_id,user_id) DO UPDATE SET balance = balance + excluded.balance""",
        (guild_id, user_id, amount),
    )
    new = get_balance(guild_id, user_id)
    log_change(guild_id, user_id, amount, new, by_id, reason)
    db.commit()
    return new


def subtract_credits(guild_id, user_id, amount, by_id, reason):
    """Returns new balance, or None if the user doesn't have enough (balance never goes below 0)."""
    cur = db.execute(
        "UPDATE credits SET balance = balance - ? WHERE guild_id=? AND user_id=? AND balance >= ?",
        (amount, guild_id, user_id, amount),
    )
    if cur.rowcount == 0:
        db.rollback()
        return None
    new = get_balance(guild_id, user_id)
    log_change(guild_id, user_id, -amount, new, by_id, reason)
    db.commit()
    return new


# ---------- Bot ----------
class CreditBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = CreditBot()
admin_only = app_commands.checks.has_permissions(manage_guild=True)


async def send_log(guild: discord.Guild, text: str):
    if not LOG_CHANNEL_ID:
        return
    channel = guild.get_channel(int(LOG_CHANNEL_ID))
    if channel:
        try:
            await channel.send(text)
        except discord.Forbidden:
            pass


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="check_credits", description="Check your credits (admins can check anyone)")
@app_commands.describe(user="Whose credits to check (admins only; leave empty for yourself)")
@app_commands.guild_only()
async def check_credits(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    if target.id != interaction.user.id and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You can only check your own credits.", ephemeral=True
        )
        return
    bal = get_balance(interaction.guild_id, target.id)
    who = "You have" if target.id == interaction.user.id else f"{target.display_name} has"
    await interaction.response.send_message(f"💰 {who} **{bal}** credits.", ephemeral=True)


@bot.tree.command(name="add_credits", description="Add credits to a user (admin only)")
@app_commands.describe(user="Who gets the credits", amount="How many credits", reason="Optional note")
@app_commands.guild_only()
@admin_only
async def add_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: app_commands.Range[int, 1, 1_000_000],
    reason: str = "No reason given",
):
    new = add_credits(interaction.guild_id, user.id, amount, interaction.user.id, reason)
    await interaction.response.send_message(
        f"✅ Added **{amount}** credits to {user.mention}. New balance: **{new}**."
    )
    await send_log(
        interaction.guild,
        f"➕ {interaction.user.mention} added **{amount}** to {user.mention} (now {new}) — {reason}",
    )


@bot.tree.command(name="subtract_credits", description="Remove credits from a user (admin only)")
@app_commands.describe(user="Who to remove credits from", amount="How many credits", reason="Optional note")
@app_commands.guild_only()
@admin_only
async def subtract_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: app_commands.Range[int, 1, 1_000_000],
    reason: str = "No reason given",
):
    new = subtract_credits(interaction.guild_id, user.id, amount, interaction.user.id, reason)
    if new is None:
        have = get_balance(interaction.guild_id, user.id)
        await interaction.response.send_message(
            f"⚠️ Insufficient credits. {user.mention} only has **{have}** credits.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"✅ Removed **{amount}** credits from {user.mention}. New balance: **{new}**."
    )
    await send_log(
        interaction.guild,
        f"➖ {interaction.user.mention} removed **{amount}** from {user.mention} (now {new}) — {reason}",
    )


@bot.tree.command(name="leaderboard", description="Top 10 users by credits")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT user_id, balance FROM credits WHERE guild_id=? AND balance>0 ORDER BY balance DESC LIMIT 10",
        (interaction.guild_id,),
    ).fetchall()
    if not rows:
        await interaction.response.send_message("No one has any credits yet.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'**{i + 1}.**'} <@{uid}> — **{bal}**"
        for i, (uid, bal) in enumerate(rows)
    ]
    embed = discord.Embed(title="🏆 Credits Leaderboard", description="\n".join(lines), color=0xF1C40F)
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="credit_history", description="Recent credit changes for a user (admin only)")
@app_commands.describe(user="Whose history to view")
@app_commands.guild_only()
@admin_only
async def credit_history(interaction: discord.Interaction, user: discord.Member):
    rows = db.execute(
        "SELECT change, balance_after, by_id, reason, ts FROM history "
        "WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
        (interaction.guild_id, user.id),
    ).fetchall()
    if not rows:
        await interaction.response.send_message("No history for this user.", ephemeral=True)
        return
    lines = [
        f"<t:{ts}:d> **{chg:+}** → {after} by <@{by}> — {reason}"
        for chg, after, by, reason, ts in rows
    ]
    await interaction.response.send_message(
        f"📜 History for {user.mention}:\n" + "\n".join(lines),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.error
async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the **Manage Server** permission to use this command."
    else:
        msg = "❌ Something went wrong."
        print(f"Command error: {error!r}")
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if not TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN. Copy .env.example to .env and fill it in.")
bot.run(TOKEN)
