import discord
from discord.ext import tasks, commands
import aiohttp
import time
import json
import os
import logging
from logging.handlers import RotatingFileHandler
from pprint import pformat

# ---------------- FILES ----------------

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"
LOG_FILE = "bot.log"

# ---------------- LOGGING ----------------

logger = logging.getLogger("bftracker")
logger.setLevel(logging.INFO) # DEBUG or INFO

handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=5 * 1024 * 1024,
    backupCount=3
)

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

handler.setFormatter(formatter)
logger.addHandler(handler)

# ---------------- CONFIG ----------------

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

config = load_config()

TOKEN = config["token"]
CHANNEL_ID = config["channel_id"]
CHECK_INTERVAL = config["check_interval_minutes"] * 60
INACTIVITY_THRESHOLD = config["inactivity_threshold_minutes"] * 60

API_URL = (
    "https://api.gametools.network/bf6/stats/"
    "?categories=multiplayer"
    "&raw=false"
    "&format_values=true"
    "&name={username}"
    "&platform=pc"
    "&skip_battlelog=true"
)

# ---------------- BOT ----------------

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(
    command_prefix="!bf ",
    intents=intents,
    help_command=None
)

# ---------------- STATE ----------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

player_state = load_state()

for player in config["players"]:
    player_state.setdefault(player, {
        "seconds_played": None,
        "kills": None,
        "last_check": 0,
        "playing": False
    })

save_state(player_state)

# ---------------- API ----------------

async def fetch_raw_stats(session, username):
    url = API_URL.format(username=username)
    logger.debug(f"Fetching API data from {url}")
    async with session.get(url, timeout=30) as response:
        if response.status != 200:
            logger.error(f"API request failed for {username} with status {response.status}")
            return None
        return await response.json()

async def fetch_stats(session, username):
    data = await fetch_raw_stats(session, username)
    if not data:
        logger.error(f"Failed to fetch data for {username}")
        return None
    try:
        return {
            "secondsPlayed": data["secondsPlayed"],
            "kills": data["kills"]
        }
    except KeyError:
        logger.error(f"Malformed data for {username}: {data}")
        return None

# ---------------- LOOP ----------------

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_players():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        logger.error(f"Channel {CHANNEL_ID} not found")
        return

    async with aiohttp.ClientSession() as session:
        logger.info(f"Checking {len(config['players'])} players")
        for player in config["players"]:
            stats = await fetch_stats(session, player)
            if not stats:
                continue

            state = player_state[player]
            now = time.time()

            # Start tracking if first time
            if state["seconds_played"] is None:
                state["seconds_played"] = stats["secondsPlayed"]
                state["kills"] = stats["kills"]
                state["last_check"] = now
                state["playing"] = False
                logger.debug(f"Initialized state for {player}")
                continue
            
            was_playing = state["playing"]
            
            # Check if play time has changed
            if stats["secondsPlayed"] != state["seconds_played"]:

                # If player was not playing before, send notification
                if not was_playing:
                    logger.info(f"Activity detected for {player}")
                    await channel.send(f"🎮 **{player}** has played again!\n")
                else:
                    logger.debug(f"{player} is still playing")
                    session_kills = max(stats["kills"] - state["kills"], 0)
                    await channel.send(f"🎮 **{player}** is on a rampage, racking up {session_kills} kills!")

                # Update records
                state["last_check"] = now
                state["playing"] = True

            # Check if player stopped playing
            elif was_playing and (now - state["last_check"]) >= INACTIVITY_THRESHOLD:
                session_kills = max(stats["kills"] - state["kills"], 0)
                session_min = round( (stats["secondsPlayed"] - state["seconds_played"]) / 60, 0)

                logger.info(
                    f"Inactivity detected for {player} | "
                    f"Seconds played: {state['seconds_played']} -> {stats['secondsPlayed']} | "
                    f"Kills gained: {session_kills}"
                )

                # Update records
                state["seconds_played"] = stats["secondsPlayed"]
                state["kills"] = stats["kills"]
                state["playing"] = False
                
                await channel.send(f"🎮 **{player}** stopped playing after claiming 💀 **{session_kills}** souls in ⏱️ {session_min} minutes.")

                # f"⏱️ Time played: **{round(session_min, 0)} min**\n"
                # f"💀 Kills this session: **{max(session_kills, 0)}**"

        save_state(player_state)

# ---------------- COMMANDS ----------------

@bot.command(name="help")
async def help_command(ctx):
    logger.info(f"{ctx.author} used help")
    await ctx.send(
        "**🪖 Battlefield Tracker Bot – Commands**\n\n"
        "`!bf help` – Show this help message\n"
        "`!bf players` – List monitored players\n"
        "`!bf addplayer <EA username>` – Add a player *(Admin only)*\n"
        "`!bf removeplayer <EA username>` – Remove a player *(Admin only)*\n"
        "`!bf checkplayer <EA username>` – Show API stats for a player\n"
    )

@bot.command(name="players")
async def list_players(ctx):
    logger.info(f"{ctx.author} used players")
    if not config["players"]:
        await ctx.send("📭 No players are being monitored.")
        return

    await ctx.send(
        "**📋 Monitored Players**\n" +
        "\n".join(f"• {p}" for p in config["players"])
    )

@bot.command(name="addplayer")
@commands.has_permissions(administrator=True)
async def add_player(ctx, username: str):
    logger.info(f"{ctx.author} used addplayer for {username}")

    if username in config["players"]:
        await ctx.send(f"⚠️ **{username}** is already monitored.")
        return

    config["players"].append(username)
    save_config(config)

    player_state[username] = {
        "seconds_played": None,
        "kills": None,
        "last_check": 0,
        "playing": False
    }
    save_state(player_state)

    await ctx.send(f"✅ Now monitoring **{username}**")

@bot.command(name="removeplayer")
@commands.has_permissions(administrator=True)
async def remove_player(ctx, username: str):
    logger.info(f"{ctx.author} used removeplayer for {username}")

    if username not in config["players"]:
        await ctx.send(f"⚠️ **{username}** is not monitored.")
        return

    config["players"].remove(username)
    save_config(config)

    player_state.pop(username, None)
    save_state(player_state)

    await ctx.send(f"🗑️ Stopped monitoring **{username}**")

@bot.command(name="checkplayer")
async def check_player(ctx, username: str):
    logger.info(f"{ctx.author} used checkplayer for {username}")

    async with aiohttp.ClientSession() as session:
        data = await fetch_raw_stats(session, username)

    if not data:
        await ctx.send(f"⚠️ Failed to fetch data for **{username}**")
        return

    pretty = pformat(data, width=80)
    if len(pretty) > 1900:
        pretty = pretty[:1900] + "\n... (truncated)"

    await ctx.send(f"```json\n{pretty}\n```")

# ---------------- ERRORS ----------------

@bot.event
async def on_command_error(ctx, error):
    logger.error(f"Command error by {ctx.author}: {error}")
    await ctx.send("⚠️ An error occurred while processing the command.")

# ---------------- EVENTS ----------------

@bot.event
async def on_ready():
    logger.info(f"Bot logged in as {bot.user}")
    check_players.start()

bot.run(TOKEN)
