from __future__ import annotations

import re
import unicodedata
import json
from dataclasses import dataclass
from typing import Optional


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
RGB_RE = re.compile(r"\x1b\[38;2;(\d+);(\d+);(\d+)m")
PASTE_BLOCK_RE = re.compile(r"^\[Pasted text #\d+(?: \+\d+ lines)?\]$")
IMAGE_PLACEHOLDER_RE = re.compile(r"\[Image #\d+\]\s*")
PROMPT_MARKERS = ("❯", "›")
PLACEHOLDER_TEXT = (
    "Type a message",
    "Press up to edit queued messages",
    "Write tests for @filename",
    "Explain this codebase",
)
DISMISSIBLE_OVERLAY_TEXT = "Esc to cancel"


@dataclass(frozen=True)
class InputState:
    kind: str
    text_after_prompt: str
    contains_expected_message: bool = False


def inspect_input(
    ansi_text: str,
    expected_message: str,
    allow_paste_block_residue: bool = False,
    allow_transformed_residue: bool = False,
) -> InputState:
    opencode = _inspect_opencode(ansi_text, expected_message)
    if opencode is not None:
        return opencode
    segment = _segment_after_last_prompt(ansi_text)
    if segment is None:
        if DISMISSIBLE_OVERLAY_TEXT in _strip_ansi(ansi_text):
            return InputState(kind="dismissible_overlay", text_after_prompt=DISMISSIBLE_OVERLAY_TEXT)
        return InputState(kind="unknown", text_after_prompt="")

    input_line = _first_line(segment)
    visible = _strip_ansi(input_line).strip()
    if (
        expected_message and _last_codex_prompt_is_dim(ansi_text)
        and (visible == expected_message or _is_expected_soft_wrapped_residue(segment, expected_message))
    ):
        # Codex dims submitted transcript prompts while its active composer may
        # be hidden during generation. This is evidence of submission, not residue.
        kind = "submitted" if allow_transformed_residue else "unknown"
        return InputState(kind=kind, text_after_prompt=visible, contains_expected_message=True)
    if expected_message and visible == expected_message:
        return InputState(
            kind="residue",
            text_after_prompt=visible,
            contains_expected_message=True,
        )
    if allow_paste_block_residue and _is_expected_multiline_paste_block(visible, expected_message):
        return InputState(
            kind="residue",
            text_after_prompt=visible,
            contains_expected_message=True,
        )
    if allow_transformed_residue and _is_expected_paste_block_residue(visible, expected_message):
        return InputState(
            kind="residue",
            text_after_prompt=visible,
            contains_expected_message=True,
        )
    if allow_transformed_residue and _is_expected_soft_wrapped_residue(segment, expected_message):
        return InputState(
            kind="residue",
            text_after_prompt=visible,
            contains_expected_message=True,
        )
    if allow_transformed_residue and _is_expected_image_placeholder_residue(visible, expected_message):
        return InputState(
            kind="residue",
            text_after_prompt=visible,
            contains_expected_message=True,
        )
    if not visible:
        return InputState(kind="empty", text_after_prompt="")
    if _looks_like_placeholder(input_line, visible):
        return InputState(kind="empty", text_after_prompt=visible)
    return InputState(kind="occupied", text_after_prompt=visible)


def _cell_slice(text, start, end):
    result, column = [], 0
    for char in text:
        width = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1)
        if start <= column < end:
            result.append(char)
        column += width
        if column >= end:
            break
    return ''.join(result)


def _inspect_opencode(ansi_text, expected):
    # OpenCode has a boxed composer, rather than a Codex/Claude prompt marker.
    # The bottom border fixes its width; right-hand sidebar text is not input.
    lines = [line for line in ANSI_RE.sub('', ansi_text).splitlines() if line.strip()]
    borders = [(i, re.match(r'^(\s*)╹(▀{8,})', line)) for i, line in enumerate(lines)]
    borders = [(i, match) for i, match in borders if match]
    if not borders:
        return None
    index, border = borders[-1]
    if index == 0 or not re.match(r'^\s*┃\s+(Build|Plan)\s*·', lines[index - 1]):
        return None
    if not any('ctrl+p' in line and 'commands' in line for line in lines[index + 1:]):
        return None
    if index + 2 != len(lines):
        return InputState(kind='unknown', text_after_prompt='')
    left = len(border.group(1))
    end = left + 1 + len(border.group(2))
    content = []
    for line in reversed(lines[:index - 1]):
        cells = _cell_slice(line, left, end)
        if cells.startswith('┃'):
            content.append(cells[1:].strip())
        elif not cells.strip():
            continue
        else:
            break
    if not content:
        return InputState(kind='unknown', text_after_prompt='')
    visible = '\n'.join(reversed(content)).strip()
    if not visible:
        return InputState(kind='empty', text_after_prompt='')
    normalized = ''.join(visible.split())
    expected_normalized = ''.join(expected.split())
    if expected and (normalized == expected_normalized or (
        len(normalized) >= 128 and expected_normalized.endswith(normalized)
    )):
        return InputState(kind='residue', text_after_prompt=visible, contains_expected_message=True)
    return InputState(kind='occupied', text_after_prompt=visible)


def strip_opencode_sidebar(text):
    """Keep the transcript columns, excluding OpenCode's independently drawn sidebar."""
    if _inspect_opencode(text, '') is None:
        return text
    lines = ANSI_RE.sub('', text).splitlines()
    borders = [re.match(r'^(\s*)╹(▀{8,})', line) for line in lines]
    border = [match for match in borders if match][-1]
    end = len(border.group(1)) + 1 + len(border.group(2))
    output = []
    for line in lines:
        # recent_unwrapped may join a protocol response beyond the current
        # viewport width. A complete JSON object is not sidebar decoration.
        if line.lstrip().startswith('{'):
            try:
                if isinstance(json.loads(line), dict):
                    output.append(line.rstrip())
                    continue
            except ValueError:
                pass
        output.append(_cell_slice(line, 0, end).rstrip())
    return '\n'.join(output)


def _last_codex_prompt_is_dim(text: str) -> bool:
    marker = text.rfind("›")
    if marker < 0 or text.rfind("❯") > marker:
        return False
    prefix = text[text.rfind('\n', 0, marker) + 1:marker]
    dim = False
    for match in SGR_RE.finditer(prefix):
        parts = [int(part or '0') for part in match.group(1).split(';')]
        index = 0
        while index < len(parts):
            code = parts[index]
            if code in (38, 48) and index + 1 < len(parts):
                # Color mode 2 and RGB channels are not SGR dim attributes.
                index += 5 if parts[index + 1] == 2 else 3
                continue
            if code in (0, 22):
                dim = False
            elif code == 2:
                dim = True
            index += 1
    return dim


def _segment_after_last_prompt(text: str) -> Optional[str]:
    marker_index = -1
    marker_len = 0
    for marker in PROMPT_MARKERS:
        index = text.rfind(marker)
        if index > marker_index:
            marker_index = index
            marker_len = len(marker)

    if marker_index < 0:
        return None
    return text[marker_index + marker_len :]


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "").replace("\n", " ")


def _normalize_visible(text: str) -> str:
    return re.sub(r"\s+", " ", _strip_ansi(text)).strip()


def _compact_visible(text: str) -> str:
    return re.sub(r"\s+", "", _strip_ansi(text))


def _first_line(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)[0]


def _looks_like_placeholder(ansi_segment: str, visible: str) -> bool:
    if visible in PLACEHOLDER_TEXT:
        return True
    if _has_dim_sgr(ansi_segment):
        return True
    rgb = RGB_RE.search(ansi_segment)
    if not rgb:
        return False
    red, green, blue = (int(part) for part in rgb.groups())
    return max(red, green, blue) <= 170


def _is_expected_multiline_paste_block(visible: str, expected_message: str) -> bool:
    return "\n" in expected_message and PASTE_BLOCK_RE.match(visible) is not None


def _is_expected_paste_block_residue(visible: str, expected_message: str) -> bool:
    expected = _normalize_visible(expected_message)
    return bool(
        expected
        and PASTE_BLOCK_RE.match(visible)
        and ("\n" in expected_message or len(expected) >= 40)
    )


def _is_expected_soft_wrapped_residue(segment: str, expected_message: str) -> bool:
    expected = _normalize_visible(expected_message)
    first_line = _normalize_visible(_first_line(segment))
    rendered = _normalize_visible(segment)
    rendered_matches_expected = rendered.startswith(expected) or _compact_visible(segment).startswith(
        _compact_visible(expected_message)
    )
    return bool(
        expected
        and len(expected) >= 40
        and first_line
        and expected.startswith(first_line)
        and rendered_matches_expected
    )


def _is_expected_image_placeholder_residue(visible: str, expected_message: str) -> bool:
    """Recognize terminal media rendering that replaces leading text with image chips."""
    if not IMAGE_PLACEHOLDER_RE.search(visible):
        return False

    rendered_tail = _normalize_visible(IMAGE_PLACEHOLDER_RE.sub("", visible))
    expected = _normalize_visible(expected_message)
    return bool(len(rendered_tail) >= 24 and expected.endswith(rendered_tail))


def _has_dim_sgr(text: str) -> bool:
    for match in SGR_RE.finditer(text):
        params = match.group(1) or "0"
        parts = params.split(";")
        for index, part in enumerate(parts):
            if part == "2" and (index == 0 or parts[index - 1] not in ("38", "48")):
                return True
    return False
