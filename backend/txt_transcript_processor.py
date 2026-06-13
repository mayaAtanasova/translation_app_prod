#!/usr/bin/env python3
"""
TXT Transcript Processor
Parses an Adobe Premiere transcript export (.txt) with timecode blocks,
translates content, synthesizes TTS, and assembles a time-aligned WAV
file per language.

Expected format:
    HH:MM:SS:FF - HH:MM:SS:FF
    Speaker N
    Text of the utterance.

    HH:MM:SS:FF - HH:MM:SS:FF
    Speaker M
    More text.

Usage:
    python txt_transcript_processor.py transcript.txt
    python txt_transcript_processor.py transcript.txt --languages cs sv --fps 25
    python txt_transcript_processor.py transcript.txt --dry-run --trim-start
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
    Parse the TXT transcript into a list of segments.
    Each segment: {start, end, duration, speaker, text}
    """
    content = txt_path.read_text(encoding='utf-8')

    # Split into blocks separated by blank lines
    blocks = re.split(r'\n\s*\n', content.strip())

    segments = []
    for block in blocks:
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 3:
            continue

        tc_match = TIMECODE_RE.match(lines[0])
        if not tc_match:
            continue

        start = parse_timecode(tc_match.group(1), fps)
        end   = parse_timecode(tc_match.group(2), fps)
        if end <= start:
            continue

        speaker = lines[1]
        text    = ' '.join(lines[2:]).strip()
        if not text:
            continue

        segments.append({
            'start':    start,
            'end':      end,
            'duration': end - start,
            'speaker':  speaker,
            'text':     text,
        })

    segments.sort(key=lambda s: s['start'])
    return segments

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
    """Speed up if audio exceeds slot; pad with silence if shorter."""
    if len(audio) > slot_ms:
        rate = len(audio) / slot_ms
        audio = speedup(audio, playback_speed=rate, chunk_size=150, crossfade=25)
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
        description='Translate and synthesize an Adobe Premiere TXT transcript.'
    )
    parser.add_argument('txt', help='Path to the transcript .txt file')
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
    args = parser.parse_args()

    txt_path = Path(args.txt)
    if not txt_path.exists():
        print(f"Error: file not found: {txt_path}")
        sys.exit(1)

    print(f"\nLoading: {txt_path.name}  (fps={args.fps})")
    segments = load_transcript(txt_path, fps=args.fps)
    print(f"Segments parsed: {len(segments)}")

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
