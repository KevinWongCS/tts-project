#!/usr/bin/env python3
"""
reader.py - a background text-to-speech utility for Windows and macOS.

Highlight text in ANY application and press a hotkey; the text is read aloud
with Microsoft Edge's neural voices (via `edge-tts`) and played back with
`pygame`.

    Alt + A  (Option + A on macOS)   read the currently highlighted text
    Alt + S  (Option + S on macOS)   stop speaking and clear the queue

How it works
------------
1. A global hotkey listener (`pynput`) runs in a background daemon thread.
2. On "read", the app temporarily borrows the clipboard: it saves what you had,
   simulates Ctrl+C / Cmd+C, grabs the copied text and restores your clipboard.
3. The text is cleaned (PDF/web line breaks joined, URLs removed) and split
   into sentences.
4. A producer thread synthesizes each sentence to a temporary .mp3 with
   `edge-tts` while a player thread plays finished sentences in order, so the
   first sentence starts speaking while the rest are still being generated.
5. A system-tray / menu-bar icon (`pystray`) lets you choose the voice and the
   speed, stop speaking, or quit. Choices are saved to `config.json`.

Threading model
---------------
* Main thread       : the `pystray` event loop (REQUIRED on macOS).
* Hotkey thread     : `pynput` listener (daemon thread).
* Capture thread    : short-lived worker that does the clipboard dance so the
                      hotkey listener is never blocked.
* Producer thread   : owns an asyncio loop, runs `edge-tts` synthesis.
* Player thread     : the ONLY thread that touches `pygame.mixer`.

Interrupting speech never kills threads. It bumps a "generation" counter;
every worker checks it and silently discards work that belongs to an older
generation. That is what makes Alt+A / Alt+S safe to mash while audio plays.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import json
import logging
import os
import queue
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Dict, List, Optional

# Hide pygame's "Hello from the pygame community" banner. Must be set before import.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import edge_tts  # noqa: E402
import pygame  # noqa: E402
import pyperclip  # noqa: E402
import pystray  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from pynput import keyboard  # noqa: E402

__version__ = "1.0.0"  # the release workflow stamps this from the git tag

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")

if IS_MAC:
    import Quartz  # installed automatically with pynput on macOS (pyobjc)

if IS_WINDOWS:
    import ctypes

log = logging.getLogger("tts-reader")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

APP_NAME = "TTS Reader"


def user_data_dir() -> Path:
    """Per-user folder for settings and logs (same location from source or when packaged)."""
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "TTSReader"


DATA_DIR = user_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
# Earlier versions kept config.json next to reader.py; it is still read once if present.
LEGACY_CONFIG_PATH = Path(__file__).resolve().with_name("config.json")

# voice id -> label shown in the tray menu (insertion order = menu order)
VOICES: Dict[str, str] = {
    "en-US-AndrewNeural": "Andrew (US, male)",
    "en-US-AriaNeural": "Aria (US, female)",
    "en-US-BrianNeural": "Brian (US, male)",
    "en-US-AvaNeural": "Ava (US, female)",
    "en-GB-SoniaNeural": "Sonia (UK, female)",
    "en-GB-RyanNeural": "Ryan (UK, male)",
}
SPEEDS = (1.0, 1.25, 1.5, 1.75, 2.0)
DEFAULT_VOICE = "en-US-AndrewNeural"
DEFAULT_SPEED = 1.0

MAX_CHUNK_CHARS = 300        # sentences longer than this are split further
PREFETCH_CHUNKS = 3          # how many synthesized sentences may wait for playback
SYNTH_CONCURRENCY = 3        # sentences generated in parallel (still played in order)
FIRST_CHUNK_CHARS = 90       # the first chunk is kept short so speech starts sooner
MIN_HEAD_CHARS = 25          # ...but never split off a head shorter than this
COPY_DELAY_S = 0.10          # time given to the OS to register the simulated copy
EXTRA_COPY_POLLS = 4         # extra 50 ms polls if the clipboard is not ready yet
SYNTH_TIMEOUT_S = 30         # per-sentence network timeout
SYNTH_ATTEMPTS = 2           # tries per sentence before giving up on it
MODIFIER_RELEASE_TIMEOUT_S = 1.0

# Hotkey key identification (virtual key codes differ per platform).
# macOS: kVK_ANSI_A = 0, kVK_ANSI_S = 1 (physical key positions).
# Windows: VK_A = 0x41, VK_S = 0x53.   Other: X11 keysyms.
if IS_MAC:
    VK_ACTIONS = {0: "speak", 1: "stop"}
elif IS_WINDOWS:
    VK_ACTIONS = {0x41: "speak", 0x53: "stop"}
else:
    VK_ACTIONS = {0x61: "speak", 0x41: "speak", 0x73: "stop", 0x53: "stop"}
CHAR_ACTIONS = {"a": "speak", "s": "stop"}


# --------------------------------------------------------------------------- #
# Text cleaning and sentence chunking (pure functions, no side effects)
# --------------------------------------------------------------------------- #

_URL_RE = re.compile(r"""(?:https?://|www\.)[^\s<>"')\]]+""", re.IGNORECASE)
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])(?=\s|$)")
_EMPTY_BRACKETS_RE = re.compile(r"\(\s*\)|\[\s*\]|<\s*>|\{\s*\}")
_INVISIBLE_RE = re.compile("[­​‌‍⁠﻿]")
_HYPHEN_BREAK_RE = re.compile(r"([a-z])-[ \t]*\n[ \t]*([a-z])")
_LIST_ITEM_RE = re.compile(
    r"\n(?=[ \t]*(?:[•●▪◦‣▫■□–—\-*]|\d{1,3}[.)])[ \t])"
)
_BULLET_GLYPH_RE = re.compile(r"^[•●▪◦‣▫■□]\s*")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_END_RE = re.compile(r"""([.!?…]+["'”’)\]]*)(\s+)""")

# Words that end in a period but do not end a sentence ("Dr. Smith", "e.g. this").
_ABBREVIATIONS = frozenset(
    """mr mrs ms dr prof sr jr vs etc e.g i.e fig figs vol vols inc ltd approx dept
    al cf pp gen col lt sgt capt rev hon mt ft jan feb apr aug sept oct nov dec""".split()
)


def _strip_url(match: "re.Match[str]") -> str:
    """Drop a URL but keep sentence punctuation that was glued to its end."""
    tail = re.search(r"[.,;:!?]+$", match.group(0))
    return tail.group(0) if tail else ""


def clean_text(raw: str) -> str:
    """
    Turn messy copied text (PDF, web page, email) into clean paragraphs.

    * removes raw http/https/www URLs so they are not read aloud
    * re-joins words hyphenated across a line break ("exam-\\nple" -> "example")
    * joins hard-wrapped lines inside a paragraph into one line
    * keeps blank lines and list items as paragraph boundaries
    * collapses runs of spaces/tabs

    Returns paragraphs separated by a blank line ("\\n\\n").
    """
    if not raw:
        return ""

    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n")
    text = _INVISIBLE_RE.sub("", text).replace(" ", " ")
    text = _URL_RE.sub(_strip_url, text)
    text = _EMPTY_BRACKETS_RE.sub("", text)
    text = _HYPHEN_BREAK_RE.sub(r"\1\2", text)
    text = _LIST_ITEM_RE.sub("\n\n", text)  # every list item is its own paragraph

    paragraphs: List[str] = []
    for block in _PARAGRAPH_RE.split(text):
        line = re.sub(r"\s+", " ", block).strip()  # joins wrapped lines, collapses spaces
        line = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", line)  # "Visit ." (left by a removed URL) -> "Visit."
        line = _BULLET_GLYPH_RE.sub("", line)
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def split_sentences(paragraph: str) -> List[str]:
    """Split one paragraph into sentences, tolerating common abbreviations."""
    sentences: List[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(paragraph):
        punct = match.group(1)
        next_char = paragraph[match.end():match.end() + 1]
        words = paragraph[start:match.start(1)].split()
        last_word = words[-1].lower().strip("(\"'“‘[") if words else ""

        if punct.startswith("."):
            if next_char.islower():
                continue  # "approx. five", "e.g. this" -> same sentence
            if last_word in _ABBREVIATIONS:
                continue  # "Dr. Smith"
            if len(last_word) == 1 and last_word.isalpha():
                continue  # initials: "J. K. Rowling"
            if len(words) == 1 and last_word.isdigit() and len(last_word) <= 3:
                continue  # list numbering: "1. First item"

        sentence = paragraph[start:match.end(1)].strip()
        if sentence:
            sentences.append(sentence)
        start = match.end()

    tail = paragraph[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _wrap_long(sentence: str, limit: int = MAX_CHUNK_CHARS) -> List[str]:
    """Break an over-long sentence at commas/semicolons, then at spaces."""
    if len(sentence) <= limit:
        return [sentence]

    out: List[str] = []
    current = ""
    for piece in re.split(r"(?<=[,;:—])\s+", sentence):
        while len(piece) > limit:  # a single clause that is still too long
            cut = piece.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            head, piece = piece[:cut].strip(), piece[cut:].strip()
            if current:
                out.append(current)
                current = ""
            if head:
                out.append(head)
        if not piece:
            continue
        if current and len(current) + 1 + len(piece) > limit:
            out.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        out.append(current)
    return out


def _split_head(chunk: str, limit: int = FIRST_CHUNK_CHARS) -> List[str]:
    """
    Split a long first chunk into a short head + the rest, preferably at a
    comma/semicolon, so the first audio is ready sooner.
    """
    if len(chunk) <= limit:
        return [chunk]
    window = chunk[:limit]
    cut = max(window.rfind(", "), window.rfind("; "), window.rfind(": "))
    if cut >= MIN_HEAD_CHARS:
        cut += 1  # keep the punctuation with the head
    else:
        cut = window.rfind(" ")
        if cut < MIN_HEAD_CHARS:
            return [chunk]
    head, rest = chunk[:cut].strip(), chunk[cut:].strip()
    return [head, rest] if head and rest else [chunk]


def prepare_chunks(raw: str) -> List[str]:
    """Raw selection -> ordered list of speakable chunks (usually sentences)."""
    chunks: List[str] = []
    for paragraph in clean_text(raw).split("\n\n"):
        for sentence in split_sentences(paragraph):
            for piece in _wrap_long(sentence):
                if re.search(r"\w", piece):  # skip chunks with nothing to pronounce
                    chunks.append(piece)
    if chunks:
        chunks[0:1] = _split_head(chunks[0])  # short first chunk = faster time-to-first-word
    return chunks


# --------------------------------------------------------------------------- #
# Persistent settings
# --------------------------------------------------------------------------- #

class Settings:
    """Voice + speed, stored in config.json next to this script."""

    def __init__(self, path: Path = CONFIG_PATH, legacy_path: Optional[Path] = None) -> None:
        self._path = path
        self._legacy_path = legacy_path
        self._lock = threading.RLock()
        self._voice = DEFAULT_VOICE
        self._speed = DEFAULT_SPEED
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        source = self._path
        if not source.exists() and self._legacy_path is not None and self._legacy_path.exists():
            source = self._legacy_path  # first run after upgrading: keep the old preferences
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Could not read %s (%s); using defaults.", source, exc)
            return
        if not isinstance(data, dict):
            return
        voice = data.get("voice")
        if voice in VOICES:
            self._voice = voice
        try:
            speed = float(data.get("speed", DEFAULT_SPEED))
        except (TypeError, ValueError):
            speed = DEFAULT_SPEED
        match = [s for s in SPEEDS if abs(s - speed) < 1e-6]
        if match:
            self._speed = match[0]

    def _save(self) -> None:
        payload = json.dumps({"voice": self._voice, "speed": self._speed}, indent=2)
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)  # atomic: never leaves a half-written file
        except OSError as exc:
            log.warning("Could not save settings to %s: %s", self._path, exc)

    # -- accessors ---------------------------------------------------------
    @property
    def voice(self) -> str:
        with self._lock:
            return self._voice

    @property
    def speed(self) -> float:
        with self._lock:
            return self._speed

    @property
    def rate(self) -> str:
        """Speed as an edge-tts rate string: 1.0 -> '+0%', 1.25 -> '+25%'."""
        with self._lock:
            return f"{round((self._speed - 1.0) * 100):+d}%"

    def set_voice(self, voice: str) -> None:
        if voice not in VOICES:
            return
        with self._lock:
            self._voice = voice
            self._save()

    def set_speed(self, speed: float) -> None:
        if speed not in SPEEDS:
            return
        with self._lock:
            self._speed = speed
            self._save()


# --------------------------------------------------------------------------- #
# Speech engine: synthesis (producer) + playback (player)
# --------------------------------------------------------------------------- #

@dataclass
class _Job:
    generation: int
    chunks: List[str]
    voice: str
    rate: str


class SpeechEngine:
    """
    Pipelined text-to-speech.

    speak(chunks) -> the producer thread turns each chunk into an .mp3 and the
    player thread plays them in order, deleting every file after use.
    stop()        -> bumps the generation counter; everything queued or playing
                     for older generations is dropped within ~20 ms.
    """

    def __init__(self, notify: Callable[[str], None]) -> None:
        self._notify = notify
        self._generation = 0
        self._gen_lock = threading.Lock()
        self._jobs: "queue.Queue[_Job]" = queue.Queue()
        self._audio: "queue.Queue[tuple[int, str]]" = queue.Queue(maxsize=PREFETCH_CHUNKS)
        self._shutdown = threading.Event()
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="tts_reader_"))

        pygame.mixer.init()  # raises pygame.error if there is no audio device

        self._producer = threading.Thread(target=self._producer_loop, name="tts-producer", daemon=True)
        self._player = threading.Thread(target=self._player_loop, name="tts-player", daemon=True)
        self._producer.start()
        self._player.start()

    # -- public API --------------------------------------------------------
    def speak(self, chunks: List[str], voice: str, rate: str) -> None:
        """Interrupt anything currently speaking and read `chunks` instead."""
        if not chunks or self._shutdown.is_set():
            return
        with self._gen_lock:
            self._generation += 1
            generation = self._generation
        self._jobs.put(_Job(generation, list(chunks), voice, rate))

    def stop(self) -> None:
        """Stop playback immediately and discard everything still queued."""
        with self._gen_lock:
            self._generation += 1

    def shutdown(self) -> None:
        """Stop everything, join the workers and delete temporary files."""
        if self._shutdown.is_set():
            return
        self.stop()
        self._shutdown.set()
        for thread in (self._player, self._producer):
            thread.join(timeout=2.0)
        try:
            pygame.mixer.quit()
        except pygame.error:
            pass
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def _is_current(self, generation: int) -> bool:
        with self._gen_lock:
            return generation == self._generation and not self._shutdown.is_set()

    def _new_path(self) -> str:
        return str(self._tmp_dir / f"{uuid.uuid4().hex}.mp3")

    @staticmethod
    def _remove(path: str) -> None:
        # On Windows a file can stay locked for a moment after playback.
        for _ in range(10):
            try:
                os.remove(path)
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.05)
        log.debug("Could not delete temporary file %s", path)

    async def _synthesize(self, text: str, voice: str, rate: str, path: str) -> None:
        """Generate `text` into an mp3 file at `path` (runs on the producer's asyncio loop)."""
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await asyncio.wait_for(communicate.save(path), timeout=SYNTH_TIMEOUT_S)

    async def _synthesize_one(self, chunk: str, job: _Job, path: str) -> bool:
        """Synthesize one chunk with retries. Returns True if `path` holds audio."""
        for attempt in range(1, SYNTH_ATTEMPTS + 1):
            if not self._is_current(job.generation):
                return False
            try:
                await self._synthesize(chunk, job.voice, job.rate, path)
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    return True
                log.warning("edge-tts produced no audio (attempt %d).", attempt)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors, NoAudioReceived, timeouts...
                log.warning("Speech synthesis failed (attempt %d): %s", attempt, exc)
            self._remove(path)
            await asyncio.sleep(0.3)
        return False

    async def _enqueue(self, generation: int, path: str) -> bool:
        """Hand a finished file to the player; give up if the job went stale."""
        while True:
            if not self._is_current(generation):
                self._remove(path)
                return False
            try:
                self._audio.put_nowait((generation, path))
                return True
            except queue.Full:
                await asyncio.sleep(0.03)

    async def _await_result(self, task: "asyncio.Future[bool]", generation: int) -> Optional[bool]:
        """Wait for a synthesis task, but notice interruptions within ~50 ms."""
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.05)
            if done:
                return task.result()
            if not self._is_current(generation):
                return None

    async def _run_job(self, job: _Job) -> None:
        """
        Synthesize a job's chunks with up to SYNTH_CONCURRENCY running in
        parallel, but hand them to the player strictly in order.
        """
        started = time.monotonic()
        pending: "collections.deque[tuple[asyncio.Future, str]]" = collections.deque()
        chunks = iter(job.chunks)
        exhausted = False
        failures = 0
        first_audio_logged = False
        try:
            while True:
                while not exhausted and len(pending) < SYNTH_CONCURRENCY:
                    chunk = next(chunks, None)
                    if chunk is None:
                        exhausted = True
                        break
                    path = self._new_path()
                    pending.append((asyncio.ensure_future(self._synthesize_one(chunk, job, path)), path))
                if not pending:
                    return
                task, path = pending[0]  # peek: stays in `pending` until done, so cleanup can cancel it
                ok = await self._await_result(task, job.generation)
                if ok is None:  # interrupted while waiting; `finally` cancels and cleans up
                    return
                pending.popleft()
                if not ok:
                    self._remove(path)
                    failures += 1
                    if failures == 1:
                        self._notify("Could not generate speech. Check your internet connection.")
                    if failures >= 2:
                        return  # offline: do not burn through the whole selection
                    continue
                failures = 0
                if not first_audio_logged:
                    first_audio_logged = True
                    log.info("First audio ready after %.2fs.", time.monotonic() - started)
                if not await self._enqueue(job.generation, path):
                    return
        finally:
            # Cancel anything still running and delete its files.
            leftovers = list(pending)
            for task, _ in leftovers:
                task.cancel()
            if leftovers:
                await asyncio.gather(*(t for t, _ in leftovers), return_exceptions=True)
                for _, path in leftovers:
                    self._remove(path)

    # -- worker threads ----------------------------------------------------
    def _producer_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self._shutdown.is_set():
                try:
                    job = self._jobs.get(timeout=0.1)
                except queue.Empty:
                    continue
                if not self._is_current(job.generation):
                    continue  # superseded before we even started
                try:
                    loop.run_until_complete(self._run_job(job))
                except Exception:
                    log.exception("Speech job failed")
        finally:
            loop.close()

    def _player_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                generation, path = self._audio.get(timeout=0.2)
            except queue.Empty:
                continue
            if not self._is_current(generation):
                self._remove(path)  # stale: belongs to interrupted speech
                continue
            self._play_file(generation, path)

    def _play_file(self, generation: int, path: str) -> None:
        music = pygame.mixer.music
        try:
            music.load(path)
            music.play()
            while music.get_busy() and self._is_current(generation):
                time.sleep(0.02)
        except pygame.error as exc:
            log.warning("Playback error: %s", exc)
        finally:
            try:
                music.stop()
                music.unload()  # release the file handle so it can be deleted (Windows)
            except pygame.error:
                pass
            self._remove(path)


# --------------------------------------------------------------------------- #
# Modifier-key state and clipboard capture
# --------------------------------------------------------------------------- #

def alt_is_down() -> bool:
    """Is Alt (Option on macOS) physically held right now? Asks the OS directly."""
    try:
        if IS_WINDOWS:
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x12) & 0x8000)  # VK_MENU
        if IS_MAC:
            flags = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateCombinedSessionState)
            return bool(flags & Quartz.kCGEventFlagMaskAlternate)
    except Exception:  # pragma: no cover - defensive
        log.debug("Could not query modifier state", exc_info=True)
    return True  # unknown platform: assume held (hotkey is only evaluated on key press)


class ClipboardCapture:
    """Reads the highlighted text via a simulated copy, without losing the user's clipboard."""

    def __init__(self) -> None:
        self._controller = keyboard.Controller()
        self._lock = threading.Lock()
        self._copy_modifier = keyboard.Key.cmd if IS_MAC else keyboard.Key.ctrl

    def _wait_for_alt_release(self) -> None:
        """
        The user is still holding Alt/Option from the hotkey. Sending Ctrl+C
        now would arrive as Ctrl+Alt+C, so wait (briefly) for them to let go.
        """
        deadline = time.monotonic() + MODIFIER_RELEASE_TIMEOUT_S
        while alt_is_down() and time.monotonic() < deadline:
            time.sleep(0.01)
        if alt_is_down():  # still held: best-effort synthetic release
            for key in (keyboard.Key.alt_l, keyboard.Key.alt_r):
                try:
                    self._controller.release(key)
                except Exception:
                    pass

    def _send_copy(self) -> None:
        with self._controller.pressed(self._copy_modifier):
            self._controller.press("c")
            self._controller.release("c")

    def get_selection(self) -> str:
        """Return the highlighted text ('' if nothing was selected). Restores the clipboard."""
        with self._lock:
            try:
                original: Optional[str] = pyperclip.paste()
            except Exception:
                original = None  # clipboard held something we cannot read as text

            # A unique marker lets us tell "copy produced nothing" from "copy worked".
            sentinel = f"__tts_reader_{uuid.uuid4().hex}__"
            text = ""
            try:
                pyperclip.copy(sentinel)
                self._wait_for_alt_release()
                self._send_copy()
                time.sleep(COPY_DELAY_S)
                text = pyperclip.paste()
                for _ in range(EXTRA_COPY_POLLS):
                    if text and text != sentinel:
                        break
                    time.sleep(0.05)
                    text = pyperclip.paste()
            except Exception:
                log.exception("Clipboard capture failed")
            finally:
                try:
                    pyperclip.copy(original if original is not None else "")
                except Exception:
                    log.exception("Could not restore the clipboard")
            return "" if text == sentinel else (text or "")


# --------------------------------------------------------------------------- #
# Global hotkeys
# --------------------------------------------------------------------------- #

class HotkeyManager:
    """
    Alt+A / Alt+S listener (Option on macOS) running in a pynput daemon thread.

    Why not pynput's GlobalHotKeys? On macOS, Option+A produces the character
    "a-with-ring", so character matching fails. We match physical key codes and
    ask the OS whether Alt is held instead.

    macOS extra: the Option+A/S key events are swallowed (via pynput's
    `darwin_intercept`) so the focused app does not type "a-ring"/"sharp-s"
    over your selection.
    """

    def __init__(self, on_speak: Callable[[], None], on_stop: Callable[[], None]) -> None:
        self._callbacks = {"speak": on_speak, "stop": on_stop}
        self._held: Dict[str, float] = {}
        self._win_swallowed: set = set()
        self._listener: Optional[keyboard.Listener] = None

    def start(self) -> None:
        kwargs = {}
        if IS_MAC:
            kwargs["darwin_intercept"] = self._darwin_intercept
        elif IS_WINDOWS:
            kwargs["win32_event_filter"] = self._win32_filter
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release, **kwargs)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()

    # -- shared ------------------------------------------------------------
    @staticmethod
    def _action_for(key) -> Optional[str]:
        vk = getattr(key, "vk", None)
        if vk is not None:
            return VK_ACTIONS.get(vk)
        char = getattr(key, "char", None)
        return CHAR_ACTIONS.get(char.lower()) if char else None

    def _dispatch(self, action: str) -> None:
        try:
            self._callbacks[action]()
        except Exception:
            log.exception("Hotkey action %r failed", action)

    # -- pynput callbacks (all platforms) ----------------------------------
    def _on_press(self, key) -> None:
        action = self._action_for(key)
        if action is None or not alt_is_down():
            return
        now = time.monotonic()
        last = self._held.get(action)
        self._held[action] = now
        if last is not None and now - last < 0.6:
            return  # key auto-repeat while held: ignore
        self._dispatch(action)

    def _on_release(self, key) -> None:
        action = self._action_for(key)
        if action is not None:
            self._held.pop(action, None)

    # -- Windows: swallow the Alt+A / Alt+S keystrokes ---------------------
    # Without this the focused app also receives Alt+A. Apps such as Slack react
    # by moving focus (e.g. into the message box), which drops the text selection
    # before the simulated Ctrl+C runs, so nothing gets copied.
    def _win32_filter(self, msg, data):
        swallow = False
        try:
            if not data.flags & 0x10:  # skip LLKHF_INJECTED: our own / other synthetic keys
                action = VK_ACTIONS.get(data.vkCode)
                if action is not None:
                    if msg in (0x100, 0x104):  # WM_KEYDOWN / WM_SYSKEYDOWN
                        if data.flags & 0x20:  # LLKHF_ALTDOWN: Alt is held
                            swallow = True
                            if action not in self._win_swallowed:
                                self._win_swallowed.add(action)
                                # Alt pressed+released with nothing in between would activate
                                # the app's menu bar; send a dummy key so Windows treats it
                                # as a chord.
                                threading.Thread(target=self._win_mask_alt, daemon=True).start()
                                self._dispatch(action)
                    elif msg in (0x101, 0x105):  # WM_KEYUP / WM_SYSKEYUP
                        if action in self._win_swallowed:
                            self._win_swallowed.discard(action)
                            swallow = True
        except Exception:
            log.debug("win32 filter failed", exc_info=True)
        # suppress_event() works by raising an exception that pynput handles, so it
        # must be called outside the try/except above.
        if swallow:
            self._listener.suppress_event()
        return True

    def _win_mask_alt(self) -> None:
        try:
            ctl = keyboard.Controller()
            ctl.press(keyboard.KeyCode.from_vk(0xFF))  # unassigned virtual key
            ctl.release(keyboard.KeyCode.from_vk(0xFF))
        except Exception:
            log.debug("could not mask Alt", exc_info=True)

    # -- macOS: swallow the Option+A / Option+S keystrokes -----------------
    def _darwin_intercept(self, event_type, event):
        try:
            if event_type not in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                return event
            flags = Quartz.CGEventGetFlags(event)
            if not flags & Quartz.kCGEventFlagMaskAlternate:
                return event
            if flags & (Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskControl):
                return event
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            action = VK_ACTIONS.get(keycode)
            if action is None:
                return event
            if event_type == Quartz.kCGEventKeyDown:
                repeat = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
                if not repeat:
                    self._dispatch(action)
            return None  # suppress: the focused app never sees it, and neither does on_press
        except Exception:
            log.debug("darwin intercept failed", exc_info=True)
            return event


# --------------------------------------------------------------------------- #
# Packaging / OS integration: logging, error dialogs, single instance, autostart
# --------------------------------------------------------------------------- #

def setup_logging() -> None:
    """
    Log to the console (when there is one) and to <data dir>/reader.log.

    A packaged windowed app has no console, so the log file is the only way to
    see what happened. Logs contain counts and errors, never the text you read.
    """
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    handlers: List[logging.Handler] = []
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(DATA_DIR / "reader.log", maxBytes=256_000, backupCount=1, encoding="utf-8"))
    except OSError:
        pass
    if sys.stderr is not None:  # None when running as a windowed (no-console) executable
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers or [logging.NullHandler()], force=True)

    def log_uncaught(exc_type, exc, tb) -> None:
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = log_uncaught
    threading.excepthook = lambda args: log_uncaught(args.exc_type, args.exc_value, args.exc_traceback)


def show_message(text: str, error: bool = False) -> None:
    """Tell the user something even when there is no console (Windows message box)."""
    log.error(text) if error else log.info(text)
    if IS_WINDOWS:
        try:
            flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(0, text, APP_NAME, flags)
            return
        except Exception:
            pass
    if sys.stderr is not None:
        print(text, file=sys.stderr)


_instance_mutex = None  # kept referenced for the life of the process


def acquire_single_instance() -> bool:
    """Windows: refuse to start a second copy (two copies would both speak on Alt+A)."""
    global _instance_mutex
    if not IS_WINDOWS:
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\TTSReader.SingleInstance")
        if not handle:
            return True  # could not create it: do not block startup
        _instance_mutex = handle
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        log.debug("Single-instance check failed", exc_info=True)
        return True


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "TTSReader"


def autostart_command() -> str:
    """The command Windows should run at sign-in: the packaged .exe, or pythonw + this script."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    if windowless.exists():
        interpreter = windowless
    return f'"{interpreter}" "{Path(__file__).resolve()}"'


def autostart_enabled() -> bool:
    """Is there a 'start at login' entry for the current user? (Windows only.)"""
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_VALUE)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    """Add or remove the per-user 'start at login' entry. No admin rights needed."""
    if not IS_WINDOWS:
        return
    import winreg

    try:
        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, _RUN_VALUE)
    except FileNotFoundError:
        pass  # nothing to remove
    except OSError as exc:
        log.warning("Could not update the start-at-login setting: %s", exc)


def refresh_autostart() -> None:
    """If start-at-login is on but points at an old location (app moved/updated), repoint it."""
    if not IS_WINDOWS or not autostart_enabled():
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            current, _ = winreg.QueryValueEx(key, _RUN_VALUE)
        if current != autostart_command():
            set_autostart(True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Tray icon
# --------------------------------------------------------------------------- #

def make_icon_image(size: int = 64) -> Image.Image:
    """Draw a speaker-with-sound-waves icon in memory (no external image files)."""
    scale = 4  # draw big, then downsample for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def u(v: float) -> int:  # 0..64 design grid -> pixels
        return int(v * s / 64)

    white = (255, 255, 255, 255)
    d.rounded_rectangle((u(2), u(2), u(62), u(62)), radius=u(14), fill=(37, 99, 235, 255))
    # speaker body + cone
    d.polygon(
        [(u(13), u(26)), (u(22), u(26)), (u(33), u(16)), (u(33), u(48)), (u(22), u(38)), (u(13), u(38))],
        fill=white,
    )
    # three sound waves (arcs on the right of the cone)
    for radius in (9, 15, 21):
        box = (u(33 - radius + 2), u(32 - radius), u(33 + radius + 2), u(32 + radius))
        d.arc(box, -48, 48, fill=white, width=max(2, u(3)))
    return img.resize((size, size), Image.Resampling.LANCZOS)


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

class ReaderApp:
    def __init__(self) -> None:
        self.settings = Settings(CONFIG_PATH, LEGACY_CONFIG_PATH)
        self.icon: Optional[pystray.Icon] = None
        self.engine = SpeechEngine(notify=self._notify)
        self.capture = ClipboardCapture()
        self.hotkeys = HotkeyManager(on_speak=self._on_speak_hotkey, on_stop=self.engine.stop)
        self._capture_busy = threading.Lock()
        self._quitting = threading.Event()

    # -- reading -----------------------------------------------------------
    def _on_speak_hotkey(self) -> None:
        # Never block the hotkey thread: simulating keystrokes from inside a
        # keyboard hook can deadlock, so do the work on a short-lived thread.
        # Cut off whatever is playing right away so a repeat press feels instant;
        # the newly highlighted text is captured and read right after.
        self.engine.stop()
        threading.Thread(target=self.read_selection, name="tts-capture", daemon=True).start()

    def read_selection(self) -> None:
        if not self._capture_busy.acquire(blocking=False):
            return  # a capture is already in progress (double press)
        try:
            raw = self.capture.get_selection()
            chunks = prepare_chunks(raw)
            if not chunks:
                log.info("No readable text was selected.")
                return
            log.info("Reading %d chunk(s).", len(chunks))
            # speak() interrupts whatever is playing, so a new Alt+A cleanly replaces old speech.
            self.engine.speak(chunks, self.settings.voice, self.settings.rate)
        except Exception:
            log.exception("read_selection failed")
        finally:
            self._capture_busy.release()

    # -- tray --------------------------------------------------------------
    def _notify(self, message: str) -> None:
        log.warning(message)
        if self.icon is not None:
            try:
                self.icon.notify(message, APP_NAME)
            except Exception:
                pass  # notifications are best-effort (unsupported on some desktops)

    def _title(self) -> str:
        hint = "Option" if IS_MAC else "Alt"
        return f"{APP_NAME} - {VOICES[self.settings.voice].split(' ')[0]}, {self.settings.speed:g}x ({hint}+A read, {hint}+S stop)"

    def _refresh_tray(self) -> None:
        if self.icon is not None:
            self.icon.title = self._title()
            self.icon.update_menu()

    def _voice_item(self, voice: str, label: str) -> pystray.MenuItem:
        def choose(icon, item) -> None:
            self.settings.set_voice(voice)
            self._refresh_tray()

        return pystray.MenuItem(label, choose, checked=lambda item: self.settings.voice == voice, radio=True)

    def _speed_item(self, speed: float) -> pystray.MenuItem:
        def choose(icon, item) -> None:
            self.settings.set_speed(speed)
            self._refresh_tray()

        return pystray.MenuItem(f"{speed:g}x", choose, checked=lambda item: self.settings.speed == speed, radio=True)

    def _build_menu(self) -> pystray.Menu:
        hint = "Option" if IS_MAC else "Alt"
        items = [
            pystray.MenuItem(f"{hint}+A: read selection   {hint}+S: stop", lambda icon, item: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Voice", pystray.Menu(*[self._voice_item(v, l) for v, l in VOICES.items()])),
            pystray.MenuItem("Speed", pystray.Menu(*[self._speed_item(s) for s in SPEEDS])),
        ]
        if IS_WINDOWS:
            items.append(pystray.MenuItem("Start at login", self._toggle_autostart, checked=lambda item: autostart_enabled()))
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Version {__version__}", lambda icon, item: None, enabled=False),
            pystray.MenuItem("Stop Speaking", lambda icon, item: self.engine.stop()),
            pystray.MenuItem("Quit", lambda icon, item: self.quit()),
        ]
        return pystray.Menu(*items)

    def _toggle_autostart(self, icon, item) -> None:
        set_autostart(not autostart_enabled())
        self._refresh_tray()

    def _on_tray_ready(self, icon: pystray.Icon) -> None:
        """Runs on a helper thread once the tray event loop is up."""
        icon.visible = True
        # Start the hotkey listener only now, after the main-thread GUI loop exists.
        self.hotkeys.start()
        log.info("%s is running. Select text and press %s+A.", APP_NAME, "Option" if IS_MAC else "Alt")

    # -- lifecycle ---------------------------------------------------------
    def quit(self) -> None:
        if self._quitting.is_set():
            return
        self._quitting.set()
        log.info("Shutting down.")
        self.hotkeys.stop()
        self.engine.shutdown()
        if self.icon is not None:
            self.icon.stop()

    def run(self) -> None:
        refresh_autostart()
        if IS_MAC:
            try:  # menu-bar-only app: no Dock icon
                from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

                NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            except Exception:
                pass

        self.icon = pystray.Icon("tts-reader", make_icon_image(), self._title(), self._build_menu())
        atexit.register(self.quit)
        signal.signal(signal.SIGINT, lambda *_: self.quit())
        # pystray MUST own the main thread (hard requirement on macOS).
        self.icon.run(setup=self._on_tray_ready)


def ensure_macos_accessibility() -> None:
    """On macOS, warn (and trigger the system prompt) if keyboard monitoring is not permitted."""
    if not IS_MAC:
        return
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

        if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}):
            print(
                "\n[!] macOS has not granted Accessibility access to this app yet.\n"
                "    Open System Settings > Privacy & Security > Accessibility (and Input Monitoring),\n"
                "    enable your terminal (Terminal / iTerm / VS Code ...), then restart this script.\n"
                "    The hotkeys will not work until you do.\n",
                file=sys.stderr,
            )
    except Exception:
        pass  # the check is a convenience; pynput prints its own warning if needed


def main() -> int:
    setup_logging()
    if not acquire_single_instance():
        show_message(f"{APP_NAME} is already running. Look for its icon in the system tray.")
        return 0
    log.info("%s %s starting.", APP_NAME, __version__)
    ensure_macos_accessibility()
    try:
        app = ReaderApp()
    except pygame.error as exc:
        show_message(f"Could not initialise audio output: {exc}", error=True)
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
