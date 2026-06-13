#!/usr/bin/env python3
"""
Subtitle-style Transcript Processor
Parses an Adobe Premiere transcript export (.txt) where timecode and text
are separated by a blank line (subtitle/SRT-like format, no speaker names).

Expected format:
    HH:MM:SS:FF - HH:MM:SS:FF

    Text of the utterance,
    possibly on multiple lines.

    HH:MM:SS:FF - HH:MM:SS:FF

    More text.

Usage:
    python sub_transcript_processor.py transcript.txt
    python sub_transcript_processor.py transcript.txt --languages cs sv --fps 25
    python sub_transcript_processor.py transcript.txt --dry-run --trim-start
"""

import argparse
import io
import re
import sys
from pathlib import Path

from pydub import AudioSegment
from pydub.effects import speedup
from dotenv import load_dotenv

from google.cloud import texttospeech
from google.cloud import translate_v2 as translate

load_dotenv()

# ── Language configuration ────────────────────────────────────────────────────

LANGUAGES = {
    'cs': {
        'name': 'Czech',
        'translate_code': 'cs',
        'tts_language': 'cs-CZ',
        'voice': 'cs-CZ-Wavenet-A',
    },
    'sv': {
        'name': 'Swedish',
        'translate_code': 'sv',
        'tts_language': 'sv-SE',
        'voice': 'sv-SE-Wavenet-A',
    },
    'bg': {
        'name': 'Bulgarian',
        'translate_code': 'bg',
        'tts_language': 'bg-BG',
        'voice': 'bg-BG-Standard-A',
    },
}

# ── TXT parsing ───────────────────────────────────────────────────────────────

TIMECODE_RE = re.compile(
    r'(\d{2}:\d{2}:\d{2}:\d{2})\s*-\s*(\d{2}:\d{2}:\d{2}:\d{2})'
)


def parse_timecode(tc: str, fps: int) -> float:
    """Convert HH:MM:SS:FF timecode to seconds."""
    h, m, s, f = map(int, tc.split(':'))
    return h * 3600 + m * 60 + s + f / fps


def load_transcript(txt_path: Path, fps: int = 25) -> list[dict]:
    """
    Parse the subtitle-style TXT transcript into a list of segments.
    Timecode and text are in separate blank-line-separated blocks.
    No speaker names in this format — single voice throughout.
    Each segment: {start, end, duration, speaker, text}
    """
    content = txt_path.read_text(encoding='utf-8')
    blocks = re.split(r'\n\s*\n', content.strip())

    segments = []
    i = 0
    while i < len(blocks):
        lines = [l.strip() for l in blocks[i].strip().splitlines() if l.strip()]
        if not lines:
            i += 1
            continue

        tc_match = TIMECODE_RE.match(lines[0])
        if not tc_match:
            i += 1
            continue

        start = parse_timecode(tc_match.group(1), fps)
        end   = parse_timecode(tc_match.group(2), fps)
        if end <= start:
            i += 1
            continue

        # Text is in the next block
        text = ''
        if i + 1 < len(blocks):
            next_lines = [l.strip() for l in blocks[i + 1].strip().splitlines() if l.strip()]
            if next_lines and not TIMECODE_RE.match(next_lines[0]):
                text = ' '.join(next_lines).strip()
                i += 2
            else:
                i += 1
        else:
            i += 1

        if not text:
            continue

        segments.append({
            'start':    start,
            'end':      end,
            'duration': end - start,
            'speaker':  'Narrator',
            'text':     text,
        })

    segments.sort(key=lambda s: s['start'])
    return segments


SENTENCE_END_RE = re.compile(r'[.?!]["\']?\s*$')

def merge_sentences(segments: list[dict]) -> list[dict]:
    """
    Merge consecutive segments that don't end with sentence-ending punctuation
    (.  ?  !) into a single segment, combining their text and total duration.
    """
    merged = []
    buffer: dict | None = None

    for seg in segments:
        if buffer is None:
            buffer = dict(seg)
        else:
            buffer['text']     += ' ' + seg['text']
            buffer['end']       = seg['end']
            buffer['duration']  = buffer['end'] - buffer['start']

        if SENTENCE_END_RE.search(buffer['text']):
            merged.append(buffer)
            buffer = None

    if buffer is not None:  # flush any trailing incomplete segment
        merged.append(buffer)

    return merged

# ── Audio helpers ─────────────────────────────────────────────────────────────

def translate_text(text: str, target_lang: str, translate_client) -> str:
    result = translate_client.translate(text, target_language=target_lang)
    return result['translatedText']


def synthesize_clip(text: str, lang_cfg: dict, tts_client) -> AudioSegment:
    response = tts_client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(
            language_code=lang_cfg['tts_language'],
            name=lang_cfg['voice'],
        ),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=1.0,
        ),
    )
    return AudioSegment.from_mp3(io.BytesIO(response.audio_content))


def fit_to_slot(audio: AudioSegment, slot_ms: int) -> AudioSegment:
    """Speed up if audio exceeds slot; pad with silence if shorter.
    Always trims to exact slot_ms to prevent drift accumulation."""
    if len(audio) > slot_ms:
        rate = len(audio) / slot_ms
        audio = speedup(audio, playback_speed=rate, chunk_size=150, crossfade=25)
        audio = audio[:slot_ms]  # enforce exact length — speedup chunking is imprecise
    gap = slot_ms - len(audio)
    if gap > 0:
        audio = audio + AudioSegment.silent(duration=gap)
    return audio

# ── Timeline assembly ─────────────────────────────────────────────────────────

def build_language_audio(
    segments: list[dict],
    lang_cfg: dict,
    translate_client,
    tts_client,
    trim_start: bool = False,
) -> AudioSegment:
    timeline        = AudioSegment.empty()
    origin          = segments[0]['start'] if trim_start else 0.0
    total           = len(segments)
    prev_source_end = origin  # tracks end of previous segment in source time

    for i, seg in enumerate(segments, 1):
        slot_ms = int(seg['duration'] * 1000)

        # Gap between end of previous segment and start of this one (source time)
        gap_ms = int((seg['start'] - prev_source_end) * 1000)
        if gap_ms > 0:
            timeline += AudioSegment.silent(duration=gap_ms)

        print(f"  [{i:>4}/{total}]  {seg['start']:.1f}s  {seg['speaker']:<12}  {seg['text'][:50]}", flush=True)

        try:
            translated = translate_text(seg['text'], lang_cfg['translate_code'], translate_client)
            clip       = synthesize_clip(translated, lang_cfg, tts_client)
            timeline  += fit_to_slot(clip, slot_ms)
        except Exception as exc:
            print(f"    ⚠ Skipped: {exc}")
            timeline += AudioSegment.silent(duration=slot_ms)

        prev_source_end = seg['end']  # use source end time, not start + duration

    return timeline

# ── Dry run ───────────────────────────────────────────────────────────────────

def dry_run(segments: list[dict], trim_start: bool, fps: int) -> None:
    origin = segments[0]['start'] if trim_start else 0.0
    print(f"\n── DRY RUN {'(trimmed) ' if trim_start else ''}fps={fps} {'─' * 40}")
    for seg in segments:
        pos = seg['start'] - origin
        m, s = divmod(int(pos), 60)
        preview = seg['text'][:65]
        ellipsis = '…' if len(seg['text']) > 65 else ''
        print(f"  {m:02d}:{s:02d}  [{seg['duration']:.1f}s]  {seg['speaker']:<12}  {preview}{ellipsis}")

    total_s = (segments[-1]['end']) - (segments[0]['start'] if trim_start else 0.0)
    print(f"\n── Summary {'─' * 50}")
    print(f"  Segments  : {len(segments)}")
    print(f"  Timeline  : {total_s/60:.1f} minutes")
    print()

# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Translate and synthesize a subtitle-style Premiere TXT transcript.'
    )
    parser.add_argument('txt', help='Path to the subtitle-style transcript .txt file')
    parser.add_argument('--output', default='./output', help='Output directory (default: ./output)')
    parser.add_argument(
        '--languages', nargs='+', default=['cs', 'sv'],
        choices=list(LANGUAGES.keys()),
        help='Target languages (default: cs sv)',
    )
    parser.add_argument(
        '--fps', type=int, default=25,
        help='Video frame rate for timecode conversion (default: 25)',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Preview segments without calling any APIs',
    )
    parser.add_argument(
        '--trim-start', action='store_true',
        help='Start output at first segment (skip leading silence)',
    )
    parser.add_argument(
        '--merge-sentences', action='store_true',
        help='Merge segments that lack sentence-ending punctuation into the next one',
    )
    args = parser.parse_args()

    txt_path = Path(args.txt)
    if not txt_path.exists():
        print(f"Error: file not found: {txt_path}")
        sys.exit(1)

    print(f"\nLoading: {txt_path.name}  (fps={args.fps})")
    segments = load_transcript(txt_path, fps=args.fps)
    print(f"Segments parsed: {len(segments)}")

    if args.merge_sentences:
        segments = merge_sentences(segments)
        print(f"After merging incomplete sentences: {len(segments)}")

    if not segments:
        print("No segments found. Check file format and encoding.")
        sys.exit(0)

    if args.dry_run:
        dry_run(segments, args.trim_start, args.fps)
        return

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    translate_client = translate.Client()
    tts_client       = texttospeech.TextToSpeechClient()

    for lang_key in args.languages:
        lang_cfg = LANGUAGES[lang_key]
        print(f"\n── {lang_cfg['name']} {'─' * 50}")
        audio = build_language_audio(
            segments, lang_cfg, translate_client, tts_client,
            trim_start=args.trim_start,
        )
        out_path = output_dir / f"{txt_path.stem}_{lang_key}.wav"
        audio.export(str(out_path), format='wav')
        print(f"   → {out_path}  ({len(audio)/1000:.1f}s)")

    print("\nDone.")


if __name__ == '__main__':
    main()
