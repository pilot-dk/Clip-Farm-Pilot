# Clip Farm Pilot MVP — Studio GUI

Clip Farm Pilot turns livestream recordings into export-ready social clips.

Official website: [clipfarmpilot.com](https://clipfarmpilot.com)

This updated build includes a simple, polished browser editor connected directly to the existing FastAPI and FFmpeg processing engine. It is a functional local app, not a visual mockup.

## Web app for iPhone, iPad, Android, and desktop

Clip Farm Pilot v1.2 is an installable Progressive Web App (PWA). The same editor now works in mobile Safari and Chrome, includes phone-safe spacing and touch controls, and can be added to a home screen without an App Store download.

The web release adds:

- A private password screen for hosted copies.
- Install support for iPhone, iPad, Android, and desktop browsers.
- Browser-rendered square captions so the exact emoji from the device is carried into the MP4.
- Temporary cloud storage behavior, including permanent cleanup of finished VOD working copies.
- A Docker image, Render Blueprint, and GitHub Actions checks for a GitHub-based deployment.

GitHub Pages alone cannot run Clip Farm Pilot because Pages is static while Clip Farm Pilot needs Python and FFmpeg. Keep the source code on GitHub and connect that repository to a container host such as Render.

### Free GitHub + Render deployment

1. Create a new public or private GitHub repository and upload this project.
2. In Render, choose **New → Blueprint** and connect the repository. Render will detect `render.yaml`.
3. When prompted for `CLIPFARMPILOT_WEB_PASSWORD`, enter a long password that only you know.
4. Wait for the deployment to finish, then open the `onrender.com` address on your phone or iPad.
5. On iPhone/iPad, tap **Install app** in Clip Farm Pilot, then use **Share → Add to Home Screen**. On Android/Chrome, press **Install app** and accept the prompt.

Render's free web service is suitable for testing this personal MVP, but it sleeps when idle, has limited processing power, and does not retain working files across restarts. The first visit after it sleeps may take about a minute, and long 1080p VOD exports may need a paid CPU worker later. Always save completed clips to your device before leaving them on a free instance.

For local container testing:

```bash
docker build -t clipfarmpilot-web .
docker run --rm -p 8000:8000 \
  -e CLIPFARMPILOT_WEB_PASSWORD='choose-a-private-password' \
  -e CLIPFARMPILOT_SESSION_SECRET='replace-with-a-long-random-value' \
  clipfarmpilot-web
```

Then open `http://localhost:8000`. Copy `.env.example` when configuring another Docker host.

## Open the Mac app — no Terminal required

The packaged **Clip Farm Pilot.app** is built for Apple Silicon Macs (M1, M2, M3, M4, and newer):

1. Unzip `Clip-Farm-Pilot-macOS-v1.2.0-Apple-Silicon.zip`.
2. Drag **Clip Farm Pilot.app** into your Applications folder.
3. Right-click **Clip Farm Pilot.app** and choose **Open** the first time.

This development build is ad-hoc signed but not Apple-notarized, so macOS may require the right-click **Open** step. The app contains its own Python runtime, FFmpeg engine, and JavaScript runtime. Homebrew and Terminal setup are not required.

Clip Farm Pilot saves imported videos and exports under:

```text
~/Library/Application Support/Clip Farm Pilot
```

## Studio GUI

The editor now includes:

- Drag-and-drop or file-picker livestream upload with upload progress.
- YouTube and Twitch VOD link importing with download progress.
- An **Imported videos** library showing each cached source file, its original VOD link, size, date, and exact local save path.
- One-click **Show in Finder** and recoverable **Move to Trash** controls. Exported clips are never removed with a source video.
- A responsive video preview that changes shape with the selected export ratio.
- Clip start/end controls, playhead scrubbing, and clip-range playback.
- **16:9**, **9:16**, and **1:1** visual presets.
- A **Gaming overlay** switch with a live vertical layout preview.
- Face-cam source position, crop width/height, and horizontal/vertical inset controls with an accurate zoom preview.
- Optional centered text for 1:1 clips, styled as bold white type with a black outline (and native color emoji).
- Device-rendered color emoji for square captions in the browser, avoiding Linux-server missing-glyph boxes.
- Native macOS emoji shaping for square captions, including hearts, flags, skin tones, keycaps, and joined family/profession emoji without missing-glyph boxes.
- A live **Caption size** slider for 1:1 clips, adjustable from 50% to 175% with the selected size carried into the rendered MP4.
- A **Moment effects** editor available in 16:9, 9:16, 1:1, and the gaming layout.
- Original impact-boom, whoosh, and record-scratch sound effects with adjustable volume.
- Lens flare, punch zoom, and white flash visual effects with adjustable strength and precise playhead-based timing.
- **Auto-Find Clips** with ranked candidate moments and one-click selection.
- **Export Clip** actions in the toolbar and editor panel.
- Rendering status, errors, success feedback, and a finished MP4 save button.
- A native macOS **Save As** window after rendering, with a reusable **Save exported MP4** button if saving is cancelled.
- An optional **Viral clip filename** generator that inspects the finished clip’s audio-energy arc and combines it with the VOD/upload title or creator-entered center text.
- A **Publish directly** panel for connecting YouTube, Instagram, and TikTok accounts through their official OAuth sign-in flows.
- Multi-platform publishing of the finished MP4 with a reusable title, caption/hashtags, visibility choice, per-platform success/error results, and direct links to successful posts.
- Responsive layouts for desktop, phone, and tablet browsers, plus home-screen installation.

## Direct publishing to YouTube, Instagram, and TikTok

After exporting a clip, scroll to **Publish directly**:

1. Press **Connect** beside each configured platform and finish the official sign-in in your browser.
2. Select one or more connected destinations.
3. Review the generated title, add a caption/hashtags, and choose a visibility level.
4. Press **Publish selected accounts**.

The upload uses the already-rendered MP4 in Clip Farm Pilot’s export storage, so saving a duplicate copy first is not required. Account tokens are stored locally in `social-accounts.json` inside Clip Farm Pilot’s Application Support folder with owner-only file permissions; passwords are never collected.

Social networks require the app owner to register Clip Farm Pilot before third-party accounts can connect. Until the corresponding credentials are provided, that platform is labelled **Developer setup required** and links to the official setup documentation instead of pretending to connect.

The development build reads these values:

```text
CLIPFARMPILOT_YOUTUBE_CLIENT_ID
CLIPFARMPILOT_YOUTUBE_CLIENT_SECRET

CLIPFARMPILOT_INSTAGRAM_CLIENT_ID
CLIPFARMPILOT_INSTAGRAM_CLIENT_SECRET

CLIPFARMPILOT_TIKTOK_CLIENT_KEY
CLIPFARMPILOT_TIKTOK_CLIENT_SECRET
```

Optional `CLIPFARMPILOT_OAUTH_REDIRECT_BASE` can point all OAuth callbacks at a fixed production callback service. YouTube uploads support public, unlisted, and private visibility. Instagram publishing requires a professional Creator or Business account. TikTok requires Content Posting API approval; posts made through an unaudited TikTok client are limited to private visibility by TikTok.

For a public release, keep Meta and TikTok client secrets on a small Clip Farm Pilot web service rather than embedding them in the Mac app. The included local integration is suitable for development and testing with accounts authorized in each provider’s developer dashboard.

## Automatic viral filenames

The **Viral clip filename** switch is enabled by default. After rendering, Clip Farm Pilot analyzes the finished clip and suggests a short title such as:

```text
FC 26 Weekend League — Wait for the Ending.mp4
```

The generated title appears in the editor and becomes the default filename in the macOS **Save As** window. You can edit that filename in the Save window before saving, or turn the switch off to use Clip Farm Pilot’s standard filename.

This offline MVP uses the source/VOD title, creator-entered square caption, and changes in audio intensity. It does not yet transcribe speech or understand the exact game event, and no title can guarantee virality. Transcript-aware title generation is a natural next production upgrade.

## Sound and visual effects

The **Moment effects** panel lets you emphasize one payoff moment without changing templates. Choose one sound effect, one visual effect, or both, then set the trigger as seconds after the selected clip begins. You can type the time or move the preview to the moment and press **Use playhead**.

The included **Impact boom** is generated by Clip Farm Pilot from original synthesized audio rather than copying or bundling the recognizable Vine sound file. All effects are applied locally during export and work with landscape, portrait, square-caption, and gaming-overlay renders.

## Import a YouTube or Twitch VOD

1. Choose **Paste VOD link** under Livestream Video.
2. Paste a public YouTube video, YouTube livestream replay, or Twitch VOD URL.
3. Press **Import** and wait for the progress bar to finish.
4. Preview the cached video, run **Auto-Find Clips**, and export normally.

Clip Farm Pilot downloads a local working copy because audio analysis and FFmpeg rendering need direct media access. Private, subscriber-only, deleted, region-blocked, age-restricted, or DRM-protected videos may not import without an authenticated integration. Only import videos you own or have permission to download and reuse.

## Remove a cached VOD when you are done

1. Press **Imported videos** in the top toolbar.
2. Use **Show in Finder** to reveal Clip Farm Pilot’s local source copy. URL imports also include a link back to the original YouTube or Twitch page.
3. Press **Move to Trash** and confirm when you no longer need the full VOD.

The source copy goes to the macOS Trash, so it can still be recovered until the Trash is emptied. Finished MP4 exports saved through **Export Clip** remain wherever you saved them and are not touched.

## What this build already does

- Upload MP4 / MOV / MKV / WEBM / M4V livestream recordings.
- Auto-pick exciting 15–60 second candidate moments using local audio-energy and excitement-spike analysis.
- Export normal clips as:
  - **16:9** — YouTube / landscape
  - **9:16** — Shorts / TikTok / Reels
  - **1:1** — square social posts
- Export a **9:16 gaming layout** with a large face-cam at the top and gameplay underneath.
- Choose which corner of the original stream contains the face-cam.
- Includes a Mac-friendly browser UI and an Expo React Native starter for iOS + Android.

> “Viral” cannot be guaranteed. The included detector is an offline MVP that finds high-energy moments. The next production step should combine transcript hooks, semantic payoff, audio excitement, motion/game events, chat velocity, and visual face/emotion signals into a multi-signal ranking model.

## Architecture

- `backend/` — FastAPI + FFmpeg video engine.
- `backend/app/static/` — responsive installable browser dashboard.
- `mobile/` — Expo/React Native client for iOS and Android.
- `Dockerfile` and `render.yaml` — portable hosted web deployment.

## Run the Mac/web MVP

### 1. Install FFmpeg

```bash
brew install ffmpeg
```

### 2. Start the backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open:

```text
http://localhost:8000
```

Upload a livestream and Clip Farm Pilot will let you preview, analyze, frame, and export it.

The browser version supports VOD links too. Its Python dependencies now include `yt-dlp` and a standalone FFmpeg build.

### Basic workflow

1. Choose or drop a livestream video into the upload area.
2. Enter a clip start/end manually, or press **Auto-Find Clips** and choose a candidate.
3. Choose **16:9**, **9:16**, or **1:1**.
4. Optionally enable **Gaming overlay** and identify the source face-cam corner, crop size, and inward offset.
5. For **1:1**, optionally enter center text and adjust **Caption size** from 50% to 175%.
6. Optionally select a sound and visual effect, place the preview on the payoff, and press **Use playhead**.
7. Press **Export Clip**, choose a location in the macOS **Save As** window, and press **Save**. If you cancel, use **Save exported MP4** in the status panel.

## Run the iPhone / Android prototype

Keep the backend running, then open another terminal:

```bash
cd mobile
npm install
EXPO_PUBLIC_API_BASE_URL=http://YOUR-MAC-LAN-IP:8000 npx expo start
```

For example, if your Mac is `192.168.1.50`:

```bash
EXPO_PUBLIC_API_BASE_URL=http://192.168.1.50:8000 npx expo start
```

Use Expo Go while prototyping. For an App Store / Google Play release, deploy the backend publicly, add auth/storage/billing, then create native production builds with EAS.

## How the gaming layout works

The livestream is assumed to already contain a webcam in one corner. Clip Farm Pilot:

1. Crops that webcam area from the original stream.
2. Enlarges it across the top of a 1080×1920 vertical canvas.
3. Crops the gameplay separately and places it under the camera.
4. Keeps the original stream audio.

The browser UI lets you choose the webcam corner, adjust its crop width and height, and move the crop inward horizontally or vertically. Smaller face-area values zoom in closer. The live preview now uses the same crop math as the exported video. Gaming mode automatically selects the required 9:16 format.

## Verified output sizes

The included backend and updated GUI were tested locally with an uploaded video, automatic moment analysis, and finished exports:

- 16:9 standard — `1920×1080`
- 9:16 standard — `1080×1920`
- 1:1 standard — `1080×1080`
- 9:16 gaming overlay — `1080×1920`

## Build the Apple Silicon app from source

The included build script creates an ad-hoc-signed `.app` bundle:

```bash
export CLIPFARMPILOT_NODE_BINARY=/absolute/path/to/an/arm64/node
./build_macos.sh
```

The result is written to `dist/Clip Farm Pilot.app`. To ship through the Mac App Store or avoid first-open Gatekeeper warnings for wider distribution, use an Apple Developer signing identity and notarization workflow.

## Recommended v0.2 — what makes Clip Farm Pilot truly competitive

1. **Transcript intelligence** — transcribe the stream and score moments containing a strong setup, surprise, conflict, joke, rage/reaction, achievement, or payoff.
2. **Automatic face-cam detection** — detect the webcam rectangle rather than asking the user to choose a corner.
3. **Auto captions** — word-by-word animated subtitles with safe-area presets.
4. **Smart gameplay reframing** — track important game HUD/action instead of always center-cropping.
5. **Face tracking** — keep the streamer centered when the source webcam moves.
6. **Clip title/hook generation** — generate Shorts titles, on-screen hooks, captions, and descriptions.
7. **Multiple clip lengths** — 15 sec / 30 sec / 45 sec / 60 sec with platform-specific scoring.
8. **Creator profiles** — remember each creator's face-cam position, caption style, font, watermark, game, and preferred layout.
9. **Batch mode** — upload a 4-hour stream and receive 10–20 ranked clips.
10. **YouTube/Twitch ingestion** — import VODs directly after authentication instead of manually downloading them.

## Production notes

Long livestreams should eventually be uploaded directly to object storage (S3/R2/etc.) instead of passing through one API server. Rendering should be moved to background workers with a job queue, progress reporting, retry handling, and GPU-assisted models when semantic/visual scoring is added.
