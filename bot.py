import asyncio
import contextlib
import importlib
import io
import json
import logging
import os
import random
import re
import tempfile
import time
import urllib.request
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Optional, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

wavelink = None
try:
    wavelink = importlib.import_module("wavelink")
except Exception:
    wavelink = None

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except Exception:
    spotipy = None


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
guild_id_value = (os.getenv("GUILD_ID") or "").strip()
GUILD_ID = int(guild_id_value) if guild_id_value.isdigit() else 0

DEFAULT_AUTOPLAY = os.getenv("DEFAULT_AUTOPLAY", "false").lower() in {"1", "true", "yes", "on"}
MAX_PLAYLIST_ITEMS = int(os.getenv("MAX_PLAYLIST_ITEMS", "50"))
IDLE_DISCONNECT_SECONDS = int(os.getenv("IDLE_DISCONNECT_SECONDS", "300"))
YTDL_WORKERS = int(os.getenv("YTDL_WORKERS", "4"))
OPUS_BITRATE = int(os.getenv("OPUS_BITRATE", "384"))
YOUTUBE_COOKIES_FILE = (os.getenv("YOUTUBE_COOKIES_FILE") or "").strip()
if not YOUTUBE_COOKIES_FILE:
    YOUTUBE_COOKIES_FILE = os.path.join(os.path.dirname(__file__), ".cookies.txt")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

LAVALINK_URL = os.getenv("LAVALINK_URL")
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT") or "2333")
LAVALINK_NAME = os.getenv("LAVALINK_NAME", "music-bot-lavalink")
LAVALINK_ENABLED = (
    os.getenv("ENABLE_LAVALINK", "false").strip().lower() in {"1", "true", "yes", "on"}
    and bool(LAVALINK_URL and LAVALINK_PASSWORD)
)
BENGALI_TTS_MODEL = "facebook/mms-tts-ben"
BENGALI_VOICE_NAME = "bn"
PLAYER_EMOJI_DIR = os.path.join(
    os.path.dirname(__file__), "assets", "emojis"
)
PLAYER_EMOJI_ASSETS = {
    "play": ("rh_play", "66137-blue-play.png"),
    "shuffle": ("rh_shuffle", "43888-blue-shuffle.png"),
    "music": ("rh_music", "721701-music.png"),
    "repeat": ("rh_repeat", "3943-blue-repeat.png"),
    "pause": ("rh_pause", "53672-pause.png"),
    "cd": ("rh_cd", "60263-cd.gif"),
}
FAVOURITES_FILE = os.path.join(os.path.dirname(__file__), "favourites.json")
PLAYLISTS_FILE = os.path.join(os.path.dirname(__file__), "playlists.json")
VOICE_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "voice_settings.json")

BOT_NAME = os.getenv("BOT_NAME", "Music Bot").strip() or "Music Bot"
BOT_AUTHOR = (os.getenv("BOT_AUTHOR") or "").strip()  # optional credit shown in /about
BOT_CREDIT_TEXT = os.getenv("BOT_CREDIT_TEXT", "A high-quality Discord music bot.")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("music-bot")
TTS_MODEL_CACHE = {}
TTS_SYNTHESIS_LOCK = Lock()
PLAYER_EMOJI_CACHE: dict[int, dict[str, discord.PartialEmoji]] = {}

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in .env")

spotify_client = None
if spotipy and SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    spotify_client = spotipy.Spotify(
        client_credentials_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
        )
    )


URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?(track|playlist|album)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)
SPOTIFY_URI_RE = re.compile(
    r"spotify:(track|playlist|album):([A-Za-z0-9]+)",
    re.IGNORECASE,
)

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".ogg", ".opus", ".flac", ".m4a", ".aac", ".webm", ".mp4"
}

YTDL_POOL = ThreadPoolExecutor(max_workers=YTDL_WORKERS)

BASE_YTDL_OPTIONS = {
    "format": "bestaudio[abr<=192]/bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "ignoreerrors": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "cachedir": False,
}
if os.path.isfile(YOUTUBE_COOKIES_FILE):
    BASE_YTDL_OPTIONS["cookiefile"] = YOUTUBE_COOKIES_FILE
    BASE_YTDL_OPTIONS["extractor_args"] = {
        "youtube": {"player_client": ["default", "web_embedded"]}
    }

STREAM_YTDL_OPTIONS = {
    **BASE_YTDL_OPTIONS,
    "noplaylist": True,
    "ignoreerrors": False,
}

FLAT_YTDL_OPTIONS = {
    **BASE_YTDL_OPTIONS,
    "extract_flat": "in_playlist",
    "skip_download": True,
}

FFMPEG_BEFORE_OPTIONS = (
    "-nostdin "
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_delay_max 5"
)

FFMPEG_OPTIONS = "-vn"


async def fetch_synced_lyrics(title: str, duration: Optional[int] = None):
    loop = asyncio.get_running_loop()
    clean_title = re.sub(
        r"\(.*?\)|\[.*?\]|official(?: music video| audio| video)?|lyrics?|hd|4k",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    clean_title = re.sub(r"\s+", " ", clean_title).strip(" -")
    queries = list(dict.fromkeys((title, clean_title)))

    def fetch():
        headers = {"User-Agent": "DiscordMusicBot/1.0"}
        plain_lyrics = None

        for search_title in queries:
            query = urlencode({"q": search_title})
            request = urllib.request.Request(
                f"https://lrclib.net/api/search?{query}",
                headers=headers,
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                results = json.loads(response.read().decode("utf-8"))

            for result in results:
                synced = result.get("syncedLyrics")
                if synced:
                    return synced
                if not plain_lyrics and result.get("plainLyrics"):
                    plain_lyrics = result["plainLyrics"]

        parts = re.split(r"\s+-\s+", clean_title, maxsplit=1)
        if len(parts) == 2:
            query = urlencode({"artist_name": parts[0], "track_name": parts[1]})
            request = urllib.request.Request(
                f"https://lrclib.net/api/get?{query}",
                headers=headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if result.get("syncedLyrics"):
                    return result["syncedLyrics"]
                if not plain_lyrics:
                    plain_lyrics = result.get("plainLyrics")
            except HTTPError as exc:
                if exc.code != 404:
                    raise

        if plain_lyrics:
            plain_lines = [line.strip() for line in plain_lyrics.splitlines() if line.strip()]
            interval = max(2.0, float(duration or 180) / max(len(plain_lines), 1))
            return "\n".join(
                f"[{int(index * interval // 60):02}:{index * interval % 60:05.2f}]{line}"
                for index, line in enumerate(plain_lines)
            )

        return None

    raw_lyrics = None
    for attempt in range(3):
        try:
            raw_lyrics = await loop.run_in_executor(None, fetch)
            break
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise RuntimeError("The lyrics service is temporarily unavailable.") from exc
            await asyncio.sleep(attempt + 1)
        except URLError as exc:
            if attempt == 2:
                raise RuntimeError("The lyrics service is temporarily unavailable.") from exc
            await asyncio.sleep(attempt + 1)

    if not raw_lyrics:
        return []

    lines = []
    for line in raw_lyrics.splitlines():
        match = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", line)
        if match:
            minutes, seconds, text = match.groups()
            lines.append((int(minutes) * 60 + float(seconds), text.strip()))

    return sorted(lines)


def is_url(text: str) -> bool:
    return bool(text and URL_RE.match(text.strip()))


def is_spotify_link(text: str) -> bool:
    if not text:
        return False
    return bool(SPOTIFY_URL_RE.search(text) or SPOTIFY_URI_RE.search(text))


def spotify_kind_id(text: str):
    m = SPOTIFY_URL_RE.search(text) or SPOTIFY_URI_RE.search(text)
    if not m:
        return None, None
    return m.group(1).lower(), m.group(2)


def trim(text: str, size: int = 80) -> str:
    if not text:
        return "Unknown"
    return text if len(text) <= size else text[: size - 1] + "�"


def escape(text: str) -> str:
    return discord.utils.escape_markdown(text or "Unknown")


async def log_interaction_error(
    interaction: discord.Interaction, error: Exception, context: str
):
    log.error(
        "%s: %s",
        context,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.delete_original_response()
    except discord.NotFound:
        pass
    except discord.HTTPException:
        log.exception("Could not clear failed interaction response")


async def report_view_error(
    interaction: discord.Interaction,
    error: Exception,
    context: str,
):
    log.error(
        "%s: %s",
        context,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
    message = "Something went wrong. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.exception("Could not report the component interaction error")


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "Unknown/live"

    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)

    if h:
        return f"{h}:{m:02}:{s:02}"
    return f"{m}:{s:02}"


def platform_name(info: dict, parent: Optional[dict] = None) -> str:
    key = (
        info.get("extractor_key")
        or info.get("ie_key")
        or info.get("extractor")
        or (parent or {}).get("extractor_key")
        or "Web"
    )

    key = str(key)
    if key.lower().startswith("youtube"):
        return "YouTube"
    if key.lower().startswith("soundcloud"):
        return "SoundCloud"
    return key


def best_webpage_url(info: dict) -> Optional[str]:
    if not info:
        return None

    for key in ("webpage_url", "original_url"):
        value = info.get(key)
        if value and str(value).startswith(("http://", "https://")):
            return value

    raw = info.get("url")
    if raw and str(raw).startswith(("http://", "https://")):
        return raw

    ie_key = str(info.get("ie_key") or info.get("extractor_key") or "").lower()
    video_id = info.get("id") or info.get("url")

    if video_id and "youtube" in ie_key and not str(video_id).startswith("http"):
        return f"https://www.youtube.com/watch?v={video_id}"

    return None


def history_key(text: Optional[str]) -> Optional[str]:
    if not text:
        return None

    text = text.lower().strip()
    text = re.sub(r"[?&]list=[^&]+", "", text)
    text = re.sub(r"[?&]index=\d+", "", text)
    text = re.sub(r"\s+", " ", text)

    return text[:250] if text else None


async def ytdl_extract(query: str, options: dict) -> dict:
    loop = asyncio.get_running_loop()

    def run():
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(query, download=False)

    return await loop.run_in_executor(YTDL_POOL, run)


def first_entry(info: Optional[dict]) -> Optional[dict]:
    if not info:
        return None

    entries = info.get("entries") if isinstance(info, dict) else None
    if entries:
        for entry in entries:
            if entry:
                return entry

    return info


@dataclass
class Track:
    title: str
    query: str
    requester_id: int
    requester_name: str
    webpage_url: Optional[str] = None
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    platform: str = "YouTube"
    is_file: bool = False
    source_id: Optional[str] = None
    autoplay: bool = False
    stream_headers: Optional[dict] = None
    temporary_file: Optional[str] = None


def track_from_info(
    info: dict,
    query: str,
    requester_id: int,
    requester_name: str,
    parent: Optional[dict] = None,
) -> Track:
    title = info.get("title") or info.get("fulltitle") or query
    webpage_url = best_webpage_url(info) or query if is_url(query) else best_webpage_url(info)
    duration = info.get("duration")
    thumbnail = info.get("thumbnail")
    platform = platform_name(info, parent)

    source_id = None
    if platform.lower() == "youtube":
        source_id = info.get("id")

    return Track(
        title=str(title),
        query=webpage_url or query,
        requester_id=requester_id,
        requester_name=requester_name,
        webpage_url=webpage_url,
        duration=duration,
        thumbnail=thumbnail,
        platform=platform,
        source_id=source_id,
    )


async def ytdlp_to_tracks(query: str, requester: discord.abc.User) -> List[Track]:
    query = query.strip().strip("<>")

    if not is_url(query):
        search_query = query if query.startswith("ytsearch") else f"ytsearch1:{query}"
        info = await ytdl_extract(search_query, STREAM_YTDL_OPTIONS)
        entry = first_entry(info)

        if not entry:
            raise RuntimeError("No results found.")

        return [
            track_from_info(
                entry,
                best_webpage_url(entry) or search_query,
                requester.id,
                requester.display_name,
                info,
            )
        ]

    info = await ytdl_extract(query, FLAT_YTDL_OPTIONS)

    if not info:
        raise RuntimeError("Could not read this URL.")

    entries = list(info.get("entries") or [])

    if entries and (info.get("_type") == "playlist" or len(entries) > 1):
        tracks = []

        for entry in entries:
            if not entry:
                continue

            if len(tracks) >= MAX_PLAYLIST_ITEMS:
                break

            url = best_webpage_url(entry) or query

            tracks.append(
                track_from_info(
                    entry,
                    url,
                    requester.id,
                    requester.display_name,
                    info,
                )
            )

        if not tracks:
            raise RuntimeError("No playable items found in that playlist.")

        return tracks

    if entries:
        entry = entries[0]
        return [
            track_from_info(
                entry,
                best_webpage_url(entry) or query,
                requester.id,
                requester.display_name,
                info,
            )
        ]

    return [
        track_from_info(
            info,
            best_webpage_url(info) or query,
            requester.id,
            requester.display_name,
            info,
        )
    ]


async def spotify_to_tracks(query: str, requester: discord.abc.User) -> List[Track]:
    if not spotify_client:
        raise RuntimeError(
            "Spotify support is not configured. Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to .env"
        )

    kind, sp_id = spotify_kind_id(query)
    if not kind or not sp_id:
        raise RuntimeError("Invalid Spotify link.")

    loop = asyncio.get_running_loop()

    def run_spotify():
        raw_tracks = []

        if kind == "track":
            raw_tracks.append(spotify_client.track(sp_id))

        elif kind == "playlist":
            results = spotify_client.playlist_items(
                sp_id,
                limit=100,
                additional_types=("track",),
            )

            while results and len(raw_tracks) < MAX_PLAYLIST_ITEMS:
                for item in results.get("items", []):
                    track = item.get("track")
                    if track and track.get("type") == "track":
                        raw_tracks.append(track)

                    if len(raw_tracks) >= MAX_PLAYLIST_ITEMS:
                        break

                if len(raw_tracks) >= MAX_PLAYLIST_ITEMS or not results.get("next"):
                    break

                results = spotify_client.next(results)

        elif kind == "album":
            results = spotify_client.album_tracks(sp_id, limit=50)

            while results and len(raw_tracks) < MAX_PLAYLIST_ITEMS:
                for track in results.get("items", []):
                    if track:
                        raw_tracks.append(track)

                    if len(raw_tracks) >= MAX_PLAYLIST_ITEMS:
                        break

                if len(raw_tracks) >= MAX_PLAYLIST_ITEMS or not results.get("next"):
                    break

                results = spotify_client.next(results)

        else:
            raise RuntimeError("Unsupported Spotify type.")

        return raw_tracks

    raw_tracks = await loop.run_in_executor(YTDL_POOL, run_spotify)

    tracks = []

    for item in raw_tracks:
        artists = ", ".join(a["name"] for a in item.get("artists", [])) or "Unknown Artist"
        title = item.get("name") or "Unknown Title"

        yt_query = f"ytsearch1:{artists} - {title} official audio"

        duration = item.get("duration_ms")
        if duration:
            duration = int(duration / 1000)

        tracks.append(
            Track(
                title=f"{artists} - {title}",
                query=yt_query,
                requester_id=requester.id,
                requester_name=requester.display_name,
                webpage_url=(item.get("external_urls") or {}).get("spotify"),
                duration=duration,
                platform="Spotify ? YouTube",
            )
        )

    if not tracks:
        raise RuntimeError("No Spotify tracks found.")

    return tracks


async def create_tracks(
    query: Optional[str],
    file: Optional[discord.Attachment],
    requester: discord.abc.User,
) -> List[Track]:
    tracks: List[Track] = []

    if file:
        filename = file.filename or "uploaded-audio"
        lower = filename.lower()
        ext_ok = any(lower.endswith(ext) for ext in AUDIO_EXTENSIONS)
        content_type_ok = file.content_type and (
            file.content_type.startswith("audio/")
            or file.content_type.startswith("video/")
        )

        if not ext_ok and not content_type_ok:
            raise RuntimeError("Please upload a valid audio/video file.")

        tracks.append(
            Track(
                title=filename,
                query=file.url,
                requester_id=requester.id,
                requester_name=requester.display_name,
                webpage_url=file.url,
                duration=None,
                platform="Discord File",
                is_file=True,
            )
        )

    if query:
        if is_spotify_link(query):
            tracks.extend(await spotify_to_tracks(query, requester))
        else:
            tracks.extend(await ytdlp_to_tracks(query, requester))

    return tracks


async def resolve_stream_url(track: Track) -> str:
    if track.is_file:
        return track.webpage_url or track.query

    query = track.query

    if not is_url(query) and not query.startswith(("ytsearch", "scsearch")):
        query = f"ytsearch1:{query}"

    is_youtube_source = "youtube" in track.platform.lower() or query.startswith("ytsearch")

    try:
        info = await ytdl_extract(query, STREAM_YTDL_OPTIONS)
        entry = first_entry(info)

        if not entry:
            raise RuntimeError("Could not resolve audio stream.")

        stream_url = entry.get("url")

        if not stream_url or not str(stream_url).startswith(("http://", "https://")):
            page_url = best_webpage_url(entry)
            if page_url:
                info = await ytdl_extract(page_url, STREAM_YTDL_OPTIONS)
                entry = first_entry(info)
                if entry:
                    stream_url = entry.get("url")

        if not stream_url:
            raise RuntimeError("No playable audio stream found.")
    except Exception as youtube_error:
        if not is_youtube_source:
            raise

        log.warning("YouTube resolution failed; trying SoundCloud for %s", track.title)
        try:
            fallback_info = await ytdl_extract(
                f"scsearch1:{track.title}",
                STREAM_YTDL_OPTIONS,
            )
        except Exception as fallback_error:
            raise RuntimeError(
                "YouTube blocked playback and the SoundCloud fallback failed."
            ) from fallback_error

        entry = first_entry(fallback_info)
        stream_url = entry.get("url") if entry else None
        if not stream_url or not str(stream_url).startswith(("http://", "https://")):
            raise RuntimeError(
                "YouTube blocked playback and no playable SoundCloud match was found."
            ) from youtube_error

        track.source_id = None

    track.title = entry.get("title") or track.title
    track.webpage_url = best_webpage_url(entry) or track.webpage_url
    track.duration = entry.get("duration") or track.duration
    track.thumbnail = entry.get("thumbnail") or track.thumbnail
    track.platform = platform_name(entry)
    track.stream_headers = entry.get("http_headers")

    if track.platform.lower() == "youtube":
        track.source_id = entry.get("id") or track.source_id

    return stream_url


async def make_audio_source(
    stream_url: str,
    stream_headers: Optional[dict] = None,
    bass_db: float = 0.0,
    treble_db: float = 0.0,
    seek_seconds: float = 0.0,
    bass_boost: int = 1,
    speed: float = 1.0,
    pitch: float = 1.0,
    reverb: bool = False,
):
    # PCM is required by PCMVolumeTransformer, which powers volume controls.
    # It also leaves the source compatible with the Lavalink-style EQ fallback.
    before_options = "" if os.path.isfile(stream_url) else FFMPEG_BEFORE_OPTIONS
    if seek_seconds > 0:
        before_options = f"-ss {seek_seconds:.2f} " + before_options

    if stream_headers:
        header_lines = "".join(
            f"{key}: {value}\r\n" for key, value in stream_headers.items()
        )
        before_options += f' -headers "{header_lines}"'

    filters = []
    if bass_db:
        filters.append(f"equalizer=f=100:t=q:w=1:g={bass_db:.1f}")
    if treble_db:
        filters.append(f"equalizer=f=4000:t=q:w=1:g={treble_db:.1f}")
    if bass_boost > 1:
        filters.append(f"equalizer=f=100:t=q:w=1:g={3 * (bass_boost - 1):.1f}")
    if pitch != 1.0:
        filters.extend(
            (
                "aresample=48000",
                f"asetrate=48000*{pitch:.3f}",
                "aresample=48000",
            )
        )
    if speed != 1.0 or pitch != 1.0:
        remaining_tempo = speed / pitch
        while remaining_tempo < 0.5:
            filters.append("atempo=0.5")
            remaining_tempo /= 0.5
        while remaining_tempo > 2.0:
            filters.append("atempo=2.0")
            remaining_tempo /= 2.0
        filters.append(f"atempo={remaining_tempo:.3f}")
    if reverb:
        filters.append("aecho=0.8:0.7:60|120:0.35|0.2")

    options = FFMPEG_OPTIONS
    if filters:
        options += f' -af "{",".join(filters)}"'

    stderr_capture = io.BytesIO()
    source = discord.FFmpegPCMAudio(
        stream_url,
        before_options=before_options,
        options=options,
        stderr=stderr_capture,
    )
    return source, stderr_capture


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        self.synced_once = False

    async def setup_hook(self):
        if wavelink and LAVALINK_ENABLED:
            try:
                if hasattr(wavelink, "Pool") and hasattr(wavelink.Pool, "connect"):
                    node_uri = (
                        LAVALINK_URL
                        if LAVALINK_URL.startswith(("http://", "https://"))
                        else f"http://{LAVALINK_URL}:{LAVALINK_PORT}"
                    )
                    node = wavelink.Node(
                        identifier=LAVALINK_NAME,
                        uri=node_uri,
                        password=LAVALINK_PASSWORD,
                        client=self,
                    )
                    await wavelink.Pool.connect(client=self, nodes=[node])
                elif hasattr(wavelink, "NodePool") and hasattr(wavelink.NodePool, "connect"):
                    await wavelink.NodePool.connect(
                        client=self,
                        nodes=[
                            wavelink.Node(
                                identifier=LAVALINK_NAME,
                                host=LAVALINK_URL,
                                port=LAVALINK_PORT,
                                password=LAVALINK_PASSWORD,
                            )
                        ],
                    )
                else:
                    log.warning("Installed Wavelink is unsupported for this bot; missing NodePool/Pool.connect")
                    return
                log.info("Connected to Lavalink node: %s", LAVALINK_URL)
            except Exception as exc:
                log.warning("Lavalink connection failed: %s", exc)

        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            log.info("Synced slash commands to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced global slash commands")

        asyncio.create_task(self.preload_bengali_tts_model())

    async def preload_bengali_tts_model(self):
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(YTDL_POOL, _load_bengali_tts_model)
            log.info("Bengali TTS model is ready")
        except Exception:
            log.exception(
                "Could not preload the Bengali TTS model; the first speech "
                "request will retry loading it"
            )

    async def ensure_player_emojis(self, guild: discord.Guild):
        emojis = {emoji.name: emoji for emoji in guild.emojis}
        bot_member = guild.me
        can_create = bool(
            bot_member and bot_member.guild_permissions.manage_emojis_and_stickers
        )
        resolved = {}

        for key, (emoji_name, filename) in PLAYER_EMOJI_ASSETS.items():
            emoji = emojis.get(emoji_name)
            asset_path = os.path.join(PLAYER_EMOJI_DIR, filename)
            if emoji is None and can_create and os.path.isfile(asset_path):
                try:
                    with open(asset_path, "rb") as asset_file:
                        emoji = await guild.create_custom_emoji(
                            name=emoji_name,
                            image=asset_file.read(),
                            reason=f"{BOT_NAME} player controls",
                        )
                except (discord.Forbidden, discord.HTTPException, OSError) as exc:
                    log.warning(
                        "Could not register player emoji %s in guild %s: %s",
                        emoji_name,
                        guild.id,
                        exc,
                    )
            if emoji is not None:
                resolved[key] = discord.PartialEmoji(
                    name=emoji.name,
                    id=emoji.id,
                    animated=emoji.animated,
                )

        PLAYER_EMOJI_CACHE[guild.id] = resolved
        if len(resolved) < len(PLAYER_EMOJI_ASSETS):
            log.info(
                "Player emojis available in guild %s: %s/%s. "
                "Grant Manage Emojis and Stickers to register all supplied icons.",
                guild.id,
                len(resolved),
                len(PLAYER_EMOJI_ASSETS),
            )

    async def on_guild_join(self, guild: discord.Guild):
        await self.ensure_player_emojis(guild)

    async def on_command_error(self, context, exception):
        if isinstance(exception, commands.CommandNotFound):
            return
        raise exception

    async def on_ready(self):
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"music | {BOT_NAME}",
            )
        )
        for guild in self.guilds:
            await self.ensure_player_emojis(guild)
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if not self.user or member.id != self.user.id:
            return

        player = players.get(member.guild.id)
        if not player:
            return

        # If we just moved/connected/disconnected ourselves on purpose (via a
        # command), skip the reaction entirely.
        # Also guard against re-entrant state updates triggered by the bot's own
        # disconnect sequence, which can otherwise send duplicate status messages.
        if player.expecting_voice_change or player.stopping:
            return

        # Kicked: Discord fully removed us from the channel.
        if before.channel is not None and after.channel is None:
            await player.handle_forced_voice_change(
                "Discord disconnected the bot from voice."
            )
            return

        # Dragged: someone moved us into a different channel.
        if (
            before.channel is not None
            and after.channel is not None
            and before.channel.id != after.channel.id
        ):
            await player.handle_forced_voice_change(
                "The bot was moved to another voice channel."
            )
            return


bot = MusicBot()
players = {}
guild_likes: dict = {}

class MusicPlayer:
    def __init__(self, bot_instance: MusicBot, guild: discord.Guild):
        self.bot = bot_instance
        self.guild = guild
        self.guild_id = guild.id

        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.current: Optional[Track] = None
        self.last_track: Optional[Track] = None

        self.autoplay = DEFAULT_AUTOPLAY
        self.voice: Optional[discord.VoiceClient] = None
        self.text_channel_id: Optional[int] = None

        self.next_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.stopping = False
        self.bass_db = 0.0
        self.treble_db = 0.0
        self.bass_boost = 1
        self.speed = 1.0
        self.pitch = 1.0
        self.reverb = False
        self.effect_preset = "Off"
        self.repeat_mode = "off"
        self.volume = 1.0
        self.shuffle_enabled = False
        self.started_at = 0.0
        self.progress_started_at = 0.0
        self.progress_offset = 0.0
        self.lyrics_task: Optional[asyncio.Task] = None
        self.lyrics_message = None
        self.now_playing_message = None

        # Set to True right before we deliberately connect/move/disconnect the
        # voice client ourselves, so on_voice_state_update can tell the
        # difference between "we did this" and "a human dragged/kicked us".
        self.expecting_voice_change = False

        self.history = deque(maxlen=120)
        self.play_history: deque[Track] = deque(maxlen=120)
        self._seek_to: Optional[float] = None
        self._replay_track: Optional[Track] = None
        self._resume_after_effect = False

    def has_lavalink(self) -> bool:
        return bool(wavelink and self.voice and isinstance(self.voice, wavelink.Player))

    def set_equalizer(self, bass_db: float = 0.0, treble_db: float = 0.0) -> bool:
        self.bass_db = bass_db
        self.treble_db = treble_db

        if self.has_lavalink():
            levels = [0.0] * 15
            for idx in (0, 1, 2, 3):
                levels[idx] = bass_db
            for idx in (11, 12, 13, 14):
                levels[idx] = treble_db

            if hasattr(wavelink, "Equalizer"):
                eq = wavelink.Equalizer(levels=levels)
                if hasattr(self.voice, "set_filter"):
                    self.voice.set_filter(eq)
                    return True

            if hasattr(self.voice, "filters"):
                self.voice.filters.equalizer = levels
                if hasattr(self.voice, "set_filter"):
                    self.voice.set_filter(self.voice.filters)
                    return True
        # The normal FFmpeg player applies these values when the next source starts.
        return bool(self.voice and self.voice.is_connected())

    def set_text_channel(self, channel_id: int):
        self.text_channel_id = channel_id

    async def stop_lyrics(self):
        task = self.lyrics_task
        self.lyrics_task = None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def start_lyrics(self, channel):
        if not self.current:
            raise RuntimeError("Nothing is playing.")

        track = self.current
        lyrics = await fetch_synced_lyrics(track.title, track.duration)
        if not lyrics:
            raise RuntimeError("No synced lyrics were found for this song.")

        await self.stop_lyrics()
        message = await channel.send("?? Loading lyrics...")
        self.lyrics_message = message
        self.lyrics_task = asyncio.create_task(
            self.update_lyrics(message, track, lyrics)
        )
        return message

    async def delete_track_messages(self):
        message = self.lyrics_message
        self.lyrics_message = None
        if message:
            with contextlib.suppress(discord.NotFound, discord.HTTPException):
                await message.delete()

    async def update_lyrics(self, message, track: Track, lyrics):
        try:
            line_index = 0
            while self.current is track and self.started_at:
                elapsed = time.monotonic() - self.started_at

                while (
                    line_index + 1 < len(lyrics)
                    and lyrics[line_index + 1][0] <= elapsed
                ):
                    line_index += 1

                text = lyrics[line_index][1] or "..."
                with contextlib.suppress(discord.NotFound, discord.HTTPException):
                    await message.edit(content=f"?? {text}")

                if line_index + 1 >= len(lyrics):
                    break

                delay = max(0.25, lyrics[line_index + 1][0] - elapsed)
                await asyncio.sleep(min(delay, 2.0))
        except asyncio.CancelledError:
            raise

    def queued_tracks(self) -> List[Track]:
        return list(self.queue._queue)

    def clear_queue(self) -> int:
        count = 0

        while True:
            try:
                track = self.queue.get_nowait()
                count += 1
                if track.temporary_file:
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(track.temporary_file)
            except asyncio.QueueEmpty:
                break

        return count

    async def enqueue_many(self, tracks: List[Track]):
        for track in tracks:
            await self.queue.put(track)

    def start(self):
        if not self.task or self.task.done():
            self.stopping = False
            self.task = asyncio.create_task(self.player_loop())

    async def _load_lavalink_track(self, track: Track):
        if not self.has_lavalink():
            return None

        query = (track.webpage_url or track.query or "").strip()
        if not query:
            return None

        try:
            node = wavelink.Pool.get_node(LAVALINK_NAME)
        except Exception:
            node = None

        try:
            result = await wavelink.Playable.search(query, node=node)
        except Exception:
            try:
                result = await wavelink.Playable.search(f"ytsearch:{query}", node=node)
            except Exception:
                return None

        if not result:
            return None

        if isinstance(result, list):
            return result[0] if result else None
        if hasattr(result, "tracks") and result.tracks:
            return result.tracks[0]
        if hasattr(result, "__getitem__"):
            try:
                return result[0]
            except Exception:
                return None
        return result

    async def _wait_for_lavalink_finish(self, track: Track):
        while (
            self.current is track
            and self.voice
            and self.voice.is_connected()
            and (self.voice.is_playing() or self.voice.is_paused())
        ):
            await asyncio.sleep(0.25)
        self.next_event.set()

    async def connect_to_member_voice(self, interaction: discord.Interaction):
        member = interaction.user

        if not isinstance(member, discord.Member):
            raise RuntimeError("This command can only be used inside a server.")

        if not member.voice or not member.voice.channel:
            raise RuntimeError("Join a voice channel first.")

        target_channel = member.voice.channel
        voice = interaction.guild.voice_client

        if voice and voice.is_connected():
            self.voice = voice

            if voice.channel.id != target_channel.id:
                if voice.is_playing() or voice.is_paused():
                    raise RuntimeError(
                        "I am already playing in another voice channel. "
                        "One bot can only join one voice channel per server."
                    )

                self.expecting_voice_change = True
                try:
                    if LAVALINK_ENABLED and wavelink:
                        self.voice = await target_channel.connect(cls=wavelink.Player, self_deaf=True)
                    else:
                        await voice.move_to(target_channel)
                finally:
                    self.expecting_voice_change = False

            return

        self.expecting_voice_change = True
        try:
            if LAVALINK_ENABLED and wavelink:
                self.voice = await target_channel.connect(cls=wavelink.Player, self_deaf=True)
            else:
                self.voice = await target_channel.connect(self_deaf=True)
        finally:
            self.expecting_voice_change = False

    def user_in_same_voice(self, user: discord.abc.User) -> bool:
        if not isinstance(user, discord.Member):
            return False

        if not self.voice or not self.voice.channel:
            return False

        return bool(user.voice and user.voice.channel and user.voice.channel.id == self.voice.channel.id)

    async def send_text(self, content=None, embed=None, view=None):
        channel = self.bot.get_channel(self.text_channel_id) if self.text_channel_id else None

        if channel:
            with contextlib.suppress(Exception):
                await channel.send(content=content, embed=embed, view=view)

    def remember(self, track: Track):
        if not self.play_history or self.play_history[-1] is not track:
            self.play_history.append(track)
        for value in (track.webpage_url, track.query, track.title):
            key = history_key(value)
            if key:
                self.history.append(key)

    def seen(self, url: Optional[str], title: Optional[str]) -> bool:
        url_key = history_key(url)
        title_key = history_key(title)
        return bool((url_key and url_key in self.history) or (title_key and title_key in self.history))

    def playback_position(self) -> float:
        if self.progress_started_at:
            return max(
                0.0,
                self.progress_offset + time.monotonic() - self.progress_started_at,
            )
        return self.progress_offset

    async def update_now_playing_progress(self, track: Track):
        try:
            while self.current is track:
                await asyncio.sleep(2)
                if self.current is track and self.voice and self.voice.is_playing():
                    await self.send_now_playing()
        except asyncio.CancelledError:
            raise

    async def make_autoplay_track(self) -> Optional[Track]:
        seed = self.last_track
        if not seed:
            return None

        requester_id = self.bot.user.id if self.bot.user else 0
        requester_name = "Autoplay"

        if seed.source_id:
            mix_url = f"https://www.youtube.com/watch?v={seed.source_id}&list=RD{seed.source_id}"

            try:
                info = await ytdl_extract(mix_url, FLAT_YTDL_OPTIONS)
                entries = list(info.get("entries") or [])

                for entry in entries[1:15]:
                    if not entry:
                        continue

                    url = best_webpage_url(entry)
                    title = entry.get("title") or "Autoplay song"

                    if url and not self.seen(url, title):
                        return Track(
                            title=title,
                            query=url,
                            requester_id=requester_id,
                            requester_name=requester_name,
                            webpage_url=url,
                            duration=entry.get("duration"),
                            thumbnail=entry.get("thumbnail"),
                            platform="YouTube Autoplay",
                            source_id=entry.get("id"),
                            autoplay=True,
                        )
            except Exception as e:
                log.warning("YouTube mix autoplay failed: %s", e)

        clean_title = re.sub(
            r"\(.*?\)|\[.*?\]|official|audio|video|lyrics|HD|4K",
            "",
            seed.title,
            flags=re.IGNORECASE,
        ).strip()

        if not clean_title:
            clean_title = seed.title

        try:
            info = await ytdl_extract(f"ytsearch10:{clean_title} similar songs", FLAT_YTDL_OPTIONS)
            entries = list(info.get("entries") or [])

            for entry in entries:
                if not entry:
                    continue

                url = best_webpage_url(entry)
                title = entry.get("title") or "Autoplay song"

                if url and not self.seen(url, title):
                    return Track(
                        title=title,
                        query=url,
                        requester_id=requester_id,
                        requester_name=requester_name,
                        webpage_url=url,
                        duration=entry.get("duration"),
                        thumbnail=entry.get("thumbnail"),
                        platform="YouTube Autoplay",
                        source_id=entry.get("id"),
                        autoplay=True,
                    )
        except Exception as e:
            log.warning("Search autoplay failed: %s", e)

        return None

    def now_embed(self) -> discord.Embed:
        track = self.current
        cd_emoji = PLAYER_EMOJI_CACHE.get(self.guild_id, {}).get("cd")
        title_icon = str(cd_emoji) if cd_emoji else "??"

        embed = discord.Embed(
            title=f"{title_icon} Now Playing",
            color=discord.Color.blurple(),
        )

        if not track:
            embed.description = "Nothing is playing."
            embed.set_footer(text=BOT_CREDIT_TEXT)
            return embed

        title = escape(track.title)
        url = track.webpage_url

        if url and is_url(url):
            embed.description = f"**[{title}]({url})**"
        else:
            embed.description = f"**{title}**"

        position = self.playback_position()
        if track.duration:
            duration = max(1, int(track.duration))
            position = min(position, duration)
            segments = 24
            playhead_index = min(
                segments - 1,
                int(position / duration * segments),
            )
            playhead = ("?", "?")[int(time.monotonic() / 2) % 2]
            progress_bar = (
                "?" * playhead_index
                + playhead
                + "?" * (segments - playhead_index - 1)
            )
            progress = (
                f"`{format_duration(int(position))}`  {progress_bar}  "
                f"`{format_duration(track.duration)}`"
            )
        else:
            progress = f"?? LIVE � `{format_duration(int(position))}`"
        embed.add_field(name="?? Progress", value=progress, inline=False)

        embed.add_field(name="Source", value=track.platform, inline=True)
        embed.add_field(name="Requested by", value=track.requester_name, inline=True)
        embed.add_field(name="Autoplay", value="ON" if self.autoplay else "OFF", inline=True)
        embed.add_field(name="Queue", value=str(self.queue.qsize()), inline=True)
        track_key = get_track_key(track)
        likes = len(guild_likes.get(self.guild_id, {}).get(track_key or "", set())) if track_key else 0
        embed.add_field(name="?? Likes", value=str(likes), inline=True)

        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        embed.set_footer(text=BOT_CREDIT_TEXT)
        return embed

    def queue_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🎶 {BOT_NAME} Queue",
            color=discord.Color.red(),
        )

        if self.current:
            embed.add_field(
                name="Now Playing",
                value=f"**{escape(trim(self.current.title, 90))}**",
                inline=False,
            )

        queued = self.queued_tracks()

        if not queued:
            embed.description = "Queue is empty."
        else:
            lines = []
            for index, track in enumerate(queued[:10], start=1):
                lines.append(
                    f"`{index}.` **{escape(trim(track.title, 60))}** "
                    f"`{format_duration(track.duration)}`"
                )

            if len(queued) > 10:
                lines.append(f"...and **{len(queued) - 10}** more.")

            embed.description = "\n".join(lines)

        embed.set_footer(
            text=f"Autoplay: {'ON' if self.autoplay else 'OFF'} � {BOT_CREDIT_TEXT}"
        )
        return embed

    async def send_now_playing(self):
        channel = self.bot.get_channel(self.text_channel_id) if self.text_channel_id else None
        if channel:
            if self.now_playing_message:
                try:
                    await self.now_playing_message.edit(
                        embed=self.now_embed(),
                        view=CompactMusicControls(self.guild_id),
                    )
                    return
                except discord.NotFound:
                    self.now_playing_message = None
                except discord.HTTPException:
                    return

            with contextlib.suppress(discord.HTTPException):
                self.now_playing_message = await channel.send(
                    embed=self.now_embed(),
                    view=CompactMusicControls(self.guild_id),
                )

    async def player_loop(self):
        await self.bot.wait_until_ready()

        try:
            while not self.stopping:
                self.next_event.clear()

                # Handle seek: replay the same track from a new offset
                if self._replay_track is not None:
                    track = self._replay_track
                    self._replay_track = None
                    seek_secs = self._seek_to or 0.0
                    self._seek_to = None
                    await self.play_one(track, seek_seconds=seek_secs)
                    continue

                try:
                    if not self.queue.empty():
                        track = self.queue.get_nowait()
                    elif self.autoplay and self.last_track:
                        track = await self.make_autoplay_track()

                        if not track:
                            track = await asyncio.wait_for(
                                self.queue.get(),
                                timeout=IDLE_DISCONNECT_SECONDS,
                            )
                    else:
                        self.current = None
                        track = await asyncio.wait_for(
                            self.queue.get(),
                            timeout=IDLE_DISCONNECT_SECONDS,
                        )

                except asyncio.TimeoutError:
                    await self.send_text("?? Left voice because I was idle.")
                    await self.disconnect()
                    break

                await self.play_one(track)


        except asyncio.CancelledError:
            pass

        finally:
            self.current = None
            self.task = None

    async def play_one(self, track: Track, seek_seconds: float = 0.0):
        started = False
        progress_task = None

        try:
            self.current = track

            if not self.voice or not self.voice.is_connected():
                log.warning("Skipping %s because the bot is not connected to voice", track.title)
                return

            if self.has_lavalink():
                lavalink_track = await self._load_lavalink_track(track)
                if lavalink_track is None:
                    raise RuntimeError(f"Could not load {track.title} from Lavalink.")

                start_ms = max(0, int(seek_seconds * 1000))
                self.voice.play(lavalink_track, replace=True, start=start_ms, volume=int(self.volume * 1000))
                started = True
                if self._resume_after_effect:
                    self.voice.pause()
                    self.progress_offset = max(0.0, seek_seconds)
                    self.progress_started_at = 0.0
                    self._resume_after_effect = False
                else:
                    self.started_at = time.monotonic()
                    self.progress_offset = max(0.0, seek_seconds)
                    self.progress_started_at = self.started_at

                self.remember(track)
                await self.send_now_playing()

                progress_task = asyncio.create_task(self.update_now_playing_progress(track))
                await self._wait_for_lavalink_finish(track)
            else:
                stream_url = await resolve_stream_url(track)
                ffmpeg_source, ffmpeg_stderr = await make_audio_source(
                    stream_url,
                    track.stream_headers,
                    self.bass_db,
                    self.treble_db,
                    seek_seconds,
                    self.bass_boost,
                    self.speed,
                    self.pitch,
                    self.reverb,
                )
                source = discord.PCMVolumeTransformer(ffmpeg_source, volume=self.volume)

                def after_play(error):
                    playback_error = error or getattr(ffmpeg_source, "_current_error", None)
                    process = getattr(ffmpeg_source, "_process", None)
                    if process is not None and process.poll() is not None:
                        reader_thread = getattr(ffmpeg_source, "_pipe_reader_thread", None)
                        if reader_thread:
                            reader_thread.join(timeout=1.0)

                    stderr_text = ffmpeg_stderr.getvalue().decode(
                        "utf-8",
                        errors="replace",
                    ).strip()
                    if playback_error:
                        log.warning(
                            "Playback error for %s (%s): %s",
                            track.title,
                            track.platform,
                            playback_error,
                        )
                    if stderr_text:
                        log.warning(
                            "FFmpeg stderr for %s (%s):\\n%s",
                            track.title,
                            track.platform,
                            stderr_text,
                        )
                    self.bot.loop.call_soon_threadsafe(self.next_event.set)

                self.voice.play(source, after=after_play)
                started = True
                if self._resume_after_effect:
                    self.voice.pause()
                    self.progress_offset = max(0.0, seek_seconds)
                    self.progress_started_at = 0.0
                    self._resume_after_effect = False
                else:
                    self.started_at = time.monotonic()
                    self.progress_offset = max(0.0, seek_seconds)
                    self.progress_started_at = self.started_at

                self.remember(track)
                await self.send_now_playing()

                progress_task = asyncio.create_task(self.update_now_playing_progress(track))
                await self.next_event.wait()

        except asyncio.CancelledError:
            raise

        except Exception:
            log.exception("Could not play track")

        finally:
            if progress_task and not progress_task.done():
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_task
            self.progress_started_at = 0.0
            self.progress_offset = 0.0
            await self.stop_lyrics()
            await self.delete_track_messages()
            self.started_at = 0.0
            if track.temporary_file and self._replay_track is not track:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(track.temporary_file)
            if (
                started
                and track.temporary_file is None
                and self._replay_track is not track
            ):
                self.last_track = track

                if self.repeat_mode == "track":
                    await self.queue.put(track)
                elif self.repeat_mode == "queue":
                    await self.queue.put(track)

            self.current = None
            await self.send_now_playing()

    async def skip(self):
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()

    async def pause(self):
        if self.voice and self.voice.is_playing():
            self.progress_offset = self.playback_position()
            self.progress_started_at = 0.0
            self.voice.pause()
            await self.send_now_playing()

    async def resume(self):
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            self.progress_started_at = time.monotonic()
            await self.send_now_playing()

    def set_volume(self, value: float):
        self.volume = max(0.0, min(2.0, float(value)))

        if self.voice and self.voice.source and hasattr(self.voice.source, "volume"):
            self.voice.source.volume = self.volume

    def cycle_repeat_mode(self):
        modes = ["off", "track", "queue"]
        idx = modes.index(self.repeat_mode)
        self.repeat_mode = modes[(idx + 1) % len(modes)]
        return self.repeat_mode

    async def shuffle_queue(self):
        items = list(self.queue._queue)
        if len(items) < 2:
            return

        random.shuffle(items)
        self.queue = asyncio.Queue()
        for track in items:
            await self.queue.put(track)

    async def disconnect(self):
        self.expecting_voice_change = True
        try:
            if self.voice and self.voice.is_connected():
                try:
                    await self.voice.disconnect(force=True)
                except Exception:
                    log.exception("Could not disconnect the voice client")
        finally:
            self.expecting_voice_change = False

        self.voice = None

    async def handle_forced_voice_change(self, reason: str):
        log.warning("%s Stopping playback and clearing the queue.", reason)
        self.stopping = True
        self._resume_after_effect = False
        self._replay_track = None
        self._seek_to = None
        self.expecting_voice_change = True

        try:
            await self.stop_lyrics()
            await self.delete_track_messages()
            self.clear_queue()

            if self.voice and self.voice.is_connected():
                try:
                    await self.voice.disconnect(force=True)
                except Exception:
                    log.exception("Could not disconnect after a forced voice change")
            self.voice = None

            task = self.task
            self.task = None
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            self.current = None
            self.last_track = None
            self.stopping = False
        finally:
            self.expecting_voice_change = False

    async def stop(self):
        self.stopping = True
        self._resume_after_effect = False
        self._replay_track = None
        self._seek_to = None
        self.clear_queue()

        self._resume_after_effect = False
        self._replay_track = None
        self._seek_to = None
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()

        await self.disconnect()

        task = self.task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self.current = None
        self.last_track = None
        self.task = None
        self.stopping = False

    async def seek(self, seconds: float):
        if not self.current or not self.voice:
            return
        self._replay_track = self.current
        self._seek_to = seconds
        if self.voice.is_playing() or self.voice.is_paused():
            self.voice.stop()

    def restart_current_with_effects(self) -> bool:
        if not self.current or not self.voice:
            return False
        if not (self.voice.is_playing() or self.voice.is_paused()):
            return False

        self._resume_after_effect = self.voice.is_paused()
        self._replay_track = self.current
        self._seek_to = self.playback_position()
        self.voice.stop()
        return True

    def audio_effects_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="??? Audio Effects Studio",
            description=(
                f"**Preset:** {self.effect_preset}\n"
                f"**Bass Boost:** {self.bass_boost}x  �  "
                f"**Speed:** {self.speed:.1f}x  �  "
                f"**Pitch:** {self.pitch:.1f}x  �  "
                f"**Reverb:** {'On' if self.reverb else 'Off'}\n\n"
                "Effects apply live. The stream briefly restarts at the current position."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=BOT_CREDIT_TEXT)
        return embed

    def play_history_embed(self, page: int = 0) -> discord.Embed:
        page_size = 10
        recent_tracks = list(reversed(self.play_history))
        start = page * page_size
        page_tracks = recent_tracks[start : start + page_size]
        lines = []

        for index, track in enumerate(page_tracks, start=start + 1):
            title = escape(trim(track.title, 80))
            if track.webpage_url and is_url(track.webpage_url):
                title = f"[{title}]({track.webpage_url})"
            lines.append(
                f"`{index}.` **{title}** � `{format_duration(track.duration)}`"
            )

        embed = discord.Embed(
            title="?? Play History",
            description="\n".join(lines) if lines else "No tracks played this session yet.",
            color=discord.Color.blurple(),
        )
        total_pages = max(1, (len(recent_tracks) + page_size - 1) // page_size)
        embed.set_footer(text=f"Page {page + 1}/{total_pages} � This session")
        return embed


def get_player(guild: discord.Guild) -> MusicPlayer:
    player = players.get(guild.id)

    if not player:
        player = MusicPlayer(bot, guild)
        players[guild.id] = player

    return player


# -- Persistence helpers -----------------------------------------------------

def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_json(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _voice_config(guild_id: int) -> dict:
    data = _load_json(VOICE_SETTINGS_FILE)
    config = data.get(str(guild_id))
    if not isinstance(config, dict):
        config = {}
    config.setdefault("users", {})
    config.setdefault("phrases", {})
    return config


def _save_voice_config(guild_id: int, config: dict):
    data = _load_json(VOICE_SETTINGS_FILE)
    data[str(guild_id)] = config
    _save_json(VOICE_SETTINGS_FILE, data)


def _user_voice_config(guild_id: int, user_id: int) -> dict:
    user_configs = _voice_config(guild_id)["users"]
    config = user_configs.get(str(user_id), {})
    if not isinstance(config, dict):
        config = {}
    return {
        "voice": config.get("tts_voice") or BENGALI_VOICE_NAME,
        "volume": config.get("piper_volume", 1.0),
        "speed": config.get("piper_speed", 1.0),
        "noise_scale": config.get("piper_noise_scale", 0.667),
    }


def _available_tts_voices() -> List[str]:
    return [BENGALI_VOICE_NAME]


def _load_bengali_tts_model():
    try:
        from transformers import AutoTokenizer, VitsModel
    except ImportError as exc:
        raise RuntimeError(
            "Bengali TTS dependencies are missing. Install the packages from requirements.txt."
        ) from exc

    with TTS_SYNTHESIS_LOCK:
        cached_model = TTS_MODEL_CACHE.get(BENGALI_TTS_MODEL)
        if cached_model is None:
            try:
                tokenizer = AutoTokenizer.from_pretrained(BENGALI_TTS_MODEL)
                model = VitsModel.from_pretrained(BENGALI_TTS_MODEL)
            except Exception as exc:
                raise RuntimeError(
                    "Could not load the Bengali TTS model. Check the internet connection "
                    "for the first download, then retry."
                ) from exc
            model.eval()
            cached_model = (tokenizer, model)
            TTS_MODEL_CACHE[BENGALI_TTS_MODEL] = cached_model
        return cached_model


def _synthesize_bengali_audio(text: str, settings: dict) -> str:
    import torch

    tokenizer, model = _load_bengali_tts_model()
    with TTS_SYNTHESIS_LOCK:
        if re.search(r"[A-Za-z]", text):
            raise RuntimeError(
                "The Bengali voice requires Bengali-script text; Latin-script "
                "English or transliteration is not supported."
            )

        inputs = tokenizer(text, return_tensors="pt")
        if inputs["input_ids"].numel() == 0:
            raise RuntimeError(
                "The Bengali voice could not read this text. Enter it using Bengali script."
            )
        model.noise_scale = float(settings.get("noise_scale", 0.667))
        model.speaking_rate = float(settings.get("speed", 1.0))
        with torch.inference_mode():
            waveform = model(**inputs).waveform[0]

        volume = float(settings.get("volume", 1.0))
        pcm_audio = (
            waveform.clamp(-1.0, 1.0)
            .mul(volume)
            .clamp(-1.0, 1.0)
            .mul(32767)
            .to(torch.int16)
            .cpu()
            .numpy()
            .tobytes()
        )
        with tempfile.NamedTemporaryFile(
            suffix=".wav", prefix="music-bot-bengali-", delete=False
        ) as audio_file:
            audio_path = audio_file.name

        try:
            with wave.open(audio_path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(model.config.sampling_rate)
                wav_file.writeframes(pcm_audio)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.remove(audio_path)
            raise

        return audio_path


async def _generate_voice_audio(text: str, voice_config: dict) -> str:
    if not text.strip():
        raise RuntimeError("Speech text cannot be empty.")
    if len(text) > 500:
        raise RuntimeError("Speech text must be 500 characters or fewer.")
    voice_name = voice_config.get("voice") or BENGALI_VOICE_NAME
    if voice_name != BENGALI_VOICE_NAME:
        raise RuntimeError(f"Unsupported Bengali TTS voice: {voice_name}")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        YTDL_POOL,
        _synthesize_bengali_audio,
        text,
        voice_config,
    )


async def _queue_voice_message(
    player: "MusicPlayer",
    interaction: discord.Interaction,
    user_id: int,
    user_name: str,
    text: str,
    voice_config: dict,
):
    synthesis_task = asyncio.create_task(_generate_voice_audio(text, voice_config))
    connection_task = asyncio.create_task(
        player.connect_to_member_voice(interaction)
    )
    audio_path = None

    try:
        audio_path, _ = await asyncio.gather(synthesis_task, connection_task)
        track = Track(
            title=f"Speech: {trim(text, 70)}",
            query=audio_path,
            requester_id=user_id,
            requester_name=user_name,
            platform="Bengali TTS",
            is_file=True,
            temporary_file=audio_path,
        )
        await player.enqueue_many([track])
        player.start()
        audio_path = None
    except BaseException:
        for task in (synthesis_task, connection_task):
            if not task.done():
                task.cancel()
        results = await asyncio.gather(
            synthesis_task,
            connection_task,
            return_exceptions=True,
        )
        if audio_path is None and isinstance(results[0], str):
            audio_path = results[0]
        if audio_path:
            with contextlib.suppress(FileNotFoundError):
                os.remove(audio_path)
        raise


def track_to_dict(track: Track) -> dict:
    return {
        "title": track.title,
        "query": track.query,
        "webpage_url": track.webpage_url,
        "platform": track.platform,
        "duration": track.duration,
        "thumbnail": track.thumbnail,
    }

def dict_to_track(d: dict, requester_id: int, requester_name: str) -> Track:
    return Track(
        title=d.get("title", "Unknown"),
        query=d.get("query", ""),
        requester_id=requester_id,
        requester_name=requester_name,
        webpage_url=d.get("webpage_url"),
        duration=d.get("duration"),
        thumbnail=d.get("thumbnail"),
        platform=d.get("platform", "YouTube"),
    )

def get_track_key(track: Track) -> Optional[str]:
    return track.source_id or track.webpage_url or history_key(track.title)


def toggle_track_like(guild_id: int, track: Track, user_id: int) -> bool:
    track_key = get_track_key(track)
    if not track_key:
        raise RuntimeError("Cannot like this track.")

    guild_track_likes = guild_likes.setdefault(guild_id, {})
    users = guild_track_likes.setdefault(track_key, set())
    if user_id in users:
        users.remove(user_id)
        return False

    users.add(user_id)
    return True


class MusicControls(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.update_buttons()

    def get_player(self) -> Optional[MusicPlayer]:
        return players.get(self.guild_id)

    async def guard(self, interaction: discord.Interaction) -> Optional[MusicPlayer]:
        player = self.get_player()

        if not player or not player.voice:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return None

        if not player.user_in_same_voice(interaction.user):
            await interaction.response.send_message(
                "Join my voice channel first to use the controls.",
                ephemeral=True,
            )
            return None

        return player

    def update_buttons(self):
        player = self.get_player()
        emoji_map = PLAYER_EMOJI_CACHE.get(self.guild_id, {})

        if player and player.voice and player.voice.is_paused():
            self.pause_resume.label = "Play"
            self.pause_resume.emoji = emoji_map.get("play", "??")
        else:
            self.pause_resume.label = "Pause"
            self.pause_resume.emoji = emoji_map.get("pause", "??")

        self.repeat.emoji = emoji_map.get("repeat", "??")
        self.shuffle.emoji = emoji_map.get("shuffle", "??")
        self.lyrics.emoji = emoji_map.get("music", "??")

        if player and player.autoplay:
            self.autoplay.label = "Autoplay: ON"
            self.autoplay.style = discord.ButtonStyle.success
        else:
            self.autoplay.label = "Autoplay: OFF"
            self.autoplay.style = discord.ButtonStyle.secondary

        if player:
            self.repeat.label = f"Repeat: {player.repeat_mode.upper()}"
            self.shuffle.label = "Shuffle" if not player.shuffle_enabled else "Shuffled"

    async def apply_eq(self, interaction: discord.Interaction, bass_delta: float, treble_delta: float, reset: bool = False):
        player = await self.guard(interaction)
        if not player:
            return

        if reset:
            player.bass_db = 0.0
            player.treble_db = 0.0
        else:
            player.bass_db = max(-10.0, min(10.0, player.bass_db + bass_delta))
            player.treble_db = max(-10.0, min(10.0, player.treble_db + treble_delta))

        if not player.set_equalizer(player.bass_db, player.treble_db):
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        if not player.has_lavalink():
            player.restart_current_with_effects()
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="- Bass", emoji="??", style=discord.ButtonStyle.secondary)
    async def bass_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply_eq(interaction, bass_delta=-1.0, treble_delta=0.0)

    @discord.ui.button(label="+ Bass", emoji="??", style=discord.ButtonStyle.secondary)
    async def bass_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply_eq(interaction, bass_delta=1.0, treble_delta=0.0)

    @discord.ui.button(label="- Treble", emoji="???", style=discord.ButtonStyle.secondary)
    async def treble_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply_eq(interaction, bass_delta=0.0, treble_delta=-1.0)

    @discord.ui.button(label="+ Treble", emoji="???", style=discord.ButtonStyle.secondary)
    async def treble_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply_eq(interaction, bass_delta=0.0, treble_delta=1.0)

    @discord.ui.button(label="Reset EQ", emoji="??", style=discord.ButtonStyle.danger)
    async def reset_eq(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply_eq(interaction, bass_delta=0.0, treble_delta=0.0, reset=True)

    @discord.ui.button(label="- Vol", emoji="??", style=discord.ButtonStyle.secondary)
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return

        player.set_volume(player.volume - 0.1)
        await interaction.response.send_message(f"?? Volume: {player.volume:.1f}x", ephemeral=True)

    @discord.ui.button(label="+ Vol", emoji="??", style=discord.ButtonStyle.secondary)
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return

        player.set_volume(player.volume + 0.1)
        await interaction.response.send_message(f"?? Volume: {player.volume:.1f}x", ephemeral=True)

    @discord.ui.button(label="Repeat: OFF", emoji="??", style=discord.ButtonStyle.secondary)
    async def repeat(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return

        mode = player.cycle_repeat_mode()
        self.update_buttons()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"?? Repeat mode: {mode.upper()}", ephemeral=True)

    @discord.ui.button(label="Shuffle", emoji="??", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return

        player.shuffle_enabled = not player.shuffle_enabled
        if player.shuffle_enabled:
            await player.shuffle_queue()
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Lyrics", emoji="??", style=discord.ButtonStyle.secondary)
    async def lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await player.start_lyrics(interaction.channel)
            await interaction.followup.send(
                "?? Live lyrics started in this channel.", ephemeral=True
            )
        except Exception as exc:
            await log_interaction_error(interaction, exc, "Could not start lyrics")

    @discord.ui.button(label="Pause", emoji="??", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return

        if player.voice.is_paused():
            await player.resume()
        elif player.voice.is_playing():
            await player.pause()

        self.update_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Skip", emoji="??", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return

        await player.skip()
        await interaction.response.send_message("?? Skipped.", ephemeral=True)

    @discord.ui.button(label="Stop", emoji="??", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return

        await player.stop()

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(content="?? Stopped and left voice.", view=self)

    @discord.ui.button(label="Autoplay: OFF", emoji="??", style=discord.ButtonStyle.secondary)
    async def autoplay(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return

        player.autoplay = not player.autoplay
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Queue", emoji="??", style=discord.ButtonStyle.secondary)
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.get_player()

        if not player:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return

        await interaction.response.send_message(embed=player.queue_embed(), ephemeral=True)

    @discord.ui.button(label="Like", emoji="??", style=discord.ButtonStyle.danger)
    async def like(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.get_player()
        if not player or not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        liked = toggle_track_like(self.guild_id, player.current, interaction.user.id)
        await player.send_now_playing()
        msg = "?? Liked" if liked else "?? Unliked"
        await interaction.response.send_message(msg, ephemeral=True)


class PlayHistoryView(discord.ui.View):
    def __init__(self, guild_id: int, page: int = 0):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.page = page
        self.update_buttons()

    def get_player(self) -> Optional[MusicPlayer]:
        return players.get(self.guild_id)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ):
        await report_view_error(interaction, error, "Play history interaction failed")

    def update_buttons(self):
        player = self.get_player()
        total_pages = max(
            1,
            ((len(player.play_history) if player else 0) + 9) // 10,
        )
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= total_pages - 1

    @discord.ui.button(
        label="Previous",
        emoji="??",
        style=discord.ButtonStyle.secondary,
    )
    async def previous_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        self.page = max(0, self.page - 1)
        self.update_buttons()
        player = self.get_player()
        await interaction.response.edit_message(
            embed=player.play_history_embed(self.page) if player else None,
            view=self,
        )

    @discord.ui.button(
        label="Next",
        emoji="??",
        style=discord.ButtonStyle.secondary,
    )
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        player = self.get_player()
        total_pages = max(
            1,
            ((len(player.play_history) if player else 0) + 9) // 10,
        )
        self.page = min(total_pages - 1, self.page + 1)
        self.update_buttons()
        await interaction.response.edit_message(
            embed=player.play_history_embed(self.page) if player else None,
            view=self,
        )

    @discord.ui.button(
        label="Close",
        emoji="??",
        style=discord.ButtonStyle.danger,
    )
    async def close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content="Play history closed.",
            embed=None,
            view=None,
        )


class AudioEffectsStudio(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.update_buttons()

    def get_player(self) -> Optional[MusicPlayer]:
        return players.get(self.guild_id)

    def update_buttons(self):
        player = self.get_player()
        if not player:
            return
        self.bass_down.label = f"- Bass ({player.bass_boost}x)"
        self.bass_up.label = f"+ Bass ({player.bass_boost}x)"
        self.speed_down.label = f"- Speed ({player.speed:.1f}x)"
        self.speed_up.label = f"+ Speed ({player.speed:.1f}x)"
        self.pitch_down.label = f"- Pitch ({player.pitch:.1f}x)"
        self.pitch_up.label = f"+ Pitch ({player.pitch:.1f}x)"
        self.reverb.label = f"Reverb: {'ON' if player.reverb else 'OFF'}"
        self.reverb.style = (
            discord.ButtonStyle.success
            if player.reverb
            else discord.ButtonStyle.secondary
        )

    async def guard(self, interaction: discord.Interaction) -> Optional[MusicPlayer]:
        player = self.get_player()
        if not player or not player.voice or not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return None
        if not player.user_in_same_voice(interaction.user):
            await interaction.response.send_message(
                "Join my voice channel first to use audio effects.",
                ephemeral=True,
            )
            return None
        return player

    async def refresh(self, interaction: discord.Interaction, player: MusicPlayer):
        player.restart_current_with_effects()
        self.update_buttons()
        await interaction.response.edit_message(
            embed=player.audio_effects_embed(),
            view=self,
        )
        await player.send_now_playing()

    @discord.ui.button(
        label="Bass -",
        emoji="??",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def bass_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return
        player.bass_boost = max(1, player.bass_boost - 1)
        player.effect_preset = "Custom"
        await self.refresh(interaction, player)

    @discord.ui.button(
        label="Bass +",
        emoji="??",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def bass_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return
        player.bass_boost = min(5, player.bass_boost + 1)
        player.effect_preset = "Custom"
        await self.refresh(interaction, player)

    @discord.ui.button(
        label="Speed -",
        emoji="??",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def speed_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return
        player.speed = max(0.1, round(player.speed - 0.1, 1))
        player.effect_preset = "Custom"
        await self.refresh(interaction, player)

    @discord.ui.button(
        label="Speed +",
        emoji="??",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def speed_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return
        player.speed = min(3.0, round(player.speed + 0.1, 1))
        player.effect_preset = "Custom"
        await self.refresh(interaction, player)

    @discord.ui.button(
        label="Pitch -",
        emoji="??",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def pitch_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return
        player.pitch = max(0.5, round(player.pitch - 0.1, 1))
        player.effect_preset = "Custom"
        await self.refresh(interaction, player)

    @discord.ui.button(
        label="Pitch +",
        emoji="??",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def pitch_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return
        player.pitch = min(2.0, round(player.pitch + 0.1, 1))
        player.effect_preset = "Custom"
        await self.refresh(interaction, player)

    @discord.ui.button(
        label="Nightcore",
        emoji="?",
        style=discord.ButtonStyle.success,
        row=3,
    )
    async def nightcore(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return
        player.speed = 1.25
        player.pitch = 1.25
        player.reverb = False
        player.effect_preset = "Nightcore"
        await self.refresh(interaction, player)

    @discord.ui.button(
        label="Slowed + Reverb",
        emoji="??",
        style=discord.ButtonStyle.secondary,
        row=3,
    )
    async def slowed_reverb(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        player = await self.guard(interaction)
        if not player:
            return
        player.speed = 0.85
        player.pitch = 0.9
        player.reverb = True
        player.effect_preset = "Slowed + Reverb"
        await self.refresh(interaction, player)

    @discord.ui.button(
        label="Reverb: OFF",
        emoji="??",
        style=discord.ButtonStyle.secondary,
        row=3,
    )
    async def reverb(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return
        player.reverb = not player.reverb
        player.effect_preset = "Custom"
        await self.refresh(interaction, player)

    @discord.ui.button(
        label="Reset Effects",
        emoji="??",
        style=discord.ButtonStyle.danger,
        row=4,
    )
    async def reset_effects(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self.guard(interaction)
        if not player:
            return
        player.bass_boost = 1
        player.speed = 1.0
        player.pitch = 1.0
        player.reverb = False
        player.effect_preset = "Off"
        player.bass_db = 0.0
        player.treble_db = 0.0
        if player.has_lavalink():
            player.set_equalizer(0.0, 0.0)
        await self.refresh(interaction, player)


class CompactMusicControls(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.update_buttons()

    def get_player(self) -> Optional[MusicPlayer]:
        return players.get(self.guild_id)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ):
        await report_view_error(interaction, error, "Music control interaction failed")

    def update_buttons(self):
        player = self.get_player()
        emoji_map = PLAYER_EMOJI_CACHE.get(self.guild_id, {})
        paused = bool(player and player.voice and player.voice.is_paused())
        self.pause_resume.label = "Play" if paused else "Pause"
        self.pause_resume.emoji = emoji_map.get(
            "play" if paused else "pause",
            "??" if paused else "??",
        )
        if player:
            self.repeat.label = (
                f"Repeat: {player.repeat_mode.upper()}"
                if player.repeat_mode != "off"
                else "Repeat"
            )
            self.repeat.emoji = emoji_map.get("repeat", "??")

    @discord.ui.button(label="Like", emoji="??", style=discord.ButtonStyle.secondary, row=0)
    async def like(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.get_player()
        if not player or not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        try:
            liked = toggle_track_like(
                self.guild_id, player.current, interaction.user.id
            )
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer()
        await player.send_now_playing()
        await interaction.followup.send(
            "?? Liked this track." if liked else "?? Removed your like.",
            ephemeral=True,
        )

    @discord.ui.button(label="Pause", emoji="??", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.get_player()
        if not player or not player.voice or not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if not player.user_in_same_voice(interaction.user):
            await interaction.response.send_message(
                "Join my voice channel first to use the controls.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        if player.voice.is_paused():
            await player.resume()
        elif player.voice.is_playing():
            await player.pause()

    @discord.ui.button(label="Repeat", emoji="??", style=discord.ButtonStyle.secondary, row=0)
    async def repeat(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.get_player()
        if not player or not player.voice or not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if not player.user_in_same_voice(interaction.user):
            await interaction.response.send_message(
                "Join my voice channel first to use the controls.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        player.cycle_repeat_mode()
        await player.send_now_playing()

    @discord.ui.button(
        label="Open music controls",
        emoji="???",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def open_controls(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.get_player()
        if not player or not player.voice or not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if not player.user_in_same_voice(interaction.user):
            await interaction.response.send_message(
                "Join my voice channel first to use the controls.",
                ephemeral=True,
            )
            return

        controls = MusicControls(self.guild_id)
        await interaction.response.send_message(
            embed=player.now_embed(),
            view=controls,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Audio Effects",
        emoji="?",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def open_audio_effects(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        player = self.get_player()
        if not player or not player.voice or not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if not player.user_in_same_voice(interaction.user):
            await interaction.response.send_message(
                "Join my voice channel first to use audio effects.",
                ephemeral=True,
            )
            return

        effects = AudioEffectsStudio(self.guild_id)
        await interaction.response.send_message(
            embed=player.audio_effects_embed(),
            view=effects,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Play History",
        emoji="??",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def open_play_history(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        player = self.get_player()
        if not player or not player.play_history:
            await interaction.response.send_message(
                "No tracks have been played in this session yet.",
                ephemeral=True,
            )
            return

        history = PlayHistoryView(self.guild_id)
        await interaction.response.send_message(
            embed=player.play_history_embed(),
            view=history,
            ephemeral=True,
        )


@bot.tree.command(name="play", description="Play a song, YouTube/default search, URL, Spotify link, or uploaded file.")
@app_commands.describe(
    query="Song name or URL. If it is not a URL, YouTube is used by default.",
    file="Optional audio/video file upload.",
)
@app_commands.guild_only()
async def play(
    interaction: discord.Interaction,
    query: Optional[str] = None,
    file: Optional[discord.Attachment] = None,
):
    if not query and not file:
        await interaction.response.send_message(
            "Give me a song name, URL, Spotify link, or upload an audio file.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    player = get_player(interaction.guild)
    player.set_text_channel(interaction.channel_id)

    try:
        await player.connect_to_member_voice(interaction)
        tracks = await create_tracks(query, file, interaction.user)

        if not tracks:
            await interaction.edit_original_response(content="No tracks found.")
            return

        await player.enqueue_many(tracks)
        player.start()

        if len(tracks) == 1:
            track = tracks[0]
            await interaction.edit_original_response(
                content=f"? Added to queue: **{escape(trim(track.title, 100))}**"
            )
        else:
            await interaction.edit_original_response(
                content=(
                    f"? Added **{len(tracks)}** tracks to the queue. "
                    f"Playlist cap is **{MAX_PLAYLIST_ITEMS}**."
                )
            )

    except Exception as exc:
        await log_interaction_error(interaction, exc, "Play command failed")


@bot.tree.command(name="join", description="Join your voice channel.")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    player = get_player(interaction.guild)
    player.set_text_channel(interaction.channel_id)

    try:
        await player.connect_to_member_voice(interaction)
        await interaction.followup.send("? Joined your voice channel.", ephemeral=True)
    except Exception as exc:
        await log_interaction_error(interaction, exc, "Join command failed")


@bot.tree.command(name="leave", description="Stop music and leave voice.")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    player = get_player(interaction.guild)

    if player.voice and not player.user_in_same_voice(interaction.user):
        await interaction.followup.send(
            "Join my voice channel first to make me leave.",
            ephemeral=True,
        )
        return

    await player.stop()
    await interaction.followup.send("?? Left voice.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip the current song.")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    player = get_player(interaction.guild)

    if not player.voice or not player.current:
        await interaction.followup.send("Nothing is playing.", ephemeral=True)
        return

    if not player.user_in_same_voice(interaction.user):
        await interaction.followup.send(
            "Join my voice channel first.",
            ephemeral=True,
        )
        return

    await player.skip()
    await interaction.followup.send("?? Skipped.", ephemeral=True)


@bot.tree.command(name="pause", description="Pause the current song.")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    player = get_player(interaction.guild)

    if not player.voice or not player.voice.is_playing():
        await interaction.followup.send("Nothing is playing.", ephemeral=True)
        return

    if not player.user_in_same_voice(interaction.user):
        await interaction.followup.send("Join my voice channel first.", ephemeral=True)
        return

    await player.pause()
    await interaction.followup.send("?? Paused.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume paused music.")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    player = get_player(interaction.guild)

    if not player.voice or not player.voice.is_paused():
        await interaction.followup.send("Music is not paused.", ephemeral=True)
        return

    if not player.user_in_same_voice(interaction.user):
        await interaction.followup.send("Join my voice channel first.", ephemeral=True)
        return

    await player.resume()
    await interaction.followup.send("?? Resumed.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop music, clear queue, and leave voice.")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    player = get_player(interaction.guild)

    if player.voice and not player.user_in_same_voice(interaction.user):
        await interaction.followup.send("Join my voice channel first.", ephemeral=True)
        return

    await player.stop()
    await interaction.followup.send("?? Stopped, cleared queue, and left voice.", ephemeral=True)


@bot.tree.command(name="clear", description="Clear the music queue.")
@app_commands.guild_only()
async def clear(interaction: discord.Interaction):
    player = get_player(interaction.guild)

    if player.voice and not player.user_in_same_voice(interaction.user):
        await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
        return

    count = player.clear_queue()
    await interaction.response.send_message(f"?? Cleared **{count}** queued tracks.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the current queue.")
@app_commands.guild_only()
async def queue_command(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    await interaction.response.send_message(embed=player.queue_embed())


@bot.tree.command(name="nowplaying", description="Show the current song with control buttons.")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    await interaction.response.send_message(
        embed=player.now_embed(),
        view=CompactMusicControls(interaction.guild.id),
    )


@bot.tree.command(name="autoplay", description="Toggle autoplay.")
@app_commands.describe(enabled="Set autoplay on/off. Leave empty to toggle.")
@app_commands.guild_only()
async def autoplay(interaction: discord.Interaction, enabled: Optional[bool] = None):
    player = get_player(interaction.guild)

    if player.voice and not player.user_in_same_voice(interaction.user):
        await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
        return

    if enabled is None:
        player.autoplay = not player.autoplay
    else:
        player.autoplay = enabled

    await interaction.response.send_message(
        f"?? Autoplay is now **{'ON' if player.autoplay else 'OFF'}**.",
        ephemeral=True,
    )


@bot.tree.command(name="volume", description="Set the bot volume.")
@app_commands.describe(amount="Volume from 0.0 to 2.0, like 0.8 or 1.5")
@app_commands.guild_only()
async def volume_command(interaction: discord.Interaction, amount: float):
    player = get_player(interaction.guild)

    if player.voice and not player.user_in_same_voice(interaction.user):
        await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
        return

    player.set_volume(amount)
    await interaction.response.send_message(f"?? Volume set to {player.volume:.1f}x", ephemeral=True)


@bot.tree.command(name="repeat", description="Cycle repeat mode: off, track, queue.")
@app_commands.guild_only()
async def repeat_command(interaction: discord.Interaction):
    player = get_player(interaction.guild)

    if player.voice and not player.user_in_same_voice(interaction.user):
        await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
        return

    mode = player.cycle_repeat_mode()
    await interaction.response.send_message(f"?? Repeat mode: {mode.upper()}", ephemeral=True)


@bot.tree.command(name="shuffle", description="Shuffle the current queue.")
@app_commands.guild_only()
async def shuffle_command(interaction: discord.Interaction):
    player = get_player(interaction.guild)

    if player.voice and not player.user_in_same_voice(interaction.user):
        await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
        return

    await player.shuffle_queue()
    await interaction.response.send_message("?? Queue shuffled.", ephemeral=True)


@bot.tree.command(
    name="reset",
    description="Reset equalizer, audio effects, volume, and repeat state.",
)
@app_commands.guild_only()
async def reset_command(interaction: discord.Interaction):
    player = get_player(interaction.guild)

    if player.voice and not player.user_in_same_voice(interaction.user):
        await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
        return

    player.volume = 1.0
    player.bass_db = 0.0
    player.treble_db = 0.0
    player.bass_boost = 1
    player.speed = 1.0
    player.pitch = 1.0
    player.reverb = False
    player.effect_preset = "Off"
    player.repeat_mode = "off"
    if player.voice and player.voice.source and hasattr(player.voice.source, "volume"):
        player.voice.source.volume = player.volume
    player.set_equalizer(0.0, 0.0)
    player.restart_current_with_effects()
    await interaction.response.send_message("? All audio settings reset.", ephemeral=True)


@bot.tree.command(name="say", description="Speak Bengali text aloud in your voice channel.")
@app_commands.describe(text="Bengali-script text to speak (up to 500 characters).")
@app_commands.guild_only()
async def say_command(interaction: discord.Interaction, text: str):
    await interaction.response.send_message(
        "??? Connecting and preparing your speech�",
        ephemeral=True,
    )
    player = get_player(interaction.guild)
    player.set_text_channel(interaction.channel_id)
    try:
        await _queue_voice_message(
            player,
            interaction,
            interaction.user.id,
            interaction.user.display_name,
            text,
            _user_voice_config(interaction.guild_id, interaction.user.id),
        )
    except (RuntimeError, OSError) as exc:
        await log_interaction_error(interaction, exc, "Text-to-speech command failed")
        return
    await interaction.edit_original_response(
        content="??? Speech added to the voice queue."
    )


@bot.tree.command(name="voices", description="List available Bengali speech voices.")
@app_commands.describe(search="Optional language or voice name to search for.")
@app_commands.guild_only()
async def voices_command(interaction: discord.Interaction, search: Optional[str] = None):
    voices = _available_tts_voices()
    if search:
        search_lower = search.casefold()
        voices = [
            voice
            for voice in voices
            if search_lower in voice.casefold() or search_lower in "bengali bangla"
        ]

    if not voices:
        await interaction.response.send_message(
            "No Bengali voice matched that search. Run `/voices` without a search "
            "to see the configured local voice.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "Available offline Bengali voice: `bn` (Facebook MMS; one speaker, "
        "voice gender is not specified). No API key or usage credits are needed. "
        "The model is licensed CC BY-NC 4.0 for non-commercial use. "
        "Use `/voice set bn` to select it.",
        ephemeral=True,
    )


voice_group = app_commands.Group(
    name="voice",
    description="Configure your local Bengali speech voice.",
    guild_only=True,
)


@voice_group.command(name="set", description="Choose the Bengali voice for your speech.")
@app_commands.describe(voice_name="Use `bn` for the Bengali voice.")
async def voice_set(interaction: discord.Interaction, voice_name: str):
    voice_name = voice_name.strip()
    if voice_name not in _available_tts_voices():
        await interaction.response.send_message(
            "That Bengali voice is not available. Run `/voices` to see available voices.",
            ephemeral=True,
        )
        return
    config = _voice_config(interaction.guild_id)
    user_config = config["users"].setdefault(str(interaction.user.id), {})
    user_config["tts_voice"] = voice_name
    _save_voice_config(interaction.guild_id, config)
    await interaction.response.send_message(
        f"? Your Bengali voice is now **{escape(voice_name)}**.",
        ephemeral=True,
    )


@voice_group.command(name="settings", description="Set your local Bengali speech sound.")
@app_commands.describe(
    volume="Speech volume from 0.1 to 2.0 (default 1.0).",
    speed="Speech speed from 0.5 to 2.0 (default 1.0).",
    noise="Voice variation from 0.0 to 1.5 (default 0.667).",
)
async def voice_settings(
    interaction: discord.Interaction,
    volume: Optional[float] = None,
    speed: Optional[float] = None,
    noise: Optional[float] = None,
):
    values = {
        "piper_volume": volume,
        "piper_speed": speed,
        "piper_noise_scale": noise,
    }
    if all(value is None for value in values.values()):
        current = _user_voice_config(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(
            f"Volume: {current['volume']:.2f}; speed: {current['speed']:.2f}; "
            f"voice variation: {current['noise_scale']:.3f}.",
            ephemeral=True,
        )
        return
    valid_ranges = {
        "piper_volume": (0.1, 2.0),
        "piper_speed": (0.5, 2.0),
        "piper_noise_scale": (0.0, 1.5),
    }
    if any(
        value is not None and not valid_ranges[key][0] <= value <= valid_ranges[key][1]
        for key, value in values.items()
    ):
        await interaction.response.send_message(
            "Volume must be 0.1�2.0, speed 0.5�2.0, and voice variation 0.0�1.5.",
            ephemeral=True,
        )
        return
    config = _voice_config(interaction.guild_id)
    user_config = config["users"].setdefault(str(interaction.user.id), {})
    for key, value in values.items():
        if value is not None:
            user_config[key] = value
    _save_voice_config(interaction.guild_id, config)
    await interaction.response.send_message("? Voice settings saved.", ephemeral=True)


phrase_group = app_commands.Group(
    name="phrase",
    description="Save and speak reusable custom phrases.",
    guild_only=True,
)


@phrase_group.command(name="save", description="Save a reusable phrase.")
@app_commands.describe(name="Short name for the phrase.", text="Phrase text (up to 500 characters).")
async def phrase_save(interaction: discord.Interaction, name: str, text: str):
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
        await interaction.response.send_message(
            "Phrase names may use lowercase letters, numbers, underscores, and hyphens (up to 32 characters).",
            ephemeral=True,
        )
        return
    if not text.strip() or len(text) > 500:
        await interaction.response.send_message(
            "Phrase text must contain 1 to 500 characters.", ephemeral=True
        )
        return
    config = _voice_config(interaction.guild_id)
    if name not in config["phrases"] and len(config["phrases"]) >= 50:
        await interaction.response.send_message(
            "You have reached the 50 saved phrase limit. Delete one before adding another.",
            ephemeral=True,
        )
        return
    config["phrases"][name] = text.strip()
    _save_voice_config(interaction.guild_id, config)
    await interaction.response.send_message(f"? Saved phrase `{name}`.", ephemeral=True)


@phrase_group.command(name="play", description="Speak a saved phrase in your voice channel.")
@app_commands.describe(name="Name of the saved phrase.")
async def phrase_play(interaction: discord.Interaction, name: str):
    phrase = _voice_config(interaction.guild_id)["phrases"].get(name.strip().lower())
    if not phrase:
        await interaction.response.send_message("No saved phrase with that name.", ephemeral=True)
        return
    await interaction.response.send_message(
        "??? Connecting and preparing your phrase�",
        ephemeral=True,
    )
    player = get_player(interaction.guild)
    player.set_text_channel(interaction.channel_id)
    try:
        await _queue_voice_message(
            player,
            interaction,
            interaction.user.id,
            interaction.user.display_name,
            phrase,
            _user_voice_config(interaction.guild_id, interaction.user.id),
        )
    except (RuntimeError, OSError) as exc:
        await log_interaction_error(interaction, exc, "Phrase speech command failed")
        return
    await interaction.edit_original_response(
        content=f"??? Phrase `{escape(name.strip())}` added to the voice queue."
    )


@phrase_group.command(name="delete", description="Delete a saved phrase.")
@app_commands.describe(name="Name of the phrase to delete.")
async def phrase_delete(interaction: discord.Interaction, name: str):
    config = _voice_config(interaction.guild_id)
    if config["phrases"].pop(name.strip().lower(), None) is None:
        await interaction.response.send_message("No saved phrase with that name.", ephemeral=True)
        return
    _save_voice_config(interaction.guild_id, config)
    await interaction.response.send_message("??? Phrase deleted.", ephemeral=True)


@phrase_group.command(name="list", description="List your saved phrases.")
async def phrase_list(interaction: discord.Interaction):
    phrases = _voice_config(interaction.guild_id)["phrases"]
    if not phrases:
        await interaction.response.send_message("No saved phrases yet.", ephemeral=True)
        return
    listed_names = sorted(phrases)[:25]
    names = ", ".join(f"`{escape(name)}`" for name in listed_names)
    if len(phrases) > len(listed_names):
        names += f"\n�and {len(phrases) - len(listed_names)} more."
    await interaction.response.send_message(f"Saved phrases: {names}", ephemeral=True)


@bot.tree.command(name="about", description="About this music bot.")
async def about(interaction: discord.Interaction):
    embed = discord.Embed(
        title=BOT_NAME,
        description=BOT_CREDIT_TEXT,
        color=discord.Color.red(),
    )

    if BOT_AUTHOR:
        embed.add_field(name="Credit", value=BOT_AUTHOR, inline=True)
    embed.add_field(name="Default source", value="YouTube search", inline=True)
    embed.add_field(name="Supports", value="YouTube, Spotify metadata, yt-dlp URLs, files", inline=False)
    embed.set_footer(text=f"{BOT_NAME} - Credit to {BOT_AUTHOR}" if BOT_AUTHOR else BOT_NAME)

    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await log_interaction_error(interaction, error, "Slash command failed")


if __name__ == "__main__":
    # -- Favourite commands ------------------------------------------------------

    favourite_group = app_commands.Group(
        name="favourite",
        description="Manage your favourite songs.",
        guild_only=True,
    )

    @favourite_group.command(name="add", description="Add the current song to your favourites.")
    async def favourite_add(interaction: discord.Interaction):
        player = get_player(interaction.guild)
        if not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        track = player.current
        uid = str(interaction.user.id)
        data = _load_json(FAVOURITES_FILE)
        favs = data.get(uid, [])
        if len(favs) >= 50:
            await interaction.response.send_message(
                "? You have reached the maximum of 50 favourites. Remove some first.",
                ephemeral=True,
            )
            return
        entry = track_to_dict(track)
        # Prevent duplicates by query
        if any(f.get("query") == entry["query"] for f in favs):
            await interaction.response.send_message(
                f"?? **{escape(trim(track.title, 60))}** is already in your favourites.",
                ephemeral=True,
            )
            return
        favs.append(entry)
        data[uid] = favs
        _save_json(FAVOURITES_FILE, data)
        await interaction.response.send_message(
            f"?? Added **{escape(trim(track.title, 60))}** to your favourites.",
            ephemeral=True,
        )

    @favourite_group.command(name="remove", description="Remove a favourite song by its number.")
    @app_commands.describe(index="The number shown in /favourite list")
    async def favourite_remove(interaction: discord.Interaction, index: int):
        uid = str(interaction.user.id)
        data = _load_json(FAVOURITES_FILE)
        favs = data.get(uid, [])
        if not favs:
            await interaction.response.send_message("Your favourites list is empty.", ephemeral=True)
            return
        if index < 1 or index > len(favs):
            await interaction.response.send_message(
                f"? Invalid index. You have {len(favs)} favourites.", ephemeral=True
            )
            return
        removed = favs.pop(index - 1)
        data[uid] = favs
        _save_json(FAVOURITES_FILE, data)
        await interaction.response.send_message(
            f"??? Removed **{escape(trim(removed.get('title', 'Unknown'), 60))}** from favourites.",
            ephemeral=True,
        )

    @favourite_group.command(name="list", description="Show your favourite songs.")
    async def favourite_list(interaction: discord.Interaction):
        uid = str(interaction.user.id)
        data = _load_json(FAVOURITES_FILE)
        favs = data.get(uid, [])
        if not favs:
            await interaction.response.send_message("Your favourites list is empty.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"?? {interaction.user.display_name}'s Favourites",
            color=discord.Color.red(),
        )
        lines = []
        for i, f in enumerate(favs[:25], start=1):
            title = escape(trim(f.get("title", "Unknown"), 60))
            dur = format_duration(f.get("duration"))
            lines.append(f"`{i}.` **{title}** `{dur}`")
        if len(favs) > 25:
            lines.append(f"...and **{len(favs) - 25}** more.")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"{len(favs)}/50 favourites � {BOT_CREDIT_TEXT}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @favourite_group.command(name="play", description="Play a song from your favourites.")
    @app_commands.describe(index="The number shown in /favourite list")
    async def favourite_play(interaction: discord.Interaction, index: int):
        uid = str(interaction.user.id)
        data = _load_json(FAVOURITES_FILE)
        favs = data.get(uid, [])
        if not favs:
            await interaction.response.send_message("Your favourites list is empty.", ephemeral=True)
            return
        if index < 1 or index > len(favs):
            await interaction.response.send_message(
                f"? Invalid index. You have {len(favs)} favourites.", ephemeral=True
            )
            return
        fav = favs[index - 1]
        await interaction.response.defer(ephemeral=True, thinking=True)
        player = get_player(interaction.guild)
        player.set_text_channel(interaction.channel_id)
        try:
            await player.connect_to_member_voice(interaction)
        except Exception as exc:
            await log_interaction_error(interaction, exc, "Favourite playback failed")
            return
        track = dict_to_track(fav, interaction.user.id, interaction.user.display_name)
        await player.queue.put(track)
        player.start()
        await interaction.edit_original_response(
            content=f"?? Playing favourite: **{escape(trim(track.title, 80))}**"
        )

    # -- Playlist commands -------------------------------------------------------

    playlist_group = app_commands.Group(
        name="playlist",
        description="Save and load your playlists.",
        guild_only=True,
    )

    @playlist_group.command(name="save", description="Save the current queue as a named playlist.")
    @app_commands.describe(name="Name for your playlist")
    async def playlist_save(interaction: discord.Interaction, name: str):
        if len(name) > 50:
            await interaction.response.send_message("? Playlist name too long (max 50 chars).", ephemeral=True)
            return
        player = get_player(interaction.guild)
        tracks_to_save = []
        if player.current:
            tracks_to_save.append(track_to_dict(player.current))
        tracks_to_save.extend(track_to_dict(t) for t in player.queued_tracks())
        if not tracks_to_save:
            await interaction.response.send_message("Nothing is playing or queued.", ephemeral=True)
            return
        tracks_to_save = tracks_to_save[:100]
        uid = str(interaction.user.id)
        data = _load_json(PLAYLISTS_FILE)
        user_playlists = data.get(uid, {})
        if name not in user_playlists and len(user_playlists) >= 20:
            await interaction.response.send_message(
                "? You already have 20 saved playlists. Delete one first.", ephemeral=True
            )
            return
        user_playlists[name] = tracks_to_save
        data[uid] = user_playlists
        _save_json(PLAYLISTS_FILE, data)
        await interaction.response.send_message(
            f"?? Saved **{len(tracks_to_save)}** tracks as playlist **{escape(name)}**.",
            ephemeral=True,
        )

    @playlist_group.command(name="load", description="Load a saved playlist into the queue.")
    @app_commands.describe(name="Name of the playlist to load")
    async def playlist_load(interaction: discord.Interaction, name: str):
        uid = str(interaction.user.id)
        data = _load_json(PLAYLISTS_FILE)
        user_playlists = data.get(uid, {})
        if name not in user_playlists:
            await interaction.response.send_message(
                f"? No playlist named **{escape(name)}** found.", ephemeral=True
            )
            return
        saved = user_playlists[name]
        await interaction.response.defer(ephemeral=True, thinking=True)
        player = get_player(interaction.guild)
        player.set_text_channel(interaction.channel_id)
        try:
            await player.connect_to_member_voice(interaction)
        except Exception as exc:
            await log_interaction_error(interaction, exc, "Playlist loading failed")
            return
        tracks = [dict_to_track(d, interaction.user.id, interaction.user.display_name) for d in saved]
        await player.enqueue_many(tracks)
        player.start()
        await interaction.edit_original_response(
            content=f"?? Loaded **{len(tracks)}** tracks from playlist **{escape(name)}**."
        )

    @playlist_group.command(name="delete", description="Delete a saved playlist.")
    @app_commands.describe(name="Name of the playlist to delete")
    async def playlist_delete(interaction: discord.Interaction, name: str):
        uid = str(interaction.user.id)
        data = _load_json(PLAYLISTS_FILE)
        user_playlists = data.get(uid, {})
        if name not in user_playlists:
            await interaction.response.send_message(
                f"? No playlist named **{escape(name)}** found.", ephemeral=True
            )
            return
        del user_playlists[name]
        data[uid] = user_playlists
        _save_json(PLAYLISTS_FILE, data)
        await interaction.response.send_message(
            f"??? Deleted playlist **{escape(name)}**.", ephemeral=True
        )

    @playlist_group.command(name="list", description="List all your saved playlists.")
    async def playlist_list(interaction: discord.Interaction):
        uid = str(interaction.user.id)
        data = _load_json(PLAYLISTS_FILE)
        user_playlists = data.get(uid, {})
        if not user_playlists:
            await interaction.response.send_message("You have no saved playlists.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"?? {interaction.user.display_name}'s Playlists",
            color=discord.Color.blurple(),
        )
        lines = []
        for i, (pname, ptracks) in enumerate(user_playlists.items(), start=1):
            lines.append(f"`{i}.` **{escape(pname)}** � {len(ptracks)} tracks")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"{len(user_playlists)}/20 playlists � {BOT_CREDIT_TEXT}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- Queue management commands -----------------------------------------------

    @bot.tree.command(name="remove", description="Remove a song from the queue by position.")
    @app_commands.describe(position="Queue position to remove (1-indexed, use /queue to see positions)")
    @app_commands.guild_only()
    async def remove_command(interaction: discord.Interaction, position: int):
        player = get_player(interaction.guild)
        if player.voice and not player.user_in_same_voice(interaction.user):
            await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
            return
        items = list(player.queue._queue)
        if not items:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        if position < 1 or position > len(items):
            await interaction.response.send_message(
                f"? Invalid position. Queue has {len(items)} tracks.", ephemeral=True
            )
            return
        removed = items.pop(position - 1)
        player.queue = asyncio.Queue()
        for t in items:
            await player.queue.put(t)
        await interaction.response.send_message(
            f"??? Removed `#{position}` **{escape(trim(removed.title, 60))}** from the queue.",
            ephemeral=True,
        )

    @bot.tree.command(name="move", description="Move a song to a different position in the queue.")
    @app_commands.describe(
        from_pos="Current position of the song (1-indexed)",
        to_pos="Target position to move it to (1-indexed)",
    )
    @app_commands.guild_only()
    async def move_command(interaction: discord.Interaction, from_pos: int, to_pos: int):
        player = get_player(interaction.guild)
        if player.voice and not player.user_in_same_voice(interaction.user):
            await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
            return
        items = list(player.queue._queue)
        if not items:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        n = len(items)
        if from_pos < 1 or from_pos > n or to_pos < 1 or to_pos > n:
            await interaction.response.send_message(
                f"? Invalid positions. Queue has {n} tracks.", ephemeral=True
            )
            return
        if from_pos == to_pos:
            await interaction.response.send_message("Positions are the same.", ephemeral=True)
            return
        track = items.pop(from_pos - 1)
        items.insert(to_pos - 1, track)
        player.queue = asyncio.Queue()
        for t in items:
            await player.queue.put(t)
        await interaction.response.send_message(
            f"?? Moved **{escape(trim(track.title, 60))}** from position {from_pos} to {to_pos}.",
            ephemeral=True,
        )

    @bot.tree.command(name="seek", description="Seek to a position in the current song.")
    @app_commands.describe(seconds="Position in seconds to seek to (e.g. 90 for 1:30)")
    @app_commands.guild_only()
    async def seek_command(interaction: discord.Interaction, seconds: float):
        player = get_player(interaction.guild)
        if not player.voice or not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if not player.user_in_same_voice(interaction.user):
            await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
            return
        if seconds < 0:
            await interaction.response.send_message("? Seek position cannot be negative.", ephemeral=True)
            return
        dur = player.current.duration
        if dur and seconds > dur:
            await interaction.response.send_message(
                f"? Seek position exceeds song duration ({format_duration(dur)}).", ephemeral=True
            )
            return
        await player.seek(seconds)
        await interaction.response.send_message(
            f"? Seeked to **{format_duration(int(seconds))}**.", ephemeral=True
        )

    # Register command groups
    bot.tree.add_command(favourite_group)
    bot.tree.add_command(playlist_group)
    bot.tree.add_command(voice_group)
    bot.tree.add_command(phrase_group)

    bot.run(DISCORD_TOKEN)