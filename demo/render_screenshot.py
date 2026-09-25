"""Reusable terminal-style screenshot renderer for feature documentation.

Every new feature (OPA policy engine, NiFi adapter, verification loop, CIS)
gets a set of screenshots produced by calling `render_terminal(...)` with real
transcripts captured from the running system. These land under
`demo/screenshots/<feature>/NN_step.png` and go into the judge-facing docs.

Design goals:
  * Real transcripts, not mockups — we run the command, pipe the output, embed it.
  * Consistent visual language across features (title bar, subtitle, prompt colors).
  * Judge-readable at export size (≥1400px wide, retina crisp).
  * Zero external deps beyond Pillow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# --- Palette (matches project Cisco-language design system) ------------------
BG              = (16, 22, 32)        # deep navy — terminal ground
CHROME          = (24, 32, 44)        # title bar
BORDER          = (48, 58, 72)
FG              = (218, 226, 236)     # default text
DIM             = (140, 152, 168)     # subtitle / metadata
PROMPT          = (110, 220, 200)     # $-style prompt (teal)
CMD             = (240, 244, 250)     # command text
SUCCESS         = (98,  222, 128)     # ✓ / allow / green tick
DANGER          = (248, 108, 108)     # ✗ / deny / error
WARN            = (240, 196, 96)      # highlight
INFO            = (100, 176, 255)     # cyan info
ACCENT          = (4,   159, 217)     # #049FD9 Cisco blue
SHADOW          = (0, 0, 0, 90)

# --- Fonts -------------------------------------------------------------------
_MONO   = "/System/Library/Fonts/Menlo.ttc"
_SANS   = "/System/Library/Fonts/SFNS.ttf"
_SANS_FALLBACK = "/System/Library/Fonts/Helvetica.ttc"


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(_SANS_FALLBACK, size)


@dataclass
class Step:
    """One prompt+output block on the screenshot."""
    cmd: str
    output: str
    heading: str = ""   # optional bright label above the prompt (e.g. "Step 1 — verify OPA is live")


@dataclass
class Highlight:
    """Optional colored line-substring highlighting inside output text.
    `pattern` is a compiled regex; matches get painted `color`. Sparingly."""
    pattern: re.Pattern
    color: tuple[int, int, int]


# Predefined highlight sets — safe to compose per screenshot.
HIGHLIGHTS_DEFAULT = [
    Highlight(re.compile(r"\b(True|allow=True|healthy|✓|OK|Up\s+\d+\s+seconds|100%)\b"), SUCCESS),
    Highlight(re.compile(r"\b(False|allow=False|DENY|denied|ERROR|error|✗|blocked)\b"), DANGER),
    Highlight(re.compile(r"\[(INFO|OK)\]"), INFO),
    Highlight(re.compile(r"\b(Δ|delta|posterior=[\d.]+)\b"), WARN),
]


def _wrap_lines(text: str, font: ImageFont.FreeTypeFont, max_px: int) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines() or [""]:
        if font.getlength(raw) <= max_px:
            out.append(raw)
            continue
        # rare wrap for very long lines — break on whitespace
        cur = ""
        for tok in raw.split(" "):
            trial = f"{cur} {tok}".strip()
            if font.getlength(trial) <= max_px:
                cur = trial
            else:
                if cur:
                    out.append(cur)
                cur = tok
        if cur:
            out.append(cur)
    return out


def _draw_highlighted(draw: ImageDraw.ImageDraw, xy: tuple[int, int], line: str,
                       font: ImageFont.FreeTypeFont, base_color: tuple[int, int, int],
                       highlights: list[Highlight]) -> None:
    """Draw a single line; where a highlight regex matches, use the highlight color."""
    x, y = xy
    # Compute matches for every highlight, resolve overlaps by first-wins.
    spans: list[tuple[int, int, tuple[int, int, int]]] = []
    for hl in highlights:
        for m in hl.pattern.finditer(line):
            spans.append((m.start(), m.end(), hl.color))
    spans.sort()
    # Merge overlaps: keep earlier.
    merged: list[tuple[int, int, tuple[int, int, int]]] = []
    for s, e, c in spans:
        if merged and s < merged[-1][1]:
            continue
        merged.append((s, e, c))

    i = 0
    for s, e, c in merged:
        if s > i:
            seg = line[i:s]
            draw.text((x, y), seg, font=font, fill=base_color)
            x += int(font.getlength(seg))
        seg = line[s:e]
        draw.text((x, y), seg, font=font, fill=c)
        x += int(font.getlength(seg))
        i = e
    if i < len(line):
        draw.text((x, y), line[i:], font=font, fill=base_color)


def render_terminal(
    title: str,
    subtitle: str,
    steps: list[Step],
    out_path: Path,
    width: int = 1500,
    highlights: list[Highlight] | None = None,
) -> Path:
    """Render a full terminal-style screenshot to `out_path`. Height auto-fits."""
    if highlights is None:
        highlights = HIGHLIGHTS_DEFAULT

    # Layout constants.
    PAD_X          = 40
    PAD_Y          = 32
    CHROME_H       = 78
    TITLE_SIZE     = 22
    SUBTITLE_SIZE  = 13
    HEADING_SIZE   = 15
    MONO_SIZE      = 14
    LINE_H         = 20
    STEP_GAP       = 26
    PROMPT_TAG     = "❯"

    f_title    = _font(_SANS, TITLE_SIZE)
    f_subtitle = _font(_SANS, SUBTITLE_SIZE)
    f_heading  = _font(_SANS, HEADING_SIZE)
    f_mono     = _font(_MONO, MONO_SIZE)

    text_max_px = width - 2 * PAD_X - 30

    # First pass — compute height.
    body_h = PAD_Y
    step_layouts: list[dict] = []
    for step in steps:
        h = 0
        if step.heading:
            h += LINE_H + 6
        cmd_lines = _wrap_lines(step.cmd, f_mono, text_max_px)
        out_lines = _wrap_lines(step.output.rstrip(), f_mono, text_max_px)
        h += LINE_H * len(cmd_lines) + 6
        h += LINE_H * max(len(out_lines), 1)
        step_layouts.append({"h": h, "cmd_lines": cmd_lines, "out_lines": out_lines,
                              "heading": step.heading})
        body_h += h + STEP_GAP
    body_h += PAD_Y
    height = CHROME_H + body_h

    # Canvas.
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    # Chrome bar.
    d.rectangle((0, 0, width, CHROME_H), fill=CHROME)
    d.line((0, CHROME_H, width, CHROME_H), fill=BORDER)
    # macOS-style traffic lights (visual cue, not literal).
    for i, color in enumerate([(248, 96, 96), (245, 192, 66), (86, 205, 96)]):
        d.ellipse((22 + i * 22, 20, 22 + i * 22 + 14, 34), fill=color)
    d.text((PAD_X + 60, 14), title, font=f_title, fill=FG)
    d.text((PAD_X + 60, 44), subtitle, font=f_subtitle, fill=DIM)
    # Accent stripe on the right.
    d.rectangle((width - 6, 0, width, CHROME_H), fill=ACCENT)

    # Body — steps.
    y = CHROME_H + PAD_Y
    for layout in step_layouts:
        if layout["heading"]:
            d.text((PAD_X, y), layout["heading"], font=f_heading, fill=ACCENT)
            y += LINE_H + 6

        # Command lines with prompt indicator.
        for i, line in enumerate(layout["cmd_lines"]):
            prompt = f"{PROMPT_TAG} " if i == 0 else "  "
            d.text((PAD_X, y), prompt, font=f_mono, fill=PROMPT)
            d.text((PAD_X + int(f_mono.getlength(prompt)), y), line,
                   font=f_mono, fill=CMD)
            y += LINE_H
        y += 6

        # Output lines, highlighted.
        for line in layout["out_lines"] or [""]:
            _draw_highlighted(d, (PAD_X, y), line, f_mono, FG, highlights)
            y += LINE_H
        y += STEP_GAP

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path
