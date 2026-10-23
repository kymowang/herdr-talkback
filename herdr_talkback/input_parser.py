from __future__ import annotations

import re
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
