<div align="center">

# noteXnote

**Slow it down. Loop it. Learn it.**

A practice tool for musicians — slow a song down, loop the hard part until
it's second nature, and hear it without the harsh edges digital audio
usually carries.

[![License: MIT](https://img.shields.io/badge/license-MIT-4dd0e1.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20%C2%B7%20Windows-ff6b35.svg)](#requirements)
[![Release](https://img.shields.io/github/v/release/PatchyFogg/noteXnote?color=8888aa&label=release)](https://github.com/PatchyFogg/noteXnote/releases)

</div>

---

## Quick start

1. Open a song from **Source** — a local file, a pasted URL, or search YouTube right in the app.
2. Hit **▶** and drag **Speed** down to whatever tempo you can actually play at.
3. Flip on **Pitch Lock** so it still sounds like the song, not a chipmunk.
4. **Set Start** / **Set End** to loop the hard part on repeat.

That's the whole workflow — everything below is what makes it worth using for hours at a time.

## D/A Reconstruction Filter — every song, automatically

Every track you load runs through a simulated analog reconstruction filter —
the same kind of stage a real DAC uses to turn digital samples back into a
smooth continuous waveform. It's a gentle, single-pole rolloff above 15kHz
(about −6dB per octave beyond the cutoff), the same shape classic
non-oversampling DACs produce, instead of the brick-wall digital filtering
that gives compressed or poorly-mastered files their glassy, fatiguing edge.

You don't turn it on. You don't tune it. It's just part of how noteXnote
sounds — on every file, every time, before anything else touches the signal.
Practice for hours without your ears getting tired.

## Features

| | |
|---|---|
| **Time-stretch with pitch lock** | Slow a song to 25% speed and it still sounds like the same song, not a chipmunk or a groan. Powered by WSOLA. |
| **Pitch transpose** | Shift up or down in half-step intervals, up to an octave either way, independent of Speed — for a capo position, a different tuning, or a singer's range. |
| **A-B Loop** | Mark a section and loop it instantly; remembered per song. |
| **YouTube Search** | Search and load a track's audio straight from the left pane — no download step to think about. |
| **3-band EQ** | Bass / Mid / Treble boost, tuned to sit safely under the built-in limiter. No sliders to fight, no clipping. |
| **Zoom view** | A second waveform pane that tracks your playhead in real time, DAW-style. |
| **Reference mode** | One click back to full-speed, unprocessed playback to re-anchor your ear. |
| **Plugins** | The core app never changes shape — new capabilities ship as reviewed plugins via pull request. |

## Requirements

- macOS 10.15+ or Windows 10+
- [`ffmpeg`](https://ffmpeg.org) — `brew install ffmpeg` (macOS) or `winget install ffmpeg` (Windows)
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) for Paste URL, [`spotdl`](https://github.com/spotDL/spotify-downloader) for Spotify — both optional

## Installing

**macOS:** download `noteXnote.dmg` from the latest [release](../../releases), open it, and drag noteXnote into Applications.

**Windows:** build from source (below) — a prebuilt `.exe` isn't published yet.

## Building from source

**macOS:**

```bash
git clone https://github.com/PatchyFogg/noteXnote.git
cd noteXnote
./build.sh
```

This creates a Homebrew-Python virtualenv, installs dependencies, and produces both `dist/noteXnote.app` and `noteXnote.dmg`.

**Windows** (PowerShell):

```powershell
git clone https://github.com/PatchyFogg/noteXnote.git
cd noteXnote
.\build_win.ps1
```

This creates a virtualenv, installs dependencies, and produces `dist\noteXnote.exe` via PyInstaller. Newer than the macOS build and less battle-tested — file an issue if something's off.

Sanity-check the core audio pipeline (WSOLA, EQ, the D/A filter, the limiter, SQLite, bundled resources) without launching the GUI, on either platform:

```bash
python notexnote.py --selftest
```

## Contributing a plugin

The core app stays exactly as it is; new capabilities ship as reviewed plugins, not random files someone hands you. Fork the repo, write a single `.py` file that exports a `register(app)` function, and open a pull request. Once merged and released, anyone can install it by dropping the file into their plugins folder.

## Uninstalling

**Help → Uninstall noteXnote…** inside the app removes all of its saved data in one step. On macOS this also removes the app bundle itself; on Windows, delete the noteXnote folder yourself afterward — there's no single standard install location to automate that part of.

## License

MIT — see [LICENSE](LICENSE).
