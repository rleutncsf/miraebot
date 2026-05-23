import asyncio
import io
import json
import logging
import math
import os
import re
import threading
import signal
import sys
import uuid
import webserver
import random
from datetime import datetime, date, timezone, timedelta
import datetime as _dt
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # Python < 3.9


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
ASSET_CHANNEL_NAME        = "bot-assets"      # Persistent bot image uploads
INSTAGRAM_CHANNEL_NAME    = "instagram"
TWITTER_CHANNEL_NAME      = "twitter"
WEVERSE_CHANNEL_NAME      = "weverse"
WEVERSE_DM_CHANNEL_PREFIX = "weverse-dm"
BIRTHDAY_FORMAT           = "%Y/%m/%d"
BIRTHDAY_DISPLAY          = "YYYY/MM/DD"
DORM_SIZES                = [1, 2, 3, 4]
MAX_PHOTOS                = 10

# Channels that must exist for the bot to function correctly.
# Each entry: (name, topic, make_private, category_hint)
# make_private=True means only the bot can see it; False means default permissions.
REQUIRED_CHANNELS: list[tuple[str, str, bool, Optional[str]]] = [
    (LOG_CHANNEL_NAME,          "📋 OC registrations, dorm assignments, and birthday announcements.", False, None),
    (AUDIT_CHANNEL_NAME,        "🔍 Full user action audit trail. Do not delete messages here.",      False, None),
    (DEBUT_CHANNEL_NAME,        "🎤 OC debut announcements.",                                         False, None),
    (NEWS_CHANNEL_NAME,         "📢 Server announcements and news posts.",                            False, None),
    (DEV_RESPONSE_CHANNEL_NAME, "💬 Bot developer DM response relay.",                               False, None),
    (INSTAGRAM_CHANNEL_NAME,    "📸 OC Instagram feed.",                                             False, None),
    (TWITTER_CHANNEL_NAME,      "🐦 OC Twitter/X feed.",                                             False, None),
    (WEVERSE_CHANNEL_NAME,      "💜 Weverse posts and fan updates.",                                 False, None),
]
PORT                      = int(os.environ.get("PORT", 8080))
KST                       = timezone(timedelta(hours=9))  # GMT+9, no DST
PRIMARY_GUILD_ID          = int(os.environ.get("PRIMARY_GUILD_ID", "0"))

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

PURCHASABLE_TYPES = {"album", "misc"}

WEVERSE_PLANS = {
    "monthly":  {"label": "Monthly",    "won": 8_000,  "days": 30},
    "biannual": {"label": "6 Months",   "won": 40_000, "days": 183},
    "annual":   {"label": "Annual",     "won": 80_000, "days": 365},
}

COLORS = {
    "system": discord.Color.blurple(),
    "success": discord.Color.green(),
    "error": discord.Color.red(),
    "neutral": discord.Color.light_grey()
}

# State Flags for DB Persistence
DB_LOADED  = False
DATA_DIRTY = False
_VIEWS_REGISTERED = False
_EVAL_SKIP_FIRST_TICK = False

_http_session: Optional[aiohttp.ClientSession] = None

TIMEZONE_CHOICES = [
    app_commands.Choice(name="UTC",                                        value="UTC"),
    app_commands.Choice(name="KST — Asia/Seoul (GMT+9)",                   value="Asia/Seoul"),
    app_commands.Choice(name="JST — Asia/Tokyo (GMT+9)",                   value="Asia/Tokyo"),
    app_commands.Choice(name="CST — Asia/Shanghai (GMT+8)",                value="Asia/Shanghai"),
    app_commands.Choice(name="HKT — Asia/Hong_Kong (GMT+8)",               value="Asia/Hong_Kong"),
    app_commands.Choice(name="SGT — Asia/Singapore (GMT+8)",               value="Asia/Singapore"),
    app_commands.Choice(name="PHT — Asia/Manila (GMT+8)",                  value="Asia/Manila"),
    app_commands.Choice(name="WIB — Asia/Jakarta (GMT+7)",                 value="Asia/Jakarta"),
    app_commands.Choice(name="ICT — Asia/Bangkok (GMT+7)",                 value="Asia/Bangkok"),
    app_commands.Choice(name="IST — Asia/Kolkata (GMT+5:30)",              value="Asia/Kolkata"),
    app_commands.Choice(name="PKT — Asia/Karachi (GMT+5)",                 value="Asia/Karachi"),
    app_commands.Choice(name="GST — Asia/Dubai (GMT+4)",                   value="Asia/Dubai"),
    app_commands.Choice(name="MSK — Europe/Moscow (GMT+3)",                value="Europe/Moscow"),
    app_commands.Choice(name="EET — Europe/Helsinki (GMT+2/3 DST)",        value="Europe/Helsinki"),
    app_commands.Choice(name="CET — Europe/Berlin (GMT+1/2 DST)",          value="Europe/Berlin"),
    app_commands.Choice(name="BST/GMT — Europe/London (GMT+0/+1 DST)",     value="Europe/London"),
    app_commands.Choice(name="EST — America/New_York (GMT-5/-4 DST)",      value="America/New_York"),
    app_commands.Choice(name="CST — America/Chicago (GMT-6/-5 DST)",       value="America/Chicago"),
    app_commands.Choice(name="MST — America/Denver (GMT-7/-6 DST)",        value="America/Denver"),
    app_commands.Choice(name="PST — America/Los_Angeles (GMT-8/-7 DST)",   value="America/Los_Angeles"),
    app_commands.Choice(name="AKT — America/Anchorage (GMT-9/-8 DST)",     value="America/Anchorage"),
    app_commands.Choice(name="HST — Pacific/Honolulu (GMT-10)",            value="Pacific/Honolulu"),
    app_commands.Choice(name="BRT — America/Sao_Paulo (GMT-3/-2 DST)",     value="America/Sao_Paulo"),
    app_commands.Choice(name="AEST — Australia/Sydney (GMT+10/+11 DST)",   value="Australia/Sydney"),
    app_commands.Choice(name="NZST — Pacific/Auckland (GMT+12/+13 DST)",   value="Pacific/Auckland"),
]

# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_tz():
    return KST

def now():
    return now_utc()

def find_oc(oc_name: str, data: dict):
    key = oc_key_of(oc_name)
    oc = data["ocs"].get(key)
    if oc:
        oc["id"] = key
    return oc

def get_embed(title: str, description: str, color_key: str = "system", reveal_color_override: int = None) -> discord.Embed:
    if reveal_color_override is not None:
        color = discord.Color(reveal_color_override)
    else:
        color = COLORS.get(color_key, COLORS["system"])
    return discord.Embed(title=title, description=description, color=color)

class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)
        self.confirmed = False

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="✖ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

async def wait_for_confirm(interaction: discord.Interaction, embed: discord.Embed) -> bool:
    view = ConfirmView()
    if interaction.response.is_done():
        msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await view.wait()
    return view.confirmed

class RankingPaginationView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=300)
        self.pages = pages
        self.current = 0
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = (self.current == 0)
        self.next_btn.disabled = (self.current >= len(self.pages) - 1)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="rank_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="rank_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

def _gen_item_id(shop: dict, used_ids: list) -> str:
    existing = set(shop.keys()) | set(used_ids)
    while True:
        candidate = "id_" + "".join([str(random.randint(0, 9)) for _ in range(7)])
        if candidate not in existing:
            return candidate

def _gen_inclusion_id(all_inclusions_globally: set, used_inc_ids: list) -> str:
    existing = all_inclusions_globally | set(used_inc_ids)
    while True:
        candidate = "inc_" + "".join([str(random.randint(0, 9)) for _ in range(7)])
        if candidate not in existing:
            return candidate

def _migrate_schema(d: dict) -> dict:
    for k in ("ocs", "floors", "dorms", "instagram", "twitter", "dms",
              "groupchats", "scheduled", "reminders", "shop", "inventories",
              "albums", "album_purchases", "weverse_artists", "weverse_posts",
              "weverse_won", "weverse_groups"):
        d.setdefault(k, {})
    if isinstance(d.get("weverse_posts"), dict):
        d["weverse_posts"] = []
    d.setdefault("weverse_posts", [])
    d.setdefault("shop_categories", [])
    d.setdefault("used_item_ids", [])
    d.setdefault("used_inclusion_ids", [])
    d.setdefault("evaluation_config", {
        "running": False,
        "last_run": None,
        "legend": {
            "bad":         [10_000,  79_999],
            "fair":        [80_000, 129_999],
            "good":       [130_000, 169_999],
            "great":      [170_000, 199_999],
            "excellent":  [200_000, 224_999],
            "outstanding":[225_000, 250_000],
        }
    })
    if "legend" not in d.get("evaluation_config", {}):
        d["evaluation_config"]["legend"] = {
            "bad":         [10_000,  79_999],
            "fair":        [80_000, 129_999],
            "good":       [130_000, 169_999],
            "great":      [170_000, 199_999],
            "Excellent":  [200_000, 224_999],
            "outstanding":[225_000, 250_000],
        }
    d.setdefault("config", {})
    for key, default in [
        ("weverse_channel_id", None),
        ("weverse_dm_category_id", None),
        ("weverse_group_category_id", None),
        ("guild_id", None),
    ]:
        d["config"].setdefault(key, default)

    for oc in d.get("ocs", {}).values():
        oc.setdefault("balance", 500000)
        
    for item in d.get("shop", {}).values():
        for inc in item.get("inclusions", []):
            if isinstance(inc, dict):
                inc.pop("weight", None)
            
    for item_id, item in d.get("shop", {}).items():
        if item.get("type") != "album":
            item.pop("inclusions", None)
            
    for oc_inv in d.get("inventories", {}).values():
        for item_instances in oc_inv.values():
            for inst in item_instances:
                inst.setdefault("pulled_inclusions", [])
                seen = set()
                deduped = []
                for p in inst.get("pulled_inclusions", []):
                    if p["inclusion_id"] not in seen:
                        deduped.append(p)
                        seen.add(p["inclusion_id"])
                inst["pulled_inclusions"] = deduped

    for post in d.get("instagram", {}).values():
        if "media" not in post and "photos" in post:
            post["media"] = [{"url": u, "type": "image"} for u in post["photos"]]
            
    for artist_id, artist_record in d.get("weverse_artists", {}).items():
        subs = artist_record.get("dm_subscribers", {})
        migrated = {}
        for k, v in subs.items():
            if k.isdigit() and len(k) >= 17:
                new_key = v.get("oc_id") or f"legacy_user_{k}"
                migrated[new_key] = v
            else:
                migrated[k] = v
        artist_record["dm_subscribers"] = migrated

    for gid, grp in d.get("weverse_groups", {}).items():
        if "member_discord_ids" in grp and "member_oc_ids" not in grp:
            grp["member_oc_ids"] = [f"legacy_user_{uid}" for uid in grp.pop("member_discord_ids")]

    d.setdefault("config", {})
    d["config"].pop("voting_scheduler", None)
                
    return d

def ensure_shop_keys(data: dict) -> None:
    data.setdefault("shop", {})
    data.setdefault("shop_categories", [])
    data.setdefault("inventories", {})

def _resolve_item_id(raw_input: str, shop: dict) -> Optional[str]:
    stripped = raw_input.strip()
    if stripped in shop:
        return stripped
    prefixed = f"id_{stripped}" if not stripped.startswith("id_") else stripped
    if prefixed in shop:
        return prefixed
    for key in shop:
        if key.endswith(stripped):
            return key
    return None

def _resolve_album(raw_input: str, data: dict) -> Optional[dict]:
    albums = data.get("albums", {})

    if raw_input in albums and albums[raw_input].get("active"):
        a = dict(albums[raw_input])
        a["album_id"] = raw_input
        return a

    needle = raw_input.strip().lower()
    exact, starts, contains = [], [], []
    for a_id, a in albums.items():
        if not a.get("active"):
            continue
        t = a.get("title", "").lower()
        if t == needle:
            exact.append((a_id, a))
        elif t.startswith(needle):
            starts.append((a_id, a))
        elif needle in t:
            contains.append((a_id, a))

    candidates = exact or starts or contains
    if len(candidates) == 1:
        a_id, a = candidates[0]
        result = dict(a)
        result["album_id"] = a_id
        return result

    return None

def _resolve_tradeable(raw_id: str, oc_key: str, inventories: dict, shop: dict) -> Optional[tuple[str, str, int]]:
    stripped = raw_id.strip()
    oc_inv = inventories.get(oc_key, {})
    if stripped.startswith("inc_"):
        if stripped in oc_inv:
            return (stripped, "inclusion", len(oc_inv[stripped]))
        return None
    resolved = _resolve_item_id(stripped, shop)
    if resolved and resolved in oc_inv:
        return (resolved, "item", len(oc_inv[resolved]))
    return None

def _extract_inclusion_location(synthetic_key: str) -> Optional[tuple[str, str, int]]:
    if "@" not in synthetic_key:
        return None
    inc_id, rest = synthetic_key.split("@", 1)
    if ":" not in rest:
        return None
    album_id, idx_str = rest.rsplit(":", 1)
    try:
        return (inc_id, album_id, int(idx_str))
    except ValueError:
        return None

def _rarity_label(rarity_int: int) -> str:
    if rarity_int == 1:   return "✦ common"
    if rarity_int == 2:   return "✦✦ uncommon"
    if rarity_int == 3:   return "✦✦✦ rare"
    if rarity_int == 4:   return "✦✦✦✦ epic"
    if rarity_int >= 5:   return "✦✦✦✦✦ legendary"
    return f"rarity {rarity_int}"

def _rarity_label_proportional(rarity_int: int, all_rarities: list) -> str:
    _TIER_LABELS = [
        "✦ common",
        "✦✦ uncommon",
        "✦✦✦ rare",
        "✦✦✦✦ epic",
        "✦✦✦✦✦ legendary",
    ]
    if not all_rarities:
        return _TIER_LABELS[0]
    min_r = min(all_rarities)
    max_r = max(all_rarities)
    if max_r == min_r:
        return _TIER_LABELS[0]
    bucket = int((rarity_int - min_r) / ((max_r - min_r) / 5))
    bucket = max(0, min(4, bucket))
    return _TIER_LABELS[bucket]

def _build_album_totals(data: dict) -> dict[str, dict[str, int]]:
    log_totals: dict[str, dict[str, int]] = {}
    for p in data.get("album_purchases", {}).values():
        oc_id = p.get("oc_id")
        a_id  = p.get("album_id")
        qty   = p.get("quantity", 0)
        if not oc_id or not a_id:
            continue
        log_totals.setdefault(oc_id, {})
        log_totals[oc_id][a_id] = log_totals[oc_id].get(a_id, 0) + qty

    title_to_album_id: dict[str, str] = {
        a.get("title", "").lower(): a_id
        for a_id, a in data.get("albums", {}).items()
        if a.get("active")
    }
    title_to_album_id_all: dict[str, str] = {
        a.get("title", "").lower(): a_id
        for a_id, a in data.get("albums", {}).items()
    }

    inv_totals: dict[str, dict[str, int]] = {}
    shop = data.get("shop", {})
    inventories = data.get("inventories", {})

    for oc_key, oc_inv in inventories.items():
        for item_id, instances in oc_inv.items():
            shop_item = shop.get(item_id)
            if not shop_item or shop_item.get("type") != "album":
                continue
            item_name_lower = shop_item.get("name", "").lower()
            album_id = (
                title_to_album_id.get(item_name_lower)
                or title_to_album_id_all.get(item_name_lower)
                or f"shop:{item_id}"
            )
            qty = len(instances)
            inv_totals.setdefault(oc_key, {})
            inv_totals[oc_key][album_id] = inv_totals[oc_key].get(album_id, 0) + qty

    merged: dict[str, dict[str, int]] = {}
    all_oc_keys = set(log_totals) | set(inv_totals)
    for oc_key in all_oc_keys:
        log_row = log_totals.get(oc_key, {})
        inv_row = inv_totals.get(oc_key, {})
        all_album_keys = set(log_row) | set(inv_row)
        merged[oc_key] = {
            a_key: max(log_row.get(a_key, 0), inv_row.get(a_key, 0))
            for a_key in all_album_keys
        }
    return merged

def _resolve_weverse_channel(guild: discord.Guild, data: dict) -> Optional[discord.TextChannel]:
    channel_id = data["config"].get("weverse_channel_id")
    if channel_id:
        ch = guild.get_channel(int(channel_id))
        if ch:
            return ch
    return discord.utils.get(guild.text_channels, name=WEVERSE_CHANNEL_NAME)

def _inclusion_sort_key(inc):
    return (inc.get("rarity", 0), inc["name"].lower())

def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        d = {
            "ocs": {}, "floors": {}, "dorms": {}, "instagram": {}, "twitter": {}, "dms": {},
            "groupchats": {}, "scheduled": {}, "reminders": {},
            "shop": {}, "inventories": {},
            "shop_categories": [],
            "used_item_ids": [],
            "used_inclusion_ids": [],
            "evaluation_config": {
                "running": False,
                "last_run": None,
                "legend": {
                    "bad":         [10_000,  79_999],
                    "fair":        [80_000, 129_999],
                    "good":       [130_000, 169_999],
                    "great":      [170_000, 199_999],
                    "Excellent":  [200_000, 224_999],
                    "outstanding":[225_000, 250_000],
                }
            },
        }
        _migrate_schema(d)
        save_data(d)
        return d

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)

    # Schema Migration Guards
    modified = False
    if "albums" not in d:
        d["albums"] = {}
        modified = True
    if "album_purchases" not in d:
        d["album_purchases"] = {}
        modified = True
    if "weverse_artists" not in d:
        d["weverse_artists"] = {}
        modified = True
    if "weverse_posts" not in d:
        d["weverse_posts"] = []
        modified = True
    if "weverse_won" not in d:
        d["weverse_won"] = {}
        modified = True
    if "weverse_groups" not in d:
        d["weverse_groups"] = {}
        modified = True
        
    d.setdefault("config", {})
    for key, default in [
        ("weverse_channel_id", None),
        ("weverse_dm_category_id", None),
        ("weverse_group_category_id", None),
        ("guild_id", None)
    ]:
        if key not in d["config"]:
            d["config"][key] = default
            modified = True

    before = json.dumps(d, sort_keys=True)
    _migrate_schema(d)
    after = json.dumps(d, sort_keys=True)

    if before != after or modified:
        save_data(d)
        log.info("load_data: schema migration applied and persisted to disk.")

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

def valid_media_url(url: str) -> tuple[bool, str]:
    img = bool(re.match(r"^https?://\S+\.(png|jpg|jpeg|gif|webp)(\?.*)?$", url, re.I))
    vid = bool(re.match(r"^https?://\S+\.(mp4|mov|webm)(\?.*)?$", url, re.I))
    if img: return (True, "image")
    if vid: return (True, "video")
    return (False, "")

async def persist_media_attachment(
    attachment: discord.Attachment,
    store_msg_id: bool = False
) -> Optional[tuple[str, Optional[int], str]]:
    if not attachment.content_type:
        return None
    is_image = attachment.content_type.startswith("image/")
    is_video = attachment.content_type.startswith("video/")
    if not (is_image or is_video):
        return None
        
    try:
        media_bytes = await attachment.read()
        file_obj = discord.File(
            io.BytesIO(media_bytes),
            filename=attachment.filename,
            description=f"persisted asset. original: {attachment.id}"
        )
        for guild in bot.guilds:
            ch = discord.utils.get(guild.text_channels, name=ASSET_CHANNEL_NAME)
            if ch:
                msg = await ch.send(file=file_obj)
                if is_image:
                    try:
                        await msg.pin()
                    except Exception:
                        pass
                
                if msg.attachments:
                    clean_url = msg.attachments[0].url.split("?")[0]
                    return (clean_url, msg.id if store_msg_id else None, "image" if is_image else "video")
    except Exception as e:
        log.error("persist_media_attachment failed: %s", e)
    return None

async def persist_image_attachment(
    attachment: discord.Attachment,
    store_msg_id: bool = False
) -> Optional[tuple[str, Optional[int]]]:
    if not attachment.content_type or not attachment.content_type.startswith("image/"):
        return None
    try:
        image_bytes = await attachment.read()
        file_obj = discord.File(
            io.BytesIO(image_bytes),
            filename=attachment.filename,
            description=f"Persisted asset from user upload. Original: {attachment.id}"
        )
        for guild in bot.guilds:
            ch = discord.utils.get(guild.text_channels, name=ASSET_CHANNEL_NAME)
            if ch:
                msg = await ch.send(file=file_obj)
                try:
                    await msg.pin()
                except Exception as e:
                    log.warning("Could not pin persisted image: %s", e)
                
                if msg.attachments:
                    clean_url = msg.attachments[0].url.split("?")[0]
                    return (clean_url, msg.id if store_msg_id else None)
    except Exception as e:
        log.error("persist_image_attachment failed: %s", e)
    return None

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
    for k in ("shop", "inventories", "evaluation_config"):
        if k in parsed and not isinstance(parsed[k], dict): return False
    if "shop_categories" in parsed and not isinstance(parsed["shop_categories"], list): return False
    if "used_item_ids" in parsed and not isinstance(parsed["used_item_ids"], list): return False
    if "used_inclusion_ids" in parsed and not isinstance(parsed["used_inclusion_ids"], list): return False
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
    try:
        bday = datetime.strptime(birthday_str, BIRTHDAY_FORMAT).date()
        this_year_bday = date(today.year, bday.month, bday.day)
        if this_year_bday >= today:
            return (this_year_bday - today).days
        else:
            days_ago = (today - this_year_bday).days
            return 365 + days_ago
    except Exception:
        return 9999

def format_birthday_long(birthday_str: str) -> str:
    try:
        bday = datetime.strptime(birthday_str, BIRTHDAY_FORMAT).date()
        return bday.strftime("%B %d, %Y").replace(" 0", " ")
    except Exception:
        return birthday_str

def resolve_guild(interaction: discord.Interaction) -> Optional[discord.Guild]:
    """Resolves interaction guild, or falls back to PRIMARY_GUILD_ID for DM usages."""
    return interaction.guild or bot.get_guild(PRIMARY_GUILD_ID)

def is_dev(interaction: discord.Interaction) -> bool:
    guild = resolve_guild(interaction)
    if guild is None:
        return False
    member = guild.get_member(interaction.user.id)
    if member is None:
        return False
    return (guild.owner_id == member.id or member.guild_permissions.administrator)

def is_dev_dec():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_dev(interaction):
            await interaction.response.send_message("❌ denied.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

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

def _safe_fmt(template: str, **kwargs) -> str:
    """Format a template string, ignoring unknown placeholders gracefully."""
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template

# ─── Audit helper ──────────────────────────────────────────────────────────────
async def audit(guild: discord.Guild, message: str) -> None:
    if not guild: return
    ch = discord.utils.get(guild.text_channels, name=AUDIT_CHANNEL_NAME)
    if ch:
        ts = now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
        await ch.send(f"[{ts}]  {message}")

async def ensure_required_channels(guild: discord.Guild) -> None:
    """
    Idempotently creates any REQUIRED_CHANNELS entries that do not already
    exist in the given guild. Existing channels (matched by name, case-insensitive)
    are left completely untouched — their topic, permissions, and position are
    never modified. Only absent channels are created.
    """
    existing_names = {ch.name.lower() for ch in guild.text_channels}
    for ch_name, topic, make_private, category_hint in REQUIRED_CHANNELS:
        if ch_name.lower() in existing_names:
            continue  # Already present — do nothing.
        try:
            category = None
            if category_hint:
                category = discord.utils.get(guild.categories, name=category_hint)
            overwrites: dict = {}
            if make_private:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True, send_messages=True,
                        read_message_history=True
                    ),
                }
            await guild.create_text_channel(
                ch_name,
                topic=topic,
                category=category,
                overwrites=overwrites if overwrites else discord.utils.MISSING,
            )
            log.info(
                "ensure_required_channels: created #%s in guild '%s' (%s).",
                ch_name, guild.name, guild.id
            )
        except discord.Forbidden:
            log.warning(
                "ensure_required_channels: missing Manage Channels permission "
                "in guild '%s' (%s) — cannot create #%s.",
                guild.name, guild.id, ch_name
            )
        except discord.HTTPException as e:
            log.error(
                "ensure_required_channels: HTTPException creating #%s in guild '%s': %s",
                ch_name, guild.name, e
            )
        except Exception as e:
            log.error(
                "ensure_required_channels: unexpected error creating #%s in guild '%s': %s",
                ch_name, guild.name, e
            )

# ─── OC embed ──────────────────────────────────────────────────────────────────
def _trunc(val, limit: int = 1024) -> str:
    """Truncate a string to `limit` chars, appending '...' if cut."""
    if val is None:
        return "—"
    s = str(val)
    return s if len(s) <= limit else s[:limit - 1] + "..."

def build_oc_embed(oc: dict, key: str) -> discord.Embed:
    age     = get_age(oc["birthday"])
    age_str = f" ({age} y/o)" if age is not None else ""
    embed   = discord.Embed(title=oc["name"], color=discord.Color.blurple())
    if oc.get("profile_picture"):
        embed.set_thumbnail(url=oc["profile_picture"])
    embed.add_field(name="Birthday",    value=f"{format_birthday_long(oc['birthday'])}{age_str}", inline=True)
    embed.add_field(name="Gender",      value=_trunc(oc["gender"]),       inline=True)
    embed.add_field(name="Pronouns",    value=_trunc(oc["pronouns"]),     inline=True)
    embed.add_field(name="Face Claim",  value=_trunc(oc["face_claim"]),   inline=True)
    embed.add_field(name="Main Skill",  value=_trunc(oc["main_skill"]),   inline=True)
    embed.add_field(name="Ethnicity",   value=_trunc(oc["ethnicity"]),    inline=True)
    embed.add_field(name="Nationality", value=_trunc(oc["nationality"]),  inline=True)
    balance = oc.get("balance", 500_000)
    embed.add_field(name="Balance",     value=f"₩{balance:,}",                     inline=True)
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

bot = commands.Bot(command_prefix="!", intents=intents)

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


# ─── Evaluation Paginator View (for persistence) ───────────────────────────────
class EvaluationPaginatorView(discord.ui.View):
    def __init__(self, pages: list = None, timeout=None):
        super().__init__(timeout=timeout)
        self.pages = pages or []
        self.current = 0
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = (self.current == 0)
        self.next_btn.disabled = (self.current >= len(self.pages) - 1)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="eval_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="eval_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

class PurchaseRevealViewPersistent(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="reveal_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="⚠️ this reveal session expired. please purchase again to reveal your inclusions.",
            embed=None,
            view=self,
        )

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="reveal_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="⚠️ this reveal session expired. please purchase again to reveal your inclusions.",
            embed=None,
            view=self,
        )

class WeversePostView(discord.ui.View):
    def __init__(self, post_id: str):
        super().__init__(timeout=None)
        self.post_id = post_id
        for child in self.children:
            if child.custom_id == "weverse_reply:placeholder":
                child.custom_id = f"weverse_reply:{post_id}"

    @discord.ui.button(label="💜 Artist Reply", style=discord.ButtonStyle.primary, custom_id="weverse_reply:placeholder")
    async def artist_reply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        owned_artist_ocs = [
            data["ocs"][oc_id]
            for oc_id in data["weverse_artists"]
            if oc_id in data["ocs"] and data["ocs"][oc_id].get("owner_id") == interaction.user.id
        ]
        is_dev_user = is_dev(interaction)
        if is_dev_user:
            owned_artist_ocs = [data["ocs"][oc_id] for oc_id in data["weverse_artists"] if oc_id in data["ocs"]]
        if not owned_artist_ocs:
            return await interaction.response.send_message(
                embed=get_embed("Not an Artist", "Only registered Weverse artists can reply via this button. Use `/weverse reply` directly.", "error"),
                ephemeral=True
            )
        await interaction.response.send_modal(WeverseReplyModal(self.post_id, owned_artist_ocs[0]["name"]))

class WeverseReplyModal(discord.ui.Modal, title="Weverse Artist Reply"):
    reply_content = discord.ui.TextInput(
        label="Reply",
        style=discord.TextStyle.paragraph,
        placeholder="Write your reply here…",
        min_length=1,
        max_length=500
    )
    artist_oc_name_input = discord.ui.TextInput(
        label="Your Artist OC Name",
        placeholder="e.g. Soyeon",
        min_length=1,
        max_length=50
    )

    def __init__(self, post_id: str, default_artist_name: str):
        super().__init__()
        self.post_id = post_id
        self.artist_oc_name_input.default = default_artist_name

    async def on_submit(self, interaction: discord.Interaction):
        content = self.reply_content.value
        artist_oc_name = self.artist_oc_name_input.value
        data = load_data()
        post = next((p for p in data.get("weverse_posts", []) if p["post_id"] == self.post_id), None)
        if not post:
            return await interaction.response.send_message("❌ Post not found.", ephemeral=True)
        oc = find_oc(artist_oc_name, data)
        if not oc:
            return await interaction.response.send_message(f"❌ Artist OC **{artist_oc_name}** not found.", ephemeral=True)
        if oc.get("owner_id") != interaction.user.id and not is_dev(interaction):
            return await interaction.response.send_message("❌ You do not own this OC.", ephemeral=True)
        if oc["id"] not in data.get("weverse_artists", {}):
            return await interaction.response.send_message(f"❌ **{artist_oc_name}** is not registered as a Weverse artist.", ephemeral=True)

        guild = resolve_guild(interaction)
        weverse_channel = _resolve_weverse_channel(guild, data)
        if not weverse_channel:
            return await interaction.response.send_message(
                embed=get_embed("Error",
                    f"Weverse channel not found. Create a channel named `{WEVERSE_CHANNEL_NAME}` "
                    f"or use `/weverse config setchannel` to configure it.", "error"),
                ephemeral=True
            )

        try:
            original_msg = await weverse_channel.fetch_message(int(post["message_id"]))
        except Exception:
            return await interaction.response.send_message("❌ Original message not found.", ephemeral=True)

        try:
            thread = original_msg.thread
            if not thread:
                poster_name = post["author_display_name"]
                thread = await original_msg.create_thread(name=f"💬 {poster_name} · Artist Reply", auto_archive_duration=10080)
            
            reply_embed = discord.Embed(title=f"💜 {oc['name']} replied", description=content, color=COLORS["system"], timestamp=now())
            if oc.get("profile_picture"):
                reply_embed.set_thumbnail(url=oc["profile_picture"])
            reply_embed.set_footer(text="Weverse Artist")
            
            await thread.send(embed=reply_embed)
            
            reply_record = {
                "reply_id": str(uuid.uuid4()),
                "artist_oc_id": oc["id"],
                "artist_name": oc["name"],
                "content": content,
                "replied_at": now().isoformat(),
                "message_id": str(original_msg.id)
            }
            post.setdefault("replies", []).append(reply_record)
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_reply"))
            
            await interaction.response.send_message("✅ Reply posted successfully.", ephemeral=True)
        except Exception as e:
            log.error(f"Weverse reply failed: {e}")
            await interaction.response.send_message("❌ Failed to create reply.", ephemeral=True)



# ─── Persistent Views ──────────────────────────────────────────────────────────

class DebutView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        user_id: int,
        oc_name: str,
        group_name: str,
        transport_channel_id: int,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.oc_name = oc_name
        self.group_name = group_name
        self.transport_channel_id = transport_channel_id

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success, custom_id="debut_accept")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id and not is_dev(interaction):
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        try:
            guild = bot.get_guild(self.guild_id)
            if guild and self.transport_channel_id:
                ch = guild.get_channel(self.transport_channel_id)
                if ch:
                    embed = discord.Embed(
                        title="🌟 New Debut!",
                        description=f"**{self.oc_name}** has debuted in **{self.group_name}**! 🎉",
                        color=discord.Color.gold(),
                        timestamp=now_utc(),
                    )
                    await ch.send(embed=embed)
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)
            if guild:
                await audit(guild, f"Debut accepted for {self.oc_name} in {self.group_name} by {interaction.user}")
        except Exception as e:
            log.error("DebutView accept error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

    @discord.ui.button(label="✖ Decline", style=discord.ButtonStyle.danger, custom_id="debut_decline")
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id and not is_dev(interaction):
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        try:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="❌ Debut declined.", view=self)
            user = bot.get_user(self.user_id)
            if user:
                try:
                    await user.send(f"❌ Your debut request for **{self.oc_name}** in **{self.group_name}** was declined.")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        except Exception as e:
            log.error("DebutView decline error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)


class DevDMReplyModal(discord.ui.Modal, title="Reply to User"):
    reply_body = discord.ui.TextInput(
        label="Reply",
        style=discord.TextStyle.paragraph,
        placeholder="Type your reply here…",
        max_length=2000,
        required=True,
    )

    def __init__(self, target_user_id: int):
        super().__init__()
        self.target_user_id = target_user_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            user = bot.get_user(self.target_user_id)
            if not user:
                user = await bot.fetch_user(self.target_user_id)
            if user:
                embed = discord.Embed(
                    title="📨 Reply from Dev Team",
                    description=self.reply_body.value,
                    color=discord.Color.blurple(),
                    timestamp=now_utc(),
                )
                embed.set_footer(text=f"Replied by {interaction.user}")
                await user.send(embed=embed)
                await interaction.response.send_message("✅ Reply sent.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Could not find the user.", ephemeral=True)
        except Exception as e:
            log.error("DevDMReplyModal submit error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)


class DevDMView(discord.ui.View):
    def __init__(self, guild_id: int, dev_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.dev_id = dev_id

    def _check_permissions(self, interaction: discord.Interaction) -> bool:
        guild = bot.get_guild(self.guild_id)
        member = guild.get_member(interaction.user.id) if guild else None
        return (
            interaction.user.id == self.dev_id
            or (member and member.guild_permissions.administrator)
        )

    @discord.ui.button(label="💬 Reply", style=discord.ButtonStyle.primary, custom_id="devdm_reply")
    async def reply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_permissions(interaction):
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        try:
            target_user_id = None
            if interaction.message and interaction.message.embeds:
                footer = interaction.message.embeds[0].footer
                if footer and footer.text:
                    import re as _re
                    match = _re.search(r"(\d{17,20})", footer.text)
                    if match:
                        target_user_id = int(match.group(1))
            if not target_user_id:
                return await interaction.response.send_message("❌ Could not determine the target user.", ephemeral=True)
            await interaction.response.send_modal(DevDMReplyModal(target_user_id=target_user_id))
        except Exception as e:
            log.error("DevDMView reply error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

    @discord.ui.button(label="🗑 Dismiss", style=discord.ButtonStyle.secondary, custom_id="devdm_dismiss")
    async def dismiss_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_permissions(interaction):
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        try:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="✅ Handled.", view=self)
        except Exception as e:
            log.error("DevDMView dismiss error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)


class CombinedNotifyReplyModal(discord.ui.Modal, title="Send Reply"):
    reply_body = discord.ui.TextInput(
        label="Reply",
        style=discord.TextStyle.paragraph,
        placeholder="Type your reply here…",
        max_length=2000,
        required=True,
    )

    def __init__(self, target_user_id: int):
        super().__init__()
        self.target_user_id = target_user_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            user = bot.get_user(self.target_user_id)
            if not user:
                user = await bot.fetch_user(self.target_user_id)
            if user:
                embed = discord.Embed(
                    title="📨 Message from Dev Team",
                    description=self.reply_body.value,
                    color=discord.Color.blurple(),
                    timestamp=now_utc(),
                )
                embed.set_footer(text=f"From {interaction.user}")
                await user.send(embed=embed)
                await interaction.response.send_message("✅ Reply sent.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Could not find the user.", ephemeral=True)
        except Exception as e:
            log.error("CombinedNotifyReplyModal error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)


class CombinedNotifyView(discord.ui.View):
    def __init__(
        self,
        guild_id: int = 0,
        user_id: int = 0,
        dev_id: int = 0,
        oc_name: str = "",
        group_name: str = "",
        transport_channel_id: int = 0,
        custom_channel_message: Optional[str] = None,
        accept_label: Optional[str] = None,
        accept_style: discord.ButtonStyle = discord.ButtonStyle.success,
        decline_label: Optional[str] = None,
        decline_style: discord.ButtonStyle = discord.ButtonStyle.danger,
        reply_label: Optional[str] = None,
        reply_style: discord.ButtonStyle = discord.ButtonStyle.primary,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.dev_id = dev_id
        self.oc_name = oc_name
        self.group_name = group_name
        self.transport_channel_id = transport_channel_id
        self.custom_channel_message = custom_channel_message

        if accept_label is None:
            self.remove_item(self.accept_btn)
        else:
            self.accept_btn.label = accept_label
            self.accept_btn.style = accept_style

        if decline_label is None:
            self.remove_item(self.decline_btn)
        else:
            self.decline_btn.label = decline_label
            self.decline_btn.style = decline_style

        if reply_label is None:
            self.remove_item(self.reply_btn)
        else:
            self.reply_btn.label = reply_label
            self.reply_btn.style = reply_style

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success, custom_id="cnotify_accept")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id and not is_dev(interaction):
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        try:
            guild = bot.get_guild(self.guild_id)
            if guild and self.transport_channel_id:
                ch = guild.get_channel(self.transport_channel_id)
                if ch:
                    msg = self.custom_channel_message or f"✅ **{self.oc_name}** has been accepted into **{self.group_name}**!"
                    await ch.send(msg)
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)
        except Exception as e:
            log.error("CombinedNotifyView accept error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

    @discord.ui.button(label="✖ Decline", style=discord.ButtonStyle.danger, custom_id="cnotify_decline")
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id and not is_dev(interaction):
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        try:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="❌ Request declined.", view=self)
            user = bot.get_user(self.user_id)
            if user:
                try:
                    await user.send(f"❌ Your request for **{self.oc_name}** regarding **{self.group_name}** was declined.")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        except Exception as e:
            log.error("CombinedNotifyView decline error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

    @discord.ui.button(label="💬 Reply", style=discord.ButtonStyle.primary, custom_id="cnotify_reply")
    async def reply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.dev_id and not is_dev(interaction):
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        try:
            await interaction.response.send_modal(CombinedNotifyReplyModal(target_user_id=self.user_id))
        except Exception as e:
            log.error("CombinedNotifyView reply error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)


class GCInviteView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        invitee_user_id: int,
        oc_key: str,
        oc_name: str,
        group_name: str,
        target_channel_id: int,
        dev_ids: list,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.invitee_user_id = invitee_user_id
        self.oc_key = oc_key
        self.oc_name = oc_name
        self.group_name = group_name
        self.target_channel_id = target_channel_id
        self.dev_ids = dev_ids or []

    @discord.ui.button(label="✅ Join", style=discord.ButtonStyle.success, custom_id="gcinvite_join")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.invitee_user_id:
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        try:
            guild = bot.get_guild(self.guild_id)
            if guild and self.target_channel_id:
                ch = guild.get_channel(self.target_channel_id)
                member = guild.get_member(self.invitee_user_id)
                if ch and member:
                    await ch.set_permissions(member, read_messages=True, send_messages=True)
                    await ch.send(f"👋 Welcome to **{self.group_name}**, {member.mention} ({self.oc_name})!")
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="✅ You have joined the group.", view=self)
        except Exception as e:
            log.error("GCInviteView join error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

    @discord.ui.button(label="✖ Decline", style=discord.ButtonStyle.danger, custom_id="gcinvite_decline")
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.invitee_user_id:
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        try:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="❌ Invite declined.", view=self)
            for dev_id in self.dev_ids:
                try:
                    dev_user = bot.get_user(dev_id)
                    if dev_user:
                        await dev_user.send(
                            f"❌ **{self.oc_name}** declined the invite to **{self.group_name}**."
                        )
                except (discord.Forbidden, discord.HTTPException):
                    pass
        except Exception as e:
            log.error("GCInviteView decline error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)


# ─── WeverseCog ────────────────────────────────────────────────────────────────

class WeverseCog(commands.GroupCog, group_name="weverse", group_description="Weverse artist & fan community system"):
    def __init__(self, bot):
        self.bot = bot

    # ── config subgroup ──────────────────────────────────────────────────────

    config_group = app_commands.Group(name="config", description="Weverse configuration (dev)")

    @config_group.command(name="setchannel", description="dev | set the Weverse feed channel by ID or mention")
    @app_commands.describe(channel="the text channel to use as the Weverse feed")
    async def config_setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not is_dev(interaction):
            return await interaction.response.send_message("❌ denied.", ephemeral=True)
        try:
            data = load_data()
            data["config"]["weverse_channel_id"] = channel.id
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_config_setchannel"))
            await interaction.response.send_message(f"✅ Weverse feed channel set to {channel.mention}.", ephemeral=True)
        except Exception as e:
            log.error("weverse config setchannel error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

    @config_group.command(name="setdmcategory", description="dev | set the category for artist DM channels")
    @app_commands.describe(category_id="the category ID to use for Weverse DM channels")
    async def config_setdmcategory(self, interaction: discord.Interaction, category_id: str):
        if not is_dev(interaction):
            return await interaction.response.send_message("❌ denied.", ephemeral=True)
        try:
            guild = resolve_guild(interaction)
            cat_id = int(category_id.strip())
            cat = guild.get_channel(cat_id) if guild else None
            if not isinstance(cat, discord.CategoryChannel):
                return await interaction.response.send_message("❌ category not found.", ephemeral=True)
            data = load_data()
            data["config"]["weverse_dm_category_id"] = cat_id
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_config_setdmcategory"))
            await interaction.response.send_message(f"✅ Weverse DM category set to **{cat.name}**.", ephemeral=True)
        except Exception as e:
            log.error("weverse config setdmcategory error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

    @config_group.command(name="setgroupcategory", description="dev | set the category for Weverse group channels")
    @app_commands.describe(category_id="the category ID to use for Weverse group channels")
    async def config_setgroupcategory(self, interaction: discord.Interaction, category_id: str):
        if not is_dev(interaction):
            return await interaction.response.send_message("❌ denied.", ephemeral=True)
        try:
            guild = resolve_guild(interaction)
            cat_id = int(category_id.strip())
            cat = guild.get_channel(cat_id) if guild else None
            if not isinstance(cat, discord.CategoryChannel):
                return await interaction.response.send_message("❌ category not found.", ephemeral=True)
            data = load_data()
            data["config"]["weverse_group_category_id"] = cat_id
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_config_setgroupcategory"))
            await interaction.response.send_message(f"✅ Weverse group category set to **{cat.name}**.", ephemeral=True)
        except Exception as e:
            log.error("weverse config setgroupcategory error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

    # ── artist subgroup ──────────────────────────────────────────────────────

    artist_group = app_commands.Group(name="artist", description="Weverse artist management")

    @artist_group.command(name="add", description="dev | register an OC as a Weverse artist (creates DM channel)")
    @app_commands.describe(oc_name="the OC to register as a Weverse artist")
    async def artist_add(self, interaction: discord.Interaction, oc_name: str):
        if not is_dev(interaction):
            return await interaction.response.send_message("❌ denied.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            data = load_data()
            guild = resolve_guild(interaction)
            oc = find_oc(oc_name, data)
            if not oc:
                return await interaction.followup.send("❌ OC not found.", ephemeral=True)
            oc_id = oc["id"]
            if oc_id in data.get("weverse_artists", {}):
                return await interaction.followup.send(f"❌ **{oc['name']}** is already a registered Weverse artist.", ephemeral=True)

            dm_channel = None
            cat_id = data["config"].get("weverse_dm_category_id")
            if guild and cat_id:
                cat = guild.get_channel(int(cat_id))
                if isinstance(cat, discord.CategoryChannel):
                    ch_name = f"{WEVERSE_DM_CHANNEL_PREFIX}-{oc_name.lower().replace(' ', '-')}"
                    try:
                        dm_channel = await guild.create_text_channel(
                            ch_name,
                            category=cat,
                            overwrites={
                                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                            },
                            topic=f"💜 Weverse DM channel for {oc['name']}",
                        )
                    except Exception as e:
                        log.warning("Failed to create Weverse DM channel: %s", e)

            data.setdefault("weverse_artists", {})[oc_id] = {
                "oc_name": oc["name"],
                "dm_channel_id": dm_channel.id if dm_channel else None,
                "dm_subscribers": {},
                "registered_at": now_iso(),
            }
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_artist_add"))
            ch_note = f" DM channel: {dm_channel.mention}" if dm_channel else " (no DM channel created)"
            await interaction.followup.send(f"✅ **{oc['name']}** registered as a Weverse artist.{ch_note}", ephemeral=True)
            if guild:
                await audit(guild, f"Weverse artist added: {oc['name']} by {interaction.user}")
        except Exception as e:
            log.error("weverse artist add error: %s", e)
            await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

    @artist_group.command(name="remove", description="dev | unregister a Weverse artist OC")
    @app_commands.describe(oc_name="the OC to unregister")
    async def artist_remove(self, interaction: discord.Interaction, oc_name: str):
        if not is_dev(interaction):
            return await interaction.response.send_message("❌ denied.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            data = load_data()
            guild = resolve_guild(interaction)
            oc = find_oc(oc_name, data)
            if not oc:
                return await interaction.followup.send("❌ OC not found.", ephemeral=True)
            oc_id = oc["id"]
            if oc_id not in data.get("weverse_artists", {}):
                return await interaction.followup.send(f"❌ **{oc['name']}** is not a registered Weverse artist.", ephemeral=True)
            del data["weverse_artists"][oc_id]
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_artist_remove"))
            await interaction.followup.send(f"✅ **{oc['name']}** removed from Weverse artists.", ephemeral=True)
            if guild:
                await audit(guild, f"Weverse artist removed: {oc['name']} by {interaction.user}")
        except Exception as e:
            log.error("weverse artist remove error: %s", e)
            await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

    @artist_group.command(name="list", description="list all registered Weverse artists")
    async def artist_list(self, interaction: discord.Interaction):
        try:
            data = load_data()
            artists = data.get("weverse_artists", {})
            if not artists:
                return await interaction.response.send_message("No Weverse artists registered.", ephemeral=True)
            lines = []
            for oc_id, rec in artists.items():
                sub_count = sum(1 for s in rec.get("dm_subscribers", {}).values() if s.get("active"))
                lines.append(f"• **{rec.get('oc_name', oc_id)}** — {sub_count} active subscriber(s)")
            embed = discord.Embed(title="💜 Weverse Artists", description="\n".join(lines), color=COLORS["system"])
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            log.error("weverse artist list error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

    # ── post / reply ──────────────────────────────────────────────────────────

    @app_commands.command(name="post", description="post a Weverse update as an artist OC (text + up to 2 image URLs)")
    @app_commands.describe(
        oc_name="your artist OC name",
        content="the post content",
        image1="optional image URL",
        image2="optional second image URL",
    )
    async def weverse_post(self, interaction: discord.Interaction, oc_name: str, content: str,
                           image1: Optional[str] = None, image2: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        try:
            data = load_data()
            guild = resolve_guild(interaction)
            oc = find_oc(oc_name, data)
            if not oc:
                return await interaction.followup.send("❌ OC not found.", ephemeral=True)
            if oc.get("owner_id") != interaction.user.id and not is_dev(interaction):
                return await interaction.followup.send("❌ You do not own this OC.", ephemeral=True)
            if oc["id"] not in data.get("weverse_artists", {}):
                return await interaction.followup.send(f"❌ **{oc['name']}** is not a registered Weverse artist.", ephemeral=True)

            weverse_channel = _resolve_weverse_channel(guild, data)
            if not weverse_channel:
                return await interaction.followup.send(
                    f"❌ Weverse channel not found. Create `#{WEVERSE_CHANNEL_NAME}` or configure it.", ephemeral=True
                )

            post_id = str(uuid.uuid4())
            embed = discord.Embed(
                title=f"💜 {oc['name']}",
                description=content,
                color=COLORS["system"],
                timestamp=now(),
            )
            if oc.get("profile_picture"):
                embed.set_thumbnail(url=oc["profile_picture"])
            if image1 and valid_image_url(image1):
                embed.set_image(url=image1)
            if image2 and valid_image_url(image2):
                embed.add_field(name="\u200b", value=f"[Image 2]({image2})", inline=False)
            embed.set_footer(text="Weverse")

            view = WeversePostView(post_id)
            msg = await weverse_channel.send(embed=embed, view=view)

            post_record = {
                "post_id": post_id,
                "author_oc_id": oc["id"],
                "author_display_name": oc["name"],
                "content": content,
                "posted_at": now().isoformat(),
                "message_id": str(msg.id),
                "replies": [],
            }
            data.setdefault("weverse_posts", []).append(post_record)
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_post"))
            bot.add_view(view)
            await interaction.followup.send("✅ Weverse post published.", ephemeral=True)
        except Exception as e:
            log.error("weverse post error: %s", e)
            await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

    @app_commands.command(name="reply", description="reply to a Weverse post as an artist OC")
    @app_commands.describe(post_id="the post ID to reply to", oc_name="your artist OC name", content="your reply")
    async def weverse_reply(self, interaction: discord.Interaction, post_id: str, oc_name: str, content: str):
        await interaction.response.defer(ephemeral=True)
        try:
            data = load_data()
            guild = resolve_guild(interaction)
            oc = find_oc(oc_name, data)
            if not oc:
                return await interaction.followup.send("❌ OC not found.", ephemeral=True)
            if oc.get("owner_id") != interaction.user.id and not is_dev(interaction):
                return await interaction.followup.send("❌ You do not own this OC.", ephemeral=True)
            if oc["id"] not in data.get("weverse_artists", {}):
                return await interaction.followup.send(f"❌ **{oc['name']}** is not a registered Weverse artist.", ephemeral=True)

            post = next((p for p in data.get("weverse_posts", []) if p["post_id"] == post_id), None)
            if not post:
                return await interaction.followup.send("❌ Post not found.", ephemeral=True)

            weverse_channel = _resolve_weverse_channel(guild, data)
            if not weverse_channel:
                return await interaction.followup.send("❌ Weverse channel not found.", ephemeral=True)

            try:
                original_msg = await weverse_channel.fetch_message(int(post["message_id"]))
            except Exception:
                return await interaction.followup.send("❌ Original message not found.", ephemeral=True)

            thread = original_msg.thread
            if not thread:
                thread = await original_msg.create_thread(name=f"💬 {oc['name']} · Reply", auto_archive_duration=10080)

            reply_embed = discord.Embed(
                title=f"💜 {oc['name']} replied",
                description=content,
                color=COLORS["system"],
                timestamp=now(),
            )
            if oc.get("profile_picture"):
                reply_embed.set_thumbnail(url=oc["profile_picture"])
            reply_embed.set_footer(text="Weverse Artist")
            await thread.send(embed=reply_embed)

            post.setdefault("replies", []).append({
                "reply_id": str(uuid.uuid4()),
                "artist_oc_id": oc["id"],
                "artist_name": oc["name"],
                "content": content,
                "replied_at": now().isoformat(),
                "message_id": str(original_msg.id),
            })
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_reply"))
            await interaction.followup.send("✅ Reply posted.", ephemeral=True)
        except Exception as e:
            log.error("weverse reply error: %s", e)
            await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

    # ── dm subgroup ────────────────────────────────────────────────────────────

    dm_group = app_commands.Group(name="dm", description="Weverse DM subscriptions")

    @dm_group.command(name="subscribe", description="subscribe an OC to an artist's DM channel (choose plan)")
    @app_commands.describe(
        oc_name="your OC subscribing",
        artist_oc_name="the artist OC to subscribe to",
        plan="subscription plan",
    )
    @app_commands.choices(plan=[
        app_commands.Choice(name="Monthly (₩8,000 / 30 days)", value="monthly"),
        app_commands.Choice(name="6 Months (₩40,000 / 183 days)", value="biannual"),
        app_commands.Choice(name="Annual (₩80,000 / 365 days)", value="annual"),
    ])
    async def dm_subscribe(self, interaction: discord.Interaction, oc_name: str, artist_oc_name: str, plan: str):
        await interaction.response.defer(ephemeral=True)
        try:
            data = load_data()
            guild = resolve_guild(interaction)
            oc = find_oc(oc_name, data)
            if not oc:
                return await interaction.followup.send("❌ OC not found.", ephemeral=True)
            if oc.get("owner_id") != interaction.user.id and not is_dev(interaction):
                return await interaction.followup.send("❌ You do not own this OC.", ephemeral=True)

            artist_oc = find_oc(artist_oc_name, data)
            if not artist_oc:
                return await interaction.followup.send("❌ Artist OC not found.", ephemeral=True)
            artist_id = artist_oc["id"]
            if artist_id not in data.get("weverse_artists", {}):
                return await interaction.followup.send(f"❌ **{artist_oc['name']}** is not a registered Weverse artist.", ephemeral=True)

            plan_data = WEVERSE_PLANS.get(plan)
            if not plan_data:
                return await interaction.followup.send("❌ Invalid plan.", ephemeral=True)

            cost = plan_data["won"]
            if oc.get("balance", 0) < cost:
                return await interaction.followup.send(
                    f"❌ Insufficient balance. **{oc['name']}** has ₩{oc.get('balance',0):,} but needs ₩{cost:,}.", ephemeral=True
                )

            artist_record = data["weverse_artists"][artist_id]
            existing = artist_record.get("dm_subscribers", {}).get(oc["id"])
            if existing and existing.get("active"):
                return await interaction.followup.send(f"❌ **{oc['name']}** already has an active subscription.", ephemeral=True)

            oc["balance"] -= cost
            next_billing = (now() + timedelta(days=plan_data["days"])).isoformat()
            artist_record.setdefault("dm_subscribers", {})[oc["id"]] = {
                "oc_id": oc["id"],
                "oc_name": oc["name"],
                "owner_discord_id": interaction.user.id,
                "plan": plan,
                "active": True,
                "cancelled": False,
                "subscribed_at": now().isoformat(),
                "next_billing": next_billing,
            }

            dm_channel_id = artist_record.get("dm_channel_id")
            if guild and dm_channel_id:
                ch = guild.get_channel(int(dm_channel_id))
                member = guild.get_member(interaction.user.id)
                if ch and isinstance(ch, discord.TextChannel) and member:
                    try:
                        await ch.set_permissions(member, read_messages=True, send_messages=True)
                    except Exception:
                        pass

            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_dm_subscribe"))
            await interaction.followup.send(
                f"✅ **{oc['name']}** subscribed to **{artist_oc['name']}**'s DM on the **{plan_data['label']}** plan. ₩{cost:,} deducted.", ephemeral=True
            )
        except Exception as e:
            log.error("weverse dm subscribe error: %s", e)
            await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

    @dm_group.command(name="unsubscribe", description="cancel an OC's DM subscription")
    @app_commands.describe(oc_name="your OC", artist_oc_name="the artist OC to unsubscribe from")
    async def dm_unsubscribe(self, interaction: discord.Interaction, oc_name: str, artist_oc_name: str):
        await interaction.response.defer(ephemeral=True)
        try:
            data = load_data()
            oc = find_oc(oc_name, data)
            if not oc:
                return await interaction.followup.send("❌ OC not found.", ephemeral=True)
            if oc.get("owner_id") != interaction.user.id and not is_dev(interaction):
                return await interaction.followup.send("❌ You do not own this OC.", ephemeral=True)

            artist_oc = find_oc(artist_oc_name, data)
            if not artist_oc or artist_oc["id"] not in data.get("weverse_artists", {}):
                return await interaction.followup.send("❌ Artist OC not found or not a Weverse artist.", ephemeral=True)

            artist_record = data["weverse_artists"][artist_oc["id"]]
            sub = artist_record.get("dm_subscribers", {}).get(oc["id"])
            if not sub or not sub.get("active"):
                return await interaction.followup.send("❌ No active subscription found.", ephemeral=True)

            sub["cancelled"] = True
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_dm_unsubscribe"))
            await interaction.followup.send(
                f"✅ **{oc['name']}**'s subscription to **{artist_oc['name']}** will end at the next billing date.", ephemeral=True
            )
        except Exception as e:
            log.error("weverse dm unsubscribe error: %s", e)
            await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

    @dm_group.command(name="status", description="view subscription status for an OC")
    @app_commands.describe(oc_name="the OC to check")
    async def dm_status(self, interaction: discord.Interaction, oc_name: str):
        try:
            data = load_data()
            oc = find_oc(oc_name, data)
            if not oc:
                return await interaction.response.send_message("❌ OC not found.", ephemeral=True)
            lines = []
            for artist_id, artist_record in data.get("weverse_artists", {}).items():
                sub = artist_record.get("dm_subscribers", {}).get(oc["id"])
                if sub:
                    status = "✅ active" if sub.get("active") else "❌ inactive"
                    cancelled = " (cancels at next billing)" if sub.get("cancelled") and sub.get("active") else ""
                    lines.append(
                        f"**{artist_record.get('oc_name', artist_id)}** — {sub['plan']} — {status}{cancelled}\n"
                        f"  next billing: {sub.get('next_billing', 'N/A')[:10]}"
                    )
            if not lines:
                return await interaction.response.send_message(f"**{oc['name']}** has no Weverse DM subscriptions.", ephemeral=True)
            embed = discord.Embed(title=f"💜 {oc['name']} — Weverse DM Status", description="\n".join(lines), color=COLORS["system"])
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            log.error("weverse dm status error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

    # ── group subgroup ─────────────────────────────────────────────────────────

    group_grp = app_commands.Group(name="group", description="Weverse fan groups")

    @group_grp.command(name="create", description="dev | create a Weverse fan group with a dedicated channel")
    @app_commands.describe(group_name="name of the fan group")
    async def group_create(self, interaction: discord.Interaction, group_name: str):
        if not is_dev(interaction):
            return await interaction.response.send_message("❌ denied.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            data = load_data()
            guild = resolve_guild(interaction)
            group_key = group_name.lower().replace(" ", "_")
            if group_key in data.get("weverse_groups", {}):
                return await interaction.followup.send("❌ A group with that name already exists.", ephemeral=True)

            ch = None
            cat_id = data["config"].get("weverse_group_category_id")
            if guild and cat_id:
                cat = guild.get_channel(int(cat_id))
                if isinstance(cat, discord.CategoryChannel):
                    try:
                        ch = await guild.create_text_channel(
                            f"wv-{group_name.lower().replace(' ', '-')}",
                            category=cat,
                            overwrites={
                                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                            },
                            topic=f"💜 Weverse fan group: {group_name}",
                        )
                    except Exception as e:
                        log.warning("Failed to create Weverse group channel: %s", e)

            data.setdefault("weverse_groups", {})[group_key] = {
                "name": group_name,
                "channel_id": ch.id if ch else None,
                "member_oc_ids": [],
                "created_at": now_iso(),
            }
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_group_create"))
            ch_note = f" Channel: {ch.mention}" if ch else " (no channel created)"
            await interaction.followup.send(f"✅ Fan group **{group_name}** created.{ch_note}", ephemeral=True)
            if guild:
                await audit(guild, f"Weverse group created: {group_name} by {interaction.user}")
        except Exception as e:
            log.error("weverse group create error: %s", e)
            await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

    @group_grp.command(name="invite", description="invite an OC to a Weverse fan group")
    @app_commands.describe(oc_name="the OC to invite", group_name="the group name")
    async def group_invite(self, interaction: discord.Interaction, oc_name: str, group_name: str):
        await interaction.response.defer(ephemeral=True)
        try:
            data = load_data()
            guild = resolve_guild(interaction)
            oc = find_oc(oc_name, data)
            if not oc:
                return await interaction.followup.send("❌ OC not found.", ephemeral=True)

            group_key = group_name.lower().replace(" ", "_")
            grp = data.get("weverse_groups", {}).get(group_key)
            if not grp:
                return await interaction.followup.send("❌ Group not found.", ephemeral=True)

            if oc["id"] in grp.get("member_oc_ids", []):
                return await interaction.followup.send("❌ That OC is already in this group.", ephemeral=True)

            ch_id = grp.get("channel_id")
            owner_id = oc.get("owner_id")
            dev_ids = []
            if guild:
                dev_ids = [m.id for m in guild.members if guild.owner_id == m.id or m.guild_permissions.administrator]

            view = GCInviteView(
                guild_id=guild.id if guild else 0,
                invitee_user_id=owner_id or 0,
                oc_key=oc["id"],
                oc_name=oc["name"],
                group_name=grp["name"],
                target_channel_id=ch_id or 0,
                dev_ids=dev_ids,
            )
            embed = discord.Embed(
                title="💜 Weverse Group Invite",
                description=f"**{oc['name']}** has been invited to join **{grp['name']}**!",
                color=COLORS["system"],
            )

            sent = False
            if owner_id and guild:
                member = guild.get_member(owner_id)
                if member:
                    try:
                        await member.send(embed=embed, view=view)
                        sent = True
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            if not sent:
                weverse_ch = _resolve_weverse_channel(guild, data)
                if weverse_ch:
                    await weverse_ch.send(embed=embed, view=view)

            grp.setdefault("member_oc_ids", []).append(oc["id"])
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_group_invite"))
            await interaction.followup.send(f"✅ Invite sent to **{oc['name']}** for group **{grp['name']}**.", ephemeral=True)
        except Exception as e:
            log.error("weverse group invite error: %s", e)
            await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

    @group_grp.command(name="leave", description="remove an OC from a Weverse fan group")
    @app_commands.describe(oc_name="the OC leaving", group_name="the group name")
    async def group_leave(self, interaction: discord.Interaction, oc_name: str, group_name: str):
        await interaction.response.defer(ephemeral=True)
        try:
            data = load_data()
            guild = resolve_guild(interaction)
            oc = find_oc(oc_name, data)
            if not oc:
                return await interaction.followup.send("❌ OC not found.", ephemeral=True)
            if oc.get("owner_id") != interaction.user.id and not is_dev(interaction):
                return await interaction.followup.send("❌ You do not own this OC.", ephemeral=True)

            group_key = group_name.lower().replace(" ", "_")
            grp = data.get("weverse_groups", {}).get(group_key)
            if not grp:
                return await interaction.followup.send("❌ Group not found.", ephemeral=True)

            if oc["id"] not in grp.get("member_oc_ids", []):
                return await interaction.followup.send("❌ That OC is not in this group.", ephemeral=True)

            grp["member_oc_ids"].remove(oc["id"])

            ch_id = grp.get("channel_id")
            if guild and ch_id:
                ch = guild.get_channel(int(ch_id))
                owner_id = oc.get("owner_id")
                member = guild.get_member(owner_id) if owner_id else None
                if ch and isinstance(ch, discord.TextChannel) and member:
                    try:
                        await ch.set_permissions(member, overwrite=None)
                    except Exception:
                        pass

            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_group_leave"))
            await interaction.followup.send(f"✅ **{oc['name']}** has left **{grp['name']}**.", ephemeral=True)
        except Exception as e:
            log.error("weverse group leave error: %s", e)
            await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

    @group_grp.command(name="list", description="list all Weverse groups and their members")
    async def group_list(self, interaction: discord.Interaction):
        try:
            data = load_data()
            groups = data.get("weverse_groups", {})
            if not groups:
                return await interaction.response.send_message("No Weverse groups exist yet.", ephemeral=True)
            embed = discord.Embed(title="💜 Weverse Fan Groups", color=COLORS["system"])
            for gkey, grp in groups.items():
                member_names = []
                for oc_id in grp.get("member_oc_ids", []):
                    oc_rec = data["ocs"].get(oc_id)
                    member_names.append(oc_rec["name"] if oc_rec else oc_id)
                embed.add_field(
                    name=grp.get("name", gkey),
                    value=", ".join(member_names) if member_names else "No members yet.",
                    inline=False,
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            log.error("weverse group list error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

    # ── won subgroup ───────────────────────────────────────────────────────────

    won_group = app_commands.Group(name="won", description="Weverse Won management")

    @won_group.command(name="add", description="dev | credit Weverse Won to an OC")
    @app_commands.describe(oc_name="the OC to credit", amount="amount of Weverse Won to add")
    async def won_add(self, interaction: discord.Interaction, oc_name: str, amount: int):
        if not is_dev(interaction):
            return await interaction.response.send_message("❌ denied.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            data = load_data()
            guild = resolve_guild(interaction)
            oc = find_oc(oc_name, data)
            if not oc:
                return await interaction.followup.send("❌ OC not found.", ephemeral=True)
            if amount <= 0:
                return await interaction.followup.send("❌ Amount must be positive.", ephemeral=True)

            won = data.setdefault("weverse_won", {})
            won[oc["id"]] = won.get(oc["id"], 0) + amount
            save_data(data)
            asyncio.ensure_future(push_backup_to_discord(data, reason="weverse_won_add"))
            await interaction.followup.send(
                f"✅ Added 💜 {amount:,} Weverse Won to **{oc['name']}**. New balance: {won[oc['id']]:,}.", ephemeral=True
            )
            if guild:
                await audit(guild, f"Weverse Won added: {amount:,} to {oc['name']} by {interaction.user}")
        except Exception as e:
            log.error("weverse won add error: %s", e)
            await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

    @won_group.command(name="balance", description="view an OC's Weverse Won balance")
    @app_commands.describe(oc_name="the OC to check")
    async def won_balance(self, interaction: discord.Interaction, oc_name: str):
        try:
            data = load_data()
            oc = find_oc(oc_name, data)
            if not oc:
                return await interaction.response.send_message("❌ OC not found.", ephemeral=True)
            balance = data.get("weverse_won", {}).get(oc["id"], 0)
            embed = discord.Embed(
                title=f"💜 {oc['name']} — Weverse Won",
                description=f"**{balance:,}** Weverse Won",
                color=COLORS["system"],
            )
            if oc.get("profile_picture"):
                embed.set_thumbnail(url=oc["profile_picture"])
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            log.error("weverse won balance error: %s", e)
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)


# ─── Cogs Setup ───────────────────────────────────────────────────────────────
async def setup_hook():
    await bot.add_cog(AlbumCog(bot))
    await bot.add_cog(WeverseCog(bot))
        
    data = load_data()
    for post in data.get("weverse_posts", []):
        bot.add_view(WeversePostView(post["post_id"]))

bot.setup_hook = setup_hook

# ─── on_ready ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    global DB_LOADED
    global _http_session
    global _VIEWS_REGISTERED

    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()

    if not _VIEWS_REGISTERED:
        _persistent_views = [
            lambda: DebutView(guild_id=0, user_id=0, oc_name="", group_name="", transport_channel_id=0),
            lambda: DevDMView(guild_id=0, dev_id=0),
            lambda: CombinedNotifyView(
                guild_id=0, user_id=0, dev_id=0,
                oc_name="", group_name="", transport_channel_id=0,
                custom_channel_message=None,
                accept_label=None, accept_style=discord.ButtonStyle.success,
                decline_label=None, decline_style=discord.ButtonStyle.danger,
                reply_label=None, reply_style=discord.ButtonStyle.primary,
            ),
            lambda: GCInviteView(
                guild_id=0, invitee_user_id=0, oc_key="", oc_name="",
                group_name="", target_channel_id=0, dev_ids=[],
            ),
            lambda: EvaluationPaginatorView([]),
            lambda: PurchaseRevealViewPersistent(),
            lambda: UnifiedTradeConfirmView(0, 0, "", "", [], []),
            lambda: MultiTradeConfirmView(0, 0, "", "", [], []),
            lambda: InclusionsBrowserView([], "", None, 0),
            lambda: TwitterPostView(tweet_id="", likes=0, retweets=0),
        ]

        for view_factory in _persistent_views:
            try:
                bot.add_view(view_factory())
            except Exception as exc:
                log.error("on_ready: failed to register persistent view %s — %s", view_factory, exc)

        _VIEWS_REGISTERED = True
        log.info("on_ready: all persistent views registered.")

    if not DB_LOADED:
        for guild in bot.guilds:
            # DB Backup Bootstrap
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
                    pass

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
                                    _migrate_schema(parsed)
                                    with open(DATA_FILE, "w", encoding="utf-8") as f:
                                        json.dump(parsed, f, indent=2, ensure_ascii=False)
                                    log.info("Restored from backup — message_id=%s guild=%s", message.id, guild.id)
                                    break
                                except (json.JSONDecodeError, ValueError) as e:
                                    log.error(f"Backup file is corrupt, skipping restore: {e}")
                                    break
                except Exception as e:
                    log.error(f"Error fetching DB backup: {e}")

            # Asset Channel Bootstrap
            asset_ch = discord.utils.get(guild.text_channels, name=ASSET_CHANNEL_NAME)
            if not asset_ch:
                try:
                    cat = discord.utils.get(guild.categories, name="Special")
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        guild.me: discord.PermissionOverwrite(
                            view_channel=True, send_messages=True,
                            attach_files=True, read_message_history=True, manage_messages=True
                        )
                    }
                    asset_ch = await guild.create_text_channel(
                        ASSET_CHANNEL_NAME,
                        category=cat,
                        overwrites=overwrites,
                        topic="⚙️ Bot-managed persistent image asset store. Do not delete messages here.",
                        slowmode_delay=21600
                    )
                except Exception as e:
                    log.warning("Could not create %s channel: %s", ASSET_CHANNEL_NAME, e)

            # Ensure all required operational channels exist.
            await ensure_required_channels(guild)

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
    if not check_reminders.is_running(): check_reminders.start()
    if not auto_backup_db.is_running():  auto_backup_db.start()
    if not run_weekly_evaluations.is_running():
        data = load_data()
        if data.get("evaluation_config", {}).get("running"):
            last_run_str = data["evaluation_config"].get("last_run")
            skip_first = False
            if last_run_str:
                try:
                    last_run_dt = datetime.fromisoformat(last_run_str)
                    if last_run_dt.tzinfo is None:
                        last_run_dt = last_run_dt.replace(tzinfo=timezone.utc)
                    elapsed_hours = (now_utc() - last_run_dt).total_seconds() / 3600
                    if elapsed_hours < 168:
                        skip_first = True
                except (ValueError, TypeError):
                    pass
            global _EVAL_SKIP_FIRST_TICK
            _EVAL_SKIP_FIRST_TICK = skip_first
        run_weekly_evaluations.start()
    if not weverse_billing_loop.is_running(): weverse_billing_loop.start()


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Ensure all required channels exist when the bot joins a new guild."""
    log.info("on_guild_join: joined guild '%s' (%s) — ensuring required channels.", guild.name, guild.id)
    await ensure_required_channels(guild)


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
@tasks.loop(time=_dt.time(hour=15, minute=0, tzinfo=timezone.utc))  # 15:00 UTC = 00:00 KST
async def check_birthdays():
    today_kst = datetime.now(KST).date()
    data       = load_data()

    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if not ch:
            continue

        for oc in data["ocs"].values():
            try:
                bday = datetime.strptime(oc["birthday"], BIRTHDAY_FORMAT).date()
                if bday.month != today_kst.month or bday.day != today_kst.day:
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
                    timestamp=datetime.now(KST),
                )
                if oc.get("profile_picture"):
                    embed.set_thumbnail(url=oc["profile_picture"])
                embed.add_field(name="Birthday", value=format_birthday_long(oc["birthday"]), inline=True)
                if age:
                    embed.add_field(name="Turning", value=str(age), inline=True)
                embed.set_footer(text="Birthday recognized in KST (GMT+9)")

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
            entry_type = entry.get("type", "announce")
            embed_color = discord.Color.red() if entry_type == "news_post" else discord.Color.blurple()
            embed = discord.Embed(
                title=entry["title"], description=entry["content"],
                color=embed_color, timestamp=now,
            )
            if entry.get("image_url"):
                embed.set_image(url=entry["image_url"])
            embed.set_footer(text="Scheduled announcement")
            await ch.send(embed=embed)
        entry["fired"] = True
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="check_scheduled"))


# ─── Reminders loop ────────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def check_reminders():
    data = load_data()
    reminders = data.get("reminders", {})
    now = now_utc()
    dirty = False

    for rid, reminder in list(reminders.items()):
        if reminder.get("fired"):
            continue
        fire_at = datetime.fromisoformat(reminder["fire_at"])
        if now >= fire_at:
            for guild in bot.guilds:
                member = guild.get_member(reminder["user_id"])
                if member:
                    try:
                        embed = discord.Embed(
                            title="⏰ Reminder",
                            description=reminder["message"],
                            color=discord.Color.blurple(),
                            timestamp=now_utc(),
                        )
                        embed.set_footer(text="Set by a server admin.")
                        await member.send(embed=embed)
                    except (discord.Forbidden, discord.HTTPException) as e:
                        log.warning("check_reminders: could not DM user %s: %s", reminder["user_id"], e)
                    break

            reminder["fired"] = True
            dirty = True

    if dirty:
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="check_reminders"))

@check_reminders.before_loop
async def before_check_reminders():
    await bot.wait_until_ready()

async def _process_weverse_subscriptions():
    data = load_data()
    modified = False
    current_time = now()

    for artist_id, artist_record in data.get("weverse_artists", {}).items():
        dm_channel_id = artist_record.get("dm_channel_id")
        for sub_key, sub in list(artist_record.get("dm_subscribers", {}).items()):
            if not sub["active"]:
                continue

            next_billing_dt = datetime.fromisoformat(sub["next_billing"]).astimezone(get_tz())
            if current_time < next_billing_dt:
                continue

            guild_id = data["config"].get("guild_id")
            guild = bot.get_guild(int(guild_id)) if guild_id else None

            if sub.get("cancelled"):
                sub["active"] = False
                modified = True
                if dm_channel_id and guild:
                    try:
                        ch = guild.get_channel(int(dm_channel_id))
                        owner_discord_id = sub.get("owner_discord_id")
                        member = guild.get_member(int(owner_discord_id)) if owner_discord_id else None
                        if ch and isinstance(ch, discord.TextChannel) and member:
                            await ch.set_permissions(member, overwrite=None)
                    except Exception:
                        pass
            else:
                plan = sub["plan"]
                plan_data = WEVERSE_PLANS.get(plan, WEVERSE_PLANS["monthly"])
                cost = plan_data["won"]
                
                sub_oc_rec = data["ocs"].get(sub_key)
                if sub_oc_rec and sub_oc_rec.get("balance", 0) >= cost:
                    sub_oc_rec["balance"] -= cost
                    success = True
                else:
                    success = False
                
                if success:
                    sub["next_billing"] = (next_billing_dt + timedelta(days=plan_data["days"])).isoformat()
                    modified = True
                else:
                    sub["active"] = False
                    sub["cancelled"] = True
                    sub["cancelled_at"] = current_time.isoformat()
                    modified = True
                    
                    if guild:
                        try:
                            owner_discord_id = sub.get("owner_discord_id")
                            member = guild.get_member(int(owner_discord_id)) if owner_discord_id else None
                            if member:
                                artist_oc = data["ocs"].get(artist_id, {})
                                artist_name = artist_oc.get("name", "the artist")
                                notify_embed = discord.Embed(
                                    title="💜 Weverse DM — Subscription Cancelled",
                                    description=(
                                        f"Your **{sub['plan']}** subscription to **{artist_name}**'s Weverse DM "
                                        f"has been cancelled due to insufficient ₩ balance on **{sub.get('oc_name', sub_key)}**.\n\n"
                                        f"You have been removed from the DM channel. "
                                        f"Resubscribe via `/weverse dm subscribe` when your balance is topped up."
                                    ),
                                    color=discord.Color.red(),
                                    timestamp=current_time,
                                )
                                try:
                                    await member.send(embed=notify_embed)
                                except (discord.Forbidden, discord.HTTPException):
                                    wv_ch = _resolve_weverse_channel(guild, data)
                                    if wv_ch:
                                        await wv_ch.send(
                                            content=f"{member.mention}",
                                            embed=notify_embed,
                                            delete_after=86400,
                                        )
                        except Exception as _notify_exc:
                            log.warning("_process_weverse_subscriptions: failed to notify owner of cancellation — %s", _notify_exc)

                    if dm_channel_id and guild:
                        try:
                            ch = guild.get_channel(int(dm_channel_id))
                            owner_discord_id = sub.get("owner_discord_id")
                            member = guild.get_member(int(owner_discord_id)) if owner_discord_id else None
                            if ch and isinstance(ch, discord.TextChannel) and member:
                                await ch.set_permissions(member, overwrite=None)
                        except Exception:
                            pass

    if modified:
        save_data(data)

@tasks.loop(minutes=1)
async def weverse_billing_loop():
    await _process_weverse_subscriptions()

@weverse_billing_loop.before_loop
async def before_weverse_billing_loop():
    await bot.wait_until_ready()

async def _execute_evaluations():
    data = load_data()
    data["evaluation_config"]["last_run"] = now_iso()
    
    present_owner_ids = set()
    for guild in bot.guilds:
        for member in guild.members:
            if not member.bot:
                present_owner_ids.add(member.id)
                
    members_ocs = {}
    for oc_key, oc in data["ocs"].items():
        owner_id = oc.get("owner_id")
        if owner_id in present_owner_ids:
            members_ocs.setdefault(owner_id, []).append((oc_key, oc))
            
    results_data = {}
    for owner_id, ocs in members_ocs.items():
        user_results = []
        member_name = f"user {owner_id}"
        for guild in bot.guilds:
            m = guild.get_member(owner_id)
            if m:
                member_name = m.display_name
                break
                
        for oc_key, oc in ocs:
            amount = random.randint(100, 2500) * 100
            amount = max(10_000, min(250_000, amount))
            
            if amount <= 80000: rating = "bad"
            elif amount <= 130000: rating = "fair"
            elif amount <= 170000: rating = "good"
            elif amount <= 200000: rating = "great"
            elif amount <= 225000: rating = "excellent"
            else: rating = "outstanding"
            
            oc["balance"] += amount
            user_results.append({
                "oc_name": oc["name"],
                "rating": rating,
                "earned": amount,
                "new_balance": oc["balance"]
            })
        results_data[str(owner_id)] = {
            "member_name": member_name,
            "ocs": user_results
        }
        
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="weekly_evaluation"))
    
    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if ch and results_data:
            guild_results = {uid: res for uid, res in results_data.items() if guild.get_member(int(uid)) is not None}
            if not guild_results: continue
            pages = []
            guild_result_items = list(guild_results.items())
            total_members = len(guild_result_items)
            for n_idx, (uid, res) in enumerate(guild_result_items, start=1):
                lines = []
                for oc_res in res["ocs"]:
                    lines.append(f"**{oc_res['oc_name']}** had a/an **{oc_res['rating']}** evaluation.")
                    lines.append(f"they earned ₩{oc_res['earned']:,} — new balance: ₩{oc_res['new_balance']:,}\n")
                page_embed = discord.Embed(
                    title=f"📊 evaluation results — {res['member_name']}",
                    description="\n".join(lines),
                    color=discord.Color.blurple(),
                    timestamp=now_utc()
                )
                page_embed.set_footer(text=f"member {n_idx} of {total_members}  •  evaluation log — {LOG_CHANNEL_NAME}")
                pages.append(page_embed)
            view = EvaluationPaginatorView(pages)
            summary_embed = discord.Embed(
                title="📊 weekly evaluations",
                description="this week's evaluation results have been processed. use ◀ ▶ to browse each member's breakdown.",
                color=discord.Color.blurple(),
                timestamp=now_utc()
            )
            summary_embed.set_footer(text=f"evaluation log — {LOG_CHANNEL_NAME}")
            await ch.send(embed=pages[0], view=view)

@tasks.loop(hours=168)
async def run_weekly_evaluations():
    global _EVAL_SKIP_FIRST_TICK
    if _EVAL_SKIP_FIRST_TICK:
        _EVAL_SKIP_FIRST_TICK = False
        return
    data = load_data()
    if not data.get("evaluation_config", {}).get("running"): return
    await _execute_evaluations()

@run_weekly_evaluations.before_loop
async def before_run_weekly_evaluations():
    await bot.wait_until_ready()

@bot.tree.command(name="evaluation_toggle", description="dev | start or stop automated weekly evaluations.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def evaluation_toggle(interaction: discord.Interaction):
    guild = resolve_guild(interaction)
    if not is_dev(interaction):
        return await interaction.response.send_message("❌ denied.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        current = data["evaluation_config"]["running"]
        new_state = not current
        data["evaluation_config"]["running"] = new_state

        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="eval_toggle"))

        if new_state:
            if run_weekly_evaluations.is_running():
                run_weekly_evaluations.cancel()

            last_run_str = data["evaluation_config"].get("last_run")
            should_run_now = True
            if last_run_str:
                try:
                    last_run_dt = datetime.fromisoformat(last_run_str)
                    if last_run_dt.tzinfo is None:
                        last_run_dt = last_run_dt.replace(tzinfo=timezone.utc)
                    elapsed_hours = (now_utc() - last_run_dt).total_seconds() / 3600
                    if elapsed_hours < 168:
                        should_run_now = False
                except (ValueError, TypeError):
                    pass

            if should_run_now:
                await _execute_evaluations()
                status_desc = "evaluations are now **running**. an evaluation was executed immediately."
            else:
                status_desc = (
                    "evaluations are now **running**. "
                    f"the last evaluation ran {int(elapsed_hours)}h ago — "
                    "the next one will fire on schedule."
                )

            global _EVAL_SKIP_FIRST_TICK
            _EVAL_SKIP_FIRST_TICK = not should_run_now
            run_weekly_evaluations.start()
        else:
            status_desc = "evaluations are now **stopped**."

        data = load_data()
        last_run = data["evaluation_config"].get("last_run") or "never"

        embed = discord.Embed(
            title="evaluation config updated",
            description=status_desc,
            color=discord.Color.green()
        )
        embed.add_field(name="running", value=str(new_state), inline=True)
        embed.add_field(name="last run", value=last_run, inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
        await audit(guild, f"evaluations toggled to {new_state} by {interaction.user}")

    except Exception as e:
        log.error(f"evaluation_toggle error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

@bot.tree.command(name="evaluation_run", description="dev | manually trigger the weekly evaluation right now.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def evaluation_run_cmd(interaction: discord.Interaction):
    guild = resolve_guild(interaction)
    if not is_dev(interaction): return await interaction.response.send_message("❌ denied.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    try:
        await _execute_evaluations()
        embed = discord.Embed(title="✅ manual evaluation triggered", description="evaluations have been generated and posted.", color=discord.Color.green())
        await interaction.followup.send(embed=embed, ephemeral=True)
        await audit(guild, f"manual evaluation triggered by {interaction.user}")
    except Exception as e:
        log.error(f"evaluation_run error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

@bot.tree.command(
    name="oc_add",
    description="register a new OC into the system."
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    name            = "the OC's display name (must be unique)",
    birthday        = "format: YYYY/MM/DD (e.g. 2001/03/15)",
    gender               = "the OC's gender",
    pronouns             = "the OC's pronouns",
    face_claim           = "the OC's face claim",
    main_skill           = "the OC's main skill",
    ethnicity            = "the OC's ethnicity",
    nationality          = "the OC's nationality",
    profile_picture      = "direct image URL ending in .png/.jpg/.jpeg/.gif/.webp (optional)",
    profile_picture_file = "upload an image file directly as the OC's profile picture (alternative to URL)",
    form_link            = "any valid http/https URL (optional)",
)
async def oc_add_cmd(
    interaction: discord.Interaction,
    name: str,
    birthday: str,
    gender: str,
    pronouns: str,
    face_claim: str,
    main_skill: str,
    ethnicity: str,
    nationality: str,
    profile_picture: Optional[str] = None,
    profile_picture_file: Optional[discord.Attachment] = None,
    form_link: Optional[str] = None,
):
    guild = resolve_guild(interaction)
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()

        # 1. Duplicate key check
        key = oc_key_of(name)
        if key in data["ocs"]:
            return await interaction.followup.send(
                f"❌ An OC with the name **{name}** already exists (key: `{key}`). Choose a different name.",
                ephemeral=True
            )

        # 2. Birthday parse check
        try:
            parsed_bday = datetime.strptime(birthday.strip(), BIRTHDAY_FORMAT)
        except ValueError:
            return await interaction.followup.send(
                f"❌ Invalid birthday format. Use **{BIRTHDAY_DISPLAY}** (e.g. `2001/03/15`).",
                ephemeral=True
            )

        # 3. Birthday sanity check
        today = date.today()
        bday_date = parsed_bday.date()
        if bday_date > today or bday_date < date(today.year - 120, today.month, today.day):
            return await interaction.followup.send(
                "❌ Birthday must be a real past date.",
                ephemeral=True
            )

        # 5. Resolve profile picture from file upload OR URL
        cleaned_pic_url: Optional[str] = None
        if profile_picture_file is not None:
            if not profile_picture_file.content_type or \
               not profile_picture_file.content_type.startswith("image/"):
                return await interaction.followup.send(
                    "❌ `profile_picture_file` must be an image (png, jpg, gif, webp, etc.).",
                    ephemeral=True,
                )
            result = await persist_image_attachment(profile_picture_file, store_msg_id=False)
            if result is None:
                return await interaction.followup.send(
                    "❌ Failed to persist the uploaded profile picture. "
                    "Ensure the `#bot-assets` channel exists and the bot can send files there.",
                    ephemeral=True,
                )
            cleaned_pic_url = result[0]
            if _is_cdn_attachment(cleaned_pic_url):
                log.info(
                    "oc_add: profile_picture for '%s' is a Discord CDN URL; "
                    "persisting via /oc_edit or direct upload is recommended.", key
                )
        elif profile_picture is not None:
            if not valid_image_url(profile_picture):
                return await interaction.followup.send(
                    "❌ Profile picture must be a direct image URL ending in "
                    ".png, .jpg, .jpeg, .gif, or .webp.",
                    ephemeral=True,
                )
            cleaned_pic_url = profile_picture.split("?")[0]

        # 6. Form link URL check
        if form_link and not valid_url(form_link):
            return await interaction.followup.send(
                "❌ Form link must be a valid URL starting with http:// or https://.",
                ephemeral=True
            )


        # Build and insert OC record
        oc = {
            "name":            name.strip(),
            "birthday":        birthday.strip(),
            "gender":          gender.strip(),
            "pronouns":        pronouns.strip(),
            "face_claim":      face_claim.strip(),
            "main_skill":      main_skill.strip(),
            "ethnicity":       ethnicity.strip(),
            "nationality":     nationality.strip(),
            "profile_picture": cleaned_pic_url,
            "form_link":       form_link.strip() if form_link else None,
            "balance":         500_000,
            "owner_id":        interaction.user.id,
            "registered_at":   now_iso(),
        }
        data["ocs"][key] = oc

        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="oc_add"))

        # Success response (ephemeral)
        success_embed = build_oc_embed(oc, key)
        await interaction.followup.send(
            f"✅ **{oc['name']}** has been registered!",
            embed=success_embed,
            ephemeral=True
        )

        # Log channel post
        if guild:
            log_ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
            if log_ch:
                log_embed = discord.Embed(
                    title="🎉 New OC Registered",
                    color=discord.Color.green(),
                    description=f"**{oc['name']}** has been registered by <@{interaction.user.id}>!",
                    timestamp=now_utc()
                )
                if cleaned_pic_url:
                    log_embed.set_thumbnail(url=cleaned_pic_url)
                log_embed.add_field(name="Birthday",    value=oc["birthday"],    inline=True)
                log_embed.add_field(name="Gender",      value=oc["gender"],      inline=True)
                log_embed.add_field(name="Pronouns",    value=oc["pronouns"],    inline=True)
                log_embed.add_field(name="Face Claim",  value=oc["face_claim"],  inline=True)
                log_embed.add_field(name="Main Skill",  value=oc["main_skill"],  inline=True)
                log_embed.add_field(name="Ethnicity",   value=oc["ethnicity"],   inline=True)
                log_embed.add_field(name="Nationality", value=oc["nationality"], inline=True)
                log_embed.set_footer(text=f"OC ID: {key}")
                await log_ch.send(embed=log_embed)

        # Audit trail
        await audit(guild, f"oc_add: '{key}' registered by {interaction.user} ({interaction.user.id})")

    except Exception as e:
        log.error("oc_add error: %s", e)
        await interaction.followup.send("❌ An unexpected error occurred. Please try again.", ephemeral=True)

@bot.tree.command(
    name="oc_edit",
    description="edit an existing OC's fields or replace their profile picture.",
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    oc_name              = "the OC whose fields you want to edit",
    gender               = "new gender value (leave blank to keep current)",
    pronouns             = "new pronouns value (leave blank to keep current)",
    face_claim           = "new face claim value (leave blank to keep current)",
    main_skill           = "new main skill value (leave blank to keep current)",
    ethnicity            = "new ethnicity value (leave blank to keep current)",
    nationality          = "new nationality value (leave blank to keep current)",
    profile_picture      = "new direct image URL (leave blank to keep current)",
    profile_picture_file = "upload a new profile picture image file directly",
    form_link            = "new form URL (leave blank to keep current; set to 'clear' to remove)",
)
async def oc_edit_cmd(
    interaction: discord.Interaction,
    oc_name: str,
    gender: Optional[str]             = None,
    pronouns: Optional[str]           = None,
    face_claim: Optional[str]         = None,
    main_skill: Optional[str]         = None,
    ethnicity: Optional[str]          = None,
    nationality: Optional[str]        = None,
    profile_picture: Optional[str]    = None,
    profile_picture_file: Optional[discord.Attachment] = None,
    form_link: Optional[str]          = None,
):
    guild = resolve_guild(interaction)
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        key = oc_key_of(oc_name)
        oc = data["ocs"].get(key)
        if not oc:
            return await interaction.followup.send(
                f"❌ No OC named **{oc_name}** found.", ephemeral=True
            )

        if oc.get("owner_id") != interaction.user.id and not is_dev(interaction):
            return await interaction.followup.send(
                "❌ You do not own this OC.", ephemeral=True
            )

        changed_fields: list[str] = []

        if gender is not None:
            oc["gender"] = gender.strip()
            changed_fields.append("gender")
        if pronouns is not None:
            oc["pronouns"] = pronouns.strip()
            changed_fields.append("pronouns")
        if face_claim is not None:
            oc["face_claim"] = face_claim.strip()
            changed_fields.append("face_claim")
        if main_skill is not None:
            oc["main_skill"] = main_skill.strip()
            changed_fields.append("main_skill")
        if ethnicity is not None:
            oc["ethnicity"] = ethnicity.strip()
            changed_fields.append("ethnicity")
        if nationality is not None:
            oc["nationality"] = nationality.strip()
            changed_fields.append("nationality")

        # Resolve profile picture
        if profile_picture_file is not None:
            if not profile_picture_file.content_type or \
               not profile_picture_file.content_type.startswith("image/"):
                return await interaction.followup.send(
                    "❌ `profile_picture_file` must be an image (png, jpg, gif, webp, etc.).",
                    ephemeral=True,
                )
            result = await persist_image_attachment(profile_picture_file, store_msg_id=False)
            if result is None:
                return await interaction.followup.send(
                    "❌ Failed to persist the uploaded profile picture. "
                    "Ensure the `#bot-assets` channel exists and the bot can send files there.",
                    ephemeral=True,
                )
            oc["profile_picture"] = result[0]
            changed_fields.append("profile_picture")
        elif profile_picture is not None:
            if not valid_image_url(profile_picture):
                return await interaction.followup.send(
                    "❌ Profile picture must be a direct image URL ending in "
                    ".png, .jpg, .jpeg, .gif, or .webp.",
                    ephemeral=True,
                )
            oc["profile_picture"] = profile_picture.split("?")[0]
            changed_fields.append("profile_picture")

        # form_link special case
        if form_link is not None:
            if form_link.strip().lower() == "clear":
                oc["form_link"] = None
            elif not valid_url(form_link):
                return await interaction.followup.send(
                    "❌ Form link must be a valid URL starting with http:// or https://.",
                    ephemeral=True,
                )
            else:
                oc["form_link"] = form_link.strip()
            changed_fields.append("form_link")

        if not changed_fields:
            return await interaction.followup.send(
                "❌ No changes provided. Pass at least one field to update.", ephemeral=True
            )

        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="oc_edit"))

        success_embed = build_oc_embed(oc, key)
        await interaction.followup.send(
            f"✅ **{oc['name']}** has been updated!",
            embed=success_embed,
            ephemeral=True,
        )

        if guild:
            log_ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
            if log_ch:
                log_embed = discord.Embed(
                    title="✏️ OC Edited",
                    color=discord.Color.blurple(),
                    description=(
                        f"**{oc['name']}** was edited by <@{interaction.user.id}>.\n"
                        f"Fields changed: `{'`, `'.join(changed_fields)}`"
                    ),
                    timestamp=now_utc(),
                )
                if oc.get("profile_picture"):
                    log_embed.set_thumbnail(url=oc["profile_picture"])
                log_embed.set_footer(text=f"OC ID: {key}")
                await log_ch.send(embed=log_embed)

        await audit(
            guild,
            f"oc_edit: '{key}' edited by {interaction.user} ({interaction.user.id}). "
            f"fields: {changed_fields}",
        )

    except Exception as e:
        log.error("oc_edit error: %s", e)
        await interaction.followup.send("❌ An unexpected error occurred. Please try again.", ephemeral=True)


# ─── OC List paginator ─────────────────────────────────────────────────────────
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
        count_label = f"oc {self.current_index + 1} of {len(self.ocs)}"
        if self.filters_text:
            count_label += f"  •  {self.filters_text}"
        embed.set_footer(text=f"oc id: {key}  |  {count_label}")
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


@bot.tree.command(name="oc_list", description="browse all ocs with an optional filter.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(filter_by="field to filter by", filter_value="value to match", search_name="partial name match")
@app_commands.choices(filter_by=[app_commands.Choice(name=f, value=f) for f in FILTERABLE_FIELDS])
async def oc_list(interaction: discord.Interaction, filter_by: Optional[str] = None, filter_value: Optional[str] = None, search_name: Optional[str] = None):
    data = load_data()
    ocs  = dict(data["ocs"])

    if not ocs: return await interaction.response.send_message("❌ no ocs registered.", ephemeral=True)
    if search_name: ocs = {k: v for k, v in ocs.items() if search_name.lower() in v["name"].lower()}
    if filter_by and filter_value: ocs = {k: v for k, v in ocs.items() if str(v.get(filter_by, "")).lower() == filter_value.lower()}
    if not ocs: return await interaction.response.send_message("❌ no ocs match.", ephemeral=True)

    view = OCPaginatorView(list(ocs.items()))
    await interaction.response.send_message(embed=view.get_embed(), view=view)


@bot.tree.command(
    name="balance_edit",
    description="dev | adjust or overwrite an oc's balance."
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    oc_name  = "name of the oc whose balance to modify",
    mode     = "adjust: add/subtract a delta  |  set: overwrite to an exact value",
    amount   = "the won value — positive or negative for adjust; must be ≥ 0 for set",
    reason   = "optional audit note"
)
@app_commands.choices(mode=[
    app_commands.Choice(name="adjust — add or subtract a relative amount", value="adjust"),
    app_commands.Choice(name="set — overwrite to an exact absolute value",  value="set"),
])
async def balance_edit_cmd(
    interaction: discord.Interaction,
    oc_name: str,
    mode: str,
    amount: int,
    reason: Optional[str] = None,
):
    guild = resolve_guild(interaction)
    if not is_dev(interaction):
        return await interaction.response.send_message("❌ denied.", ephemeral=True)
    if mode == "set" and amount < 0:
        return await interaction.response.send_message("❌ amount must be 0 or greater for set mode.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        oc = data["ocs"].get(oc_key_of(oc_name))
        if not oc:
            return await interaction.followup.send("❌ oc not found.", ephemeral=True)

        if mode == "adjust":
            old_balance = oc.get("balance", 0)
            oc["balance"] = max(0, old_balance + amount)
            sign = "+" if amount >= 0 else ""
            description_line = (
                f"**{oc['name']}**\n"
                f"adjustment: {sign}₩{amount:,}\n"
                f"old balance: ₩{old_balance:,}\n"
                f"new balance: **₩{oc['balance']:,}**"
            )
            embed_title = "balance adjusted"
            audit_line = (
                f"balance adjustment for '{oc['name']}' ({sign}{amount:,}) "
                f"by {interaction.user}. reason: {reason or 'none'}"
            )
            backup_reason = "balance_edit_adjust"
        else:  # set
            old_balance = oc.get("balance", 0)
            oc["balance"] = amount
            description_line = (
                f"**{oc['name']}**\n"
                f"₩{old_balance:,} → **₩{oc['balance']:,}**"
            )
            embed_title = "balance set"
            audit_line = (
                f"balance set for '{oc['name']}' (₩{old_balance:,} → ₩{oc['balance']:,}) "
                f"by {interaction.user}. reason: {reason or 'none'}"
            )
            backup_reason = "balance_edit_set"

        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason=backup_reason))
        embed = discord.Embed(title=embed_title, description=description_line, color=discord.Color.green())
        await interaction.followup.send(embed=embed, ephemeral=True)
        await audit(guild, audit_line)
    except Exception as e:
        log.error(f"balance_edit error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

@bot.tree.command(
    name="balance_setall",
    description="dev | set every oc's balance to the same exact value at once."
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    amount = "the exact won value to assign to every oc (must be ≥ 0)",
    reason = "optional audit note"
)
async def balance_setall_cmd(
    interaction: discord.Interaction,
    amount: int,
    reason: Optional[str] = None,
):
    guild = resolve_guild(interaction)
    if not is_dev(interaction):
        return await interaction.response.send_message("❌ denied.", ephemeral=True)
    if amount < 0:
        return await interaction.response.send_message("❌ amount must be 0 or greater.", ephemeral=True)

    confirm_embed = discord.Embed(
        title="⚠️ Set All Balances",
        description=(
            f"This will overwrite the balance of **every registered OC** to ₩{amount:,}.\n"
            f"This action cannot be undone. Are you sure?"
        ),
        color=discord.Color.orange()
    )
    confirmed = await wait_for_confirm(interaction, confirm_embed)
    if not confirmed:
        await interaction.followup.send(
            embed=discord.Embed(description="Cancelled. No balances were changed.", color=discord.Color.light_grey()),
            ephemeral=True
        )
        return

    try:
        data = load_data()
        ocs = data.get("ocs", {})
        count = 0
        for oc in ocs.values():
            oc["balance"] = amount
            count += 1

        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="balance_setall"))

        embed = discord.Embed(
            title="✅ all balances set",
            description=f"**{count}** OC balance(s) have been set to ₩{amount:,}.",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        await audit(guild, f"balance_setall: set {count} oc(s) to ₩{amount:,} by {interaction.user}. reason: {reason or 'none'}")
    except Exception as e:
        log.error(f"balance_setall error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

@bot.tree.command(name="balance", description="view an oc's current balance.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(oc_name="oc name to check")
async def balance_cmd(interaction: discord.Interaction, oc_name: str):
    try:
        data = load_data()
        oc = data["ocs"].get(oc_key_of(oc_name))
        if not oc: return await interaction.response.send_message("❌ oc not found.", ephemeral=True)
        
        balance = oc.get("balance", 0)
        embed = discord.Embed(title=f"{oc['name']}'s balance", description=f"**₩{balance:,}**", color=discord.Color.green())
        if oc.get("profile_picture"):
            embed.set_thumbnail(url=oc["profile_picture"])
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        log.error(f"balance error: {e}")
        await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

class SayModal(discord.ui.Modal, title="send message as bot"):
    content = discord.ui.TextInput(
        label="message",
        style=discord.TextStyle.paragraph,
        placeholder="what should the bot say?",
        max_length=2000,
        required=True,
    )

    def __init__(self, target_channel: discord.TextChannel, reply_to_id: Optional[int] = None):
        super().__init__()
        self.target_channel = target_channel
        self.reply_to_id: Optional[int] = reply_to_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.content.value.strip()
        if not text:
            return await interaction.response.send_message("❌ message cannot be empty.", ephemeral=True)
        try:
            reference: Optional[discord.MessageReference] = None
            if self.reply_to_id:
                try:
                    ref_msg = await self.target_channel.fetch_message(self.reply_to_id)
                    reference = ref_msg.to_reference(fail_if_not_exists=False)
                except discord.NotFound:
                    return await interaction.response.send_message(
                        f"❌ message `{self.reply_to_id}` not found in {self.target_channel.mention}.",
                        ephemeral=True,
                    )
            await self.target_channel.send(content=text, reference=reference)
            await interaction.response.send_message(
                f"✅ sent to {self.target_channel.mention}.", ephemeral=True
            )
            guild = resolve_guild(interaction)
            if guild:
                await audit(
                    guild,
                    f"say: {interaction.user} sent message to #{self.target_channel.name}"
                    + (f" (reply to {self.reply_to_id})" if self.reply_to_id else ""),
                )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ bot lacks permission to send messages in {self.target_channel.mention}.",
                ephemeral=True,
            )
        except Exception as e:
            log.error(f"say modal error: {e}")
            await interaction.response.send_message("❌ an unexpected error occurred.", ephemeral=True)

@bot.tree.command(name="say", description="dev | send a plain-text message as the bot to any channel.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    channel="the channel to send the message to",
    reply_to="(optional) message ID to reply to in that channel",
)
async def say_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    reply_to: Optional[str] = None,
) -> None:
    if not is_dev(interaction):
        return await interaction.response.send_message("❌ denied.", ephemeral=True)
    reply_to_id: Optional[int] = None
    if reply_to is not None:
        reply_to = reply_to.strip()
        if not reply_to.isdigit():
            return await interaction.response.send_message(
                "❌ `reply_to` must be a numeric message ID.", ephemeral=True
            )
        reply_to_id = int(reply_to)
    modal = SayModal(target_channel=channel, reply_to_id=reply_to_id)
    await interaction.response.send_modal(modal)

class TwitterPostView(discord.ui.View):
    def __init__(self, tweet_id: str, likes: int = 0, retweets: int = 0):
        super().__init__(timeout=None)
        self.tweet_id  = tweet_id
        self.likes     = likes
        self.retweets  = retweets
        self._sync_labels()

    def _sync_labels(self):
        for child in self.children:
            cid = getattr(child, "custom_id", "")
            if cid == "tw_like_btn":
                child.label = f"🤍 {self.likes}" if self.likes else "🤍"
            elif cid == "tw_rt_btn":
                child.label = f"🔁 {self.retweets}" if self.retweets else "🔁"

    @discord.ui.button(label="🤍", style=discord.ButtonStyle.secondary, custom_id="tw_like_btn")
    async def like_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        tweet = data.get("twitter", {}).get(self.tweet_id)
        if not tweet:
            return await interaction.response.send_message("❌ tweet not found.", ephemeral=True)
        tweet["likes"] = tweet.get("likes", 0) + 1
        self.likes = tweet["likes"]
        self._sync_labels()
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="tw_like"))
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary, custom_id="tw_rt_btn")
    async def retweet_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        tweet = data.get("twitter", {}).get(self.tweet_id)
        if not tweet:
            return await interaction.response.send_message("❌ tweet not found.", ephemeral=True)
        tweet["retweets"] = tweet.get("retweets", 0) + 1
        self.retweets = tweet["retweets"]
        self._sync_labels()
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="tw_retweet"))
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="💬 reply", style=discord.ButtonStyle.primary, custom_id="tw_reply_btn")
    async def reply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = resolve_guild(interaction)
        data  = load_data()
        tweet = data.get("twitter", {}).get(self.tweet_id)
        if not tweet:
            return await interaction.response.send_message("❌ tweet not found.", ephemeral=True)

        if tweet.get("thread_id") and guild:
            existing = guild.get_thread(tweet["thread_id"])
            if existing:
                return await interaction.response.send_message(
                    f"💬 thread already open: {existing.mention}", ephemeral=True
                )

        ch = guild.get_channel(tweet.get("channel_id")) if guild else None
        if not ch:
            return await interaction.response.send_message("❌ tweet channel not found.", ephemeral=True)
        try:
            msg = await ch.fetch_message(tweet["message_id"])
        except Exception:
            return await interaction.response.send_message("❌ original tweet message missing.", ephemeral=True)

        thread = await msg.create_thread(
            name=f"replies — {tweet['handle']}", auto_archive_duration=10080
        )
        tweet["thread_id"] = thread.id
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="tw_reply_thread"))
        await interaction.response.send_message(f"💬 reply thread: {thread.mention}", ephemeral=True)

@bot.tree.command(name="twitter_post", description="post a tweet (up to 4 photos/videos) as your oc.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    oc_name="oc name",
    handle="twitter/x handle (without @)",
    body="tweet text (max 280 chars)",
    media1_url="url for media 1 (image or video)",
    media1_file="upload for media 1 (image or video file)",
    media2_url="url for media 2",
    media2_file="upload for media 2",
    media3_url="url for media 3",
    media3_file="upload for media 3",
    media4_url="url for media 4",
    media4_file="upload for media 4",
)
async def twitter_post_cmd(
    interaction: discord.Interaction,
    oc_name: str,
    handle: str,
    body: str,
    media1_url: Optional[str] = None, media1_file: Optional[discord.Attachment] = None,
    media2_url: Optional[str] = None, media2_file: Optional[discord.Attachment] = None,
    media3_url: Optional[str] = None, media3_file: Optional[discord.Attachment] = None,
    media4_url: Optional[str] = None, media4_file: Optional[discord.Attachment] = None,
):
    guild = resolve_guild(interaction)
    await interaction.response.defer(ephemeral=True)

    if len(body) > 280:
        return await interaction.followup.send("❌ tweet body exceeds 280 characters.", ephemeral=True)

    data = load_data()
    oc_key = oc_key_of(oc_name)
    if oc_key not in data["ocs"]:
        return await interaction.followup.send("❌ oc not found.", ephemeral=True)
    if not is_dev(interaction) and data["ocs"][oc_key].get("owner_id") != interaction.user.id:
        return await interaction.followup.send("❌ you do not own this oc.", ephemeral=True)

    oc = data["ocs"][oc_key]
    handle_fmt = f"@{handle}" if not handle.startswith("@") else handle

    raw_pairs = [
        (media1_url, media1_file), (media2_url, media2_file),
        (media3_url, media3_file), (media4_url, media4_file),
    ]
    media = []
    for url, file in raw_pairs:
        if file:
            if not file.content_type or not (
                file.content_type.startswith("image/") or
                file.content_type.startswith("video/")
            ):
                return await interaction.followup.send(
                    "❌ media files must be images or videos.", ephemeral=True
                )
            result = await persist_media_attachment(file)
            if result:
                clean_url, _, media_tag = result
                media.append({"url": clean_url, "type": media_tag})
            else:
                media.append({
                    "url": file.url,
                    "type": "video" if file.content_type.startswith("video/") else "image",
                })
        elif url:
            ok, tag = valid_media_url(url)
            if not ok:
                return await interaction.followup.send(
                    "❌ invalid media url. accepted: .png .jpg .jpeg .gif .webp .mp4 .mov .webm",
                    ephemeral=True,
                )
            media.append({"url": url, "type": tag})

    tweet_id = f"tw_{oc_key}_{int(now_utc().timestamp())}"
    data.setdefault("twitter", {})[tweet_id] = {
        "oc_key":     oc_key,
        "handle":     handle_fmt,
        "body":       body,
        "media":      media,
        "likes":      0,
        "retweets":   0,
        "posted_by":  interaction.user.id,
        "posted_at":  now_iso(),
    }
    save_data(data)

    tw_ch = discord.utils.get(guild.text_channels, name=TWITTER_CHANNEL_NAME)
    if not tw_ch:
        return await interaction.followup.send(
            f"❌ twitter channel `{TWITTER_CHANNEL_NAME}` not found.", ephemeral=True
        )

    await interaction.followup.send("🐦 posting...", ephemeral=True)

    view = TwitterPostView(tweet_id, likes=0, retweets=0)
    color = discord.Color.from_rgb(29, 161, 242)

    header_embed = discord.Embed(description=body, color=color, timestamp=now_utc())
    header_embed.set_author(
        name=f"{oc['name']}  ({handle_fmt})",
        icon_url=oc.get("profile_picture"),
    )
    if oc.get("profile_picture"):
        header_embed.set_thumbnail(url=oc["profile_picture"])

    if not media:
        first_msg = await tw_ch.send(embed=header_embed, view=view)
    elif media[0]["type"] == "image":
        header_embed.set_image(url=media[0]["url"])
        if len(media) == 1:
            first_msg = await tw_ch.send(embed=header_embed, view=view)
        else:
            first_msg = await tw_ch.send(embed=header_embed)
            for m in media[1:-1]:
                if m["type"] == "image":
                    e = discord.Embed(color=color)
                    e.set_image(url=m["url"])
                    await tw_ch.send(embed=e)
                else:
                    await tw_ch.send(content=m["url"])
            last_m = media[-1]
            if last_m["type"] == "image":
                e = discord.Embed(color=color)
                e.set_image(url=last_m["url"])
                await tw_ch.send(embed=e, view=view)
            else:
                await tw_ch.send(content=last_m["url"], view=view)
    else:
        first_msg = await tw_ch.send(embed=header_embed)
        await tw_ch.send(content=media[0]["url"])
        for m in media[1:-1]:
            if m["type"] == "image":
                e = discord.Embed(color=color)
                e.set_image(url=m["url"])
                await tw_ch.send(embed=e)
            else:
                await tw_ch.send(content=m["url"])
        if len(media) > 1:
            last_m = media[-1]
            if last_m["type"] == "image":
                e = discord.Embed(color=color)
                e.set_image(url=last_m["url"])
                await tw_ch.send(embed=e, view=view)
            else:
                await tw_ch.send(content=last_m["url"], view=view)
        else:
            await first_msg.edit(view=view)

    data["twitter"][tweet_id]["message_id"]  = first_msg.id
    data["twitter"][tweet_id]["channel_id"]  = tw_ch.id
    save_data(data)
    asyncio.ensure_future(push_backup_to_discord(data, reason="twitter_post"))


@bot.tree.command(name="shop_add_category", description="dev | add a new shop category.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(name="category name")
async def shop_add_category(interaction: discord.Interaction, name: str):
    guild = resolve_guild(interaction)
    if not is_dev(interaction): return await interaction.response.send_message("❌ denied.", ephemeral=True)
    name = name.strip()
    if not name or len(name) > 50: return await interaction.response.send_message("❌ invalid name length.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        ensure_shop_keys(data)
        if any(c.lower() == name.lower() for c in data["shop_categories"]):
            return await interaction.followup.send("❌ category exists.")
        data["shop_categories"].append(name)
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="add_cat"))
        
        embed = discord.Embed(title="category added", description=f"**{name}**\n\nall categories:\n" + "\n".join(f"• {c}" for c in data["shop_categories"]), color=discord.Color.green())
        await interaction.followup.send(embed=embed)
        await audit(guild, f"shop category added: '{name}' by {interaction.user}")
    except Exception as e:
        log.error(f"shop_add_cat error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.")

@bot.tree.command(name="shop_remove_category", description="dev | remove a shop category.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(name="category name")
async def shop_remove_category(interaction: discord.Interaction, name: str):
    guild = resolve_guild(interaction)
    if not is_dev(interaction): return await interaction.response.send_message("❌ denied.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        ensure_shop_keys(data)
        matched = next((c for c in data["shop_categories"] if c.lower() == name.strip().lower()), None)
        if not matched: return await interaction.followup.send("❌ category not found.")
        
        data["shop_categories"].remove(matched)
        affected = 0
        for item in data["shop"].values():
            if item.get("category", "").lower() == matched.lower():
                item["category"] = None
                affected += 1
                
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="rm_cat"))
        embed = discord.Embed(title="category removed", description=f"**{matched}** removed. {affected} items orphaned.", color=discord.Color.green())
        await interaction.followup.send(embed=embed)
        await audit(guild, f"shop category removed: '{matched}' by {interaction.user}")
    except Exception as e:
        log.error(f"shop_rm_cat error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.")

@bot.tree.command(name="shop_add_album", description="dev | add a new album to the shop.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(name="album name", group="group", price="cost in won", inclusion_count="number of pulls", category="category name", image="album cover")
async def shop_add_album(interaction: discord.Interaction, name: str, group: str, price: int, inclusion_count: int, category: Optional[str] = None, image: Optional[discord.Attachment] = None):
    if not is_dev(interaction): return await interaction.response.send_message("❌ denied.", ephemeral=True)
    if price < 0 or inclusion_count < 1: return await interaction.response.send_message("❌ invalid numerical value.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        ensure_shop_keys(data)
        cat_match = None
        if category:
            cat_match = next((c for c in data["shop_categories"] if c.lower() == category.strip().lower()), None)
            if not cat_match: return await interaction.followup.send(f"❌ category not found. available: {', '.join(data['shop_categories'])}")
            
        pic_url, pic_id = None, None
        if image:
            if not image.content_type or not image.content_type.startswith("image/"): return await interaction.followup.send("❌ attachment must be image.")
            p = await persist_image_attachment(image, store_msg_id=True)
            if p: pic_url, pic_id = p
            
        item_id = _gen_item_id(data["shop"], data.get("used_item_ids", []))
        data.setdefault("used_item_ids", []).append(item_id)
            
        data["shop"][item_id] = {
            "id": item_id, "type": "album", "name": name, "group": group, "price": price,
            "inclusion_count": inclusion_count, "category": cat_match, "inclusions": [],
            "image_url": pic_url, "image_msg_id": pic_id, "created_at": now_iso()
        }
        
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="add_album"))
        
        embed = discord.Embed(title="album added", description=f"id: `{item_id}`\nname: {name}\ngroup: {group}\nprice: ₩{price:,}", color=discord.Color.green())
        await interaction.followup.send(embed=embed)
    except Exception as e:
        log.error(f"shop_add_album error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.")

@bot.tree.command(name="shop_add_misc", description="dev | add a miscellaneous item to the shop.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(name="item name", price="cost in won", category="category name", image="item image")
async def shop_add_misc(interaction: discord.Interaction, name: str, price: int, category: Optional[str] = None, image: Optional[discord.Attachment] = None):
    if not is_dev(interaction): return await interaction.response.send_message("❌ denied.", ephemeral=True)
    if price < 0: return await interaction.response.send_message("❌ invalid price.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        ensure_shop_keys(data)
        cat_match = None
        if category:
            cat_match = next((c for c in data["shop_categories"] if c.lower() == category.strip().lower()), None)
            if not cat_match: return await interaction.followup.send(f"❌ category not found. available: {', '.join(data['shop_categories'])}")
            
        pic_url, pic_id = None, None
        if image:
            if not image.content_type or not image.content_type.startswith("image/"): return await interaction.followup.send("❌ attachment must be image.")
            p = await persist_image_attachment(image, store_msg_id=True)
            if p: pic_url, pic_id = p
            
        item_id = _gen_item_id(data["shop"], data.get("used_item_ids", []))
        data.setdefault("used_item_ids", []).append(item_id)
            
        data["shop"][item_id] = {
            "id": item_id, "type": "misc", "name": name, "price": price,
            "category": cat_match, "image_url": pic_url, "image_msg_id": pic_id, "created_at": now_iso()
        }
        
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="add_misc"))
        embed = discord.Embed(title="misc item added", description=f"id: `{item_id}`\nname: {name}\nprice: ₩{price:,}", color=discord.Color.green())
        await interaction.followup.send(embed=embed)
    except Exception as e:
        log.error(f"shop_add_misc error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.")

@bot.tree.command(name="shop_add_inclusion", description="dev | add an inclusion to an existing album.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(item_id="7-digit item number (e.g. 1234567)", inclusion_name="name", rarity="positive int (higher = rarer)", image="image file")
async def shop_add_inclusion(interaction: discord.Interaction, item_id: str, inclusion_name: str, rarity: int, image: Optional[discord.Attachment] = None):
    if not is_dev(interaction): return await interaction.response.send_message("❌ denied.", ephemeral=True)
    if rarity < 1: return await interaction.response.send_message("❌ rarity must be >= 1.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        ensure_shop_keys(data)
        normalized_id = _resolve_item_id(item_id, data["shop"])
        if not normalized_id:
            return await interaction.followup.send("❌ item not found.")
        item = data["shop"][normalized_id]
        if item.get("type") != "album": 
            return await interaction.followup.send(
                "❌ inclusions can only be added to album items. "
                f"`{normalized_id}` is a `{item.get('type', 'unknown')}` item."
            )
        
        pic_url, pic_id = None, None
        if image:
            if not image.content_type or not image.content_type.startswith("image/"): return await interaction.followup.send("❌ attachment must be image.")
            p = await persist_image_attachment(image, store_msg_id=True)
            if p: pic_url, pic_id = p
            
        all_used_inc = {
            inc["inclusion_id"]
            for shop_item in data["shop"].values()
            for inc in shop_item.get("inclusions", [])
        }
        inc_id = _gen_inclusion_id(all_used_inc, data.get("used_inclusion_ids", []))
        data.setdefault("used_inclusion_ids", []).append(inc_id)
            
        item["inclusions"].append({
            "inclusion_id": inc_id, "name": inclusion_name, "rarity": rarity,
            "image_url": pic_url, "image_msg_id": pic_id
        })
        
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="add_inc"))
        embed = discord.Embed(title="inclusion added", description=f"album: {item['name']}\ninc: {inclusion_name}\nrarity: {rarity}", color=discord.Color.green())
        await interaction.followup.send(embed=embed)
    except Exception as e:
        log.error(f"shop_add_inc error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.")

@bot.tree.command(name="shop_remove_inclusion", description="dev | permanently delete an inclusion from an album.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    item_id="7-digit album item number (e.g. 1234567)",
    inclusion_id="inclusion ID without 'inc_' prefix (e.g. 7654321)",
)
async def shop_remove_inclusion_cmd(
    interaction: discord.Interaction,
    item_id: str,
    inclusion_id: str,
) -> None:
    if not is_dev(interaction):
        return await interaction.response.send_message("❌ denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        ensure_shop_keys(data)
        normalized_item_id = _resolve_item_id(item_id, data["shop"])
        if not normalized_item_id:
            return await interaction.followup.send("❌ album item not found.", ephemeral=True)
        item = data["shop"][normalized_item_id]
        if item.get("type") != "album":
            return await interaction.followup.send(
                f"❌ `{normalized_item_id}` is a `{item.get('type', 'unknown')}` — only albums have inclusions.",
                ephemeral=True,
            )
        inc_id_norm = (
            f"inc_{inclusion_id.strip()}"
            if not inclusion_id.strip().startswith("inc_")
            else inclusion_id.strip()
        )
        inc = next(
            (i for i in item.get("inclusions", []) if i["inclusion_id"] == inc_id_norm),
            None,
        )
        if not inc:
            return await interaction.followup.send(
                f"❌ inclusion `{inc_id_norm}` not found on **{item['name']}**.", ephemeral=True
            )
        confirm_view = ConfirmView()
        confirm_embed = discord.Embed(
            title="⚠️ confirm deletion",
            description=(
                f"**album:** {item['name']} (`{normalized_item_id}`)\n"
                f"**inclusion:** {inc['name']} (`{inc_id_norm}`)\n"
                f"**rarity:** {inc.get('rarity', '?')}\n\n"
                "this is **permanent** and cannot be undone. "
                "the inclusion ID will be retired and never reused."
            ),
            color=discord.Color.red(),
        )
        msg = await interaction.followup.send(embed=confirm_embed, view=confirm_view, ephemeral=True)
        await confirm_view.wait()
        if not confirm_view.confirmed:
            for child in confirm_view.children:
                child.disabled = True
            await msg.edit(view=confirm_view)
            return await interaction.followup.send("❌ deletion cancelled.", ephemeral=True)
        data = load_data()
        ensure_shop_keys(data)
        item = data["shop"][normalized_item_id]
        before_count = len(item.get("inclusions", []))
        item["inclusions"] = [
            i for i in item.get("inclusions", []) if i["inclusion_id"] != inc_id_norm
        ]
        after_count = len(item["inclusions"])
        if before_count == after_count:
            return await interaction.followup.send(
                f"❌ inclusion `{inc_id_norm}` was already removed.", ephemeral=True
            )
        data.setdefault("used_inclusion_ids", [])
        if inc_id_norm not in data["used_inclusion_ids"]:
            data["used_inclusion_ids"].append(inc_id_norm)
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="rm_inclusion"))
        result_embed = discord.Embed(
            title="✅ inclusion deleted",
            description=(
                f"**album:** {item['name']} (`{normalized_item_id}`)\n"
                f"**removed:** {inc['name']} (`{inc_id_norm}`)\n"
                f"inclusions remaining: {after_count}"
            ),
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=result_embed, ephemeral=True)
        guild = resolve_guild(interaction)
        if guild:
            await audit(
                guild,
                f"rm_inclusion: {interaction.user} deleted {inc['name']} ({inc_id_norm}) "
                f"from {item['name']} ({normalized_item_id})",
            )
    except Exception as e:
        log.error(f"shop_remove_inclusion error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

@bot.tree.command(name="shop_edit_item", description="dev | edit an existing shop item.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    item_id="7-digit item number (e.g. 1234567)", name="new name", price="new price", group="new group", 
    inclusion_count="new count", category="new cat (empty string to clear)",
    inclusion_id="7-digit inclusion number (e.g. 1234567, without 'inc_' prefix)", inclusion_name="new inclusion name", inclusion_rarity="new inclusion rarity", inclusion_image="new inclusion image"
)
async def shop_edit_item(
    interaction: discord.Interaction, item_id: str,
    name: Optional[str] = None, price: Optional[int] = None, group: Optional[str] = None, 
    inclusion_count: Optional[int] = None, category: Optional[str] = None, image: Optional[discord.Attachment] = None,
    inclusion_id: Optional[str] = None, inclusion_name: Optional[str] = None, inclusion_rarity: Optional[int] = None, inclusion_image: Optional[discord.Attachment] = None
):
    if not is_dev(interaction): return await interaction.response.send_message("❌ denied.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        ensure_shop_keys(data)
        normalized_id = _resolve_item_id(item_id, data["shop"])
        if not normalized_id:
            return await interaction.followup.send("❌ item not found.")
        item = data["shop"][normalized_id]
        
        changes = []
        if name: item["name"] = name; changes.append("name")
        if price is not None and price >= 0: item["price"] = price; changes.append("price")
        if group and item.get("type") == "album": item["group"] = group; changes.append("group")
        if inclusion_count is not None and inclusion_count >= 1 and item.get("type") == "album": item["inclusion_count"] = inclusion_count; changes.append("inclusion_count")
        
        if category is not None:
            if category.strip() == "":
                item["category"] = None
                changes.append("category (cleared)")
            else:
                cat_match = next((c for c in data["shop_categories"] if c.lower() == category.strip().lower()), None)
                if not cat_match: return await interaction.followup.send("❌ category not found.")
                item["category"] = cat_match
                changes.append("category")
                
        if image:
            p = await persist_image_attachment(image, store_msg_id=True)
            if p:
                item["image_url"], item["image_msg_id"] = p
                changes.append("image")
                
        if inclusion_id:
            if item.get("type") != "album":
                return await interaction.followup.send(
                    "❌ inclusion edits are only valid on album items. "
                    f"`{normalized_id}` is a `{item.get('type', 'unknown')}` item."
                )
            if not any([inclusion_name, inclusion_rarity is not None, inclusion_image]):
                return await interaction.followup.send("❌ provide at least one of: inclusion_name, inclusion_rarity, inclusion_image when specifying an inclusion_id.")
            
            inc_id_norm = f"inc_{inclusion_id.strip()}" if not inclusion_id.strip().startswith("inc_") else inclusion_id.strip()
            inc = next((i for i in item.get("inclusions", []) if i["inclusion_id"] == inc_id_norm), None)
            if not inc: return await interaction.followup.send("❌ inclusion not found.")
            
            if inclusion_name:
                inc["name"] = inclusion_name
                changes.append("inclusion name")
            if inclusion_rarity is not None and inclusion_rarity >= 1:
                inc["rarity"] = inclusion_rarity
                changes.append("inclusion rarity")
            if inclusion_image:
                if not inclusion_image.content_type or not inclusion_image.content_type.startswith("image/"):
                    return await interaction.followup.send("❌ inclusion image must be an image file.")
                p = await persist_image_attachment(inclusion_image, store_msg_id=True)
                if p:
                    inc["image_url"], inc["image_msg_id"] = p
                    changes.append("inclusion image")
                
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="edit_item"))
        embed = discord.Embed(title="item edited", description=f"fields updated: {', '.join(changes) if changes else 'none'}", color=discord.Color.green())
        await interaction.followup.send(embed=embed)
    except Exception as e:
        log.error(f"shop_edit error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.")

@bot.tree.command(name="shop_remove_item", description="dev | remove an item from the shop entirely.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(item_id="7-digit item number (e.g. 1234567)", confirm="type the item name exactly to confirm")
async def shop_remove_item(interaction: discord.Interaction, item_id: str, confirm: str):
    if not is_dev(interaction): return await interaction.response.send_message("❌ denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        ensure_shop_keys(data)
        normalized_id = _resolve_item_id(item_id, data["shop"])
        if not normalized_id:
            return await interaction.followup.send("❌ item not found.")
        item = data["shop"][normalized_id]
        
        if item["name"] != confirm:
            return await interaction.followup.send("❌ confirmation name does not match item name.")
            
        del data["shop"][normalized_id]
        data.setdefault("used_item_ids", []).append(normalized_id)
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="rm_item"))
        embed = discord.Embed(title="item removed", color=discord.Color.green())
        await interaction.followup.send(embed=embed)
    except Exception as e:
        log.error(f"shop_rm_item error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.")


def _build_shop_pages(data) -> dict:
    categories = {}
    for item in data["shop"].values():
        if item.get("type") not in PURCHASABLE_TYPES:
            continue
        cat = item.get("category")
        if not cat or cat.lower() not in [c.lower() for c in data["shop_categories"]]:
            cat = "uncategorized"
        else:
            cat = next(c for c in data["shop_categories"] if c.lower() == cat.lower())
        categories.setdefault(cat, []).append(item)
        
    for cat in categories:
        categories[cat].sort(key=lambda i: i["name"].lower())
        
    sorted_cats = sorted([c for c in categories if c != "uncategorized"], key=str.lower)
    if "uncategorized" in categories:
        sorted_cats.append("uncategorized")
        
    pages = {}
    for cat in sorted_cats:
        items = categories[cat]
        chunked = [items[i:i + 10] for i in range(0, len(items), 10)]
        if not chunked:
            chunked = [[]]
        pages[cat] = chunked
    return pages

class PurchaseRevealView(discord.ui.View):
    def __init__(
        self,
        pulled: list[dict],
        album_name: str,
        oc_name: str,
        album_image_url: Optional[str],
        invoker_id: int,
        quantity: int = 1,
    ):
        super().__init__(timeout=300)
        self.pulled          = pulled
        self.album_name      = album_name
        self.oc_name         = oc_name
        self.album_cover     = album_image_url
        self.invoker_id      = invoker_id
        self.quantity        = quantity
        self.index           = 0
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "❌ this reveal belongs to someone else.", ephemeral=True
            )
            return False
        return True

    def _sync_buttons(self) -> None:
        if not self.pulled: return
        self.prev_btn.disabled = (self.index == 0)
        self.next_btn.disabled = (self.index >= len(self.pulled) - 1)

    def _rarity_color(self, rarity_int: int) -> discord.Color:
        if rarity_int == 1:   return discord.Color.light_grey()
        if rarity_int == 2:   return discord.Color.green()
        if rarity_int == 3:   return discord.Color.blue()
        if rarity_int == 4:   return discord.Color.purple()
        if rarity_int >= 5:   return discord.Color.gold()
        return discord.Color.blurple()

    def build_embed(self) -> discord.Embed:
        if not self.pulled: return discord.Embed(description="no inclusions pulled.")
        inc   = self.pulled[self.index]
        total = len(self.pulled)
        page  = self.index + 1

        rarity_int   = inc.get("rarity", 0)
        all_rarities = [i.get("rarity", 0) for i in self.pulled]
        rarity_label = _rarity_label_proportional(rarity_int, all_rarities)

        qty_note = f" (x{self.quantity} purchased)" if self.quantity > 1 else ""
        embed = discord.Embed(
            title=f"✨ {inc['name']}",
            description=(
                f"**album:** {self.album_name}{qty_note}\n"
                f"**oc:** {self.oc_name}"
            ),
            color=self._rarity_color(rarity_int),
        )
        embed.add_field(name="rarity", value=rarity_label, inline=True)
        embed.add_field(name="inclusion", value=f"{page} of {total}", inline=True)

        img_url = inc.get("image_url") or self.album_cover
        if img_url:
            embed.set_image(url=img_url)

        embed.set_footer(text="use ◀ ▶ to reveal your other inclusions.")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="reveal_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="reveal_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

async def _handle_shop_buy(interaction: discord.Interaction, item_id: str, oc_name: str, quantity: int = 1):
    data = load_data()
    ensure_shop_keys(data)
    item = data["shop"].get(item_id)
    if not item:
        if item_id.startswith("inc_"):
            return await interaction.response.send_message(
                "❌ that looks like an inclusion ID (`inc_…`). inclusions cannot be purchased directly — "
                "they are part of albums. browse the shop with `/shop` and buy the parent album instead.",
                ephemeral=True
            )
        return await interaction.response.send_message("❌ item not found.", ephemeral=True)
        
    oc_key = oc_key_of(oc_name)
    oc = data["ocs"].get(oc_key)
    if not oc: return await interaction.response.send_message("❌ oc not found.", ephemeral=True)
        
    if not is_dev(interaction) and oc.get("owner_id") != interaction.user.id:
        return await interaction.response.send_message("❌ you do not own this oc.", ephemeral=True)
        
    quantity = max(1, quantity)
    total_cost = item["price"] * quantity
    
    if oc["balance"] < total_cost:
        return await interaction.response.send_message(f"❌ {oc['name']} doesn't have enough won. current balance: ₩{oc['balance']:,} — purchase costs: ₩{total_cost:,}.", ephemeral=True)
        
    embed = discord.Embed(title="confirm purchase", color=discord.Color.orange())
    qty_str = f" x{quantity}" if quantity > 1 else ""
    embed.description = f"**item:** {item['name']}{qty_str}\n**total cost:** ₩{total_cost:,}\n**buyer:** {oc['name']}\n**current balance:** ₩{oc['balance']:,}"
    
    view = discord.ui.View()
    
    async def confirm_cb(inter):
        try:
            d = load_data()
            ensure_shop_keys(d)
            i_data = d["shop"].get(item_id)
            o_data = d["ocs"].get(oc_key)
            if not i_data or not o_data or o_data["balance"] < i_data["price"] * quantity:
                return await inter.response.edit_message(content="❌ purchase failed (insufficient balance or missing info).", embed=None, view=None)
                
            o_data["balance"] -= i_data["price"] * quantity
            all_pulled = []
            
            for _ in range(quantity):
                instance = {"acquired_at": now_iso()}
                pulled = []
                if i_data.get("type") == "album":
                    count = i_data.get("inclusion_count", 0)
                    incs = i_data.get("inclusions", [])
                    if count > 0 and incs:
                        seen_ids: set[str] = set()
                        pool = []
                        for inc in incs:
                            repeat = max(1, round(6 - inc.get("rarity", 1)))
                            pool.extend([inc] * repeat)

                        if len(pool) >= count:
                            choices = random.sample(pool, k=count)
                        else:
                            weights = [1.0 / max(inc.get("rarity", 1), 1) for inc in incs]
                            choices = []
                            attempts = 0
                            while len(choices) < count and attempts < count * 10:
                                candidate = random.choices(incs, weights=weights, k=1)[0]
                                if candidate["inclusion_id"] not in seen_ids:
                                    choices.append(candidate)
                                    seen_ids.add(candidate["inclusion_id"])
                                attempts += 1
                            if len(choices) < count:
                                remaining = count - len(choices)
                                choices.extend(random.choices(incs, weights=weights, k=remaining))

                        for c in choices:
                            pulled.append({
                                "inclusion_id": c["inclusion_id"],
                                "name": c["name"],
                                "rarity": c["rarity"],
                                "image_url": c.get("image_url"),
                            })
                        all_pulled.extend(pulled)
                instance["pulled_inclusions"] = pulled
                d["inventories"].setdefault(oc_key, {}).setdefault(item_id, []).append(instance)
                
            save_data(d)
            asyncio.ensure_future(push_backup_to_discord(d, reason="shop_buy"))
            
            qty_success_str = f" x{quantity}" if quantity > 1 else ""
            res_embed = discord.Embed(
                title="🛍️ purchase successful",
                description=(
                    f"**{o_data['name']}** bought **{i_data['name']}**{qty_success_str} "
                    f"for ₩{i_data['price'] * quantity:,}!\n"
                    f"new balance: ₩{o_data['balance']:,}"
                ),
                color=discord.Color.green(),
            )

            if all_pulled:
                reveal_view = PurchaseRevealView(
                    pulled          = all_pulled,
                    album_name      = i_data["name"],
                    oc_name         = o_data["name"],
                    album_image_url = i_data.get("image_url"),
                    invoker_id      = inter.user.id,
                    quantity        = quantity,
                )
                await inter.response.edit_message(content=None, embed=res_embed, view=None)
                await inter.followup.send(
                    embed=reveal_view.build_embed(),
                    view=reveal_view
                )
            else:
                await inter.response.edit_message(content=None, embed=res_embed, view=None)
        except Exception as e:
            log.error(f"buy confirm error: {e}")
            await inter.response.edit_message(content="❌ an unexpected error occurred.", embed=None, view=None)
            
    async def cancel_cb(inter): await inter.response.edit_message(content="❌ purchase cancelled.", embed=None, view=None)
        
    btn_y = discord.ui.Button(label="confirm", style=discord.ButtonStyle.success)
    btn_y.callback = confirm_cb
    btn_n = discord.ui.Button(label="cancel", style=discord.ButtonStyle.danger)
    btn_n.callback = cancel_cb
    view.add_item(btn_y)
    view.add_item(btn_n)
    
    if interaction.response.is_done(): await interaction.followup.send(embed=embed, view=view)
    else: await interaction.response.send_message(embed=embed, view=view)

class InclusionPaginatorView(discord.ui.View):
    def __init__(self, item: dict, pages: list[list[dict]], invoker_id: int):
        super().__init__(timeout=300)
        self.item = item
        self.pages = pages
        self.index = 0
        self.invoker_id = invoker_id
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "❌ this panel belongs to someone else.", ephemeral=True
            )
            return False
        return True

    def _sync_buttons(self):
        self.prev_btn.disabled = (self.index == 0)
        self.next_btn.disabled = (self.index >= len(self.pages) - 1)

    def build_embed(self) -> discord.Embed:
        page = self.pages[self.index]
        total_pages = len(self.pages)
        embed = discord.Embed(
            title=f"{self.item['name']} — possible inclusions",
            description=f"page {self.index + 1} of {total_pages}  •  sorted by rarity, then a–z",
            color=discord.Color.gold()
        )
        for inc in page:
            rarity_int = inc.get("rarity", 0)
            rarity_label = _rarity_label(rarity_int)
            embed.add_field(
                name=inc["name"],
                value=f"rarity: {rarity_label}  •  `{inc['inclusion_id']}`",
                inline=False
            )
        if self.item.get("image_url"):
            embed.set_thumbnail(url=self.item["image_url"])
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="inc_details_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="inc_details_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

class ShopDetailsModal(discord.ui.Modal, title="item details"):
    item_id = discord.ui.TextInput(label="item id", placeholder="e.g. 1234567 (7-digit number only)")

    async def on_submit(self, interaction):
        raw = self.item_id.value.strip()
        if raw.startswith("inc_") or raw.startswith("inc"):
            return await interaction.response.send_message(
                "❌ that looks like an inclusion ID (`inc_…`). inclusions are part of albums, not standalone shop items. "
                "use `/open` to browse an album's inclusions.",
                ephemeral=True
            )
        full_id = f"id_{raw}" if not raw.startswith("id_") else raw

        data = load_data()
        ensure_shop_keys(data)
        item = data["shop"].get(full_id)
        if not item:
            return await interaction.response.send_message("❌ item not found.", ephemeral=True)

        embed = discord.Embed(title=item["name"], color=discord.Color.gold())
        embed.add_field(name="id", value=f"`{full_id}`", inline=True)
        embed.add_field(name="price", value=f"₩{item['price']:,}", inline=True)
        embed.add_field(name="type", value=item.get("type", "misc"), inline=True)
        if item.get("group"):
            embed.add_field(name="group", value=item["group"], inline=True)
        if item.get("type") == "album":
            embed.add_field(name="inclusion count", value=str(item.get("inclusion_count", 0)), inline=True)
        if item.get("image_url"):
            embed.set_thumbnail(url=item["image_url"])
        embed.set_footer(text="use /shop_buy to purchase this item.")

        if item.get("type") == "album":
            sorted_incs = sorted(item.get("inclusions", []), key=_inclusion_sort_key)
            pages = [sorted_incs[i:i+10] for i in range(0, len(sorted_incs), 10)] or [[]]
            inc_view = InclusionPaginatorView(item=item, pages=pages, invoker_id=interaction.user.id)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            await interaction.followup.send(embed=inc_view.build_embed(), view=inc_view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

class ShopCategorySelect(discord.ui.Select):
    def __init__(self, categories):
        super().__init__(placeholder="jump to category...", options=[discord.SelectOption(label=c, value=str(i)) for i, c in enumerate(categories)], custom_id="shop_cat_select")
    async def callback(self, interaction):
        self.view.cat_idx = int(self.values[0])
        self.view.page_idx = 0
        self.view._update_state()
        await interaction.response.edit_message(embed=self.view._get_embed(), view=self.view)

class ShopPaginationView(discord.ui.View):
    def __init__(self, pages: dict, user_id: int):
        super().__init__(timeout=300)
        self.pages = pages
        self.categories = list(pages.keys())
        self.cat_idx = 0
        self.page_idx = 0
        self.user_id = user_id
        
        self.prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, custom_id="shop_prev")
        self.prev_btn.callback = self.on_prev
        self.add_item(self.prev_btn)
        
        self.next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, custom_id="shop_next")
        self.next_btn.callback = self.on_next
        self.add_item(self.next_btn)
        
        if self.categories: self.add_item(ShopCategorySelect(self.categories))
            
        self.details_btn = discord.ui.Button(label="details", style=discord.ButtonStyle.primary, custom_id="shop_details")
        self.details_btn.callback = self.on_details
        self.add_item(self.details_btn)
        self._update_state()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ this is not your shop view.", ephemeral=True)
            return False
        return True

    def _update_state(self):
        if not self.categories:
            self.prev_btn.disabled, self.next_btn.disabled = True, True
            return
        self.prev_btn.disabled = (self.cat_idx == 0 and self.page_idx == 0)
        self.next_btn.disabled = (self.cat_idx == len(self.categories) - 1 and self.page_idx == len(self.pages[self.categories[-1]]) - 1)

    def _get_embed(self):
        if not self.categories: return discord.Embed(description="the shop is currently empty.")
        cat_name = self.categories[self.cat_idx]
        lines = [f"**{item['name']}** — ₩{item['price']:,}  •  {item.get('type', 'misc')}\n  ↳ id: `{item['id'].replace('id_', '')}`" for item in self.pages[cat_name][self.page_idx]]
        embed = discord.Embed(title=cat_name, description="\n".join(lines), color=discord.Color.green())
        embed.add_field(name="navigation", value=f"page {self.page_idx + 1} / {len(self.pages[cat_name])}", inline=False)
        return embed

    async def on_prev(self, interaction):
        self.page_idx -= 1
        if self.page_idx < 0:
            self.cat_idx -= 1
            self.page_idx = len(self.pages[self.categories[self.cat_idx]]) - 1
        self._update_state()
        await interaction.response.edit_message(embed=self._get_embed(), view=self)

    async def on_next(self, interaction):
        self.page_idx += 1
        if self.page_idx >= len(self.pages[self.categories[self.cat_idx]]):
            self.cat_idx += 1
            self.page_idx = 0
        self._update_state()
        await interaction.response.edit_message(embed=self._get_embed(), view=self)

    async def on_details(self, interaction): await interaction.response.send_modal(ShopDetailsModal())

@bot.tree.command(name="shop", description="browse the shop.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def shop_cmd(interaction: discord.Interaction):
    data = load_data()
    ensure_shop_keys(data)
    if not data["shop"]: return await interaction.response.send_message("the shop is currently empty.", ephemeral=True)
    pages = _build_shop_pages(data)
    view = ShopPaginationView(pages, interaction.user.id)
    await interaction.response.send_message(embed=view._get_embed(), view=view)

@bot.tree.command(name="shop_buy", description="purchase a shop item for one of your ocs.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(item_id="7-digit item number (e.g. 1234567)", oc_name="oc making the purchase", quantity="number of items to purchase")
async def shop_buy_cmd(interaction: discord.Interaction, item_id: str, oc_name: str, quantity: int = 1):
    data = load_data()
    ensure_shop_keys(data)
    normalized_id = f"id_{item_id.strip()}" if not item_id.strip().startswith("id_") else item_id.strip()
    await _handle_shop_buy(interaction, normalized_id, oc_name, quantity)


class InventoryPaginatorView(discord.ui.View):
    PAGE_SIZE = 8

    def __init__(self, entries: list[dict], oc_name: str, oc_pic_url: Optional[str], invoker_id: int):
        super().__init__(timeout=300)
        self.entries = entries
        self.oc_name = oc_name
        self.oc_pic_url = oc_pic_url
        self.invoker_id = invoker_id
        self.page = 0
        self.total_pages = max(1, math.ceil(len(entries) / self.PAGE_SIZE))
        self._sync_buttons()

    def _sync_buttons(self):
        if not self.entries:
            self.prev_btn.disabled = True
            self.next_btn.disabled = True
            return
        self.prev_btn.disabled = (self.page == 0)
        self.next_btn.disabled = (self.page >= self.total_pages - 1)

    def _build_embed(self) -> discord.Embed:
        page_entries = self.entries[self.page * self.PAGE_SIZE : (self.page + 1) * self.PAGE_SIZE]
        lines = []
        for entry in page_entries:
            lines.append(f"**{entry['name']}** (x{entry['count']})\n`{entry['display_id']}`")

        description = "\n\n".join(lines) if lines else "no items."

        embed = discord.Embed(
            title=f"{self.oc_name}'s inventory",
            description=description,
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"page {self.page + 1} of {self.total_pages}  •  {len(self.entries)} unique item(s)/inclusion(s)")
        if self.oc_pic_url:
            embed.set_thumbnail(url=self.oc_pic_url)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ this inventory belongs to someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="inv_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="inv_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

@bot.tree.command(name="inventory", description="view an oc's inventory.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(oc_name="oc name")
async def inventory_cmd(interaction: discord.Interaction, oc_name: str):
    data = load_data()
    ensure_shop_keys(data)
    oc_key_str = oc_key_of(oc_name)
    if oc_key_str not in data["ocs"]:
        return await interaction.response.send_message("❌ oc not found.", ephemeral=True)

    oc = data["ocs"][oc_key_str]
    inv = data["inventories"].get(oc_key_str, {})
    if not inv:
        return await interaction.response.send_message(
            f"**{oc['name']}**'s inventory is empty."
        )

    await interaction.response.defer()

    entry_map: dict[str, dict] = {}

    for item_id, instances in inv.items():
        if item_id.startswith("inc_"):
            if instances:
                inc_name = instances[0].get("name", item_id)
            else:
                inc_name = item_id
            existing = entry_map.get(item_id)
            if existing:
                existing["count"] += len(instances)
            else:
                entry_map[item_id] = {
                    "name": inc_name,
                    "display_id": item_id,
                    "count": len(instances),
                }
        else:
            shop_item = data["shop"].get(item_id)
            item_name = shop_item["name"] if shop_item else item_id
            entry_map[item_id] = {
                "name": item_name,
                "display_id": item_id,
                "count": len(instances),
            }

            if shop_item and shop_item.get("type") == "album":
                inclusion_counts: dict[str, dict] = {}
                for inst in instances:
                    for pulled in inst.get("pulled_inclusions", []):
                        inc_id = pulled.get("inclusion_id")
                        if not inc_id:
                            continue
                        if inc_id not in inclusion_counts:
                            inclusion_counts[inc_id] = {
                                "name": pulled.get("name", inc_id),
                                "count": 0,
                            }
                        inclusion_counts[inc_id]["count"] += 1

                for inc_id, inc_info in inclusion_counts.items():
                    existing = entry_map.get(inc_id)
                    if existing:
                        existing["count"] += inc_info["count"]
                    else:
                        entry_map[inc_id] = {
                            "name": inc_info["name"],
                            "display_id": inc_id,
                            "count": inc_info["count"],
                        }

    sorted_entries = sorted(entry_map.values(), key=lambda e: e["name"].lower())

    if not sorted_entries:
        return await interaction.followup.send(f"**{oc['name']}**'s inventory is empty.")

    view = InventoryPaginatorView(
        entries=sorted_entries,
        oc_name=oc["name"],
        oc_pic_url=oc.get("profile_picture"),
        invoker_id=interaction.user.id,
    )
    await interaction.followup.send(embed=view._build_embed(), view=view)


class AlbumViewerView(discord.ui.View):
    def __init__(
        self,
        inclusions: list[dict],
        album_name: str,
        oc_name: str,
        album_image_url: Optional[str],
        invoker_id: int,
    ):
        super().__init__(timeout=300)
        self.inclusions    = inclusions
        self.album_name    = album_name
        self.oc_name       = oc_name
        self.album_cover   = album_image_url
        self.invoker_id    = invoker_id
        self.index         = 0
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "❌ this album viewer belongs to someone else.", ephemeral=True
            )
            return False
        return True

    def _update_buttons(self) -> None:
        self.prev_btn.disabled = (self.index == 0)
        self.next_btn.disabled = (self.index == len(self.inclusions) - 1)

    def build_embed(self) -> discord.Embed:
        inc    = self.inclusions[self.index]
        total  = len(self.inclusions)
        page   = self.index + 1

        rarity_int = inc.get("rarity", 0)
        all_rarities = [i.get("rarity", 0) for i in self.inclusions]
        rarity_label = _rarity_label_proportional(rarity_int, all_rarities)
        
        if rarity_int == 1: color = discord.Color.light_grey()
        elif rarity_int == 2: color = discord.Color.green()
        elif rarity_int == 3: color = discord.Color.blue()
        elif rarity_int == 4: color = discord.Color.purple()
        elif rarity_int >= 5: color = discord.Color.gold()
        else: color = discord.Color.blurple()

        embed = discord.Embed(
            title=inc["name"],
            description=f"**album:** {self.album_name}\n**oc:** {self.oc_name}",
            color=color,
        )
        embed.add_field(name="rarity", value=rarity_label, inline=True)
        embed.add_field(name="from instance", value=f"#{inc['_inst_idx']}", inline=True)
        embed.add_field(name="acquired", value=inc["_acquired_at"] or "unknown", inline=True)

        img_url = inc.get("image_url") or self.album_cover
        if img_url:
            embed.set_image(url=img_url)

        embed.set_footer(text=f"inclusion {page} of {total}  •  common → rare")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="album_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="album_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

@bot.tree.command(name="open", description="[deprecated] use /shop_buy to open albums.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    oc_name="the oc who owns the album",
    album_name="name of the album to open (partial matchsupported)"
)
async def open_album_cmd(interaction: discord.Interaction, oc_name: str, album_name: str):
    if not is_dev(interaction):
        return await interaction.response.send_message(
            "⚠️ `/open` has been retired. your inclusions are now revealed automatically "
            "when you purchase an album via `/shop_buy`. use `/inventory` to see what you own.",
            ephemeral=True,
        )
    data = load_data()
    ensure_shop_keys(data)
    oc_key = oc_key_of(oc_name)
    if oc_key not in data["ocs"]:
        return await interaction.response.send_message("❌ oc not found.", ephemeral=True)
    oc = data["ocs"][oc_key]

    if not is_dev(interaction) and oc.get("owner_id") != interaction.user.id:
        return await interaction.response.send_message("❌ you do not own this oc.", ephemeral=True)

    inv = data["inventories"].get(oc_key, {})
    if not inv:
        return await interaction.response.send_message(f"❌ **{oc['name']}** has no items in their inventory.", ephemeral=True)

    album_name_lower = album_name.strip().lower()
    matching_album_ids = []
    for item_id in inv:
        shop_item = data["shop"].get(item_id)
        if shop_item and shop_item.get("type") == "album":
            if album_name_lower in shop_item["name"].lower():
                matching_album_ids.append(item_id)

    if not matching_album_ids:
        return await interaction.response.send_message(
            f"❌ **{oc['name']}** doesn't own an album matching **\"{album_name}\"**.", ephemeral=True
        )
    if len(matching_album_ids) > 1:
        names = ", ".join(f"**{data['shop'][aid]['name']}**" for aid in matching_album_ids)
        return await interaction.response.send_message(
            f"❌ multiple albums matched: {names}. please be more specific.", ephemeral=True
        )

    resolved_item_id = matching_album_ids[0]
    album_item = data["shop"][resolved_item_id]

    instances = inv.get(resolved_item_id, [])
    if not instances:
        return await interaction.response.send_message("❌ no instances of this album found.", ephemeral=True)

    all_pulled = []
    for inst_idx, inst in enumerate(instances):
        for pulled in inst.get("pulled_inclusions", []):
            all_pulled.append({
                **pulled,
                "_inst_idx": inst_idx,
                "_acquired_at": inst.get("acquired_at", "")[:10],
            })

    if not all_pulled:
        return await interaction.response.send_message(
            f"❌ **{album_item['name']}** has no inclusions to browse.", ephemeral=True
        )

    random.shuffle(all_pulled)
    all_pulled.sort(key=lambda x: x["rarity"])

    view = AlbumViewerView(
        inclusions    = all_pulled,
        album_name    = album_item["name"],
        oc_name       = oc["name"],
        album_image_url = album_item.get("image_url"),
        invoker_id    = interaction.user.id,
    )

    await interaction.response.send_message(
        embed=view.build_embed(),
        view=view,
        ephemeral=True,
    )

def _find_inclusion_instance(
    oc_inv: dict,
    inc_id: str,
    instance_index: int,
) -> tuple[Optional[dict], Optional[dict]]:
    if inc_id in oc_inv:
        instances = oc_inv[inc_id]
        if 0 <= instance_index < len(instances):
            return (instances[instance_index], {"type": "standalone", "inc_id": inc_id, "inst_idx": instance_index})

    occurrence = 0
    for album_id, album_instances in oc_inv.items():
        if not album_id.startswith("id_"):
            continue
        for album_inst_idx, album_inst in enumerate(album_instances):
            for pulled_idx, pulled in enumerate(album_inst.get("pulled_inclusions", [])):
                if pulled.get("inclusion_id") == inc_id:
                    if occurrence == instance_index:
                        return (
                            pulled,
                            {
                                "type": "nested",
                                "album_id": album_id,
                                "album_inst_idx": album_inst_idx,
                                "pulled_idx": pulled_idx,
                            },
                        )
                    occurrence += 1

    return (None, None)

def _count_inclusion_instances(oc_inv: dict, inc_id: str) -> int:
    total = 0
    if inc_id in oc_inv:
        total += len(oc_inv[inc_id])
    for album_id, album_instances in oc_inv.items():
        if not album_id.startswith("id_"):
            continue
        for album_inst in album_instances:
            for pulled in album_inst.get("pulled_inclusions", []):
                if pulled.get("inclusion_id") == inc_id:
                    total += 1
    return total

def _pop_n_inclusion_instances(d: dict, oc_key: str, inc_id: str, n: int) -> Optional[list[dict]]:
    oc_inv = d["inventories"].get(oc_key, {})
    popped = []

    standalone = oc_inv.get(inc_id, [])
    while standalone and len(popped) < n:
        popped.append(standalone.pop(0))
    if not standalone and inc_id in oc_inv:
        del oc_inv[inc_id]

    if len(popped) < n:
        for album_id in list(oc_inv.keys()):
            if not album_id.startswith("id_"):
                continue
            for album_inst in oc_inv[album_id]:
                pulled_list = album_inst.get("pulled_inclusions", [])
                i = 0
                while i < len(pulled_list) and len(popped) < n:
                    if pulled_list[i].get("inclusion_id") == inc_id:
                        popped.append(pulled_list.pop(i))
                    else:
                        i += 1
            if len(popped) >= n:
                break

    if len(popped) < n:
        return None
    return popped

def _deposit_inclusion_instances(d: dict, oc_key: str, inc_dicts: list[dict]) -> None:
    for inc_dict in inc_dicts:
        inc_id = inc_dict.get("inclusion_id") or "unknown"
        inc_dict.setdefault("acquired_at", now_iso())
        d["inventories"].setdefault(oc_key, {}).setdefault(inc_id, []).append(inc_dict)

def _inclusion_sell_price(rarity_int: int, all_rarities: list, album_price: int) -> int:
    if not all_rarities:
        return 100
    min_r = min(all_rarities)
    max_r = max(all_rarities)
    if max_r == min_r:
        score = 0.0
    else:
        score = (rarity_int - min_r) / (max_r - min_r)
    raw_price = album_price * (0.05 + 0.45 * (score ** 2))
    rounded = round(raw_price / 100) * 100
    return max(100, rounded)

class UnifiedTradeConfirmView(discord.ui.View):
    def __init__(
        self,
        init_uid: int, tgt_uid: int,
        init_oc_key: str, tgt_oc_key: str,
        offered_entries: list[dict],
        requested_entries: list[dict],
    ):
        super().__init__(timeout=None)
        self.init_uid = init_uid
        self.tgt_uid = tgt_uid
        self.init_oc_key = init_oc_key
        self.tgt_oc_key = tgt_oc_key
        self.offered_entries = offered_entries
        self.requested_entries = requested_entries

    @discord.ui.button(label="accept", style=discord.ButtonStyle.success, custom_id="unified_trade_accept")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.tgt_uid:
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        try:
            d = load_data()
            ensure_shop_keys(d)

            for entry in self.offered_entries:
                if entry["kind"] == "money":
                    bal = d["ocs"].get(self.init_oc_key, {}).get("balance", 0)
                    if bal < entry["qty"]:
                        return await interaction.response.edit_message(content=f"❌ trade failed: **{d['ocs'][self.init_oc_key]['name']}** only has ₩{bal:,} (needs ₩{entry['qty']:,}).", embed=None, view=None)
                elif entry["kind"] == "item":
                    if len(d["inventories"].get(self.init_oc_key, {}).get(entry["id"], [])) < entry["qty"]:
                        return await interaction.response.edit_message(content=f"❌ trade failed: offering oc no longer has {entry['qty']}× `{entry['id']}`.", embed=None, view=None)
                else:
                    if _count_inclusion_instances(d["inventories"].get(self.init_oc_key, {}), entry["id"]) < entry["qty"]:
                        return await interaction.response.edit_message(content=f"❌ trade failed: offering oc no longer has {entry['qty']}× `{entry['id']}`.", embed=None, view=None)

            for entry in self.requested_entries:
                if entry["kind"] == "money":
                    bal = d["ocs"].get(self.tgt_oc_key, {}).get("balance", 0)
                    if bal < entry["qty"]:
                        return await interaction.response.edit_message(content=f"❌ trade failed: **{d['ocs'][self.tgt_oc_key]['name']}** only has ₩{bal:,} (needs ₩{entry['qty']:,}).", embed=None, view=None)
                elif entry["kind"] == "item":
                    if len(d["inventories"].get(self.tgt_oc_key, {}).get(entry["id"], [])) < entry["qty"]:
                        return await interaction.response.edit_message(content=f"❌ trade failed: requested oc no longer has {entry['qty']}× `{entry['id']}`.", embed=None, view=None)
                else:
                    if _count_inclusion_instances(d["inventories"].get(self.tgt_oc_key, {}), entry["id"]) < entry["qty"]:
                        return await interaction.response.edit_message(content=f"❌ trade failed: requested oc no longer has {entry['qty']}× `{entry['id']}`.", embed=None, view=None)

            popped_offers = []
            for entry in self.offered_entries:
                if entry["kind"] == "money":
                    d["ocs"][self.init_oc_key]["balance"] -= entry["qty"]
                    popped_offers.append((entry, [{"amount": entry["qty"]}]))
                elif entry["kind"] == "item":
                    insts = [d["inventories"][self.init_oc_key][entry["id"]].pop(-1) for _ in range(entry["qty"])]
                    if not d["inventories"][self.init_oc_key][entry["id"]]:
                        del d["inventories"][self.init_oc_key][entry["id"]]
                    popped_offers.append((entry, insts))
                else:
                    insts = _pop_n_inclusion_instances(d, self.init_oc_key, entry["id"], entry["qty"])
                    popped_offers.append((entry, insts))

            popped_requests = []
            for entry in self.requested_entries:
                if entry["kind"] == "money":
                    d["ocs"][self.tgt_oc_key]["balance"] -= entry["qty"]
                    popped_requests.append((entry, [{"amount": entry["qty"]}]))
                elif entry["kind"] == "item":
                    insts = [d["inventories"][self.tgt_oc_key][entry["id"]].pop(-1) for _ in range(entry["qty"])]
                    if not d["inventories"][self.tgt_oc_key][entry["id"]]:
                        del d["inventories"][self.tgt_oc_key][entry["id"]]
                    popped_requests.append((entry, insts))
                else:
                    insts = _pop_n_inclusion_instances(d, self.tgt_oc_key, entry["id"], entry["qty"])
                    popped_requests.append((entry, insts))

            for entry, insts in popped_offers:
                if entry["kind"] == "money":
                    d["ocs"][self.tgt_oc_key]["balance"] = d["ocs"].get(self.tgt_oc_key, {}).get("balance", 0) + entry["qty"]
                elif entry["kind"] == "item":
                    for inst in insts:
                        d["inventories"].setdefault(self.tgt_oc_key, {}).setdefault(entry["id"], []).append(inst)
                else:
                    _deposit_inclusion_instances(d, self.tgt_oc_key, insts)

            for entry, insts in popped_requests:
                if entry["kind"] == "money":
                    d["ocs"][self.init_oc_key]["balance"] = d["ocs"].get(self.init_oc_key, {}).get("balance", 0) + entry["qty"]
                elif entry["kind"] == "item":
                    for inst in insts:
                        d["inventories"].setdefault(self.init_oc_key, {}).setdefault(entry["id"], []).append(inst)
                else:
                    _deposit_inclusion_instances(d, self.init_oc_key, insts)

            save_data(d)
            asyncio.ensure_future(push_backup_to_discord(d, reason="unified_trade"))

            await interaction.response.edit_message(content="✅ trade executed.", embed=None, view=None)

            init_user = bot.get_user(self.init_uid)
            if init_user:
                try:
                    tgt_name = d["ocs"].get(self.tgt_oc_key, {}).get("name", "the other oc")
                    await init_user.send(f"✅ your trade offer was **accepted** by **{tgt_name}**.")
                except Exception: pass

        except Exception as e:
            log.error(f"unified trade accept error: {e}")
            await interaction.response.edit_message(content="❌ an unexpected error occurred.", embed=None, view=None)

    @discord.ui.button(label="decline", style=discord.ButtonStyle.danger, custom_id="unified_trade_decline")
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.tgt_uid:
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        await interaction.response.edit_message(content="❌ trade declined.", embed=None, view=None)
        init_user = bot.get_user(self.init_uid)
        if init_user:
            try:
                tgt_name = (load_data())["ocs"].get(self.tgt_oc_key, {}).get("name", "the other oc")
                await init_user.send(f"❌ your trade offer was **declined** by **{tgt_name}**.")
            except Exception: pass

@bot.tree.command(name="trade", description="trade items, inclusions, or money (cross-type, supports one-sided).")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    your_oc="your oc making the offer",
    their_oc="the oc you are trading with",
    offer="comma-separated IDs to offer (or 'none')",
    offer_quantity="comma-separated quantities matching offer list (default '1')",
    request="comma-separated IDs to request (or 'none')",
    request_quantity="comma-separated quantities matching request list (default '1')",
    offer_type="type of offer: 'item', 'inclusion', or 'money' (default: auto-detect)",
    request_type="type of item to request: 'item', 'inclusion', or 'money' (default: auto-detect)",
)
@app_commands.choices(
    offer_type=[
        app_commands.Choice(name="item",      value="item"),
        app_commands.Choice(name="inclusion", value="inclusion"),
        app_commands.Choice(name="money",     value="money"),
    ],
    request_type=[
        app_commands.Choice(name="item",      value="item"),
        app_commands.Choice(name="inclusion", value="inclusion"),
        app_commands.Choice(name="money",     value="money"),
    ],
)
async def trade_cmd(
    interaction: discord.Interaction,
    your_oc: str,
    their_oc: str,
    offer: str = "",
    offer_quantity: str = "1",
    request: str = "",
    request_quantity: str = "1",
    offer_type: Optional[str] = None,
    request_type: Optional[str] = None,
):
    await interaction.response.defer(ephemeral=True)
    try:
        guild = resolve_guild(interaction)
        d = load_data()
        ensure_shop_keys(d)

        y_key, t_key = oc_key_of(your_oc), oc_key_of(their_oc)
        if y_key not in d["ocs"]: return await interaction.followup.send("❌ your oc not found.")
        if t_key not in d["ocs"]: return await interaction.followup.send("❌ target oc not found.")

        if not is_dev(interaction) and d["ocs"][y_key].get("owner_id") != interaction.user.id:
            return await interaction.followup.send("❌ you don't own the offering oc.")

        def parse_entries(id_str: str, qty_str: str, oc_inv: dict, type_hint: Optional[str] = None):
            id_str = id_str.strip().lower()
            if not id_str or id_str == "none":
                return [], None

            ids = [i.strip() for i in id_str.split(",") if i.strip()]
            qtys = [q.strip() for q in qty_str.split(",") if q.strip()]

            entries = []
            errors = []

            for idx, raw_id in enumerate(ids):
                qty = int(qtys[idx]) if idx < len(qtys) and qtys[idx].isdigit() else 1
                qty = max(1, qty)
                
                if raw_id in ("money", "₩", "won", "krw") or type_hint == "money":
                    amount = qty
                    if raw_id.isdigit():
                        amount = int(raw_id)
                    if amount < 1:
                        errors.append(f"money amount must be ≥ ₩1")
                        continue
                    entries.append({"kind": "money", "id": "money", "qty": amount, "name": f"₩{amount:,}"})
                    continue

                if type_hint == "item":
                    kind = "item"
                    resolved_id = _resolve_item_id(raw_id, d["shop"]) or raw_id
                elif type_hint == "inclusion":
                    kind = "inclusion"
                    resolved_id = raw_id if raw_id.startswith("inc_") else f"inc_{raw_id}"
                else:
                    if raw_id.startswith("inc_"):
                        kind = "inclusion"
                        resolved_id = raw_id
                    elif raw_id.startswith("id_"):
                        kind = "item"
                        resolved_id = raw_id
                    else:
                        r_item = _resolve_item_id(raw_id, d["shop"])
                        if r_item:
                            kind = "item"
                            resolved_id = r_item
                        else:
                            r_inc = f"inc_{raw_id}"
                            kind = "inclusion"
                            resolved_id = r_inc

                if kind == "item":
                    avail = len(oc_inv.get(resolved_id, []))
                    name = d["shop"].get(resolved_id, {}).get("name", resolved_id)
                else:
                    avail = _count_inclusion_instances(oc_inv, resolved_id)
                    inst, _ = _find_inclusion_instance(oc_inv, resolved_id, 0)
                    name = inst["name"] if inst else resolved_id

                if avail < qty:
                    errors.append(f"missing {qty}× `{raw_id}` (has {avail})")
                else:
                    entries.append({"kind": kind, "id": resolved_id, "qty": qty, "name": name})

            return entries, errors

        y_inv = d["inventories"].get(y_key, {})
        t_inv = d["inventories"].get(t_key, {})

        offered_entries, off_err = parse_entries(offer, offer_quantity, y_inv, type_hint=offer_type)
        requested_entries, req_err = parse_entries(request, request_quantity, t_inv, type_hint=request_type)

        if offer_type:
            mismatched = [e for e in offered_entries if e["kind"] != offer_type]
            if mismatched:
                names = ", ".join(e["id"] for e in mismatched)
                return await interaction.followup.send(
                    f"❌ you specified offer type `{offer_type}` but the following IDs could not be resolved as that type: {names}",
                    ephemeral=True,
                )
        if request_type:
            mismatched = [e for e in requested_entries if e["kind"] != request_type]
            if mismatched:
                names = ", ".join(e["id"] for e in mismatched)
                return await interaction.followup.send(
                    f"❌ you specified request type `{request_type}` but the following IDs could not be resolved as that type: {names}",
                    ephemeral=True,
                )

        if not offered_entries and not requested_entries:
            return await interaction.followup.send("❌ you must offer or request at least one item or amount.")

        all_errors = []
        if off_err: all_errors.extend([f"offer: {e}" for e in off_err])
        if req_err: all_errors.extend([f"request: {e}" for e in req_err])

        if all_errors:
            err_msg = "\n".join(f"• {e}" for e in all_errors)
            return await interaction.followup.send(f"❌ trade validation failed:\n{err_msg}")

        tgt_owner_id = d["ocs"][t_key].get("owner_id")
        if not tgt_owner_id: return await interaction.followup.send("❌ target oc owner missing.")

        embed = discord.Embed(
            title="🤝 unified trade offer",
            description=f"**{d['ocs'][y_key]['name']}** wants to trade with **{d['ocs'][t_key]['name']}**.",
            color=discord.Color.gold()
        )

        KIND_LABELS = {"item": "📦 item", "inclusion": "🃏 inclusion", "money": "💰 money"}

        off_lines = [
            f"• {KIND_LABELS.get(e['kind'], e['kind'])} — **{e['name']}**" + (f" × {e['qty']}  (`{e['id']}`)" if e["kind"] != "money" else "")
            for e in offered_entries
        ]
        req_lines = [
            f"• {KIND_LABELS.get(e['kind'], e['kind'])} — **{e['name']}**" + (f" × {e['qty']}  (`{e['id']}`)" if e["kind"] != "money" else "")
            for e in requested_entries
        ]

        embed.add_field(name="📤 they offer", value="\n".join(off_lines) if off_lines else "• nothing", inline=False)
        embed.add_field(name="📥 they request", value="\n".join(req_lines) if req_lines else "• nothing", inline=False)

        view = UnifiedTradeConfirmView(interaction.user.id, tgt_owner_id, y_key, t_key, offered_entries, requested_entries)

        tgt_member = guild.get_member(tgt_owner_id) if guild else None
        sent = False
        if tgt_member:
            try:
                await tgt_member.send(embed=embed, view=view)
                sent = True
            except: pass

        if not sent and guild:
            ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
            if ch:
                await ch.send(content=f"<@{tgt_owner_id}> you have a trade offer!", embed=embed, view=view)
                sent = True

        if sent:
            await interaction.followup.send("✅ trade offer sent.")
            off_ids = ",".join(e["name"] if e["kind"] == "money" else f"{e['id']}x{e['qty']}" for e in offered_entries)
            req_ids = ",".join(e["name"] if e["kind"] == "money" else f"{e['id']}x{e['qty']}" for e in requested_entries)
            await audit(guild, f"trade: {y_key} ↔ {t_key} [offered: {off_ids}] [requested: {req_ids}] by {interaction.user}")
        else:
            await interaction.followup.send("❌ failed to send offer.")

    except Exception as e:
        log.error(f"trade cmd error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.")


class TradeAddItemModal(discord.ui.Modal):
    item_id_input = discord.ui.TextInput(
        label="item id (7-digit number, no 'id_' prefix)",
        placeholder="e.g. 1234567",
        min_length=1, max_length=10,
    )
    instance_index_input = discord.ui.TextInput(
        label="instance index (0, 1, 2 ...)",
        placeholder="0",
        min_length=1, max_length=10,
    )

    def __init__(self, builder_view: "TradeMultiBuilderView", side: str):
        super().__init__(title=f"add {'offered' if side == 'offer' else 'requested'} item")
        self.builder_view = builder_view
        self.side = side

    async def on_submit(self, interaction: discord.Interaction):
        raw_id = self.item_id_input.value.strip().lower()

        if raw_id in ("money", "₩", "won", "krw") or (raw_id.isdigit()):
            amount_str = self.instance_index_input.value.strip() if raw_id in ("money", "₩", "won", "krw") else raw_id
            try:
                amount = int(amount_str)
            except ValueError:
                return await interaction.response.send_message("❌ invalid money amount.", ephemeral=True)
            if amount < 1:
                return await interaction.response.send_message("❌ amount must be ≥ ₩1.", ephemeral=True)
            entry = {
                "kind": "money", "item_id": "money", "inc_id": None,
                "instance_index": 0, "name": f"₩{amount:,}", "amount": amount,
            }
            if self.side == "offer":
                if len(self.builder_view.offered_items) >= 10:
                    return await interaction.response.send_message("❌ maximum 10 offered items.", ephemeral=True)
                self.builder_view.offered_items.append(entry)
            else:
                if len(self.builder_view.requested_items) >= 10:
                    return await interaction.response.send_message("❌ maximum 10 requested items.", ephemeral=True)
                self.builder_view.requested_items.append(entry)
            return await interaction.response.edit_message(
                embed=self.builder_view.build_summary_embed(), view=self.builder_view
            )

        if raw_id.startswith("inc_") or (not raw_id.startswith("id_") and len(raw_id) == 11 and raw_id[:4] == "inc_"):
            inc_id = raw_id if raw_id.startswith("inc_") else f"inc_{raw_id}"
            try:
                idx = int(self.instance_index_input.value.strip())
            except ValueError:
                return await interaction.response.send_message("❌ instance index must be an integer.", ephemeral=True)

            data = load_data()
            if self.side == "offer":
                inst, source = _find_inclusion_instance(
                    data["inventories"].get(self.builder_view.init_oc_key, {}), inc_id, idx
                )
                if inst is None:
                    return await interaction.response.send_message(
                        f"❌ **{self.builder_view.init_oc_name}** does not have inclusion `{inc_id}` at index {idx}.",
                        ephemeral=True,
                    )
                if any(o.get("inc_id") == inc_id and o.get("instance_index") == idx for o in self.builder_view.offered_items):
                    return await interaction.response.send_message("❌ that inclusion is already in your offer.", ephemeral=True)
                if len(self.builder_view.offered_items) >= 10:
                    return await interaction.response.send_message("❌ maximum 10 offered items per trade.", ephemeral=True)
                self.builder_view.offered_items.append({
                    "kind": "inclusion", "inc_id": inc_id, "item_id": None,
                    "instance_index": idx, "name": inst["name"], "source": source,
                })
            else:
                inst, source = _find_inclusion_instance(
                    data["inventories"].get(self.builder_view.tgt_oc_key, {}), inc_id, idx
                )
                if inst is None:
                    return await interaction.response.send_message(
                        f"❌ **{self.builder_view.tgt_oc_name}** does not have inclusion `{inc_id}` at index {idx}.",
                        ephemeral=True,
                    )
                if any(r.get("inc_id") == inc_id and r.get("instance_index") == idx for r in self.builder_view.requested_items):
                    return await interaction.response.send_message("❌ that inclusion is already in your request.", ephemeral=True)
                if len(self.builder_view.requested_items) >= 10:
                    return await interaction.response.send_message("❌ maximum 10 requested items per trade.", ephemeral=True)
                self.builder_view.requested_items.append({
                    "kind": "inclusion", "inc_id": inc_id, "item_id": None,
                    "instance_index": idx, "name": inst["name"], "source": source,
                })

            return await interaction.response.edit_message(
                embed=self.builder_view.build_summary_embed(), view=self.builder_view
            )

        item_id = f"id_{raw_id}" if not raw_id.startswith("id_") else raw_id
        try:
            idx = int(self.instance_index_input.value.strip())
        except ValueError:
            return await interaction.response.send_message("❌ instance index must be an integer.", ephemeral=True)

        data = load_data()
        item = data["shop"].get(item_id)
        if not item:
            return await interaction.response.send_message(f"❌ item `{item_id}` not found in shop.", ephemeral=True)

        if self.side == "offer":
            oc_inv = data["inventories"].get(self.builder_view.init_oc_key, {}).get(item_id, [])
            if idx < 0 or idx >= len(oc_inv):
                return await interaction.response.send_message(
                    f"❌ **{self.builder_view.init_oc_name}** does not have instance #{idx} of **{item['name']}**.", ephemeral=True
                )
            if any(o.get("item_id") == item_id and o.get("instance_index") == idx for o in self.builder_view.offered_items):
                return await interaction.response.send_message("❌ that instance is already in your offer.", ephemeral=True)
            if len(self.builder_view.offered_items) >= 10:
                return await interaction.response.send_message("❌ maximum 10 offered items per trade.", ephemeral=True)
            self.builder_view.offered_items.append({"kind": "item", "item_id": item_id, "instance_index": idx, "name": item["name"]})
        else:
            tgt_inv = data["inventories"].get(self.builder_view.tgt_oc_key, {}).get(item_id, [])
            if idx < 0 or idx >= len(tgt_inv):
                return await interaction.response.send_message(
                    f"❌ **{self.builder_view.tgt_oc_name}** does not have instance #{idx} of **{item['name']}**.", ephemeral=True
                )
            if any(r.get("item_id") == item_id and r.get("instance_index") == idx for r in self.builder_view.requested_items):
                return await interaction.response.send_message("❌ that instance is already in your request.", ephemeral=True)
            if len(self.builder_view.requested_items) >= 10:
                return await interaction.response.send_message("❌ maximum 10 requested items per trade.", ephemeral=True)
            self.builder_view.requested_items.append({"kind": "item", "item_id": item_id, "instance_index": idx, "name": item["name"]})

        await interaction.response.edit_message(embed=self.builder_view.build_summary_embed(), view=self.builder_view)


class MultiTradeConfirmView(discord.ui.View):
    def __init__(self, init_uid: int, tgt_uid: int,
                 init_oc_key: str, tgt_oc_key: str,
                 offered_items: list[dict], requested_items: list[dict]):
        super().__init__(timeout=None)
        self.init_uid = init_uid
        self.tgt_uid  = tgt_uid
        self.init_oc_key = init_oc_key
        self.tgt_oc_key  = tgt_oc_key
        self.offered_items   = offered_items
        self.requested_items = requested_items

    def _remove_inclusion(self, d: dict, oc_key: str, source: dict) -> Optional[dict]:
        oc_inv = d["inventories"].get(oc_key, {})
        if source["type"] == "standalone":
            instances = oc_inv.get(source["inc_id"], [])
            idx = source["inst_idx"]
            if idx >= len(instances): return None
            inst = instances.pop(idx)
            if not instances: del oc_inv[source["inc_id"]]
            return inst
        elif source["type"] == "nested":
            album_instances = oc_inv.get(source["album_id"], [])
            a_idx = source["album_inst_idx"]
            p_idx = source["pulled_idx"]
            if a_idx >= len(album_instances):
                return None
            pulled_list = album_instances[a_idx].get("pulled_inclusions", [])
            if p_idx >= len(pulled_list):
                return None
            return pulled_list.pop(p_idx)
        return None

    def _deposit_inclusion(self, d: dict, oc_key: str, inc_dict: dict) -> None:
        inc_id = inc_dict.get("inclusion_id") or inc_dict.get("inc_id", "unknown")
        inc_dict.setdefault("acquired_at", now_iso())
        d["inventories"].setdefault(oc_key, {}).setdefault(inc_id, []).append(inc_dict)

    @discord.ui.button(label="accept", style=discord.ButtonStyle.success, custom_id="mtrade_accept")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.tgt_uid:
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        try:
            d = load_data()
            for o in self.offered_items:
                if o.get("kind") == "money":
                    bal = d["ocs"].get(self.init_oc_key, {}).get("balance", 0)
                    if bal < o["amount"]:
                        return await interaction.response.edit_message(
                            content=f"❌ trade failed: **{d['ocs'].get(self.init_oc_key, {}).get('name', 'oc')}** only has ₩{bal:,} (needs ₩{o['amount']:,}).",
                            embed=None, view=None
                        )
                elif o.get("kind") == "inclusion":
                    inst, _ = _find_inclusion_instance(d["inventories"].get(self.init_oc_key, {}), o["inc_id"], o["instance_index"])
                    if inst is None:
                        return await interaction.response.edit_message(
                            content=f"❌ trade failed: **{o['name']}** inclusion instance #{o['instance_index']} from the offering oc no longer exists.",
                            embed=None, view=None
                        )
                else:
                    inv = d["inventories"].get(self.init_oc_key, {}).get(o["item_id"], [])
                    if o["instance_index"] >= len(inv):
                        return await interaction.response.edit_message(
                            content=f"❌ trade failed: **{o['name']}** instance #{o['instance_index']} from the offering oc no longer exists.",
                            embed=None, view=None
                        )
            for r in self.requested_items:
                if r.get("kind") == "money":
                    bal = d["ocs"].get(self.tgt_oc_key, {}).get("balance", 0)
                    if bal < r["amount"]:
                        return await interaction.response.edit_message(
                            content=f"❌ trade failed: **{d['ocs'].get(self.tgt_oc_key, {}).get('name', 'oc')}** only has ₩{bal:,} (needs ₩{r['amount']:,}).",
                            embed=None, view=None
                        )
                elif r.get("kind") == "inclusion":
                    inst, _ = _find_inclusion_instance(d["inventories"].get(self.tgt_oc_key, {}), r["inc_id"], r["instance_index"])
                    if inst is None:
                        return await interaction.response.edit_message(
                            content=f"❌ trade failed: **{r['name']}** inclusion instance #{r['instance_index']} from your oc no longer exists.",
                            embed=None, view=None
                        )
                else:
                    inv = d["inventories"].get(self.tgt_oc_key, {}).get(r["item_id"], [])
                    if r["instance_index"] >= len(inv):
                        return await interaction.response.edit_message(
                            content=f"❌ trade failed: **{r['name']}** instance #{r['instance_index']} from your oc no longer exists.",
                            embed=None, view=None
                        )

            init_inc_sources = []
            for o in self.offered_items:
                if o.get("kind") == "inclusion":
                    _, src = _find_inclusion_instance(d["inventories"].get(self.init_oc_key, {}), o["inc_id"], o["instance_index"])
                    init_inc_sources.append(src)
                else:
                    init_inc_sources.append(None)
                    
            tgt_inc_sources = []
            for r in self.requested_items:
                if r.get("kind") == "inclusion":
                    _, src = _find_inclusion_instance(d["inventories"].get(self.tgt_oc_key, {}), r["inc_id"], r["instance_index"])
                    tgt_inc_sources.append(src)
                else:
                    tgt_inc_sources.append(None)

            def _pop_item_instances(oc_key: str, items: list[dict]) -> dict:
                from collections import defaultdict
                groups: dict[str, list[tuple[int, int]]] = defaultdict(list)
                for pos, item in enumerate(items):
                    if item.get("kind") == "item":
                        groups[item["item_id"]].append((pos, item["instance_index"]))

                popped = {}
                for item_id, entries in groups.items():
                    entries_sorted = sorted(entries, key=lambda x: x[1], reverse=True)
                    inv_list = d["inventories"][oc_key][item_id]
                    for pos, idx in entries_sorted:
                        popped[pos] = inv_list.pop(idx)
                    if not d["inventories"][oc_key][item_id]:
                        del d["inventories"][oc_key][item_id]
                if not d["inventories"].get(oc_key):
                    d["inventories"].pop(oc_key, None)
                return popped

            init_item_instances = _pop_item_instances(self.init_oc_key, self.offered_items)
            tgt_item_instances  = _pop_item_instances(self.tgt_oc_key,  self.requested_items)

            init_instances = []
            for i, o in enumerate(self.offered_items):
                if o.get("kind") == "money":
                    d["ocs"][self.init_oc_key]["balance"] -= o["amount"]
                    init_instances.append(None)
                elif o.get("kind") == "inclusion":
                    init_instances.append(self._remove_inclusion(d, self.init_oc_key, init_inc_sources[i]))
                else:
                    init_instances.append(init_item_instances[i])
                    
            tgt_instances = []
            for i, r in enumerate(self.requested_items):
                if r.get("kind") == "money":
                    d["ocs"][self.tgt_oc_key]["balance"] -= r["amount"]
                    tgt_instances.append(None)
                elif r.get("kind") == "inclusion":
                    tgt_instances.append(self._remove_inclusion(d, self.tgt_oc_key, tgt_inc_sources[i]))
                else:
                    tgt_instances.append(tgt_item_instances[i])

            for i, r in enumerate(self.requested_items):
                if r.get("kind") == "money":
                    d["ocs"][self.init_oc_key]["balance"] += r["amount"]
                elif r.get("kind") == "inclusion":
                    self._deposit_inclusion(d, self.init_oc_key, tgt_instances[i])
                else:
                    d["inventories"].setdefault(self.init_oc_key, {}).setdefault(r["item_id"], []).append(tgt_instances[i])

            for i, o in enumerate(self.offered_items):
                if o.get("kind") == "money":
                    d["ocs"][self.tgt_oc_key]["balance"] += o["amount"]
                elif o.get("kind") == "inclusion":
                    self._deposit_inclusion(d, self.tgt_oc_key, init_instances[i])
                else:
                    d["inventories"].setdefault(self.tgt_oc_key, {}).setdefault(o["item_id"], []).append(init_instances[i])

            save_data(d)
            asyncio.ensure_future(push_backup_to_discord(d, reason="multi_trade"))
            await interaction.response.edit_message(content="✅ multi-item trade executed.", embed=None, view=None)

            init_user = bot.get_user(self.init_uid)
            if init_user:
                try:
                    await init_user.send(f"✅ your multi-item trade with **{d['ocs'][self.tgt_oc_key]['name']}** was accepted.")
                except Exception:
                    pass

        except Exception as e:
            log.error(f"multi_trade accept error: {e}")
            await interaction.response.edit_message(content="❌ an unexpected error occurred.", embed=None, view=None)

    @discord.ui.button(label="decline", style=discord.ButtonStyle.danger, custom_id="mtrade_decline")
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.tgt_uid:
            return await interaction.response.send_message("❌ not for you.", ephemeral=True)
        await interaction.response.edit_message(content="❌ trade declined.", embed=None, view=None)
        init_user = bot.get_user(self.init_uid)
        if init_user:
            try:
                await init_user.send("❌ your multi-item trade offer was declined.")
            except Exception:
                pass


async def _send_multi_trade_offer(
    interaction: discord.Interaction,
    builder: "TradeMultiBuilderView"
) -> None:
    guild = resolve_guild(interaction)

    if not builder.offered_items and not builder.requested_items:
        return await interaction.response.send_message("❌ you must offer or request at least one item.", ephemeral=True)

    data = load_data()
    for o in builder.offered_items:
        if o.get("kind") == "money":
            pass
        elif o.get("kind") == "inclusion":
            inst, _ = _find_inclusion_instance(data["inventories"].get(builder.init_oc_key, {}), o["inc_id"], o["instance_index"])
            if inst is None:
                return await interaction.response.send_message(
                    f"❌ offered inclusion instance of **{o['name']}** no longer exists (index #{o['instance_index']}). please rebuild the offer.",
                    ephemeral=True
                )
        else:
            inv = data["inventories"].get(builder.init_oc_key, {}).get(o["item_id"], [])
            if o["instance_index"] >= len(inv):
                return await interaction.response.send_message(
                    f"❌ offered instance of **{o['name']}** no longer exists (index #{o['instance_index']}). please rebuild the offer.",
                    ephemeral=True
                )
    for r in builder.requested_items:
        if r.get("kind") == "money":
            pass
        elif r.get("kind") == "inclusion":
            inst, _ = _find_inclusion_instance(data["inventories"].get(builder.tgt_oc_key, {}), r["inc_id"], r["instance_index"])
            if inst is None:
                return await interaction.response.send_message(
                    f"❌ requested inclusion instance of **{r['name']}** no longer exists (index #{r['instance_index']}). please rebuild the offer.",
                    ephemeral=True
                )
        else:
            inv = data["inventories"].get(builder.tgt_oc_key, {}).get(r["item_id"], [])
            if r["instance_index"] >= len(inv):
                return await interaction.response.send_message(
                    f"❌ requested instance of **{r['name']}** no longer exists (index #{r['instance_index']}). please rebuild the offer.",
                    ephemeral=True
                )

    embed = discord.Embed(
        title="🤝 multi-item trade offer",
        description=(
            f"**{builder.init_oc_name}** wants to trade with **{builder.tgt_oc_name}**.\n"
            f"accept or decline below."
        ),
        color=discord.Color.gold()
    )
    offer_lines = []
    for o in builder.offered_items:
        if o.get("kind") == "money":
            offer_lines.append(f"• 💰 money — **{o['name']}**")
        elif o.get("kind") == "inclusion":
            offer_lines.append(f"• 🃏 inclusion — **{o['name']}** (`{o['inc_id']}`, inst #{o['instance_index']})")
        else:
            offer_lines.append(f"• 📦 item — **{o['name']}** (`{o['item_id'].replace('id_', '')}`, inst #{o['instance_index']})")
            
    req_lines = []
    for r in builder.requested_items:
        if r.get("kind") == "money":
            req_lines.append(f"• 💰 money — **{r['name']}**")
        elif r.get("kind") == "inclusion":
            req_lines.append(f"• 🃏 inclusion — **{r['name']}** (`{r['inc_id']}`, inst #{r['instance_index']})")
        else:
            req_lines.append(f"• 📦 item — **{r['name']}** (`{r['item_id'].replace('id_', '')}`, inst #{r['instance_index']})")
            
    embed.add_field(name="📤 they offer", value="\n".join(offer_lines) if offer_lines else "• nothing", inline=False)
    embed.add_field(name="📥 they request", value="\n".join(req_lines) if req_lines else "• nothing", inline=False)

    confirm_view = MultiTradeConfirmView(
        init_uid=interaction.user.id,
        tgt_uid=builder.tgt_owner_id,
        init_oc_key=builder.init_oc_key,
        tgt_oc_key=builder.tgt_oc_key,
        offered_items=list(builder.offered_items),
        requested_items=list(builder.requested_items),
    )

    tgt_member = guild.get_member(builder.tgt_owner_id) if guild else None
    sent = False
    if tgt_member:
        try:
            await tgt_member.send(embed=embed, view=confirm_view)
            sent = True
        except Exception:
            pass
    if not sent and guild:
        ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if ch:
            await ch.send(content=f"<@{builder.tgt_owner_id}> you have a multi-item trade offer!", embed=embed, view=confirm_view)
            sent = True

    if sent:
        await interaction.response.edit_message(content="✅ multi-item trade offer sent.", embed=None, view=None)
    else:
        await interaction.response.send_message("❌ could not reach the target oc's owner.", ephemeral=True)

class TradeMultiBuilderView(discord.ui.View):
    def __init__(self, invoker_id: int, init_oc_key: str, tgt_oc_key: str,
                 init_oc_name: str, tgt_oc_name: str, tgt_owner_id: int,
                 init_inventory: dict, shop_data: dict):
        super().__init__(timeout=600)
        self.invoker_id = invoker_id
        self.init_oc_key = init_oc_key
        self.tgt_oc_key = tgt_oc_key
        self.init_oc_name = init_oc_name
        self.tgt_oc_name = tgt_oc_name
        self.tgt_owner_id = tgt_owner_id
        self.init_inventory = init_inventory
        self.shop_data = shop_data
        self.offered_items: list[dict] = []
        self.requested_items: list[dict] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ not your trade builder.", ephemeral=True)
            return False
        return True

    def build_summary_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🤝 trade builder — {self.init_oc_name} → {self.tgt_oc_name}",
            color=discord.Color.gold()
        )
        offer_lines = []
        for o in self.offered_items:
            if o.get("kind") == "money":
                offer_lines.append(f"• 💰 money — **{o['name']}**")
            elif o.get("kind") == "inclusion":
                offer_lines.append(f"• 🃏 inclusion — **{o['name']}** (`{o['inc_id']}`, inst #{o['instance_index']})")
            else:
                offer_lines.append(f"• 📦 item — **{o['name']}** (`{o['item_id'].replace('id_', '')}`, inst #{o['instance_index']})")
        if not offer_lines:
            offer_lines = ["*nothing added yet*"]
            
        request_lines = []
        for r in self.requested_items:
            if r.get("kind") == "money":
                request_lines.append(f"• 💰 money — **{r['name']}**")
            elif r.get("kind") == "inclusion":
                request_lines.append(f"• 🃏 inclusion — **{r['name']}** (`{r['inc_id']}`, inst #{r['instance_index']})")
            else:
                request_lines.append(f"• 📦 item — **{r['name']}** (`{r['item_id'].replace('id_', '')}`, inst #{r['instance_index']})")
        if not request_lines:
            request_lines = ["*nothing added yet*"]
            
        embed.add_field(name=f"📤 you offer ({len(self.offered_items)} item(s))", value="\n".join(offer_lines), inline=False)
        embed.add_field(name=f"📥 you request ({len(self.requested_items)} item(s))", value="\n".join(request_lines), inline=False)
        embed.set_footer(text="add items, then press 'send offer' when ready.")
        return embed

    @discord.ui.button(label="+ add offered item", style=discord.ButtonStyle.primary, custom_id="add_offer")
    async def add_offer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TradeAddItemModal(self, "offer"))

    @discord.ui.button(label="+ add requested item", style=discord.ButtonStyle.primary, custom_id="add_req")
    async def add_req(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TradeAddItemModal(self, "request"))

    @discord.ui.button(label="🗑 clear all", style=discord.ButtonStyle.secondary, custom_id="clear_all")
    async def clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.offered_items.clear()
        self.requested_items.clear()
        await interaction.response.edit_message(embed=self.build_summary_embed(), view=self)

    @discord.ui.button(label="📨 send offer", style=discord.ButtonStyle.success, custom_id="send_offer")
    async def send_offer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _send_multi_trade_offer(interaction, self)

@bot.tree.command(name="trade_multi", description="offer a multi-item trade from your oc to another oc.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    your_oc="your oc making the offer",
    their_oc="the oc you're trading with",
)
async def trade_multi_cmd(interaction: discord.Interaction, your_oc: str, their_oc: str):
    await interaction.response.defer(ephemeral=True)
    try:
        guild = resolve_guild(interaction)
        data = load_data()
        init_key = oc_key_of(your_oc)
        tgt_key = oc_key_of(their_oc)
        
        if init_key not in data["ocs"]:
            return await interaction.followup.send("❌ your oc not found.")
        if tgt_key not in data["ocs"]:
            return await interaction.followup.send("❌ target oc not found.")
        
        if not is_dev(interaction) and data["ocs"][init_key].get("owner_id") != interaction.user.id:
            return await interaction.followup.send("❌ you don't own the offering oc.")
        
        tgt_owner_id = data["ocs"][tgt_key].get("owner_id")
        if not tgt_owner_id:
            return await interaction.followup.send("❌ target oc owner missing.")
        
        builder_view = TradeMultiBuilderView(
            invoker_id=interaction.user.id,
            init_oc_key=init_key,
            tgt_oc_key=tgt_key,
            init_oc_name=data["ocs"][init_key]["name"],
            tgt_oc_name=data["ocs"][tgt_key]["name"],
            tgt_owner_id=tgt_owner_id,
            init_inventory=data["inventories"].get(init_key, {}),
            shop_data=data["shop"]
        )
        
        await interaction.followup.send(embed=builder_view.build_summary_embed(), view=builder_view, ephemeral=True)
    except Exception as e:
        log.error(f"trade_multi cmd error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

@bot.tree.command(name="gift", description="gift an item from one oc to another.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(from_oc="your oc", to_oc="recipient oc", item_id="7-digit item number (e.g. 1234567)", instance_index="index to gift (0, 1, 2...)")
async def gift_cmd(interaction: discord.Interaction, from_oc: str, to_oc: str, item_id: str, instance_index: int):
    await interaction.response.defer()
    try:
        d = load_data()
        f_key, t_key = oc_key_of(from_oc), oc_key_of(to_oc)
        item_id = f"id_{item_id.strip()}" if not item_id.strip().startswith("id_") else item_id.strip()
        
        if f_key not in d["ocs"]: return await interaction.followup.send("❌ your oc not found.")
        if t_key not in d["ocs"]: return await interaction.followup.send("❌ recipient oc not found.")
        
        if not is_dev(interaction) and d["ocs"][f_key].get("owner_id") != interaction.user.id:
            return await interaction.followup.send("❌ you don't own the sending oc.")
            
        f_inv = d["inventories"].get(f_key, {}).get(item_id, [])
        if instance_index < 0 or instance_index >= len(f_inv):
            return await interaction.followup.send("❌ invalid instance index.")
            
        inst = f_inv.pop(instance_index)
        if not f_inv: del d["inventories"][f_key][item_id]
            
        d["inventories"].setdefault(t_key, {}).setdefault(item_id, []).append(inst)
        save_data(d)
        asyncio.ensure_future(push_backup_to_discord(d, reason="gift"))
        
        item_name = d["shop"].get(item_id, {}).get("name", "an item")
        embed = discord.Embed(title="🎁 gift delivered!", description=f"**{d['ocs'][f_key]['name'].lower()}** gifted **{item_name.lower()}** to **{d['ocs'][t_key]['name'].lower()}**.", color=discord.Color.purple())
        await interaction.followup.send(embed=embed)
    except Exception as e:
        log.error(f"gift error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.")


@bot.tree.command(name="gift_money", description="gift ₩ from one oc to another.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    from_oc="your oc sending the money",
    to_oc="the recipient oc",
    amount="amount of ₩ to send (must be ≥ 1)",
)
async def gift_money_cmd(
    interaction: discord.Interaction,
    from_oc: str,
    to_oc: str,
    amount: int,
):
    await interaction.response.defer()
    try:
        if amount < 1:
            return await interaction.followup.send("❌ amount must be at least ₩1.")

        d = load_data()
        f_key = oc_key_of(from_oc)
        t_key = oc_key_of(to_oc)

        if f_key not in d["ocs"]:
            return await interaction.followup.send("❌ your oc not found.")
        if t_key not in d["ocs"]:
            return await interaction.followup.send("❌ recipient oc not found.")
        if f_key == t_key:
            return await interaction.followup.send("❌ an oc cannot gift money to themselves.")

        f_oc = d["ocs"][f_key]
        t_oc = d["ocs"][t_key]

        if not is_dev(interaction) and f_oc.get("owner_id") != interaction.user.id:
            return await interaction.followup.send("❌ you do not own the sending oc.")

        if f_oc["balance"] < amount:
            return await interaction.followup.send(
                f"❌ **{f_oc['name'].lower()}** only has ₩{f_oc['balance']:,} "
                f"(you tried to send ₩{amount:,})."
            )

        confirm_embed = discord.Embed(
            title="💸 confirm money gift",
            description=(
                f"**from:** {f_oc['name'].lower()}\n"
                f"**to:** {t_oc['name'].lower()}\n"
                f"**amount:** ₩{amount:,}\n"
                f"**{f_oc['name'].lower()}'s balance after:** ₩{f_oc['balance'] - amount:,}"
            ),
            color=discord.Color.orange(),
        )

        if await wait_for_confirm(interaction, confirm_embed):
            dconf = load_data()
            fo = dconf["ocs"].get(f_key)
            to = dconf["ocs"].get(t_key)
            if not fo or not to:
                return await interaction.followup.send("❌ oc data missing.", ephemeral=True)
            if fo["balance"] < amount:
                return await interaction.followup.send("❌ insufficient balance.", ephemeral=True)
            fo["balance"] -= amount
            to["balance"] += amount
            save_data(dconf)
            asyncio.ensure_future(push_backup_to_discord(dconf, reason="gift_money"))

            guild = resolve_guild(interaction)
            result_embed = discord.Embed(
                title="💸 money gifted!",
                description=(
                    f"**{fo['name'].lower()}** sent ₩{amount:,} to **{to['name'].lower()}**.\n"
                    f"**{fo['name'].lower()}'s** new balance: ₩{fo['balance']:,}\n"
                    f"**{to['name'].lower()}'s** new balance: ₩{to['balance']:,}"
                ),
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=result_embed, ephemeral=True)
            await audit(guild, f"gift_money: {fo['name'].lower()} → {to['name'].lower()} ₩{amount:,} by {interaction.user}")
        else:
            await interaction.followup.send("❌ cancelled.", ephemeral=True)

    except Exception as e:
        log.error(f"gift_money error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.")


@bot.tree.command(name="sell", description="sell copies of an item from an oc's inventory.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    oc_name="the oc selling the item",
    item_id="7-digit item id (e.g. 1234567)",
    quantity="number of copies to sell (must be ≥ 1, max = how many you own)"
)
async def sell_cmd(interaction: discord.Interaction, oc_name: str, item_id: str, quantity: int):
    await interaction.response.defer(ephemeral=True)
    try:
        if quantity < 1:
            return await interaction.followup.send("❌ quantity must be at least 1.", ephemeral=True)

        data = load_data()
        ensure_shop_keys(data)
        guild = resolve_guild(interaction)

        oc_key = oc_key_of(oc_name)
        if oc_key not in data["ocs"]:
            return await interaction.followup.send("❌ oc not found.", ephemeral=True)

        oc = data["ocs"][oc_key]

        if not is_dev(interaction) and oc.get("owner_id") != interaction.user.id:
            return await interaction.followup.send("❌ you do not own this oc.", ephemeral=True)

        resolved_id = _resolve_item_id(item_id, data["shop"])
        shop_item = data["shop"].get(resolved_id) if resolved_id else None
        if not shop_item:
            return await interaction.followup.send(
                "❌ this item is no longer listed in the shop and cannot be valued for sale. "
                "use `/gift` or `/trade` to transfer it instead.",
                ephemeral=True
            )

        item_name = shop_item["name"]
        oc_inv = data["inventories"].get(oc_key, {})
        instances = oc_inv.get(resolved_id, [])
        available = len(instances)

        if available < quantity:
            return await interaction.followup.send(
                f"❌ {oc['name'].lower()} only has {available} cop{'y' if available == 1 else 'ies'} of {item_name} (you requested {quantity}).",
                ephemeral=True
            )

        base_price = shop_item["price"]
        if shop_item.get("type") == "album":
            discount = round(base_price * 0.30 / 100) * 100
            sell_price_per_unit = max(100, base_price - discount)
        else:
            sell_price_per_unit = base_price
        total_payout = sell_price_per_unit * quantity

        confirm_desc = (
            f"**item:** {item_name.lower()}\n"
            f"**quantity:** {quantity}\n"
            f"**sell price per copy:** ₩{sell_price_per_unit:,}\n"
            f"**total payout:** ₩{total_payout:,}\n"
            f"**seller:** {oc['name'].lower()}\n"
            f"**current balance:** ₩{oc['balance']:,}\n"
            f"**balance after sale:** ₩{oc['balance'] + total_payout:,}\n\n"
            f"this action cannot be undone."
        )
        confirm_embed = discord.Embed(
            title="💰 confirm sale",
            description=confirm_desc,
            color=discord.Color.orange()
        )
        if shop_item.get("type") == "album":
            confirm_embed.set_footer(text=f"albums are sold at 30% off the original price (₩{base_price:,} → ₩{sell_price_per_unit:,} per copy)")
        if shop_item.get("image_url"):
            confirm_embed.set_thumbnail(url=shop_item["image_url"])

        if await wait_for_confirm(interaction, confirm_embed):
            d = load_data()
            ensure_shop_keys(d)

            o = d["ocs"].get(oc_key)
            shop_i = d["shop"].get(resolved_id)
            inv = d["inventories"].get(oc_key, {}).get(resolved_id, [])

            if not o or not shop_i:
                return await interaction.followup.send("❌ sale failed: oc or item data is no longer valid.", ephemeral=True)
            if len(inv) < quantity:
                return await interaction.followup.send(
                    f"❌ sale failed: only {len(inv)} cop{'y' if len(inv)==1 else 'ies'} remain (you requested {quantity}).",
                    ephemeral=True
                )

            for _ in range(quantity):
                inv.pop()
            if not inv:
                del d["inventories"][oc_key][resolved_id]
                if not d["inventories"][oc_key]:
                    del d["inventories"][oc_key]

            o["balance"] += total_payout

            save_data(d)
            asyncio.ensure_future(push_backup_to_discord(d, reason="sell"))

            result_embed = discord.Embed(
                title="✅ item sold",
                description=(
                    f"**{o['name'].lower()}** sold {quantity}× **{shop_i['name'].lower()}** "
                    f"for ₩{sell_price_per_unit:,} each (₩{total_payout:,} total).\n"
                    f"new balance: ₩{o['balance']:,}"
                ),
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=result_embed, ephemeral=True)
            await audit(guild, f"sell: {o['name'].lower()} sold {quantity}× {shop_i['name'].lower()} (id: {resolved_id}) for ₩{total_payout:,} by {interaction.user}")
        else:
            await interaction.followup.send("❌ sale cancelled.", ephemeral=True)

    except Exception as e:
        log.error(f"sell cmd error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)


@bot.tree.command(name="sell_inclusion", description="sell an inclusion from an oc's inventory.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    oc_name="the oc selling the inclusion",
    inclusion_id="7-digit inclusion id (e.g. 1234567, without 'inc_' prefix)",
    quantity="number of copies to sell (must be ≥ 1, max = how many the oc owns)",
)
async def sell_inclusion_cmd(
    interaction: discord.Interaction,
    oc_name: str,
    inclusion_id: str,
    quantity: int = 1,
):
    await interaction.response.defer(ephemeral=True)
    try:
        if quantity < 1:
            return await interaction.followup.send("❌ quantity must be at least 1.", ephemeral=True)
        data = load_data()
        ensure_shop_keys(data)
        guild = resolve_guild(interaction)

        stripped = inclusion_id.strip()
        inc_id = f"inc_{stripped}" if not stripped.startswith("inc_") else stripped
        inc_name = inc_id

        oc_key = oc_key_of(oc_name)
        if oc_key not in data["ocs"]:
            return await interaction.followup.send("❌ oc not found.", ephemeral=True)

        oc = data["ocs"][oc_key]
        if not is_dev(interaction) and oc.get("owner_id") != interaction.user.id:
            return await interaction.followup.send("❌ you do not own this oc.", ephemeral=True)

        oc_inv = data["inventories"].get(oc_key, {})
        count = _count_inclusion_instances(oc_inv, inc_id)
        if count == 0:
            return await interaction.followup.send("❌ no instances of this inclusion found.", ephemeral=True)
        if quantity > count:
            return await interaction.followup.send(
                f"❌ {oc['name'].lower()} only has {count} cop{'y' if count == 1 else 'ies'} "
                f"of **{inc_name}** (you requested {quantity}).",
                ephemeral=True,
            )

        rarity_int = 0
        inc_name = inc_id
        album_price = 50_000
        all_rarities = [0]
        found_in_album = False
        for shop_item in data["shop"].values():
            if shop_item.get("type") != "album":
                continue
            for inc_def in shop_item.get("inclusions", []):
                if inc_def.get("inclusion_id") == inc_id:
                    rarity_int = inc_def.get("rarity", 0)
                    inc_name = inc_def.get("name", inc_id)
                    album_price = shop_item.get("price", 50_000)
                    all_rarities = [i.get("rarity", 0) for i in shop_item.get("inclusions", [])]
                    found_in_album = True
                    break
            if found_in_album:
                break

        if not found_in_album:
            all_rarities = [rarity_int]

        sell_price = _inclusion_sell_price(rarity_int, all_rarities, album_price)
        rarity_label = _rarity_label_proportional(rarity_int, all_rarities)
        total_payout = sell_price * quantity

        confirm_embed = discord.Embed(
            title="💰 confirm inclusion sale",
            description=(
                f"**inclusion:** {inc_name}\n"
                f"**inclusion id:** `{inc_id}`\n"
                f"**rarity:** {rarity_label}\n"
                f"**sell price per copy:** ₩{sell_price:,}\n"
                f"**quantity:** {quantity}\n"
                f"**total payout:** ₩{total_payout:,}\n"
                f"**copies owned:** {count}\n"
                f"**seller:** {oc['name'].lower()}\n"
                f"**current balance:** ₩{oc['balance']:,}\n"
                f"**balance after sale:** ₩{oc['balance'] + total_payout:,}\n\n"
                f"this action cannot be undone."
            ),
            color=discord.Color.orange()
        )

        if await wait_for_confirm(interaction, confirm_embed):
            d = load_data()
            ensure_shop_keys(d)
            o = d["ocs"].get(oc_key)
            if not o:
                return await interaction.followup.send("❌ sale failed: oc no longer exists.", ephemeral=True)

            popped = _pop_n_inclusion_instances(d, oc_key, inc_id, quantity)
            if popped is None:
                return await interaction.followup.send(
                    "❌ sale failed: not enough copies remain.", ephemeral=True
                )
            o["balance"] += total_payout
            await save_and_backup(d, reason="sell_inclusion")
            result_embed = discord.Embed(
                title="✅ inclusion sold",
                description=(
                    f"**{o['name'].lower()}** sold {quantity}× **{inc_name}** "
                    f"for ₩{sell_price:,} each (₩{total_payout:,} total).\n"
                    f"new balance: ₩{o['balance']:,}"
                ),
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=result_embed, ephemeral=True)
            await audit(
                guild,
                f"sell_inclusion: {o['name'].lower()} sold {quantity}× {inc_name} "
                f"({inc_id}) for ₩{total_payout:,} by {interaction.user}"
            )
        else:
            await interaction.followup.send("❌ sale cancelled.", ephemeral=True)

    except Exception as e:
        log.error(f"sell_inclusion cmd error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)


class DevInclusionPaginatorView(discord.ui.View):
    def __init__(
        self,
        item: dict,
        pages: list[list[dict]],
        invoker_id: int,
        normalized_id: str,
    ):
        super().__init__(timeout=300)
        self.item = item
        self.pages = pages
        self.index = 0
        self.invoker_id = invoker_id
        self.normalized_id = normalized_id
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "❌ this panel belongs to someone else.", ephemeral=True
            )
            return False
        return True

    def _sync_buttons(self) -> None:
        self.prev_btn.disabled = (self.index == 0)
        self.next_btn.disabled = (self.index >= len(self.pages) - 1)

    def build_dev_embed(self) -> discord.Embed:
        page = self.pages[self.index]
        all_rarities = [inc.get("rarity", 0) for p in self.pages for inc in p]
        embed = discord.Embed(
            title=f"[dev] {self.item['name'].lower()} — inclusions ({sum(len(p) for p in self.pages)} total)",
            description=f"album id: `{self.normalized_id}`  •  page {self.index + 1}/{len(self.pages)}",
            color=discord.Color.orange(),
        )
        for inc in page:
            embed.add_field(
                name=inc["name"].lower(),
                value=(
                    f"id: `{inc['inclusion_id']}`\n"
                    f"rarity: {_rarity_label_proportional(inc.get('rarity', 0), all_rarities)}\n"
                    f"image: {'✅' if inc.get('image_url') else '❌ none'}"
                ),
                inline=True,
            )
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="dev_inc_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_dev_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="dev_inc_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_dev_embed(), view=self)


@bot.tree.command(name="album_inclusions", description="dev | list all inclusions for an album, including their IDs.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(item_id="7-digit item number of the album (e.g. 1234567)")
async def album_inclusions_cmd(interaction: discord.Interaction, item_id: str):
    if not is_dev(interaction):
        return await interaction.response.send_message("❌ denied.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    data = load_data()
    ensure_shop_keys(data)

    normalized_id = _resolve_item_id(item_id, data["shop"])
    if not normalized_id:
        return await interaction.followup.send("❌ item not found.", ephemeral=True)

    item = data["shop"][normalized_id]
    if item.get("type") != "album":
        return await interaction.followup.send("❌ that item is not an album.", ephemeral=True)

    inclusions = item.get("inclusions", [])
    if not inclusions:
        return await interaction.followup.send(f"**{item['name'].lower()}** has no inclusions defined.", ephemeral=True)

    sorted_incs = sorted(inclusions, key=_inclusion_sort_key)
    INC_PAGE_SIZE = 10
    pages = [sorted_incs[i:i+INC_PAGE_SIZE] for i in range(0, len(sorted_incs), INC_PAGE_SIZE)]

    view = DevInclusionPaginatorView(item=item, pages=pages, invoker_id=interaction.user.id, normalized_id=normalized_id)
    await interaction.followup.send(embed=view.build_dev_embed(), view=view, ephemeral=True)


class InclusionsBrowserView(discord.ui.View):
    PAGE_SIZE = 1

    def __init__(
        self,
        inclusions: list[dict],
        album_name: str,
        album_image_url: Optional[str],
        invoker_id: int,
        album_price: int = 50_000,
    ):
        super().__init__(timeout=300)
        self.inclusions     = inclusions
        self.album_name     = album_name
        self.album_cover    = album_image_url
        self.invoker_id     = invoker_id
        self.album_price    = album_price
        self.index          = 0
        self._sync_buttons()

    def _sync_buttons(self):
        if not self.inclusions:
            self.prev_btn.disabled = True
            self.next_btn.disabled = True
            return
        self.prev_btn.disabled = (self.index == 0)
        self.next_btn.disabled = (self.index >= len(self.inclusions) - 1)

    def _build_embed(self) -> discord.Embed:
        inc   = self.inclusions[self.index]
        total = len(self.inclusions)
        page  = self.index + 1

        rarity_int   = inc.get("rarity", 0)
        all_rarities = [i.get("rarity", 0) for i in self.inclusions]
        rarity_label = _rarity_label_proportional(rarity_int, all_rarities)
        sell_price = _inclusion_sell_price(rarity_int, all_rarities, self.album_price)
        
        if rarity_int == 1: color = discord.Color.light_grey()
        elif rarity_int == 2: color = discord.Color.green()
        elif rarity_int == 3: color = discord.Color.blue()
        elif rarity_int == 4: color = discord.Color.purple()
        elif rarity_int >= 5: color = discord.Color.gold()
        else: color = discord.Color.blurple()

        embed = discord.Embed(
            title=inc["name"].lower(),
            description=f"**album:** {self.album_name}",
            color=color,
        )
        embed.add_field(name="rarity",        value=rarity_label,                  inline=True)
        embed.add_field(name="inclusion id",  value=f"`{inc['inclusion_id']}`",    inline=True)
        embed.add_field(name="sell price",    value=f"₩{sell_price:,}",            inline=True)
        embed.set_footer(text=f"inclusion {page} of {total}  •  common → rare")

        img_url = inc.get("image_url") or self.album_cover
        if img_url:
            embed.set_image(url=img_url)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="inclbr_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="inclbr_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


@bot.tree.command(name="inclusions", description="browse all inclusions for an album.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(album_name="album name or partial match")
async def inclusions_cmd(interaction: discord.Interaction, album_name: str):
    await interaction.response.defer()
    try:
        data = load_data()
        ensure_shop_keys(data)

        matches = [
            (item_id, item)
            for item_id, item in data["shop"].items()
            if item.get("type") == "album"
            and album_name.strip().lower() in item["name"].lower()
        ]

        if not matches:
            return await interaction.followup.send("❌ no album found matching that name.", ephemeral=True)

        if len(matches) > 1:
            names = ", ".join(f"**{item['name']}**" for _, item in matches)
            return await interaction.followup.send(
                f"❌ multiple albums matched: {names}. please be more specific.",
                ephemeral=True,
            )

        _, album_item = matches[0]
        raw_inclusions = album_item.get("inclusions", [])

        if not raw_inclusions:
            return await interaction.followup.send(
                f"**{album_item['name']}** has no inclusions registered yet.", ephemeral=True
            )

        sorted_incs = sorted(raw_inclusions, key=_inclusion_sort_key)

        view = InclusionsBrowserView(
            inclusions=sorted_incs,
            album_name=album_item["name"],
            album_image_url=album_item.get("image_url"),
            invoker_id=interaction.user.id,
            album_price=album_item.get("price", 50_000),
        )
        await interaction.followup.send(embed=view._build_embed(), view=view)

    except Exception as e:
        log.error(f"inclusions_cmd error: {e}")
        await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)

class AlbumCog(commands.GroupCog, group_name="album", group_description="album purchase tracking"):
    def __init__(self, bot):
        self.bot = bot
        
    @app_commands.command(name="list", description="browse all albums available in the shop with purchase totals.")
    async def album_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = load_data()
        ensure_shop_keys(data)

        shop_albums = [
            (item_id, item)
            for item_id, item in data["shop"].items()
            if item.get("type") == "album"
        ]

        if not shop_albums:
            return await interaction.followup.send(
                embed=get_embed("album list", "no albums are currently listed in the shop.", "neutral")
            )

        unified = _build_album_totals(data)

        def _total_for_item_safe(item_id: str) -> tuple[int, str]:
            item_name_lower = data["shop"][item_id].get("name", "").lower()
            title_to_uuid = {
                a.get("title", "").lower(): a_id
                for a_id, a in data.get("albums", {}).items()
            }
            uuid_key = title_to_uuid.get(item_name_lower)
            shop_key = f"shop:{item_id}"

            per_oc: dict[str, int] = {}
            for oc_key, oc_map in unified.items():
                count = max(oc_map.get(shop_key, 0), oc_map.get(uuid_key, 0) if uuid_key else 0)
                if count > 0:
                    per_oc[oc_key] = count

            total = sum(per_oc.values())

            if per_oc:
                top_oc_key, top_qty = max(per_oc.items(), key=lambda x: x[1])
                top_name = data.get("ocs", {}).get(top_oc_key, {}).get("name", "unknown").lower()
                top_str = f"{top_name} ({top_qty} cop{'y' if top_qty == 1 else 'ies'})"
            else:
                top_str = "—"

            return total, top_str

        album_rows = []
        for item_id, item in shop_albums:
            total, top_str = _total_for_item_safe(item_id)
            album_rows.append((item_id, item, total, top_str))
        album_rows.sort(key=lambda x: x[2], reverse=True)

        PAGE_SIZE = 5
        pages = []
        total_pages = max(1, math.ceil(len(album_rows) / PAGE_SIZE))
        for i in range(0, len(album_rows), PAGE_SIZE):
            chunk = album_rows[i:i + PAGE_SIZE]
            embed = get_embed(
                "album list",
                f"page {i // PAGE_SIZE + 1} of {total_pages}  •  {len(album_rows)} album(s) in shop"
            )
            for item_id, item, total, top_str in chunk:
                short_id = item_id.replace("id_", "")
                embed.add_field(
                    name=item["name"],
                    value=(
                        f"shop id: `{short_id}`\n"
                        f"price: ₩{item['price']:,}\n"
                        f"total purchased: {total} cop{'y' if total == 1 else 'ies'}\n"
                        f"top buyer: {top_str}"
                    ),
                    inline=False
                )
            pages.append(embed)

        view = RankingPaginationView(pages)
        await interaction.followup.send(embed=pages[0], view=view)

    @app_commands.command(name="stats", description="view album purchase stats for an oc")
    @app_commands.describe(
        oc_name="the oc to look up stats for",
        album_ref="album title or album ID"
    )
    async def album_stats(self, interaction: discord.Interaction, oc_name: str, album_ref: str = None):
        data = load_data()
        oc = find_oc(oc_name, data)
        if not oc:
            return await interaction.response.send_message(embed=get_embed("error", "oc not found.", "error"), ephemeral=True)
            
        purchases = [p for p in data.get("album_purchases", {}).values() if p["oc_id"] == oc["id"]]
            
        if album_ref:
            album = _resolve_album(album_ref, data)
            if not album:
                return await interaction.response.send_message(embed=get_embed("error", f"album '{album_ref.lower()}' not found.", "error"), ephemeral=True)
            
            album_purchases = [p for p in purchases if p["album_id"] == album["album_id"]]
            album_purchases.sort(key=lambda x: x["purchase_date"])
            
            log_total = sum(p["quantity"] for p in album_purchases)

            unified = _build_album_totals(data)
            oc_unified = unified.get(oc["id"], {})
            resolved_item_id = None
            album_title_lower = album.get("title", "").lower()
            for iid, si in data.get("shop", {}).items():
                if si.get("type") == "album" and si.get("name", "").lower() == album_title_lower:
                    resolved_item_id = iid
                    break
            inv_count = max(
                oc_unified.get(f"shop:{resolved_item_id}", 0) if resolved_item_id else 0,
                oc_unified.get(album["album_id"], 0)
            )
            total = max(log_total, inv_count)

            if total == 0 and not album_purchases:
                return await interaction.response.send_message(embed=get_embed("album stats", f"**{oc['name'].lower()}** has no purchases for **{album['title']}**.", "neutral"))
            
            all_album_purchases = [
                p for p in data.get("album_purchases", {}).values()
                if p["album_id"] == album["album_id"]
            ]
            album_grand_total = sum(p["quantity"] for p in all_album_purchases)
            pct = (total / max(album_grand_total, total) * 100) if max(album_grand_total, total) else 0.0
            
            oc_totals_for_album: dict[str, int] = {}
            for p in all_album_purchases:
                oc_totals_for_album[p["oc_id"]] = oc_totals_for_album.get(p["oc_id"], 0) + p["quantity"]
            top_oc_id, top_oc_count = max(oc_totals_for_album.items(), key=lambda x: x[1]) if oc_totals_for_album else (None, 0)
            top_oc_name = data.get("ocs", {}).get(top_oc_id, {}).get("name", "n/a").lower() if top_oc_id else "n/a"

            inv_note = f"\n*(+ {inv_count - log_total} from shop inventory)*" if inv_count > log_total else ""
            
            pages = []
            page_source = album_purchases if album_purchases else [None]
            for i in range(0, max(len(album_purchases), 1), 5):
                chunk = album_purchases[i:i+5]
                if i == 0:
                    embed = get_embed(
                        f"{oc['name'].lower()} — {album['title']}",
                        (
                            f"**total:** {total} cop{'y' if total == 1 else 'ies'} ({pct:.1f}% of all {album_grand_total} logged){inv_note}\n"
                            f"**top buyer:** {top_oc_name} ({top_oc_count} cop{'y' if top_oc_count == 1 else 'ies'})\n"
                            f"page {i//5 + 1}"
                        )
                    )
                else:
                    embed = get_embed(f"{oc['name'].lower()} — {album['title']}", f"total: {total} cop{'y' if total == 1 else 'ies'}\npage {i//5 + 1}")
                
                for p in chunk:
                    v_str = p.get('version') or "—"
                    n_str = p.get('note') or "—"
                    embed.add_field(name=f"date: {p['purchase_date']}", value=f"quantity: {p['quantity']}\nversion: {v_str.lower()}\nnote: {n_str.lower()}", inline=False)
                pages.append(embed)
            
            if not pages:
                return await interaction.response.send_message(embed=get_embed("album stats", f"**{oc['name'].lower()}** has no purchases for **{album['title']}**.", "neutral"))
            
            view = RankingPaginationView(pages)
            await interaction.response.send_message(embed=pages[0], view=view)
        else:
            totals_by_album = {}
            for p in purchases:
                a_id = p["album_id"]
                if a_id not in totals_by_album:
                    totals_by_album[a_id] = {"total": 0, "versions": {}}
                totals_by_album[a_id]["total"] += p["quantity"]
                v = p.get("version") or "unspecified"
                totals_by_album[a_id]["versions"][v.lower()] = totals_by_album[a_id]["versions"].get(v.lower(), 0) + p["quantity"]

            unified = _build_album_totals(data)
            oc_unified = unified.get(oc["id"], {})
            for a_key, inv_qty in oc_unified.items():
                if a_key in totals_by_album:
                    totals_by_album[a_key]["total"] = max(totals_by_album[a_key]["total"], inv_qty)
                else:
                    if a_key.startswith("shop:"):
                        item_id_part = a_key[5:]
                        display_name = data.get("shop", {}).get(item_id_part, {}).get("name", "unknown album")
                    else:
                        display_name = data.get("albums", {}).get(a_key, {}).get("title", "unknown album")
                    totals_by_album[a_key] = {"total": inv_qty, "versions": {}, "_display_name": display_name}

            if not totals_by_album:
                return await interaction.response.send_message(embed=get_embed("album stats", f"**{oc['name'].lower()}** has no album purchase records yet.", "neutral"))

            grand_total = sum(t["total"] for t in totals_by_album.values())
            
            embeds = []
            album_list = list(totals_by_album.items())
            for i in range(0, len(album_list), 5):
                chunk = album_list[i:i+5]
                embed = get_embed(f"{oc['name'].lower()} — all albums", f"grand total: {grand_total} cop{'y' if grand_total == 1 else 'ies'}\npage {i//5 + 1}")
                for a_id, stats in chunk:
                    if "_display_name" in stats:
                        a_title = stats["_display_name"]
                    else:
                        a_title = data.get("albums", {}).get(a_id, {}).get("title", "unknown")
                    v_lines = "\n".join(f"- {v}: {c}" for v, c in stats["versions"].items())
                    embed.add_field(name=f"{a_title} (total: {stats['total']})", value=v_lines or "—", inline=False)
                embeds.append(embed)
            
            view = RankingPaginationView(embeds)
            await interaction.response.send_message(embed=embeds[0], view=view)

    @app_commands.command(name="leaderboard", description="see who has bought the most copies of a specific album.")
    @app_commands.describe(album_ref="7-digit shop item id or album name (partial match ok)")
    async def album_leaderboard(self, interaction: discord.Interaction, album_ref: str):
        await interaction.response.defer()
        data = load_data()
        ensure_shop_keys(data)

        resolved_item_id: Optional[str] = None
        resolved_name: Optional[str] = None
        resolved_uuid_key: Optional[str] = None

        shop_candidate = _resolve_item_id(album_ref.strip(), data["shop"])
        if shop_candidate and data["shop"].get(shop_candidate, {}).get("type") == "album":
            resolved_item_id = shop_candidate
            resolved_name = data["shop"][shop_candidate]["name"]
            name_lower = resolved_name.lower()
            for a_id, a in data.get("albums", {}).items():
                if a.get("title", "").lower() == name_lower:
                    resolved_uuid_key = a_id
                    break

        if not resolved_item_id:
            needle = album_ref.strip().lower()
            name_matches = [
                (iid, item)
                for iid, item in data["shop"].items()
                if item.get("type") == "album" and needle in item["name"].lower()
            ]
            if len(name_matches) == 1:
                resolved_item_id = name_matches[0][0]
                resolved_name = name_matches[0][1]["name"]
                name_lower = resolved_name.lower()
                for a_id, a in data.get("albums", {}).items():
                    if a.get("title", "").lower() == name_lower:
                        resolved_uuid_key = a_id
                        break
            elif len(name_matches) > 1:
                names = ", ".join(f"**{item['name'].lower()}**" for _, item in name_matches)
                return await interaction.followup.send(
                    f"❌ multiple albums matched: {names}. use the 7-digit shop id to be specific.",
                    ephemeral=True
                )

        if not resolved_item_id:
            legacy_album = _resolve_album(album_ref, data)
            if legacy_album:
                resolved_uuid_key = legacy_album["album_id"]
                resolved_name = legacy_album.get("title", "unknown album")

        if not resolved_item_id and not resolved_uuid_key:
            return await interaction.followup.send(
                embed=get_embed("error", f"album `{album_ref}` not found in shop or records.", "error"),
                ephemeral=True
            )

        unified = _build_album_totals(data)
        shop_key = f"shop:{resolved_item_id}" if resolved_item_id else None

        per_oc: dict[str, int] = {}
        for oc_key, oc_map in unified.items():
            count = 0
            if shop_key:
                count = max(count, oc_map.get(shop_key, 0))
            if resolved_uuid_key:
                count = max(count, oc_map.get(resolved_uuid_key, 0))
            if count > 0:
                per_oc[oc_key] = count

        if not per_oc:
            return await interaction.followup.send(
                embed=get_embed(
                    "leaderboard",
                    f"no purchases found for **{resolved_name.lower()}** yet.",
                    "neutral"
                )
            )

        album_grand_total = sum(per_oc.values())
        sorted_ocs = sorted(per_oc.items(), key=lambda x: x[1], reverse=True)

        display_id = resolved_item_id.replace("id_", "") if resolved_item_id else resolved_uuid_key
        pages = []
        for i in range(0, len(sorted_ocs), 10):
            chunk = sorted_ocs[i:i + 10]
            page_num = i // 10 + 1
            total_pages = max(1, math.ceil(len(sorted_ocs) / 10))
            header = (
                f"**total copies:** {album_grand_total}\n"
                f"**shop id:** `{display_id}`\n"
                f"page {page_num} of {total_pages}"
            ) if i == 0 else f"page {page_num} of {total_pages}"
            embed = get_embed(f"leaderboard — {resolved_name}", header)
            lines = []
            for idx, (oc_id, total) in enumerate(chunk):
                oc_name = data.get("ocs", {}).get(oc_id, {}).get("name", "unknown oc").lower()
                share_pct = (total / album_grand_total * 100) if album_grand_total else 0.0
                lines.append(
                    f"**#{i + idx + 1}** · {oc_name} · {total} cop{'y' if total == 1 else 'ies'} ({share_pct:.1f}%)"
                )
            embed.description += "\n\n" + "\n".join(lines)
            pages.append(embed)

        view = RankingPaginationView(pages)
        await interaction.followup.send(embed=pages[0], view=view)

    @app_commands.command(name="global_leaderboard", description="overall leaderboard — total albums purchased across all albums, sourced from shop + logs")
    async def album_global_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = load_data()
        unified = _build_album_totals(data)

        oc_grand: dict[str, int] = {}
        for oc_key, album_map in unified.items():
            oc_grand[oc_key] = sum(album_map.values())

        if not oc_grand:
            return await interaction.followup.send(
                embed=get_embed("global leaderboard", "no album purchase data found.", "neutral")
            )

        grand_total = sum(oc_grand.values())
        sorted_ocs = sorted(oc_grand.items(), key=lambda x: x[1], reverse=True)

        pages = []
        for i in range(0, len(sorted_ocs), 10):
            chunk = sorted_ocs[i:i+10]
            header = (
                f"**overall copies across all albums: {grand_total}**\n"
                f"sources: shop inventories + purchase logs (max deduplication)\n"
                f"page {i // 10 + 1}"
            ) if i == 0 else f"page {i // 10 + 1}"
            embed = get_embed("🏆 global album leaderboard", header)
            lines = []
            for idx, (oc_id, total) in enumerate(chunk):
                oc_name = data.get("ocs", {}).get(oc_id, {}).get("name", "unknown oc").lower()
                share_pct = (total / grand_total * 100) if grand_total else 0.0
                lines.append(f"**#{i + idx + 1}** · {oc_name} · {total} cop{'y' if total == 1 else 'ies'} ({share_pct:.1f}%)")
            embed.description += "\n\n" + "\n".join(lines)
            pages.append(embed)

        view = RankingPaginationView(pages)
        await interaction.followup.send(embed=pages[0], view=view)

    @app_commands.command(name="archive", description="[dev] soft-delete an album (purchase history is preserved)")
    @app_commands.describe(album_ref="album title or album ID")
    @is_dev_dec()
    async def album_archive(self, interaction: discord.Interaction, album_ref: str):
        data = load_data()
        album = _resolve_album(album_ref, data)
        if not album:
            return await interaction.response.send_message(embed=get_embed("error", "album not found or already archived.", "error"), ephemeral=True)
        
        data["albums"][album["album_id"]]["active"] = False
        save_data(data)
        asyncio.ensure_future(push_backup_to_discord(data, reason="album_archive"))
        
        count = sum(1 for p in data.get("album_purchases", {}).values() if p["album_id"] == album["album_id"])
        await interaction.response.send_message(embed=get_embed("success", f"album **{album['title'].lower()}** has been archived. its {count} purchase record(s) are preserved but the album will no longer appear in active listings.", "success"), ephemeral=True)

@bot.tree.command(name="shop_export", description="dev | export all shop items (including album inclusions and image URLs) as a JSON file.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    category="optional: export only items in this category (case-insensitive). omit to export all.",
    include_images="if true, image_url fields are included as-is (cdn urls). default true.",
)
async def shop_export_cmd(
    interaction: discord.Interaction,
    category: Optional[str] = None,
    include_images: bool = True,
) -> None:
    if not is_dev(interaction):
        return await interaction.response.send_message("❌ denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    guild = resolve_guild(interaction)
    try:
        data = load_data()
        ensure_shop_keys(data)

        resolved_category: Optional[str] = None
        if category is not None:
            for cat in data["shop_categories"]:
                if cat.lower() == category.lower():
                    resolved_category = cat
                    break
            if resolved_category is None:
                return await interaction.followup.send("❌ category not found.", ephemeral=True)

        items = []
        for item in data["shop"].values():
            if item.get("type") not in PURCHASABLE_TYPES:
                continue
            if resolved_category is not None and item.get("category", "").lower() != resolved_category.lower():
                continue
            items.append(item)

        export_items = []
        for item in items:
            item_copy = {
                "id":              item.get("id"),
                "type":            item.get("type"),
                "name":            item.get("name"),
                "group":           item.get("group", ""),
                "price":           item.get("price"),
                "inclusion_count": item.get("inclusion_count"),
                "category":        item.get("category"),
                "inclusions":      [],
                "image_url":       item.get("image_url") if include_images else None,
                "image_msg_id":    item.get("image_msg_id") if include_images else None,
                "created_at":      item.get("created_at"),
            }
            for inc in item.get("inclusions", []):
                inc_copy = dict(inc)
                if not include_images:
                    inc_copy["image_url"] = None
                    inc_copy["image_msg_id"] = None
                item_copy["inclusions"].append(inc_copy)
            export_items.append(item_copy)

        export = {
            "export_version": 1,
            "exported_at":    now_iso(),
            "categories":     data["shop_categories"],
            "items":          export_items,
        }

        raw_bytes = json.dumps(export, indent=2, ensure_ascii=False).encode("utf-8")
        if len(raw_bytes) > 8 * 1024 * 1024:
            return await interaction.followup.send(
                "❌ export too large (> 8 MB). use the category filter to split it.", ephemeral=True
            )

        filename = f"shop_export_{now_utc().strftime('%Y%m%d_%H%M%S')}.json"
        file_obj = discord.File(io.BytesIO(raw_bytes), filename=filename)

        albums  = sum(1 for i in export_items if i.get("type") == "album")
        miscs   = sum(1 for i in export_items if i.get("type") == "misc")
        inc_tot = sum(len(i.get("inclusions", [])) for i in export_items)

        embed = discord.Embed(title="📦 shop export", color=discord.Color.green())
        embed.add_field(name="items exported",    value=str(len(export_items)), inline=True)
        embed.add_field(name="albums",            value=str(albums),            inline=True)
        embed.add_field(name="misc",              value=str(miscs),             inline=True)
        embed.add_field(name="inclusions total",  value=str(inc_tot),           inline=True)
        embed.add_field(name="category filter",   value=resolved_category or "all", inline=True)

        await interaction.followup.send(embed=embed, file=file_obj, ephemeral=True)
        await audit(guild, f"shop_export: {len(export_items)} items exported by {interaction.user}")
    except Exception as e:
        log.error("shop_export_cmd error: %s", e, exc_info=True)
        await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)


@bot.tree.command(name="shop_import", description="dev | import shop items from a previously exported JSON file. images are re-persisted automatically.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    file="the .json file produced by /shop_export",
    dry_run="if true, validate and preview what would be imported without writing any data. default false.",
    skip_existing="if true, silently skip items whose name already exists in the shop (same type). if false, fail on first conflict. default true.",
    reimport_images="if true, re-download and re-persist any image_url fields via the bot-assets channel. if false, keep URLs as-is. default true.",
)
async def shop_import_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    dry_run: bool = False,
    skip_existing: bool = True,
    reimport_images: bool = True,
) -> None:
    if not is_dev(interaction):
        return await interaction.response.send_message("❌ denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    guild = resolve_guild(interaction)
    try:
        if not file.filename.lower().endswith(".json"):
            return await interaction.followup.send("❌ attachment must be a .json file.", ephemeral=True)
        if file.size > 8 * 1024 * 1024:
            return await interaction.followup.send("❌ file too large.", ephemeral=True)

        raw = await file.read()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return await interaction.followup.send("❌ file is not valid JSON.", ephemeral=True)

        if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
            return await interaction.followup.send("❌ invalid export format (missing 'items' list).", ephemeral=True)

        warnings: list[str] = []
        if parsed.get("export_version") != 1:
            warnings.append(f"export_version is {parsed.get('export_version')!r}, expected 1 — proceed with caution.")

        data = load_data()
        ensure_shop_keys(data)

        errors:   list[str] = []
        skipped:  list[str] = []
        to_import: list[dict] = []

        for idx, raw_item in enumerate(parsed["items"]):
            label = raw_item.get("name") or f"item[{idx}]"

            if not isinstance(raw_item.get("name"), str) or not raw_item["name"].strip():
                errors.append(f"{label}: missing or empty 'name' field.")
                continue
            if raw_item.get("type") not in PURCHASABLE_TYPES:
                errors.append(f"{label}: 'type' must be one of {PURCHASABLE_TYPES}, got {raw_item.get('type')!r}.")
                continue
            if not isinstance(raw_item.get("price"), (int, float)) or int(raw_item["price"]) < 0:
                errors.append(f"{label}: 'price' must be a non-negative integer.")
                continue
            if raw_item["type"] == "album":
                if not isinstance(raw_item.get("inclusion_count"), (int, float)) or int(raw_item.get("inclusion_count", 0)) < 1:
                    errors.append(f"{label}: 'inclusion_count' must be a positive integer for albums.")
                    continue
                if not isinstance(raw_item.get("inclusions"), list):
                    errors.append(f"{label}: 'inclusions' must be a list for albums.")
                    continue
                inc_errors = []
                for i, inc in enumerate(raw_item["inclusions"]):
                    if not isinstance(inc.get("name"), str):
                        inc_errors.append(f"inclusion[{i}] missing 'name'")
                    if not isinstance(inc.get("rarity"), (int, float)) or int(inc.get("rarity", 0)) < 1:
                        inc_errors.append(f"inclusion[{i}] 'rarity' must be int ≥ 1")
                if inc_errors:
                    errors.append(f"{label}: " + "; ".join(inc_errors))
                    continue

            conflict = any(
                s.get("name", "").lower() == raw_item["name"].lower() and s.get("type") == raw_item["type"]
                for s in data["shop"].values()
            )
            if conflict:
                if skip_existing:
                    skipped.append(raw_item["name"])
                    continue
                else:
                    errors.append(f"{label}: name conflict — an item of type '{raw_item['type']}' with this name already exists.")
                    continue

            to_import.append(raw_item)

        if errors:
            err_text = "\n".join(errors)
            if len(err_text) > 4000:
                err_text = err_text[:3997] + "…"
            embed = discord.Embed(
                title="❌ shop import — validation errors",
                description=err_text,
                color=discord.Color.red(),
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        if dry_run:
            embed = discord.Embed(title="🔍 shop import dry run", color=discord.Color.gold())
            embed.add_field(name="would import", value=str(len(to_import)), inline=True)
            embed.add_field(name="would skip",   value=str(len(skipped)),  inline=True)
            embed.add_field(name="warnings",     value="\n".join(warnings) or "none", inline=False)
            skip_preview = "\n".join(skipped[:20]) + ("…" if len(skipped) > 20 else "")
            embed.add_field(name="skipped items", value=skip_preview or "none", inline=False)
            return await interaction.followup.send(embed=embed, ephemeral=True)

        if not to_import:
            embed = discord.Embed(
                title="ℹ️ shop import",
                description="nothing to import — all items were skipped or the file contained no valid items.",
                color=discord.Color.blue(),
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        asset_ch = None
        if reimport_images and bot.guilds:
            asset_ch = discord.utils.get(bot.guilds[0].text_channels, name=ASSET_CHANNEL_NAME)

        used_inc_ids_this_batch: list[str] = []
        all_existing_inc_ids: set[str] = set()
        for shop_item in data["shop"].values():
            for inc in shop_item.get("inclusions", []):
                if inc_id_val := inc.get("inclusion_id"):
                    all_existing_inc_ids.add(inc_id_val)

        async def _repersist_image(url: Optional[str], filename: str) -> tuple[Optional[str], Optional[int]]:
            if not url or not reimport_images:
                return url, None
            if not asset_ch:
                return url, None
            if _http_session is None or _http_session.closed:
                return url, None
            try:
                async with _http_session.get(url) as resp:
                    if resp.status == 200:
                        img_bytes = await resp.read()
                        file_obj = discord.File(io.BytesIO(img_bytes), filename=filename)
                        msg = await asset_ch.send(file=file_obj)
                        if msg.attachments:
                            return msg.attachments[0].url.split("?")[0], msg.id
            except Exception:
                pass
            return url, None

        imported_count = 0
        for raw_item in to_import:
            new_item_id = _gen_item_id(data["shop"], list(data.get("used_item_ids", [])))
            data.setdefault("used_item_ids", []).append(new_item_id)

            item_img_url   = raw_item.get("image_url")
            item_img_msg   = raw_item.get("image_msg_id")
            if reimport_images and item_img_url:
                new_url, new_msg = await _repersist_image(item_img_url, f"import_{new_item_id}.png")
                if new_url != item_img_url or new_msg is not None:
                    item_img_url, item_img_msg = new_url, new_msg
                elif new_url == item_img_url and new_msg is None and reimport_images:
                    warnings.append(f"item '{raw_item['name']}': image re-persist failed, kept original URL")

            resolved_cat: Optional[str] = None
            raw_cat = raw_item.get("category")
            if raw_cat:
                for cat in data["shop_categories"]:
                    if cat.lower() == raw_cat.lower():
                        resolved_cat = cat
                        break
                if resolved_cat is None:
                    warnings.append(f"item '{raw_item['name']}': category '{raw_cat}' not found in shop_categories, set to None.")

            new_record: dict = {
                "id":           new_item_id,
                "type":         raw_item["type"],
                "name":         raw_item["name"],
                "price":        int(raw_item["price"]),
                "category":     resolved_cat,
                "image_url":    item_img_url,
                "image_msg_id": item_img_msg,
                "created_at":   now_iso(),
            }
            if raw_item["type"] == "album":
                new_record["group"]           = raw_item.get("group", "")
                new_record["inclusion_count"] = int(raw_item["inclusion_count"])
                new_record["inclusions"]      = []

                for inc in raw_item.get("inclusions", []):
                    combined_used = all_existing_inc_ids | set(used_inc_ids_this_batch)
                    new_inc_id = _gen_inclusion_id(combined_used, [])
                    used_inc_ids_this_batch.append(new_inc_id)
                    all_existing_inc_ids.add(new_inc_id)
                    data.setdefault("used_inclusion_ids", []).append(new_inc_id)

                    inc_img_url = inc.get("image_url")
                    inc_img_msg = inc.get("image_msg_id")
                    if reimport_images and inc_img_url:
                        new_url, new_msg = await _repersist_image(inc_img_url, f"import_{new_inc_id}.png")
                        if new_url != inc_img_url or new_msg is not None:
                            inc_img_url, inc_img_msg = new_url, new_msg
                        elif new_url == inc_img_url and new_msg is None:
                            warnings.append(f"item '{raw_item['name']}' inclusion '{inc.get('name', new_inc_id)}': image re-persist failed, kept original URL")

                    new_record["inclusions"].append({
                        "inclusion_id": new_inc_id,
                        "name":         inc["name"],
                        "rarity":       int(inc["rarity"]),
                        "image_url":    inc_img_url,
                        "image_msg_id": inc_img_msg,
                    })

            data["shop"][new_item_id] = new_record
            imported_count += 1

        await save_and_backup(data, reason="shop_import")

        embed = discord.Embed(title="✅ shop import complete", color=discord.Color.green())
        embed.add_field(name="imported", value=str(imported_count),    inline=True)
        embed.add_field(name="skipped",  value=str(len(skipped)),      inline=True)
        embed.add_field(name="dry run",  value="no",                   inline=True)
        embed.add_field(name="warnings", value=("\n".join(warnings)[:1000] or "none"), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
        await audit(guild, f"shop_import: {imported_count} items imported, {len(skipped)} skipped by {interaction.user}")
    except Exception as e:
        log.error("shop_import_cmd error: %s", e, exc_info=True)
        await interaction.followup.send("❌ an unexpected error occurred.", ephemeral=True)


# ─── Refresh Assets Command ────────────────────────────────────────────────────
def _is_cdn_attachment(url: str) -> bool:
    return bool(url and "cdn.discordapp.com/attachments/" in url)

@bot.tree.command(
    name="refresh_assets",
    description="[Dev] Re-upload any expired CDN attachment URLs for OC pictures, IG posts, and Tweets."
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def refresh_assets_cmd(interaction: discord.Interaction):
    if not is_dev(interaction):
        return await interaction.response.send_message("❌ Only devs can use this command.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    refreshed = 0
    failed = 0

    # -- OC profile pictures --
    for oc_key, oc in data["ocs"].items():
        url = oc.get("profile_picture", "")
        if _is_cdn_attachment(url):
            try:
                async with _http_session.get(url) as resp:
                    if resp.status == 200:
                        file_obj = discord.File(io.BytesIO(await resp.read()), filename=f"oc_{oc_key}.png")
                        for guild in bot.guilds:
                            ch = discord.utils.get(guild.text_channels, name=ASSET_CHANNEL_NAME)
                            if ch:
                                msg = await ch.send(file=file_obj)
                                if msg.attachments:
                                    oc["profile_picture"] = msg.attachments[0].url.split("?")[0]
                                    oc["profile_picture_msg_id"] = msg.id
                                    refreshed += 1
                                    break
                    else: failed += 1
            except: failed += 1

    # -- Instagram post photos --
    for post_id, post in data.get("instagram", {}).items():
        new_media = []
        for m in post.get("media", []):
            if m["type"] != "image" or not _is_cdn_attachment(m["url"]):
                new_media.append(m)
                continue
            try:
                async with _http_session.get(m["url"]) as resp:
                    if resp.status == 200:
                        file_obj = discord.File(io.BytesIO(await resp.read()), filename=f"ig_{post_id}.png")
                        for guild in bot.guilds:
                            ch = discord.utils.get(guild.text_channels, name=ASSET_CHANNEL_NAME)
                            if ch:
                                msg = await ch.send(file=file_obj)
                                if msg.attachments:
                                    new_media.append({"url": msg.attachments[0].url.split("?")[0], "type": "image"})
                                    refreshed += 1
                                    break
                    else:
                        new_media.append(m)
                        failed += 1
            except:
                new_media.append(m)
                failed += 1
        post["media"] = new_media
        post["photos"] = [m["url"] for m in new_media if m["type"] == "image"]

    # -- Twitter media --
    for tweet_id, tweet in data.get("twitter", {}).items():
        new_media = []
        for m in tweet.get("media", []):
            if m["type"] != "image" or not _is_cdn_attachment(m["url"]):
                new_media.append(m)
                continue
            try:
                async with _http_session.get(m["url"]) as resp:
                    if resp.status == 200:
                        file_obj = discord.File(io.BytesIO(await resp.read()), filename=f"tw_{tweet_id}.png")
                        for guild in bot.guilds:
                            ch = discord.utils.get(guild.text_channels, name=ASSET_CHANNEL_NAME)
                            if ch:
                                msg = await ch.send(file=file_obj)
                                if msg.attachments:
                                    new_media.append({"url": msg.attachments[0].url.split("?")[0], "type": "image"})
                                    refreshed += 1
                                    break
                    else:
                        new_media.append(m)
                        failed += 1
            except:
                new_media.append(m)
                failed += 1
        tweet["media"] = new_media

    await save_and_backup(data, reason="refresh_assets")

    embed = discord.Embed(
        title="🔄 Asset Refresh Complete",
        color=discord.Color.green() if not failed else discord.Color.orange(),
        timestamp=now_utc()
    )
    embed.add_field(name="✅ Refreshed", value=str(refreshed), inline=True)
    embed.add_field(name="❌ Failed", value=str(failed), inline=True)
    embed.set_footer(text=f"Run by {interaction.user}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.event
async def on_close():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        log.info("on_close: aiohttp session closed cleanly.")


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
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
    if not os.path.exists(DATA_FILE): return
    try:
        data = load_data()
        await push_backup_to_discord(data, reason="EMERGENCY-SHUTDOWN")
        log.info("Emergency backup completed via push_backup_to_discord.")
    except Exception as e:
        log.error("Emergency backup failed: %s", e)


@bot.tree.command(name="startup", description="[Dev] Manually revive the bot: re-sync commands and restart task loops.")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def startup_cmd(interaction: discord.Interaction):
    guild = resolve_guild(interaction)
    if not guild: return await interaction.response.send_message("❌ Could not resolve server context.", ephemeral=True)

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
    for task_obj, label in [
        (check_birthdays, "check_birthdays"),
        (check_scheduled, "check_scheduled"),
        (check_reminders, "check_reminders"),
        (auto_backup_db, "auto_backup_db"),
        (run_weekly_evaluations, "run_weekly_evaluations"),
        (weverse_billing_loop, "weverse_billing_loop"),
    ]:
        if task_obj is run_weekly_evaluations and not task_obj.is_running():
            data = load_data()
            if data.get("evaluation_config", {}).get("running"):
                global _EVAL_SKIP_FIRST_TICK
                _EVAL_SKIP_FIRST_TICK = True
            task_obj.start()
            task_lines.append(f"🔄 `{label}` — restarted.")
        elif not task_obj.is_running():
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
    await audit(guild, f"[STARTUP] Manual startup executed by {interaction.user}")


# ─── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set. Add it in Render → Environment.")

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    webserver.keep_alive()
    bot.run(token, log_handler=None)