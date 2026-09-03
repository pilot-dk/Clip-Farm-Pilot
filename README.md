# Clip Farm Pilot MVP — Studio GUI

Clip Farm Pilot turns livestream recordings into export-ready social clips and tightened full-length YouTube videos.

Official website: [clipfarmpilot.com](https://clipfarmpilot.com)

This updated build includes a simple, polished browser editor connected directly to the existing FastAPI and FFmpeg processing engine. It is a functional local app, not a visual mockup.

## Fully local iPhone, iPad, and Android sideload builds

Version 1.9 adds separate native mobile apps that do not connect to the hosted web backend. They have no account screen, analytics, upload client, cloud endpoint, or remote VOD importer. Video selection, preview, moment analysis, captions/effects, rendering, filenames, saving, and sharing are performed on the device. The Android manifest intentionally does not request `android.permission.INTERNET`; the iOS privacy manifest declares no tracking or collected data. Live speech recognition on iPhone/iPad is allowed only when Apple's on-device recognizer is available and never falls back to a server.

Downloads:

- `Clip-Farm-Pilot-iOS-v1.9.0-Local-Unsigned.ipa` — arm64 iPhone/iPad, iOS 17 or newer. Install with AltStore, SideStore, or Sideloadly; the IPA must be signed to your Apple ID during installation.
- `Clip-Farm-Pilot-Android-v1.9.0-Local.apk` — Android 8 or newer. Enable installation from the app you use to open the APK, then install it.

The local mobile editor includes local-file import, video preview, clip-range controls, local Auto-Find Clips, 16:9/9:16/1:1 export, square caption text with emoji/size/placement controls, filters, bundled Vine Boom placement, fresh local title filenames, MP4 export, and the device share sheet. The iPhone/iPad build also includes the gaming face-cam layout, moment visual effects, and optional on-device live captions. Direct YouTube/Twitch link import and direct social publishing are intentionally omitted because those features require internet access and would violate the offline-only guarantee.

### Install and update with SideStore

Add the Clip Farm Pilot source to SideStore once, then SideStore can show each newer version as an update:

- Source URL: `https://raw.githubusercontent.com/pilot-dk/Clip-Farm-Pilot/main/sidestore-source.json`
- One-tap link on iPhone/iPad: `sidestore://source?url=https://raw.githubusercontent.com/pilot-dk/Clip-Farm-Pilot/main/sidestore-source.json`

The source identifier and app bundle identifier are intentionally stable. Every iOS release must increase `CFBundleShortVersionString`, publish the matching IPA to a GitHub release, and place that version first in `sidestore-source.json`. The mobile release workflow performs the feed update automatically after uploading a new IPA.

Build the packages from source with:

```bash
./build_mobile_local_ios.sh
./build_mobile_local_android.sh
```

## Web app for iPhone, iPad, Android, and desktop

Clip Farm Pilot v1.13.1 is an installable Progressive Web App (PWA). The streamlined editor works in mobile Safari and Chrome, includes phone-safe spacing and touch controls, and can be added to a home screen without an App Store download.

The web release adds:

- A private password screen for hosted copies.
- Install support for iPhone, iPad, Android, and desktop browsers.
- Browser-rendered square captions so the exact emoji from the device is carried into the MP4.
- Offline live captions with word-level speech timing and selectable highlight colours.
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

## Desktop downloads

### macOS — Apple Silicon

1. Unzip `Clip-Farm-Pilot-macOS-v1.13.1-Apple-Silicon.zip`.
2. Drag **Clip Farm Pilot.app** into your Applications folder.
3. Right-click **Clip Farm Pilot.app** and choose **Open** the first time.

This build is ad-hoc signed but not Apple-notarized, so macOS may require the right-click **Open** step.

### Windows — x64 or ARM64

1. In **Settings → System → About**, check **System type**. Download `Clip-Farm-Pilot-Windows-v1.13.1-arm64.zip` for an ARM-based PC, or `Clip-Farm-Pilot-Windows-v1.13.1-x64.zip` for an Intel/AMD PC.
2. Unzip the download.
3. Open the **ClipFarmPilot** folder and run `ClipFarmPilot.exe`.
4. If Windows SmartScreen appears, choose **More info → Run anyway**.

The Windows build is currently unsigned. Both downloads are native packages and use the WebView2 runtime included with current Windows 10 and Windows 11 installations. The ARM64 package includes native ARM64 video tools instead of relying on x64 emulation.

### Linux — x64 or ARM64

1. Extract `Clip-Farm-Pilot-Linux-v1.13.1-x64.tar.gz` on an Intel/AMD computer or `Clip-Farm-Pilot-Linux-v1.13.1-arm64.tar.gz` on an ARM64 computer.
2. Open the **ClipFarmPilot** folder and run `./ClipFarmPilot`.

The Linux builds target Ubuntu 24.04 and compatible distributions on their matching architecture. They use the system GTK 3 and WebKitGTK 4.1 libraries. On Ubuntu, install them with `sudo apt install libgtk-3-0 libwebkit2gtk-4.1-0` if they are not already present.

All five desktop downloads contain their own Python runtime, FFmpeg engine, JavaScript runtime, and offline Whisper.cpp live-caption engine. No separate transcription account is required.

Clip Farm Pilot saves imported videos and exports under:

```text
macOS:   ~/Library/Application Support/Clip Farm Pilot
Windows: %LOCALAPPDATA%\Clip Farm Pilot
Linux:   ~/.local/share/clipfarmpilot
```

## Studio GUI

The editor now includes:

- Separate **Viral clips** and **Full YouTube video** workspaces without leaving the current project.
- A full-length 16:9 YouTube editor with independent **Remove silent pauses** and **Remove filler words** switches.
- Conservative pause trimming that keeps a short natural breath at speech boundaries, plus local word-timed removal for “um”, “uh”, “you know”, and similar filler.
- The supplied transparent YouTube subscribe animation, fully visible and centred at the very start with its original sound.
- Filters, live captions, smart Vine Boom and Check Sound placement, visual effects, original title suggestions, saving, and direct publishing in the full-length workspace.
- Drag-and-drop or file-picker livestream upload with upload progress.
- YouTube and Twitch VOD link importing with download progress.
- A searchable **Your videos** library for every cached upload and VOD, with a primary **Select video** action that reopens it directly in the editor—no folder browsing or re-uploading.
- Secondary **Show file** and recoverable **Move to Trash** controls. Exported clips are never removed with a source video.
- A responsive video preview that changes shape with the selected export ratio.
- Clip start/end controls, playhead scrubbing, and clip-range playback.
- **16:9**, **9:16**, and **1:1** visual presets.
- A **Gaming overlay** switch with a live vertical layout preview.
- Face-cam source position, crop width/height, and horizontal/vertical inset controls with an accurate zoom preview.
- Optional centered text for 1:1 clips, styled as bold white type with a black outline (and native color emoji).
- Device-rendered color emoji for square captions in the browser, avoiding Linux-server missing-glyph boxes.
- Native macOS emoji shaping for square captions, including hearts, flags, skin tones, keycaps, and joined family/profession emoji without missing-glyph boxes.
- Pixel-based optical centering that keeps the complete text-and-emoji caption centered consistently on macOS, Windows, Linux, and the web app.
- Top, centre, and bottom placement choices for square captions, with the selected placement reflected in the preview and exported MP4.
- A live **Caption size** slider for 1:1 clips, adjustable from 50% to 175% with the selected size carried into the rendered MP4.
- Optional **Live captions** that transcribe English speech locally and keep a readable group of words on screen while highlighting the word currently being spoken.
- Five live-caption colour schemes: **Pilot Lime**, **Ocean Blue**, **Sunset Gold**, **Neon Pink**, and **Electric Violet**.
- Seven full-clip looks: **Black & white**, **Cinematic**, **Vivid**, **Warm**, **Cool**, **Faded / Vintage**, and **High contrast**, with an instant preview in every template.
- A **Moment effects** editor available in 16:9, 9:16, 1:1, and the gaming layout.
- Bundled **Vine Boom** and **Check Sound** effects with adjustable volume. Either sound can be selected alone, or both can be added to the same clip.
- **Smart sound placement** that combines local speech cues with reactions, energy changes, and scene cuts, then assigns each selected sound to different well-spaced moments.
- Check Sound favours wins, confirmations, and positive payoffs; Vine Boom favours awkward lines, questions, sudden pauses, and surprising beats.
- A manual timing override for creators who want every selected sound at one exact playhead position.
- Lens flare, punch zoom, and white flash visual effects with adjustable strength and precise playhead-based timing.
- **Auto-Find Clips** with multi-signal ranking, clear explanations, and one-click selection.
- **Export Clip** actions in the toolbar and editor panel.
- Rendering status, errors, success feedback, and a finished MP4 save button.
- A native desktop **Save As** window after rendering, with a reusable **Save exported MP4** button if saving is cancelled.
- An optional **Original viral title** generator that listens to the exported clip, extracts its strongest spoken phrase, reads the audio-energy arc, and avoids recently recommended titles.
- A **Publish directly** panel for connecting YouTube, Instagram, and TikTok accounts through their official OAuth sign-in flows.
- Multi-platform publishing of the finished MP4 with a reusable title, caption/hashtags, visibility choice, per-platform success/error results, and direct links to successful posts.
- Responsive layouts for desktop, phone, and tablet browsers, plus home-screen installation.

## Full-Length YouTube Editor

Choose **Full YouTube video** at the top of the studio, then load an upload or saved VOD. The editor selects the complete timeline and locks the output to Full HD 16:9. **Remove silent pauses** detects dead air longer than a natural breath and keeps a short safety margin around speech. **Remove filler words** uses the bundled offline speech engine and conservative word timestamps so the two cleanup options can be enabled independently or together.

The full-length editor retains the existing filters, live-caption colours, smart sound effects, visual effects, viral-title recommendation, native Save As flow, and direct-publishing controls. Smart sound placement runs against the cleaned timeline and can distribute effect-specific moments across a long edit without crowding them.

Version 1.13 uses a single-pass render pipeline for full-length edits. Silence and filler cleanup, resizing, filters, captions, visual effects, smart sound mixing, and the subscribe animation are combined into one high-quality H.264 encode instead of repeatedly encoding the whole video. One cached offline transcript is shared by filler removal, live captions, smart sounds, and title generation, and long-video sound analysis now streams audio in bounded memory.

Enable **YouTube subscribe animation** to place the complete supplied transparent animation at 00:00. It is scaled to the 1920×1080 frame without cropping, vertically centred, mixed with its original audio, and disappears when its 3.72-second animation ends. Full-length editing and speech analysis run locally in the desktop app; no source video is uploaded to an AI provider.

### How Auto-Find Clips ranks moments

Version 1.3 replaces the original loudness-only heuristic with a two-stage local analysis:

1. A memory-safe streaming pass scores second-by-second loudness, reaction bursts, sudden rises, sustained momentum, and contrast with the surrounding VOD.
2. A diverse shortlist receives targeted visual analysis for motion and scene changes. This avoids decoding every frame of a multi-hour stream.
3. Each likely payoff is placed roughly two-thirds into the proposed clip, leaving room for setup before it and reaction afterward.
4. Overlapping results and dead-air-heavy windows are suppressed. Every result includes a plain-language reason and separate reaction, energy, and visual indicators.

The analysis runs locally and does not upload VOD audio or frames to an AI provider. Repeating an analysis of the same source during one app session reuses its cached audio features.

## Direct publishing to YouTube, Instagram, and TikTok

After exporting a clip, scroll to **Publish directly**:

1. Press **Connect** beside each configured platform and finish the official sign-in in your browser.
2. Select one or more connected destinations.
3. Review the generated title, add a caption/hashtags, and choose a visibility level.
4. Press **Publish selected accounts**.

The upload uses the already-rendered MP4 in Clip Farm Pilot’s export storage, so saving a duplicate copy first is not required. Account tokens are stored locally in `social-accounts.json` inside the platform-specific Clip Farm Pilot data folder with owner-only file permissions; passwords are never collected.

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

## Original viral title recommendations

The **Original viral title** switch is enabled by default. During export, Clip Farm Pilot privately transcribes the selected clip, finds a concrete phrase that was actually spoken, combines it with the clip's energy pattern, and suggests a concise curiosity-driven title such as:

```text
The Craziest Goal I Have Ever Scored — Wait for the Payoff.mp4
```

The recommendation appears in the editor, becomes the default filename in the desktop **Save As** window, and is copied into the direct-publishing title field. The app rotates through a large set of truthful hook structures and remembers its recent recommendations, so exporting again produces a different title instead of recycling the same line. You can edit it before saving or publishing, or turn the switch off to use Clip Farm Pilot’s standard filename.

Title analysis uses the same bundled offline English speech engine as live captions and never sends the clip to a third-party AI service. Creator-entered square text takes priority when present, then the spoken transcript, VOD title, and energy pattern. A strong title can improve packaging, but no title can guarantee virality.

## Live captions

Open **Live captions**, enable the switch, and choose a colour scheme before exporting. Clip Farm Pilot extracts the selected clip's audio, transcribes English speech with the bundled offline `base.en` Whisper.cpp model, groups the result into short readable phrases, and highlights each word for its own spoken time range. The result is burned into the MP4 and works in 16:9, 9:16, 1:1, and gaming layouts.

Caption transcription stays on the machine or hosted Clip Farm Pilot server running the export. It does not send audio to a third-party transcription API. Accuracy depends on microphone quality, overlapping speakers, music volume, accents, and game audio. The first live-captioned export takes longer because speech recognition runs before video rendering.

## Filters, sound, and visual effects

The **Effects** panel can apply a classic post-processing filter across the complete clip. When one or both sounds are selected, **Smart sound placement** is enabled by default. It scores local transcript cues, phrase endings, reaction spikes, abrupt stops, and scene cuts; assigns Check Sound and Vine Boom independently; prevents the two effects from crowding the same moment; and can repeat either sound while enforcing spacing. The finished editor shows the timestamps chosen for each effect. Turn Smart placement off to trigger every selected sound at one manual playhead position. Visual effects keep their own manual payoff trigger.

Smart placement uses the bundled offline speech engine when available. Positive cues such as wins, completion, and confirmation favour Check Sound, while awkward or questioning cues favour Vine Boom. Audio energy and scene structure remain as fallbacks when speech is unclear. The analysis stays on the machine or the Clip Farm Pilot server performing the export; it does not require a third-party AI account or usage fees.

Select **Vine Boom**, **Check Sound**, or both. Both samples were supplied by the project owner, trimmed for immediate playback, and are applied locally during export in landscape, portrait, square-caption, and gaming-overlay renders.

## Import a YouTube or Twitch VOD

1. Choose **Paste VOD link** under Livestream Video.
2. Paste a public YouTube video, YouTube livestream replay, or Twitch VOD URL.
3. Press **Import** and wait for the progress bar to finish.
4. Preview the cached video, run **Auto-Find Clips**, and export normally.

Clip Farm Pilot downloads a local working copy because audio analysis and FFmpeg rendering need direct media access. Private, subscriber-only, deleted, region-blocked, age-restricted, or DRM-protected videos may not import without an authenticated integration. Only import videos you own or have permission to download and reuse.

## Reopen or remove a saved video

1. Press **Videos** in the top toolbar.
2. Search by title and press **Select video** to reopen the saved upload or VOD immediately in the editor.
3. Use **Show file** only when you need the source itself, or press **Move to Trash** and confirm when you no longer need the full VOD.

The source copy goes to the operating system’s Trash or Recycle Bin, so it can still be recovered until that is emptied. Finished MP4 exports saved through **Export Clip** remain wherever you saved them and are not touched.

## What this build already does

- Upload MP4 / MOV / MKV / WEBM / M4V livestream recordings.
- Auto-pick exciting 15–60 second candidate moments using reactions, audio dynamics, build-up/payoff timing, local contrast, visual motion, and scene changes.
- Export normal clips as:
  - **16:9** — YouTube / landscape
  - **9:16** — Shorts / TikTok / Reels
  - **1:1** — square social posts
- Export a **9:16 gaming layout** with a large face-cam at the top and gameplay underneath.
- Choose which corner of the original stream contains the face-cam.
- Includes a Mac-friendly browser UI and an Expo React Native starter for iOS + Android.

> “Viral” cannot be guaranteed. The detector is deliberately honest about what it sees: reactions, momentum, contrast, payoff timing, motion, and visual changes. Transcript meaning, chat velocity, and game-specific event feeds can improve ranking further when those data are available.

## Architecture

- `backend/` — FastAPI + FFmpeg video engine.
- `backend/app/static/` — responsive installable browser dashboard.
- `mobile/` — Expo/React Native client for iOS and Android.
- `Dockerfile` and `render.yaml` — portable hosted web deployment.

## Run the web MVP locally

### 1. Install FFmpeg

```bash
brew install ffmpeg
```

### 2. Prepare the offline caption engine

From the project root, install CMake and Ninja when building on macOS, then run:

```bash
python3 scripts/prepare_caption_runtime.py
```

This downloads a checksum-verified Whisper.cpp runtime and the `base.en` model into the ignored `.caption-runtime/` build folder. Packaged and Docker builds run this preparation automatically.

### 3. Start the backend

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
7. Press **Export Clip**, choose a location in the desktop **Save As** window, and press **Save**. If you cancel, use **Save exported MP4** in the status panel.

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

## Build Windows and Linux apps from source

On native Windows x64 or ARM64 with matching Python 3.12 and Node.js installations:

```powershell
./build_windows.ps1 -Version 1.13.1 -Architecture x64
./build_windows.ps1 -Version 1.13.1 -Architecture arm64
```

On Ubuntu 24.04 x64 or ARM64 with matching Python 3.12, Node.js, GTK 3, and WebKitGTK 4.1 installations:

```bash
CLIPFARMPILOT_ARCHITECTURE=x64 ./build_linux.sh
CLIPFARMPILOT_ARCHITECTURE=arm64 ./build_linux.sh
```

The GitHub Actions **Build Windows and Linux releases** workflow builds and GUI-tests native Windows x64, Windows ARM64, Linux x64, and Linux ARM64 packages before uploading them to an existing GitHub release tag.

## Recommended v0.2 — what makes Clip Farm Pilot truly competitive

1. **Transcript intelligence** — transcribe the stream and score moments containing a strong setup, surprise, conflict, joke, rage/reaction, achievement, or payoff.
2. **Automatic face-cam detection** — detect the webcam rectangle rather than asking the user to choose a corner.
3. **Caption customization** — custom fonts, placement, safe-area presets, and multilingual speech models.
4. **Smart gameplay reframing** — track important game HUD/action instead of always center-cropping.
5. **Face tracking** — keep the streamer centered when the source webcam moves.
6. **Publishing copy generation** — generate platform-specific descriptions, hashtags, and on-screen hooks from the title transcript.
7. **Multiple clip lengths** — 15 sec / 30 sec / 45 sec / 60 sec with platform-specific scoring.
8. **Creator profiles** — remember each creator's face-cam position, caption style, font, watermark, game, and preferred layout.
9. **Batch mode** — upload a 4-hour stream and receive 10–20 ranked clips.
10. **YouTube/Twitch ingestion** — import VODs directly after authentication instead of manually downloading them.

## Production notes

Long livestreams should eventually be uploaded directly to object storage (S3/R2/etc.) instead of passing through one API server. Rendering should be moved to background workers with a job queue, progress reporting, retry handling, and GPU-assisted models when semantic/visual scoring is added.
