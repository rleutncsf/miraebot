import asyncio
import json
import logging
import os
import re
import threading
import signal
import sys
import webserver
from datetime import datetime, date, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

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
LOG_CHANNEL_NAME          = "oc-log"          # OC registrations, dorm logs
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

FILTERABLE_FIELDS  = ["gender", "pronouns", "face_claim", "main_skill",
                      "ethnicity", "nationality"]

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

def get_age(birthday_str: str):
    try:
        bday  = datetime.strptime(birthday_str, BIRTHDAY_FORMAT).date()
        today = date.today()
        return (today.year - bday.year
                - ((today.month, today.day) < (bday.month, bday.day)))
    except Exception:
        return None

def format_birthday_long(birthday_str: str) -> str:
    """
    Convert a stored birthday string (BIRTHDAY_FORMAT) to long date display,
    e.g. '2000/06/25' → 'June 25, 2000'.
    Falls back to the raw string if parsing fails.
    """
    try:
        bday = datetime.strptime(birthday_str, BIRTHDAY_FORMAT).date()
        return bday.strftime("%B %d, %Y").replace(" 0", " ")  # strips leading zero on day
    except Exception:
        return birthday_str

def is_dev(interaction: discord.Interaction) -> bool:
    return (interaction.guild.owner_id == interaction.user.id
            or interaction.user.guild_permissions.administrator)

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
    """Post a plain audit message to #logs (no embeds, no emojis)."""
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

# ─── Bot ───────────────────────────────────────────────────────────────────────
intents         = discord.Intents.default()
intents.members = True
intents.guilds  = True
bot             = commands.Bot(command_prefix="!", intents=intents)

# ─── Health-check HTTP server (required by Render) ────────────────────────────
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *_):   # silence default stdout spam
        pass

def _run_http():
    HTTPServer(("0.0.0.0", PORT), _Health).serve_forever()

# ─── Guard check to prevent overwriting memory during bootup ───────────────────
@bot.tree.interaction_check
async def global_interaction_check(interaction: discord.Interaction) -> bool:
    if not DB_LOADED:
        await interaction.response.send_message(
            "⏳ The bot is currently booting up and restoring memory. Please try again in a moment.", 
            ephemeral=True
        )
        return False
    return True

# ─── on_ready ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    global DB_LOADED

    if not DB_LOADED:
        # Attempt to restore DB from the backup channel
        for guild in bot.guilds:
            ch = discord.utils.get(guild.text_channels, name=DB_BACKUP_CHANNEL_NAME)
            if not ch:
                try:
                    # Try placing it under a Special category if it exists
                    cat = discord.utils.get(guild.categories, name="Special")
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
                    }
                    ch = await guild.create_text_channel(DB_BACKUP_CHANNEL_NAME, category=cat, overwrites=overwrites)
                except Exception:
                    continue

            if ch:
                try:
                    async for message in ch.history(limit=1):
                        if message.attachments:
                            att = message.attachments[0]
                            if att.filename == "data.json":
                                file_bytes = await att.read()
                                try:
                                    parsed = json.loads(file_bytes)
                                    # Basic schema validation
                                    if not isinstance(parsed, dict):
                                        raise ValueError("Root is not a dict")
                                    for k in ("ocs", "floors", "dorms", "instagram", "dms", "groupchats", "scheduled"):
                                        parsed.setdefault(k, {})
                                except (json.JSONDecodeError, ValueError) as e:
                                    log.error(f"Backup file is corrupt, skipping restore: {e}")
                                    break  # Don't write corrupt data to disk
                                
                                with open(DATA_FILE, "wb") as f:
                                    f.write(file_bytes)
                                log.info("Successfully restored database memory from Discord backup.")
                                break
                except Exception as e:
                    log.error(f"Error fetching DB backup: {e}")
                break

        DB_LOADED = True

    await bot.tree.sync()
    if not check_birthdays.is_running():
        check_birthdays.start()
    if not check_scheduled.is_running():
        check_scheduled.start()
    if not auto_backup_db.is_running():
        auto_backup_db.start()
        
    log.info("Logged in as %s — slash commands synced", bot.user)


# ─── Automated Backup loop ─────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def auto_backup_db():
    global DATA_DIRTY
    if not DATA_DIRTY or not DB_LOADED:
        return
    
    if not os.path.exists(DATA_FILE):
        return

    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name=DB_BACKUP_CHANNEL_NAME)
        if ch:
            try:
                # Upload first
                file = discord.File(DATA_FILE, filename="data.json")
                new_msg = await ch.send(
                    f"Automated DB Backup - {now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}",
                    file=file
                )
                DATA_DIRTY = False

                # Then clean up old backups (skip the one just posted)
                async for msg in ch.history(limit=10):
                    if msg.author == bot.user and msg.id != new_msg.id:
                        try:
                            await msg.delete()
                        except Exception:
                            pass
                break
            except Exception as e:
                log.error(f"Backup failed: {e}")

# ─── Birthday loop ─────────────────────────────────────────────────────────────
@tasks.loop(hours=24)
async def check_birthdays():
    today = date.today()
    data  = load_data()
    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if not ch:
            continue
        for oc in data["ocs"].values():
            try:
                bday = datetime.strptime(oc["birthday"], BIRTHDAY_FORMAT).date()
                if bday.month == today.month and bday.day == today.day:
                    age    = get_age(oc["birthday"])
                    suffix = f" They turn {age} today!" if age else ""
                    await ch.send(f"Happy Birthday to {oc['name']}!{suffix}")
            except Exception:
                pass

# ─── Scheduled announcements loop ─────────────────────────────────────────────
@tasks.loop(seconds=30)
async def check_scheduled():
    data    = load_data()
    now     = now_utc()
    to_fire = [k for k, v in data["scheduled"].items()
               if datetime.fromisoformat(v["fire_at"]) <= now and not v.get("fired")]
    if not to_fire:
        return
    for sched_key in to_fire:
        entry = data["scheduled"][sched_key]
        for guild in bot.guilds:
            ch = discord.utils.get(guild.text_channels, name=entry["channel"])
            if not ch:
                continue
            embed = discord.Embed(
                title       = entry["title"],
                description = entry["content"],
                color       = discord.Color.blurple(),
                timestamp   = now,
            )
            if entry.get("image_url"):
                embed.set_image(url=entry["image_url"])
            embed.set_footer(text="Scheduled announcement")
            await ch.send(embed=embed)
        entry["fired"] = True
    save_data(data)

# ══════════════════════════════════════════════════════════════════════════════
#  OC MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="oc_add", description="Register a new OC to the database (unlimited OCs per user).")
@app_commands.describe(
    name="OC's full name", 
    birthday=f"Birthday in {BIRTHDAY_DISPLAY}", gender="OC's gender",
    pronouns="Pronouns (e.g. she/her)", face_claim="Face claim",
    main_skill="Primary skill", ethnicity="Ethnicity",
    nationality="Nationality", 
    profile_picture_url="Direct image URL — required if no file is attached",
    profile_picture_file="Upload image file — required if no URL is provided",
    form_link="Link to OC form (optional)"
)
async def oc_add(
    interaction: discord.Interaction,
    name: str, birthday: str,
    gender: str, pronouns: str, face_claim: str,
    main_skill: str, ethnicity: str, nationality: str,
    profile_picture_url: Optional[str] = None,
    profile_picture_file: Optional[discord.Attachment] = None,
    form_link: Optional[str] = None
):
    try:
        datetime.strptime(birthday, BIRTHDAY_FORMAT)
    except ValueError:
        return await interaction.response.send_message(
            f"❌ Birthday must be in **{BIRTHDAY_DISPLAY}** format (e.g. `2000/06/25`).",
            ephemeral=True)

    pic_url = None
    if profile_picture_file:
        if not profile_picture_file.content_type or not profile_picture_file.content_type.startswith("image/"):
            return await interaction.response.send_message(
                "❌ Attached file must be an image.", ephemeral=True)
        pic_url = profile_picture_file.url
    elif profile_picture_url:
        if not valid_image_url(profile_picture_url):
            return await interaction.response.send_message(
                "❌ Profile picture must be a direct image URL (.png .jpg .jpeg .gif .webp).",
                ephemeral=True)
        pic_url = profile_picture_url
    else:
        return await interaction.response.send_message(
            "❌ A profile picture is required. Provide either a `profile_picture_url` "
            "or upload a `profile_picture_file`.",
            ephemeral=True)

    if form_link and not valid_url(form_link):
        return await interaction.response.send_message(
            "❌ Form link must start with http:// or https://.", ephemeral=True)

    data = load_data()
    key  = oc_key_of(name)
    if key in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ An OC named **{name}** already exists. Use /oc_edit to update.",
            ephemeral=True)

    data["ocs"][key] = {
        "name": name, "profile_picture": pic_url, "birthday": birthday,
        "gender": gender, "pronouns": pronouns, "face_claim": face_claim,
        "main_skill": main_skill, "ethnicity": ethnicity, "nationality": nationality,
        "form_link": form_link, "owner_id": interaction.user.id,
        "registered_at": now_iso(),
    }
    save_data(data)

    embed = build_oc_embed(data["ocs"][key], key)
    await interaction.response.send_message(
        f"**{name}** registered successfully.", embed=embed, ephemeral=True)

    log_ch = discord.utils.get(interaction.guild.text_channels, name=LOG_CHANNEL_NAME)
    if log_ch:
        await log_ch.send(
            f"New OC registered: **{name}** — added by {interaction.user.mention}",
            embed=embed)

    await audit(interaction.guild,
                f"OC added: '{name}' by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="oc_edit",
                  description="Edit an existing OC (only filled fields are changed).")
@app_commands.describe(
    oc_name="Name of the OC to edit",
    name="New name", 
    profile_picture_url="New image URL (optional)",
    profile_picture_file="Upload new image file (optional)",
    birthday=f"New birthday in {BIRTHDAY_DISPLAY} format (e.g. 2000/06/25)", gender="New gender",
    pronouns="New pronouns", face_claim="New face claim",
    main_skill="New main skill", ethnicity="New ethnicity",
    nationality="New nationality", form_link="New form link"
)
async def oc_edit(
    interaction: discord.Interaction, oc_name: str,
    name: Optional[str] = None, 
    profile_picture_url: Optional[str] = None,
    profile_picture_file: Optional[discord.Attachment] = None,
    birthday: Optional[str] = None, gender: Optional[str] = None,
    pronouns: Optional[str] = None, face_claim: Optional[str] = None,
    main_skill: Optional[str] = None, ethnicity: Optional[str] = None,
    nationality: Optional[str] = None, form_link: Optional[str] = None
):
    data = load_data()
    key  = oc_key_of(oc_name)
    if key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    oc = data["ocs"][key]
    if not is_dev(interaction) and interaction.user.id != oc.get("owner_id"):
        return await interaction.response.send_message(
            "❌ You can only edit your own OCs.", ephemeral=True)

    if birthday:
        try:
            datetime.strptime(birthday, BIRTHDAY_FORMAT)
        except ValueError:
            return await interaction.response.send_message(
                f"❌ Birthday must be **{BIRTHDAY_DISPLAY}**.", ephemeral=True)

    pic_url = None
    if profile_picture_file:
        if not profile_picture_file.content_type or not profile_picture_file.content_type.startswith("image/"):
            return await interaction.response.send_message(
                "❌ Attached file must be an image.", ephemeral=True)
        pic_url = profile_picture_file.url
    elif profile_picture_url:
        if not valid_image_url(profile_picture_url):
            return await interaction.response.send_message(
                "❌ Profile picture must be a direct image URL.", ephemeral=True)
        pic_url = profile_picture_url

    if form_link and not valid_url(form_link):
        return await interaction.response.send_message(
            "❌ Form link must be a valid URL.", ephemeral=True)

    updates = {
        "name": name, "birthday": birthday,
        "gender": gender, "pronouns": pronouns, "face_claim": face_claim,
        "main_skill": main_skill, "ethnicity": ethnicity,
        "nationality": nationality, "form_link": form_link,
    }
    
    if pic_url:
        updates["profile_picture"] = pic_url
        
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
        return await interaction.response.send_message(
            "❌ No changes were provided.", ephemeral=True)

    new_key = oc_key_of(oc["name"])
    if new_key != key:
        data["ocs"][new_key] = data["ocs"].pop(key)
        for floor in data["floors"].values():
            for room in floor["rooms"].values():
                if key in room["occupants"]:
                    room["occupants"].remove(key)
                    room["occupants"].append(new_key)

    save_data(data)
    embed = build_oc_embed(oc, new_key)
    await interaction.response.send_message(
        f"**{oc['name']}** updated.\n\n**Changes:**\n" + "\n".join(changes),
        embed=embed, ephemeral=True)


@bot.tree.command(name="oc_delete", description="Delete an OC entirely.")
@app_commands.describe(oc_name="Name of the OC to delete")
async def oc_delete(interaction: discord.Interaction, oc_name: str):
    data = load_data()
    key  = oc_key_of(oc_name)
    
    if key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)
            
    oc = data["ocs"][key]
    
    if not is_dev(interaction) and interaction.user.id != oc.get("owner_id"):
        return await interaction.response.send_message(
            "❌ You do not have permission to delete this OC. Only the owner or a dev can delete it.", 
            ephemeral=True)
            
    del data["ocs"][key]
    
    # Remove from floors/rooms
    for floor in data["floors"].values():
        for room in floor.get("rooms", {}).values():
            if key in room.get("occupants", []):
                room["occupants"].remove(key)
                
    # Remove from group chats
    for gc in data["groupchats"].values():
        if key in gc.get("participants", []):
            gc["participants"].remove(key)
            
    # Remove from DMs
    for dm in data["dms"].values():
        if key in dm.get("participants", []):
            dm["participants"].remove(key)
            
    save_data(data)
    
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
        if self.filters_text:
            footer_text += f"  ·  Filters: {self.filters_text}"
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
@app_commands.describe(
    filter_by="Field to filter by",
    filter_value="Value to match (case-insensitive)",
    search_name="Filter by OC name (partial match)",
)
@app_commands.choices(filter_by=[
    app_commands.Choice(name=f, value=f) for f in FILTERABLE_FIELDS
])
async def oc_list(
    interaction: discord.Interaction,
    filter_by: Optional[str] = None,
    filter_value: Optional[str] = None,
    search_name: Optional[str] = None,
):
    data = load_data()
    ocs  = dict(data["ocs"])

    if not ocs:
        return await interaction.response.send_message(
            "❌ No OCs registered yet.", ephemeral=True)

    # Filter by name (partial, case-insensitive)
    if search_name:
        ocs = {k: v for k, v in ocs.items()
               if search_name.lower() in v["name"].lower()}

    # Filter by field (exact, case-insensitive)
    if filter_by and filter_value:
        ocs = {k: v for k, v in ocs.items()
               if str(v.get(filter_by, "")).lower() == filter_value.lower()}

    if not ocs:
        return await interaction.response.send_message(
            "❌ No OCs match the given filters.", ephemeral=True)

    filters_active = []
    if search_name:
        filters_active.append(f"name contains '{search_name}'")
    if filter_by:
        filters_active.append(f"{filter_by} = {filter_value}")
    filters_text = ", ".join(filters_active)

    items = list(ocs.items())
    view = OCPaginatorView(items, filters_text)
    await interaction.response.send_message(embed=view.get_embed(), view=view)


# ══════════════════════════════════════════════════════════════════════════════
#  FLOOR & DORM MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="floor_create", description="[Dev] Create a new floor category.")
@app_commands.describe(floor_name="Display name for the floor (e.g. 1st Floor)")
async def floor_create(interaction: discord.Interaction, floor_name: str):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can create floors.", ephemeral=True)

    data = load_data()
    key  = dorm_key_of(floor_name)
    if key in data["floors"]:
        return await interaction.response.send_message(
            f"❌ A floor named **{floor_name}** already exists.", ephemeral=True)

    data["floors"][key] = {"name": floor_name, "rooms": {}}
    save_data(data)

    category = discord.utils.get(interaction.guild.categories, name=floor_name)
    if category is None:
        await interaction.guild.create_category(floor_name)

    await interaction.response.send_message(
        f"🏢 Floor **{floor_name}** created.\n"
        f"Use `/dorm_create` to add rooms to this floor.", ephemeral=True)

    await audit(interaction.guild,
                f"Floor created: '{floor_name}' by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="floor_rename", description="[Dev] Rename a floor without affecting room assignments.")
@app_commands.describe(
    old_name="Current floor name",
    new_name="New floor name",
)
async def floor_rename(interaction: discord.Interaction, old_name: str, new_name: str):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can rename floors.", ephemeral=True)

    # --- Validation ---
    old_key = dorm_key_of(old_name)
    new_key = dorm_key_of(new_name)
    data    = load_data()

    if old_key not in data["floors"]:
        return await interaction.response.send_message(
            f"❌ No floor named **{old_name}** found.", ephemeral=True)

    if new_key == old_key:
        return await interaction.response.send_message(
            "❌ The new name resolves to the same key as the current name. "
            "Choose a more distinct name.", ephemeral=True)

    if new_key in data["floors"]:
        return await interaction.response.send_message(
            f"❌ A floor with the key `{new_key}` already exists "
            f"(from name **{data['floors'][new_key]['name']}**). "
            "Pick a different name.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    # --- Data migration ---
    floor_data          = data["floors"].pop(old_key)       # remove old key
    floor_data["name"]  = new_name                          # update display name
    data["floors"][new_key] = floor_data                    # re-insert under new key

    # --- Rename Discord category ---
    category = discord.utils.get(interaction.guild.categories, name=old_name)
    if category:
        try:
            await category.edit(name=new_name)
        except discord.Forbidden:
            await interaction.followup.send(
                "⚠️ Floor data renamed in DB but I lack permissions to rename the Discord category.",
                ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"⚠️ Floor data renamed in DB but Discord category rename failed: {e}",
                ephemeral=True)
    else:
        # Non-fatal: category may have been manually deleted
        log.warning("floor_rename: Discord category '%s' not found; skipping rename.", old_name)

    save_data(data)

    await interaction.followup.send(
        f"✅ Floor **{old_name}** renamed to **{new_name}**. "
        f"All room assignments preserved.", ephemeral=True)

    await audit(interaction.guild,
                f"Floor renamed: '{old_name}' → '{new_name}' "
                f"by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="dorm_create", description="[Dev] Create a dorm room inside a floor.")
@app_commands.describe(
    floor_name="Name of the floor this room belongs to",
    room_name="Name for the room (e.g. Room 101)",
    capacity="Capacity for this room (2, 3, or 4)",
)
async def dorm_create(
    interaction: discord.Interaction, 
    floor_name: str, 
    room_name: str,
    capacity: int
):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can create dorms.", ephemeral=True)

    if capacity not in DORM_SIZES:
        return await interaction.response.send_message(
            f"❌ Capacity must be **2**, **3**, or **4**, not `{capacity}`.", ephemeral=True)

    data = load_data()
    f_key = dorm_key_of(floor_name)
    r_key = room_key_of(room_name)

    if f_key not in data["floors"]:
        return await interaction.response.send_message(
            f"❌ No floor named **{floor_name}** found. Use `/floor_create` first.", ephemeral=True)

    floor = data["floors"][f_key]
    if r_key in floor["rooms"]:
        return await interaction.response.send_message(
            f"❌ A room named **{room_name}** already exists on this floor.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    floor["rooms"][r_key] = {"name": room_name, "capacity": capacity, "occupants": []}
    
    category = discord.utils.get(interaction.guild.categories, name=floor["name"])
    if category is None:
        category = await interaction.guild.create_category(floor["name"])

    ch_name = f"{r_key}"
    if not discord.utils.get(interaction.guild.text_channels, name=ch_name, category=category):
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.guild.me:           discord.PermissionOverwrite(view_channel=True),
        }
        if interaction.guild.owner:
            overwrites[interaction.guild.owner] = discord.PermissionOverwrite(view_channel=True)
        await interaction.guild.create_text_channel(
            ch_name, category=category, overwrites=overwrites)

    save_data(data)

    await interaction.followup.send(
        f"🚪 Room **{room_name}** (capacity: {capacity}) created on **{floor['name']}**.")

    await audit(interaction.guild,
                f"Room created: '{room_name}' on '{floor['name']}' (cap {capacity}) "
                f"by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="dorm_rename", description="[Dev] Rename a dorm room without affecting occupant assignments.")
@app_commands.describe(
    floor_name="Floor this room belongs to",
    old_room_name="Current room name",
    new_room_name="New room name",
)
async def dorm_rename(
    interaction: discord.Interaction,
    floor_name: str,
    old_room_name: str,
    new_room_name: str,
):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can rename dorms.", ephemeral=True)

    # --- Validation ---
    f_key    = dorm_key_of(floor_name)
    old_rkey = room_key_of(old_room_name)
    new_rkey = room_key_of(new_room_name)
    data     = load_data()

    if f_key not in data["floors"]:
        return await interaction.response.send_message(
            f"❌ No floor named **{floor_name}** found.", ephemeral=True)

    floor = data["floors"][f_key]

    if old_rkey not in floor["rooms"]:
        return await interaction.response.send_message(
            f"❌ No room named **{old_room_name}** on floor **{floor_name}**.",
            ephemeral=True)

    if new_rkey == old_rkey:
        return await interaction.response.send_message(
            "❌ The new room name resolves to the same key as the current name. "
            "Choose a more distinct name.", ephemeral=True)

    if new_rkey in floor["rooms"]:
        return await interaction.response.send_message(
            f"❌ A room with the key `{new_rkey}` already exists on this floor "
            f"(from name **{floor['rooms'][new_rkey]['name']}**). "
            "Pick a different name.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    # --- Data migration: rename key and display name; occupants list is preserved ---
    room_data           = floor["rooms"].pop(old_rkey)
    room_data["name"]   = new_room_name
    floor["rooms"][new_rkey] = room_data

    # --- Rename Discord text channel inside the floor's category ---
    category = discord.utils.get(interaction.guild.categories, name=floor["name"])
    if category:
        channel = discord.utils.get(
            interaction.guild.text_channels, name=old_rkey, category=category)
        if channel:
            try:
                await channel.edit(name=new_rkey)
            except discord.Forbidden:
                await interaction.followup.send(
                    "⚠️ Room data renamed in DB but I lack permissions to rename the Discord channel.",
                    ephemeral=True)
            except discord.HTTPException as e:
                await interaction.followup.send(
                    f"⚠️ Room data renamed in DB but Discord channel rename failed: {e}",
                    ephemeral=True)
        else:
            log.warning(
                "dorm_rename: Discord channel '%s' not found in category '%s'; skipping.",
                old_rkey, floor["name"])
    else:
        log.warning(
            "dorm_rename: Discord category '%s' not found; skipping channel rename.", floor["name"])

    save_data(data)

    await interaction.followup.send(
        f"✅ Room **{old_room_name}** on **{floor_name}** renamed to **{new_room_name}**. "
        f"All occupant assignments preserved.", ephemeral=True)

    await audit(interaction.guild,
                f"Room renamed: '{old_room_name}' → '{new_room_name}' "
                f"on floor '{floor_name}' "
                f"by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="dorm_assign", description="Assign an OC to a dorm room.")
@app_commands.describe(
    oc_name="OC name", floor_name="Floor name", room_name="Room name")
async def dorm_assign(
    interaction: discord.Interaction, oc_name: str, floor_name: str, room_name: str
):
    data   = load_data()
    oc_key = oc_key_of(oc_name)
    f_key  = dorm_key_of(floor_name)
    r_key  = room_key_of(room_name)

    if oc_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    if f_key not in data["floors"]:
        return await interaction.response.send_message(
            f"❌ No floor named **{floor_name}** found.", ephemeral=True)

    floor = data["floors"][f_key]
    if r_key not in floor["rooms"]:
        return await interaction.response.send_message(
            f"❌ Room **{room_name}** does not exist on **{floor_name}**.", ephemeral=True)

    for f_k, f_v in data["floors"].items():
        for r_k, r_v in f_v["rooms"].items():
            if oc_key in r_v["occupants"]:
                return await interaction.response.send_message(
                    f"❌ **{oc_name}** is already assigned to "
                    f"**{r_v['name']}** on **{f_v['name']}**.",
                    ephemeral=True)

    room_data = floor["rooms"][r_key]
    if len(room_data["occupants"]) >= room_data["capacity"]:
        return await interaction.response.send_message(
            f"❌ **{room_name}** is full ({room_data['capacity']}/{room_data['capacity']}).",
            ephemeral=True)

    room_data["occupants"].append(oc_key)
    save_data(data)

    category = discord.utils.get(interaction.guild.categories, name=floor["name"])
    ch_name = f"{r_key}"
    channel = discord.utils.get(interaction.guild.text_channels, name=ch_name, category=category)
    if channel:
        await channel.set_permissions(
            interaction.user, view_channel=True, send_messages=True)

    occupants_display = ", ".join(
        data["ocs"][o]["name"] for o in room_data["occupants"] if o in data["ocs"])
    is_full = len(room_data["occupants"]) >= room_data["capacity"]

    await interaction.response.send_message(
        f"🛏️ **{oc_name}** assigned to **{room_name}** on **{floor_name}**.\n"
        f"👥 Occupants: {occupants_display}\n"
        f"{'🔴 Full' if is_full else '🟢 Has space'}")

    log_ch = discord.utils.get(interaction.guild.text_channels, name=LOG_CHANNEL_NAME)
    if log_ch:
        spots      = room_data["capacity"] - len(room_data["occupants"])
        room_note  = "🔴 Room is now full." if is_full else f"🟢 {spots} spot(s) remaining."
        await log_ch.send(
            f"🛏️ **{oc_name}** assigned to **{room_name}** on **{floor_name}** "
            f"by {interaction.user.mention}. {room_note}")

    await audit(interaction.guild,
                f"OC '{oc_name}' assigned to '{room_name}' on '{floor_name}' "
                f"by {interaction.user} ({interaction.user.id}). "
                f"{'Room full.' if is_full else ''}")


@bot.tree.command(name="dorm_unassign", description="Remove an OC from their room.")
@app_commands.describe(oc_name="Name of the OC to unassign")
async def dorm_unassign(interaction: discord.Interaction, oc_name: str):
    data   = load_data()
    oc_key = oc_key_of(oc_name)
    if oc_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    for f_key, floor in data["floors"].items():
        for r_key, room_data in floor["rooms"].items():
            if oc_key in room_data["occupants"]:
                room_data["occupants"].remove(oc_key)
                save_data(data)
                
                category = discord.utils.get(interaction.guild.categories, name=floor["name"])
                ch = discord.utils.get(interaction.guild.text_channels, name=f"{r_key}", category=category)
                if ch:
                    await ch.set_permissions(interaction.user, overwrite=None)
                    
                await interaction.response.send_message(
                    f"🚶 **{oc_name}** removed from **{room_data['name']}** on **{floor['name']}**.")
                await audit(interaction.guild,
                            f"OC '{oc_name}' unassigned from dorm "
                            f"by {interaction.user} ({interaction.user.id})")
                return

    await interaction.response.send_message(
        f"❌ **{oc_name}** is not assigned to any dorm room.", ephemeral=True)


@bot.tree.command(
    name="dorm_kick",
    description="[Dev] Force-remove a user's OC(s) from all dorm assignments."
)
@app_commands.describe(
    user="The server member whose OC(s) should be removed from their dorm",
    oc_name="Specific OC to remove (optional — omit to remove ALL of this user's OCs)",
)
async def dorm_kick(
    interaction: discord.Interaction,
    user: discord.Member,
    oc_name: Optional[str] = None,
):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can force-remove dorm occupants.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    data = load_data()

    # Determine which OC keys belong to this user and are assigned to a room
    if oc_name:
        candidate_keys = [oc_key_of(oc_name)]
        if candidate_keys[0] not in data["ocs"]:
            return await interaction.followup.send(
                f"❌ No OC named **{oc_name}** found.", ephemeral=True)
        if data["ocs"][candidate_keys[0]].get("owner_id") != user.id:
            return await interaction.followup.send(
                f"❌ **{oc_name}** is not registered to {user.mention}.",
                ephemeral=True)
    else:
        # All OCs owned by this user
        candidate_keys = [
            k for k, v in data["ocs"].items()
            if v.get("owner_id") == user.id
        ]
        if not candidate_keys:
            return await interaction.followup.send(
                f"❌ {user.mention} has no registered OCs.", ephemeral=True)

    removed = []   # list of (oc_display_name, floor_name, room_name)

    for f_key, floor in data["floors"].items():
        for r_key, room_data in floor["rooms"].items():
            for oc_k in candidate_keys:
                if oc_k in room_data["occupants"]:
                    room_data["occupants"].remove(oc_k)
                    oc_display = data["ocs"][oc_k]["name"]
                    removed.append((oc_display, floor["name"], room_data["name"]))

                    # Revoke per-user channel permission override if present
                    category = discord.utils.get(
                        interaction.guild.categories, name=floor["name"])
                    ch = discord.utils.get(
                        interaction.guild.text_channels,
                        name=r_key, category=category)
                    if ch:
                        try:
                            await ch.set_permissions(user, overwrite=None)
                        except discord.Forbidden:
                            pass

    if not removed:
        return await interaction.followup.send(
            f"❌ None of {user.mention}'s OC(s) are currently assigned to a dorm.",
            ephemeral=True)

    save_data(data)

    lines = "\n".join(
        f"• **{name}** removed from **{room}** on **{floor}**"
        for name, floor, room in removed
    )
    await interaction.followup.send(
        f"🚷 Dorm kick complete for {user.mention}:\n{lines}", ephemeral=True)

    log_ch = discord.utils.get(interaction.guild.text_channels, name=LOG_CHANNEL_NAME)
    if log_ch:
        await log_ch.send(
            f"🚷 **Dorm kick** by {interaction.user.mention}: "
            f"{user.mention}'s OC(s) were force-removed.\n{lines}")

    await audit(
        interaction.guild,
        f"Dorm kick: {user} ({user.id}) — OCs removed: "
        f"{[r[0] for r in removed]} by {interaction.user} ({interaction.user.id})"
    )


class DormPaginatorView(discord.ui.View):
    def __init__(self, floors_items: list, ocs: dict):
        super().__init__(timeout=300)
        self.floors = floors_items
        self.ocs = ocs
        self.current_index = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.current_index == 0
        self.next_btn.disabled = len(self.floors) == 0 or self.current_index == len(self.floors) - 1

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

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary, custom_id="prev_floor")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_index -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary, custom_id="next_floor")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_index += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

@bot.tree.command(name="dorm_view", description="View floors and dorm occupancy.")
async def dorm_view(interaction: discord.Interaction):
    data = load_data()
    if not data["floors"]:
        return await interaction.response.send_message(
            "❌ No floors have been created yet.", ephemeral=True)

    items = list(data["floors"].items())
    view = DormPaginatorView(items, data["ocs"])
    await interaction.response.send_message(embed=view.get_embed(), view=view)


# ══════════════════════════════════════════════════════════════════════════════
#  NEWS  (dev only)
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="news_post", description="[Dev] Post a news article embed.")
@app_commands.describe(
    title="Article headline", content="Article body",
    image_url="Optional image URL")
async def news_post(
    interaction: discord.Interaction,
    title: str,
    content: str,
    image_url: Optional[str] = None,
):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can post news.", ephemeral=True)

    if image_url and not valid_image_url(image_url):
        return await interaction.response.send_message(
            "❌ Image URL must be a direct image link (.png .jpg .jpeg .gif .webp).",
            ephemeral=True)

    news_ch = discord.utils.get(interaction.guild.text_channels, name=NEWS_CHANNEL_NAME)
    if not news_ch:
        return await interaction.response.send_message(
            f"❌ Channel `#{NEWS_CHANNEL_NAME}` not found. Please create it first.",
            ephemeral=True)

    embed = discord.Embed(
        title=title, description=content,
        color=discord.Color.red(), timestamp=now_utc())
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text=f"Posted by {interaction.user.display_name}")

    await news_ch.send(embed=embed)
    await interaction.response.send_message(
        f"Article **{title}** posted to {news_ch.mention}.", ephemeral=True)

    await audit(interaction.guild,
                f"News posted: '{title}' by {interaction.user} ({interaction.user.id})")


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULED ANNOUNCEMENTS  (dev only)
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="announce_schedule",
                  description="[Dev] Schedule an announcement for a future time (UTC).")
@app_commands.describe(
    title="Announcement title",
    content="Announcement body text",
    fire_at="When to post: YYYY-MM-DD HH:MM (UTC, 24h)",
    channel="Channel to post in (default: announcements)",
    image_url="Optional image URL",
)
async def announce_schedule(
    interaction: discord.Interaction,
    title: str,
    content: str,
    fire_at: str,
    channel: Optional[str] = None,
    image_url: Optional[str] = None,
):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can schedule announcements.", ephemeral=True)

    try:
        fire_dt = datetime.strptime(fire_at, "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return await interaction.response.send_message(
            "❌ Date/time must be in **YYYY-MM-DD HH:MM** format (UTC, 24-hour). "
            "Example: `2025-12-25 09:00`", ephemeral=True)

    if fire_dt <= now_utc():
        return await interaction.response.send_message(
            "❌ Scheduled time must be in the future.", ephemeral=True)

    if image_url and not valid_image_url(image_url):
        return await interaction.response.send_message(
            "❌ Image URL must be a direct image link.", ephemeral=True)

    target_channel = channel or NEWS_CHANNEL_NAME
    ch = discord.utils.get(interaction.guild.text_channels, name=target_channel)
    if not ch:
        return await interaction.response.send_message(
            f"❌ Channel `#{target_channel}` not found.", ephemeral=True)

    data     = load_data()
    sched_id = f"sched_{int(fire_dt.timestamp())}_{interaction.user.id}"
    data["scheduled"][sched_id] = {
        "title":     title,
        "content":   content,
        "fire_at":   fire_dt.isoformat(),
        "channel":   target_channel,
        "image_url": image_url,
        "created_by": interaction.user.id,
        "fired":     False,
    }
    save_data(data)

    await interaction.response.send_message(
        f"Announcement **{title}** scheduled for "
        f"`{fire_dt.strftime('%Y-%m-%d %H:%M UTC')}` in `#{target_channel}`.",
        ephemeral=True)

    await audit(interaction.guild,
                f"Announcement scheduled: '{title}' for {fire_dt.strftime('%Y-%m-%d %H:%M UTC')} "
                f"by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="announce_list",
                  description="[Dev] List all pending scheduled announcements.")
async def announce_list(interaction: discord.Interaction):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can view scheduled announcements.", ephemeral=True)

    data    = load_data()
    pending = {k: v for k, v in data["scheduled"].items() if not v.get("fired")}
    if not pending:
        return await interaction.response.send_message(
            "No pending scheduled announcements.", ephemeral=True)

    embed = discord.Embed(title="Scheduled Announcements", color=discord.Color.blurple())
    for k, v in sorted(pending.items(), key=lambda x: x[1]["fire_at"]):
        fire_str = datetime.fromisoformat(v["fire_at"]).strftime("%Y-%m-%d %H:%M UTC")
        embed.add_field(
            name=v["title"],
            value=f"Posts at: {fire_str}\nChannel: #{v['channel']}\nID: `{k}`",
            inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="announce_cancel",
                  description="[Dev] Cancel a scheduled announcement by ID.")
@app_commands.describe(sched_id="Announcement ID from /announce_list")
async def announce_cancel(interaction: discord.Interaction, sched_id: str):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can cancel announcements.", ephemeral=True)

    data = load_data()
    if sched_id not in data["scheduled"]:
        return await interaction.response.send_message(
            f"❌ No scheduled announcement with ID `{sched_id}`.", ephemeral=True)

    if data["scheduled"][sched_id].get("fired"):
        return await interaction.response.send_message(
            "❌ That announcement has already been posted.", ephemeral=True)

    title = data["scheduled"][sched_id]["title"]
    del data["scheduled"][sched_id]
    save_data(data)
    await interaction.response.send_message(
        f"Scheduled announcement **{title}** cancelled.", ephemeral=True)

    await audit(interaction.guild,
                f"Announcement cancelled: '{title}' (id={sched_id}) "
                f"by {interaction.user} ({interaction.user.id})")


# ══════════════════════════════════════════════════════════════════════════════
#  INSTAGRAM-STYLE POSTS
# ══════════════════════════════════════════════════════════════════════════════

class IGPostView(discord.ui.View):
    """Persistent Like + Comment buttons on every IG post."""

    def __init__(self, post_id: str, likes: int = 0):
        super().__init__(timeout=None)
        self.post_id = post_id
        self.likes   = likes
        self._update_like_label()

    def _update_like_label(self):
        for child in self.children:
            if getattr(child, "custom_id", "").startswith("ig_like_"):
                child.label = f"🤍 Like  {self.likes}" if self.likes else "🤍 Like"

    # ── Like button ───────────────────────────────────────────────────────────
    @discord.ui.button(label="🤍 Like", style=discord.ButtonStyle.secondary,
                       custom_id="ig_like_btn")
    async def like_btn(self, interaction: discord.Interaction,
                       button: discord.ui.Button):
        data = load_data()
        if self.post_id not in data["instagram"]:
            return await interaction.response.send_message(
                "❌ Post not found.", ephemeral=True)

        post = data["instagram"][self.post_id]
        likers: list = post.setdefault("likers", [])

        if interaction.user.id in likers:
            likers.remove(interaction.user.id)
        else:
            likers.append(interaction.user.id)

        post["likes"] = len(likers)
        self.likes    = post["likes"]
        self._update_like_label()
        save_data(data)

        await interaction.response.edit_message(view=self)

    # ── Comment button — creates the thread and surfaces it to the user ───────
    @discord.ui.button(label="💬 Comment", style=discord.ButtonStyle.primary,
                       custom_id="ig_comment_btn")
    async def comment_btn(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        data = load_data()
        if self.post_id not in data["instagram"]:
            return await interaction.response.send_message(
                "❌ Post not found.", ephemeral=True)

        post      = data["instagram"][self.post_id]
        thread_id = post.get("thread_id")

        if thread_id:
            # Thread already exists — just point the user to it
            thread = interaction.guild.get_thread(thread_id)
            if thread:
                return await interaction.response.send_message(
                    f"💬 Comment thread is already open: {thread.mention}",
                    ephemeral=True)

        # Thread absent — create it from the original post message
        ch = interaction.guild.get_channel(post.get("channel_id"))
        if not ch:
            return await interaction.response.send_message(
                "❌ Could not find the original post channel.", ephemeral=True)

        try:
            msg = await ch.fetch_message(post["message_id"])
        except discord.NotFound:
            return await interaction.response.send_message(
                "❌ Original post message no longer exists.", ephemeral=True)

        thread = await msg.create_thread(
            name=f"Comments — {post['username']}",
            auto_archive_duration=10080,   # 7 days
        )
        post["thread_id"] = thread.id
        save_data(data)

        await interaction.response.send_message(
            f"💬 Comment thread created: {thread.mention}", ephemeral=True)


@bot.tree.command(name="ig_post",
                  description="Post an Instagram-style photo post as your OC.")
@app_commands.describe(
    oc_name="Your OC's name",
    username="Instagram handle (e.g. @username)",
    caption="Post caption",
    photo1_url="First photo as a direct image URL — required (either this or photo1_file)",
    photo1_file="First photo as a file upload — required (either this or photo1_url)",
)
async def ig_post(
    interaction: discord.Interaction,
    oc_name: str, username: str, caption: str,
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
    data   = load_data()
    oc_key = oc_key_of(oc_name)

    if oc_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    pairs = [
        (photo1_url, photo1_file), (photo2_url, photo2_file),
        (photo3_url, photo3_file), (photo4_url, photo4_file),
        (photo5_url, photo5_file), (photo6_url, photo6_file),
        (photo7_url, photo7_file), (photo8_url, photo8_file),
        (photo9_url, photo9_file), (photo10_url, photo10_file),
    ]

    photos = []
    for url, file in pairs:
        if file:
            if not file.content_type or not file.content_type.startswith("image/"):
                return await interaction.response.send_message(
                    "❌ All attached files must be valid images.", ephemeral=True)
            photos.append(file.url)
        elif url:
            if not valid_image_url(url):
                return await interaction.response.send_message(
                    f"❌ Invalid image URL provided: `{url}`", ephemeral=True)
            photos.append(url)

    if not photos:
        return await interaction.response.send_message(
            "❌ At least one photo is required — provide `photo1_url` or upload `photo1_file`.", ephemeral=True)

    oc     = data["ocs"][oc_key]
    handle = username if username.startswith("@") else f"@{username}"

    post_id  = f"{oc_key}_{int(now_utc().timestamp())}"
    post_obj = {
        "oc_key":    oc_key,
        "username":  handle,
        "caption":   caption,
        "photos":    photos,
        "likes":     0,
        "likers":    [],
        "posted_by": interaction.user.id,
        "posted_at": now_iso(),
        "channel_id": None,
        "message_id": None,
        "thread_id":  None,
    }
    data["instagram"][post_id] = post_obj
    save_data(data)

    ig_ch = discord.utils.get(interaction.guild.text_channels, name=INSTAGRAM_CHANNEL_NAME)
    if not ig_ch:
        return await interaction.response.send_message(
            f"❌ Channel `#{INSTAGRAM_CHANNEL_NAME}` not found. "
            f"Please create it before posting.", ephemeral=True)

    await interaction.response.send_message(
        f"📸 Posting {handle}'s photo{'s' if len(photos) > 1 else ''} to {ig_ch.mention}…",
        ephemeral=True)

    view  = IGPostView(post_id, likes=0)
    embed = discord.Embed(
        description=f"**{handle}**  {caption}",
        color=discord.Color.from_rgb(225, 48, 108),
        timestamp=now_utc(),
    )
    embed.set_author(
        name=f"{oc['name']}  ({handle})",
        icon_url=oc.get("profile_picture") or discord.Embed.Empty,
    )
    embed.set_image(url=photos[0])
    embed.set_footer(text=f"{len(photos)} photo(s)  ·  post id: {post_id}")

    msg = await ig_ch.send(embed=embed, view=view)

    for i, photo in enumerate(photos[1:], start=2):
        extra = discord.Embed(color=discord.Color.from_rgb(225, 48, 108))
        extra.set_image(url=photo)
        extra.set_footer(text=f"Photo {i}/{len(photos)}")
        await ig_ch.send(embed=extra)

    data["instagram"][post_id]["channel_id"] = ig_ch.id
    data["instagram"][post_id]["message_id"] = msg.id
    save_data(data)

    await audit(interaction.guild,
                f"IG post by OC '{oc_name}' ({handle}) "
                f"by {interaction.user} ({interaction.user.id})  post_id={post_id}")


# ══════════════════════════════════════════════════════════════════════════════
#  DEV DM COMMAND (dev only)
# ══════════════════════════════════════════════════════════════════════════════

class DevDMModal(discord.ui.Modal, title="Reply to Dev"):
    response_text = discord.ui.TextInput(
        label="Your Response", 
        style=discord.TextStyle.paragraph, 
        max_length=2000
    )

    def __init__(self, guild_id: int, dev_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.dev_id = dev_id

    async def on_submit(self, interaction: discord.Interaction):
        guild = bot.get_guild(self.guild_id)
        if not guild:
            return await interaction.response.send_message("❌ Could not locate the origin server.", ephemeral=True)
            
        ch = discord.utils.get(guild.text_channels, name=DEV_RESPONSE_CHANNEL_NAME)
        if not ch:
            return await interaction.response.send_message(
                f"❌ The server does not have a `#{DEV_RESPONSE_CHANNEL_NAME}` channel setup.", ephemeral=True)
            
        embed = discord.Embed(
            title="Response to Dev Message", 
            description=self.response_text.value, 
            color=discord.Color.green(),
            timestamp=now_utc()
        )
        if interaction.user.display_avatar:
            embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        else:
            embed.set_author(name=interaction.user.display_name)
            
        embed.set_footer(text=f"User ID: {interaction.user.id}  ·  Replying to Dev ID: {self.dev_id}")
        
        await ch.send(embed=embed)
        await interaction.response.send_message("✅ Your response has been sent to the developers.", ephemeral=True)


class DevDMView(discord.ui.View):
    def __init__(self, guild_id: int, dev_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.dev_id = dev_id

    @discord.ui.button(label="Reply to Dev", style=discord.ButtonStyle.primary, custom_id="dev_dm_reply")
    async def reply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DevDMModal(self.guild_id, self.dev_id))


@bot.tree.command(name="dev_dm", description="[Dev] Send a DM to a specific user.")
@app_commands.describe(
    user="The user to message",
    message="Message content",
    require_response="Whether the user gets a button to reply (logs to dev-responses channel)"
)
async def dev_dm(
    interaction: discord.Interaction, 
    user: discord.Member, 
    message: str, 
    require_response: bool = False
):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can use this command.", ephemeral=True)
            
    embed = discord.Embed(
        title="Message from Server Dev", 
        description=message, 
        color=discord.Color.brand_red(),
        timestamp=now_utc()
    )
    embed.set_footer(text=f"From Server: {interaction.guild.name}")
    
    view = DevDMView(interaction.guild.id, interaction.user.id) if require_response else None
    
    try:
        await user.send(embed=embed, view=view)
        await interaction.response.send_message(f"✅ DM successfully sent to {user.mention}.", ephemeral=True)
        await audit(interaction.guild, f"Dev DM sent to {user} by {interaction.user}. Requires response: {require_response}")
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ Could not DM {user.mention}. They likely have DMs disabled.", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  DEBUT DM  (dev only)
# ══════════════════════════════════════════════════════════════════════════════

class DebutView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int,
                 oc_name: str, group_name: str, debut_channel_id: int,
                 custom_channel_message: Optional[str] = None):
        super().__init__(timeout=None)
        self.guild_id               = guild_id
        self.user_id                = user_id
        self.oc_name                = oc_name
        self.group_name             = group_name
        self.debut_channel_id       = debut_channel_id
        self.custom_channel_message = custom_channel_message

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success,
                       custom_id="debut_accept")
    async def accept(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        guild   = bot.get_guild(self.guild_id)
        member  = guild.get_member(self.user_id) if guild else None
        channel = guild.get_channel(self.debut_channel_id) if guild else None

        if member and channel:
            await channel.set_permissions(
                member, view_channel=True, send_messages=True)

            # Optional custom message posted silently in the debut channel
            if self.custom_channel_message:
                await channel.send(
                    self.custom_channel_message.format(
                        member=member.mention,
                        oc=self.oc_name,
                        group=self.group_name,
                    ))

            await interaction.response.edit_message(
                content=(f"You accepted the debut contract for **{self.oc_name}** "
                         f"in **{self.group_name}**. "
                         f"You now have access to the debuts channel in **{guild.name}**."),
                view=None)
        else:
            await interaction.response.edit_message(
                content="❌ Could not complete the debut — server or channel not found.",
                view=None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger,
                       custom_id="debut_decline")
    async def decline(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        await interaction.response.edit_message(
            content=(f"You declined the debut contract for **{self.oc_name}** "
                     f"in **{self.group_name}**."),
            view=None)


@bot.tree.command(name="debut_notify",
                  description="[Dev] DM a user a debut contract for their OC.")
@app_commands.describe(
    oc_name="Name of the OC being offered a debut",
    group_name="Name of the group the OC is debuting in",
    message="Custom debut message shown in the DM contract",
    channel_message="Message posted in #debuts after acceptance "
                    "(use {member}, {oc}, {group} as placeholders; optional)",
)
async def debut_notify(
    interaction: discord.Interaction,
    oc_name: str,
    group_name: str,
    message: str,
    channel_message: Optional[str] = None,
):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can send debut notifications.", ephemeral=True)

    data   = load_data()
    oc_key = oc_key_of(oc_name)

    if oc_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    oc       = data["ocs"][oc_key]
    owner_id = oc.get("owner_id")
    if not owner_id:
        return await interaction.response.send_message(
            f"❌ **{oc_name}** has no registered owner.", ephemeral=True)

    member = interaction.guild.get_member(owner_id)
    if not member:
        return await interaction.response.send_message(
            f"❌ The owner of **{oc_name}** is not in this server.", ephemeral=True)

    # Auto-create #debuts if missing
    debut_ch = discord.utils.get(interaction.guild.text_channels, name=DEBUT_CHANNEL_NAME)
    if not debut_ch:
        cat = discord.utils.get(interaction.guild.categories, name="Special")
        if not cat:
            cat = await interaction.guild.create_category("Special")
        debut_ch = await interaction.guild.create_text_channel(
            DEBUT_CHANNEL_NAME,
            category=cat,
            overwrites={
                interaction.guild.default_role: discord.PermissionOverwrite(
                    view_channel=False),
                interaction.guild.me: discord.PermissionOverwrite(view_channel=True),
            })

    embed = discord.Embed(
        title=f"Debut Contract  —  {oc_name}  |  {group_name}",
        description=message,
        color=discord.Color.gold(),
        timestamp=now_utc(),
    )
    if oc.get("profile_picture"):
        embed.set_thumbnail(url=oc["profile_picture"])
    embed.add_field(name="OC",    value=oc_name,    inline=True)
    embed.add_field(name="Group", value=group_name, inline=True)
    embed.set_footer(text=f"From: {interaction.guild.name}")

    view = DebutView(
        guild_id=interaction.guild.id,
        user_id=member.id,
        oc_name=oc_name,
        group_name=group_name,
        debut_channel_id=debut_ch.id,
        custom_channel_message=channel_message,
    )

    try:
        await member.send(
            content=(f"You have received a debut contract for your OC **{oc_name}** "
                     f"to join **{group_name}**. Please accept or decline below."),
            embed=embed,
            view=view,
        )
        await interaction.response.send_message(
            f"Debut contract sent to {member.mention} for **{oc_name}** ({group_name}).",
            ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Could not DM {member.mention} — they may have DMs disabled.",
            ephemeral=True)


class GCInviteView(discord.ui.View):
    """
    Sent via DM to the OC owner. On Accept the bot creates (or reuses) a
    private category + text channel and grants access to the user and all
    connected devs. On Decline, no channel is created.
    """
    def __init__(
        self,
        guild_id: int,
        invitee_user_id: int,
        oc_key: str,
        oc_name: str,
        group_name: str,
        dev_ids: list[int],        # user IDs of all dev/admin members
    ):
        super().__init__(timeout=None)
        self.guild_id         = guild_id
        self.invitee_user_id  = invitee_user_id
        self.oc_key           = oc_key
        self.oc_name          = oc_name
        self.group_name       = group_name
        self.dev_ids          = dev_ids

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success,
                       custom_id="gcinvite_accept")
    async def accept(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        guild  = bot.get_guild(self.guild_id)
        if not guild:
            return await interaction.response.edit_message(
                content="❌ Server not found.", view=None)

        # Build overwrites: deny everyone, grant invitee + all devs
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me:           discord.PermissionOverwrite(view_channel=True,
                                    send_messages=True, read_message_history=True),
        }
        member = guild.get_member(self.invitee_user_id)
        if member:
            overwrites[member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True)

        for dev_id in self.dev_ids:
            dev_member = guild.get_member(dev_id)
            if dev_member:
                overwrites[dev_member] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True,
                    manage_messages=True, read_message_history=True)

        # Create or reuse a "Group Chats" category
        cat_name = "Group Chats"
        category = discord.utils.get(guild.categories, name=cat_name)
        if not category:
            category = await guild.create_category(cat_name)

        # Channel name derived from group name (Discord-safe slug)
        ch_slug = self.group_name.lower().replace(" ", "-")
        channel = discord.utils.get(guild.text_channels,
                                    name=ch_slug, category=category)
        if not channel:
            channel = await guild.create_text_channel(
                ch_slug, category=category, overwrites=overwrites)

        # Persist to data store
        data  = load_data()
        gc_key = f"gc_{ch_slug}_{int(now_utc().timestamp())}"
        data["groupchats"][gc_key] = {
            "name":         self.group_name,
            "channel_id":   channel.id,
            "participants": [self.oc_key],
            "created_at":   now_iso(),
        }
        save_data(data)

        # Welcome embed in the new channel
        embed = discord.Embed(
            title=f"Welcome to {self.group_name}!",
            description=(f"{member.mention if member else 'The invited user'}'s OC "
                         f"**{self.oc_name}** has joined this group chat.\n"
                         f"Devs are present in this channel."),
            color=discord.Color.blue(),
            timestamp=now_utc(),
        )
        await channel.send(embed=embed)

        await interaction.response.edit_message(
            content=(f"✅ You accepted the group chat invite for **{self.oc_name}** "
                     f"in **{self.group_name}**. Channel: see the server."),
            view=None)

        await audit(guild,
                    f"GC invite accepted: OC '{self.oc_name}' added to "
                    f"'{self.group_name}' by {interaction.user} ({interaction.user.id})")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger,
                       custom_id="gcinvite_decline")
    async def decline(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        await interaction.response.edit_message(
            content=(f"You declined the group chat invite for **{self.oc_name}** "
                     f"in **{self.group_name}**."),
            view=None)


@bot.tree.command(
    name="gc_invite",
    description="[Dev] DM a debuted OC's owner a group-chat invite that creates a private channel on acceptance."
)
@app_commands.describe(
    oc_name="Name of the already-debuted OC to invite",
    group_name="Display name for the new group chat (becomes the channel name)",
    message="Custom invitation message shown in the DM",
)
async def gc_invite(
    interaction: discord.Interaction,
    oc_name: str,
    group_name: str,
    message: str,
):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can send group chat invites.", ephemeral=True)

    data   = load_data()
    oc_key = oc_key_of(oc_name)

    if oc_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    oc       = data["ocs"][oc_key]
    owner_id = oc.get("owner_id")
    if not owner_id:
        return await interaction.response.send_message(
            f"❌ **{oc_name}** has no registered owner.", ephemeral=True)

    member = interaction.guild.get_member(owner_id)
    if not member:
        return await interaction.response.send_message(
            f"❌ The owner of **{oc_name}** is not in this server.", ephemeral=True)

    # Collect all dev/admin user IDs (excluding bots)
    dev_ids = [
        m.id for m in interaction.guild.members
        if (m.guild_permissions.administrator or
            m.id == interaction.guild.owner_id)
        and not m.bot
    ]

    embed = discord.Embed(
        title=f"Group Chat Invite  —  {oc_name}  |  {group_name}",
        description=message,
        color=discord.Color.purple(),
        timestamp=now_utc(),
    )
    if oc.get("profile_picture"):
        embed.set_thumbnail(url=oc["profile_picture"])
    embed.add_field(name="OC",         value=oc_name,    inline=True)
    embed.add_field(name="Group Chat", value=group_name, inline=True)
    embed.set_footer(text=f"From: {interaction.guild.name}")

    view = GCInviteView(
        guild_id=interaction.guild.id,
        invitee_user_id=member.id,
        oc_key=oc_key,
        oc_name=oc_name,
        group_name=group_name,
        dev_ids=dev_ids,
    )

    try:
        await member.send(
            content=(f"You have been invited to add your OC **{oc_name}** "
                     f"to the group chat **{group_name}**. Accept or decline below."),
            embed=embed,
            view=view,
        )
        await interaction.response.send_message(
            f"✅ Group chat invite sent to {member.mention} for OC **{oc_name}**.",
            ephemeral=True)
        await audit(
            interaction.guild,
            f"GC invite sent to {member} ({member.id}) for OC '{oc_name}' "
            f"group '{group_name}' by {interaction.user} ({interaction.user.id})"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Could not DM {member.mention} — they may have DMs disabled.",
            ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  OC DM
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="oc_dm",
                  description="Open a private DM channel between two OCs.")
@app_commands.describe(your_oc="Your OC's name", target_oc="OC to DM")
async def oc_dm(interaction: discord.Interaction, your_oc: str, target_oc: str):
    data    = load_data()
    src_key = oc_key_of(your_oc)
    tgt_key = oc_key_of(target_oc)

    if src_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{your_oc}** found.", ephemeral=True)
    if tgt_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{target_oc}** found.", ephemeral=True)
    if src_key == tgt_key:
        return await interaction.response.send_message(
            "❌ You cannot DM an OC with themselves.", ephemeral=True)

    dm_key       = "dm_" + "_".join(sorted([src_key, tgt_key]))
    src_oc       = data["ocs"][src_key]
    tgt_oc       = data["ocs"][tgt_key]
    tgt_owner_id = tgt_oc.get("owner_id")
    tgt_member   = interaction.guild.get_member(tgt_owner_id) if tgt_owner_id else None

    if dm_key in data["dms"]:
        existing_ch = interaction.guild.get_channel(data["dms"][dm_key]["channel_id"])
        if existing_ch:
            await existing_ch.set_permissions(
                interaction.user, view_channel=True, send_messages=True)
            if tgt_member:
                await existing_ch.set_permissions(
                    tgt_member, view_channel=True, send_messages=True)
            return await interaction.response.send_message(
                f"DM channel: {existing_ch.mention}", ephemeral=True)

    cat = discord.utils.get(interaction.guild.categories, name="OC DMs")
    if not cat:
        cat = await interaction.guild.create_category("OC DMs")

    ch_name    = f"dm-{src_key[:15]}-{tgt_key[:15]}"
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.guild.me:           discord.PermissionOverwrite(view_channel=True),
        interaction.user:               discord.PermissionOverwrite(
            view_channel=True, send_messages=True),
    }
    if tgt_member:
        overwrites[tgt_member] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True)

    channel = await interaction.guild.create_text_channel(
        ch_name, category=cat, overwrites=overwrites)

    data["dms"][dm_key] = {
        "participants": [src_key, tgt_key],
        "channel_id":  channel.id,
        "created_at":  now_iso(),
    }
    save_data(data)

    embed = discord.Embed(
        title="OC DM",
        description=(f"Private conversation between **{src_oc['name']}** "
                     f"and **{tgt_oc['name']}**.\n"
                     f"Visible only to the owners of these OCs."),
        color=discord.Color.purple(),
    )
    await channel.send(embed=embed)
    await interaction.response.send_message(
        f"DM channel created: {channel.mention}", ephemeral=True)

    await audit(interaction.guild,
                f"OC DM opened: '{your_oc}' <-> '{target_oc}' "
                f"by {interaction.user} ({interaction.user.id})")


# ══════════════════════════════════════════════════════════════════════════════
#  OC GROUP CHAT
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="oc_groupchat",
                  description="Create a group chat channel between multiple OCs.")
@app_commands.describe(
    your_oc="Your OC's name", group_name="Name for this group chat",
    target_oc1="OC to invite #1", target_oc2="OC to invite #2",
    target_oc3="OC to invite #3 (optional)", target_oc4="OC to invite #4 (optional)",
    target_oc5="OC to invite #5 (optional)",
)
async def oc_groupchat(
    interaction: discord.Interaction,
    your_oc: str, group_name: str,
    target_oc1: str, target_oc2: str,
    target_oc3: Optional[str] = None,
    target_oc4: Optional[str] = None,
    target_oc5: Optional[str] = None,
):
    data    = load_data()
    src_key = oc_key_of(your_oc)

    if src_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{your_oc}** found.", ephemeral=True)

    raw_targets  = [target_oc1, target_oc2, target_oc3, target_oc4, target_oc5]
    target_names = [t for t in raw_targets if t]
    target_keys  = [oc_key_of(t) for t in target_names]

    missing = [n for n, k in zip(target_names, target_keys) if k not in data["ocs"]]
    if missing:
        return await interaction.response.send_message(
            f"❌ OC(s) not found: {', '.join(f'**{m}**' for m in missing)}",
            ephemeral=True)

    dupes = set()
    for k in target_keys:
        if k == src_key or target_keys.count(k) > 1:
            dupes.add(data["ocs"][k]["name"])
    if dupes:
        return await interaction.response.send_message(
            f"❌ Duplicate OC(s): {', '.join(f'**{d}**' for d in dupes)}",
            ephemeral=True)

    all_keys = [src_key] + target_keys
    cat = discord.utils.get(interaction.guild.categories, name="OC Group Chats")
    if not cat:
        cat = await interaction.guild.create_category("OC Group Chats")

    ch_name    = "gc-" + group_name.lower().replace(" ", "-")[:28]
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.guild.me:           discord.PermissionOverwrite(view_channel=True),
        interaction.user:               discord.PermissionOverwrite(
            view_channel=True, send_messages=True),
    }
    members_added = [interaction.user]
    for oc_k in target_keys:
        owner_id = data["ocs"][oc_k].get("owner_id")
        if owner_id:
            m = interaction.guild.get_member(owner_id)
            if m and m not in members_added:
                overwrites[m] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True)
                members_added.append(m)

    channel = await interaction.guild.create_text_channel(
        ch_name, category=cat, overwrites=overwrites)

    gc_id = f"gc_{int(now_utc().timestamp())}_{src_key[:8]}"
    data["groupchats"][gc_id] = {
        "name":         group_name,
        "participants": all_keys,
        "channel_id":  channel.id,
        "created_by":  interaction.user.id,
        "created_at":  now_iso(),
    }
    save_data(data)

    oc_names_str = ", ".join(data["ocs"][k]["name"] for k in all_keys)
    embed = discord.Embed(
        title=group_name,
        description=(f"**Members:** {oc_names_str}\n\n"
                     f"Visible only to the owners of these OCs."),
        color=discord.Color.blue(),
        timestamp=now_utc(),
    )
    await channel.send(embed=embed)
    await interaction.response.send_message(
        f"Group chat **{group_name}** created: {channel.mention}", ephemeral=True)

    await audit(interaction.guild,
                f"Group chat '{group_name}' created with [{oc_names_str}] "
                f"by {interaction.user} ({interaction.user.id})")


# ══════════════════════════════════════════════════════════════════════════════
#  HELP
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="help", description="Show available bot commands.")
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
        ))
        embed.add_field(name="Floor/Dorm Management", inline=False, value=(
            "`/dorm_assign` — Assign an OC to a room\n"
            "`/dorm_unassign` — Remove an OC from their room\n"
            "`/dorm_view` — View floors and rooms interactively\n"
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
        embed.add_field(name="⚙️ Dev Tools", inline=False, value=(
            "`/floor_create` — Create a floor category\n"
            "`/floor_rename` — Rename a floor (preserves assignments)\n"
            "`/dorm_create` — Create a dorm room inside a floor\n"
            "`/dorm_rename` — Rename a dorm room (preserves occupants)\n"
            "`/dorm_kick` — Force-remove a user's OC(s) from their dorm assignment\n"
            "`/news_post` — Post a news article embed\n"
            "`/announce_schedule` — Schedule a future announcement\n"
            "`/announce_cancel` — Cancel a scheduled announcement\n"
            "`/debut_notify` — Send a debut contract DM\n"
            "`/gc_invite` — Invite a debuted OC's owner to a new private group chat\n"
            "`/dev_dm` — Message a user directly\n"
            "`/shutdown` — Gracefully shut down the bot with a final DB backup\n"
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
            f"Scheduled time format: YYYY-MM-DD HH:MM (UTC)\n"
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
        ))
        embed.add_field(name="Floor/Dorm Management", inline=False, value=(
            "`/dorm_assign` — Assign an OC to a room\n"
            "`/dorm_unassign` — Remove an OC from their room\n"
            "`/dorm_view` — View floors and rooms interactively\n"
        ))
        embed.add_field(name="Instagram", inline=False, value=(
            "`/ig_post` — Post 1–10 photos (files or URLs) as your OC\n"
        ))
        embed.add_field(name="Messaging", inline=False, value=(
            "`/oc_dm` — Private DM channel between two OCs\n"
            "`/oc_groupchat` — Group chat for multiple OCs\n"
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
    """Triggered on SIGTERM or SIGINT."""
    log.info("Shutdown signal received (signal %s). Running emergency backup…", signum)
    # Schedule the coroutine and wait for it to complete before the process dies.
    future = asyncio.run_coroutine_threadsafe(_emergency_backup(), bot.loop)
    try:
        # Block the signal-handler thread for up to 15 seconds.
        future.result(timeout=15)
    except Exception as e:
        log.error("Emergency backup during shutdown raised: %s", e)
    finally:
        log.info("Shutdown complete. Exiting.")
        sys.exit(0)

async def _emergency_backup():
    global DATA_DIRTY
    if not os.path.exists(DATA_FILE):
        return
    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name=DB_BACKUP_CHANNEL_NAME)
        if ch:
            try:
                file = discord.File(DATA_FILE, filename="data.json")
                await ch.send(
                    f"[EMERGENCY BACKUP] Shutdown signal — {now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}",
                    file=file
                )
                DATA_DIRTY = False
                log.info("Emergency backup completed.")
            except Exception as e:
                log.error(f"Emergency backup failed: {e}")
            break


@bot.tree.command(name="shutdown", description="[Dev] Gracefully shut down the bot with a final DB backup.")
async def shutdown_cmd(interaction: discord.Interaction):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can shut down the bot.", ephemeral=True)

    await interaction.response.send_message(
        "🔴 Shutdown initiated. Performing final DB backup before going offline…",
        ephemeral=True)

    await audit(
        interaction.guild,
        f"[SHUTDOWN] Bot shutdown initiated by {interaction.user} "
        f"({interaction.user.id}) at {now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )

    await _emergency_backup()
    log.info("Graceful shutdown via /shutdown command by %s.", interaction.user)
    await bot.close()


@bot.tree.command(
    name="startup",
    description="[Dev] Manually revive the bot: re-sync commands and restart task loops."
)
async def startup_cmd(interaction: discord.Interaction):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "❌ Only devs can use this command.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    # 1. Re-sync slash-command tree
    try:
        await bot.tree.sync()
        sync_status = "✅ Command tree re-synced."
    except Exception as e:
        sync_status = f"⚠️ Sync failed: {e}"

    # 2. Restart background tasks if stopped
    task_lines = []
    for task_obj, label in [
        (check_birthdays,  "check_birthdays"),
        (check_scheduled,  "check_scheduled"),
        (auto_backup_db,   "auto_backup_db"),
    ]:
        if not task_obj.is_running():
            task_obj.start()
            task_lines.append(f"🔄 `{label}` — restarted.")
        else:
            task_lines.append(f"✅ `{label}` — already running.")

    # 3. Validate DB readability
    try:
        _ = load_data()
        db_status = "✅ Database readable."
    except Exception as e:
        db_status = f"⚠️ Database error: {e}"

    embed = discord.Embed(
        title="Manual Startup Report",
        color=discord.Color.green(),
        timestamp=now_utc(),
    )
    embed.add_field(name="Slash Commands", value=sync_status, inline=False)
    embed.add_field(name="Background Tasks", value="\n".join(task_lines), inline=False)
    embed.add_field(name="Database", value=db_status, inline=False)
    embed.set_footer(text=f"Initiated by {interaction.user} ({interaction.user.id})")

    await interaction.followup.send(embed=embed, ephemeral=True)
    await audit(
        interaction.guild,
        f"[STARTUP] Manual startup executed by {interaction.user} ({interaction.user.id})"
    )


# ─── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable is not set. "
            "Add it in Render → Environment.")

    # Register handlers for clean shutdowns and emergency backups
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Start health-check server in background thread
    t = threading.Thread(target=_run_http, daemon=True)
    t.start()
    log.info("Health-check HTTP server started on port %d", PORT)

    # Start the web server
    webserver.keep_alive()

    bot.run(token, log_handler=None)   # logging already configured above