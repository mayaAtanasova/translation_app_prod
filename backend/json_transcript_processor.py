#!/usr/bin/env python3
"""
JSON Transcript Processor
Parses an Adobe Premiere transcript export (.json), translates content,
synthesizes TTS, and assembles a time-aligned WAV file per language.

The JSON must contain:
  - "segments": [{start, duration, speaker, words: [{text, type, ...}]}]
  - "speakers": [{id, name}]

Usage:
    python json_transcript_processor.py transcript.json
    python json_transcript_processor.py transcript.json --languages cs sv --trim-start
"""

import argparse
import io
import json
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

# ── JSON parsing ──────────────────────────────────────────────────────────────

def get_segment_text(segment: dict) -> str:
    """Join all word-type entries in a segment into a single string."""
    words = [
        w['text'] for w in segment.get('words', [])
        if w.get('type') == 'word' and w.get('text', '').strip()
    ]
    return ' '.join(words).strip()


def load_transcript(json_path: Path) -> tuple[list[dict], dict]:
    """
    Load and validate the transcript JSON.
    Returns (segments, speaker_map) where speaker_map is {uuid: name}.
    Segments are sorted by start time and filtered to those with text.
    """
    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)

    # Build speaker UUID → name lookup
    speaker_map = {s['id']: s['name'] for s in data.get('speakers', [])}

    # Extract and clean segments
    segments = []
    for seg in data.get('segments', []):
        text = get_segment_text(seg)
        if not text:
            continue
        segments.append({
            'start':    float(seg['start']),
            'duration': float(seg['duration']),
            'speaker':  speaker_map.get(seg.get('speaker', ''), 'Unknown'),
            'text':     text,
        })

    segments.sort(key=lambda s: s['start'])
    return segments, speaker_map

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
    """Speed up only if audio exceeds slot. Pad with silence if shorter."""
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
    timeline = AudioSegment.empty()
    origin = segments[0]['start'] if trim_start else 0.0

    cursor_s = 0.0  # current position in output timeline (seconds from origin)
    total = len(segments)

    for i, seg in enumerate(segments, 1):
        seg_start_s = seg['start'] - origin
        slot_ms = int(seg['duration'] * 1000)

        # Insert silence gap between previous segment end and this segment start
        gap_ms = int((seg_start_s - cursor_s) * 1000)
        if gap_ms > 0:
            timeline += AudioSegment.silent(duration=gap_ms)

        print(f"  [{i:>3}/{total}]  {seg['start']:.1f}s  {seg['text'][:55]}", flush=True)

        try:
            translated = translate_text(seg['text'], lang_cfg['translate_code'], translate_client)
            clip = synthesize_clip(translated, lang_cfg, tts_client)
            timeline += fit_to_slot(clip, slot_ms)
        except Exception as exc:
            print(f"    ⚠ Skipped: {exc}")
            timeline += AudioSegment.silent(duration=slot_ms)

        cursor_s = seg_start_s + seg['duration']

    return timeline

# ── Dry run ───────────────────────────────────────────────────────────────────

def dry_run(segments: list[dict], trim_start: bool) -> None:
    origin = segments[0]['start'] if trim_start else 0.0
    print(f"\n── DRY RUN {'(trimmed start) ' if trim_start else ''}{'─' * 45}")
    for seg in segments:
        pos = seg['start'] - origin
        m, s = divmod(int(pos), 60)
        preview = seg['text'][:70]
        ellipsis = '…' if len(seg['text']) > 70 else ''
        print(f"  {m:02d}:{s:02d}  [{seg['duration']:.1f}s]  {seg['speaker']:<12}  {preview}{ellipsis}")

    total_s = (segments[-1]['start'] + segments[-1]['duration']) - origin
    print(f"\n── Summary {'─' * 50}")
    print(f"  Segments  : {len(segments)}")
    print(f"  Timeline  : {total_s/60:.1f} minutes")
    print()

# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Translate and synthesize an Adobe Premiere JSON transcript.'
    )
    parser.add_argument('json', help='Path to the transcript .json file')
    parser.add_argument('--output', default='./output', help='Output directory (default: ./output)')
    parser.add_argument(
        '--languages', nargs='+', default=['cs', 'sv'],
        choices=list(LANGUAGES.keys()),
        help='Target languages (default: cs sv)',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Preview segments without calling any APIs',
    )
    parser.add_argument(
        '--trim-start', action='store_true',
        help='Start output audio at first segment (skip leading silence)',
    )
    args = parser.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"Error: file not found: {json_path}")
        sys.exit(1)

    print(f"\nLoading: {json_path.name}")
    segments, _ = load_transcript(json_path)
    print(f"Segments with text: {len(segments)}")

    if not segments:
        print("No translatable segments found.")
        sys.exit(0)

    if args.dry_run:
        dry_run(segments, args.trim_start)
        return

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    translate_client = translate.Client()
    tts_client = texttospeech.TextToSpeechClient()

    for lang_key in args.languages:
        lang_cfg = LANGUAGES[lang_key]
        print(f"\n── {lang_cfg['name']} {'─' * 50}")
        audio = build_language_audio(
            segments, lang_cfg, translate_client, tts_client,
            trim_start=args.trim_start,
        )
        out_path = output_dir / f"{json_path.stem}_{lang_key}.wav"
        audio.export(str(out_path), format='wav')
        print(f"   → {out_path}  ({len(audio)/1000:.1f}s)")

    print("\nDone.")


if __name__ == '__main__':
    main()
