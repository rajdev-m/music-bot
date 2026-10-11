# Discord Music Bot

A Discord music bot built with `discord.py`, `yt-dlp`, and optional Lavalink/Spotify support,
with slash commands, interactive player controls, playlists/favourites, audio effects, lyrics,
and Bengali text-to-speech.

## Setup

1. Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications) and copy its token.
2. Copy the example config and fill it in:
   ```bash
   cp .env.example .env
   ```
   At minimum set `DISCORD_TOKEN`. Set `GUILD_ID` for instant slash-command sync to a single server.
3. Install and run (Python 3.11+, FFmpeg required):
   ```bash
   pip install -r requirements.txt
   python bot.py
   ```

### Docker
```bash
docker build -t music-bot .
docker run --env-file .env music-bot
```

### YouTube cookies (optional)
If YouTube asks for sign-in, export **your own** cookies in Netscape format to `.cookies.txt`
(or set `YOUTUBE_COOKIES_FILE`). This file is gitignored - never commit it.

### Branding
Set `BOT_NAME`, `BOT_AUTHOR`, and `BOT_CREDIT_TEXT` in `.env` to customise `/about` and the bot status.

## Security
`.env`, cookies, and per-server data files are listed in `.gitignore`. If a token or cookies are
ever committed, treat them as compromised: reset the token and sign out of the Google session.
