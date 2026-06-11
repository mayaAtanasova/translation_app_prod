#!/usr/bin/env python3
"""
Transcript Processor
Parses a production rundown .docx file, translates content, synthesizes TTS,
and assembles a time-aligned WAV file per language.

Requires: sudo apt install ffmpeg (for pydub WAV export)

Usage:
    python transcript_processor.py rundown.docx
    python transcript_processor.py rundown.docx --output ./output --languages cs sv
"""

import argparse
import io
import re
import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
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
        'voice_female': 'cs-CZ-Wavenet-A',
        'voice_male': 'cs-CZ-Wavenet-A',   # no male Czech Wavenet voice
    },
    'sv': {
        'name': 'Swedish',
        'translate_code': 'sv',
        'tts_language': 'sv-SE',
        'voice_female': 'sv-SE-Wavenet-A',
        'voice_male': 'sv-SE-Wavenet-B',
    },
}

# Extend these lists as new speakers appear in future documents
MALE_NAMES = {'dag', 'orjan', 'ørjan', 'ivan', 'jonas', 'erik', 'lars', 'magnus', 'bjorn', 'bjørn'}
FEMALE_NAMES = {'hilde', 'maya', 'camilla', 'daniela', 'anna', 'maria', 'sara', 'lisa', 'ingrid'}

TRANSLATABLE_TYPES = {'video', 'vignette'}

# Detects "Name: text" or "Name : text" at paragraph start
NAME_COLON_RE = re.compile(
    r'^([A-ZÆØÅÄÖÜ][a-zA-ZÆØÅÄÖÜæøåäöü\-]{1,20}(?:\s[A-ZÆØÅÄÖÜ][a-zA-ZÆØÅÄÖÜæøåäöü\-]{1,20}){0,2})\s*:\s*(.+)$',
    re.DOTALL,
)

TBL_TAG = qn('w:tbl')
P_TAG = qn('w:p')

# ── Timestamp / duration helpers ──────────────────────────────────────────────

def parse_timestamp(ts_str: str) -> float:
    """Parse HH:MM:SS or HH:MM into seconds since midnight."""
    ts_str = ts_str.strip()
    parts = ts_str.split(':')
    if len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 2:
        h, m, s = int(parts[0]), int(parts[1]), 0
    else:
        raise ValueError(f"Cannot parse timestamp: {ts_str!r}")
    return h * 3600 + m * 60 + s


def parse_duration(dur_str: str) -> int:
    """Parse '1m', '2m', '30s' etc. into seconds."""
    dur_str = dur_str.strip().lower()
    if dur_str.endswith('m'):
        return int(dur_str[:-1]) * 60
    if dur_str.endswith('s'):
        return int(dur_str[:-1])
    return 0

# ── Paragraph analysis ────────────────────────────────────────────────────────

def paragraph_is_bold(para: DocxParagraph) -> bool:
    """True if every non-empty run in the paragraph is bold."""
    runs = [r for r in para.runs if r.text.strip()]
    return bool(runs) and all(r.bold for r in runs)


def is_stage_direction(text: str) -> bool:
    text = text.strip()
    return text.startswith('(') and text.endswith(')')


def get_voice_gender(name: str) -> str:
    name_lower = name.lower().strip()
    if name_lower in MALE_NAMES:
        return 'male'
    if name_lower in FEMALE_NAMES:
        return 'female'
    return 'female'   # default

# ── Rundown table parsing ─────────────────────────────────────────────────────

def parse_table_row(table: DocxTable) -> dict | None:
    """
    Parse a single rundown table row.
    Expected columns: number | type | timestamp | duration | title
    Returns None if the table doesn't look like a rundown row.
    """
    if not table.rows:
        return None
    cells = [c.text.strip() for c in table.rows[0].cells]
    if len(cells) < 5:
        return None
    # Validate: first cell should be a number
    if not cells[0].isdigit():
        return None
    # Validate: third cell should parse as a timestamp
    try:
        ts = parse_timestamp(cells[2])
    except (ValueError, IndexError):
        return None
    return {
        'number': int(cells[0]),
        'type': cells[1].lower().strip(),
        'timestamp_s': ts,
        'duration_s': parse_duration(cells[3]),
        'title': cells[4],
        'utterances': [],
        'slot_ms': 0,   # filled in after all rows are collected
    }

# ── Content paragraph parsing ─────────────────────────────────────────────────

def parse_content_paragraphs(paragraphs: list[DocxParagraph]) -> list[dict]:
    """
    Convert content paragraphs into a list of utterances.
    Each utterance: {'text': str, 'gender': str}
    """
    utterances = []
    current_gender = 'female'

    for para in paragraphs:
        text = para.text.strip()
        if not text:
            continue
        # Bold paragraph → skip entirely (clarification title or speaker name label)
        if paragraph_is_bold(para):
            # If it looks like a name, update the voice for following text
            words = text.split()
            if 1 <= len(words) <= 3 and all(w[0].isupper() for w in words if w):
                current_gender = get_voice_gender(words[0])
            continue
        # Stage direction → skip
        if is_stage_direction(text):
            continue
        # "Name: text" pattern → voice switch, translate only the text part
        match = NAME_COLON_RE.match(text)
        if match:
            name = match.group(1).strip()
            content = match.group(2).strip()
            current_gender = get_voice_gender(name)
            if content:
                utterances.append({'text': content, 'gender': current_gender})
            continue
        # Plain text → translate
        utterances.append({'text': text, 'gender': current_gender})

    return utterances

# ── Full document parse ───────────────────────────────────────────────────────

def parse_rundown(docx_path: Path) -> list[dict]:
    """
    Parse the rundown .docx into an ordered list of segments.
    Each segment has type, timestamp, slot_ms, utterances.
    """
    doc = Document(docx_path)
    segments: list[dict] = []
    current_seg: dict | None = None
    pending_paras: list[DocxParagraph] = []

    def flush():
        if current_seg is not None and pending_paras:
            current_seg['utterances'] = parse_content_paragraphs(pending_paras)

    for child in doc.element.body:
        if child.tag == TBL_TAG:
            flush()
            pending_paras = []
            table = DocxTable(child, doc)
            seg = parse_table_row(table)
            if seg:
                current_seg = seg
                segments.append(seg)
        elif child.tag == P_TAG:
            if current_seg is not None:
                para = DocxParagraph(child, doc)
                pending_paras.append(para)

    flush()

    # Calculate slot_ms for each segment from the delta to the next row's timestamp
    for i, seg in enumerate(segments):
        if i + 1 < len(segments):
            delta_s = segments[i + 1]['timestamp_s'] - seg['timestamp_s']
            seg['slot_ms'] = max(0, int(delta_s * 1000))
        else:
            seg['slot_ms'] = seg['duration_s'] * 1000

    return segments

# ── Translation & TTS ─────────────────────────────────────────────────────────

def translate_text(text: str, target_lang: str, translate_client) -> str:
    result = translate_client.translate(text, target_language=target_lang)
    return result['translatedText']


def synthesize_clip(text: str, lang_cfg: dict, gender: str, tts_client) -> AudioSegment:
    voice_name = lang_cfg['voice_male'] if gender == 'male' else lang_cfg['voice_female']
    response = tts_client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(
            language_code=lang_cfg['tts_language'],
            name=voice_name,
        ),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=1.0,
        ),
    )
    return AudioSegment.from_mp3(io.BytesIO(response.audio_content))


def fit_to_slot(audio: AudioSegment, slot_ms: int) -> AudioSegment:
    """Speed up audio only if it exceeds the slot. Pad with silence if shorter."""
    if len(audio) > slot_ms:
        rate = len(audio) / slot_ms
        audio = speedup(audio, playback_speed=rate, chunk_size=150, crossfade=25)
    silence_needed = slot_ms - len(audio)
    if silence_needed > 0:
        audio = audio + AudioSegment.silent(duration=silence_needed)
    return audio

# ── Audio assembly ────────────────────────────────────────────────────────────

def build_language_audio(
    segments: list[dict],
    lang_key: str,
    translate_client,
    tts_client,
) -> AudioSegment:
    lang_cfg = LANGUAGES[lang_key]
    timeline = AudioSegment.empty()

    for seg in segments:
        slot_ms = seg['slot_ms']
        if slot_ms == 0:
            continue

        seg_type = seg['type']
        utterances = seg.get('utterances', [])

        if seg_type not in TRANSLATABLE_TYPES or not utterances:
            # Stinger, bumper, or empty translatable row → silence
            timeline += AudioSegment.silent(duration=slot_ms)
            continue

        print(f"  [{seg['number']:>3}] {seg['title'][:55]}")

        seg_audio = AudioSegment.empty()
        for utt in utterances:
            try:
                translated = translate_text(
                    utt['text'], lang_cfg['translate_code'], translate_client
                )
                clip = synthesize_clip(translated, lang_cfg, utt['gender'], tts_client)
                seg_audio += clip
            except Exception as exc:
                print(f"         ⚠ Skipped utterance: {exc}")

        if len(seg_audio) == 0:
            timeline += AudioSegment.silent(duration=slot_ms)
        else:
            timeline += fit_to_slot(seg_audio, slot_ms)

    return timeline

# ── Dry run ───────────────────────────────────────────────────────────────────

def dry_run(segments: list[dict]) -> None:
    """Print a summary of what would be translated without calling any APIs."""
    print("\n── DRY RUN — no API calls made " + "─" * 35)
    translatable_count = 0
    utterance_count = 0

    for seg in segments:
        slot_s = seg['slot_ms'] / 1000
        seg_type = seg['type']
        utterances = seg.get('utterances', [])

        if seg_type not in TRANSLATABLE_TYPES:
            print(f"  [{seg['number']:>3}] SKIP  {seg_type:<10}  slot={slot_s:.0f}s")
            continue

        if not utterances:
            print(f"  [{seg['number']:>3}] EMPTY {seg_type:<10}  slot={slot_s:.0f}s  \"{seg['title'][:40]}\"")
            continue

        translatable_count += 1
        print(f"\n  [{seg['number']:>3}] {seg_type.upper():<10}  slot={slot_s:.0f}s  \"{seg['title'][:40]}\"")
        for utt in utterances:
            utterance_count += 1
            speaker = f"[{utt['gender']}]"
            preview = utt['text'][:80].replace('\n', ' ')
            ellipsis = '…' if len(utt['text']) > 80 else ''
            print(f"         {speaker}  {preview}{ellipsis}")

    print(f"\n── Summary " + "─" * 50)
    print(f"  Translatable segments : {translatable_count}")
    print(f"  Utterances to process : {utterance_count}")
    total_s = sum(s['slot_ms'] for s in segments) / 1000
    print(f"  Total timeline        : {total_s/60:.1f} minutes")
    print()

# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Translate and synthesize a rundown .docx transcript.'
    )
    parser.add_argument('docx', help='Path to the rundown .docx file')
    parser.add_argument('--output', default='./output', help='Output directory (default: ./output)')
    parser.add_argument(
        '--languages', nargs='+', default=['cs', 'sv'],
        choices=list(LANGUAGES.keys()),
        help='Target languages (default: cs sv)',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Parse and preview what would be translated — no API calls',
    )
    args = parser.parse_args()

    docx_path = Path(args.docx)
    if not docx_path.exists():
        print(f"Error: file not found: {docx_path}")
        sys.exit(1)

    print(f"\nParsing: {docx_path.name}")
    segments = parse_rundown(docx_path)

    if args.dry_run:
        dry_run(segments)
        return

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    translatable = [s for s in segments if s['type'] in TRANSLATABLE_TYPES and s['utterances']]
    total_s = sum(s['slot_ms'] for s in segments) / 1000
    print(f"Rows: {len(segments)} total, {len(translatable)} with translatable content")
    print(f"Total timeline: {total_s/60:.1f} minutes\n")

    translate_client = translate.Client()
    tts_client = texttospeech.TextToSpeechClient()

    for lang_key in args.languages:
        lang_name = LANGUAGES[lang_key]['name']
        print(f"── {lang_name} ({'─' * 50})")
        audio = build_language_audio(segments, lang_key, translate_client, tts_client)
        out_path = output_dir / f"{docx_path.stem}_{lang_key}.wav"
        audio.export(str(out_path), format='wav')
        print(f"   → {out_path}  ({len(audio)/1000:.1f}s)\n")

    print("Done.")


if __name__ == '__main__':
    main()
