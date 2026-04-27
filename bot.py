"""
OC & Dorm Discord Bot  ·  v3
─────────────────────────────────────────────────────────────────────────────
All interaction through slash commands only.
Render-ready: health-check HTTP server runs alongside the bot.
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import json
import logging
import os
import re
import threading
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
DATA_FILE          = os.environ.get("DATA_FILE", "data.json")
LOG_CHANNEL_NAME   = "oc-log"          # OC registrations, dorm logs
AUDIT_CHANNEL_NAME = "logs"            # ALL user action audit trail
DEBUT_CHANNEL_NAME = "debuts"
NEWS_CHANNEL_NAME  = "announcements"
BIRTHDAY_FORMAT    = "%d/%m/%Y"
BIRTHDAY_DISPLAY   = "DD/MM/YYYY"
DORM_SIZES         = [2, 3]
MAX_PHOTOS         = 10
PORT               = int(os.environ.get("PORT", 8080))

FILTERABLE_FIELDS  = ["gender", "pronouns", "face_claim", "main_skill",
                      "ethnicity", "nationality"]

# ─── Helpers ───────────────────────────────────────────────────────────────────
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"ocs": {}, "dorms": {}, "instagram": {}, "dms": {},
                "groupchats": {}, "scheduled": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
    for k in ("ocs", "dorms", "instagram", "dms", "groupchats", "scheduled"):
        d.setdefault(k, {})
    return d

def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_age(birthday_str: str):
    try:
        bday  = datetime.strptime(birthday_str, BIRTHDAY_FORMAT).date()
        today = date.today()
        return (today.year - bday.year
                - ((today.month, today.day) < (bday.month, bday.day)))
    except Exception:
        return None

def is_admin(interaction: discord.Interaction) -> bool:
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

def floor_key_of(n: int) -> str:
    return f"floor-{n}"

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
    embed.add_field(name="Birthday",    value=f"{oc['birthday']}{age_str}", inline=True)
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

# ─── on_ready ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await bot.tree.sync()
    if not check_birthdays.is_running():
        check_birthdays.start()
    if not check_scheduled.is_running():
        check_scheduled.start()
    log.info("Logged in as %s — slash commands synced", bot.user)

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

@bot.tree.command(name="oc_add", description="Register a new OC to the database.")
@app_commands.describe(
    name="OC's full name", profile_picture="Direct image URL",
    birthday=f"Birthday in {BIRTHDAY_DISPLAY}", gender="OC's gender",
    pronouns="Pronouns (e.g. she/her)", face_claim="Face claim",
    main_skill="Primary skill", ethnicity="Ethnicity",
    nationality="Nationality", form_link="Link to OC form (optional)"
)
async def oc_add(
    interaction: discord.Interaction,
    name: str, profile_picture: str, birthday: str,
    gender: str, pronouns: str, face_claim: str,
    main_skill: str, ethnicity: str, nationality: str,
    form_link: Optional[str] = None
):
    try:
        datetime.strptime(birthday, BIRTHDAY_FORMAT)
    except ValueError:
        return await interaction.response.send_message(
            f"❌ Birthday must be in **{BIRTHDAY_DISPLAY}** format (e.g. `25/06/2000`).",
            ephemeral=True)

    if not valid_image_url(profile_picture):
        return await interaction.response.send_message(
            "❌ Profile picture must be a direct image URL (.png .jpg .jpeg .gif .webp).",
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
        "name": name, "profile_picture": profile_picture, "birthday": birthday,
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
    name="New name", profile_picture="New image URL",
    birthday="New birthday (DD/MM/YYYY)", gender="New gender",
    pronouns="New pronouns", face_claim="New face claim",
    main_skill="New main skill", ethnicity="New ethnicity",
    nationality="New nationality", form_link="New form link"
)
async def oc_edit(
    interaction: discord.Interaction, oc_name: str,
    name: Optional[str] = None, profile_picture: Optional[str] = None,
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

    if birthday:
        try:
            datetime.strptime(birthday, BIRTHDAY_FORMAT)
        except ValueError:
            return await interaction.response.send_message(
                f"❌ Birthday must be **{BIRTHDAY_DISPLAY}**.", ephemeral=True)

    if profile_picture and not valid_image_url(profile_picture):
        return await interaction.response.send_message(
            "❌ Profile picture must be a direct image URL.", ephemeral=True)

    if form_link and not valid_url(form_link):
        return await interaction.response.send_message(
            "❌ Form link must be a valid URL.", ephemeral=True)

    oc      = data["ocs"][key]
    updates = {
        "name": name, "profile_picture": profile_picture, "birthday": birthday,
        "gender": gender, "pronouns": pronouns, "face_claim": face_claim,
        "main_skill": main_skill, "ethnicity": ethnicity,
        "nationality": nationality, "form_link": form_link,
    }
    changes = []
    for field, val in updates.items():
        if val is not None:
            changes.append(f"`{field}`: {oc.get(field)} → {val}")
            oc[field] = val

    if not changes:
        return await interaction.response.send_message(
            "❌ No changes were provided.", ephemeral=True)

    new_key = oc_key_of(oc["name"])
    if new_key != key:
        data["ocs"][new_key] = data["ocs"].pop(key)
        for dorm in data["dorms"].values():
            for floor in dorm["floors"].values():
                if key in floor["occupants"]:
                    floor["occupants"].remove(key)
                    floor["occupants"].append(new_key)

    save_data(data)
    embed = build_oc_embed(oc, new_key)
    await interaction.response.send_message(
        f"**{oc['name']}** updated.\n\n**Changes:**\n" + "\n".join(changes),
        embed=embed, ephemeral=True)


@bot.tree.command(name="oc_view", description="View an OC's full profile.")
@app_commands.describe(oc_name="Name of the OC")
async def oc_view(interaction: discord.Interaction, oc_name: str):
    data = load_data()
    key  = oc_key_of(oc_name)
    if key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)
    await interaction.response.send_message(embed=build_oc_embed(data["ocs"][key], key))


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

    items  = list(ocs.items())
    total  = len(items)
    embeds = []
    for i in range(0, total, 10):
        chunk = items[i:i+10]
        title = "OC Database"
        filters_active = []
        if search_name:
            filters_active.append(f"name contains '{search_name}'")
        if filter_by:
            filters_active.append(f"{filter_by} = {filter_value}")
        if filters_active:
            title += "  —  " + ", ".join(filters_active)

        embed = discord.Embed(title=title, color=discord.Color.teal())
        for k, oc in chunk:
            age     = get_age(oc["birthday"])
            age_str = f", {age} y/o" if age else ""
            embed.add_field(
                name=oc["name"],
                value=(f"**Gender:** {oc['gender']}  |  **Pronouns:** {oc['pronouns']}\n"
                       f"**Skill:** {oc['main_skill']}  |  **Nationality:** {oc['nationality']}"
                       f"{age_str}"),
                inline=False,
            )
        embed.set_footer(text=f"Page {i//10+1}/{(total-1)//10+1}  ·  {total} OC(s)")
        embeds.append(embed)

    await interaction.response.send_message(embed=embeds[0])
    for e in embeds[1:]:
        await interaction.followup.send(embed=e)


# ══════════════════════════════════════════════════════════════════════════════
#  DORM MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="dorm_create",
                  description="[Admin] Create a new dorm (add floors separately).")
@app_commands.describe(
    dorm_name="Display name for the dorm",
    capacity="Capacity per floor: 2 or 3",
)
async def dorm_create(interaction: discord.Interaction, dorm_name: str, capacity: int):
    if not is_admin(interaction):
        return await interaction.response.send_message(
            "❌ Only admins can create dorms.", ephemeral=True)

    if capacity not in DORM_SIZES:
        return await interaction.response.send_message(
            f"❌ Capacity must be **2** or **3**, not `{capacity}`.", ephemeral=True)

    data = load_data()
    key  = dorm_key_of(dorm_name)
    if key in data["dorms"]:
        return await interaction.response.send_message(
            f"❌ A dorm named **{dorm_name}** already exists.", ephemeral=True)

    data["dorms"][key] = {"name": dorm_name, "capacity_per_floor": capacity, "floors": {}}
    save_data(data)

    category = discord.utils.get(interaction.guild.categories, name="Dorms")
    if category is None:
        await interaction.guild.create_category("Dorms")

    await interaction.response.send_message(
        f"Dorm **{dorm_name}** created (capacity: **{capacity}**/floor).\n"
        f"Use `/dorm_add_floor` to add floors.", ephemeral=True)

    await audit(interaction.guild,
                f"Dorm created: '{dorm_name}' (cap {capacity}/floor) "
                f"by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="dorm_add_floor",
                  description="[Admin] Add a floor to an existing dorm.")
@app_commands.describe(dorm_name="Name of the dorm")
async def dorm_add_floor(interaction: discord.Interaction, dorm_name: str):
    if not is_admin(interaction):
        return await interaction.response.send_message(
            "❌ Only admins can add floors.", ephemeral=True)

    data = load_data()
    key  = dorm_key_of(dorm_name)
    if key not in data["dorms"]:
        return await interaction.response.send_message(
            f"❌ No dorm named **{dorm_name}** found.", ephemeral=True)

    dorm      = data["dorms"][key]
    floor_num = len(dorm["floors"]) + 1
    floor_key = floor_key_of(floor_num)

    dorm["floors"][floor_key] = {
        "capacity": dorm["capacity_per_floor"], "occupants": []}
    save_data(data)

    category = discord.utils.get(interaction.guild.categories, name="Dorms")
    if category is None:
        category = await interaction.guild.create_category("Dorms")

    ch_name = f"{key}-{floor_key}"
    if not discord.utils.get(interaction.guild.text_channels, name=ch_name):
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.guild.me:           discord.PermissionOverwrite(view_channel=True),
        }
        if interaction.guild.owner:
            overwrites[interaction.guild.owner] = discord.PermissionOverwrite(
                view_channel=True)
        await interaction.guild.create_text_channel(
            ch_name, category=category, overwrites=overwrites)

    await interaction.response.send_message(
        f"Floor {floor_num} added to **{dorm['name']}**. Channel `#{ch_name}` created.",
        ephemeral=True)

    await audit(interaction.guild,
                f"Floor {floor_num} added to dorm '{dorm_name}' "
                f"by {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="dorm_assign", description="Assign an OC to a dorm floor.")
@app_commands.describe(
    oc_name="OC name", dorm_name="Dorm name", floor="Floor number")
async def dorm_assign(
    interaction: discord.Interaction, oc_name: str, dorm_name: str, floor: int
):
    data     = load_data()
    oc_key   = oc_key_of(oc_name)
    dorm_k   = dorm_key_of(dorm_name)
    floor_k  = floor_key_of(floor)

    if oc_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    if dorm_k not in data["dorms"]:
        return await interaction.response.send_message(
            f"❌ No dorm named **{dorm_name}** found.", ephemeral=True)

    dorm = data["dorms"][dorm_k]
    if floor_k not in dorm["floors"]:
        max_fl = len(dorm["floors"])
        return await interaction.response.send_message(
            f"❌ **{dorm_name}** has **{max_fl}** floor(s). "
            f"Floor `{floor}` does not exist.", ephemeral=True)

    for dk, dv in data["dorms"].items():
        for fk, fv in dv["floors"].items():
            if oc_key in fv["occupants"]:
                return await interaction.response.send_message(
                    f"❌ **{oc_name}** is already in "
                    f"**{dv['name']} – {fk.replace('-', ' ').title()}**.",
                    ephemeral=True)

    floor_data = dorm["floors"][floor_k]
    if len(floor_data["occupants"]) >= floor_data["capacity"]:
        available = [
            f"Floor {fk.split('-')[1]}  —  "
            f"{fv['capacity'] - len(fv['occupants'])} spot(s) left"
            for fk, fv in dorm["floors"].items()
            if len(fv["occupants"]) < fv["capacity"]
        ]
        avail_str = "\n".join(f"• {a}" for a in available) or "No floors available."
        return await interaction.response.send_message(
            f"❌ **{dorm_name} – Floor {floor}** is full "
            f"({floor_data['capacity']}/{floor_data['capacity']}).\n\n"
            f"**Available floors in {dorm_name}:**\n{avail_str}",
            ephemeral=True)

    floor_data["occupants"].append(oc_key)
    save_data(data)

    ch_name = f"{dorm_k}-{floor_k}"
    channel = discord.utils.get(interaction.guild.text_channels, name=ch_name)
    if channel:
        await channel.set_permissions(
            interaction.user, view_channel=True, send_messages=True)

    occupants_display = ", ".join(
        data["ocs"][o]["name"] for o in floor_data["occupants"] if o in data["ocs"])
    is_full = len(floor_data["occupants"]) >= floor_data["capacity"]

    await interaction.response.send_message(
        f"**{oc_name}** assigned to **{dorm_name} – Floor {floor}**.\n"
        f"Occupants: {occupants_display}\n"
        f"Status: {'Full' if is_full else 'Has space'}")

    log_ch = discord.utils.get(interaction.guild.text_channels, name=LOG_CHANNEL_NAME)
    if log_ch:
        spots      = floor_data["capacity"] - len(floor_data["occupants"])
        room_note  = "Room is now full." if is_full else f"{spots} spot(s) remaining."
        await log_ch.send(
            f"**{oc_name}** assigned to **{dorm_name} – Floor {floor}** "
            f"by {interaction.user.mention}. {room_note}")

    await audit(interaction.guild,
                f"OC '{oc_name}' assigned to '{dorm_name}' floor {floor} "
                f"by {interaction.user} ({interaction.user.id}). "
                f"{'Room full.' if is_full else ''}")


@bot.tree.command(name="dorm_unassign", description="Remove an OC from their dorm.")
@app_commands.describe(oc_name="Name of the OC to unassign")
async def dorm_unassign(interaction: discord.Interaction, oc_name: str):
    data   = load_data()
    oc_key = oc_key_of(oc_name)
    if oc_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    for dorm_k, dorm in data["dorms"].items():
        for floor_k, floor_data in dorm["floors"].items():
            if oc_key in floor_data["occupants"]:
                floor_data["occupants"].remove(oc_key)
                save_data(data)
                ch = discord.utils.get(
                    interaction.guild.text_channels,
                    name=f"{dorm_k}-{floor_k}")
                if ch:
                    await ch.set_permissions(interaction.user, overwrite=None)
                await interaction.response.send_message(
                    f"**{oc_name}** removed from "
                    f"**{dorm['name']} – {floor_k.replace('-', ' ').title()}**.")
                await audit(interaction.guild,
                            f"OC '{oc_name}' unassigned from dorm "
                            f"by {interaction.user} ({interaction.user.id})")
                return

    await interaction.response.send_message(
        f"❌ **{oc_name}** is not assigned to any dorm.", ephemeral=True)


@bot.tree.command(name="dorm_view", description="View dorm occupancy.")
@app_commands.describe(dorm_name="Specific dorm to view (leave blank for all)")
async def dorm_view(interaction: discord.Interaction, dorm_name: Optional[str] = None):
    data = load_data()
    if not data["dorms"]:
        return await interaction.response.send_message(
            "❌ No dorms have been created yet.", ephemeral=True)

    if dorm_name:
        key = dorm_key_of(dorm_name)
        if key not in data["dorms"]:
            return await interaction.response.send_message(
                f"❌ No dorm named **{dorm_name}** found.", ephemeral=True)
        dorms_to_show = {key: data["dorms"][key]}
    else:
        dorms_to_show = data["dorms"]

    first = True
    for dorm_k, dorm in dorms_to_show.items():
        embed = discord.Embed(title=dorm["name"], color=discord.Color.green())
        if not dorm["floors"]:
            embed.description = "No floors added yet. Use /dorm_add_floor."
        for floor_k, floor_data in dorm["floors"].items():
            floor_num = floor_k.split("-")[1]
            occupants = [
                data["ocs"][o]["name"]
                for o in floor_data["occupants"] if o in data["ocs"]]
            occ_str   = ", ".join(occupants) if occupants else "Empty"
            is_full   = len(floor_data["occupants"]) >= floor_data["capacity"]
            status    = ("Full"
                         if is_full
                         else f"{floor_data['capacity'] - len(floor_data['occupants'])} spot(s) left")
            embed.add_field(
                name=f"Floor {floor_num}  [{len(floor_data['occupants'])}/{floor_data['capacity']}]  {status}",
                value=occ_str, inline=False)
        if first:
            await interaction.response.send_message(embed=embed)
            first = False
        else:
            await interaction.followup.send(embed=embed)


# ══════════════════════════════════════════════════════════════════════════════
#  NEWS  (admin only)
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="news_post", description="[Admin] Post a news article embed.")
@app_commands.describe(
    title="Article headline", content="Article body",
    image_url="Optional image URL")
async def news_post(
    interaction: discord.Interaction,
    title: str,
    content: str,
    image_url: Optional[str] = None,
):
    if not is_admin(interaction):
        return await interaction.response.send_message(
            "❌ Only admins can post news.", ephemeral=True)

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
#  SCHEDULED ANNOUNCEMENTS  (admin only)
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="announce_schedule",
                  description="[Admin] Schedule an announcement for a future time (UTC).")
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
    if not is_admin(interaction):
        return await interaction.response.send_message(
            "❌ Only admins can schedule announcements.", ephemeral=True)

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
                  description="[Admin] List all pending scheduled announcements.")
async def announce_list(interaction: discord.Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message(
            "❌ Only admins can view scheduled announcements.", ephemeral=True)

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
                  description="[Admin] Cancel a scheduled announcement by ID.")
@app_commands.describe(sched_id="Announcement ID from /announce_list")
async def announce_cancel(interaction: discord.Interaction, sched_id: str):
    if not is_admin(interaction):
        return await interaction.response.send_message(
            "❌ Only admins can cancel announcements.", ephemeral=True)

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
                child.label = f"Like  {self.likes}" if self.likes else "Like"

    # ── Like button ───────────────────────────────────────────────────────────
    @discord.ui.button(label="Like", style=discord.ButtonStyle.secondary,
                       custom_id="ig_like_btn", emoji="♡")
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

    # ── Comment button — opens a modal ────────────────────────────────────────
    @discord.ui.button(label="Comment", style=discord.ButtonStyle.primary,
                       custom_id="ig_comment_btn", emoji="💬")
    async def comment_btn(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        await interaction.response.send_modal(IGCommentModal(self.post_id))


class IGCommentModal(discord.ui.Modal, title="Leave a Comment"):
    oc_name = discord.ui.TextInput(
        label="Your OC's name",
        placeholder="Exactly as registered",
        max_length=100,
    )
    comment = discord.ui.TextInput(
        label="Comment",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, post_id: str):
        super().__init__()
        self.post_id = post_id

    async def on_submit(self, interaction: discord.Interaction):
        data   = load_data()
        oc_key = oc_key_of(self.oc_name.value)

        if self.post_id not in data["instagram"]:
            return await interaction.response.send_message(
                "❌ Post not found.", ephemeral=True)

        if oc_key not in data["ocs"]:
            return await interaction.response.send_message(
                f"❌ No OC named **{self.oc_name.value}** found.", ephemeral=True)

        post      = data["instagram"][self.post_id]
        oc        = data["ocs"][oc_key]
        thread_id = post.get("thread_id")

        # Create thread if missing
        if not thread_id:
            ch = interaction.guild.get_channel(post.get("channel_id"))
            if ch:
                msg = await ch.fetch_message(post["message_id"])
                thread = await msg.create_thread(
                    name=f"Comments — {post['username']}",
                    auto_archive_duration=10080)
                post["thread_id"] = thread.id
                save_data(data)
            else:
                return await interaction.response.send_message(
                    "❌ Could not find the original post channel.", ephemeral=True)
        else:
            thread = interaction.guild.get_thread(thread_id)
            if not thread:
                return await interaction.response.send_message(
                    "❌ Comment thread not found.", ephemeral=True)

        embed = discord.Embed(
            description=f"**{oc['name']}**  {self.comment.value}",
            color=discord.Color.from_rgb(200, 200, 200),
            timestamp=now_utc(),
        )
        if oc.get("profile_picture"):
            embed.set_author(name=oc["name"], icon_url=oc["profile_picture"])
        else:
            embed.set_author(name=oc["name"])
        await thread.send(embed=embed)
        await interaction.response.send_message("Comment posted.", ephemeral=True)


@bot.tree.command(name="ig_post",
                  description="Post an Instagram-style photo post as your OC.")
@app_commands.describe(
    oc_name="Your OC's name",
    username="Instagram handle (e.g. @username)",
    caption="Post caption",
    photo1="Photo URL #1 (required)",
    photo2="Photo URL #2",  photo3="Photo URL #3",
    photo4="Photo URL #4",  photo5="Photo URL #5",
    photo6="Photo URL #6",  photo7="Photo URL #7",
    photo8="Photo URL #8",  photo9="Photo URL #9",
    photo10="Photo URL #10",
)
async def ig_post(
    interaction: discord.Interaction,
    oc_name: str, username: str, caption: str, photo1: str,
    photo2: Optional[str] = None, photo3: Optional[str] = None,
    photo4: Optional[str] = None, photo5: Optional[str] = None,
    photo6: Optional[str] = None, photo7: Optional[str] = None,
    photo8: Optional[str] = None, photo9: Optional[str] = None,
    photo10: Optional[str] = None,
):
    data   = load_data()
    oc_key = oc_key_of(oc_name)

    if oc_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    photos  = [p for p in [photo1, photo2, photo3, photo4, photo5,
                             photo6, photo7, photo8, photo9, photo10] if p]
    invalid = [p for p in photos if not valid_image_url(p)]
    if invalid:
        return await interaction.response.send_message(
            "❌ Invalid image URL(s):\n" +
            "\n".join(f"• `{u}`" for u in invalid), ephemeral=True)

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

    await interaction.response.send_message(
        f"Posting {handle}'s photo{'s' if len(photos) > 1 else ''}…", ephemeral=True)

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

    msg = await interaction.channel.send(embed=embed, view=view)

    for i, photo in enumerate(photos[1:], start=2):
        extra = discord.Embed(color=discord.Color.from_rgb(225, 48, 108))
        extra.set_image(url=photo)
        extra.set_footer(text=f"Photo {i}/{len(photos)}")
        await interaction.channel.send(embed=extra)

    data["instagram"][post_id]["channel_id"] = interaction.channel.id
    data["instagram"][post_id]["message_id"] = msg.id
    save_data(data)

    await audit(interaction.guild,
                f"IG post by OC '{oc_name}' ({handle}) "
                f"by {interaction.user} ({interaction.user.id})  post_id={post_id}")


# ── /ig_comment kept for backward-compat (slash command path still works) ─────
@bot.tree.command(name="ig_comment",
                  description="Leave a comment on an Instagram post as your OC.")
@app_commands.describe(
    post_id="The post ID shown on the post embed",
    oc_name="Your OC's name",
    comment="Your comment text",
)
async def ig_comment(
    interaction: discord.Interaction, post_id: str, oc_name: str, comment: str
):
    data   = load_data()
    oc_key = oc_key_of(oc_name)

    if post_id not in data["instagram"]:
        return await interaction.response.send_message(
            f"❌ Post ID `{post_id}` not found.", ephemeral=True)

    if oc_key not in data["ocs"]:
        return await interaction.response.send_message(
            f"❌ No OC named **{oc_name}** found.", ephemeral=True)

    post      = data["instagram"][post_id]
    oc        = data["ocs"][oc_key]
    thread_id = post.get("thread_id")

    if not thread_id:
        ch = interaction.guild.get_channel(post.get("channel_id"))
        if ch and post.get("message_id"):
            msg    = await ch.fetch_message(post["message_id"])
            thread = await msg.create_thread(
                name=f"Comments — {post['username']}",
                auto_archive_duration=10080)
            post["thread_id"] = thread.id
            save_data(data)
        else:
            return await interaction.response.send_message(
                "❌ Could not locate the post.", ephemeral=True)
    else:
        thread = interaction.guild.get_thread(thread_id)
        if not thread:
            return await interaction.response.send_message(
                "❌ Comment thread not found.", ephemeral=True)

    embed = discord.Embed(
        description=f"**{oc['name']}**  {comment}",
        color=discord.Color.from_rgb(200, 200, 200),
        timestamp=now_utc(),
    )
    if oc.get("profile_picture"):
        embed.set_author(name=oc["name"], icon_url=oc["profile_picture"])
    else:
        embed.set_author(name=oc["name"])

    await thread.send(embed=embed)
    await interaction.response.send_message("Comment posted.", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  DEBUT DM  (admin only)
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
                  description="[Admin] DM a user a debut contract for their OC.")
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
    if not is_admin(interaction):
        return await interaction.response.send_message(
            "❌ Only admins can send debut notifications.", ephemeral=True)

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

@bot.tree.command(name="oc_help", description="Show all available commands.")
async def oc_help(interaction: discord.Interaction):
    embed = discord.Embed(title="OC Bot — Command Reference", color=discord.Color.gold())
    embed.add_field(name="OC Management", inline=False, value=(
        "`/oc_add` — Register a new OC\n"
        "`/oc_edit` — Edit OC fields (only filled fields change)\n"
        "`/oc_view` — View an OC's full profile\n"
        "`/oc_list` — Browse/filter OCs "
        "(filter_by, filter_value, search_name)\n"
    ))
    embed.add_field(name="Dorm Management", inline=False, value=(
        "`/dorm_create` *(admin)* — Create a dorm\n"
        "`/dorm_add_floor` *(admin)* — Add a floor + channel\n"
        "`/dorm_assign` — Assign OC to a floor\n"
        "`/dorm_unassign` — Remove OC from their floor\n"
        "`/dorm_view` — View dorm occupancy\n"
    ))
    embed.add_field(name="News & Announcements", inline=False, value=(
        "`/news_post` *(admin)* — Post a news article\n"
        "`/announce_schedule` *(admin)* — Schedule a future announcement\n"
        "`/announce_list` *(admin)* — View pending scheduled announcements\n"
        "`/announce_cancel` *(admin)* — Cancel a scheduled announcement\n"
    ))
    embed.add_field(name="Instagram", inline=False, value=(
        "`/ig_post` — Post 1–10 photos as your OC (Like + Comment buttons included)\n"
        "`/ig_comment` — Comment on a post via slash command\n"
    ))
    embed.add_field(name="Messaging", inline=False, value=(
        "`/oc_dm` — Private DM channel between two OCs\n"
        "`/oc_groupchat` — Group chat for multiple OCs\n"
        "`/debut_notify` *(admin)* — Send a debut contract DM\n"
    ))
    embed.add_field(name="Notes", inline=False, value=(
        f"Birthday format: **{BIRTHDAY_DISPLAY}**\n"
        f"Filterable fields: {', '.join(FILTERABLE_FIELDS)}\n"
        f"Log channel: `#{LOG_CHANNEL_NAME}`\n"
        f"Audit channel: `#{AUDIT_CHANNEL_NAME}`\n"
        f"News channel: `#{NEWS_CHANNEL_NAME}`\n"
        f"Debut channel: `#{DEBUT_CHANNEL_NAME}` (auto-created)\n"
        f"Scheduled time format: YYYY-MM-DD HH:MM (UTC)\n"
        f"Up to {MAX_PHOTOS} photos per Instagram post\n"
    ))
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable is not set. "
            "Add it in Render → Environment.")

    # Start health-check server in background thread
    t = threading.Thread(target=_run_http, daemon=True)
    t.start()
    log.info("Health-check HTTP server started on port %d", PORT)

    # Start the web server
    webserver.keep_alive()

    bot.run(token, log_handler=None)   # logging already configured above