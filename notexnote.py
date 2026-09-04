#!/usr/bin/env python3
"""noteXnote - Practice Tool"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import sys
import numpy as np
import sounddevice as sd
import sqlite3
import threading
import wave
import subprocess
import shutil
import os
import json
import tempfile
import math
from pathlib import Path

# ─── Constants ────────────────────────────────────────────────────────

APP_NAME = "noteXnote"
VERSION = "1.0.0"
SUPPORTED_FORMATS = ('.wav', '.mp3', '.m4a', '.flac', '.ogg', '.aac', '.wma', '.aiff')
MIN_SPEED = 0.25
MAX_SPEED = 2.0
DEFAULT_SR = 44100
EQ_FREQS = [200, 1000, 5000]

EQ_PRESETS = {
    'Flat':   [0, 0, 0],
    'Bass':   [+5, -1, -2],
    'Mid':    [-2, +5, -2],
    'Treble': [-2, -1, +5],
}

NORM_TARGET_DB = -6.0
DA_FILTER_CUTOFF_HZ = 15000.0
PITCH_MIN_ST = -12
PITCH_MAX_ST = 12

_BREW_PATHS = ['/usr/local/bin', '/opt/homebrew/bin',
               os.path.expanduser('~/bin'), '/usr/bin']

_EXTERNAL_ENV_STRIP = (
    'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH',
    'DYLD_FRAMEWORK_PATH', 'DYLD_FALLBACK_FRAMEWORK_PATH',
    'DYLD_INSERT_LIBRARIES', 'PYTHONHOME', 'PYTHONPATH', 'RESOURCEPATH',
)


def _external_env():
    """Environment for an independently-linked external tool (yt-dlp,
    ffmpeg, spotdl) — a copy of ours with our own bundled Python's
    library-resolution overrides stripped out.

    The packaged app sets DYLD_LIBRARY_PATH/PYTHONHOME (etc.) so its own
    bundled Python and extension modules resolve dylibs from inside
    Contents/Frameworks — including a bundled libssl/libcrypto pulled in
    by the numpy/scipy stack. An external tool has its own, separately
    linked dylibs; if it inherits these overrides, its dynamic linker can
    get pointed at the app's copies instead of its own, and for OpenSSL
    that means loading an ABI-incompatible libssl/libcrypto — a real,
    deterministic SIGSEGV inside OpenSSL's provider code (confirmed via
    a crash whose captured stderr was a faulthandler dump, not a normal
    yt-dlp error), not a rare timing race. External tools must get a
    clean environment so their own linker resolves their own dylibs."""
    env = os.environ.copy()
    for key in _EXTERNAL_ENV_STRIP:
        env.pop(key, None)
    return env


def _find_tool(name):
    """Find an external tool, checking Homebrew paths first."""
    found = shutil.which(name)
    if found:
        return found
    for d in _BREW_PATHS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _extract_ytdlp_error(stderr):
    """yt-dlp's stderr is often several lines of diagnostic noise before
    the actual reason — pull out the real ERROR: line instead of dumping
    all of it (or worse, none of it) at the user."""
    if not stderr:
        return "unknown error"
    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    for l in lines:
        if l.startswith('ERROR:'):
            return l[len('ERROR:'):].strip()
    return lines[-1] if lines else "unknown error"


def _youtube_search(query, n=8):
    """Search YouTube via yt-dlp with no download — flat-playlist mode
    skips per-video metadata fetch, so this is fast. Returns a list of
    {'title', 'duration', 'channel', 'url'} dicts. Raises on failure with
    yt-dlp's actual error message, not just "exit status 1"."""
    ytdlp = _find_tool('yt-dlp')
    if not ytdlp:
        raise RuntimeError("yt-dlp not found. Install with: brew install yt-dlp")
    try:
        result = subprocess.run(
            [ytdlp, f'ytsearch{n}:{query}', '--flat-playlist', '-J', '--no-warnings'],
            check=True, capture_output=True, text=True, timeout=30,
            env=_external_env())
    except subprocess.CalledProcessError as e:
        raise RuntimeError(_extract_ytdlp_error(e.stderr)) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("search timed out after 30s") from None
    data = json.loads(result.stdout)
    out = []
    for e in data.get('entries') or []:
        out.append({
            'title': e.get('title') or 'Untitled',
            'duration': e.get('duration'),
            'channel': e.get('channel') or e.get('uploader') or '',
            'url': e.get('url') or e.get('webpage_url') or '',
        })
    return out


WAVE_BG = '#1a1a2e'
WAVE_PEAK = '#4CAF50'
WAVE_RMS = '#1B5E20'
PLAYHEAD = '#ff6b35'
LOOP_FILL = '#2a3f6d'
LOOP_EDGE = '#5577ff'
GRID = '#252540'


def _data_dir():
    d = os.path.expanduser(f'~/Library/Application Support/{APP_NAME}')
    os.makedirs(d, exist_ok=True)
    return d


def _temp_dir():
    d = os.path.join(tempfile.gettempdir(), 'notexnote_temp')
    os.makedirs(d, exist_ok=True)
    return d


def _prefs_path():
    return os.path.join(_data_dir(), 'prefs.json')


def _resource_path(filename):
    """Locate a bundled resource — works both running from source and from
    the packaged .app (py2app sets RESOURCEPATH to Contents/Resources)."""
    res_dir = os.environ.get('RESOURCEPATH')
    if res_dir:
        p = os.path.join(res_dir, filename)
        if os.path.isfile(p):
            return p
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def _resolve_alias(path):
    """Resolve a macOS Finder alias file to its target POSIX path.

    Returns `path` unchanged if it isn't a valid alias.
    """
    if not os.path.isfile(path):
        return path
    try:
        script = (f'tell application "Finder" to POSIX path of '
                  f'(original item of (POSIX file "{path}" as alias) as alias)')
        out = subprocess.run(['osascript', '-e', script],
                              capture_output=True, text=True, timeout=3)
        target = out.stdout.strip().rstrip('/')
        if target and os.path.isdir(target):
            return target
    except Exception:
        pass
    return path


def _find_music_dirs():
    """Return a list of (label, path) shortcuts to music folders on this Mac.

    Scans typical Apple Music paths, mounted volumes, and ~/Music aliases.
    Excludes the .musiclibrary Apple Music database (not useful for browsing).
    """
    seen = set()
    results = []

    def add(label, path):
        p = os.path.expanduser(path)
        p = _resolve_alias(p)
        try:
            if not os.path.isdir(p):
                return
            if not os.listdir(p):
                return
        except OSError:
            return
        real = os.path.realpath(p)
        if real in seen:
            return
        seen.add(real)
        results.append((label, p))

    add('Home Music', '~/Music')
    add('Music/Music', '~/Music/Music')
    add('Downloads', '~/Downloads')
    try:
        for vol in sorted(os.listdir('/Volumes')):
            base = f'/Volumes/{vol}'
            add(f'{vol} / Music', f'{base}/Music/Music')
            add(f'{vol}', f'{base}/Music')
    except OSError:
        pass
    return results


SOURCE_MAP = {
    'local': 'Local File',
    'spotify': 'Spotify', 'paste_url': 'Paste URL',
}
SOURCE_RMAP = {v: k for k, v in SOURCE_MAP.items()}


def _fmt_time(seconds):
    seconds = max(0, seconds)
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _fmt_time_precise(seconds):
    """mm:ss.mmm — sub-second precision for scrubbing in the zoom view."""
    seconds = max(0, seconds)
    m = int(seconds) // 60
    s = seconds - m * 60
    return f"{m:02d}:{s:06.3f}"


def _fmt_time_lcd(seconds):
    """HH:MM:SS.ss — hardware-counter-style digital readout."""
    seconds = max(0, seconds)
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


# ─── Song Database ───────────────────────────────────────────────────

class SongDB:
    def __init__(self):
        self._conn = sqlite3.connect(
            os.path.join(_data_dir(), 'notexnote.db'),
            check_same_thread=False)
        self._conn.execute('''CREATE TABLE IF NOT EXISTS songs (
            path TEXT PRIMARY KEY,
            speed REAL DEFAULT 1.0,
            pitch_lock INTEGER DEFAULT 1,
            loop_a REAL, loop_b REAL,
            position REAL DEFAULT 0,
            eq_preset TEXT,
            pitch_semitones INTEGER DEFAULT 0
        )''')
        try:
            self._conn.execute('ALTER TABLE songs ADD COLUMN eq_preset TEXT')
        except sqlite3.OperationalError:
            pass  # already exists (pre-existing DB from before this column)
        try:
            self._conn.execute(
                'ALTER TABLE songs ADD COLUMN pitch_semitones INTEGER DEFAULT 0')
        except sqlite3.OperationalError:
            pass  # already exists (pre-existing DB from before this column)
        self._conn.commit()

    def load(self, path):
        row = self._conn.execute(
            'SELECT speed, pitch_lock, loop_a, loop_b, position, eq_preset, '
            'pitch_semitones FROM songs WHERE path=?',
            (path,)).fetchone()
        if row:
            return {'speed': row[0], 'pitch_lock': bool(row[1]),
                    'loop_a': row[2], 'loop_b': row[3], 'position': row[4],
                    'eq_preset': row[5], 'pitch_semitones': row[6] or 0}
        return None

    def save(self, path, speed, pitch_lock, loop_a, loop_b, position,
              eq_preset=None, pitch_semitones=0):
        self._conn.execute('''INSERT INTO songs (path, speed, pitch_lock, loop_a, loop_b,
            position, eq_preset, pitch_semitones)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
            speed=excluded.speed, pitch_lock=excluded.pitch_lock,
            loop_a=excluded.loop_a, loop_b=excluded.loop_b,
            position=excluded.position, eq_preset=excluded.eq_preset,
            pitch_semitones=excluded.pitch_semitones''',
            (path, speed, int(pitch_lock), loop_a, loop_b, position, eq_preset,
             pitch_semitones))
        self._conn.commit()


# ─── Audio Loading ───────────────────────────────────────────────────

def _audio_cache_dir():
    d = os.path.join(_data_dir(), 'audio_cache')
    os.makedirs(d, exist_ok=True)
    return d


def _audio_cache_key(source_path):
    """Cache key: hashes absolute path + mtime + size. Any of those change → miss."""
    import hashlib
    try:
        st = os.stat(source_path)
        raw = f'{os.path.abspath(source_path)}|{st.st_mtime_ns}|{st.st_size}'
        return hashlib.sha1(raw.encode()).hexdigest()
    except OSError:
        return None


def load_audio(path, sr=DEFAULT_SR):
    # Cache lookup: fast path avoids ffmpeg + wav decode on re-open
    key = _audio_cache_key(path)
    if key:
        cache_file = os.path.join(_audio_cache_dir(), f'{key}.npy')
        if os.path.isfile(cache_file):
            try:
                audio = np.load(cache_file)
                return audio, sr
            except Exception:
                # Corrupt cache — fall through and re-decode
                try:
                    os.remove(cache_file)
                except OSError:
                    pass

    temp_wav = os.path.join(_temp_dir(), '_input.wav')
    ffmpeg = _find_tool('ffmpeg')
    cmd = [ffmpeg, '-y', '-i', path,
           '-ar', str(sr), '-ac', '1', '-sample_fmt', 's16',
           '-loglevel', 'error', temp_wav]
    subprocess.run(cmd, check=True, capture_output=True, text=True, env=_external_env())
    with wave.open(temp_wav, 'r') as wf:
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    # Normalize to NORM_TARGET_DB so hot mixes have headroom for EQ/processing
    peak = np.max(np.abs(audio))
    if peak > 1e-6:
        target = 10 ** (NORM_TARGET_DB / 20.0)
        audio *= target / peak

    # Save to cache (best-effort — a failed save must not break the load)
    if key:
        try:
            np.save(os.path.join(_audio_cache_dir(), f'{key}.npy'), audio)
        except Exception:
            pass
    return audio, sr


def _lookahead_limit(audio, sr=DEFAULT_SR, threshold=0.90,
                      lookahead_ms=5.0, release_ms=50.0):
    """Look-ahead peak limiter.

    Peeks `lookahead_ms` ahead of each sample; when an incoming peak exceeds
    `threshold`, gain reduction ramps in *before* the peak arrives so transients
    are attenuated smoothly instead of hard-clipped.

    Runs in vectorized C via scipy.ndimage.maximum_filter1d + scipy.signal.lfilter.
    """
    from scipy.ndimage import maximum_filter1d
    from scipy.signal import lfilter

    n = len(audio)
    lookahead = max(1, int(lookahead_ms * sr / 1000))
    # Rolling max looking AHEAD of the current sample
    abs_audio = np.abs(audio)
    rolling_max = maximum_filter1d(
        abs_audio, size=lookahead, mode='constant', cval=0.0,
        origin=-(lookahead // 2))
    # Per-sample target: reduce so the peak within the window sits at threshold
    target_gain = np.where(
        rolling_max > threshold,
        threshold / np.maximum(rolling_max, 1e-8),
        1.0,
    ).astype(np.float32)
    # Exponential-release IIR filter (instant attack achieved via min() below)
    release_alpha = float(np.exp(-1.0 / max(1.0, release_ms * sr / 1000.0)))
    b = np.array([1.0 - release_alpha], dtype=np.float32)
    a = np.array([1.0, -release_alpha], dtype=np.float32)
    zi = np.array([release_alpha * target_gain[0]], dtype=np.float32)
    smoothed, _ = lfilter(b, a, target_gain, zi=zi)
    # Instant attack, slow release: gain follows target down but rises slowly
    final_gain = np.minimum(smoothed.astype(np.float32), target_gain)
    return (audio * final_gain).astype(np.float32)


def _soft_clip_inplace(x, knee=0.7, ceiling=0.95):
    """Smoothly compress samples above `knee` toward `ceiling`. In-place."""
    mag = np.abs(x)
    over = mag > knee
    if not np.any(over):
        return
    excess = mag[over] - knee
    max_excess = 1.0 - knee
    compressed = knee + (ceiling - knee) * np.tanh(excess / max_excess * 1.5)
    x[over] = np.sign(x[over]) * compressed


# ─── WSOLA Time-Stretch ──────────────────────────────────────────────

def wsola_stretch(audio, speed, sr=DEFAULT_SR):
    if abs(speed - 1.0) < 0.01:
        return audio.copy()
    # At slow speeds, a longer analysis window gives each grain more
    # context, reducing the grainy/distorted character a short window
    # produces under heavy overlap. Normal-and-above keeps the shorter
    # window (better transient response, cheaper to compute).
    if speed < 0.65:
        frame_len = 4096
        hop_s = frame_len // 4        # 75% overlap
    else:
        frame_len = 2048
        hop_s = frame_len // 2        # 50% overlap
    search = frame_len // 4
    overlap = frame_len - hop_s
    ana_step = max(1, int(hop_s * speed))
    window = np.hanning(frame_len).astype(np.float32)
    n_out = int(len(audio) / speed)
    n_frames = max(1, (n_out - overlap) // hop_s)
    tail_pad = frame_len + search + 1
    if len(audio) >= tail_pad:
        padded = np.pad(audio, (0, tail_pad), mode='reflect')
    else:
        padded = np.pad(audio, (0, tail_pad))
    padded_max_start = len(padded) - frame_len
    output = np.zeros(n_out + frame_len, dtype=np.float32)
    win_sum = np.zeros(n_out + frame_len, dtype=np.float32)
    ideal = 0
    for i in range(n_frames):
        syn_pos = i * hop_s
        if syn_pos + frame_len > len(output):
            break
        if i == 0:
            best = 0
        else:
            lo = max(0, ideal - search)
            hi = min(padded_max_start, ideal + search)
            target = max(0, min(ideal, padded_max_start))
            if lo >= hi:
                best = target
            else:
                ref = output[syn_pos:syn_pos + overlap]
                ref_energy = float(np.dot(ref, ref))
                if ref_energy < 1e-8:
                    best = target
                else:
                    search_buf = padded[lo:hi + overlap]
                    if len(search_buf) >= len(ref) > 0:
                        corr = np.correlate(search_buf, ref, mode='valid')
                        # Normalize by each candidate window's own energy so
                        # the search picks the best-ALIGNED splice point, not
                        # just the loudest one. Raw correlation is energy-
                        # biased — it can grab a high-energy-but-phase-
                        # mismatched frame, and summing that against the
                        # existing overlap produces comb-filtering that's
                        # audible as a faint echo/doubling, especially on
                        # reverb tails and especially at slow speeds where
                        # more overlap sums more copies together.
                        win_len = len(ref)
                        sq = search_buf.astype(np.float64) ** 2
                        csum = np.concatenate(([0.0], np.cumsum(sq)))
                        local_energy = csum[win_len:] - csum[:-win_len]
                        norm = np.sqrt(local_energy * ref_energy) + 1e-8
                        best = lo + int(np.argmax(corr / norm))
                    else:
                        best = target
        if best + frame_len > len(padded):
            best = padded_max_start
        frame = padded[best:best + frame_len] * window
        output[syn_pos:syn_pos + frame_len] += frame
        win_sum[syn_pos:syn_pos + frame_len] += window
        ideal += ana_step
    mask = win_sum > 1e-3
    output[mask] /= win_sum[mask]
    output = output[:n_out]
    in_rms = np.sqrt(np.mean(audio[:min(len(audio), n_out)] ** 2))
    out_rms = np.sqrt(np.mean(output ** 2))
    if out_rms > 1e-8 and in_rms > 1e-8:
        output *= in_rms / out_rms
    peak = np.max(np.abs(output))
    if peak > 0.98:
        output *= 0.98 / peak
    return output


def resample_stretch(audio, speed):
    if abs(speed - 1.0) < 0.01:
        return audio.copy()
    n_out = max(1, int(len(audio) / speed))
    return np.interp(
        np.linspace(0, 1, n_out),
        np.linspace(0, 1, len(audio)), audio
    ).astype(np.float32)


def _semitone_ratio(semitones):
    return 2.0 ** (semitones / 12.0)


def _apply_eq_fft(audio, sr, bands, da_filter=False):
    n = len(audio)
    has_bands = any(abs(db) >= 0.1 for _, db in bands)
    if not has_bands and not da_filter:
        return audio.copy()
    n_fft = 1
    while n_fft < n:
        n_fft <<= 1
    X = np.fft.rfft(audio, n_fft)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    freqs_safe = np.where(freqs > 0, freqs, 1.0)
    gain_db = np.zeros(len(freqs), dtype=np.float32)
    for center, db in bands:
        if abs(db) < 0.1:
            continue
        log_ratio = np.log2(freqs_safe / center)
        bell = np.exp(-0.5 * (log_ratio / 1.0) ** 2)
        bell[0] = 0
        gain_db += db * bell
    X *= 10 ** (gain_db / 20.0)
    if da_filter:
        # Emulate a classic DAC's analog reconstruction filter: a single-pole
        # RC-style lowpass (-3dB at the cutoff, ~-6dB/octave beyond) that
        # softens harsh digital top-end rather than a brick-wall digital cut.
        mag = 1.0 / np.sqrt(1.0 + (freqs / DA_FILTER_CUTOFF_HZ) ** 2)
        X *= mag
    result = np.fft.irfft(X, n_fft)[:n].astype(np.float32)
    peak = np.max(np.abs(result))
    if peak > 0.98:
        result *= 0.98 / peak
    return result


def _compress(audio, threshold_db=-18, ratio=2.5, sr=DEFAULT_SR):
    block = 512
    n = len(audio)
    n_blocks = (n + block - 1) // block
    threshold = 10 ** (threshold_db / 20.0)
    pad = n_blocks * block - n
    if pad > 0:
        padded = np.concatenate([audio, np.zeros(pad, dtype=audio.dtype)])
    else:
        padded = audio
    blocks = padded.reshape(n_blocks, block)
    rms_blocks = np.sqrt(np.mean(blocks * blocks, axis=1))
    gains = np.ones(n_blocks, dtype=np.float32)
    over = rms_blocks > threshold
    if np.any(over):
        over_db = 20.0 * np.log10(rms_blocks[over] / threshold)
        gains[over] = 10 ** (-over_db * (1.0 - 1.0 / ratio) / 20.0)
    # IIR one-pole smoothing (vectorized via cumulative product/sum)
    a = 0.85
    b = 0.15
    smoothed = np.empty(n_blocks, dtype=np.float32)
    smoothed[0] = gains[0]
    for i in range(1, n_blocks):
        smoothed[i] = b * gains[i] + a * smoothed[i - 1]
    centers = np.arange(n_blocks) * block + block // 2
    sample_gains = np.interp(np.arange(n), centers, smoothed).astype(np.float32)
    result = audio * sample_gains
    in_rms = np.sqrt(np.mean(audio * audio))
    out_rms = np.sqrt(np.mean(result * result))
    if out_rms > 1e-8 and in_rms > 1e-8:
        result *= in_rms / out_rms
    peak = np.max(np.abs(result))
    if peak > 0.98:
        result *= 0.98 / peak
    return result


# ─── Audio Engine ────────────────────────────────────────────────────

class AudioEngine:
    def __init__(self):
        self.original = None
        self.stretched = None
        self.processed = None
        self.sr = DEFAULT_SR
        self.eq_bands = [(f, 0) for f in EQ_FREQS]
        self.reference_mode = False
        self.ref_position = 0
        self.speed = 1.0
        self.pitch_lock = True
        self.pitch_semitones = 0
        self.volume = 0.5
        self.playing = False
        self.position = 0
        self.loop_a = None
        self.loop_b = None
        self.file_path = None
        self.stream = None
        self._on_end = None
        self._eq_busy = False
        self._eq_pending = False
        self._eq_error = None

    @property
    def loaded(self):
        return self.original is not None

    @property
    def duration(self):
        return len(self.original) / self.sr if self.original is not None else 0

    def load(self, path):
        self.original, self.sr = load_audio(path, self.sr)
        self.file_path = path
        self.loop_a = None
        self.loop_b = None
        self.position = 0
        self.ref_position = 0
        self.reference_mode = False
        self.stretched = None
        self.processed = None

    def unload(self):
        self.stop()
        self.original = None
        self.stretched = None
        self.processed = None
        self.file_path = None
        self.loop_a = None
        self.loop_b = None
        self.position = 0
        self.ref_position = 0
        self.reference_mode = False

    def process(self, speed=None, pitch_lock=None, pitch_semitones=None):
        if speed is not None:
            self.speed = speed
        if pitch_lock is not None:
            self.pitch_lock = pitch_lock
        if pitch_semitones is not None:
            self.pitch_semitones = pitch_semitones
        if self.original is None:
            return
        pr = _semitone_ratio(self.pitch_semitones)
        no_shift = abs(pr - 1.0) < 1e-6
        # Transpose is layered on top of the existing speed/pitch_lock
        # behavior via one resample pass (sets pitch) + one WSOLA pass
        # (restores whatever duration the speed setting already dictates)
        # — the same resample-then-WSOLA-restore trick used for pitch_lock
        # itself, just with the ratios solved so the no-transpose case
        # (pitch_semitones == 0) reduces exactly to the original single-call
        # behavior below, unchanged.
        if self.pitch_lock:
            if no_shift:
                self.stretched = wsola_stretch(self.original, self.speed, self.sr)
            else:
                shifted = resample_stretch(self.original, pr)
                self.stretched = wsola_stretch(shifted, self.speed / pr, self.sr)
        else:
            if no_shift:
                self.stretched = resample_stretch(self.original, self.speed)
            else:
                shifted = resample_stretch(self.original, self.speed * pr)
                self.stretched = wsola_stretch(shifted, 1.0 / pr, self.sr)
        self._finalize()

    def _finalize(self):
        if self.stretched is None:
            return
        # Chain: compress → EQ → D/A reconstruction filter → look-ahead limit
        # Compression before EQ tames dynamics so EQ boosts don't push peaks
        # further into the limiter than they need to. The D/A filter runs
        # unconditionally on every song — it's baked-in processing, not a
        # user preset.
        audio = _compress(self.stretched, sr=self.sr)
        audio = _apply_eq_fft(audio, self.sr, self.eq_bands, da_filter=True)
        self.processed = _lookahead_limit(audio, sr=self.sr)

    def apply_eq(self, bands):
        """Reprocess with new EQ bands. Silences output during the recompute
        (position stays put) so users hear no click at the buffer swap; work
        happens on a background thread so the UI never freezes.

        Clicking through several presets fast used to spawn one thread per
        click, all racing to write self.processed with no ordering
        guarantee — last to finish won, not last clicked, and an exception
        in any of them died silently. This coalesces to a single worker:
        a click while one is already running just updates the pending
        target, and the worker re-runs once more for the latest request
        instead of piling up N full reprocess passes."""
        self.eq_bands = bands
        if self.stretched is None:
            return
        self._eq_pending = True
        if self._eq_busy:
            return
        self._eq_busy = True
        self._eq_was_playing = self.playing
        self.playing = False  # callback outputs silence — position frozen
        self._run_eq_worker()

    def _run_eq_worker(self):
        self._eq_pending = False
        def work():
            try:
                self._finalize()
            except Exception as e:
                self._eq_error = str(e)
            finally:
                if self._eq_pending:
                    self._run_eq_worker()
                else:
                    self._eq_busy = False
                    if self._eq_was_playing:
                        self.playing = True
        threading.Thread(target=work, daemon=True).start()

    def play(self):
        if self.processed is None:
            return
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass
        self.playing = True
        self.stream = sd.OutputStream(
            samplerate=self.sr, channels=1, dtype='float32',
            callback=self._callback, blocksize=512,
            latency='low')
        self.stream.start()

    def pause(self):
        self.playing = False

    def stop(self):
        self.playing = False
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.position = 0

    def seek(self, ratio):
        ratio = max(0, min(ratio, 1.0))
        if self.reference_mode and self.original is not None:
            self.ref_position = int(ratio * len(self.original))
        elif self.processed is not None:
            self.position = int(ratio * len(self.processed))

    def seek_relative(self, seconds):
        if self.reference_mode and self.original is not None:
            self.ref_position = max(0, min(
                self.ref_position + int(seconds * self.sr),
                len(self.original) - 1))
        elif self.processed is not None:
            self.position = max(0, min(
                self.position + int(seconds * self.sr),
                len(self.processed) - 1))

    def pos_ratio(self):
        if self.reference_mode:
            if self.original is None or len(self.original) == 0:
                return 0.0
            return self.ref_position / len(self.original)
        if self.processed is None or len(self.processed) == 0:
            return 0.0
        return self.position / len(self.processed)

    def current_time(self):
        if self.reference_mode:
            return self.ref_position / self.sr if self.original is not None else 0.0
        return self.position / self.sr if self.processed is not None else 0.0

    def total_time(self):
        if self.reference_mode:
            return len(self.original) / self.sr if self.original is not None else 0.0
        return len(self.processed) / self.sr if self.processed is not None else 0.0

    def toggle_reference(self):
        if self.original is None:
            return
        if not self.reference_mode:
            if self.processed is not None and len(self.processed) > 0:
                ratio = self.position / len(self.processed)
                self.ref_position = int(ratio * len(self.original))
            self.reference_mode = True
        else:
            if self.original is not None and len(self.original) > 0 and self.processed is not None:
                ratio = self.ref_position / len(self.original)
                self.position = int(ratio * len(self.processed))
            self.reference_mode = False

    def _callback(self, outdata, frames, time_info, status):
        if not self.playing:
            outdata[:] = 0
            return
        if self.reference_mode:
            buf = self.original
        else:
            buf = self.processed
        if buf is None:
            outdata[:] = 0
            return
        pos = self.ref_position if self.reference_mode else self.position
        # A-B loop
        if self.loop_a is not None and self.loop_b is not None and self.original is not None:
            if self.reference_mode:
                la, lb = self.loop_a, self.loop_b
            else:
                orig_len = len(self.original)
                proc_len = len(self.processed)
                la = int(self.loop_a / orig_len * proc_len)
                lb = int(self.loop_b / orig_len * proc_len)
            if pos >= lb:
                pos = la
        end = pos + frames
        if end > len(buf):
            rem = len(buf) - pos
            if rem > 0:
                outdata[:rem, 0] = buf[pos:pos + rem] * self.volume
                _soft_clip_inplace(outdata[:rem, 0])
            outdata[rem:] = 0
            self.playing = False
            if self.reference_mode:
                self.ref_position = 0
            else:
                self.position = 0
            if self._on_end:
                self._on_end()
            return
        outdata[:, 0] = buf[pos:end] * self.volume
        _soft_clip_inplace(outdata[:, 0])
        if self.reference_mode:
            self.ref_position = end
        else:
            self.position = end


# ─── Spinning Pie ────────────────────────────────────────────────────

class ProgressIndicator(tk.Label):
    """Busy/loading indicator that plays an animated GIF's own frames —
    no hand-drawn shape math. Swap the visual by pointing at a different
    GIF; this class only handles frame-cycling and playback timing."""

    def __init__(self, parent, gif_path, fps=8, scale=1, **kwargs):
        kwargs.setdefault('bg', WAVE_BG)
        kwargs.setdefault('bd', 0)
        kwargs.setdefault('highlightthickness', 0)
        super().__init__(parent, **kwargs)
        self._frames = []
        i = 0
        while True:
            try:
                frame = tk.PhotoImage(file=gif_path, format=f'gif -index {i}')
                if scale > 1:
                    frame = frame.subsample(scale, scale)
                self._frames.append(frame)
                i += 1
            except tk.TclError:
                break
        self._frame_idx = 0
        self._delay = max(20, int(1000 / fps))
        self.running = False
        if self._frames:
            self.config(image=self._frames[0])

    def start(self):
        if not self._frames:
            return
        self.running = True
        self._animate()

    def stop(self):
        self.running = False
        if self._frames:
            self.config(image=self._frames[0])
            self._frame_idx = 0

    def _animate(self):
        if not self.running or not self._frames:
            return
        self.config(image=self._frames[self._frame_idx])
        self._frame_idx = (self._frame_idx + 1) % len(self._frames)
        self.after(self._delay, self._animate)


# ─── Segment palette (shared with plugins) ──────────────────────────

SEGMENT_COLORS = {
    'A': '#e57373', 'B': '#64b5f6', 'C': '#81c784',
    'D': '#ffb74d', 'E': '#ba68c8', 'F': '#4dd0e1', 'G': '#fff176',
}


# ─── Plugin loader ──────────────────────────────────────────────────

def _plugins_dir():
    d = os.path.join(_data_dir(), 'plugins')
    os.makedirs(d, exist_ok=True)
    return d


def _plugin_state_path():
    return os.path.join(_data_dir(), 'plugin_state.json')


def _load_plugin_state():
    """Maps plugin filename -> enabled bool. Missing entries default enabled."""
    try:
        with open(_plugin_state_path()) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_plugin_state(state):
    try:
        with open(_plugin_state_path(), 'w') as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def _scan_plugin_metadata():
    """List every .py file in the plugins folder with its name/description/
    enabled state, WITHOUT importing/executing any of them — so this is safe
    to call even for plugins the user has disabled. Uses static AST parsing
    to read top-level PLUGIN_NAME / PLUGIN_DESCRIPTION string assignments."""
    d = _plugins_dir()
    state = _load_plugin_state()
    plugins = []
    for fname in sorted(os.listdir(d)):
        if not fname.endswith('.py') or fname.startswith('_'):
            continue
        path = os.path.join(d, fname)
        name = fname[:-3]
        description = "No description provided."
        try:
            import ast
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                tree = ast.parse(f.read(), filename=fname)
            for node in tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                        and isinstance(node.targets[0], ast.Name) \
                        and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, str):
                    if node.targets[0].id == 'PLUGIN_NAME':
                        name = node.value.value
                    elif node.targets[0].id == 'PLUGIN_DESCRIPTION':
                        description = node.value.value
        except Exception:
            pass
        plugins.append({
            'fname': fname, 'name': name, 'description': description,
            'enabled': state.get(fname, True),
        })
    return plugins


def load_plugins(app):
    """Discover and register enabled plugins from
    ~/Library/Application Support/noteXnote/plugins/. A plugin is a single
    .py file that exports a `register(app)` function. Plugins the user has
    disabled (via Plugins → Manage Plugins…) are skipped entirely — never
    imported, so their code never runs."""
    import importlib.util
    d = _plugins_dir()
    state = _load_plugin_state()
    loaded = []
    # Don't litter the user-facing plugins folder with a __pycache__ dir —
    # it reads as clutter/breakage to someone just browsing their plugins.
    prev_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        for fname in sorted(os.listdir(d)):
            if not fname.endswith('.py') or fname.startswith('_'):
                continue
            if not state.get(fname, True):
                continue  # disabled — skip entirely, no import
            path = os.path.join(d, fname)
            try:
                spec = importlib.util.spec_from_file_location(
                    f'notexnote_plugin_{fname[:-3]}', path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, 'register') and callable(mod.register):
                    mod.register(app)
                    loaded.append(getattr(mod, 'PLUGIN_NAME', fname[:-3]))
            except Exception as e:
                print(f'[{APP_NAME}] plugin {fname} failed: {e}')
    finally:
        sys.dont_write_bytecode = prev_dont_write
    return loaded


# ─── Hardware-style EQ Fader ────────────────────────────────────────

class FaderSlider(tk.Canvas):
    """Mixing-console-style fader (vertical or horizontal) on a Canvas.

    Args:
        variable      : tk.Variable holding the current value
        from_ / to    : range endpoints (`from_` is the "high" end for vertical,
                        "right" end for horizontal — matches ttk.Scale semantics)
        length        : long-axis size in pixels
        orient        : 'vertical' | 'horizontal'
        accent        : indicator stripe color on the fader cap
        snap_to       : optional value to snap to when the user releases nearby;
                        pass None to disable (e.g. volume sliders)
        tick_values   : list of values where tick marks are drawn. If None, no ticks.
        reset_value   : double-click resets to this. Defaults to `snap_to` or 0.
        on_change     : optional callback fired on click/drag
    """

    def __init__(self, parent, variable, from_=12, to=-12, length=65,
                 orient='vertical', accent='#ffcc00', snap_to=None,
                 tick_values=None, reset_value=None, on_change=None,
                 **kwargs):
        self.orient = 'horizontal' if orient.startswith('h') else 'vertical'
        thickness = 34
        if self.orient == 'vertical':
            w_px, h_px = thickness, length
        else:
            w_px, h_px = length, thickness
        kwargs.setdefault('highlightthickness', 0)
        kwargs.setdefault('bd', 0)
        try:
            kwargs.setdefault('bg', parent.cget('bg'))
        except Exception:
            kwargs.setdefault('bg', '#2a2a2a')
        super().__init__(parent, width=w_px, height=h_px, **kwargs)
        # Current widget dimensions — kept up to date by <Configure>
        self._cw = w_px
        self._ch = h_px
        self.var = variable
        self.from_ = float(from_)
        self.to = float(to)
        self.on_change = on_change
        self.accent = accent
        self.snap_to = snap_to
        self.tick_values = tick_values
        self.reset_value = reset_value if reset_value is not None else (snap_to or 0)
        self.length = length
        self.thickness = thickness
        # Long-axis inset for endpoints; cross-axis center
        self.pad = 8
        self.axis_low = self.pad             # top (vertical) or left (horizontal)
        self.axis_high = length - self.pad   # bottom or right
        self.cross_center = thickness // 2
        # Thumb (cap) dimensions — long side is the "narrow" side of the cap.
        # Larger cap sizes give the hardware-fader silhouette from faders.png.
        self.cap_long = 22   # cap thickness along track axis
        self.cap_short = 30  # cap width perpendicular
        self._dragging = False
        self.var.trace_add('write', lambda *_: self._draw_thumb())
        self.bind('<Button-1>', self._on_click)
        self.bind('<B1-Motion>', self._on_drag)
        self.bind('<ButtonRelease-1>', self._on_release)
        self.bind('<Double-Button-1>', self._reset)
        self.bind('<Configure>', self._on_configure)
        self._draw_static()
        self._draw_thumb()

    def _on_configure(self, event):
        # Track stretches with the widget along its long axis
        new_length = event.width if self.orient == 'horizontal' else event.height
        if new_length > 20 and abs(new_length - self.length) > 1:
            self.length = new_length
            self.axis_high = new_length - self.pad
            self._cw = event.width
            self._ch = event.height
            self._draw_static()
            self._draw_thumb()

    # ── value / axis conversion ──
    # Horizontal: left = low, right = high
    # Vertical  : top  = high, bottom = low  (mixer-desk convention)

    def _val_to_axis(self, v):
        lo = min(self.from_, self.to)
        hi = max(self.from_, self.to)
        v = max(min(v, hi), lo)
        if self.orient == 'horizontal':
            frac = (v - lo) / (hi - lo)
        else:
            frac = (hi - v) / (hi - lo)
        return self.axis_low + frac * (self.axis_high - self.axis_low)

    def _axis_to_val(self, a):
        a = max(self.axis_low, min(self.axis_high, a))
        frac = (a - self.axis_low) / (self.axis_high - self.axis_low)
        lo = min(self.from_, self.to)
        hi = max(self.from_, self.to)
        if self.orient == 'horizontal':
            return lo + frac * (hi - lo)
        return hi - frac * (hi - lo)

    def _event_axis(self, event):
        return event.y if self.orient == 'vertical' else event.x

    # ── drawing ──

    def _line(self, axis_pos, cross_span, color, width, tags):
        """Draw a tick line perpendicular to the track axis."""
        c = self.cross_center
        if self.orient == 'vertical':
            self.create_line(c - cross_span, axis_pos, c + cross_span, axis_pos,
                             fill=color, width=width, tags=tags)
        else:
            self.create_line(axis_pos, c - cross_span, axis_pos, c + cross_span,
                             fill=color, width=width, tags=tags)

    _BRUSHED_SHADES = ('#8f8f96', '#96969d', '#9d9da4', '#a5a5ac',
                        '#adadb4', '#9898a0', '#a0a0a7')

    def _draw_static(self):
        self.delete('static')
        c = self.cross_center
        w = self._cw
        h = self._ch
        # Brushed aluminum panel — solid base then fine grain streaks
        # perpendicular to the fader's travel (grain runs across, like real hardware).
        self.create_rectangle(0, 0, w, h,
                              fill='#a0a0a7', outline='#3a3a40', width=1,
                              tags='static')
        import random
        rng = random.Random(42)  # deterministic pattern
        n_shades = len(self._BRUSHED_SHADES)
        if self.orient == 'vertical':
            # Streaks run horizontally across a vertical fader
            for y in range(1, h - 1):
                self.create_line(1, y, w - 1, y,
                                 fill=self._BRUSHED_SHADES[rng.randrange(n_shades)],
                                 tags='static')
        else:
            # Streaks run vertically down a horizontal fader
            for x in range(1, w - 1):
                self.create_line(x, 1, x, h - 1,
                                 fill=self._BRUSHED_SHADES[rng.randrange(n_shades)],
                                 tags='static')
        # Panel highlight (top / left edge) and shadow (bottom / right)
        self.create_line(1, 1, w - 1, 1, fill='#d0d0d5', tags='static')
        self.create_line(1, 1, 1, h - 1, fill='#d0d0d5', tags='static')
        self.create_line(w - 2, 1, w - 2, h - 1, fill='#5a5a60', tags='static')
        self.create_line(1, h - 2, w - 1, h - 2, fill='#5a5a60', tags='static')
        # Recessed track cutout — dark, with inner shadow
        if self.orient == 'vertical':
            self.create_rectangle(c - 3, self.axis_low - 2, c + 4, self.axis_high + 2,
                                  fill='#151519', outline='#3a3a3f',
                                  tags='static')
            # Inner shadow on the top of the slot
            self.create_line(c - 2, self.axis_low - 1, c + 3, self.axis_low - 1,
                             fill='#000', tags='static')
        else:
            self.create_rectangle(self.axis_low - 2, c - 3, self.axis_high + 2, c + 4,
                                  fill='#151519', outline='#3a3a3f',
                                  tags='static')
            self.create_line(self.axis_low - 1, c - 2, self.axis_low - 1, c + 3,
                             fill='#000', tags='static')
        # Tick marks etched into the aluminum
        if self.tick_values:
            for tv in self.tick_values:
                axis_pos = self._val_to_axis(tv)
                is_center = self.snap_to is not None and abs(tv - self.snap_to) < 1e-6
                span = 10 if is_center else 6
                color = '#1a1a1e' if is_center else '#4a4a50'
                width = 2 if is_center else 1
                self._line(axis_pos, span, color, width, 'static')

    def _draw_thumb(self):
        self.delete('thumb')
        try:
            v = float(self.var.get())
        except Exception:
            v = 0
        a = self._val_to_axis(v)
        c = self.cross_center
        long_half = self.cap_long // 2
        short_half = self.cap_short // 2
        if self.orient == 'vertical':
            x0, x1 = c - short_half, c + short_half
            y0, y1 = a - long_half, a + long_half
        else:
            x0, x1 = a - long_half, a + long_half
            y0, y1 = c - short_half, c + short_half

        # Drop shadow (softer, offset diagonally like faders.png)
        self.create_oval(x0 + 2, y0 + 3, x1 + 2, y1 + 3,
                         fill='#000', outline='', tags='thumb')
        # Main pill body — dark plastic
        self.create_oval(x0, y0, x1, y1,
                         fill='#28282e', outline='#0a0a0d', width=1, tags='thumb')
        # Top-lit highlight (upper crescent) — offset oval clipped by main
        # Uses a smaller oval at the top edge to fake a gloss highlight
        if self.orient == 'vertical':
            self.create_arc(x0 + 1, y0 + 1, x1 - 1, y0 + long_half + 4,
                            start=15, extent=150,
                            style='chord', fill='#4e4e58', outline='',
                            tags='thumb')
            # Deep shadow crescent at the bottom
            self.create_arc(x0 + 1, y1 - long_half - 4, x1 - 1, y1 - 1,
                            start=195, extent=150,
                            style='chord', fill='#0f0f13', outline='',
                            tags='thumb')
            # Recessed finger slot across the middle
            slot_x0, slot_x1 = x0 + 3, x1 - 3
            slot_y0, slot_y1 = a - 2, a + 2
            self.create_oval(slot_x0, slot_y0, slot_x1, slot_y1,
                             fill='#050507', outline='#000', width=1,
                             tags='thumb')
            # Accent LED stripe
            self.create_line(slot_x0 + 2, a, slot_x1 - 2, a,
                             fill=self.accent, width=2, tags='thumb')
        else:
            # Left crescent (lit)
            self.create_arc(x0 + 1, y0 + 1, x0 + long_half * 2 + 4, y1 - 1,
                            start=75, extent=150,
                            style='chord', fill='#4e4e58', outline='',
                            tags='thumb')
            # Right crescent (shadow)
            self.create_arc(x1 - long_half * 2 - 4, y0 + 1, x1 - 1, y1 - 1,
                            start=-105, extent=150,
                            style='chord', fill='#0f0f13', outline='',
                            tags='thumb')
            slot_x0, slot_x1 = a - 2, a + 2
            slot_y0, slot_y1 = y0 + 3, y1 - 3
            self.create_oval(slot_x0, slot_y0, slot_x1, slot_y1,
                             fill='#050507', outline='#000', width=1,
                             tags='thumb')
            self.create_line(a, slot_y0 + 2, a, slot_y1 - 2,
                             fill=self.accent, width=2, tags='thumb')

    # ── input ──

    def _apply(self, v):
        # Snap tolerance scales with the slider's own range (~1.5% of span)
        # instead of a fixed 0.8 — on Speed's 0.25-2.0 range (span 1.75)
        # that old fixed value snapped almost the ENTIRE slider to 1.0x,
        # making the slider itself nearly non-functional.
        if self.snap_to is not None:
            span = abs(self.to - self.from_)
            tol = span * 0.015
            if abs(v - self.snap_to) < tol:
                v = self.snap_to
        self.var.set(v)
        if self.on_change:
            self.on_change(v)

    def _on_click(self, event):
        self._dragging = True
        self._apply(self._axis_to_val(self._event_axis(event)))

    def _on_drag(self, event):
        if not self._dragging:
            return
        self._apply(self._axis_to_val(self._event_axis(event)))

    def _on_release(self, event):
        self._dragging = False

    def _reset(self, event):
        self.var.set(self.reset_value)
        if self.on_change:
            self.on_change(self.reset_value)


# ─── Waveform View ──────────────────────────────────────────────────

SEGMENT_STRIP_H = 22


class WaveformView(tk.Canvas):
    def __init__(self, parent, engine, **kwargs):
        kwargs.setdefault('bg', WAVE_BG)
        kwargs.setdefault('highlightthickness', 0)
        super().__init__(parent, **kwargs)
        self.engine = engine
        self._peaks = None
        self._rms = None
        self._head_id = None
        self._busy = False
        self.segments = []  # list of {'start','end','tag','name'}
        self.on_segment_click = None  # callback(segment_dict)
        self.on_seek = None  # callback() fired after a click/drag/wheel seek
        self.bind('<Configure>', lambda e: self._draw())
        self.bind('<Button-1>', self._click)
        self.bind('<B1-Motion>', self._drag)
        self.bind('<MouseWheel>', self._wheel)

    def set_busy(self, busy):
        """Blank the drawn waveform while the busy spinner is overlaid on
        top of us. The spinner Label is an opaque widget, not a real
        transparent overlay, so whenever it sits on top of an already-drawn
        waveform it cuts a flat-colored rectangle out of the drawing —
        visible as a "frame"/border around the spinner. Blanking first
        recreates the one case that already looks right (a fresh launch,
        nothing drawn yet), instead of trying to pixel-match a Label's bg
        against whatever the waveform happens to be drawing underneath it."""
        self._busy = busy
        self._draw()

    def clear(self):
        self._peaks = None
        self._rms = None
        self.segments = []
        self._draw()

    def set_audio(self, audio):
        n = max(self.winfo_width(), 400)
        chunk = max(1, len(audio) // n)
        trim = len(audio) - len(audio) % chunk
        reshaped = audio[:trim].reshape(-1, chunk)
        self._peaks = np.max(np.abs(reshaped), axis=1)
        self._rms = np.sqrt(np.mean(reshaped ** 2, axis=1))
        self._draw()

    def set_segments(self, segments):
        self.segments = segments or []
        self._draw()

    def _draw(self):
        self.delete('all')
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10 or h < 10:
            return
        if self._busy:
            return  # blank canvas — see set_busy()
        # Reserve top strip for segments if any
        strip_h = SEGMENT_STRIP_H if self.segments else 0
        wave_top = strip_h
        wave_h = h - strip_h
        cy = wave_top + wave_h / 2
        for i in range(1, 10):
            x = w * i / 10
            self.create_line(x, wave_top, x, h, fill=GRID, width=1)
        self.create_line(0, cy, w, cy, fill=GRID, width=1)
        if self._peaks is None:
            self.create_text(w // 2, cy, text="No audio loaded",
                             fill='#666', font=('Helvetica', 14))
            return
        peaks = np.interp(np.linspace(0, 1, w), np.linspace(0, 1, len(self._peaks)), self._peaks)
        rms = np.interp(np.linspace(0, 1, w), np.linspace(0, 1, len(self._rms)), self._rms)
        # Segment strip
        if self.segments and self.engine.original is not None:
            dur = len(self.engine.original) / self.engine.sr
            if dur > 0:
                for seg in self.segments:
                    x0 = seg['start'] / dur * w
                    x1 = seg['end'] / dur * w
                    color = SEGMENT_COLORS.get(seg['tag'], '#888')
                    self.create_rectangle(x0, 0, x1, strip_h,
                                          fill=color, outline='#111', width=1)
                    if x1 - x0 > 32:
                        self.create_text((x0 + x1) / 2, strip_h / 2,
                                         text=seg['name'],
                                         fill='#111', anchor='center',
                                         font=('Helvetica', 9, 'bold'))
        # Loop region
        if (self.engine.loop_a is not None and self.engine.loop_b is not None
                and self.engine.original is not None):
            la = self.engine.loop_a / len(self.engine.original) * w
            lb = self.engine.loop_b / len(self.engine.original) * w
            self.create_rectangle(la, wave_top, lb, h, fill=LOOP_FILL,
                                  stipple='gray25', outline='')
            self.create_line(la, wave_top, la, h, fill=LOOP_EDGE, width=2)
            self.create_line(lb, wave_top, lb, h, fill=LOOP_EDGE, width=2)
            self.create_text(la + 4, wave_top + 4, text='A', fill='#8899ff',
                             anchor='nw', font=('Helvetica', 9, 'bold'))
            self.create_text(lb - 4, wave_top + 4, text='B', fill='#8899ff',
                             anchor='ne', font=('Helvetica', 9, 'bold'))
        # Peak polygon
        margin = 4
        amp = wave_h / 2 - margin
        pts_peak = []
        for x in range(w):
            pts_peak.append((x, cy - peaks[x] * amp))
        for x in range(w - 1, -1, -1):
            pts_peak.append((x, cy + peaks[x] * amp))
        if len(pts_peak) >= 6:
            self.create_polygon(pts_peak, fill=WAVE_PEAK, outline='')
        # RMS polygon
        pts_rms = []
        for x in range(w):
            pts_rms.append((x, cy - rms[x] * amp))
        for x in range(w - 1, -1, -1):
            pts_rms.append((x, cy + rms[x] * amp))
        if len(pts_rms) >= 6:
            self.create_polygon(pts_rms, fill=WAVE_RMS, outline='')

    def update_head(self):
        if self._head_id:
            self.delete(self._head_id)
        w = self.winfo_width()
        h = self.winfo_height()
        strip_h = SEGMENT_STRIP_H if self.segments else 0
        x = self.engine.pos_ratio() * w
        self._head_id = self.create_line(x, strip_h, x, h, fill=PLAYHEAD, width=2)

    def _seg_at_x(self, event):
        if not self.segments or self.engine.original is None:
            return None
        if event.y > SEGMENT_STRIP_H:
            return None
        dur = len(self.engine.original) / self.engine.sr
        if dur <= 0:
            return None
        t = event.x / max(1, self.winfo_width()) * dur
        for seg in self.segments:
            if seg['start'] <= t <= seg['end']:
                return seg
        return None

    def _click(self, event):
        if not self.engine.loaded:
            return
        # Click on the segment strip = load segment as A-B loop
        seg = self._seg_at_x(event)
        if seg and self.on_segment_click:
            self.on_segment_click(seg)
            return
        self.engine.seek(event.x / max(1, self.winfo_width()))
        self.update_head()
        if self.on_seek:
            self.on_seek()

    def _drag(self, event):
        if not self.engine.loaded:
            return
        # Dragging never triggers segment load — treat as scrub
        if event.y <= SEGMENT_STRIP_H:
            return
        self.engine.seek(event.x / max(1, self.winfo_width()))
        self.update_head()
        if self.on_seek:
            self.on_seek()

    def _wheel(self, event):
        if not self.engine.loaded:
            return
        self.engine.seek_relative(1.0 if event.delta > 0 else -1.0)
        self.update_head()
        if self.on_seek:
            self.on_seek()


class ZoomedWaveformView(tk.Canvas):
    """DAW-style detail view: a fixed time window centered on the playhead,
    scrolling underneath a stationary center line as playback advances."""

    def __init__(self, parent, engine, window_seconds=5.0, **kwargs):
        kwargs.setdefault('bg', WAVE_BG)
        kwargs.setdefault('highlightthickness', 0)
        super().__init__(parent, **kwargs)
        self.engine = engine
        self.window_seconds = window_seconds
        self._win_start = 0.0   # seconds — left edge of the visible window
        self._win_span = window_seconds
        self.on_seek = None  # callback() fired after a click/drag/wheel seek
        self.bind('<Configure>', lambda e: self.refresh())
        self.bind('<Button-1>', self._click)
        self.bind('<B1-Motion>', self._drag)
        self.bind('<MouseWheel>', self._wheel)

    def _buf(self):
        if self.engine.reference_mode:
            return self.engine.original
        return self.engine.processed if self.engine.processed is not None else self.engine.original

    RULER_H = 18
    TICK_INTERVAL = 0.5  # seconds between minor ticks; every 2nd is major

    def refresh(self):
        self.delete('all')
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10 or h < 10:
            return
        buf = self._buf()
        if buf is None or len(buf) == 0:
            cy = h / 2
            for i in range(1, 10):
                x = w * i / 10
                self.create_line(x, 0, x, h, fill=GRID, width=1)
            self.create_line(0, cy, w, cy, fill=GRID, width=1)
            self.create_text(w // 2, cy, text="No audio loaded",
                             fill='#666', font=('Helvetica', 14))
            return
        sr = self.engine.sr
        pos = self.engine.ref_position if self.engine.reference_mode else self.engine.position
        n = len(buf)
        half_win = int(self.window_seconds * sr / 2)
        start = pos - half_win
        end = pos + half_win
        self._win_start = start / sr
        self._win_span = self.window_seconds
        total_window = max(1, end - start)

        # ── Precision time ruler — a strip reserved at the top with tick
        # marks and labels at fixed real-time intervals, so the exact
        # position under any point in the view is readable at a glance.
        ruler_h = self.RULER_H
        wave_top = ruler_h
        wave_h = h - ruler_h
        cy = wave_top + wave_h / 2
        first_tick = math.floor(self._win_start / self.TICK_INTERVAL) * self.TICK_INTERVAL
        t = first_tick
        tick_i = round(first_tick / self.TICK_INTERVAL)
        while t <= self._win_start + self._win_span + self.TICK_INTERVAL:
            x = (t * sr - start) / total_window * w
            if -1 <= x <= w + 1:
                is_major = (tick_i % 2) == 0
                if is_major:
                    self.create_line(x, ruler_h, x, h, fill=GRID, width=1)
                    self.create_line(x, 0, x, ruler_h, fill='#888', width=1)
                    self.create_text(x + 2, 1, text=_fmt_time(max(0, t)),
                                     fill='#aaa', anchor='nw', font=('Menlo', 8))
                else:
                    self.create_line(x, ruler_h * 0.5, x, ruler_h, fill='#555', width=1)
            t += self.TICK_INTERVAL
            tick_i += 1
        self.create_line(0, cy, w, cy, fill=GRID, width=1)

        # Loop region overlay — drawn BEFORE the waveform polygon (not
        # after) so the peaks are always visible on top of the tint,
        # matching the main WaveformView's z-order. Drawn after here, the
        # tint rectangle could span the entire panel width whenever the
        # loop is wider than the 5s zoom window (very common), completely
        # hiding the waveform underneath — worse still since stipple-based
        # "see-through" fills are unreliable on Retina Tk and can render
        # fully opaque instead of dithered.
        if self.engine.loop_a is not None and self.engine.loop_b is not None:
            la_x = (self.engine.loop_a - start) / total_window * w
            lb_x = (self.engine.loop_b - start) / total_window * w
            if lb_x > 0 and la_x < w:
                self.create_rectangle(max(0, la_x), wave_top, min(w, lb_x), h,
                                      fill=LOOP_FILL, outline='')
                if 0 <= la_x <= w:
                    self.create_line(la_x, wave_top, la_x, h, fill=LOOP_EDGE, width=2)
                if 0 <= lb_x <= w:
                    self.create_line(lb_x, wave_top, lb_x, h, fill=LOOP_EDGE, width=2)

        seg_start = max(0, start)
        seg_end = min(n, end)
        px_start = (seg_start - start) / total_window * w
        px_end = w - (end - seg_end) / total_window * w
        if seg_end > seg_start and px_end > px_start:
            segment = buf[seg_start:seg_end]
            seg_w = max(1, int(round(px_end - px_start)))
            chunk = max(1, len(segment) // seg_w)
            trim = len(segment) - len(segment) % chunk
            if trim >= chunk:
                reshaped = segment[:trim].reshape(-1, chunk)
                peaks = np.max(np.abs(reshaped), axis=1)
            else:
                peaks = np.abs(segment)
            xs = np.linspace(px_start, px_end, len(peaks))
            margin = 6
            amp = wave_h / 2 - margin
            pts = [(x, cy - p * amp) for x, p in zip(xs, peaks)]
            pts += [(x, cy + p * amp) for x, p in zip(xs[::-1], peaks[::-1])]
            if len(pts) >= 6:
                self.create_polygon(pts, fill=WAVE_PEAK, outline='')
        # Playhead — fixed at center, waveform scrolls beneath it
        cx = w / 2
        self.create_line(cx, wave_top, cx, h, fill=PLAYHEAD, width=2)
        # Precise sub-second readout, anchored to the playhead
        cur_t = pos / sr
        label = _fmt_time_precise(cur_t)
        label_w = 7 * len(label) + 8
        lx = min(max(cx, label_w / 2 + 2), w - label_w / 2 - 2)
        self.create_rectangle(lx - label_w / 2, wave_top + 2, lx + label_w / 2, wave_top + 18,
                              fill=WAVE_BG, outline=PLAYHEAD, width=1)
        self.create_text(lx, wave_top + 10, text=label, fill=PLAYHEAD,
                         font=('Menlo', 11, 'bold'))

    def _time_at_x(self, x, w):
        return self._win_start + (x / max(1, w)) * self._win_span

    def _click(self, event):
        if not self.engine.loaded:
            return
        buf = self._buf()
        if buf is None or len(buf) == 0:
            return
        t = self._time_at_x(event.x, self.winfo_width())
        dur = len(buf) / self.engine.sr
        self.engine.seek(max(0.0, min(t, dur)) / dur if dur > 0 else 0)
        self.refresh()
        if self.on_seek:
            self.on_seek()

    def _drag(self, event):
        self._click(event)

    def _wheel(self, event):
        if not self.engine.loaded:
            return
        self.engine.seek_relative(0.25 if event.delta > 0 else -0.25)
        self.refresh()
        if self.on_seek:
            self.on_seek()


# ─── Main Application ───────────────────────────────────────────────

class NoteXNoteApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{VERSION}")
        self.root.minsize(1265, 897)
        self.engine = AudioEngine()
        self.engine._on_end = self._on_playback_end
        self.db = SongDB()
        self._busy = False
        self._speed_pending = None
        self._pitch_pending = None
        self._reprocess_after = None
        self._update_id = None
        self._load_prefs()
        self._build_ui()
        self._center()
        try:
            loaded = load_plugins(self)
            if loaded:
                print(f'[{APP_NAME}] loaded plugins: {loaded}')
        except Exception as e:
            print(f'[{APP_NAME}] plugin loader failed: {e}')
        self._tick()

    # ── Prefs ──

    def _load_prefs(self):
        self.prefs = {'speed': 1.0, 'pitch_lock': True, 'volume': 0.5,
                      'last_dir': os.path.expanduser('~/Music'),
                      'geometry': '1481x1035', 'source': 'local',
                      'music_scan_consent': None}
        try:
            with open(_prefs_path()) as f:
                self.prefs.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        self.engine.speed = self.prefs['speed']
        self.engine.pitch_lock = self.prefs['pitch_lock']
        self.engine.volume = self.prefs['volume']

    def _save_prefs(self):
        self.prefs.update(speed=self.engine.speed, pitch_lock=self.engine.pitch_lock,
                          volume=self.engine.volume, geometry=self.root.geometry())
        try:
            with open(_prefs_path(), 'w') as f:
                json.dump(self.prefs, f, indent=2)
        except Exception:
            pass

    def _center(self):
        try:
            w, h = self.prefs['geometry'].split('+')[0].split('x')
            w, h = int(w), int(h)
        except Exception:
            w, h = 1481, 1035
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f'{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}')

    # ── UI ──

    def _build_ui(self):
        self._build_menubar()
        # Tk gives any focused button its own default binding where
        # <space>/<Return> re-invokes that button's command — separate
        # from and in addition to any of our own key bindings elsewhere.
        # With as many buttons as this app has (Transport, EQ, Speed/Pitch
        # presets, search...), a click leaves some button focused, and the
        # next unrelated Space press can silently re-trigger it (confirmed:
        # a Pitch +/- click followed by any Space press re-applied the
        # same step, making one semitone sound like a whole step).
        #
        # takefocus=0 alone does NOT fix this — takefocus only excludes a
        # widget from Tab-key traversal, it doesn't stop a mouse click from
        # giving that widget real keyboard focus (verified: focus_force()
        # on a takefocus=0 button still lands focus on it, and <space>
        # still fires its command). The actual fix has to override the
        # button classes' own <space> binding directly, at the class
        # bindtag level, so it's dead regardless of which button — old or
        # newly added — happens to hold focus.
        style = ttk.Style()
        style.configure('TButton', takefocus=0)
        style.configure('TCheckbutton', takefocus=0)
        for cls in ('TButton', 'TCheckbutton', 'Button'):
            self.root.bind_class(cls, '<space>', lambda e: 'break')
        main = ttk.Frame(self.root)
        main.pack(fill='both', expand=True, padx=8, pady=8)
        paned = ttk.PanedWindow(main, orient='horizontal')
        paned.pack(fill='both', expand=True)

        # Left: source + overview waveform + zoomed detail view
        left = ttk.Frame(paned)
        paned.add(left, weight=3)

        # Source (top of left pane)
        src = ttk.LabelFrame(left, text="Source", padding=6)
        src.pack(fill='x', pady=(0, 8), padx=(0, 4))
        sources = list(SOURCE_MAP.values())
        stored = self.prefs.get('source', 'local')
        self.src_var = tk.StringVar(value=SOURCE_MAP.get(stored, 'Local File'))
        combo = ttk.Combobox(src, textvariable=self.src_var,
                              values=sources, state='readonly', width=18)
        combo.pack(fill='x')
        combo.bind('<<ComboboxSelected>>', self._src_changed)
        self.file_frame = ttk.Frame(src)
        self.file_frame.pack(fill='x', pady=(8, 0))
        self.file_lbl = ttk.Label(self.file_frame, text="No file loaded",
                                   wraplength=400)
        self.file_lbl.pack(side='left', fill='x', expand=True)
        ttk.Button(self.file_frame, text="Open", width=6,
                   command=self._open_file).pack(side='right', padx=(4, 0))
        self.folders_btn = ttk.Button(
            self.file_frame, text="\U0001f4c1 ▾", width=4,
            command=self._open_shortcut_menu)
        self.folders_btn.pack(side='right', padx=(4, 0))
        ttk.Button(self.file_frame, text="⏏", width=3,
                   command=self._eject_source).pack(side='right', padx=(4, 0))
        self.url_frame = ttk.Frame(src)
        self.url_var = tk.StringVar()
        url_row = ttk.Frame(self.url_frame)
        url_row.pack(fill='x')
        url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        url_entry.pack(side='left', fill='x', expand=True)
        url_entry.bind('<Return>', lambda e: self._fetch_source())
        ttk.Button(url_row, text="Fetch", width=6,
                   command=self._fetch_source).pack(side='right', padx=(4, 0))
        self.url_hint = ttk.Label(self.url_frame, text="",
                                   foreground='gray', font=('Helvetica', 10))
        self.url_hint.pack(anchor='w', pady=(2, 0))
        self._src_changed()

        # YouTube Search — sits directly under Source, above the waveform:
        # a natural transition from "here's what's loaded" to "here's
        # where you go find something new." Core behavior is audio-only
        # (search, click, load audio into the practice engine, exactly
        # like Paste URL). A plugin can register a video renderer via
        # set_youtube_renderer() to additionally draw live video into
        # yt_video_container; without one, that container just stays
        # empty and unused.
        yt = ttk.LabelFrame(left, text="YouTube Search", padding=5)
        yt.pack(fill='x', pady=(8, 0), padx=(0, 4))
        yt_row = ttk.Frame(yt)
        yt_row.pack(fill='x')
        self.yt_query_var = tk.StringVar()
        yt_entry = ttk.Entry(yt_row, textvariable=self.yt_query_var)
        yt_entry.pack(side='left', fill='x', expand=True)
        yt_entry.bind('<Return>', lambda e: self._yt_search())
        self.yt_search_btn = ttk.Button(yt_row, text="Search", width=7,
                                         command=self._yt_search)
        self.yt_search_btn.pack(side='left', padx=(4, 0))
        ttk.Button(yt_row, text="⏏", width=2,
                   command=self._eject_source).pack(side='left', padx=(2, 0))
        self._yt_search_busy = False
        self._yt_search_token = 0

        yt_list_row = ttk.Frame(yt)
        yt_list_row.pack(fill='x', pady=(4, 0))
        yt_scroll = ttk.Scrollbar(yt_list_row, orient='vertical')
        self.yt_listbox = tk.Listbox(
            yt_list_row, height=6, bg='#22223a', fg='#e0e0e8',
            selectbackground='#4dd0e1', selectforeground='#111',
            highlightthickness=0, bd=0, font=('Helvetica', 10),
            yscrollcommand=yt_scroll.set)
        yt_scroll.config(command=self.yt_listbox.yview)
        self.yt_listbox.pack(side='left', fill='both', expand=True)
        yt_scroll.pack(side='right', fill='y')
        self.yt_listbox.bind('<Double-Button-1>', lambda e: self._yt_load_selected())
        self.yt_listbox.bind('<Return>', lambda e: self._yt_load_selected())

        self.yt_status_lbl = ttk.Label(yt, text="", foreground='#888',
                                        font=('Helvetica', 9))
        self.yt_status_lbl.pack(anchor='w', pady=(2, 0))

        # Reserved for a video-renderer plugin — empty otherwise
        self.yt_video_container = ttk.Frame(yt)
        self.yt_video_container.pack(fill='x', pady=(4, 0))

        self._yt_results = []
        self._youtube_video_renderer = None

        # Overview waveform (same size as before, now underneath Source)
        wf_container = ttk.Frame(left)
        wf_container.pack(fill='x', expand=False, padx=(0, 4))
        self.waveform = WaveformView(wf_container, self.engine, height=180)
        self.waveform.on_segment_click = self._on_segment_click
        self.waveform.on_seek = self._on_scrub
        self.waveform.pack(fill='both', expand=True)
        # Busy overlay, centered over the waveform — the shared cooliosis
        # spinner (61 frames, native 200x200, flattened onto WAVE_BG),
        # standardized across the user's apps in Python-Projects/cc/cooliosis.
        # scale=2 (100x100) — at scale=1 the spinner's own bounding box
        # (200px) was taller than the whole waveform view (height=180),
        # overwhelming it well beyond just the visible spinner glyph.
        self._spinner = ProgressIndicator(
            self.waveform, _resource_path('progress.gif'), fps=16, scale=2)
        self._spin_lbl = tk.Label(self.waveform, text="", bg=WAVE_BG,
                                   fg='#ccc', font=('Helvetica', 11, 'bold'))
        time_bar = ttk.LabelFrame(left, text="Time", padding=6)
        time_bar.pack(fill='x', pady=(8, 0), padx=(0, 4))
        # LCD-style digital counter — dark panel, big bright current time,
        # dim smaller total alongside it (hardware-transport-deck look).
        lcd = tk.Frame(time_bar, bg='#0a0a0a')
        lcd.pack(fill='x', ipady=6, ipadx=10)
        # Centered within the LCD panel — the LabelFrame's own "Time" title
        # stays put; only this readout cluster is centered.
        time_row = tk.Frame(lcd, bg='#0a0a0a')
        time_row.pack()
        self.time_lbl = tk.Label(time_row, text="00:00:00.00", bg='#0a0a0a',
                                  fg=PLAYHEAD, font=('Menlo', 26, 'bold'),
                                  anchor='w')
        self.time_lbl.pack(side='left')
        self.time_tot_lbl = tk.Label(time_row, text="/ 00:00", bg='#0a0a0a',
                                      fg='white', font=('Menlo', 13),
                                      anchor='w')
        self.time_tot_lbl.pack(side='left', padx=(10, 0), pady=(10, 0))

        # No visible status bar — single-file workflow means there's nothing
        # ongoing to report. self.status is kept as a plain StringVar (no
        # widget attached) so existing .set() calls elsewhere stay harmless.
        self.status = tk.StringVar(value="Ready")

        # Zoomed detail view — fills remaining real estate, tracks playhead
        zoom_frame = ttk.LabelFrame(left, text="Zoom (follows playhead)", padding=4)
        zoom_frame.pack(fill='both', expand=True, pady=(8, 0), padx=(0, 4))
        self.zoom_waveform = ZoomedWaveformView(zoom_frame, self.engine)
        self.zoom_waveform.on_seek = self._on_scrub
        self.zoom_waveform.pack(fill='both', expand=True)

        # Right: controls
        right = ttk.Frame(paned, width=310)
        paned.add(right, weight=1)

        # Speed
        spd = ttk.LabelFrame(right, text="Speed", padding=5)
        spd.pack(fill='x', pady=(0, 4))
        self.spd_var = tk.DoubleVar(value=self.engine.speed)
        spd_disp_row = ttk.Frame(spd)
        spd_disp_row.pack()
        self.spd_input_var = tk.StringVar(value=f"{self.engine.speed:.2f}")
        spd_entry = ttk.Entry(spd_disp_row, textvariable=self.spd_input_var,
                               width=5, justify='right',
                               font=('Menlo', 14, 'bold'))
        spd_entry.pack(side='left')
        spd_entry.bind('<Return>', self._spd_typed)
        spd_entry.bind('<FocusOut>', self._spd_typed)
        ttk.Label(spd_disp_row, text="x",
                   font=('Menlo', 14, 'bold')).pack(side='left')
        FaderSlider(spd, self.spd_var, from_=MAX_SPEED, to=MIN_SPEED,
                    length=240, orient='horizontal',
                    accent='#4dd0e1', snap_to=1.0,
                    tick_values=[0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0],
                    reset_value=1.0,
                    on_change=lambda v: self._spd_slide()).pack(fill='x', pady=(2, 0))
        presets = ttk.Frame(spd)
        presets.pack(fill='x', pady=(2, 0))
        for s in (0.25, 0.5, 0.75, 1.0, 1.25, 1.5):
            ttk.Button(presets, text=f"{s}x", width=4,
                       command=lambda v=s: self._spd_preset(v)).pack(
                           side='left', padx=1, expand=True)
        self.pl_var = tk.BooleanVar(value=self.engine.pitch_lock)
        ttk.Checkbutton(spd, text="Pitch Lock (preserve pitch)",
                         variable=self.pl_var,
                         command=self._pitch_lock_toggle).pack(anchor='w', pady=(4, 0))

        # Pitch — semitone transpose, independent of Speed. Works whether
        # or not Pitch Lock is on: it shifts pitch relative to whatever
        # Speed/Pitch Lock already produce, rather than replacing them.
        pit = ttk.LabelFrame(right, text="Pitch", padding=5)
        pit.pack(fill='x', pady=(0, 4))
        pit_row = ttk.Frame(pit)
        pit_row.pack()
        ttk.Button(pit_row, text="−", width=3,
                   command=lambda: self._pitch_step(-1)).pack(side='left')
        self.pitch_var = tk.StringVar(value="0 st")
        ttk.Label(pit_row, textvariable=self.pitch_var, width=7,
                  anchor='center', font=('Menlo', 14, 'bold')).pack(side='left', padx=4)
        ttk.Button(pit_row, text="+", width=3,
                   command=lambda: self._pitch_step(1)).pack(side='left')
        ttk.Button(pit_row, text="Reset", width=6,
                   command=lambda: self._pitch_step(0, absolute=True)).pack(
                       side='left', padx=(8, 0))

        # Master Volume
        vol = ttk.LabelFrame(right, text="Master Volume", padding=5)
        vol.pack(fill='x', pady=(0, 4))
        vol_row = ttk.Frame(vol)
        vol_row.pack(fill='x')
        self.vol_var = tk.DoubleVar(value=self.engine.volume)
        FaderSlider(vol_row, self.vol_var, from_=0, to=1.0,
                    length=200, orient='horizontal',
                    accent='#66BB6A',
                    tick_values=[0, 0.25, 0.5, 0.75, 1.0],
                    reset_value=0.8,
                    on_change=lambda v: self._vol_slide(v)
                    ).pack(side='left', fill='x', expand=True)
        self.vol_pct_var = tk.StringVar(value=str(int(round(self.engine.volume * 100))))
        vol_entry = ttk.Entry(vol_row, textvariable=self.vol_pct_var,
                               width=4, justify='right')
        vol_entry.pack(side='left', padx=(6, 2))
        vol_entry.bind('<Return>', self._vol_typed)
        vol_entry.bind('<FocusOut>', self._vol_typed)
        ttk.Label(vol_row, text="%").pack(side='left')

        # Transport
        tp = ttk.LabelFrame(right, text="Transport", padding=5)
        tp.pack(fill='x', pady=(0, 4))
        btns = ttk.Frame(tp)
        btns.pack()
        for sym, cmd in [('⏮', self._skip_start), ('⏪', self._rw),
                         ('▶', self._toggle_play), ('⏹', self._stop),
                         ('⏩', self._ff), ('⏭', self._skip_end)]:
            b = ttk.Button(btns, text=sym, width=3, command=cmd)
            b.pack(side='left', padx=2)
            if sym == '▶':
                self.play_btn = b

        # Reference
        ref = ttk.LabelFrame(right, text="Reference", padding=4)
        ref.pack(fill='x', pady=(0, 4))
        self.ref_btn = tk.Button(ref, text="\U0001f3af  Play at 1× (Reference)",
                                  font=('Helvetica', 11),
                                  relief='raised', bd=2,
                                  command=self._toggle_reference,
                                  activebackground='#ffb347',
                                  highlightthickness=0, takefocus=0)
        self.ref_btn.pack(fill='x', ipady=1)
        self._ref_default_bg = self.ref_btn.cget('bg')

        # A-B Loop
        lp = ttk.LabelFrame(right, text="A-B Loop", padding=5)
        lp.pack(fill='x', pady=(0, 4))
        lb = ttk.Frame(lp)
        lb.pack()
        ttk.Button(lb, text="Set Start", width=8, command=self._set_a).pack(side='left', padx=4)
        ttk.Button(lb, text="Set End", width=8, command=self._set_b).pack(side='left', padx=4)
        ttk.Button(lb, text="Clear", width=8, command=self._clr_loop).pack(side='left', padx=4)
        self.loop_lbl = ttk.Label(lp, text="No loop set")
        self.loop_lbl.pack(pady=(2, 0))

        # EQ
        eq = ttk.LabelFrame(right, text="EQ — Boost", padding=5)
        eq.pack(fill='x', pady=(0, 4))
        eq_row = ttk.Frame(eq)
        eq_row.pack(fill='x')
        self._eq_active = 'Mid'
        self._eq_btns = {}
        for name in EQ_PRESETS:
            b = tk.Button(eq_row, text=name, width=7,
                          font=('Helvetica', 11, 'bold'),
                          relief='raised', bd=2,
                          highlightthickness=0, takefocus=0,
                          command=lambda k=name: self._eq_preset(k))
            b.pack(side='left', padx=2, expand=True, fill='x', ipady=2)
            self._eq_btns[name] = b
        self._eq_highlight()

        # Keys — no Space binding: a focused ttk.Button/Checkbutton has
        # its own default Space-activates-me binding, so Space double-fired
        # (once for the button that last had focus, once for playback)
        # whenever any button anywhere had keyboard focus, which given how
        # many buttons this app has was most of the time. Not worth
        # fighting Tk's focus/binding model for — removed rather than fixed.
        self.root.bind('<Left>', lambda e: self._rw())
        self.root.bind('<Right>', lambda e: self._ff())
        self.root.bind('<Command-o>', lambda e: self._open_file())
        self.root.protocol('WM_DELETE_WINDOW', self._quit)

    # ── Source ──

    def _src_changed(self, _=None):
        choice = self.src_var.get()
        self.file_frame.pack_forget()
        self.url_frame.pack_forget()
        if choice == 'Local File':
            self.file_frame.pack(fill='x', pady=(8, 0))
        else:
            self.url_frame.pack(fill='x', pady=(8, 0))
            if choice == 'Spotify':
                self.url_hint.config(
                    text="Paste a Spotify track, album, or playlist URL")
            else:
                self.url_hint.config(
                    text="YouTube, SoundCloud, Bandcamp, Vimeo, and 1000+ sites")
        self.prefs['source'] = SOURCE_RMAP.get(choice, 'local')

    def _open_file(self, initial=None):
        exts = ' '.join(f'*{e}' for e in SUPPORTED_FORMATS)
        if initial is None:
            initial = self.prefs.get('last_dir', '~')
        path = filedialog.askopenfilename(
            title="Open Audio File",
            initialdir=initial,
            filetypes=[('Audio files', exts), ('All files', '*.*')])
        if path:
            self.prefs['last_dir'] = os.path.dirname(path)
            self._load(path)

    def _open_shortcut_menu(self):
        """Popup menu of detected music folder shortcuts.

        Gated behind our own explicit yes/no, asked once and remembered —
        macOS's own Music/Downloads permission prompt exists, but a plain
        unsandboxed os.listdir() from this app isn't reliably blocked by a
        "Don't Allow" on it, so the app has to be the one that actually
        honors a no rather than assuming the OS already did."""
        consent = self.prefs.get('music_scan_consent')
        if consent is None:
            consent = messagebox.askyesno(
                "Look for music folders?",
                "noteXnote can jump straight to folders it finds on your "
                "Mac (~/Music, ~/Downloads, and any mounted volumes) — "
                "macOS may ask permission the first time. Only folder "
                "names are read, nothing is sent anywhere. Look now?")
            self.prefs['music_scan_consent'] = consent
            self._save_prefs()
        menu = tk.Menu(self.root, tearoff=0)
        if not consent:
            menu.add_command(label="Folder shortcuts declined", state='disabled')
            menu.add_command(label="Enable folder shortcuts…",
                              command=self._reenable_folder_shortcuts)
        else:
            shortcuts = _find_music_dirs()
            if not shortcuts:
                menu.add_command(label="No music folders detected", state='disabled')
            else:
                for label, path in shortcuts:
                    menu.add_command(
                        label=f"{label}   ({path})",
                        command=lambda p=path: self._open_file(initial=p))
        try:
            x = self.folders_btn.winfo_rootx()
            y = self.folders_btn.winfo_rooty() + self.folders_btn.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _reenable_folder_shortcuts(self):
        self.prefs['music_scan_consent'] = None
        self._save_prefs()

    def _fetch_source(self):
        if self.src_var.get() == 'Spotify':
            self._fetch_spotify()
        else:
            self._fetch_url()

    def _fetch_url(self):
        url = self.url_var.get().strip()
        if not url:
            return
        self._download_and_load(url)

    def _download_and_load(self, url, busy_text="Downloading audio...", _retried=False):
        """Download a URL's audio via yt-dlp and load it — shared by the
        Paste URL source and YouTube search results. yt-dlp needs frequent
        updates to keep up with YouTube's own changes; a stale copy fails
        with cryptic errors ("The page needs to be reloaded", etc.) that
        mean nothing to a user. On failure, offer to self-update yt-dlp
        (its own -U flag, so this works regardless of how it was
        installed — Homebrew, pip, or a standalone binary) and retry once
        automatically rather than just surfacing a raw traceback."""
        ytdlp = _find_tool('yt-dlp')
        if not ytdlp:
            messagebox.showerror("Missing yt-dlp",
                                 "Install with: brew install yt-dlp")
            return
        self._show_busy(busy_text)
        def work():
            try:
                td = _temp_dir()
                for f in os.listdir(td):
                    if f.startswith('url_dl'):
                        os.remove(os.path.join(td, f))
                # yt-dlp needs ffmpeg for the -x/--audio-format
                # postprocessing step and does its own internal search
                # for it (separate from our _find_tool fallback) — under
                # a real Finder launch, PATH is minimal and that search
                # comes up empty even though _find_tool() just found it
                # fine, so tell yt-dlp exactly where it is explicitly.
                cmd = [ytdlp, '-x', '--audio-format', 'wav',
                       '-o', os.path.join(td, 'url_dl.%(ext)s'),
                       '--no-playlist', '--no-warnings', url]
                ffmpeg = _find_tool('ffmpeg')
                if ffmpeg:
                    cmd += ['--ffmpeg-location', ffmpeg]
                subprocess.run(
                    cmd, check=True, capture_output=True, text=True,
                    env=_external_env())
                found = next((os.path.join(td, f) for f in sorted(os.listdir(td))
                              if f.startswith('url_dl')), None)
                if found:
                    self.root.after(0, lambda: self._load(found))
                else:
                    self.root.after(0, lambda: self._load_err("Output file not found"))
            except subprocess.CalledProcessError as e:
                reason = _extract_ytdlp_error(e.stderr)
                if _retried:
                    self.root.after(0, lambda: self._load_err(reason))
                else:
                    self.root.after(0, lambda: self._ytdlp_failed(ytdlp, url, busy_text, reason))
            except Exception as e:
                # Must stringify e HERE, synchronously — CPython deletes
                # the exception-clause variable the moment this except
                # block exits, so a lambda that closes over `e` itself and
                # calls str(e) only when root.after() later runs it hits
                # "NameError: cannot access free variable 'e'" instead of
                # ever showing the real error. Every deferred-lambda error
                # handler in this file needs the str(e)/extraction done
                # immediately and only the resulting plain string captured.
                msg = str(e)
                self.root.after(0, lambda: self._load_err(msg))
        threading.Thread(target=work, daemon=True).start()

    def _ytdlp_failed(self, ytdlp, url, busy_text, reason):
        self._hide_busy()
        self.status.set("Download failed")
        update = messagebox.askyesno(
            "Download Failed",
            "This download failed — most often that means yt-dlp is out "
            "of date (YouTube changes frequently and yt-dlp needs to keep "
            "up).\n\n"
            f"Details: {reason[:300]}\n\n"
            "Update yt-dlp now and try again?",
            icon='warning')
        if not update:
            self._load_err(reason)
            return
        self._show_busy("Updating yt-dlp...")
        def work():
            try:
                subprocess.run([ytdlp, '-U'], check=True,
                               capture_output=True, text=True, timeout=60,
                               env=_external_env())
            except Exception:
                pass  # best-effort — retry the download regardless
            self.root.after(0, lambda: self._download_and_load(
                url, busy_text, _retried=True))
        threading.Thread(target=work, daemon=True).start()

    # ── YouTube Search ──

    def _yt_search(self):
        query = self.yt_query_var.get().strip()
        if not query:
            return
        # Guard against overlapping searches — pressing Enter/Search again
        # while one's already running used to spawn a second concurrent
        # yt-dlp process with no ordering guarantee on which result set
        # wins, which reads as the search "hanging" even longer. Disabling
        # the button also gives unambiguous feedback that it registered,
        # instead of a barely-noticeable status-label change.
        if self._yt_search_busy:
            return
        self._yt_search_busy = True
        self._yt_search_token = getattr(self, '_yt_search_token', 0) + 1
        my_token = self._yt_search_token
        self.yt_search_btn.config(state='disabled')
        self.yt_status_lbl.config(text="Searching…")
        self.yt_listbox.delete(0, 'end')
        self._yt_results = []
        def work():
            try:
                results = _youtube_search(query, n=8)
            except Exception as e:
                msg = str(e)  # stringify now — see note in _download_and_load
                self.root.after(0, lambda: self._yt_search_error(msg, my_token))
                return
            self.root.after(0, lambda: self._yt_search_done(results, my_token))
        threading.Thread(target=work, daemon=True).start()
        # Safety net: the subprocess itself has a 30s timeout, but if
        # anything else goes wrong between here and there (a callback that
        # never fires, an unexpected exception class, etc.) this guarantees
        # the UI can't get stuck in "Searching…" forever. The token check
        # keeps a late-firing watchdog from clobbering a newer search that
        # started after this one's window closed.
        self.root.after(35000, lambda: self._yt_search_watchdog(my_token))

    def _yt_search_watchdog(self, token):
        if self._yt_search_busy and self._yt_search_token == token:
            self._yt_search_busy = False
            self.yt_search_btn.config(state='normal')
            self.yt_status_lbl.config(text="Search timed out — try again")

    def _yt_search_error(self, msg, token):
        if self._yt_search_token != token:
            return  # superseded by a newer search — ignore this stale result
        print(f"[yt-search] failed: {msg}", file=sys.stderr)
        self._yt_search_busy = False
        self.yt_search_btn.config(state='normal')
        self.yt_status_lbl.config(text=f"Search failed: {msg}")

    def _yt_search_done(self, results, token):
        if self._yt_search_token != token:
            return  # superseded — ignore
        self._yt_search_busy = False
        self.yt_search_btn.config(state='normal')
        self._yt_results = results
        self.yt_listbox.delete(0, 'end')
        for r in results:
            dur = _fmt_time(r['duration']) if r['duration'] else '--:--'
            line = f"{dur}  {r['title']}"
            if r['channel']:
                line += f"  — {r['channel']}"
            self.yt_listbox.insert('end', line)
        self.yt_status_lbl.config(
            text=f"{len(results)} result(s) — double-click to load"
            if results else "No results")

    def _yt_load_selected(self):
        sel = self.yt_listbox.curselection()
        if not sel or sel[0] >= len(self._yt_results):
            return
        info = self._yt_results[sel[0]]
        self._download_and_load(info['url'], f'Downloading "{info["title"]}"...')
        if self._youtube_video_renderer:
            try:
                self._youtube_video_renderer(self.yt_video_container, info)
            except Exception as e:
                print(f'[{APP_NAME}] youtube video renderer failed: {e}')

    def set_youtube_renderer(self, fn):
        """Plugin API: register fn(parent_frame, video_info) to render live
        video into the YouTube panel's reserved container whenever a search
        result is loaded. video_info is a dict:
        {'title', 'duration', 'channel', 'url'}. Optional — without a
        renderer registered, search results just load audio on click, same
        as pasting a URL."""
        self._youtube_video_renderer = fn

    def _fetch_spotify(self):
        url = self.url_var.get().strip()
        if not url:
            return
        spotdl = _find_tool('spotdl')
        if not spotdl:
            messagebox.showerror(
                "Missing spotdl",
                "spotdl is required for Spotify downloads.\n\n"
                "Install with:\n  pip install spotdl\n\n"
                "Requires ffmpeg (brew install ffmpeg).")
            return
        self._show_busy("Downloading from Spotify...")
        def work():
            try:
                td = _temp_dir()
                for f in os.listdir(td):
                    if f.startswith('spot_'):
                        os.remove(os.path.join(td, f))
                subprocess.run(
                    [spotdl, 'download', url,
                     '--output', os.path.join(td, 'spot_{title}.{output-ext}')],
                    check=True, capture_output=True, text=True,
                    timeout=120, env=_external_env())
                found = next((os.path.join(td, f) for f in sorted(os.listdir(td))
                              if f.startswith('spot_')), None)
                if found:
                    self.root.after(0, lambda: self._load(found))
                else:
                    self.root.after(0, lambda: self._load_err(
                        "Spotify download completed but no audio file found"))
            except subprocess.TimeoutExpired:
                self.root.after(0, lambda: self._load_err("Download timed out"))
            except subprocess.CalledProcessError as e:
                reason = _extract_ytdlp_error(e.stderr)
                self.root.after(0, lambda: self._load_err(reason))
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda: self._load_err(msg))
        threading.Thread(target=work, daemon=True).start()

    # ── Loading ──

    def _load(self, path):
        self._save_song()
        self.engine.stop()
        self.play_btn.config(text='▶')
        self._show_busy("Loading audio...")
        def work():
            try:
                self.engine.load(path)
                # Draw the overview waveform as soon as the file is decoded,
                # before the WSOLA/EQ/limiter pipeline runs. The Zoom view
                # already renders this early (it pulls live from the engine
                # on every tick) — without this, the overview waveform sat
                # blank behind the busy overlay until processing finished,
                # while Zoom was already showing real content beside it.
                self.root.after(0, lambda: self._show_raw_waveform(path))
                self.engine.process()
                self.root.after(0, lambda: self._loaded(path))
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda: self._load_err(msg))
        threading.Thread(target=work, daemon=True).start()

    def _show_raw_waveform(self, path):
        self.file_lbl.config(text=os.path.basename(path))
        self.waveform.set_segments([])
        self.waveform.set_audio(self.engine.original)

    def _loaded(self, path):
        self._hide_busy()
        name = os.path.basename(path)
        dur = _fmt_time(self.engine.duration)
        self.status.set(f"Loaded: {name}  ({dur})")
        self.ref_btn.config(text="🎯  Play at 1× (Reference)",
                            bg=self._ref_default_bg, fg='black')
        self._update_time()
        # Restore per-song settings
        saved = self.db.load(path)
        if saved:
            if saved.get('loop_a') is not None and saved.get('loop_b') is not None:
                self.engine.loop_a = int(saved['loop_a'])
                self.engine.loop_b = int(saved['loop_b'])
                self._refresh_loop()
            pos = saved.get('position', 0)
            if pos and self.engine.processed is not None:
                self.engine.seek(pos / len(self.engine.processed))
        # EQ: restore this song's saved preset, else default to Mid
        eq_name = saved.get('eq_preset') if saved else None
        if not eq_name or eq_name not in EQ_PRESETS:
            eq_name = 'Mid'
        self._eq_preset(eq_name)

    def _load_err(self, msg):
        self._hide_busy()
        self.status.set(f"Error: {msg}")
        messagebox.showerror("Load Error",
                             f"{msg}\n\nMake sure ffmpeg is installed:\n  brew install ffmpeg")

    def _eject_source(self):
        """Unload the current song entirely — wired to the eject button
        both next to Source and in the YouTube Search row. Blanks both
        waveforms, the Source label, and the Time readout, matching the
        "no source, no waveform" state a fresh launch already shows
        correctly."""
        if not self.engine.loaded or self._busy:
            return
        self._save_song()
        self.engine.unload()
        self.play_btn.config(text='▶')
        self.file_lbl.config(text="No file loaded")
        self.waveform.clear()
        self.zoom_waveform.refresh()
        self._refresh_loop()
        self._update_time()
        self.ref_btn.config(text="🎯  Play at 1× (Reference)",
                            bg=self._ref_default_bg, fg='black')
        self.status.set("Ready")

    # ── Speed / Pitch ──

    def _spd_slide(self, val=None):
        spd = self.spd_var.get()
        self.spd_input_var.set(f"{spd:.2f}")
        # Debounce lives centrally in _request_reprocess() now (used by
        # Speed and Pitch alike) — no separate one needed here.
        self._apply_speed(spd)

    def _spd_preset(self, val):
        self.spd_var.set(val)
        self.spd_input_var.set(f"{val:.2f}")
        self._apply_speed(val)

    def _spd_typed(self, _event=None):
        raw = self.spd_input_var.get().strip().rstrip('x').rstrip()
        try:
            v = float(raw)
        except ValueError:
            self.spd_input_var.set(f"{self.spd_var.get():.2f}")
            return
        v = max(MIN_SPEED, min(MAX_SPEED, v))
        self.spd_var.set(v)
        self.spd_input_var.set(f"{v:.2f}")
        self._apply_speed(v)

    def _apply_speed(self, spd):
        if not self.engine.loaded:
            self.engine.speed = spd
            return
        self._speed_pending = spd
        self._request_reprocess()

    def _apply_pitch(self, semitones):
        self.pitch_var.set(f"{semitones:+d} st" if semitones else "0 st")
        if not self.engine.loaded:
            self.engine.pitch_semitones = semitones
            return
        self._pitch_pending = semitones
        self._request_reprocess()

    # ── Reprocessing (shared by Speed and Pitch) ──

    def _request_reprocess(self):
        """Debounce + coalesce a reprocess for whatever's currently in
        self._speed_pending / self._pitch_pending. Shared by Speed and
        Pitch (both just end up calling engine.process()) rather than
        each running its own separate worker+pending pair — with two
        separate pairs, a click on one control while the other's worker
        was still running got its pending value stranded, since each
        worker's done-callback only ever checked its own flag. And a
        300ms debounce (same window already used for the Speed slider's
        drag) means a quick burst of clicks settles into exactly one
        full WSOLA pass for the final value instead of a chain of full
        passes each racing to catch up with the next click — confirmed
        via screen recording: 5 pitch clicks + a speed change in ~15s of
        real time took nearly 30s of continuous "Processing..." to fully
        drain, one full-song pass at a time."""
        if getattr(self, '_reprocess_after', None):
            try:
                self.root.after_cancel(self._reprocess_after)
            except Exception:
                pass
        self._reprocess_after = self.root.after(300, self._reprocess_debounced)

    def _reprocess_debounced(self):
        self._reprocess_after = None
        if self._busy:
            return  # already running; its own done-callback re-checks both pending flags
        self._run_reprocess_worker()

    def _run_reprocess_worker(self):
        spd = self._speed_pending
        st = self._pitch_pending
        self._speed_pending = None
        self._pitch_pending = None
        if self.engine.reference_mode:
            self.engine.reference_mode = False
            if self.engine.original is not None and len(self.engine.original) > 0:
                ratio_r = self.engine.ref_position / len(self.engine.original)
            else:
                ratio_r = 0.0
            if self.engine.processed is not None:
                self.engine.position = int(ratio_r * len(self.engine.processed))
            self.ref_btn.config(text="🎯  Play at 1× (Reference)",
                                bg=self._ref_default_bg, fg='black')
        was = self.engine.playing
        ratio = self.engine.pos_ratio()
        self.engine.stop()
        self.play_btn.config(text='▶')
        self._show_busy("Processing...")
        def work():
            error = None
            try:
                self.engine.process(speed=spd, pitch_semitones=st)
                if self.engine.processed is not None:
                    self.engine.position = int(ratio * len(self.engine.processed))
            except Exception as e:
                error = str(e)
            self.root.after(0, lambda: self._reprocess_done(was, error))
        threading.Thread(target=work, daemon=True).start()

    def _reprocess_done(self, was, error=None):
        self._hide_busy()
        if error:
            self.status.set(f"Processing failed: {error}")
        elif was and self.engine.processed is not None:
            self.engine.play()
            self.play_btn.config(text='⏸')
        if self._speed_pending is not None or self._pitch_pending is not None:
            self._run_reprocess_worker()

    # ── Pitch (semitone transpose) ──

    def _pitch_step(self, delta, absolute=False):
        cur = self._pitch_pending if self._pitch_pending is not None \
            else self.engine.pitch_semitones
        new_val = delta if absolute else cur + delta
        new_val = max(PITCH_MIN_ST, min(PITCH_MAX_ST, new_val))
        if new_val == cur:
            return
        self._apply_pitch(new_val)

    # ── Volume ──

    def _vol_slide(self, val):
        v = float(val)
        self.engine.volume = v
        self.vol_pct_var.set(str(int(round(v * 100))))

    def _vol_typed(self, _event=None):
        raw = self.vol_pct_var.get().strip().rstrip('%')
        try:
            pct = int(round(float(raw)))
        except ValueError:
            self.vol_pct_var.set(str(int(round(self.engine.volume * 100))))
            return
        pct = max(0, min(100, pct))
        v = pct / 100.0
        self.vol_var.set(v)
        self.engine.volume = v
        self.vol_pct_var.set(str(pct))

    def _pitch_lock_toggle(self):
        if not self.engine.loaded:
            self.engine.pitch_lock = self.pl_var.get()
            return
        if self._busy:
            self.pl_var.set(self.engine.pitch_lock)
            return
        was = self.engine.playing
        ratio = self.engine.pos_ratio()
        self.engine.stop()
        self.play_btn.config(text='▶')
        self._show_busy("Reprocessing...")
        def work():
            error = None
            try:
                self.engine.process(pitch_lock=self.pl_var.get())
                if self.engine.processed is not None:
                    self.engine.position = int(ratio * len(self.engine.processed))
            except Exception as e:
                error = str(e)
            self.root.after(0, lambda: self._done_processing(was, error))
        threading.Thread(target=work, daemon=True).start()

    def _done_processing(self, resume, error=None):
        self._hide_busy()
        if error:
            self.status.set(f"Processing failed: {error}")
            return
        if resume and self.engine.processed is not None:
            self.engine.play()
            self.play_btn.config(text='⏸')

    # ── Transport ──

    def _toggle_play(self):
        if not self.engine.loaded or self._busy:
            return
        if self.engine.playing:
            self.engine.pause()
            self.play_btn.config(text='▶')
        else:
            self.engine.play()
            self.play_btn.config(text='⏸')

    def _stop(self):
        self.engine.stop()
        self.play_btn.config(text='▶')

    def _rw(self):
        if not self.engine.loaded:
            return
        self.engine.seek_relative(-5)
        self.waveform.update_head()
        self._update_time()

    def _ff(self):
        if not self.engine.loaded:
            return
        self.engine.seek_relative(5)
        self.waveform.update_head()
        self._update_time()

    def _skip_start(self):
        if not self.engine.loaded:
            return
        # Jump to loop A if set, else beginning
        if self.engine.loop_a is not None and self.engine.original is not None:
            ratio = self.engine.loop_a / len(self.engine.original)
            self.engine.seek(ratio)
        else:
            self.engine.seek(0)
        self.waveform.update_head()
        self._update_time()

    def _skip_end(self):
        if not self.engine.loaded:
            return
        # Jump to loop B if set, else near end
        if self.engine.loop_b is not None and self.engine.original is not None:
            ratio = self.engine.loop_b / len(self.engine.original)
            self.engine.seek(ratio)
        else:
            self.engine.seek(0.99)
        self.waveform.update_head()
        self._update_time()

    def _on_playback_end(self):
        self.root.after(0, lambda: self.play_btn.config(text='▶'))

    # ── Reference ──

    def _toggle_reference(self):
        if not self.engine.loaded:
            return
        was_playing = self.engine.playing
        self.engine.stop()
        self.play_btn.config(text='▶')
        self.engine.toggle_reference()
        if self.engine.reference_mode:
            self.ref_btn.config(text="◉  Reference ON — Click for practice speed",
                                bg='#ffb347', fg='black')
        else:
            self.ref_btn.config(text="🎯  Play at 1× (Reference)",
                                bg=self._ref_default_bg, fg='black')
        self.waveform.update_head()
        self._update_time()
        if was_playing:
            self.engine.play()
            self.play_btn.config(text='⏸')

    # ── Loop ──

    def _set_a(self):
        if self.engine.original is not None:
            self.engine.loop_a = int(self.engine.pos_ratio() * len(self.engine.original))
            self._refresh_loop()

    def _set_b(self):
        if self.engine.original is not None:
            self.engine.loop_b = int(self.engine.pos_ratio() * len(self.engine.original))
            if self.engine.loop_a is not None and self.engine.loop_b < self.engine.loop_a:
                self.engine.loop_a, self.engine.loop_b = self.engine.loop_b, self.engine.loop_a
            self._refresh_loop()

    def _clr_loop(self):
        self.engine.loop_a = None
        self.engine.loop_b = None
        self._refresh_loop()
        # Persist immediately — otherwise the old A-B loop reappears next
        # time this song is loaded, since it's normally saved on quit/switch.
        if self.engine.file_path and self.engine.loaded:
            self.db.save(self.engine.file_path, self.engine.speed,
                         self.engine.pitch_lock, None, None,
                         self.engine.position, self._eq_active)

    def _refresh_loop(self):
        a, b = self.engine.loop_a, self.engine.loop_b
        if a is not None and b is not None:
            ta = _fmt_time(a / self.engine.sr)
            tb = _fmt_time(b / self.engine.sr)
            self.loop_lbl.config(text=f"A: {ta}  →  B: {tb}")
        elif a is not None:
            self.loop_lbl.config(text=f"A: {_fmt_time(a / self.engine.sr)}  →  B: ...")
        else:
            self.loop_lbl.config(text="No loop set")
        self.waveform._draw()

    # ── Menubar / Plugin surface ──

    def _build_menubar(self):
        menubar = tk.Menu(self.root)
        self.plugin_menu = tk.Menu(menubar, tearoff=0)
        self.plugin_menu.add_command(label="Manage Plugins…",
                                      command=self._open_plugin_manager)
        self.plugin_menu.add_command(label="Open Plugins Folder…",
                                      command=self._open_plugins_folder)
        self.plugin_menu.add_command(label="Clear Detected Sections",
                                      command=self._clear_sections)
        self.plugin_menu.add_separator()
        menubar.add_cascade(label="Plugins", menu=self.plugin_menu)
        # Deliberately NOT name='help', and the label is "Help " (trailing
        # space) rather than an exact "Help" match. Naming the Tcl widget
        # 'help' OR labeling the cascade the exact string "Help" both make
        # modern macOS auto-splice its own content into this menu — a
        # search field, a phantom "<App> Help" item with no real Help Book
        # behind it (dead link), and a "Send Feedback to Apple" item Apple
        # injects into any app whose Help menu title matches exactly. The
        # trailing space is invisible to the user but breaks that exact
        # match, so only the items added below appear.
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label=f"{APP_NAME} Help", command=self._open_help)
        help_menu.add_separator()
        help_menu.add_command(label=f"Uninstall {APP_NAME}…", command=self._uninstall)
        menubar.add_cascade(label="Help ", menu=help_menu)
        self.root.config(menu=menubar)
        # Snapshot the count of built-in items so plugins can add after them
        self._plugin_menu_static_end = self.plugin_menu.index('end')
        # Convenient accessor for plugins
        self.get_plugin_menu = lambda: self.plugin_menu

    def _open_help(self):
        path = _resource_path('HELP.html')
        if os.path.isfile(path):
            subprocess.run(['open', path], check=False)
        else:
            messagebox.showerror("Help", "HELP.html not found.")

    def _uninstall(self):
        app_path = f'/Applications/{APP_NAME}.app'
        data_dir = _data_dir()
        msg = (
            f"This will permanently remove {APP_NAME} from your Mac:\n\n"
            f"  • {app_path}\n"
            f"  • {data_dir}\n"
            "    (preferences, saved loops, EQ settings, plugins, audio cache)\n\n"
            "This cannot be undone. Continue?"
        )
        if not messagebox.askyesno(f"Uninstall {APP_NAME}", msg, icon='warning'):
            return
        try:
            if os.path.isdir(data_dir):
                shutil.rmtree(data_dir, ignore_errors=True)
        except Exception:
            pass
        # Remove the .app bundle from a detached shell after this process
        # exits, since we're currently running from inside it.
        try:
            subprocess.Popen(
                ['/bin/sh', '-c', f'sleep 1; rm -rf "{app_path}"'])
        except Exception:
            pass
        self.root.destroy()
        sys.exit(0)

    def _open_plugins_folder(self):
        try:
            subprocess.run(['open', _plugins_dir()], check=False)
        except Exception:
            pass

    def _open_plugin_manager(self):
        win = tk.Toplevel(self.root)
        win.title("Manage Plugins")
        win.geometry("480x380")
        win.minsize(420, 260)
        win.transient(self.root)

        ttk.Label(win, text="Installed Plugins", font=('Helvetica', 14, 'bold'),
                  padding=(14, 12, 14, 4)).pack(anchor='w')

        plugins = _scan_plugin_metadata()
        list_frame = ttk.Frame(win, padding=(14, 0, 14, 0))
        list_frame.pack(fill='both', expand=True)

        restart_note = ttk.Label(win, text="", foreground='#ffb74d',
                                  padding=(14, 4))
        restart_note.pack(fill='x')

        if not plugins:
            ttk.Label(list_frame,
                      text="No plugins installed.\n\nOfficial plugins ship "
                           f"through the {APP_NAME} GitHub repo — install a "
                           "released one by dropping its file into the "
                           "plugins folder.",
                      foreground='#888', justify='center',
                      wraplength=380).pack(expand=True, pady=40)
        else:
            for meta in plugins:
                row = ttk.Frame(list_frame, padding=(0, 8))
                row.pack(fill='x')
                top = ttk.Frame(row)
                top.pack(fill='x')
                ttk.Label(top, text=meta['name'],
                          font=('Helvetica', 12, 'bold')).pack(side='left')
                toggle_btn = tk.Label(top, width=3, font=('Helvetica', 12, 'bold'),
                                       relief='flat', cursor='pointinghand')
                toggle_btn.pack(side='right')
                ttk.Label(row, text=meta['description'], foreground='#888',
                          wraplength=420, justify='left').pack(anchor='w', pady=(2, 0))
                ttk.Separator(list_frame, orient='horizontal').pack(fill='x', pady=(4, 0))

                def refresh_toggle(btn=toggle_btn, m=meta):
                    if m['enabled']:
                        btn.config(text='✓', fg='white', bg='#4CAF50')
                    else:
                        btn.config(text='✗', fg='white', bg='#e57373')

                def on_toggle(event=None, btn=toggle_btn, m=meta):
                    m['enabled'] = not m['enabled']
                    state = _load_plugin_state()
                    state[m['fname']] = m['enabled']
                    _save_plugin_state(state)
                    refresh_toggle(btn, m)
                    restart_note.config(text=f"Restart {APP_NAME} for changes to take effect.")

                toggle_btn.bind('<Button-1>', on_toggle)
                refresh_toggle()

        btn_row = ttk.Frame(win, padding=12)
        btn_row.pack(fill='x')
        ttk.Button(btn_row, text="Open Plugins Folder…",
                   command=self._open_plugins_folder).pack(side='left')
        ttk.Button(btn_row, text="Close", command=win.destroy).pack(side='right')

    def _clear_sections(self):
        self.waveform.set_segments([])

    # ── Plugin-facing helpers (stable API for plugins) ──

    def set_segments(self, segments):
        """Plugins call this to display detected sections on the waveform."""
        self.waveform.set_segments(segments or [])

    def _on_segment_click(self, seg):
        if self.engine.original is None:
            return
        a = int(seg['start'] * self.engine.sr)
        b = int(seg['end'] * self.engine.sr)
        self.engine.loop_a = a
        self.engine.loop_b = b
        self._refresh_loop()
        # Also seek to loop start
        self.engine.seek(a / len(self.engine.original))
        self.waveform.update_head()
        self._update_time()
        self.status.set(f"Loop set: {seg['name']} ({_fmt_time(seg['start'])} – {_fmt_time(seg['end'])})")

    # ── EQ ──

    def _eq_preset(self, name):
        gains = EQ_PRESETS.get(name)
        if gains is None:
            return
        self._eq_active = name
        self._eq_highlight()
        if not self.engine.loaded:
            return
        bands = list(zip(EQ_FREQS, gains))
        self.engine.apply_eq(bands)

    def _eq_highlight(self):
        colors = {'Bass': '#4fc3f7', 'Mid': '#fff176', 'Treble': '#ef9a9a',
                  'Flat': '#66BB6A'}
        for name, btn in self._eq_btns.items():
            if name == self._eq_active:
                btn.config(relief='sunken', bg=colors[name], fg='black')
            else:
                try:
                    btn.config(relief='raised', bg='SystemButtonFace', fg='black')
                except Exception:
                    btn.config(relief='raised', bg='#d9d9d9', fg='black')

    # ── Busy indicator ──

    # A full-song WSOLA pass on a long track at slow speed (plus a pitch
    # shift, which adds its own resample pass) can genuinely take 20+
    # real seconds — measured 23.56s for a 6:59 song at 0.5x with a
    # semitone shift. The old first checkpoint at 30s meant that entire
    # wait showed a static, unchanging "Processing..." the whole time,
    # indistinguishable from a hang. First ping quickly, then a steady
    # ~15s heartbeat after that, so a long pass gets repeated
    # acknowledgment throughout instead of one checkpoint near the end.
    _BUSY_STAGES = [
        (0,   "{text}"),
        (5,   "Still {verb}..."),
        (20,  "Still {verb}... (long or slow-speed songs take longer)"),
        (35,  "Still {verb}..."),
        (50,  "Still {verb}... hang tight"),
        (65,  "Still {verb}... this one's taking a while"),
        (95,  "Still {verb}... sorry for the wait"),
    ]

    def _show_busy(self, text):
        self._busy = True
        self._busy_text = text
        # Derive a verb from the initial message ("Loading audio..." → "loading")
        first_word = text.split()[0] if text else "working"
        self._busy_verb = first_word.rstrip('.').lower()
        self.waveform.set_busy(True)
        self._spinner.place(relx=0.5, rely=0.5, anchor='center', y=-16)
        self._spin_lbl.config(text=text)
        self._spin_lbl.place(relx=0.5, rely=1.0, anchor='s', y=-6)
        self._spinner.start()
        self.status.set(text)
        if getattr(self, '_busy_after', None):
            try:
                self.root.after_cancel(self._busy_after)
            except Exception:
                pass
        self._busy_after = self.root.after(1000, self._busy_tick)
        self._busy_start = self._monotime()

    def _monotime(self):
        import time
        return time.monotonic()

    def _busy_tick(self):
        if not self._busy:
            return
        elapsed = int(self._monotime() - self._busy_start)
        # Find the highest stage whose threshold ≤ elapsed
        chosen = self._BUSY_STAGES[0]
        for stage in self._BUSY_STAGES:
            if elapsed >= stage[0]:
                chosen = stage
        msg = chosen[1].format(text=self._busy_text, verb=self._busy_verb)
        self._spin_lbl.config(text=msg)
        self.status.set(msg)
        self._busy_after = self.root.after(1000, self._busy_tick)

    def _hide_busy(self):
        self._busy = False
        if getattr(self, '_busy_after', None):
            try:
                self.root.after_cancel(self._busy_after)
            except Exception:
                pass
            self._busy_after = None
        # Redraw the real waveform first, while still hidden behind the
        # spinner, so there's no frame where the spinner is gone but the
        # canvas is still the blank busy placeholder underneath it.
        self.waveform.set_busy(False)
        # Hide first, *then* reset to frame 0 — stop() snaps the visible
        # image back to frame 0 immediately, so calling it before
        # place_forget() let that snap flash on screen for a moment right
        # as the waveform underneath was also updating, looking like a
        # rendering glitch. Hiding first means the reset happens off-screen.
        self._spinner.place_forget()
        self._spin_lbl.place_forget()
        self._spinner.stop()
        self.status.set("Ready")

    # ── Tick ──

    def _tick(self):
        if self.engine.playing:
            self.waveform.update_head()
            self._update_time()
        if self.engine.loaded:
            self.zoom_waveform.refresh()
        if self.engine._eq_error:
            self.status.set(f"EQ change failed: {self.engine._eq_error}")
            self.engine._eq_error = None
        self._update_id = self.root.after(50, self._tick)

    def _update_time(self):
        cur = _fmt_time_lcd(self.engine.current_time())
        tot = _fmt_time(self.engine.total_time())
        self.time_lbl.config(text=cur)
        self.time_tot_lbl.config(text=f"/ {tot}")

    def _on_scrub(self):
        """Fired by either waveform view after a click/drag seek — keeps
        the Time display and both waveform views in sync immediately,
        instead of waiting for the next 50ms tick (which only runs while
        playing)."""
        self.waveform.update_head()
        self.zoom_waveform.refresh()
        self._update_time()

    # ── Save / Quit ──

    def _save_song(self):
        if self.engine.file_path and self.engine.loaded:
            self.db.save(self.engine.file_path, self.engine.speed,
                         self.engine.pitch_lock,
                         self.engine.loop_a, self.engine.loop_b,
                         self.engine.position, self._eq_active,
                         self.engine.pitch_semitones)

    def _quit(self):
        self._save_song()
        self.engine.stop()
        self._save_prefs()
        if self._update_id:
            self.root.after_cancel(self._update_id)
        try:
            td = _temp_dir()
            for f in os.listdir(td):
                os.remove(os.path.join(td, f))
            os.rmdir(td)
        except Exception:
            pass
        self.root.destroy()


# ─── Entry Point ─────────────────────────────────────────────────────

def run_selftest():
    """Headless sanity check of the core pipeline — no GUI, no audio
    playback. Exits 0 if everything checks out, 1 otherwise."""
    print(f"{APP_NAME} v{VERSION} — self-test")
    failures = []

    def check(label, fn):
        try:
            fn()
            print(f"  [PASS] {label}")
        except Exception as e:
            print(f"  [FAIL] {label}: {e}")
            failures.append(label)

    def check_ffmpeg():
        if not _find_tool('ffmpeg'):
            raise RuntimeError("ffmpeg not found on PATH")

    def check_wsola():
        sr = 44100
        t = np.arange(sr * 2) / sr
        tone = (np.sin(2 * np.pi * 220 * t) * 0.6).astype(np.float32)
        out = wsola_stretch(tone, 0.5, sr)
        if len(out) == 0:
            raise RuntimeError("empty output")
        if np.any(np.isnan(out)) or np.any(np.isinf(out)):
            raise RuntimeError("NaN/Inf in output")
        if np.max(np.abs(out)) > 1.0:
            raise RuntimeError(f"clipping: peak={np.max(np.abs(out)):.3f}")

    def check_eq():
        sr = 44100
        tone = (np.sin(2 * np.pi * 440 * np.arange(sr) / sr) * 0.5).astype(np.float32)
        bands = list(zip(EQ_FREQS, EQ_PRESETS['Bass']))
        out = _apply_eq_fft(tone, sr, bands, da_filter=True)
        if np.any(np.isnan(out)) or np.any(np.isinf(out)):
            raise RuntimeError("NaN/Inf in EQ output")
        if np.max(np.abs(out)) > 1.0:
            raise RuntimeError(f"clipping: peak={np.max(np.abs(out)):.3f}")

    def check_da_filter():
        sr = 44100
        t = np.arange(sr) / sr
        high = (np.sin(2 * np.pi * 18000 * t) * 0.5).astype(np.float32)
        flat_bands = [(f, 0) for f in EQ_FREQS]
        out = _apply_eq_fft(high, sr, flat_bands, da_filter=True)
        in_rms = np.sqrt(np.mean(high ** 2))
        out_rms = np.sqrt(np.mean(out ** 2))
        if out_rms >= in_rms:
            raise RuntimeError("18kHz content was not attenuated")

    def check_limiter():
        sr = 44100
        t = np.arange(sr) / sr
        loud = (np.sin(2 * np.pi * 220 * t) * 2.0).astype(np.float32)
        out = _lookahead_limit(loud, sr=sr)
        if np.max(np.abs(out)) > 1.0:
            raise RuntimeError(f"limiter let peak through: {np.max(np.abs(out)):.3f}")

    def check_db():
        tmp_dir = tempfile.mkdtemp(prefix='notexnote_selftest_')
        try:
            conn = sqlite3.connect(os.path.join(tmp_dir, 'test.db'))
            conn.execute('CREATE TABLE t (a TEXT)')
            conn.execute('INSERT INTO t VALUES (?)', ('ok',))
            conn.commit()
            row = conn.execute('SELECT a FROM t').fetchone()
            conn.close()
            if row[0] != 'ok':
                raise RuntimeError("round-trip mismatch")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def check_resources():
        for fname in ('HELP.html', 'progress.gif'):
            p = _resource_path(fname)
            if not os.path.isfile(p):
                raise RuntimeError(f"{fname} not found at {p}")

    def check_plugin_scan():
        _scan_plugin_metadata()  # must not raise even with zero plugins

    check("ffmpeg on PATH", check_ffmpeg)
    check("WSOLA time-stretch (0.5x, no NaN/clip)", check_wsola)
    check("EQ FFT processing (no NaN/clip)", check_eq)
    check("D/A filter attenuates high frequencies", check_da_filter)
    check("Look-ahead limiter caps peaks", check_limiter)
    check("SQLite read/write", check_db)
    check("Bundled resources present", check_resources)
    check("Plugin metadata scan", check_plugin_scan)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


def main():
    root = tk.Tk()
    missing = []
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append('numpy')
    try:
        import sounddevice  # noqa: F401
    except ImportError:
        missing.append('sounddevice')
    if missing:
        root.withdraw()
        messagebox.showerror("Missing Dependencies",
                             f"Install: pip install {' '.join(missing)}")
        return
    if not _find_tool('ffmpeg'):
        root.withdraw()
        messagebox.showerror("Missing ffmpeg",
                             "Install with: brew install ffmpeg")
        return
    NoteXNoteApp(root)
    root.mainloop()


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.exit(run_selftest())
    main()
