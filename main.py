# main.py
import os
import asyncio
import zipfile
import shutil
from pathlib import Path
from urllib.parse import urlparse
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

import yt_dlp
from spotdl import Spotdl
import requests
from bs4 import BeautifulSoup

# ────────────────────────────────────────
# Config ─ only from Railway variables or .env
# ────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

# Optional: get from Spotify developer dashboard if you want better results
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

spotdl_client = Spotdl(
    client_id=SPOTIFY_CLIENT_ID or "your-spotify-client-id",
    client_secret=SPOTIFY_CLIENT_SECRET or "your-spotify-client-secret"
)

# ────────────────────────────────────────
# Helpers
# ────────────────────────────────────────

def is_spotify_url(url: str) -> bool:
    return "spotify.com" in urlparse(url).netloc

def is_pinterest_url(url: str) -> bool:
    domain = urlparse(url).netloc.lower()
    return "pinterest." in domain or "pin.it" in domain

def is_youtube_url(url: str) -> bool:
    domain = urlparse(url).netloc.lower()
    return "youtube.com" in domain or "youtu.be" in domain

async def download_youtube(url: str, format_type: str, update: Update) -> Path | None:
    ydl_opts = {
        "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }
    if format_type == "mp3":
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    else:  # mp4
        ydl_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            filename = ydl.prepare_filename(info)
            if format_type == "mp3":
                filename = filename.rsplit(".", 1)[0] + ".mp3"
            return Path(filename)
    except Exception as e:
        await update.message.reply_text(f"YouTube download failed: {str(e)[:200]}")
        return None

def download_pinterest(url: str) -> list[Path] | None:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        media = []

        # Try to find main image
        img_tag = soup.find("img", {"src": lambda s: s and any(x in s for x in [".jpg", ".png", "pinimg.com"])})
        if img_tag and img_tag.get("src"):
            img_url = img_tag["src"]
            if not img_url.startswith("http"):
                img_url = "https:" + img_url
            fname = DOWNLOAD_DIR / f"pin_{hash(url)}.jpg"
            with open(fname, "wb") as f:
                f.write(requests.get(img_url, headers=headers).content)
            media.append(fname)

        # Try video
        video_tag = soup.find("video", {"src": True})
        if video_tag:
            vid_url = video_tag["src"]
            if not vid_url.startswith("http"):
                vid_url = "https:" + vid_url
            fname = DOWNLOAD_DIR / f"pin_{hash(url)}.mp4"
            with open(fname, "wb") as f:
                f.write(requests.get(vid_url, headers=headers).content)
            media.append(fname)

        return media if media else None
    except Exception as e:
        logger.error(f"Pinterest error: {e}")
        return None

async def process_spotify(url: str, update: Update) -> None:
    chat_id = update.effective_chat.id
    try:
        songs = spotdl_client.search([url])
        if not songs:
            await update.message.reply_text("No tracks found on Spotify.")
            return

        name = songs[0].artist if len(songs) == 1 else "Playlist"
        await update.message.reply_text(f"Found {len(songs)} track(s). Downloading...")

        temp_dir = DOWNLOAD_DIR / f"temp_{hash(url)}"
        temp_dir.mkdir(exist_ok=True)
        zip_path = DOWNLOAD_DIR / f"{name.replace(' ', '_')}.zip"

        for song in songs:
            try:
                result = await asyncio.to_thread(spotdl_client.download_song, song)
                if result and result[0]:
                    song_file = Path(result[0])
                    if song_file.exists():
                        song_file.rename(temp_dir / song_file.name)
            except:
                pass  # skip failed songs

        if not any(temp_dir.iterdir()):
            await update.message.reply_text("Download failed — no files saved.")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return

        # Create ZIP
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in temp_dir.rglob("*"):
                zf.write(file, file.relative_to(temp_dir))

        file_size_mb = zip_path.stat().st_size / (1024 * 1024)
        if file_size_mb < 48:  # Telegram bot limit ~50 MB
            await update.message.reply_document(
                document=zip_path,
                caption=f"Spotify download: {name} ({len(songs)} tracks)"
            )
        else:
            await update.message.reply_text(
                f"ZIP is too big ({file_size_mb:.1f} MB). Telegram limit is ~50 MB.\n"
                "Try a smaller playlist or ask for individual songs."
            )

        # Cleanup
        shutil.rmtree(temp_dir, ignore_errors=True)
        zip_path.unlink(missing_ok=True)

    except Exception as e:
        await update.message.reply_text(f"Spotify error: {str(e)[:300]}")

# ────────────────────────────────────────
# Handlers
# ────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a link:\n"
        "• Spotify playlist/album/track → ZIP of songs\n"
        "• Pinterest pin → image or video\n"
        "• YouTube link → choose MP3 or MP4"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith(("http://", "https://")):
        return

    url = text

    if is_spotify_url(url):
        await update.message.reply_text("Spotify detected → starting download...")
        asyncio.create_task(process_spotify(url, update))

    elif is_pinterest_url(url):
        await update.message.reply_text("Pinterest detected → fetching media...")
        files = download_pinterest(url)
        if files:
            for f in files:
                if f.suffix.lower() == ".mp4":
                    await update.message.reply_video(video=f.open("rb"))
                else:
                    await update.message.reply_photo(photo=f.open("rb"))
            for f in files:
                f.unlink(missing_ok=True)
        else:
            await update.message.reply_text("Could not download Pinterest media (page changed?).")

    elif is_youtube_url(url):
        keyboard = [
            [
                InlineKeyboardButton("🎵 MP3 (audio)", callback_data=f"yt|mp3|{url}"),
                InlineKeyboardButton("🎥 MP4 (video)", callback_data=f"yt|mp4|{url}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("YouTube detected → choose format:", reply_markup=reply_markup)

    else:
        await update.message.reply_text("Only Spotify, Pinterest, YouTube links supported.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("yt|"):
        return

    _, fmt, url = data.split("|", 2)
    await query.edit_message_text(f"Downloading as {fmt.upper()}...")

    file_path = await download_youtube(url, fmt, update)
    if file_path:
        try:
            if fmt == "mp3":
                await query.message.reply_audio(audio=file_path.open("rb"))
            else:
                await query.message.reply_video(video=file_path.open("rb"))
        finally:
            file_path.unlink(missing_ok=True)

    await query.message.delete()

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
