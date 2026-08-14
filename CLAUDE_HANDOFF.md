# Clip Farm Pilot — Claude Handoff

Last updated: 2026-08-14  
Current version: 1.3.1
Clip-detection baseline commit: `3c86861` (`Improve VOD clip detection with multi-signal ranking`)

## Start here

Clip Farm Pilot turns livestream recordings and public YouTube/Twitch VODs into export-ready social clips. It has one shared Python/FFmpeg backend, a responsive browser/PWA editor, and an Apple Silicon Mac wrapper around that same web UI.

Read `README.md` after this file for the complete user-facing feature guide.

Important URLs:

- Repository: https://github.com/pilot-dk/Clip-Farm-Pilot
- Current hosted app: https://clipfarmpilot-web-pilot-dk.onrender.com
- Owned domain: https://clipfarmpilot.com
- Render Blueprint service name: `clipfarmpilot-web-pilot-dk`

The hosted app deploys from `main` and should report Clip Farm Pilot 1.3.1 after the current deployment completes. The new Render service currently has **no web password configured**, so it is publicly accessible. Do not invent, commit, print, or request an existing password. If the owner asks to protect the service, set a new secret value for `CLIPFARMPILOT_WEB_PASSWORD` directly in Render.

## Product status

Working features:

- Unified aviation-inspired Flight Deck interface across the responsive PWA and Expo mobile starter
- New aircraft/play app icon, navy instrument palette, amber launch actions, and phone-safe compact controls
- File upload and public YouTube/Twitch VOD import
- Imported-video library with source links, local paths, reveal/open, and deletion
- Video preview, start/end controls, and clip-range playback
- Auto-Find Clips multi-signal ranking
- 16:9, 9:16, and 1:1 exports
- 9:16 gaming layout with enlarged face cam above gameplay
- Adjustable source face-cam corner, width, height, and insets
- Centered square-format captions with adjustable size and color emoji support
- Bundled Vine Boom plus original impact-boom, whoosh, and record-scratch audio effects
- Lens flare, punch zoom, and white flash visual effects
- Content-aware clip filename suggestions
- Native Mac Save As flow
- YouTube, Instagram, and TikTok connection/publishing framework using official APIs
- Installable responsive PWA
- Apple Silicon `.app` bundle

The product name is **Clip Farm Pilot**. The canonical slug is `clipfarmpilot`, the bundle identifier is `com.clipfarmpilot.desktop`, and the environment variable prefix is `CLIPFARMPILOT_`.

## Architecture map

| Path | Responsibility |
| --- | --- |
| `backend/app/brand.py` | Product name, slug, version, environment-name compatibility |
| `backend/app/main.py` | FastAPI routes, auth, uploads, analysis, exports, library, social API |
| `backend/app/video.py` | Probing, clip analysis, titles, FFmpeg layouts, captions, effects |
| `backend/app/vod.py` | yt-dlp imports and persistent cached-video catalog |
| `backend/app/social.py` | OAuth account state and official-platform publishing |
| `backend/app/static/index.html` | Entire browser/PWA interface, CSS, and JavaScript |
| `backend/app/static/sw.js` | PWA shell cache; bump the cache name after static changes |
| `desktop_launcher.py` | Mac window, local server, native Save As/reveal links, bundle smoke test |
| `build_macos.sh` | Reproducible Apple Silicon PyInstaller build and ad-hoc signing |
| `Dockerfile` | Hosted Linux/FFmpeg image |
| `render.yaml` | Render Blueprint and hosted environment settings |
| `mobile/` | Expo mobile starter with the shared Flight Deck theme; functionality is still behind the PWA |
| `tests/` | Unit and integration regression suite |

There is no separate frontend build step. The production UI is the single tracked `backend/app/static/index.html` file.

## Auto-Find Clips 1.3

The old loudness-only detector was replaced at commit `3c86861`.

The current pipeline in `backend/app/video.py`:

1. Streams mono PCM from FFmpeg at 8 kHz, one second at a time. It does not load a multi-hour audio track into memory.
2. Extracts RMS energy, near-peak level, short reaction bursts, and high-frequency texture.
3. Builds normalized rise, local contrast, momentum, and salience signals using robust percentiles.
4. Locates diverse local maxima and places the likely payoff about 68% through each proposed clip, preserving setup and reaction.
5. Penalizes dead-air-heavy windows.
6. Runs low-resolution visual motion/scene-change analysis only on the strongest diverse shortlist. Quiet VODs receive visual probes distributed across the recording.
7. Re-ranks candidates, removes overlaps/near-duplicates, and returns:
   - `start`, `end`, and `peak`
   - a 1–98 `score`
   - a short `label` and `reason`
   - `reaction`, `momentum`, `visual`, and `contrast` indicators
8. Caches extracted audio features for up to four source files during the process lifetime.

Keep the detector honest: it estimates clip-worthiness from audiovisual dynamics, but it does not understand speech, game rules, or whether a moment will actually go viral.

Good next improvements, in priority order:

1. Background analysis jobs with progress polling and persistent analysis results. The current `/analyze` request is synchronous, which is fragile for very long VODs on Render's free CPU.
2. Optional transcript semantics: hooks, jokes, conflict, surprise, achievements, and payoff. Make it opt-in and explicit about API/model costs.
3. Twitch/YouTube chat-velocity ingestion when legally and technically available.
4. Automatic face-cam detection and face/reaction signals.
5. Game-specific event adapters or HUD/OCR signals rather than one universal visual heuristic.
6. Human feedback (“great pick” / “bad pick”) and an evaluation dataset before changing scoring weights aggressively.

Do not add a large transcription model to the Mac bundle or Docker image without discussing download size, memory, licensing, latency, and cost with the owner.

## Video/export behavior to preserve

Expected finished resolutions:

- 16:9 standard: `1920×1080`
- 9:16 standard: `1080×1920`
- 1:1 standard: `1080×1080`
- 9:16 gaming: `1080×1920`

Gaming mode assumes the webcam is already embedded in one source corner. Smaller face-cam crop fractions mean a tighter zoom. Preview and FFmpeg export must use matching crop math.

Square caption rendering has two paths:

- Browser/PWA: the browser renders a caption overlay image so device emoji survive Linux rendering.
- Mac/local fallback: Pillow plus macOS font shaping handles text and color emoji.

Do not regress the caption overlay dimensions or the emoji tests.

Impact Boom, Whoosh, and Record Scratch are generated locally. Vine Boom is a separate owner-supplied audio asset at `backend/app/assets/vine-boom.wav`; preserve its separate label and review its redistribution rights before commercial distribution.

## Storage and data lifecycle

Desktop default:

```text
~/Library/Application Support/Clip Farm Pilot
```

The desktop launcher migrates the pre-rebrand storage folder when possible and still supports earlier environment names through `backend/app/brand.py`.

Hosted Render storage:

```text
/tmp/clipfarmpilot
```

Render's free filesystem is ephemeral. Source VOD working copies are permanently deleted when requested in cloud mode; completed exports must be downloaded before the instance restarts. Desktop deletion moves source VODs to Trash and preserves exported clips.

Never commit uploads, exports, OAuth tokens, passwords, or provider client secrets.

## Social publishing reality

The UI and backend support official OAuth/publishing flows, but each network requires owner-created developer credentials and approval:

- YouTube: Data API upload credentials
- Instagram: professional Creator/Business account and Meta app
- TikTok: Content Posting API approval; unaudited clients may be limited to private posts

Secrets are environment variables. See `.env.example` and `README.md`. Keep client secrets server-side for a public production release.

## Running and testing

Local backend:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Full test suite from the repository root:

```bash
.build-venv/bin/python -m unittest discover -s tests -v
git diff --check
zsh -n build_macos.sh
```

At handoff, all **32 tests** pass. Important coverage includes:

- Isolated reactions and payoff placement
- Separate highlights and near-duplicate suppression
- Quiet visual-only VODs
- Short VODs
- Emoji captions and caption sizing
- Effects assets
- Video library deletion/preservation
- Social API behavior
- Hosted password sessions

After changing detection, test both synthetic ranking cases in `tests/test_clip_detection.py` and a real media file. Avoid tuning solely to one VOD.

## Building the Mac app

The build is Apple Silicon only:

```bash
export CLIPFARMPILOT_NODE_BINARY=/absolute/path/to/an/arm64/node
./build_macos.sh
```

Output:

```text
dist/Clip Farm Pilot.app
```

The current verified local archive is ignored by Git and lives at:

```text
outputs/Clip-Farm-Pilot-macOS-v1.3.1-Apple-Silicon.zip
```

Its SHA-256 at handoff is:

```text
7f8a9107135d8307b485d98f043dde125320574266a729e40c906d7c549f71bb
```

The app is ad-hoc signed, not Apple-notarized. Users may need to right-click **Open** on first launch. A public polished release eventually needs an Apple Developer ID, hardened runtime, notarization, and preferably universal or separate Intel/Apple Silicon builds.

The 1.3.1 archive includes the owner-supplied Vine Boom effect, was verified, and is published as the latest GitHub release:

```text
https://github.com/pilot-dk/Clip-Farm-Pilot/releases/tag/v1.3.1
```

## Deployment

Pushing `main` to GitHub triggers:

- GitHub Actions tests and Docker build
- Render auto-deploy through the Blueprint

After a deployment, verify:

```text
GET https://clipfarmpilot-web-pilot-dk.onrender.com/api/health
```

Expected response currently includes:

```json
{"ok": true, "name": "Clip Farm Pilot", "version": "1.3.1"}
```

Also verify `/api/auth/status`. At handoff it returns `required: false`, which confirms the hosted service is not password-protected.

The owned `clipfarmpilot.com` domain is referenced canonically in the app and README, but it has not been confirmed as connected to Render. Inspect DNS and Render custom-domain state before promising that the domain serves the app.

## Versioning checklist

When preparing a new release, update all of these together:

- `APP_VERSION` in `backend/app/brand.py`
- Default version in `build_macos.sh`
- PWA cache name in `backend/app/static/sw.js`
- Expo version in `mobile/app.json`
- Mobile package version in `mobile/package.json`
- Version assertions in `tests/test_brand.py`
- README download filename/version

Then run tests, build the Mac app, run the packaged smoke test in `desktop_launcher.py`, verify the bundle version/signature/architecture, create a fresh zip, and compute a new SHA-256.

## Working rules for the next assistant

- Preserve user media and unrelated changes. Never delete cached VODs or exports unless the owner explicitly asks.
- Do not expose secrets in logs, commits, screenshots, handoff text, or chat.
- Prefer incremental changes with tests over rewriting the working FFmpeg pipeline.
- Keep browser preview and exported framing mathematically consistent.
- Do not claim a title or clip is guaranteed to go viral.
- Check current Git status before editing and leave the tree clean after an approved commit.
- Confirm immediately before public release creation, credential changes, account connections, publishing to social networks, or destructive cloud actions.

## Suggested first task for Claude

Run the 33-test suite, open `backend/app/video.py` and `tests/test_clip_detection.py`, then inspect a few owner-provided VODs and record which proposed clips were genuinely good or bad. Use that labeled feedback to tune or train the next ranking layer. The highest-value engineering upgrade after evaluation is an asynchronous analysis job with progress reporting, followed by optional transcript semantics.
