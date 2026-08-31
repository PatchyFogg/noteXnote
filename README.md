# noteXnote

A practice tool for musicians who need to slow a song down, loop a section
until it's second nature, and hear it without the harsh edges digital audio
usually carries.

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

## Everything else

- **Time-stretch with pitch lock** — slow a song to 25% speed and it still
  sounds like the same song, not a chipmunk or a groan. Powered by WSOLA.
- **A-B Loop + Loop Library** — mark a section, loop it instantly, and save
  up to 5 named loop regions per song so you can jump between the parts
  you're drilling.
- **3-band EQ** — Bass / Mid / Treble boost buttons, tuned to sit safely
  under the built-in limiter. No sliders to fight, no clipping.
- **Zoom view** — a second waveform pane that tracks your playhead in real
  time, DAW-style, so you can see exactly where you are inside a loop.
- **Reference mode** — snap back to full-speed, unprocessed playback with
  one click to re-anchor your ear, then jump right back into practice speed.
- **Plugin architecture** — the core app never changes; new capabilities
  ship as reviewed plugins via pull request, not random files someone
  hands you. Install a released plugin by dropping it in the plugins
  folder, then enable or disable it any time from
  **Plugins → Manage Plugins…**.

## Requirements

- macOS 10.15+
- `ffmpeg` (for loading anything beyond raw WAV) — `brew install ffmpeg`
- `yt-dlp` for the Paste URL source, `spotdl` for Spotify — both optional

## Installing

Download `noteXnote.dmg` from the latest [release](../../releases), open it,
and drag noteXnote into Applications.

## Building from source

```bash
git clone https://github.com/PatchyFogg/noteXnote.git
cd noteXnote
./build.sh
```

This creates a Homebrew-Python virtualenv, installs dependencies, and
produces both `dist/noteXnote.app` and `noteXnote.dmg`.

Run the self-test to sanity-check the core audio pipeline (WSOLA, EQ, the
D/A filter, the limiter, SQLite, bundled resources) without launching the
GUI:

```bash
python notexnote.py --selftest
```

## Contributing a plugin

noteXnote's plugin system is a contribution model, not a free-for-all: the
core app stays exactly as it is, and new capabilities ship as reviewed
plugins, not random files someone hands you. To add one, fork the repo,
write a single `.py` file that exports a `register(app)` function, and
open a pull request. Once merged and released, anyone can install it by
dropping the file into their plugins folder.

## Uninstalling

**Help → Uninstall noteXnote…** inside the app removes noteXnote and all
of its saved data in one step.

## License

MIT — see [LICENSE](LICENSE).
