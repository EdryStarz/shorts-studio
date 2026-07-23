import re
from pathlib import Path


def _timestamp(seconds: float, separator: str = ",") -> str:
    millis = max(0, round(seconds * 1000))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{ms:03}"


def _chunks(
    words: list[dict], max_words: int = 3, max_chars: int = 28,
    max_duration: float = 2.2, max_gap: float = 0.55,
) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        if current:
            candidate_chars = len(" ".join(str(item["word"]) for item in [*current, word]))
            duration = float(word["end"]) - float(current[0]["start"])
            gap = float(word["start"]) - float(current[-1]["end"])
            if (
                len(current) >= max_words or candidate_chars > max_chars
                or duration > max_duration or gap > max_gap
            ):
                chunks.append(current)
                current = []
        current.append(word)
        if str(word["word"]).rstrip().endswith((".", "!", "?")) and len(current) >= 2:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def write_srt(words: list[dict], path: Path, clip_start: float = 0.0) -> Path:
    entries = []
    for index, chunk in enumerate(_chunks(words), 1):
        start = max(0, float(chunk[0]["start"]) - clip_start)
        end = max(start + 0.1, float(chunk[-1]["end"]) - clip_start)
        text = " ".join(str(word["word"]) for word in chunk)
        entries.append(f"{index}\n{_timestamp(start)} --> {_timestamp(end)}\n{text}\n")
    path.write_text("\n".join(entries), encoding="utf-8")
    return path


def _ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, remainder = divmod(centis, 360_000)
    minutes, remainder = divmod(remainder, 6000)
    secs, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{cs:02}"


def _ass_escape(text: str) -> str:
    return re.sub(r"([{}])", r"\\\1", text.replace("\n", r"\N"))


def _balanced_line_break(chunk: list[dict], max_line_chars: int = 22) -> int | None:
    """Return a word index for a balanced two-line caption when it is needed."""
    values = [str(item["word"]) for item in chunk]
    if len(" ".join(values)) <= max_line_chars or len(values) < 2:
        return None
    candidates = []
    for index in range(1, len(values)):
        left = len(" ".join(values[:index]))
        right = len(" ".join(values[index:]))
        candidates.append((max(left, right), abs(left - right), index))
    return min(candidates)[2]


def _styled_chunk(chunk: list[dict], active_index: int | None = None) -> str:
    line_break = _balanced_line_break(chunk)
    styled: list[str] = []
    for index, item in enumerate(chunk):
        if line_break == index:
            styled.append(r"\N")
        value = _ass_escape(str(item["word"]))
        colour = r"{\c&H00D7FF&}" if index == active_index else r"{\c&HFFFFFF&}"
        styled.append(colour + value)
    return " ".join(styled).replace(r"\N ", r"\N")


def write_ass(words: list[dict], path: Path, clip_start: float = 0.0, dynamic: bool = True) -> Path:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Arial,54,&H00FFFFFF,&H0000D7FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,110,110,90,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines: list[str] = []
    for chunk in _chunks(words):
        if dynamic:
            for active_index, word in enumerate(chunk):
                start = max(0, float(word["start"]) - clip_start)
                end = max(start + 0.08, float(word["end"]) - clip_start)
                text = _styled_chunk(chunk, active_index)
                lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}")
        else:
            start = max(0, float(chunk[0]["start"]) - clip_start)
            end = max(start + 0.1, float(chunk[-1]["end"]) - clip_start)
            text = _styled_chunk(chunk)
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8-sig")
    return path


def write_vtt(words: list[dict], path: Path, clip_start: float = 0.0) -> Path:
    content = ["WEBVTT\n"]
    for chunk in _chunks(words):
        start = max(0, float(chunk[0]["start"]) - clip_start)
        end = max(start + 0.1, float(chunk[-1]["end"]) - clip_start)
        content.append(f"{_timestamp(start, '.')} --> {_timestamp(end, '.')}\n")
        content.append(" ".join(str(word["word"]) for word in chunk) + "\n\n")
    path.write_text("".join(content), encoding="utf-8")
    return path
