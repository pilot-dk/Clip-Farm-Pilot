# Third-party notices

Clip Farm Pilot's desktop build includes third-party open-source components. Their licenses remain with their respective projects.

- FFmpeg executable supplied by `imageio-ffmpeg` — FFmpeg project licensing applies.
- Node.js runtime — Node.js contributors, MIT license and bundled third-party notices apply.
- yt-dlp — The Unlicense.
- PyInstaller — GPL-2.0-or-later with a special exception allowing bundled applications.
- pywebview — BSD-3-Clause.
- whisper.cpp — Georgi Gerganov and contributors, MIT license. The bundled `base.en` speech-recognition model is derived from OpenAI Whisper and is used for offline word-level live captions.
- llama.cpp (llama-server, build b11799) — Georgi Gerganov and contributors, MIT license. It runs the auto-caption model.
- Qwen3.5 4B (Q4_K_M quantisation by Unsloth) — Alibaba Cloud's Qwen team, Apache License 2.0. Not bundled: downloaded from Hugging Face the first time Auto captions are used.
- LLVM OpenMP runtime (`libomp.dll`, Windows) — Apache License 2.0 with LLVM exceptions; its license is included beside it.
- GCC OpenMP runtime (`libgomp.so.1`, Linux) — GPL-3.0 with the GCC Runtime Library Exception.
- Microsoft Visual C++ runtime (`msvcp140.dll`, `vcruntime140.dll`, `vcruntime140_1.dll`, Windows) — redistributed with the speech and caption engines under the Visual Studio redistribution terms.
- "Vine Boom" and "Check" audio — media assets supplied by the project owner for inclusion and redistribution; no separate license files were provided.

The Windows ARM64 package additionally includes an FFmpeg 9.0.1 GPL build produced by [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-08-31-13-27) from FFmpeg commit `e47273f4d9`. FFmpeg is available under the GNU General Public License for this build. The corresponding FFmpeg source and BtbN build scripts are available from the linked release and repository.

The Windows ARM64 package also includes the Microsoft .NET 10 runtime and Windows Desktop runtime (version 10.0.12) from [Microsoft](https://dotnet.microsoft.com/download/dotnet/10.0), used to draw the app window. .NET is licensed under the MIT license; its license and third-party notices are included in the package's `dotnet` folder.

Clip Farm Pilot invokes FFmpeg as a separate local command-line program for video processing. No user media is uploaded by this integration.

Before distributing Clip Farm Pilot commercially, review the exact FFmpeg build configuration, the redistribution rights for the supplied sound-effect audio, and all licenses included in the final app bundle.
