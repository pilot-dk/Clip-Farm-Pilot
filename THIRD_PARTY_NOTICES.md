# Third-party notices

Clip Farm Pilot's desktop build includes third-party open-source components. Their licenses remain with their respective projects.

- FFmpeg executable supplied by `imageio-ffmpeg` — FFmpeg project licensing applies.
- Node.js runtime — Node.js contributors, MIT license and bundled third-party notices apply.
- yt-dlp — The Unlicense.
- PyInstaller — GPL-2.0-or-later with a special exception allowing bundled applications.
- pywebview — BSD-3-Clause.
- whisper.cpp — Georgi Gerganov and contributors, MIT license. The bundled `base.en` speech-recognition model is derived from OpenAI Whisper and is used for offline word-level live captions.
- "Vine Boom" and "Check" audio — media assets supplied by the project owner for inclusion and redistribution; no separate license files were provided.

The Windows ARM64 package additionally includes an FFmpeg GPL build produced by [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-09-03-13-17) from FFmpeg commit `9fc8c785e2`. FFmpeg is available under the GNU General Public License for this build. The corresponding FFmpeg source and BtbN build scripts are available from the linked release and repository.

Clip Farm Pilot invokes FFmpeg as a separate local command-line program for video processing. No user media is uploaded by this integration.

Before distributing Clip Farm Pilot commercially, review the exact FFmpeg build configuration, the redistribution rights for the supplied sound-effect audio, and all licenses included in the final app bundle.
