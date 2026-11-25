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
CODEX_PASTE_BLOCK_RE = re.compile(r"\[Pasted Content [1-9]\d* chars\]")
# Multi-line drafts folded by CLIs that keep the draft in the composer until
# Enter (seen with the deepseek `cmd` agent: `[[lead2→lea… +2L]`). The block is
# neither empty nor submitted, so it must count as residue/occupancy.
FOLDED_PASTE_RE = re.compile(r"^\[(.*?)\u2026\s*\+\s*(\d+)\s*L\]$")
FOLDED_PASTE_MARKER_RE = re.compile(r"\u2026\s*\+\s*\d+\s*L\]")
FOLDED_PASTE_MIN_PREFIX = 8
# Only agents observed to fold drafts in the composer get the residue
# interpretation; for anything else a folded block is merely occupied.
FOLDING_AGENTS = ("cmd",)
IMAGE_PLACEHOLDER_RE = re.compile(r"\[Image #\d+\]\s*")
PROMPT_MARKERS = ("❯", "›")
# A `»` prompt sits at visible column zero, but the line may start with SGR
# sequences; consuming them keeps "indented chrome is not a prompt" intact.
CHEVRON_PROMPT_RE = re.compile(r"^(?:\x1b\[[0-9;?]*[A-Za-z])*\u00bb", re.MULTILINE)
# agy (Antigravity CLI) uses no ❯/› marker: its composer is a full-width
# horizontal rule, a `>` line holding the draft, and a matching rule, with the
# CLI's footer hint below. The bottom-most matching pair with that footer is the
# only such block on screen, so transcript `>` echoes and diffs are excluded.
AGY_RULE_RE = re.compile(r"^\s*\u2500{20,}\s*$")
AGY_PROMPT_RE = re.compile(r"^\s*>(?: (.*))?$")
# The footer hint identifies the live composer, but agy rewrites it as its state
# changes: `? for shortcuts` when idle, `esc to cancel` while working, and
# `Press up to edit queued messages` once a draft is waiting behind a running
# turn. Missing one of them makes the composer unrecognizable exactly when a
# message has to be queued, so every observed variant belongs here.
AGY_FOOTER_HINTS = (
    "? for shortcuts",
    "esc to cancel",
    "Press up to edit queued messages",
)
# A draft longer than the composer viewport scrolls: the `>` line goes out of
# sight and the CLI marks the hidden part with an up arrow on the first line.
AGY_SCROLL_MARKER_RE = re.compile(r"\u2191\s*$")
# agy paints its prompt hint inside the composer instead of leaving it blank.
# Those hints are not drafts: treating them as input blocks every delivery.
AGY_PLACEHOLDERS = (
    "Auto mode: edits & safe commands auto-approved (shift+tab to cycle)",
)
PLACEHOLDER_TEXT = (
    "Type a message",
    "Press up to edit queued messages",
    "Write tests for @filename",
    "Explain this codebase",
    "Ask Codex to do anything",
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
    agent: Optional[str] = None,
) -> InputState:
    opencode = _inspect_opencode(ansi_text, expected_message)
    if opencode is not None:
        return opencode
    agy = _inspect_agy(ansi_text, expected_message, allow_transformed_residue)
    if agy is not None:
        return agy
    segment = _segment_after_last_prompt(ansi_text)
    if segment is None:
        if DISMISSIBLE_OVERLAY_TEXT in _strip_ansi(ansi_text):
            return InputState(kind="dismissible_overlay", text_after_prompt=DISMISSIBLE_OVERLAY_TEXT)
        return InputState(kind="unknown", text_after_prompt="")

    input_line = _first_line(segment)
    visible = _strip_ansi(input_line).strip()
    if CODEX_PASTE_BLOCK_RE.search(_strip_ansi(segment)) and ansi_text.rfind('›') > ansi_text.rfind('❯'):
        # Dim Codex prompts are transcript history, not the active composer.
        if _last_codex_prompt_is_dim(ansi_text):
            return InputState(kind='unknown', text_after_prompt=visible)
        if allow_transformed_residue and expected_message:
            return InputState(kind='residue', text_after_prompt=visible, contains_expected_message=True)
        return InputState(kind='occupied', text_after_prompt=visible)
    if (
        expected_message and _last_codex_prompt_is_dim(ansi_text)
        and (visible == expected_message or _is_expected_soft_wrapped_residue(segment, expected_message))
    ):
        # Codex dims submitted transcript prompts while its active composer may
        # be hidden during generation. This is evidence of submission, not residue.
        kind = "submitted" if allow_transformed_residue else "unknown"
        return InputState(kind=kind, text_after_prompt=visible, contains_expected_message=True)
    if not visible:
        return InputState(kind="empty", text_after_prompt="")
    if _folded_paste(visible) is not None:
        # 折叠的多行草稿仍在 composer 里,既不是空输入也不算已提交:
        # 忙时 CLI 会把它留着,只有 Enter 才真正提交。误判为空会让发送前
        # 检查漏掉占用,也会让发送后校验假报 delivered。
        # 发送后(allow_transformed_residue)按渲染出的折叠标记判定即可,
        # 不必依赖 agent 名: Herdr 的 pane agent 元数据可能落后于真实
        # 前台 CLI(例如 pane 里已是 Command Code 但仍标着 codex), 按白名单
        # 判会把刚写入的消息误判成 occupied, 最终落成 delivery ambiguous。
        if ((allow_transformed_residue or _supports_folded_drafts(agent)) and expected_message
                and _is_expected_folded_residue(visible, expected_message)):
            return InputState(
                kind="residue",
                text_after_prompt=visible,
                contains_expected_message=True,
            )
        return InputState(kind="occupied", text_after_prompt=visible)
    # 占位/ghost 文本必须先于「等于期望消息 → residue」判定：
    # Claude Code 的空输入框会画 dim 的 ghost 建议（如 continue），而监控发的控制词恰好
    # 也可能是 continue；若先走相等判定，会把 ghost 当成"我方消息残留在输入框"，
    # strict 控制发送就会以 "target input is not empty" 被自己拒绝。
    # 注意：Codex 的 dim 提交回显由上面的 _last_codex_prompt_is_dim 分支先行处理。
    if _looks_like_placeholder(input_line, visible):
        return InputState(kind="empty", text_after_prompt=visible)
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
    if (allow_transformed_residue and _supports_folded_drafts(agent)
            and _is_expected_head_truncated_residue(visible, expected_message)):
        # A long opening line is often rendered without the fold marker, so the
        # composer shows only a head of our own message.
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
    if allow_transformed_residue and _is_expected_scrolled_residue(ansi_text, expected_message):
        return InputState(
            kind="residue",
            text_after_prompt=visible,
            contains_expected_message=True,
        )
    return InputState(kind="occupied", text_after_prompt=visible)


# A draft that itself looks like a Talkback message: `[source→target] body`,
# `[source->target] body`, or the folded rendering `[[source→… +NL]`.
TALKBACK_DRAFT_RE = re.compile(r"^\[\[?[^\[\]\r\n]*(?:->|\u2192)")
# A long multi-line draft scrolls inside the composer, pushing its header out of
# view; the CLI then marks hidden lines above with an up arrow on a visible line.
SCROLLED_COMPOSER_RE = re.compile(r"\u2191[ \t]*$", re.MULTILINE)


def _agy_composer_indexes(plain_lines):
    """Return the composer's draft line indexes, or None if agy is not on screen.

    agy (Antigravity CLI) has no ``❯``/``›`` prompt marker. Its composer is a
    full-width rule, the ``>` line holding the draft, and a matching rule, with
    the CLI's footer hint underneath (a status or log line may sit between
    them). Scanning rule pairs bottom-up and requiring matching widths, draft
    shaped interior lines and a chrome-only tail keeps transcript ``>`` echoes,
    diffs and stale composers from being read as the live input box.
    """
    rules = [index for index, line in enumerate(plain_lines) if AGY_RULE_RE.match(line)]
    for start, end in reversed(list(zip(rules, rules[1:]))):
        if len(plain_lines[start].strip()) != len(plain_lines[end].strip()):
            continue
        interior = [index for index in range(start + 1, end) if plain_lines[index].strip()]
        if not interior:
            continue
        if AGY_PROMPT_RE.match(plain_lines[interior[0]]):
            # Draft continuations are indented; anything else (diff markers,
            # action lines, transcript text) means this is not the composer.
            if any(not AGY_PROMPT_RE.match(plain_lines[index])
                   and not plain_lines[index][:1].isspace() for index in interior[1:]):
                continue
        elif not AGY_SCROLL_MARKER_RE.search(plain_lines[interior[0]]):
            # A scrolled draft hides its `>` line; the up arrow is the only
            # evidence that this rule pair is the live composer.
            continue
        tail = [line for line in plain_lines[end + 1:] if line.strip()]
        if any(AGY_PROMPT_RE.match(line) for line in tail):
            continue
        if not any(hint in line for line in tail for hint in AGY_FOOTER_HINTS):
            continue
        return interior
    return (_agy_tall_composer_indexes(plain_lines, rules)
            or _agy_fullscreen_composer_indexes(plain_lines, rules))


def _agy_fullscreen_composer_indexes(plain_lines, rules):
    """Recover a draft taller than the whole viewport.

    Such a composer is drawn edge to edge: no rule and no footer are visible,
    and its first visible line carries the prompt marker with the right-aligned
    up arrow. Requiring that marker plus the arrow keeps transcript echoes out.
    """
    if rules:
        return None
    for index, line in enumerate(plain_lines):
        if not AGY_SCROLL_MARKER_RE.search(line):
            continue
        if not re.match(r"^[\u203a>\u276f]", line):
            continue
        if any(
            SEPARATOR_RE.match(trailing.strip()) or any(hint in trailing for hint in CHROME_HINTS)
            for trailing in plain_lines[index + 1:]
        ):
            return None
        interior = [position for position in range(index, len(plain_lines))
                    if plain_lines[position].strip()]
        if len(interior) >= 2:
            return interior
    return None


def _agy_tall_composer_indexes(plain_lines, rules):
    """Recover a composer taller than the viewport.

    When the draft outgrows the composer area, the top rule scrolls away and
    only the bottom rule stays on screen. The up-arrow line marks where the
    visible part starts. Any ``❯``/``›`` on screen means this is not agy;
    without that guard a Command Code screen would be misread as a tall agy
    composer.
    """
    if any(marker in line for line in plain_lines for marker in PROMPT_MARKERS):
        return None
    for end in reversed(rules):
        tail = [line for line in plain_lines[end + 1:] if line.strip()]
        if not tail or not any(hint in line for line in tail for hint in AGY_FOOTER_HINTS):
            continue
        if any(AGY_PROMPT_RE.match(line) for line in tail):
            continue
        markers = [index for index in range(end) if AGY_SCROLL_MARKER_RE.search(plain_lines[index])]
        if not markers:
            continue
        interior = [index for index in range(markers[-1], end) if plain_lines[index].strip()]
        if interior:
            return interior
    return None


def _is_agy_placeholder(text: str) -> bool:
    normalized = _normalize_visible(text)
    return any(normalized == _normalize_visible(hint) for hint in AGY_PLACEHOLDERS)


def _agy_drafts(plain_lines, indexes):
    """Return each composer line's draft text; continuation lines are indented."""
    drafts = []
    for index in indexes:
        match = AGY_PROMPT_RE.match(plain_lines[index])
        drafts.append(match.group(1) if match else plain_lines[index].strip())
    return drafts


def _inspect_agy(ansi_text, expected, allow_transformed_residue=False):
    raw_lines = ansi_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    plain_lines = [ANSI_RE.sub("", line) for line in raw_lines]
    indexes = _agy_composer_indexes(plain_lines)
    if indexes is None:
        return None
    drafts = _agy_drafts(plain_lines, indexes)
    visible = "\n".join(part for part in drafts if part is not None).strip()
    if not visible or _is_agy_placeholder(visible):
        return InputState(kind="empty", text_after_prompt="")
    if expected and _normalize_visible(expected) == _normalize_visible(visible):
        return InputState(kind="residue", text_after_prompt=visible, contains_expected_message=True)
    if allow_transformed_residue and expected and (
            _is_expected_head_truncated_residue(visible, expected)
            or _is_expected_soft_wrapped_residue(visible, expected)):
        # agy truncates and soft-wraps a long draft, so the composer may show
        # only a head or a wrapped rendering of our own message.
        return InputState(kind="residue", text_after_prompt=visible, contains_expected_message=True)
    return InputState(kind="occupied", text_after_prompt=visible)


def _plain_lines(ansi_text: str) -> list:
    return ANSI_RE.sub("", ansi_text).replace("\r\n", "\n").replace("\r", "\n").split("\n")


def composer_block(ansi_text: str) -> str:
    """Return the text under the active prompt, without the chrome below it.

    Status lines, separators and hints change constantly while an agent works;
    only this block is evidence of what is sitting in the input box.
    """
    plain = _plain_lines(ansi_text)
    indexes = _agy_composer_indexes(plain)
    if indexes is not None:
        draft = "\n".join(part or "" for part in _agy_drafts(plain, indexes)).strip()
        return "" if _is_agy_placeholder(draft) else draft
    segment = _segment_after_last_prompt(ansi_text)
    if segment is None:
        return ""
    lines = ANSI_RE.sub("", segment).replace("\r", "").split("\n")
    block = [lines[0]]
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            block.append("")
            continue
        if SEPARATOR_RE.match(stripped) or any(hint in stripped for hint in CHROME_HINTS):
            break
        block.append(line)
    return "\n".join(block).strip()


def is_talkback_draft(text: str) -> bool:
    stripped = text.strip()
    if TALKBACK_DRAFT_RE.match(stripped):
        return True
    lines = [line for line in stripped.split("\n") if line.strip()]
    return len(lines) >= 2 and SCROLLED_COMPOSER_RE.search(stripped) is not None


PROMPT_LINE_RE = re.compile(r"^\s*[\u276f\u203a\u00bb]")
CHROME_HINTS = (
    "ctx ", "bypass permissions", "shift+tab", "agents", "esc", "ctrl+p",
    "commands", "Tip ", "enter/esc", "shortcuts", "f2", "warnings",
    "Back to bottom", "Goal ", "~/arc", "interrupt", "tokens",
    "Main [", "tab to queue message",
)
SEPARATOR_RE = re.compile(r"^[\u2500\u2501\u2550_\-\s]{10,}$")


def sanitize_snapshot(ansi_text: str) -> str:
    """删掉【屏幕底部输入框那一行】,不让占位/ghost 文本冒充用户输入。

    判断器拿到的是剥掉 ANSI 的纯文本,而三种 CLI 的空输入框都会画占位提示:
    Codex 的 ``Ask Codex to do anything``、Claude 的 dim ``continue``、OpenCode
    方框里的 ``Ask anything…``。不处理时它们和"用户真敲了一行"完全同形,足以让
    状态判断误以为还有未提交的用户指令(例如把 dim ``continue`` 读成用户输入)。

    判定为输入框的条件(三条同时满足,避免误删历史里的用户消息回显):
      1) 该行以 ``❯`` / ``›`` 开头,且是最后一条这样的行;
      2) 它上面紧邻的是空行或界面 chrome(分隔线 / 状态栏 / 快捷键提示);
      3) 它下面直到屏幕底部也只剩界面 chrome。

    Codex 生成中会把最后一条用户消息 dim 回显在历史里,那行的上面是助手输出,
    下面通常还有 ``Working …``,两条都不满足 → 原样保留。
    """
    opencode = _inspect_opencode(ansi_text, "")
    if opencode is not None and opencode.kind in ("empty", "occupied"):
        # 方框已确认是 OpenCode 的输入框:占位与草稿都不进判断快照(与 Codex/Claude 口径一致)。
        # kind == "unknown" 说明布局没认准(多余尾行等),保持原样,不冒险删内容。
        return _sanitize_opencode(ansi_text)

    agy = _inspect_agy(ansi_text, "")
    if agy is not None and agy.kind in ("empty", "occupied"):
        # agy 的输入框由两条等宽分隔线夹住;草稿同样不属于判断快照,
        # 只删草稿行,保留分隔线与页脚,布局判断仍能认出输入框。
        return _sanitize_agy(ansi_text)

    plain = ANSI_RE.sub("", ansi_text).replace("\r\n", "\n").replace("\r", "\n")
    lines = plain.split("\n")
    prompt_index = None
    for index in range(len(lines) - 1, -1, -1):
        if PROMPT_LINE_RE.match(lines[index]):
            prompt_index = index
            break
    if prompt_index is None:
        return ansi_text
    if prompt_index == 0 or not _is_chrome_line(lines[prompt_index - 1]):
        return ansi_text
    if not _only_chrome_after("\n".join(lines[prompt_index + 1:])):
        return ansi_text
    del lines[prompt_index]
    return "\n".join(lines)


def _is_chrome_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if SEPARATOR_RE.match(stripped):
        return True
    return any(hint in stripped for hint in CHROME_HINTS)


def _only_chrome_after(text: str) -> bool:
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or SEPARATOR_RE.match(stripped):
            continue
        if not any(hint in stripped for hint in CHROME_HINTS):
            return False
    return True


def _sanitize_opencode(ansi_text: str) -> str:
    """OpenCode 输入在方框内:删掉草稿/占位区,保留 ``Build ·`` 标题行。

    标题行必须保留 —— ``strip_opencode_sidebar`` 依赖它识别方框,删了会让监控
    自身回复的侧栏裁剪失效。
    """
    lines = ansi_text.split("\n")
    plain = [ANSI_RE.sub("", line) for line in lines]
    border_index = None
    border = None
    for index, line in enumerate(plain):
        match = re.match(r"^(\s*)\u2579(\u2580{8,})", line)
        if match:
            border_index, border = index, match
    if border_index is None or border_index < 2:
        return ansi_text
    left = len(border.group(1))
    end = left + 1 + len(border.group(2))
    title_index = border_index - 1
    if not _cell_slice(plain[title_index], left, end).startswith("\u2503"):
        return ansi_text
    draft_indexes = []
    for index in range(title_index - 1, -1, -1):
        cells = _cell_slice(plain[index], left, end)
        if cells.startswith("\u2503"):
            draft_indexes.append(index)
        elif not cells.strip():
            continue
        else:
            break
    if not draft_indexes:
        return ansi_text
    return "\n".join(line for index, line in enumerate(lines) if index not in draft_indexes)


def _sanitize_agy(ansi_text: str) -> str:
    """Drop only agy's draft lines, keeping the rules and footer that frame them."""
    lines = ansi_text.split("\n")
    plain = [ANSI_RE.sub("", line) for line in lines]
    indexes = _agy_composer_indexes(plain)
    if indexes is None:
        return ansi_text
    return "\n".join(
        "" if index in indexes else line
        for index, line in enumerate(lines)
    )


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
    if index == 0 or not re.match(r'^\s*┃\s+(?:(?:Build|Plan)(?:\s+auto)?\s*·|Sisyphus\s+-\s+Ultraworker\b.*·.*OpenCode)', lines[index - 1]):
        return None
    trailing = lines[index + 1:]
    if not trailing or 'ctrl+p' not in trailing[0] or 'commands' not in trailing[0]:
        return None
    is_home = ('Sisyphus - Ultraworker' in lines[index - 1]
               or (len(trailing) == 2 and re.fullmatch(r'\s*\d+\.\d+\.\d+\s*', trailing[1]) is not None))
    if is_home:
        extra = trailing[1:]
        if any(not ('Tip Use up' in line or ('/status' in line and re.search(r'\b\d+\.\d+\.\d+\b', line)) or re.fullmatch(r'\s*\d+\.\d+\.\d+\s*', line)) for line in extra):
            return InputState(kind='unknown', text_after_prompt='')
    elif len(trailing) != 1:
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
    if is_home and re.fullmatch(r'Ask anything…\s+"[^"\r\n]+"', visible):
        line = next((line for line in reversed(ansi_text.splitlines()) if 'Ask anything…' in line), '')
        placeholder = re.search(r'\x1b\[38;2;128;128;128m(?:\x1b\[48;2;\d+;\d+;\d+m)*Ask anything…', line)
        if placeholder:
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
    # Codex also renders » at column zero, usually behind SGR styling
    # ("\x1b[0m\x1b[1m\x1b[38;2;188;140;247m»"). Indented » is chrome
    # ("  » permission bypass on [shift+tab]"), so leading spaces still exclude it.
    for match in CHEVRON_PROMPT_RE.finditer(text):
        if match.start() > marker_index:
            marker_index, marker_len = match.start(), match.end() - match.start()

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
    # A scrolled multi-line composer draws a dim `↑` at the right edge of its
    # first visible line; never mistake a scrolled draft for an empty placeholder.
    if SCROLLED_COMPOSER_RE.search(visible) is not None:
        return False
    return _all_visible_styled_as_placeholder(ansi_segment)


def _is_expected_multiline_paste_block(visible: str, expected_message: str) -> bool:
    return "\n" in expected_message and PASTE_BLOCK_RE.match(visible) is not None


def _is_expected_paste_block_residue(visible: str, expected_message: str) -> bool:
    expected = _normalize_visible(expected_message)
    return bool(
        expected
        and PASTE_BLOCK_RE.match(visible)
        and ("\n" in expected_message or len(expected) >= 40)
    )


def _folded_paste(visible: str):
    """Return the visible prefix and folded-line count of a folded composer block."""
    match = FOLDED_PASTE_RE.match(visible)
    if match is None:
        return None
    return _normalize_visible(match.group(1)), int(match.group(2))


def _is_expected_folded_residue(visible: str, expected_message: str) -> bool:
    folded = _folded_paste(visible)
    if folded is None or not expected_message:
        return False
    prefix, extra_lines = folded
    if len(prefix) < FOLDED_PASTE_MIN_PREFIX:
        return False
    lines = expected_message.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # The marker counts rendered lines beyond the first, so soft wrapping of a
    # long opening line makes it larger than the logical newline count. A draft
    # with fewer lines than the message cannot be the same message.
    if extra_lines < max(0, len(lines) - 1):
        return False
    expected = _normalize_visible(expected_message)
    return bool(
        expected
        and (expected.startswith(prefix) or _compact_visible(expected).startswith(_compact_visible(prefix)))
    )


HEAD_TRUNCATED_MIN = 24


def _is_expected_head_truncated_residue(visible: str, expected_message: str) -> bool:
    expected = _normalize_visible(expected_message)
    return bool(
        expected
        and len(visible) >= HEAD_TRUNCATED_MIN
        and len(expected) > len(visible)
        and expected.startswith(visible)
    )


def _supports_folded_drafts(agent: Optional[str]) -> bool:
    if not agent:
        return False
    return agent.split()[0].split(".")[0].lower() in FOLDING_AGENTS


def is_folded_paste_block(text: str) -> bool:
    """Detect a folded multi-line draft anywhere in a rendered composer."""
    return bool(FOLDED_PASTE_MARKER_RE.search(_strip_ansi(text)))


def _is_expected_soft_wrapped_residue(segment: str, expected_message: str) -> bool:
    # Terminal rendering can omit zero-width spaces that remain in the sent text.
    # Limit this normalization to post-send transformed-residue comparison.
    segment = segment.replace('\u200b', '')
    expected_message = expected_message.replace('\u200b', '')
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
    return bool(len(rendered_tail) >= 24 and rendered_tail in expected)


def _is_expected_scrolled_residue(ansi_text: str, expected_message: str) -> bool:
    """Recognize a long just-sent message whose top lines scrolled above the composer."""
    block = composer_block(ansi_text)
    if not block or SCROLLED_COMPOSER_RE.search(block) is None:
        return False
    cleaned = SCROLLED_COMPOSER_RE.sub("", block).replace("\u200b", "")
    block_compact = _compact_visible(cleaned)
    expected_compact = _compact_visible(expected_message.replace("\u200b", ""))
    return bool(
        len(block_compact) >= 40
        and expected_compact
        and expected_compact.endswith(block_compact)
    )


def _all_visible_styled_as_placeholder(ansi_segment: str) -> bool:
    """Return True only when every non-whitespace character is dim or muted-gray."""
    dim = False
    fg_gray = False
    has_visible = False
    pos = 0
    for match in SGR_RE.finditer(ansi_segment):
        chunk = ansi_segment[pos:match.start()]
        if any(not ch.isspace() for ch in chunk):
            has_visible = True
            if not (dim or fg_gray):
                return False
        params = match.group(1) or "0"
        parts = [int(part or "0") for part in params.split(";")]
        index = 0
        while index < len(parts):
            code = parts[index]
            if code == 0:
                dim = False
                fg_gray = False
            elif code == 2:
                dim = True
            elif code == 22:
                dim = False
            elif code == 39:
                fg_gray = False
            elif code == 38 and index + 1 < len(parts):
                mode = parts[index + 1]
                if mode == 2 and index + 4 < len(parts):
                    red, green, blue = parts[index + 2], parts[index + 3], parts[index + 4]
                    fg_gray = max(red, green, blue) <= 170
                    index += 5
                    continue
                if mode == 5 and index + 2 < len(parts):
                    fg_gray = False
                    index += 3
                    continue
            elif code == 48 and index + 1 < len(parts):
                mode = parts[index + 1]
                if mode == 2 and index + 4 < len(parts):
                    index += 5
                    continue
                if mode == 5 and index + 2 < len(parts):
                    index += 3
                    continue
            elif (30 <= code <= 37) or (90 <= code <= 97):
                fg_gray = False
            index += 1
        pos = match.end()
    tail = ansi_segment[pos:]
    if any(not ch.isspace() for ch in tail):
        has_visible = True
        if not (dim or fg_gray):
            return False
    return has_visible
