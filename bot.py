import asyncio
import io
import json
import logging
import os
import re
import threading
import signal
import sys
import webserver
from datetime import datetime, date, timezone, timedelta
import datetime as _dt
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # Python < 3.9

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("oc-bot")

# ─── Config ────────────────────────────────────────────────────────────────────
DATA_FILE                 = os.environ.get("DATA_FILE", "data.json")
LOG_CHANNEL_NAME          = "oc-logs"         # OC registrations, dorm logs, birthdays
AUDIT_CHANNEL_NAME        = "logs"            # ALL user action audit trail
DEBUT_CHANNEL_NAME        = "debuts"
NEWS_CHANNEL_NAME         = "announcements"
DEV_RESPONSE_CHANNEL_NAME = "dev-responses"   # Dev DM responses
DB_BACKUP_CHANNEL_NAME    = "bot-db-backup"   # Automatic DB persistence channel
INSTAGRAM_CHANNEL_NAME    = "instagram"
BIRTHDAY_FORMAT           = "%Y/%m/%d"
BIRTHDAY_DISPLAY          = "YYYY/MM/DD"
DORM_SIZES                = [2, 3, 4]
MAX_PHOTOS                = 10
PORT                      = int(os.environ.get("PORT", 8080))
JST                       = timezone(timedelta(hours=9))  # GMT+9, no DST

FILTERABLE_FIELDS  = ["gender", "pronouns", "face_claim", "main_skill",
                      "ethnicity", "nationality"]

BUTTON_COLOR_MAP = {
    "green":   discord.ButtonStyle.success,
    "red":     discord.ButtonStyle.danger,
    "grey":    discord.ButtonStyle.secondary,
    "gray":    discord.ButtonStyle.secondary,
    "blurple": discord.ButtonStyle.primary,
    "blue":    discord.ButtonStyle.primary,
}

# State Flags for DB Persistence
DB_LOADED  = False
DATA_DIRTY = False

# ─── Helpers ───────────────────────────────────────────────────────────────────
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"ocs": {}, "floors": {}, "dorms": {}, "instagram": {}, "dms": {},
                "groupchats": {}, "scheduled": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
    for k in ("ocs", "floors", "dorms", "instagram", "dms", "groupchats", "scheduled"):
        d.setdefault(k, {})
    return d

def save_data(data: dict) -> None:
    global DATA_DIRTY
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    DATA_DIRTY = True

async def push_backup_to_discord(data: dict, reason: str = "mutation") -> None:
    global DATA_DIRTY
    if not DB_LOADED:
        log.debug("push_backup_to_discord: DB not yet loaded, skipping upload.")
        return

    try:
        payload_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        if len(payload_bytes) > 8_000_000:  # 8 MB Discord limit
            log.error("push_backup_to_discord: payload too large (%d bytes), skipping upload.", len(payload_bytes))
            return

        for guild in bot.guilds:
            ch = discord.utils.get(guild.text_channels, name=DB_BACKUP_CHANNEL_NAME)
            if ch:
                file = discord.File(io.BytesIO(payload_bytes), filename="data.json")
                new_msg = await ch.send(
                    f"[BACKUP] {reason} — {now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}",
                    file=file
                )
                DATA_DIRTY = False
                log.info("Backup uploaded — message_id=%s guild=%s", new_msg.id, guild.id)

                try:
                    await ch.purge(limit=50, check=lambda m: m.author == bot.user and m.id != new_msg.id)
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"Could not purge old backups: {e}")
                
                return
            
        log.warning("push_backup_to_discord: No guild with #%s found.", DB_BACKUP_CHANNEL_NAME)
    except Exception as e:
        log.error("push_backup_to_discord failed: %s", e)

async def save_and_backup(data: dict, reason: str = "mutation") -> None:
    save_data(data)
    await push_backup_to_discord(data, reason=reason)

def _validate_backup(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    if "ocs" in parsed:
        for v in parsed["ocs"].values():
            if not isinstance(v, dict):
                return False
    if "floors" in parsed:
        for v in parsed["floors"].values():
            if not isinstance(v, dict) or "rooms" not in v or not isinstance(v["rooms"], dict):
                return False
    if "dorms" in parsed:
        for v in parsed["dorms"].values():
            if not isinstance(v, dict):
                return False
    return True

def _validate_command_tree_schema(tree: app_commands.CommandTree) -> list[str]:
    violations: list[str] = []

    for cmd in tree.get_commands():
        desc = getattr(cmd, "description", "") or ""
        if not (1 <= len(desc) <= 100):
            violations.append(
                f"/{cmd.name}: command description length={len(desc)} "
                f"(must be 1–100). Value: {desc!r}"
            )

        params = getattr(cmd, "_params", {})
        if len(params) > 25:
            violations.append(f"/{cmd.name}: has {len(params)} options (max 25).")

        for param_name, param in params.items():
            p_desc = getattr(param, "description", "") or ""
            if not (1 <= len(p_desc) <= 100):
                violations.append(
                    f"/{cmd.name} [{param_name}]: option description length={len(p_desc)} "
                    f"(must be 1–100). Value: {p_desc!r}"
                )
            if not (1 <= len(param_name) <= 32):
                violations.append(
                    f"/{cmd.name} [{param_name}]: option name length={len(param_name)} "
                    f"(must be 1–32)."
                )
            choices = getattr(param, "choices", []) or []
            for choice in choices:
                c_name = getattr(choice, "name", "") or ""
                c_val  = getattr(choice, "value", "")
                if not (1 <= len(c_name) <= 100):
                    violations.append(
                        f"/{cmd.name} [{param_name}] choice name length={len(c_name)}: {c_name!r}"
                    )
                if isinstance(c_val, str) and not (1 <= len(c_val) <= 100):
                    violations.append(
                        f"/{cmd.name} [{param_name}] choice value length={len(c_val)}: {c_val!r}"
                    )

    return violations

def get_age(birthday_str: str):
    try:
        bday  = datetime.strptime(birthday_str, BIRTHDAY_FORMAT).date()
        today = date.today()
        return (today.year - bday.year
                - ((today.month, today.day) < (bday.month, bday.day)))
    except Exception:
        return None

def days_until_birthday(birthday_str: str, today: date) -> int:
    """
    Returns the number of days until the OC's next birthday occurrence from `today`.
    If the birthday has already occurred this year (month/day < today), returns a
    LARGE sentinel value (e.g. 365 + days since it passed) so that past birthdays
    sort after upcoming ones, while still being internally ordered by recency.
    Returns 9999 on parse error.
    """
    try:
        bday = datetime.strptime(birthday_str, BIRTHDAY_FORMAT).date()
        this_year_bday = date(today.year, bday.month, bday.day)
        if this_year_bday >= today:
            return (this_year_bday - today).days          # upcoming: 0–364
        else:
            # Already passed: use large base offset plus days since it passed
            days_ago = (today - this_year_bday).days      # 1–364
            return 365 + days_ago                         # 366–729
    except Exception:
        return 9999

def format_birthday_long(birthday_str: str) -> str:
    try:
        bday = datetime.strptime(birthday_str, BIRTHDAY_FORMAT).date()
        return bday.strftime("%B %d, %Y").replace(" 0", " ")
    except Exception:
        return birthday_str

def is_dev(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return (interaction.guild.owner_id == interaction.user.id
            or interaction.user.guild_permissions.administrator)

def resolve_button_style(value: Optional[str], default: discord.ButtonStyle) -> discord.ButtonStyle:
    if value:
        return BUTTON_COLOR_MAP.get(value.lower().strip(), default)
    return default

def valid_image_url(url: str) -> bool:
    return bool(re.match(
        r"^https?://\S+\.(png|jpg|jpeg|gif|webp)(\?.*)?$", url, re.I))

def valid_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")

def oc_key_of(name: str) -> str:
    return name.lower().replace(" ", "_")

def dorm_key_of(name: str) -> str:
    return name.lower().replace(" ", "-")

def room_key_of(name: str) -> str:
    return name.lower().replace(" ", "-")

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_iso() -> str:
    return now_utc().isoformat()

# ─── Audit helper ──────────────────────────────────────────────────────────────
async def audit(guild: discord.Guild, message: str) -> None:
    ch = discord.utils.get(guild.text_channels, name=AUDIT_CHANNEL_NAME)
    if ch:
        ts = now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
        await ch.send(f"[{ts}]  {message}")

# ─── OC embed ──────────────────────────────────────────────────────────────────
def build_oc_embed(oc: dict, key: str) -> discord.Embed:
    age     = get_age(oc["birthday"])
    age_str = f" ({age} y/o)" if age is not None else ""
    embed   = discord.Embed(title=oc["name"], color=discord.Color.blurple())
    if oc.get("profile_picture"):
        embed.set_thumbnail(url=oc["profile_picture"])
    embed.add_field(name="Birthday",    value=f"{format_birthday_long(oc['birthday'])}{age_str}", inline=True)
    embed.add_field(name="Gender",      value=oc["gender"],                 inline=True)
    embed.add_field(name="Pronouns",    value=oc["pronouns"],               inline=True)
    embed.add_field(name="Face Claim",  value=oc["face_claim"],             inline=True)
    embed.add_field(name="Main Skill",  value=oc["main_skill"],             inline=True)
    embed.add_field(name="Ethnicity",   value=oc["ethnicity"],              inline=True)
    embed.add_field(name="Nationality", value=oc["nationality"],            inline=True)
    if oc.get("form_link"):
        embed.add_field(name="Form", value=f"[Click here]({oc['form_link']})", inline=True)
    embed.set_footer(text=f"OC ID: {key}")
    return embed

def _build_available_rooms_text(data: dict) -> str:
    lines = []
    for f_key, floor in data["floors"].items():
        for r_key, room in floor["rooms"].items():
            occ   = len(room.get("occupants", []))
            cap   = room.get("capacity", 0)
            if occ < cap:
                lines.append(f"**{floor['name']}** → {room['name']} [{occ}/{cap} spots taken]")
    return "\n".join(lines) if lines else "No rooms available."

def _build_displaced_dm_embed(
    oc_name: str, evicted_from_lines: list[str], available_rooms_text: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="🏚️ Your OC Has Been Displaced!",
        color=discord.Color.orange(),
        description=(
            f"**{oc_name}** has been removed from their dorm assignment "
            f"because the following room(s) were deleted by a server admin:\n"
            + "\n".join(f"• {line}" for line in evicted_from_lines)
        ),
    )
    embed.add_field(name="Available Rooms", value=available_rooms_text or "No rooms available at this time.", inline=False)
    embed.set_footer(text="Use /dorm_assign to pick a new room.")
    return embed

# ─── Bot ───────────────────────────────────────────────────────────────────────
intents         = discord.Intents.default()
intents.members = True
intents.guilds  = True

# DEPLOYMENT NOTE: In the Discord Developer Portal → Installation, ensure both
# "Guild Install" and "User Install" are enabled. User install scope: applications.commands.
# This allows users to add the bot to their account for use in DMs and private channels.
bot = commands.Bot(command_prefix="!", intents=intents)

# Allow the tree to be installed both by guilds and by individual users
bot.tree._default_installation_types = [
    discord.app_commands.AppInstallationType.guild,
    discord.app_commands.AppInstallationType.user,
]
bot.tree._default_interaction_contexts = [
    discord.app_commands.AppCommandContext.guild,
    discord.app_commands.AppCommandContext.bot_dm,
    discord.app_commands.AppCommandContext.private_channel,
]

class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *_):
        pass

def _run_http():
    HTTPServer(("0.0.0.0", PORT), _Health).serve_forever()

@bot.tree.interaction_check
async def global_interaction_check(interaction: discord.Interaction) -> bool:
    if not DB_LOADED:
        await interaction.response.send_message(
            "⏳ The bot is currently booting up and restoring memory. Please try again in a moment.", 
            ephemeral=True
        )
        return False
    return True


# ─── Ping ──────────────────────────────────────────────────────────────────────
@bot.tree.command(name="ping", description="Check the bot's responsiveness and WebSocket latency.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ping_cmd(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"WebSocket latency: **{latency_ms} ms**",
        color=discord.Color.green() if latency_ms < 200 else discord.Color.orange(),
        timestamp=now_utc(),
    )
    embed.set_footer(text="Latency is the bot's last WebSocket heartbeat round-trip time.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── on_ready ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    global DB_LOADED

    if not DB_LOADED:
        for guild in bot.guilds:
            ch = discord.utils.get(guild.text_channels, name=DB_BACKUP_CHANNEL_NAME)
            if not ch:
                try:
                    cat = discord.utils.get(guild.categories, name="Special")
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
                    }
                    ch = await guild.create_text_channel(
                        DB_BACKUP_CHANNEL_NAME, 
                        category=cat, 
                        overwrites=overwrites,
                        topic="⚙️ Automated DB backup storage. Do not send messages here.",
                        slowmode_delay=21600
                    )
                except Exception:
                    continue

            if ch:
                try:
                    await ch.edit(topic="⚙️ Automated DB backup storage. Do not send messages here.", slowmode_delay=21600)
                except (discord.Forbidden, discord.HTTPException):
                    pass

                try:
                    async for message in ch.history(limit=20):
                        if message.author == bot.user and message.attachments:
                            att = message.attachments[0]
                            if att.filename == "data.json":
                                file_bytes = await att.read()
                                try:
                                    parsed = json.loads(file_bytes)
                                    if not _validate_backup(parsed):
                                        log.error("Backup file failed schema validation, skipping restore.")
                                        break
                                    for k in ("ocs", "floors", "dorms", "instagram", "dms", "groupchats", "scheduled"):
                                        parsed.setdefault(k, {})
                                except (json.JSONDecodeError, ValueError) as e:
                                    log.error(f"Backup file is corrupt, skipping restore: {e}")
                                    break
                                
                                with open(DATA_FILE, "wb") as f:
                                    f.write(file_bytes)
                                log.info("Restored from backup — message_id=%s guild=%s", message.id, guild.id)
                                break
                except Exception as e:
                    log.error(f"Error fetching DB backup: {e}")
                break

        DB_LOADED = True

    schema_violations = _validate_command_tree_schema(bot.tree)
    if schema_violations:
        log.critical(
            "Pre-sync schema validation found %d violation(s). Sync will fail!\n%s",
            len(schema_violations),
            "\n".join(f"  • {v}" for v in schema_violations)
        )
    else:
        log.info("Pre-sync schema validation passed — all %d commands are valid.", len(bot.tree.get_commands()))

    try:
        synced = await bot.tree.sync()
        log.info("Logged in as %s — %d slash command(s) synced.", bot.user, len(synced))
    except discord.app_commands.errors.CommandSyncFailure as e:
        log.critical("CommandSyncFailure during on_ready sync. Full error: %s", e)
    except discord.HTTPException as e:
        log.error("HTTPException during on_ready sync (status=%s, code=%s): %s", e.status, e.code, e.text)

    if not check_birthdays.is_running(): check_birthdays.start()
    if not check_scheduled.is_running(): check_scheduled.start()
    if not auto_backup_db.is_running():  auto_backup_db.start()


# ─── Automated Backup loop ─────────────────────────────────────────────────────
@tasks.loop(minutes=5)
async def auto_backup_db():
    global DATA_DIRTY
    if not DATA_DIRTY or not DB_LOADED: return
    if not os.path.exists(DATA_FILE): return
    
    log.warning("auto_backup_db: DATA_DIRTY is still True after 5 minutes — triggering catch-up backup.")
    try:
        data = load_data()
        await push_backup_to_discord(data, reason="auto-watchdog")
    except Exception as e:
        log.error(f"Watchdog backup failed: {e}")


# ─── Birthday loop ─────────────────────────────────────────────────────────────
@tasks.loop(time=_dt.time(hour=15, minute=0, tzinfo=timezone.utc))  # 15:00 UTC = 00:00 JST
async def check_birthdays():
    # Note: get_age() returns the new age on their exact birthday date, so "turns N today" is accurate.
    today_jst = datetime.now(JST).date()
    data       = load_data()

    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if not ch:
            continue

        for oc in data["ocs"].values():
            try:
                bday = datetime.strptime(oc["birthday"], BIRTHDAY_FORMAT).date()
                if bday.month != today_jst.month or bday.day != today_jst.day:
                    continue

                age        = get_age(oc["birthday"])
                age_suffix = f" They turn **{age}** today!" if age else ""

                owner_id  = oc.get("owner_id")
                owner_mention = f"<@{owner_id}>" if owner_id else None

                embed = discord.Embed(
                    title=f"🎂 Happy Birthday, {oc['name']}!",
                    description=(
                        f"Today is **{oc['name']}**'s birthday! 🎉{age_suffix}\n"
                        + (f"Owned by {owner_mention}" if owner_mention else "")
                    ),
                    color=discord.Color.from_rgb(255, 182, 193),
                    timestamp=datetime.now(JST),
                )
                if oc.get("profile_picture"):
                    embed.set_thumbnail(url=oc["profile_picture"])
                embed.add_field(name="Birthday", value=format_birthday_long(oc["birthday"]), inline=True)
                if age:
                    embed.add_field(name="Turning", value=str(age), inline=True)
                embed.set_footer(text="Birthday recognized in JST (GMT+9)")

                await ch.send(embed=embed)
            except Exception as e:
                log.warning("Birthday check failed for OC '%s': %s", oc.get("name", "?"), e)

# ─── Scheduled announcements loop ─────────────────────────────────────────────
@tasks.loop(seconds=30)
async def check_scheduled():
    data    = load_data()
    now     = now_utc()
    to_fire = [k for k, v in data["scheduled"].items()
               if datetime.fromisoformat(v["fire_at"]) <= now and not v.get("fired")]
    if not to_fire: return
    for sched_key in to_fire:
        entry = data["scheduled"][sched_key]
        for guild in bot.guilds:
            ch = discord.utils.get(guild.text_channels, name=entry["channel"])
            if not ch: continue
            embed = discord.Embed(
                title=entry["title"], description=entry["content"],
                color=discord.Color.blurple(), timestamp=now,
            )
            if entry.get("image_url"):
                embed.set_image(url=entry["image_url"])
            embed.set_footer(text="Scheduled announcement")
            await ch.send(embed=embed)
        entry["fired"] = True
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="check_scheduled"))

# ══════════════════════════════════════════════════════════════════════════════
#  OC MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="oc_add", description="Register a new OC to the database (unlimited OCs per user).")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    name="OC's full name", birthday=f"Birthday in {BIRTHDAY_DISPLAY}", gender="OC's gender",
    pronouns="Pronouns (e.g. she/her)", face_claim="Face claim", main_skill="Primary skill", 
    ethnicity="Ethnicity", nationality="Nationality", 
    profile_picture_url="Direct image URL — required if no file is attached",
    profile_picture_file="Upload image file — required if no URL is provided",
    form_link="Link to OC form (optional)"
)
async def oc_add(
    interaction: discord.Interaction, name: str, birthday: str,
    gender: str, pronouns: str, face_claim: str, main_skill: str, 
    ethnicity: str, nationality: str,
    profile_picture_url: Optional[str] = None, profile_picture_file: Optional[discord.Attachment] = None,
    form_link: Optional[str] = None
):
    try:
        datetime.strptime(birthday, BIRTHDAY_FORMAT)
    except ValueError:
        return await interaction.response.send_message(
            f"❌ Birthday must be in **{BIRTHDAY_DISPLAY}** format (e.g. `2000/06/25`).", ephemeral=True)

    pic_url = None
    if profile_picture_file:
        if not profile_picture_file.content_type or not profile_picture_file.content_type.startswith("image/"):
            return await interaction.response.send_message("❌ Attached file must be an image.", ephemeral=True)
        pic_url = profile_picture_file.url
    elif profile_picture_url:
        if not valid_image_url(profile_picture_url):
            return await interaction.response.send_message("❌ Profile picture must be a direct image URL (.png .jpg .jpeg .gif .webp).", ephemeral=True)
        pic_url = profile_picture_url
    else:
        return await interaction.response.send_message("❌ A profile picture is required. Provide either a URL or upload a file.", ephemeral=True)

    if form_link and not valid_url(form_link):
        return await interaction.response.send_message("❌ Form link must start with http:// or https://.", ephemeral=True)

    data = load_data()
    key  = oc_key_of(name)
    if key in data["ocs"]:
        return await interaction.response.send_message(f"❌ An OC named **{name}** already exists. Use /oc_edit to update.", ephemeral=True)

    data["ocs"][key] = {
        "name": name, "profile_picture": pic_url, "birthday": birthday,
        "gender": gender, "pronouns": pronouns, "face_claim": face_claim,
        "main_skill": main_skill, "ethnicity": ethnicity, "nationality": nationality,
        "form_link": form_link, "owner_id": interaction.user.id,
        "registered_at": now_iso(),
    }
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="oc_add"))

    embed = build_oc_embed(data["ocs"][key], key)
    await interaction.response.send_message(f"**{name}** registered successfully.", embed=embed, ephemeral=True)

    log_ch = discord.utils.get(interaction.guild.text_channels, name=LOG_CHANNEL_NAME)
    if log_ch:
        log_embed = discord.Embed(
            title="✨ New OC Logged",
            description=f"**{name}** has been registered by {interaction.user.mention}.\nWelcome them to the database!",
            color=discord.Color.green(),
            timestamp=now_utc(),
        )
        if pic_url: log_embed.set_thumbnail(url=pic_url)
        log_embed.add_field(name="OC Name", value=name, inline=True)
        log_embed.add_field(name="Registered By", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="OC ID", value=f"`{oc_key_of(name)}`", inline=True)
        await log_ch.send(embed=log_embed)

    await audit(interaction.guild, f"OC added: '{name}' by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="oc_edit", description="Edit an existing OC (only filled fields are changed).")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    oc_name="Name of the OC to edit", name="New name", 
    profile_picture_url="New image URL (optional)", profile_picture_file="Upload new image file (optional)",
    birthday=f"New birthday in {BIRTHDAY_DISPLAY}", gender="New gender",
    pronouns="New pronouns", face_claim="New face claim", main_skill="New main skill", 
    ethnicity="New ethnicity", nationality="New nationality", form_link="New form link"
)
async def oc_edit(
    interaction: discord.Interaction, oc_name: str,
    name: Optional[str] = None, profile_picture_url: Optional[str] = None, profile_picture_file: Optional[discord.Attachment] = None,
    birthday: Optional[str] = None, gender: Optional[str] = None, pronouns: Optional[str] = None, face_claim: Optional[str] = None,
    main_skill: Optional[str] = None, ethnicity: Optional[str] = None, nationality: Optional[str] = None, form_link: Optional[str] = None
):
    data = load_data()
    key  = oc_key_of(oc_name)
    if key not in data["ocs"]:
        return await interaction.response.send_message(f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    oc = data["ocs"][key]
    if not is_dev(interaction) and interaction.user.id != oc.get("owner_id"):
        return await interaction.response.send_message("❌ You can only edit your own OCs.", ephemeral=True)

    if birthday:
        try: datetime.strptime(birthday, BIRTHDAY_FORMAT)
        except ValueError: return await interaction.response.send_message(f"❌ Birthday must be **{BIRTHDAY_DISPLAY}**.", ephemeral=True)

    pic_url = None
    if profile_picture_file:
        if not profile_picture_file.content_type or not profile_picture_file.content_type.startswith("image/"):
            return await interaction.response.send_message("❌ Attached file must be an image.", ephemeral=True)
        pic_url = profile_picture_file.url
    elif profile_picture_url:
        if not valid_image_url(profile_picture_url):
            return await interaction.response.send_message("❌ Profile picture must be a direct image URL.", ephemeral=True)
        pic_url = profile_picture_url

    if form_link and not valid_url(form_link):
        return await interaction.response.send_message("❌ Form link must be a valid URL.", ephemeral=True)

    updates = {
        "name": name, "birthday": birthday, "gender": gender, "pronouns": pronouns, 
        "face_claim": face_claim, "main_skill": main_skill, "ethnicity": ethnicity,
        "nationality": nationality, "form_link": form_link,
    }
    if pic_url: updates["profile_picture"] = pic_url
        
    changes = []
    for field, val in updates.items():
        if val is not None:
            old_val = oc.get(field)
            if field == "birthday":
                display_old = format_birthday_long(old_val) if old_val else "None"
                display_new = format_birthday_long(val)
                changes.append(f"`birthday`: {display_old} → {display_new}")
            else:
                changes.append(f"`{field}`: {old_val} → {val}")
            oc[field] = val

    if not changes:
        return await interaction.response.send_message("❌ No changes were provided.", ephemeral=True)

    new_key = oc_key_of(oc["name"])
    if new_key != key:
        data["ocs"][new_key] = data["ocs"].pop(key)
        for floor in data["floors"].values():
            for room in floor["rooms"].values():
                if key in room["occupants"]:
                    room["occupants"].remove(key)
                    room["occupants"].append(new_key)

    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="oc_edit"))
    
    embed = build_oc_embed(oc, new_key)
    await interaction.response.send_message(f"**{oc['name']}** updated.\n\n**Changes:**\n" + "\n".join(changes), embed=embed, ephemeral=True)


@bot.tree.command(name="oc_delete", description="Delete an OC entirely.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(oc_name="Name of the OC to delete")
async def oc_delete(interaction: discord.Interaction, oc_name: str):
    data = load_data()
    key  = oc_key_of(oc_name)
    if key not in data["ocs"]:
        return await interaction.response.send_message(f"❌ No OC named **{oc_name}** found.", ephemeral=True)
            
    oc = data["ocs"][key]
    if not is_dev(interaction) and interaction.user.id != oc.get("owner_id"):
        return await interaction.response.send_message("❌ You do not have permission to delete this OC.", ephemeral=True)
            
    del data["ocs"][key]
    for floor in data["floors"].values():
        for room in floor.get("rooms", {}).values():
            if key in room.get("occupants", []): room["occupants"].remove(key)
    for gc in data["groupchats"].values():
        if key in gc.get("participants", []): gc["participants"].remove(key)
    for dm in data["dms"].values():
        if key in dm.get("participants", []): dm["participants"].remove(key)
            
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="oc_delete"))
    
    await interaction.response.send_message(f"✅ **{oc['name']}** has been deleted.", ephemeral=True)
    await audit(interaction.guild, f"OC deleted: '{oc['name']}' by {interaction.user} ({interaction.user.id})")


class OCPaginatorView(discord.ui.View):
    def __init__(self, ocs: list, filters_text: str = ""):
        super().__init__(timeout=300)
        self.ocs = ocs
        self.filters_text = filters_text
        self.current_index = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.current_index == 0
        self.next_btn.disabled = self.current_index == len(self.ocs) - 1

    def get_embed(self):
        key, oc = self.ocs[self.current_index]
        embed = build_oc_embed(oc, key)
        footer_text = f"OC ID: {key}  ·  Result {self.current_index + 1} of {len(self.ocs)}"
        if self.filters_text: footer_text += f"  ·  Filters: {self.filters_text}"
        embed.set_footer(text=footer_text)
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary, custom_id="prev_oc")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_index -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary, custom_id="next_oc")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_index += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


@bot.tree.command(name="oc_list", description="Browse all OCs with optional filter.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    filter_by="Field to filter by", filter_value="Value to match (case-insensitive)", search_name="Filter by OC name (partial match)"
)
@app_commands.choices(filter_by=[app_commands.Choice(name=f, value=f) for f in FILTERABLE_FIELDS])
async def oc_list(interaction: discord.Interaction, filter_by: Optional[str] = None, filter_value: Optional[str] = None, search_name: Optional[str] = None):
    data = load_data()
    ocs  = dict(data["ocs"])

    if not ocs: return await interaction.response.send_message("❌ No OCs registered yet.", ephemeral=True)
    if search_name:
        ocs = {k: v for k, v in ocs.items() if search_name.lower() in v["name"].lower()}
    if filter_by and filter_value:
        ocs = {k: v for k, v in ocs.items() if str(v.get(filter_by, "")).lower() == filter_value.lower()}
    if not ocs: return await interaction.response.send_message("❌ No OCs match the given filters.", ephemeral=True)

    filters_active = []
    if search_name: filters_active.append(f"name contains '{search_name}'")
    if filter_by: filters_active.append(f"{filter_by} = {filter_value}")
    filters_text = ", ".join(filters_active)

    items = list(ocs.items())
    view = OCPaginatorView(items, filters_text)
    await interaction.response.send_message(embed=view.get_embed(), view=view)


class BirthdayPaginatorView(discord.ui.View):
    def __init__(self, ocs_sorted: list):
        super().__init__(timeout=300)
        self.ocs = ocs_sorted
        self.current_index = 0
        self._update_buttons()

    def _update_buttons(self):
        if not hasattr(self, "prev_btn"): return
        self.prev_btn.disabled = self.current_index == 0
        self.next_btn.disabled = self.current_index == len(self.ocs) - 1

    def get_embed(self):
        oc = self.ocs[self.current_index]
        today_jst = datetime.now(JST).date()
        days_until = days_until_birthday(oc["birthday"], today_jst)
        bday = datetime.strptime(oc["birthday"], BIRTHDAY_FORMAT).date()

        embed = discord.Embed(
            title="🎂 OC Birthday Calendar",
            color=discord.Color.from_rgb(255, 182, 193),
        )
        if oc.get("profile_picture"):
            embed.set_thumbnail(url=oc["profile_picture"])

        status_str = ""
        age_label = ""
        age_val = ""

        if days_until < 365:
            if days_until == 0:
                status_str = "🎉 Today!"
            else:
                status_str = f"📅 In {days_until} day(s)"
            age_label = "Turning"
            age_val = str(today_jst.year - bday.year)
            embed.description = ""
        elif days_until < 9999:
            pass_date = date(today_jst.year, bday.month, bday.day)
            status_str = f"✅ Turned {today_jst.year - bday.year} on {pass_date.strftime('%B %d')}"
            age_label = "Turned"
            age_val = str(today_jst.year - bday.year)
            embed.description = "— Past Birthdays This Year —"
        else:
            status_str = "Unknown"
            age_label = "Age"
            age_val = str(get_age(oc["birthday"]))

        owner_id = oc.get("owner_id")
        owner_mention = f"<@{owner_id}>" if owner_id else "Unknown"

        embed.add_field(name="OC Name", value=oc["name"], inline=True)
        embed.add_field(name="Birthday", value=format_birthday_long(oc["birthday"]), inline=True)
        embed.add_field(name="Status", value=status_str, inline=True)
        embed.add_field(name=age_label, value=age_val, inline=True)
        embed.add_field(name="Owner", value=owner_mention, inline=True)

        embed.set_footer(text=f"Result {self.current_index + 1} of {len(self.ocs)}  ·  Sorted by proximity to today (JST)")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary, custom_id="prev_bday")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_index -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary, custom_id="next_bday")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_index += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

@bot.tree.command(name="birthday_list", description="View all OC birthdays sorted by proximity to today.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def birthday_list(interaction: discord.Interaction):
    data = load_data()
    ocs = list(data["ocs"].values())
    if not ocs: return await interaction.response.send_message("❌ No OCs registered yet.", ephemeral=True)

    today_jst = datetime.now(JST).date()
    ocs_sorted = sorted(ocs, key=lambda o: days_until_birthday(o["birthday"], today_jst))

    view = BirthdayPaginatorView(ocs_sorted)
    if len(ocs_sorted) == 1:
        view.clear_items()

    await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=False)


# ══════════════════════════════════════════════════════════════════════════════
#  FLOOR & DORM MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="floor_create", description="[Dev] Create a new floor category.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(floor_name="Display name for the floor (e.g. 1st Floor)")
async def floor_create(interaction: discord.Interaction, floor_name: str):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can create floors.", ephemeral=True)
    data = load_data()
    key  = dorm_key_of(floor_name)
    if key in data["floors"]: return await interaction.response.send_message(f"❌ A floor named **{floor_name}** already exists.", ephemeral=True)

    data["floors"][key] = {"name": floor_name, "rooms": {}}
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="floor_create"))

    category = discord.utils.get(interaction.guild.categories, name=floor_name)
    if category is None: await interaction.guild.create_category(floor_name)

    await interaction.response.send_message(f"🏢 Floor **{floor_name}** created.\nUse `/dorm_create` to add rooms.", ephemeral=True)
    await audit(interaction.guild, f"Floor created: '{floor_name}' by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="floor_rename", description="[Dev] Rename a floor without affecting room assignments.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(old_name="Current floor name", new_name="New floor name")
async def floor_rename(interaction: discord.Interaction, old_name: str, new_name: str):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can rename floors.", ephemeral=True)

    old_key = dorm_key_of(old_name)
    new_key = dorm_key_of(new_name)
    data    = load_data()

    if old_key not in data["floors"]: return await interaction.response.send_message(f"❌ No floor named **{old_name}** found.", ephemeral=True)
    if new_key == old_key: return await interaction.response.send_message("❌ New name resolves to the same key.", ephemeral=True)
    if new_key in data["floors"]: return await interaction.response.send_message("❌ Floor with this key already exists.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    floor_data = data["floors"].pop(old_key)
    floor_data["name"] = new_name
    data["floors"][new_key] = floor_data

    category = discord.utils.get(interaction.guild.categories, name=old_name)
    if category:
        try: await category.edit(name=new_name)
        except discord.Forbidden: await interaction.followup.send("⚠️ Lacking permissions to rename Discord category.", ephemeral=True)
        except discord.HTTPException as e: await interaction.followup.send(f"⚠️ Discord category rename failed: {e}", ephemeral=True)

    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="floor_rename"))
    await interaction.followup.send(f"✅ Floor **{old_name}** renamed to **{new_name}**.", ephemeral=True)
    await audit(interaction.guild, f"Floor renamed: '{old_name}' → '{new_name}' by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="floor_delete", description="[Dev] Permanently delete a floor and all its dorm rooms.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(floor_name="Floor to delete", confirm="Type the floor name exactly to confirm")
async def floor_delete(interaction: discord.Interaction, floor_name: str, confirm: str):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can delete floors.", ephemeral=True)
    f_key = dorm_key_of(floor_name)
    data = load_data()
    
    if f_key not in data["floors"]: return await interaction.response.send_message(f"❌ No floor named **{floor_name}** found.", ephemeral=True)
    if confirm != floor_name: return await interaction.response.send_message(f"❌ Confirmation text does not match.", ephemeral=True)
            
    await interaction.response.defer(ephemeral=True)
    floor_data = data["floors"].pop(f_key)
    
    displaced: dict[str, list[str]] = {}
    for r_key, room_data in floor_data["rooms"].items():
        for oc_key in room_data.get("occupants", []):
            displaced.setdefault(oc_key, []).append(room_data["name"])
            
    category = discord.utils.get(interaction.guild.categories, name=floor_data["name"])
    if category:
        for ch in list(category.channels):
            try: await ch.delete(reason=f"Floor '{floor_data['name']}' deleted by dev")
            except (discord.Forbidden, discord.HTTPException) as e: log.warning("floor_delete: %s", e)
        try: await category.delete(reason=f"Floor '{floor_data['name']}' deleted by dev")
        except (discord.Forbidden, discord.HTTPException) as e: log.warning("floor_delete: %s", e)
            
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="floor_delete"))
    
    available_rooms_text = _build_available_rooms_text(data)
    failed_dms = []
    owners_dict = {}
    oc_count = 0
    for oc_key, room_names in displaced.items():
        if oc_key not in data["ocs"]: continue
        owner_id = data["ocs"][oc_key].get("owner_id")
        if not owner_id: continue
        oc_name = data["ocs"][oc_key]["name"]
        oc_count += 1
        
        if owner_id not in owners_dict: owners_dict[owner_id] = {"oc_names": [], "evicted_lines": [], "mentions": []}
        owners_dict[owner_id]["oc_names"].append(oc_name)
        for r_name in room_names: owners_dict[owner_id]["evicted_lines"].append(f"{r_name} on {floor_data['name']}")
            
    displaced_bullets = []
    for owner_id, o_data in owners_dict.items():
        for o_n in o_data["oc_names"]: displaced_bullets.append(f"• {o_n} (<@{owner_id}>)")
        try: owner = await interaction.guild.fetch_member(owner_id)
        except (discord.NotFound, discord.HTTPException):
            failed_dms.append(f"<@{owner_id}>")
            continue
            
        dm_embed = _build_displaced_dm_embed(", ".join(o_data["oc_names"]), o_data["evicted_lines"], available_rooms_text)
        try: await owner.send(embed=dm_embed)
        except (discord.Forbidden, discord.HTTPException): failed_dms.append(owner.mention)
            
    dev_embed = discord.Embed(title="🗑️ Floor Deleted", color=discord.Color.red(), timestamp=now_utc())
    dev_embed.add_field(name="Floor", value=floor_data["name"], inline=True)
    dev_embed.add_field(name="Rooms Deleted", value=f"{len(floor_data['rooms'])}", inline=True)
    if displaced_bullets: dev_embed.add_field(name="OCs Displaced", value=f"{oc_count}\n" + "\n".join(displaced_bullets), inline=False)
    else: dev_embed.add_field(name="OCs Displaced", value="0", inline=False)
        
    dm_status = "✅ All owners notified." if not failed_dms and owners_dict else "None required."
    if failed_dms: dm_status = f"⚠️ Could not DM: {', '.join(failed_dms)}"
    dev_embed.add_field(name="DM Status", value=dm_status, inline=False)
    
    await interaction.followup.send(embed=dev_embed, ephemeral=True)
    await audit(interaction.guild, f"Floor deleted: '{floor_data['name']}' — {len(displaced)} OCs displaced by {interaction.user}")


@bot.tree.command(name="dorm_create", description="[Dev] Create a dorm room inside a floor.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(floor_name="Floor this room belongs to", room_name="Name for the room", capacity="Capacity (2, 3, or 4)")
async def dorm_create(interaction: discord.Interaction, floor_name: str, room_name: str, capacity: int):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can create dorms.", ephemeral=True)
    if capacity not in DORM_SIZES: return await interaction.response.send_message("❌ Capacity must be 2, 3, or 4.", ephemeral=True)

    data = load_data()
    f_key, r_key = dorm_key_of(floor_name), room_key_of(room_name)

    if f_key not in data["floors"]: return await interaction.response.send_message(f"❌ No floor named **{floor_name}** found.", ephemeral=True)
    floor = data["floors"][f_key]
    if r_key in floor["rooms"]: return await interaction.response.send_message(f"❌ Room **{room_name}** already exists.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    floor["rooms"][r_key] = {"name": room_name, "capacity": capacity, "occupants": []}
    
    category = discord.utils.get(interaction.guild.categories, name=floor["name"])
    if category is None: category = await interaction.guild.create_category(floor["name"])

    if not discord.utils.get(interaction.guild.text_channels, name=r_key, category=category):
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.guild.me:           discord.PermissionOverwrite(view_channel=True),
        }
        if interaction.guild.owner: overwrites[interaction.guild.owner] = discord.PermissionOverwrite(view_channel=True)
        await interaction.guild.create_text_channel(r_key, category=category, overwrites=overwrites)

    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="dorm_create"))
    await interaction.followup.send(f"🚪 Room **{room_name}** (capacity: {capacity}) created on **{floor['name']}**.")
    await audit(interaction.guild, f"Room created: '{room_name}' on '{floor['name']}' by {interaction.user}")


@bot.tree.command(name="dorm_rename", description="[Dev] Rename a dorm room without affecting occupant assignments.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(floor_name="Floor this room belongs to", old_room_name="Current room name", new_room_name="New room name")
async def dorm_rename(interaction: discord.Interaction, floor_name: str, old_room_name: str, new_room_name: str):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can rename dorms.", ephemeral=True)

    f_key, old_rkey, new_rkey = dorm_key_of(floor_name), room_key_of(old_room_name), room_key_of(new_room_name)
    data = load_data()

    if f_key not in data["floors"]: return await interaction.response.send_message(f"❌ No floor named **{floor_name}** found.", ephemeral=True)
    floor = data["floors"][f_key]
    if old_rkey not in floor["rooms"]: return await interaction.response.send_message(f"❌ No room named **{old_room_name}**.", ephemeral=True)
    if new_rkey == old_rkey: return await interaction.response.send_message("❌ New name resolves to the same key.", ephemeral=True)
    if new_rkey in floor["rooms"]: return await interaction.response.send_message("❌ A room with this key already exists.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    room_data = floor["rooms"].pop(old_rkey)
    room_data["name"] = new_room_name
    floor["rooms"][new_rkey] = room_data

    category = discord.utils.get(interaction.guild.categories, name=floor["name"])
    if category:
        channel = discord.utils.get(interaction.guild.text_channels, name=old_rkey, category=category)
        if channel:
            try: await channel.edit(name=new_rkey)
            except discord.Forbidden: await interaction.followup.send("⚠️ Lacking permissions to rename Discord channel.", ephemeral=True)
            except discord.HTTPException as e: await interaction.followup.send(f"⚠️ Discord channel rename failed: {e}", ephemeral=True)

    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="dorm_rename"))
    await interaction.followup.send(f"✅ Room **{old_room_name}** renamed to **{new_room_name}**.", ephemeral=True)
    await audit(interaction.guild, f"Room renamed: '{old_room_name}' → '{new_room_name}' on '{floor_name}' by {interaction.user}")


@bot.tree.command(name="dorm_relocate", description="[Dev] Move a dorm room from one floor to another.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(room_name="Room to move", source_floor="Current floor", target_floor="Destination floor")
async def dorm_relocate(interaction: discord.Interaction, room_name: str, source_floor: str, target_floor: str):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can relocate dorm rooms.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    data = load_data()
    src_fkey, tgt_fkey, r_key = dorm_key_of(source_floor), dorm_key_of(target_floor), room_key_of(room_name)

    if src_fkey not in data["floors"]: return await interaction.followup.send(f"❌ Source floor not found.", ephemeral=True)
    if tgt_fkey not in data["floors"]: return await interaction.followup.send(f"❌ Target floor not found.", ephemeral=True)
    if src_fkey == tgt_fkey: return await interaction.followup.send("❌ Source and target floor are the same.", ephemeral=True)
    if r_key not in data["floors"][src_fkey]["rooms"]: return await interaction.followup.send(f"❌ Room not found.", ephemeral=True)
    if r_key in data["floors"][tgt_fkey]["rooms"]: return await interaction.followup.send(f"❌ Room already exists on target.", ephemeral=True)

    room_data = data["floors"][src_fkey]["rooms"].pop(r_key)
    data["floors"][tgt_fkey]["rooms"][r_key] = room_data

    src_category = discord.utils.get(interaction.guild.categories, name=data["floors"][src_fkey]["name"])
    tgt_category = discord.utils.get(interaction.guild.categories, name=data["floors"][tgt_fkey]["name"])
    
    if not tgt_category:
        try: tgt_category = await interaction.guild.create_category(data["floors"][tgt_fkey]["name"])
        except (discord.Forbidden, discord.HTTPException) as e: tgt_category = None

    if src_category and tgt_category:
        channel = discord.utils.get(interaction.guild.text_channels, name=r_key, category=src_category)
        if channel:
            try: await channel.edit(category=tgt_category)
            except discord.HTTPException: pass

    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="dorm_relocate"))

    occupants_display = ", ".join(data["ocs"][o]["name"] for o in room_data["occupants"] if o in data["ocs"]) or "None"
    embed = discord.Embed(title="Dorm Relocated", color=discord.Color.green(), timestamp=now_utc())
    embed.add_field(name="Room", value=room_data["name"], inline=True)
    embed.add_field(name="Moved From", value=data["floors"][src_fkey]["name"], inline=True)
    embed.add_field(name="Moved To", value=data["floors"][tgt_fkey]["name"], inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)
    await audit(interaction.guild, f"Room relocated: '{room_data['name']}' to '{data['floors'][tgt_fkey]['name']}' by {interaction.user}")


@bot.tree.command(name="dorm_delete", description="[Dev] Permanently delete a single dorm room from a floor.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(floor_name="Floor the room belongs to", room_name="Room to delete", confirm="Type the room name exactly to confirm")
async def dorm_delete(interaction: discord.Interaction, floor_name: str, room_name: str, confirm: str):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can delete dorm rooms.", ephemeral=True)
            
    f_key, r_key = dorm_key_of(floor_name), room_key_of(room_name)
    data = load_data()
    
    if f_key not in data["floors"]: return await interaction.response.send_message(f"❌ No floor named **{floor_name}** found.", ephemeral=True)
    floor = data["floors"][f_key]
    if r_key not in floor["rooms"]: return await interaction.response.send_message(f"❌ No room named **{room_name}** found.", ephemeral=True)
    if confirm != room_name: return await interaction.response.send_message(f"❌ Confirmation text does not match.", ephemeral=True)
            
    await interaction.response.defer(ephemeral=True)
    room_data = floor["rooms"].pop(r_key)
    displaced_oc_keys = list(room_data.get("occupants", []))
    
    category = discord.utils.get(interaction.guild.categories, name=floor["name"])
    if category:
        ch = discord.utils.get(interaction.guild.text_channels, name=r_key, category=category)
        if ch:
            try: await ch.delete()
            except (discord.Forbidden, discord.HTTPException): pass
                
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="dorm_delete"))
    
    dev_embed = discord.Embed(title="🗑️ Dorm Deleted", color=discord.Color.red(), timestamp=now_utc())
    dev_embed.add_field(name="Room", value=room_data["name"], inline=True)
    await interaction.followup.send(embed=dev_embed, ephemeral=True)
    await audit(interaction.guild, f"Dorm deleted: '{room_data['name']}' on '{floor['name']}' by {interaction.user}")


@bot.tree.command(name="dorm_assign", description="Assign an OC to a dorm room.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(oc_name="OC name", floor_name="Floor name", room_name="Room name")
async def dorm_assign(interaction: discord.Interaction, oc_name: str, floor_name: str, room_name: str):
    data   = load_data()
    oc_key, f_key, r_key = oc_key_of(oc_name), dorm_key_of(floor_name), room_key_of(room_name)

    if oc_key not in data["ocs"]: return await interaction.response.send_message(f"❌ No OC named **{oc_name}** found.", ephemeral=True)
    if f_key not in data["floors"]: return await interaction.response.send_message(f"❌ No floor named **{floor_name}** found.", ephemeral=True)

    floor = data["floors"][f_key]
    if r_key not in floor["rooms"]: return await interaction.response.send_message(f"❌ Room **{room_name}** does not exist.", ephemeral=True)

    for f_k, f_v in data["floors"].items():
        for r_k, r_v in f_v["rooms"].items():
            if oc_key in r_v["occupants"]: return await interaction.response.send_message(f"❌ **{oc_name}** is already assigned to a room.", ephemeral=True)

    room_data = floor["rooms"][r_key]
    if len(room_data["occupants"]) >= room_data["capacity"]:
        return await interaction.response.send_message(f"❌ **{room_name}** is full.", ephemeral=True)

    room_data["occupants"].append(oc_key)
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="dorm_assign"))

    category = discord.utils.get(interaction.guild.categories, name=floor["name"])
    channel = discord.utils.get(interaction.guild.text_channels, name=r_key, category=category)
    if channel: await channel.set_permissions(interaction.user, view_channel=True, send_messages=True)

    occupants_display = ", ".join(data["ocs"][o]["name"] for o in room_data["occupants"] if o in data["ocs"])
    await interaction.response.send_message(f"🛏️ **{oc_name}** assigned to **{room_name}** on **{floor_name}**.\n👥 Occupants: {occupants_display}")
    await audit(interaction.guild, f"OC '{oc_name}' assigned to '{room_name}' by {interaction.user}")


@bot.tree.command(name="dorm_unassign", description="Remove an OC from their room.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(oc_name="Name of the OC to unassign")
async def dorm_unassign(interaction: discord.Interaction, oc_name: str):
    data   = load_data()
    oc_key = oc_key_of(oc_name)
    if oc_key not in data["ocs"]: return await interaction.response.send_message(f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    for f_key, floor in data["floors"].items():
        for r_key, room_data in floor["rooms"].items():
            if oc_key in room_data["occupants"]:
                room_data["occupants"].remove(oc_key)
                save_data(data)
                asyncio.ensure_future(push_backup_to_discord(data, reason="dorm_unassign"))
                
                category = discord.utils.get(interaction.guild.categories, name=floor["name"])
                ch = discord.utils.get(interaction.guild.text_channels, name=r_key, category=category)
                if ch: await ch.set_permissions(interaction.user, overwrite=None)
                    
                await interaction.response.send_message(f"🚶 **{oc_name}** removed from **{room_data['name']}** on **{floor['name']}**.")
                await audit(interaction.guild, f"OC '{oc_name}' unassigned by {interaction.user}")
                return

    await interaction.response.send_message(f"❌ **{oc_name}** is not assigned to any dorm room.", ephemeral=True)


@bot.tree.command(name="dorm_kick", description="[Dev] Force-remove a user's OC(s) from all dorm assignments.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(user="User whose OC(s) should be removed", oc_name="Optional specific OC to remove")
async def dorm_kick(interaction: discord.Interaction, user: discord.Member, oc_name: Optional[str] = None):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can force-remove.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    data = load_data()
    if oc_name:
        candidate_keys = [oc_key_of(oc_name)]
        if candidate_keys[0] not in data["ocs"]: return await interaction.followup.send(f"❌ OC not found.", ephemeral=True)
    else:
        candidate_keys = [k for k, v in data["ocs"].items() if v.get("owner_id") == user.id]

    removed = []
    for f_key, floor in data["floors"].items():
        for r_key, room_data in floor["rooms"].items():
            for oc_k in candidate_keys:
                if oc_k in room_data["occupants"]:
                    room_data["occupants"].remove(oc_k)
                    removed.append((data["ocs"][oc_k]["name"], floor["name"], room_data["name"]))

                    category = discord.utils.get(interaction.guild.categories, name=floor["name"])
                    ch = discord.utils.get(interaction.guild.text_channels, name=r_key, category=category)
                    if ch:
                        try: await ch.set_permissions(user, overwrite=None)
                        except discord.Forbidden: pass

    if not removed: return await interaction.followup.send(f"❌ No OCs were in dorms.", ephemeral=True)

    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="dorm_kick"))

    lines = "\n".join(f"• **{name}** removed from **{room}**" for name, floor, room in removed)
    await interaction.followup.send(f"🚷 Dorm kick complete for {user.mention}:\n{lines}", ephemeral=True)
    await audit(interaction.guild, f"Dorm kick: {user} by {interaction.user}")


class DormPaginatorView(discord.ui.View):
    def __init__(self, floors_items: list, ocs: dict):
        super().__init__(timeout=300)
        self.floors = floors_items
        self.ocs = ocs
        self.current_index = 0

        options = [
            discord.SelectOption(
                label=floor["name"][:100],
                value=str(i),
                description=f"{len(floor['rooms'])} room(s)",
                default=(i == 0),
            )
            for i, (_, floor) in enumerate(floors_items[:25])
        ]
        if len(floors_items) > 25:
            log.warning("dorm_view: more than 25 floors; dropdown truncated to 25.")

        self.floor_select = discord.ui.Select(
            placeholder="Select a floor…",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="dorm_floor_select",
        )
        self.floor_select.callback = self._floor_select_callback
        self.add_item(self.floor_select)

    async def _floor_select_callback(self, interaction: discord.Interaction):
        selected_index = int(self.floor_select.values[0])
        self.current_index = selected_index
        for opt in self.floor_select.options:
            opt.default = (opt.value == str(selected_index))
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    def get_embed(self):
        if not self.floors:
            return discord.Embed(title="No Floors", description="No floors have been created yet.")
            
        f_key, floor = self.floors[self.current_index]
        embed = discord.Embed(title=f"Floor: {floor['name']}", color=discord.Color.green())
        
        if not floor["rooms"]:
            embed.description = "🪑 No rooms added yet. Use `/dorm_create` to add some."
            
        for r_key, room_data in floor["rooms"].items():
            occupants = [self.ocs[o]["name"] for o in room_data["occupants"] if o in self.ocs]
            is_full   = len(room_data["occupants"]) >= room_data["capacity"]
            status    = "🔴 Full" if is_full else f"🟢 {room_data['capacity'] - len(room_data['occupants'])} spot(s) left"
            occ_str   = ("🏠 " + ", ".join(occupants)) if occupants else "🪑 Empty"
            
            embed.add_field(
                name=f"🚪 {room_data['name']}  [{len(room_data['occupants'])}/{room_data['capacity']}]  {status}",
                value=occ_str, inline=False)
                
        embed.set_footer(text=f"Floor {self.current_index + 1} of {len(self.floors)}")
        return embed

@bot.tree.command(name="dorm_view", description="View floors and dorm occupancy.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def dorm_view(interaction: discord.Interaction):
    data = load_data()
    if not data["floors"]: return await interaction.response.send_message("❌ No floors have been created yet.", ephemeral=True)

    items = list(data["floors"].items())
    view = DormPaginatorView(items, data["ocs"])
    await interaction.response.send_message(embed=view.get_embed(), view=view)


# ══════════════════════════════════════════════════════════════════════════════
#  NEWS & ANNOUNCEMENTS (dev only)
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="news_post", description="[Dev] Post a news article embed.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(title="Article headline", content="Article body", image_url="Optional image URL")
async def news_post(interaction: discord.Interaction, title: str, content: str, image_url: Optional[str] = None):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can post news.", ephemeral=True)
    if image_url and not valid_image_url(image_url): return await interaction.response.send_message("❌ Invalid image URL.", ephemeral=True)

    news_ch = discord.utils.get(interaction.guild.text_channels, name=NEWS_CHANNEL_NAME)
    if not news_ch: return await interaction.response.send_message(f"❌ Channel `#{NEWS_CHANNEL_NAME}` not found.", ephemeral=True)

    embed = discord.Embed(title=title, description=content, color=discord.Color.red(), timestamp=now_utc())
    if image_url: embed.set_image(url=image_url)
    embed.set_footer(text=f"Posted by {interaction.user.display_name}")

    await news_ch.send(embed=embed)
    await interaction.response.send_message(f"Article **{title}** posted.", ephemeral=True)
    await audit(interaction.guild, f"News posted: '{title}' by {interaction.user}")


@bot.tree.command(name="send_embed", description="[Dev] Post a custom embed to any channel as a custom identity.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    channel="Target text channel", title="Embed title (1-256 chars)", content="Embed description (1-4096 chars)",
    custom_username="Optional custom sender name", avatar_url="Optional custom sender avatar URL",
    color="Optional hex color", image_url="Optional image URL", footer_text="Optional footer text"
)
async def send_embed(
    interaction: discord.Interaction, channel: discord.TextChannel, title: str, content: str,
    custom_username: Optional[str] = None, avatar_url: Optional[str] = None, color: Optional[str] = None,
    image_url: Optional[str] = None, footer_text: Optional[str] = None
):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can send custom embeds.", ephemeral=True)
    if len(title) < 1 or len(title) > 256: return await interaction.response.send_message("❌ Title must be 1-256 characters.", ephemeral=True)
    if len(content) < 1 or len(content) > 4096: return await interaction.response.send_message("❌ Content must be 1-4096 characters.", ephemeral=True)

    warning_msg = ""
    resolved_color = discord.Color.blurple()
    if color:
        try: resolved_color = discord.Color(int(color.lstrip('#'), 16))
        except ValueError: warning_msg = "\n⚠️ Invalid color hex, using default."

    embed = discord.Embed(title=title, description=content, color=resolved_color, timestamp=now_utc())
    embed.set_author(name=custom_username or interaction.user.display_name, icon_url=avatar_url or interaction.user.display_avatar.url)
    if image_url: embed.set_image(url=image_url)
    if footer_text: embed.set_footer(text=footer_text)

    await channel.send(embed=embed)
    await interaction.response.send_message(f"✅ Embed sent to {channel.mention}.{warning_msg}", ephemeral=True)
    await audit(interaction.guild, f"[SEND_EMBED] '{title}' to #{channel.name} by {interaction.user}")


class AnnounceView(discord.ui.View):
    def __init__(self, b1_label=None, b1_style=None, b1_url=None, b2_label=None, b2_style=None, b2_url=None):
        super().__init__(timeout=None)
        if b1_label:
            self.add_item(discord.ui.Button(label=b1_label, url=b1_url, style=discord.ButtonStyle.link if b1_url else b1_style or discord.ButtonStyle.primary, custom_id=None if b1_url else "announce_btn1"))
        if b2_label:
            self.add_item(discord.ui.Button(label=b2_label, url=b2_url, style=discord.ButtonStyle.link if b2_url else b2_style or discord.ButtonStyle.secondary, custom_id=None if b2_url else "announce_btn2"))


@bot.tree.command(name="announce", description="[Dev] Post a styled announcement embed to any channel immediately.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(channel="Target channel", title="Announcement title", content="Announcement body")
async def announce(
    interaction: discord.Interaction, channel: discord.TextChannel, title: str, content: str,
    embed_color: Optional[str] = None, image_url: Optional[str] = None, thumbnail_url: Optional[str] = None,
    footnote: Optional[str] = None, oc_note: Optional[str] = None, oc_name: Optional[str] = None,
    button1_label: Optional[str] = None, button1_color: Optional[str] = None, button1_url: Optional[str] = None,
    button2_label: Optional[str] = None, button2_color: Optional[str] = None, button2_url: Optional[str] = None,
    ping_role: Optional[discord.Role] = None,
):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can post announcements.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    warning_msgs = []
    data = load_data()
    oc_display = ""
    if oc_name:
        oc_key = oc_key_of(oc_name)
        if oc_key in data["ocs"]: oc_display = data["ocs"][oc_key]["name"]
        else:
            oc_display = oc_name
            warning_msgs.append(f"⚠️ OC '{oc_name}' not found; using raw string.")

    def _resolve(text: Optional[str]) -> Optional[str]:
        if text is not None: return text.format(server=interaction.guild.name, date=now_utc().strftime("%B %d, %Y"), oc=oc_display)
        return None

    resolved_color = discord.Color.blurple()
    if embed_color:
        try: resolved_color = discord.Color(int(embed_color.lstrip('#'), 16))
        except ValueError: warning_msgs.append("⚠️ Invalid embed color hex; using default blurple.")

    embed = discord.Embed(title=_resolve(title), description=_resolve(content), color=resolved_color, timestamp=now_utc())
    if thumbnail_url: embed.set_thumbnail(url=thumbnail_url)
    if image_url: embed.set_image(url=image_url)
    if oc_note: embed.add_field(name="Note", value=_resolve(oc_note), inline=False)
    if footnote: embed.set_footer(text=_resolve(footnote))

    view = None
    if button1_label or button2_label:
        view = AnnounceView(
            b1_label=button1_label, b1_style=resolve_button_style(button1_color, discord.ButtonStyle.primary), b1_url=button1_url,
            b2_label=button2_label, b2_style=resolve_button_style(button2_color, discord.ButtonStyle.secondary), b2_url=button2_url
        )

    await channel.send(content=ping_role.mention if ping_role else None, embed=embed, view=view)
    warn_str = "\n".join(warning_msgs)
    if warn_str: warn_str = "\n" + warn_str
    await interaction.followup.send(f"✅ Announcement posted to {channel.mention}.{warn_str}", ephemeral=True)
    await audit(interaction.guild, f"[ANNOUNCE] '{title}' to #{channel.name} by {interaction.user}")


@bot.tree.command(name="announce_schedule", description="[Dev] Schedule an announcement for a future time.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(title="Title", content="Body text", fire_at="YYYY-MM-DD HH:MM", channel="Channel", timezone_str="Timezone")
@app_commands.choices(timezone_str=[
    app_commands.Choice(name="UTC", value="UTC"),
    app_commands.Choice(name="JST — Asia/Tokyo (GMT+9)", value="Asia/Tokyo"),
    app_commands.Choice(name="KST — Asia/Seoul (GMT+9)", value="Asia/Seoul"),
    app_commands.Choice(name="PST+8 — Asia/Manila (GMT+8)", value="Asia/Manila"),
    app_commands.Choice(name="EST — America/New_York (GMT-5)", value="America/New_York"),
    app_commands.Choice(name="PST — America/Los_Angeles (GMT-8)", value="America/Los_Angeles"),
    app_commands.Choice(name="BST/GMT — Europe/London", value="Europe/London"),
])
async def announce_schedule(
    interaction: discord.Interaction, title: str, content: str, fire_at: str,
    channel: Optional[discord.TextChannel] = None, image_url: Optional[str] = None, timezone_str: Optional[str] = None
):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can schedule announcements.", ephemeral=True)
    tz_str = timezone_str or "UTC"
    try: tz = ZoneInfo(tz_str)
    except Exception: return await interaction.response.send_message("❌ Unknown timezone.", ephemeral=True)

    try:
        naive_dt = datetime.strptime(fire_at, "%Y-%m-%d %H:%M")
        local_dt = naive_dt.replace(tzinfo=tz)
        fire_dt  = local_dt.astimezone(timezone.utc)
    except ValueError: return await interaction.response.send_message("❌ Format must be YYYY-MM-DD HH:MM.", ephemeral=True)

    if fire_dt <= now_utc(): return await interaction.response.send_message("❌ Time must be in the future.", ephemeral=True)

    resolved_ch = channel or discord.utils.get(interaction.guild.text_channels, name=NEWS_CHANNEL_NAME)
    if not resolved_ch: return await interaction.response.send_message("❌ Target channel not found.", ephemeral=True)

    data = load_data()
    sched_id = f"sched_{int(fire_dt.timestamp())}_{interaction.user.id}"
    data["scheduled"][sched_id] = {
        "title": title, "content": content, "fire_at": fire_dt.isoformat(),
        "channel": resolved_ch.name, "image_url": image_url,
        "created_by": interaction.user.id, "fired": False,
    }
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="announce_schedule"))
    await interaction.response.send_message(f"✅ Scheduled for {fire_dt.strftime('%Y-%m-%d %H:%M UTC')}.", ephemeral=True)
    await audit(interaction.guild, f"Scheduled announcement '{title}' by {interaction.user}")


@bot.tree.command(name="announce_list", description="[Dev] List all pending scheduled announcements.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def announce_list(interaction: discord.Interaction):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can view scheduled announcements.", ephemeral=True)
    data = load_data()
    pending = {k: v for k, v in data["scheduled"].items() if not v.get("fired")}
    if not pending: return await interaction.response.send_message("No pending scheduled announcements.", ephemeral=True)

    embed = discord.Embed(title="Scheduled Announcements", color=discord.Color.blurple())
    for k, v in sorted(pending.items(), key=lambda x: x[1]["fire_at"]):
        fire_utc = datetime.fromisoformat(v["fire_at"])
        embed.add_field(name=v["title"], value=f"Posts at: {fire_utc.strftime('%Y-%m-%d %H:%M UTC')}\nChannel: #{v['channel']}\nID: `{k}`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="announce_cancel", description="[Dev] Cancel a scheduled announcement by ID.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(sched_id="Announcement ID")
async def announce_cancel(interaction: discord.Interaction, sched_id: str):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can cancel announcements.", ephemeral=True)
    data = load_data()
    if sched_id not in data["scheduled"]: return await interaction.response.send_message(f"❌ No scheduled announcement with ID `{sched_id}`.", ephemeral=True)

    title = data["scheduled"][sched_id]["title"]
    del data["scheduled"][sched_id]
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="announce_cancel"))
    await interaction.response.send_message(f"Scheduled announcement **{title}** cancelled.", ephemeral=True)
    await audit(interaction.guild, f"Announcement cancelled: '{title}' by {interaction.user}")


# ══════════════════════════════════════════════════════════════════════════════
#  INSTAGRAM-STYLE POSTS
# ══════════════════════════════════════════════════════════════════════════════

class IGPostView(discord.ui.View):
    def __init__(self, post_id: str, likes: int = 0):
        super().__init__(timeout=None)
        self.post_id = post_id
        self.likes   = likes
        self._update_like_label()

    def _update_like_label(self):
        for child in self.children:
            if getattr(child, "custom_id", "").startswith("ig_like_"):
                child.label = f"🤍 Like  {self.likes}" if self.likes else "🤍 Like"

    @discord.ui.button(label="🤍 Like", style=discord.ButtonStyle.secondary, custom_id="ig_like_btn")
    async def like_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        if self.post_id not in data["instagram"]: return await interaction.response.send_message("❌ Post not found.", ephemeral=True)

        post = data["instagram"][self.post_id]
        post["likes"] = post.get("likes", 0) + 1
        self.likes = post["likes"]
        self._update_like_label()
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="ig_like_btn"))
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="💬 Comment", style=discord.ButtonStyle.primary, custom_id="ig_comment_btn")
    async def comment_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        if self.post_id not in data["instagram"]: return await interaction.response.send_message("❌ Post not found.", ephemeral=True)

        post = data["instagram"][self.post_id]
        if post.get("thread_id"):
            thread = interaction.guild.get_thread(post["thread_id"])
            if thread: return await interaction.response.send_message(f"💬 Thread is already open: {thread.mention}", ephemeral=True)

        ch = interaction.guild.get_channel(post.get("channel_id"))
        if not ch: return await interaction.response.send_message("❌ Original channel not found.", ephemeral=True)

        try: msg = await ch.fetch_message(post["message_id"])
        except discord.NotFound: return await interaction.response.send_message("❌ Original message not found.", ephemeral=True)

        thread = await msg.create_thread(name=f"Comments — {post['username']}", auto_archive_duration=10080)
        post["thread_id"] = thread.id
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="ig_comment_btn"))
        await interaction.response.send_message(f"💬 Comment thread created: {thread.mention}", ephemeral=True)


@bot.tree.command(name="ig_post", description="Post an Instagram-style photo post as your OC.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(oc_name="Your OC's name", username="Instagram username (no @)", caption="Post caption")
async def ig_post(
    interaction: discord.Interaction, oc_name: str, username: str, caption: str,
    photo1_url: Optional[str] = None, photo1_file: Optional[discord.Attachment] = None,
    photo2_url: Optional[str] = None, photo2_file: Optional[discord.Attachment] = None,
    photo3_url: Optional[str] = None, photo3_file: Optional[discord.Attachment] = None,
    photo4_url: Optional[str] = None, photo4_file: Optional[discord.Attachment] = None,
    photo5_url: Optional[str] = None, photo5_file: Optional[discord.Attachment] = None,
    photo6_url: Optional[str] = None, photo6_file: Optional[discord.Attachment] = None,
    photo7_url: Optional[str] = None, photo7_file: Optional[discord.Attachment] = None,
    photo8_url: Optional[str] = None, photo8_file: Optional[discord.Attachment] = None,
    photo9_url: Optional[str] = None, photo9_file: Optional[discord.Attachment] = None,
    photo10_url: Optional[str] = None, photo10_file: Optional[discord.Attachment] = None,
):
    data = load_data()
    oc_key = oc_key_of(oc_name)
    if oc_key not in data["ocs"]: return await interaction.response.send_message(f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    pairs = [
        (photo1_url, photo1_file), (photo2_url, photo2_file), (photo3_url, photo3_file), (photo4_url, photo4_file),
        (photo5_url, photo5_file), (photo6_url, photo6_file), (photo7_url, photo7_file), (photo8_url, photo8_file),
        (photo9_url, photo9_file), (photo10_url, photo10_file),
    ]

    photos = []
    for url, file in pairs:
        if file:
            if not file.content_type or not file.content_type.startswith("image/"): return await interaction.response.send_message("❌ Files must be images.", ephemeral=True)
            photos.append(file.url)
        elif url:
            if not valid_image_url(url): return await interaction.response.send_message(f"❌ Invalid image URL: `{url}`", ephemeral=True)
            photos.append(url)

    if not photos: return await interaction.response.send_message("❌ At least one photo is required.", ephemeral=True)

    oc = data["ocs"][oc_key]
    handle = username if username.startswith("@") else f"@{username}"
    post_id = f"{oc_key}_{int(now_utc().timestamp())}"
    
    data["instagram"][post_id] = {
        "oc_key": oc_key, "username": handle, "caption": caption, "photos": photos,
        "likes": 0, "posted_by": interaction.user.id, "posted_at": now_iso(),
        "channel_id": None, "message_id": None, "last_message_id": None, "thread_id": None,
    }
    save_data(data)

    ig_ch = discord.utils.get(interaction.guild.text_channels, name=INSTAGRAM_CHANNEL_NAME)
    if not ig_ch: return await interaction.response.send_message(f"❌ Channel `#{INSTAGRAM_CHANNEL_NAME}` not found.", ephemeral=True)

    await interaction.response.send_message(f"📸 Posting to {ig_ch.mention}…", ephemeral=True)

    view = IGPostView(post_id, likes=0)
    embed = discord.Embed(description=f"**{handle}**  {caption}", color=discord.Color.from_rgb(225, 48, 108), timestamp=now_utc())
    embed.set_author(name=f"{oc['name']}  ({handle})", icon_url=oc.get("profile_picture"))
    embed.set_image(url=photos[0])

    if len(photos) == 1:
        embed.set_footer(text=f"1 photo  ·  post id: {post_id}")
        last_msg = await ig_ch.send(embed=embed, view=view)
        data["instagram"][post_id]["message_id"] = last_msg.id
        data["instagram"][post_id]["last_message_id"] = last_msg.id
    else:
        embed.set_footer(text=f"{len(photos)} photo(s)  ·  post id: {post_id}")
        first_msg = await ig_ch.send(embed=embed)
        data["instagram"][post_id]["message_id"] = first_msg.id

        for i, photo in enumerate(photos[1:-1], start=2):
            mid_embed = discord.Embed(color=discord.Color.from_rgb(225, 48, 108))
            mid_embed.set_image(url=photo)
            mid_embed.set_footer(text=f"Photo {i}/{len(photos)}")
            await ig_ch.send(embed=mid_embed)

        last_embed = discord.Embed(color=discord.Color.from_rgb(225, 48, 108))
        last_embed.set_image(url=photos[-1])
        last_embed.set_footer(text=f"Photo {len(photos)}/{len(photos)}  ·  post id: {post_id}")
        last_msg = await ig_ch.send(embed=last_embed, view=view)
        data["instagram"][post_id]["last_message_id"] = last_msg.id

    data["instagram"][post_id]["channel_id"] = ig_ch.id
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="ig_post_final"))
    await audit(interaction.guild, f"IG post by OC '{oc_name}' by {interaction.user}  post_id={post_id}")


# ══════════════════════════════════════════════════════════════════════════════
#  DEV DM COMMAND (dev only)
# ══════════════════════════════════════════════════════════════════════════════

class DevDMModal(discord.ui.Modal, title="Reply to Dev"):
    response_text = discord.ui.TextInput(label="Your Response", style=discord.TextStyle.paragraph, max_length=2000)

    def __init__(self, guild_id: int, dev_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.dev_id = dev_id

    async def on_submit(self, interaction: discord.Interaction):
        guild = bot.get_guild(self.guild_id)
        if not guild: return await interaction.response.send_message("❌ Server not found.", ephemeral=True)
            
        ch = discord.utils.get(guild.text_channels, name=DEV_RESPONSE_CHANNEL_NAME)
        if not ch: return await interaction.response.send_message(f"❌ `#{DEV_RESPONSE_CHANNEL_NAME}` not found.", ephemeral=True)
            
        embed = discord.Embed(title="Response to Dev Message", description=self.response_text.value, color=discord.Color.green(), timestamp=now_utc())
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None)
        embed.set_footer(text=f"User ID: {interaction.user.id}  ·  Replying to Dev ID: {self.dev_id}")
        
        await ch.send(embed=embed)
        await interaction.response.send_message("✅ Response sent to developers.", ephemeral=True)


class DevDMView(discord.ui.View):
    def __init__(self, guild_id: int, dev_id: int, reply_label: str = "Reply to Dev", reply_style: discord.ButtonStyle = discord.ButtonStyle.primary):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.dev_id = dev_id
        for child in self.children:
            if isinstance(child, discord.ui.Button) and getattr(child, "custom_id", "") == "dev_dm_reply":
                child.label = reply_label
                child.style = reply_style

    @discord.ui.button(label="Reply to Dev", style=discord.ButtonStyle.primary, custom_id="dev_dm_reply")
    async def reply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DevDMModal(self.guild_id, self.dev_id))


@bot.tree.command(name="dev_dm", description="[Dev] Send a DM to up to 5 users directly.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    user="Primary user to message", message="Message content",
    user2="Optional additional recipient", user3="Optional additional recipient",
    user4="Optional additional recipient", user5="Optional additional recipient",
    require_response="Whether the user gets a button to reply",
    embed_title="Optional custom title", embed_color="Optional hex color",
    oc_name="Optional OC name for {oc} placeholder context",
    reply_label="Optional custom reply button label", reply_color="Optional reply button color",
    footnote="Optional custom footer text",
    thumbnail_url="Optional thumbnail image URL", image_url="Optional main image URL"
)
async def dev_dm(
    interaction: discord.Interaction, 
    user: discord.Member, 
    message: str, 
    user2: Optional[discord.Member] = None, user3: Optional[discord.Member] = None,
    user4: Optional[discord.Member] = None, user5: Optional[discord.Member] = None,
    require_response: bool = False, embed_title: Optional[str] = None, embed_color: Optional[str] = None,
    oc_name: Optional[str] = None, reply_label: Optional[str] = None, reply_color: Optional[str] = None,
    footnote: Optional[str] = None, thumbnail_url: Optional[str] = None, image_url: Optional[str] = None,
):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can use this command.", ephemeral=True)
    if reply_label and len(reply_label) > 80: return await interaction.response.send_message("❌ reply_label exceeds 80 characters.", ephemeral=True)
    if thumbnail_url and not valid_image_url(thumbnail_url): return await interaction.response.send_message("❌ Invalid thumbnail URL.", ephemeral=True)
    if image_url and not valid_image_url(image_url): return await interaction.response.send_message("❌ Invalid image URL.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    recipients = []
    seen = set()
    for u in [user, user2, user3, user4, user5]:
        if u is not None and u.id not in seen:
            seen.add(u.id)
            recipients.append(u)

    warning_msgs = []
    data = load_data()
    
    oc_display = ""
    oc_owner_display = ""
    if oc_name:
        oc_key = oc_key_of(oc_name)
        if oc_key in data["ocs"]:
            oc_display = data["ocs"][oc_key]["name"]
            owner_id = data["ocs"][oc_key].get("owner_id")
            if owner_id:
                member = interaction.guild.get_member(owner_id)
                if member: oc_owner_display = member.display_name
        else:
            warning_msgs.append(f"⚠️ OC '{oc_name}' not found; {{oc}} placeholder will be empty.")

    resolved_color = discord.Color.brand_red()
    if embed_color:
        try: resolved_color = discord.Color(int(embed_color.lstrip('#'), 16))
        except ValueError: warning_msgs.append("⚠️ Invalid color hex, using default.")

    needs_rebuild = "{recipient}" in (message + (footnote or "") + (embed_title or ""))
    successes = []
    failures = []

    def _build_embed(recipient: discord.Member) -> discord.Embed:
        def _resolve(text: Optional[str]) -> str:
            if not text: return ""
            return text.format(
                server=interaction.guild.name, user=interaction.user.display_name,
                oc=oc_display, oc_owner=oc_owner_display,
                date=now_utc().strftime("%B %d, %Y"), recipient=recipient.display_name
            )

        actual_title = _resolve(embed_title) if embed_title else "Message from Server Dev"
        embed = discord.Embed(title=actual_title, description=_resolve(message), color=resolved_color, timestamp=now_utc())

        if thumbnail_url: embed.set_thumbnail(url=thumbnail_url)
        if image_url: embed.set_image(url=image_url)

        if footnote:
            embed.set_footer(text=_resolve(footnote))
        else:
            actual_foot = f"From Server: {interaction.guild.name}"
            if require_response: actual_foot += " · A response is requested."
            embed.set_footer(text=actual_foot)
        return embed

    view = DevDMView(
        guild_id=interaction.guild.id, dev_id=interaction.user.id,
        reply_label=reply_label or "Reply to Dev", reply_style=resolve_button_style(reply_color, discord.ButtonStyle.primary)
    ) if require_response else None

    cached_embed = None if needs_rebuild else _build_embed(recipients[0])

    for r in recipients:
        target_embed = _build_embed(r) if needs_rebuild else cached_embed
        try:
            await r.send(embed=target_embed, view=view)
            successes.append(r.mention)
        except discord.Forbidden:
            failures.append(r.mention)

    result_lines = []
    if successes: result_lines.append(f"✅ Sent to: {', '.join(successes)}")
    if failures: result_lines.append(f"❌ Failed (DMs likely off): {', '.join(failures)}")
    warn_str = "\n".join(warning_msgs)
    if warn_str: result_lines.append(warn_str)

    await interaction.followup.send("\n".join(result_lines), ephemeral=True)
    await audit(interaction.guild, f"Dev DM sent to {[str(r.id) for r in recipients]} by {interaction.user}. Requires response: {require_response}")


# ══════════════════════════════════════════════════════════════════════════════
#  DEBUT DM  (dev only)
# ══════════════════════════════════════════════════════════════════════════════

class DebutView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, oc_name: str, group_name: str, debut_channel_id: int, custom_channel_message: Optional[str] = None, accept_label: Optional[str] = None, accept_style: Optional[discord.ButtonStyle] = None, decline_label: Optional[str] = None, decline_style: Optional[discord.ButtonStyle] = None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.oc_name = oc_name
        self.group_name = group_name
        self.debut_channel_id = debut_channel_id
        self.custom_channel_message = custom_channel_message

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "debut_accept":
                    child.label = accept_label or "Accept"
                    child.style = accept_style or discord.ButtonStyle.success
                elif child.custom_id == "debut_decline":
                    child.label = decline_label or "Decline"
                    child.style = decline_style or discord.ButtonStyle.danger

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="debut_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild   = bot.get_guild(self.guild_id)
        member  = guild.get_member(self.user_id) if guild else None
        channel = guild.get_channel(self.debut_channel_id) if guild else None

        if member and channel:
            await channel.set_permissions(member, view_channel=True, send_messages=True)
            if self.custom_channel_message:
                await channel.send(self.custom_channel_message.format(member=member.mention, oc=self.oc_name, group=self.group_name))

            await interaction.response.edit_message(content=f"You accepted the debut contract for **{self.oc_name}** in **{self.group_name}**. You now have access to the debuts channel.", view=None)
        else:
            await interaction.response.edit_message(content="❌ Could not complete the debut — server or channel not found.", view=None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, custom_id="debut_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=f"You declined the debut contract for **{self.oc_name}** in **{self.group_name}**.", view=None)


@bot.tree.command(name="debut_notify", description="[Dev] DM a user a debut contract for their OC.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    oc_name="OC name", group_name="Group name", message="Custom debut message",
    channel_message="Message posted in #debuts after acceptance", embed_title="Optional custom title",
    embed_color="Optional hex color", accept_label="Optional custom accept button label",
    accept_color="Optional accept button color", decline_label="Optional custom decline button label",
    decline_color="Optional decline button color", footnote="Optional custom footer text",
    oc_placeholder="Optional Note field added to the embed"
)
async def debut_notify(
    interaction: discord.Interaction, oc_name: str, group_name: str, message: str,
    channel_message: Optional[str] = None, embed_title: Optional[str] = None, embed_color: Optional[str] = None,
    accept_label: Optional[str] = None, accept_color: Optional[str] = None, decline_label: Optional[str] = None,
    decline_color: Optional[str] = None, footnote: Optional[str] = None, oc_placeholder: Optional[str] = None,
):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can send debut notifications.", ephemeral=True)
    if accept_label and len(accept_label) > 80: return await interaction.response.send_message("❌ accept_label exceeds 80 characters.", ephemeral=True)
    if decline_label and len(decline_label) > 80: return await interaction.response.send_message("❌ decline_label exceeds 80 characters.", ephemeral=True)

    data = load_data()
    oc_key = oc_key_of(oc_name)

    if oc_key not in data["ocs"]: return await interaction.response.send_message(f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    oc = data["ocs"][oc_key]
    owner_id = oc.get("owner_id")
    if not owner_id: return await interaction.response.send_message(f"❌ **{oc_name}** has no registered owner.", ephemeral=True)

    member = interaction.guild.get_member(owner_id)
    if not member: return await interaction.response.send_message(f"❌ Owner not in this server.", ephemeral=True)

    warning_msgs = []
    resolved_color = discord.Color.gold()
    if embed_color:
        try: resolved_color = discord.Color(int(embed_color.lstrip('#'), 16))
        except ValueError: warning_msgs.append("⚠️ Invalid color hex, using default.")

    debut_ch = discord.utils.get(interaction.guild.text_channels, name=DEBUT_CHANNEL_NAME)
    if not debut_ch:
        cat = discord.utils.get(interaction.guild.categories, name="Special")
        if not cat: cat = await interaction.guild.create_category("Special")
        debut_ch = await interaction.guild.create_text_channel(
            DEBUT_CHANNEL_NAME, category=cat,
            overwrites={
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.guild.me: discord.PermissionOverwrite(view_channel=True),
            })

    actual_title = embed_title.format(oc=oc_name, group=group_name) if embed_title else f"Debut Contract  —  {oc_name}  |  {group_name}"

    embed = discord.Embed(title=actual_title, description=message, color=resolved_color, timestamp=now_utc())
    if oc.get("profile_picture"): embed.set_thumbnail(url=oc["profile_picture"])
    embed.add_field(name="OC", value=oc_name, inline=True)
    embed.add_field(name="Group", value=group_name, inline=True)

    if oc_placeholder: embed.add_field(name="Note", value=oc_placeholder.format(oc=oc_name, group=group_name), inline=False)
    if footnote: embed.set_footer(text=footnote.format(oc=oc_name, group=group_name, server=interaction.guild.name))
    else: embed.set_footer(text=f"From: {interaction.guild.name}")

    view = DebutView(
        guild_id=interaction.guild.id, user_id=member.id, oc_name=oc_name, group_name=group_name,
        debut_channel_id=debut_ch.id, custom_channel_message=channel_message,
        accept_label=accept_label, accept_style=resolve_button_style(accept_color, discord.ButtonStyle.success),
        decline_label=decline_label, decline_style=resolve_button_style(decline_color, discord.ButtonStyle.danger)
    )

    try:
        await member.send(content=f"You have received a debut contract for your OC **{oc_name}** to join **{group_name}**. Please accept or decline below.", embed=embed, view=view)
        warn_str = "\n".join(warning_msgs)
        if warn_str: warn_str = "\n" + warn_str
        await interaction.response.send_message(f"✅ Debut contract sent to {member.mention} for **{oc_name}** ({group_name}).{warn_str}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ Could not DM {member.mention} — they may have DMs disabled.", ephemeral=True)


class GCInviteView(discord.ui.View):
    def __init__(
        self, guild_id: int, invitee_user_id: int, oc_key: str, oc_name: str,
        group_name: str, target_channel_id: int, dev_ids: list[int],
        accept_label: Optional[str] = None, accept_style: Optional[discord.ButtonStyle] = None,
        decline_label: Optional[str] = None, decline_style: Optional[discord.ButtonStyle] = None,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.invitee_user_id = invitee_user_id
        self.oc_key = oc_key
        self.oc_name = oc_name
        self.group_name = group_name
        self.target_channel_id = target_channel_id
        self.dev_ids = dev_ids

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if getattr(child, "custom_id", "") == "gcinvite_accept":
                    child.label = accept_label or "Accept"
                    child.style = accept_style or discord.ButtonStyle.success
                elif getattr(child, "custom_id", "") == "gcinvite_decline":
                    child.label = decline_label or "Decline"
                    child.style = decline_style or discord.ButtonStyle.danger

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="gcinvite_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = bot.get_guild(self.guild_id)
        if not guild: return await interaction.response.edit_message(content="❌ Server not found.", view=None)

        channel = guild.get_channel(self.target_channel_id)
        if not channel: return await interaction.response.edit_message(content="❌ Channel no longer exists.", view=None)

        member = guild.get_member(self.invitee_user_id)
        if not member: return await interaction.response.edit_message(content="❌ Could not resolve your server membership.", view=None)

        await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)

        data = load_data()
        gc_key = f"gc_{channel.name}_{int(now_utc().timestamp())}"
        data["groupchats"][gc_key] = {"name": self.group_name, "channel_id": channel.id, "participants": [self.oc_key], "created_at": now_iso()}
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="gc_invite_accept"))

        await interaction.response.edit_message(content=f"✅ You accepted the group chat invite for **{self.oc_name}** in **{self.group_name}**.", view=None)
        await audit(guild, f"GC invite accepted: OC '{self.oc_name}' granted access to #{channel.name} by {interaction.user}")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, custom_id="gcinvite_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=f"You declined the group chat invite for **{self.oc_name}** in **{self.group_name}**.", view=None)


@bot.tree.command(name="gc_invite", description="[Dev] DM a debuted OC's owner a group-chat invite.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    oc_name="OC to invite", group_name="Display name for GC", message="Custom invitation message",
    target_channel="Existing channel to grant access to",
    embed_title="Optional custom embed title", embed_color="Optional hex color",
    footnote="Optional footer text", thumbnail_url="Optional thumbnail image URL",
    accept_label="Optional custom accept button label", accept_color="Optional accept button color",
    decline_label="Optional custom decline button label", decline_color="Optional decline button color"
)
async def gc_invite(
    interaction: discord.Interaction, oc_name: str, group_name: str, message: str, target_channel: discord.TextChannel,
    embed_title: Optional[str] = None, embed_color: Optional[str] = None, footnote: Optional[str] = None,
    thumbnail_url: Optional[str] = None, accept_label: Optional[str] = None, accept_color: Optional[str] = None,
    decline_label: Optional[str] = None, decline_color: Optional[str] = None,
):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can send group chat invites.", ephemeral=True)
    if not target_channel.permissions_for(interaction.guild.me).manage_permissions:
        return await interaction.response.send_message(f"❌ I lack Manage Permissions in {target_channel.mention}.", ephemeral=True)
    if accept_label and len(accept_label) > 80: return await interaction.response.send_message("❌ accept_label exceeds 80 characters.", ephemeral=True)
    if decline_label and len(decline_label) > 80: return await interaction.response.send_message("❌ decline_label exceeds 80 characters.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    data = load_data()
    oc_key = oc_key_of(oc_name)
    if oc_key not in data["ocs"]: return await interaction.followup.send(f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    oc = data["ocs"][oc_key]
    owner_id = oc.get("owner_id")
    if not owner_id: return await interaction.followup.send(f"❌ **{oc_name}** has no registered owner.", ephemeral=True)

    member = interaction.guild.get_member(owner_id)
    if not member: return await interaction.followup.send(f"❌ Owner not in server.", ephemeral=True)

    dev_ids = [m.id for m in interaction.guild.members if (m.guild_permissions.administrator or m.id == interaction.guild.owner_id) and not m.bot]

    _fmt = {
        "oc": oc["name"], "group": group_name, "server": interaction.guild.name,
        "owner": member.display_name, "channel": target_channel.name,
    }
    def _r(text: Optional[str]) -> Optional[str]: return text.format(**_fmt) if text else None

    warning_msgs = []
    resolved_color = discord.Color.purple()
    if embed_color:
        try: resolved_color = discord.Color(int(embed_color.lstrip('#'), 16))
        except ValueError: warning_msgs.append("⚠️ Invalid embed color, using default purple.")

    actual_title = _r(embed_title) or f"Group Chat Invite  —  {oc['name']}  |  {group_name}"
    embed = discord.Embed(title=actual_title, description=_r(message), color=resolved_color, timestamp=now_utc())

    resolved_thumbnail = None
    if thumbnail_url:
        if valid_image_url(thumbnail_url): resolved_thumbnail = thumbnail_url
        else: warning_msgs.append("⚠️ Invalid thumbnail URL, using OC profile picture.")
    if not resolved_thumbnail and oc.get("profile_picture"): resolved_thumbnail = oc["profile_picture"]
    if resolved_thumbnail: embed.set_thumbnail(url=resolved_thumbnail)

    embed.add_field(name="OC", value=oc["name"], inline=True)
    embed.add_field(name="Group Chat", value=group_name, inline=True)
    embed.add_field(name="Channel", value=target_channel.mention, inline=True)

    if _r(footnote): embed.set_footer(text=_r(footnote))
    else: embed.set_footer(text=f"From: {interaction.guild.name}")

    view = GCInviteView(
        guild_id=interaction.guild.id, invitee_user_id=member.id, oc_key=oc_key, oc_name=oc["name"],
        group_name=group_name, target_channel_id=target_channel.id, dev_ids=dev_ids,
        accept_label=accept_label, accept_style=resolve_button_style(accept_color, discord.ButtonStyle.success),
        decline_label=decline_label, decline_style=resolve_button_style(decline_color, discord.ButtonStyle.danger),
    )

    try:
        await member.send(content=f"You have been invited to add your OC **{oc['name']}** to the group chat **{group_name}**. Accept or decline below.", embed=embed, view=view)
        warn_str = "\n".join(warning_msgs)
        if warn_str: warn_str = "\n" + warn_str
        await interaction.followup.send(f"✅ GC invite sent to {member.mention} for OC **{oc['name']}**.{warn_str}", ephemeral=True)
        await audit(interaction.guild, f"GC invite sent to {member} for OC '{oc['name']}' group '{group_name}' target_channel=#{target_channel.name} by {interaction.user}")
    except discord.Forbidden:
        await interaction.followup.send(f"❌ Could not DM {member.mention} — they may have DMs disabled.", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  OC DM
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="oc_dm", description="Open a private DM channel between two OCs.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(your_oc="Your OC's name", target_oc="OC to DM")
async def oc_dm(interaction: discord.Interaction, your_oc: str, target_oc: str):
    data    = load_data()
    src_key = oc_key_of(your_oc)
    tgt_key = oc_key_of(target_oc)

    if src_key not in data["ocs"]: return await interaction.response.send_message(f"❌ No OC named **{your_oc}** found.", ephemeral=True)
    if tgt_key not in data["ocs"]: return await interaction.response.send_message(f"❌ No OC named **{target_oc}** found.", ephemeral=True)
    if src_key == tgt_key: return await interaction.response.send_message("❌ You cannot DM an OC with themselves.", ephemeral=True)

    dm_key       = "dm_" + "_".join(sorted([src_key, tgt_key]))
    src_oc       = data["ocs"][src_key]
    tgt_oc       = data["ocs"][tgt_key]
    tgt_owner_id = tgt_oc.get("owner_id")
    tgt_member   = interaction.guild.get_member(tgt_owner_id) if tgt_owner_id else None

    if dm_key in data["dms"]:
        existing_ch = interaction.guild.get_channel(data["dms"][dm_key]["channel_id"])
        if existing_ch:
            await existing_ch.set_permissions(interaction.user, view_channel=True, send_messages=True)
            if tgt_member: await existing_ch.set_permissions(tgt_member, view_channel=True, send_messages=True)
            return await interaction.response.send_message(f"DM channel: {existing_ch.mention}", ephemeral=True)

    cat = discord.utils.get(interaction.guild.categories, name="OC DMs")
    if not cat: cat = await interaction.guild.create_category("OC DMs")

    ch_name    = f"dm-{src_key[:15]}-{tgt_key[:15]}"
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.guild.me:           discord.PermissionOverwrite(view_channel=True),
        interaction.user:               discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if tgt_member: overwrites[tgt_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    channel = await interaction.guild.create_text_channel(ch_name, category=cat, overwrites=overwrites)
    data["dms"][dm_key] = {"participants": [src_key, tgt_key], "channel_id": channel.id, "created_at": now_iso()}
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="oc_dm"))

    embed = discord.Embed(
        title="OC DM",
        description=f"Private conversation between **{src_oc['name']}** and **{tgt_oc['name']}**.\nVisible only to the owners of these OCs.",
        color=discord.Color.purple(),
    )
    await channel.send(embed=embed)
    await interaction.response.send_message(f"DM channel created: {channel.mention}", ephemeral=True)
    await audit(interaction.guild, f"OC DM opened: '{your_oc}' <-> '{target_oc}' by {interaction.user}")


# ══════════════════════════════════════════════════════════════════════════════
#  OC GROUP CHAT
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="oc_groupchat", description="Create a group chat channel between multiple OCs.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    your_oc="Your OC", group_name="Group name",
    target_oc1="OC #1", target_oc2="OC #2", target_oc3="OC #3 (optional)",
    target_oc4="OC #4 (optional)", target_oc5="OC #5 (optional)"
)
async def oc_groupchat(
    interaction: discord.Interaction, your_oc: str, group_name: str,
    target_oc1: str, target_oc2: str, target_oc3: Optional[str] = None,
    target_oc4: Optional[str] = None, target_oc5: Optional[str] = None,
):
    data    = load_data()
    src_key = oc_key_of(your_oc)

    if src_key not in data["ocs"]: return await interaction.response.send_message(f"❌ No OC named **{your_oc}** found.", ephemeral=True)

    raw_targets  = [target_oc1, target_oc2, target_oc3, target_oc4, target_oc5]
    target_names = [t for t in raw_targets if t]
    target_keys  = [oc_key_of(t) for t in target_names]

    missing = [n for n, k in zip(target_names, target_keys) if k not in data["ocs"]]
    if missing: return await interaction.response.send_message(f"❌ OC(s) not found: {', '.join(f'**{m}**' for m in missing)}", ephemeral=True)

    dupes = set()
    for k in target_keys:
        if k == src_key or target_keys.count(k) > 1: dupes.add(data["ocs"][k]["name"])
    if dupes: return await interaction.response.send_message(f"❌ Duplicate OC(s): {', '.join(f'**{d}**' for d in dupes)}", ephemeral=True)

    all_keys = [src_key] + target_keys
    cat = discord.utils.get(interaction.guild.categories, name="OC Group Chats")
    if not cat: cat = await interaction.guild.create_category("OC Group Chats")

    ch_name    = "gc-" + group_name.lower().replace(" ", "-")[:28]
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.guild.me:           discord.PermissionOverwrite(view_channel=True),
        interaction.user:               discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    members_added = [interaction.user]
    for oc_k in target_keys:
        owner_id = data["ocs"][oc_k].get("owner_id")
        if owner_id:
            m = interaction.guild.get_member(owner_id)
            if m and m not in members_added:
                overwrites[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
                members_added.append(m)

    channel = await interaction.guild.create_text_channel(ch_name, category=cat, overwrites=overwrites)

    gc_id = f"gc_{int(now_utc().timestamp())}_{src_key[:8]}"
    data["groupchats"][gc_id] = {
        "name": group_name, "participants": all_keys, "channel_id": channel.id,
        "created_by": interaction.user.id, "created_at": now_iso(),
    }
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="oc_groupchat"))

    oc_names_str = ", ".join(data["ocs"][k]["name"] for k in all_keys)
    embed = discord.Embed(
        title=group_name, description=f"**Members:** {oc_names_str}\n\nVisible only to the owners of these OCs.",
        color=discord.Color.blue(), timestamp=now_utc(),
    )
    await channel.send(embed=embed)
    await interaction.response.send_message(f"Group chat **{group_name}** created: {channel.mention}", ephemeral=True)
    await audit(interaction.guild, f"Group chat '{group_name}' created with [{oc_names_str}] by {interaction.user}")


# ══════════════════════════════════════════════════════════════════════════════
#  HELP
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="help", description="Show available bot commands.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def help_cmd(interaction: discord.Interaction):
    dev = is_dev(interaction)

    if dev:
        embed = discord.Embed(
            title="OC Bot — Full Command Reference (Dev)",
            color=discord.Color.gold()
        )
        embed.add_field(name="OC Management", inline=False, value=(
            "`/oc_add` — Register a new OC\n"
            "`/oc_edit` — Edit OC fields\n"
            "`/oc_delete` — Delete your OC\n"
            "`/oc_list` — Browse/filter OCs with paginator\n"
            "`/birthday_list` — View all OC birthdays sorted by proximity to today\n"
        ))
        embed.add_field(name="Floor/Dorm Management", inline=False, value=(
            "`/dorm_assign` — Assign an OC to a room\n"
            "`/dorm_unassign` — Remove an OC from their room\n"
            "`/dorm_view` — View floors and rooms via dropdown selector\n"
        ))
        embed.add_field(name="News & Announcements", inline=False, value=(
            "`/announce_list` — View pending scheduled announcements\n"
        ))
        embed.add_field(name="Instagram", inline=False, value=(
            "`/ig_post` — Post 1–10 photos as your OC\n"
        ))
        embed.add_field(name="Messaging", inline=False, value=(
            "`/oc_dm` — Private DM channel between two OCs\n"
            "`/oc_groupchat` — Group chat for multiple OCs\n"
        ))
        embed.add_field(name="Utility", inline=False, value=(
            "`/ping` — Check the bot's WebSocket latency\n"
        ))
        embed.add_field(name="⚙️ Dev Tools", inline=False, value=(
            "`/floor_create` — Create a floor category\n"
            "`/floor_rename` — Rename a floor (preserves assignments)\n"
            "`/floor_delete` — Permanently delete a floor (and all rooms) with OC owner notification\n"
            "`/dorm_create` — Create a dorm room inside a floor\n"
            "`/dorm_rename` — Rename a dorm room (preserves occupants)\n"
            "`/dorm_relocate` — Move a dorm room from one floor to another\n"
            "`/dorm_delete` — Permanently delete a single dorm room with OC owner notification\n"
            "`/dorm_kick` — Force-remove a user's OC(s) from their dorm assignment\n"
            "`/announce` — Post an immediate custom announcement to any channel\n"
            "`/news_post` — Post a news article embed\n"
            "`/send_embed` — Post a custom embed to any channel as a custom identity\n"
            "`/announce_schedule` — Schedule a future announcement\n"
            "`/announce_cancel` — Cancel a scheduled announcement\n"
            "`/debut_notify` — Send a debut contract DM\n"
            "`/gc_invite` — Invite a debuted OC's owner to a GC with full placeholder support and custom button styling\n"
            "`/dev_dm` — Message up to 5 users directly with OC context and rich placeholder support\n"
            "`/startup` — Manually revive the bot, re-sync commands, restart tasks\n"
        ))
        embed.add_field(name="Notes", inline=False, value=(
            f"Birthday format: **{BIRTHDAY_DISPLAY}**\n"
            f"Filterable fields: {', '.join(FILTERABLE_FIELDS)}\n"
            f"Log channel: `#{LOG_CHANNEL_NAME}`\n"
            f"Audit channel: `#{AUDIT_CHANNEL_NAME}`\n"
            f"News channel: `#{NEWS_CHANNEL_NAME}`\n"
            f"Instagram channel: `#{INSTAGRAM_CHANNEL_NAME}`\n"
            f"Dev Response channel: `#{DEV_RESPONSE_CHANNEL_NAME}`\n"
            f"DB Backup channel: `#{DB_BACKUP_CHANNEL_NAME}` (auto-created)\n"
            f"Debut channel: `#{DEBUT_CHANNEL_NAME}` (auto-created)\n"
            f"Scheduled time format: YYYY-MM-DD HH:MM (specify timezone; default UTC)\n"
            f"Up to {MAX_PHOTOS} photos per Instagram post\n"
        ))
        embed.set_footer(text="You are seeing this view because you have Administrator permissions.")

    else:
        embed = discord.Embed(
            title="OC Bot — Command Reference",
            color=discord.Color.gold()
        )
        embed.add_field(name="OC Management", inline=False, value=(
            "`/oc_add` — Register a new OC (supports file upload; unlimited per user)\n"
            "`/oc_edit` — Edit OC fields (only filled fields change)\n"
            "`/oc_delete` — Delete your OC entirely\n"
            "`/oc_list` — Browse/filter OCs with interactive paginator\n"
            "`/birthday_list` — View all OC birthdays sorted by proximity to today\n"
        ))
        embed.add_field(name="Floor/Dorm Management", inline=False, value=(
            "`/dorm_assign` — Assign an OC to a room\n"
            "`/dorm_unassign` — Remove an OC from their room\n"
            "`/dorm_view` — View floors and rooms via dropdown selector\n"
        ))
        embed.add_field(name="Instagram", inline=False, value=(
            "`/ig_post` — Post 1–10 photos (files or URLs) as your OC\n"
        ))
        embed.add_field(name="Messaging", inline=False, value=(
            "`/oc_dm` — Private DM channel between two OCs\n"
            "`/oc_groupchat` — Group chat for multiple OCs\n"
        ))
        embed.add_field(name="Utility", inline=False, value=(
            "`/ping` — Check the bot's WebSocket latency\n"
        ))
        embed.add_field(name="Notes", inline=False, value=(
            f"Birthday format: **{BIRTHDAY_DISPLAY}**\n"
            f"Filterable fields: {', '.join(FILTERABLE_FIELDS)}\n"
            f"Log channel: `#{LOG_CHANNEL_NAME}`\n"
            f"Audit channel: `#{AUDIT_CHANNEL_NAME}`\n"
            f"Instagram channel: `#{INSTAGRAM_CHANNEL_NAME}`\n"
            f"Debut channel: `#{DEBUT_CHANNEL_NAME}`\n"
            f"Up to {MAX_PHOTOS} photos per Instagram post\n"
        ))

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── Shutdown & Emergency Backup ───────────────────────────────────────────────
def _handle_shutdown(signum, frame):
    log.info("Shutdown signal received (signal %s). Running emergency backup…", signum)
    future = asyncio.run_coroutine_threadsafe(_emergency_backup(), bot.loop)
    try:
        future.result(timeout=15)
    except Exception as e:
        log.error("Emergency backup during shutdown raised: %s", e)
    finally:
        log.info("Shutdown complete. Exiting.")
        sys.exit(0)

async def _emergency_backup():
    if not os.path.exists(DATA_FILE): return
    try:
        data = load_data()
        await push_backup_to_discord(data, reason="EMERGENCY-SHUTDOWN")
        log.info("Emergency backup completed via push_backup_to_discord.")
    except Exception as e:
        log.error("Emergency backup failed: %s", e)


@bot.tree.command(name="startup", description="[Dev] Manually revive the bot: re-sync commands and restart task loops.")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def startup_cmd(interaction: discord.Interaction):
    if not is_dev(interaction): return await interaction.response.send_message("❌ Only devs can use this command.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    try:
        synced = await bot.tree.sync()
        sync_status = f"✅ Command tree re-synced ({len(synced)} command(s))."
    except discord.app_commands.errors.CommandSyncFailure as e:
        sync_status = f"❌ CommandSyncFailure. Detail: {e}"
        log.critical("CommandSyncFailure during /startup: %s", e)
    except discord.HTTPException as e:
        sync_status = f"⚠️ HTTPException during sync (status={e.status}, code={e.code}): {e.text}"
        log.error("HTTPException during /startup sync: %s", e)
    except Exception as e:
        sync_status = f"⚠️ Sync failed: {type(e).__name__}: {e}"
        log.error("Unexpected error during /startup sync: %s", e)

    task_lines = []
    for task_obj, label in [(check_birthdays, "check_birthdays"), (check_scheduled, "check_scheduled"), (auto_backup_db, "auto_backup_db")]:
        if not task_obj.is_running():
            task_obj.start()
            task_lines.append(f"🔄 `{label}` — restarted.")
        else:
            task_lines.append(f"✅ `{label}` — already running.")

    try:
        _ = load_data()
        db_status = "✅ Database readable."
    except Exception as e:
        db_status = f"⚠️ Database error: {e}"

    embed = discord.Embed(title="Manual Startup Report", color=discord.Color.green(), timestamp=now_utc())
    embed.add_field(name="Slash Commands", value=sync_status, inline=False)
    embed.add_field(name="Background Tasks", value="\n".join(task_lines), inline=False)
    embed.add_field(name="Database", value=db_status, inline=False)
    embed.set_footer(text=f"Initiated by {interaction.user} ({interaction.user.id})")

    await interaction.followup.send(embed=embed, ephemeral=True)
    await audit(interaction.guild, f"[STARTUP] Manual startup executed by {interaction.user}")


# ─── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set. Add it in Render → Environment.")

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    webserver.keep_alive()
    bot.run(token, log_handler=None)
