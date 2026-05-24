# MediaDrop

A self-hosted, open-source video and audio downloader with a clean web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites — download as MP4, MP3, or JPG.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)

## Features

- Download videos from 1000+ supported sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- MP4 video, MP3 audio, or JPG thumbnail download
- Quality/resolution picker (up to 2160p)
- Bulk downloads — paste multiple URLs at once
- Download progress tracking with pause/resume
- Automatic URL deduplication
- Clean, responsive UI — no frameworks, no build step
- Single Python file backend (~150 lines)
- Electron desktop app for macOS

## Quick Start

### Desktop App (Recommended)

1. Download `MediaDrop.dmg` from [Releases](https://github.com/henry1786580051-lang/MediaDrop/releases)
2. Open the DMG and drag MediaDrop to Applications
3. Launch MediaDrop from Applications

### Web Version

```bash
brew install yt-dlp ffmpeg    # or apt install ffmpeg && pip install yt-dlp
git clone https://github.com/henry1786580051-lang/MediaDrop.git
cd MediaDrop/app
./reclip.sh
```

Open **http://localhost:8899**.

Or with Docker:

```bash
cd app
docker build -t mediadrop . && docker run -p 8899:8899 mediadrop
```

## Usage

1. Paste one or more video URLs into the input box
2. Choose **MP4** (video), **MP3** (audio), or **JPG** (thumbnail)
3. Click **Fetch** to load video info and thumbnails
4. Select quality/resolution if available
5. Click **Download** on individual videos, or **Download All**

## Supported Sites

Anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), including:

YouTube, TikTok, Instagram, Twitter/X, Reddit, Facebook, Vimeo, Twitch, Dailymotion, SoundCloud, Loom, Streamable, Pinterest, Tumblr, Threads, LinkedIn, and many more.

## Stack

- **Backend:** Python + Flask (~150 lines)
- **Frontend:** Vanilla HTML/CSS/JS (single file, no build step)
- **Download engine:** [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/)
- **Desktop:** Electron
- **Dependencies:** 2 (Flask, yt-dlp)

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.

## License

[MIT](LICENSE)
