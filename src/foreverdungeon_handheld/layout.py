"""Display-independent pagination and 1-bit rendering.

The app produces a `Screen` view model; this module wraps and paginates it for
whatever width/height the display reports at runtime and draws a Pillow "1"
image (0 = black ink, 255 = white paper). No panel model is assumed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

FONT_DIRS = [
    os.environ.get("FDH_FONT_DIR", ""),
    "/usr/share/fonts/truetype/dejavu/",
    "/usr/share/fonts/dejavu/",
    "/usr/local/share/fonts/",
    "/Library/Fonts/",
]


@lru_cache(maxsize=64)
def font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        if d and os.path.exists(os.path.join(d, name)):
            return ImageFont.truetype(os.path.join(d, name), size)
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1 ships a scalable default
    except TypeError:
        return ImageFont.load_default()


_MEASURE = ImageDraw.Draw(Image.new("1", (1, 1)))


def text_width(text: str, f) -> float:
    return _MEASURE.textlength(text, font=f)


def line_height(f) -> int:
    asc, desc = f.getmetrics() if hasattr(f, "getmetrics") else (10, 2)
    return asc + desc + 1


def wrap(text: str, f, width: float) -> list[str]:
    """Greedy word wrap; hard-splits words longer than the line."""
    out: list[str] = []
    for para in str(text).split("\n"):
        words = para.split()
        if not words:
            out.append("")
            continue
        cur = ""
        for word in words:
            cand = word if not cur else cur + " " + word
            if text_width(cand, f) <= width:
                cur = cand
                continue
            if cur:
                out.append(cur)
            while text_width(word, f) > width and len(word) > 1:
                cut = len(word)
                while cut > 1 and text_width(word[:cut], f) > width:
                    cut -= 1
                out.append(word[:cut])
                word = word[cut:]
            cur = word
        out.append(cur)
    return out


@dataclass
class Row:
    kind: str            # heading | text | item | big | gap
    text: str = ""
    value: object = None


@dataclass
class Screen:
    key: tuple                      # changes => full refresh (screen kind, pairing code, location ...)
    title: str
    status: str = ""
    rows: list = field(default_factory=list)
    cursor: int = 0                 # index among item rows
    page: int | None = None         # explicit page, else the page holding the cursor
    footer: str = ""
    input_line: str | None = None   # command entry buffer


@dataclass
class Line:
    text: str
    kind: str
    item: int | None
    font: object
    height: int


@dataclass
class Metrics:
    body: object
    bold: object
    small: object
    header_h: int
    footer_h: int
    margin: int


def metrics(w: int, h: int) -> Metrics:
    short = min(w, h)
    if h < 150:
        size = 11
    elif short < 260:
        size = 13
    elif short < 400:
        size = 15
    else:
        size = 20
    small = max(9, size - 2)
    m = Metrics(font(size), font(size, True), font(small), 0, 0, 2 if w < 300 else 4)
    m.header_h = line_height(m.bold) + 2
    m.footer_h = line_height(m.small) + 1
    return m


def fit_font(text: str, max_w: float, max_h: float, bold: bool = True, lo: int = 10, hi: int = 96):
    best = font(lo, bold)
    for size in range(lo, hi + 1):
        f = font(size, bold)
        if text_width(text, f) <= max_w and line_height(f) <= max_h:
            best = f
        else:
            break
    return best


def _body_height(screen: Screen, h: int, m: Metrics) -> int:
    footer = m.footer_h
    if screen.input_line is not None:
        footer += line_height(m.body) + 2
    return h - m.header_h - footer - 2


def paginate(screen: Screen, w: int, h: int) -> list[list[Line]]:
    m = metrics(w, h)
    inner = w - 2 * m.margin
    body_h = _body_height(screen, h, m)
    big_count = sum(1 for r in screen.rows if r.kind == "big")
    blocks: list[tuple[str, list[Line]]] = []
    item_i = 0
    for row in screen.rows:
        if row.kind == "gap":
            blocks.append(("gap", [Line("", "gap", None, m.small, max(2, line_height(m.small) // 2))]))
        elif row.kind == "heading":
            f = m.bold
            blocks.append(("heading", [Line(t, "heading", None, f, line_height(f)) for t in wrap(row.text, f, inner)]))
        elif row.kind == "big":
            f = fit_font(row.text, inner, max(14, body_h // max(2, big_count + 1)))
            blocks.append(("big", [Line(row.text, "big", None, f, line_height(f))]))
        elif row.kind == "item":
            f = m.body
            lines = wrap(row.text, f, inner - text_width("> ", f))
            blocks.append(("item", [Line(t, "item", item_i, f, line_height(f)) for t in lines]))
            item_i += 1
        else:
            f = m.body
            blocks.append(("text", [Line(t, "text", None, f, line_height(f)) for t in wrap(row.text, f, inner)]))

    pages: list[list[Line]] = [[]]
    used = 0

    def new_page():
        nonlocal used
        pages.append([])
        used = 0

    for bi, (kind, lines) in enumerate(blocks):
        total = sum(l.height for l in lines)
        if kind == "gap":
            if pages[-1] and used + total <= body_h:
                pages[-1].extend(lines)
                used += total
            continue
        if kind == "heading":
            # keep the heading with at least the first line of what follows
            nxt = next((b for b in blocks[bi + 1:] if b[0] != "gap"), None)
            need = total + (nxt[1][0].height if nxt else 0)
            if pages[-1] and used + need > body_h:
                new_page()
            pages[-1].extend(lines)
            used += total
            continue
        if kind in ("item", "big") and total <= body_h:
            if pages[-1] and used + total > body_h:
                new_page()
            pages[-1].extend(lines)
            used += total
            continue
        for line in lines:  # text (or an oversize item) flows line by line
            if pages[-1] and used + line.height > body_h:
                new_page()
            pages[-1].append(line)
            used += line.height
    # drop gap-only pages and trailing gaps
    pages = [[l for l in p] for p in pages if any(l.kind != "gap" for l in p)] or [[]]
    return pages


def page_of_item(pages: list[list[Line]], item: int) -> int:
    for i, page in enumerate(pages):
        if any(l.item == item for l in page):
            return i
    return 0


def first_item_on(pages, page_index: int):
    for l in pages[page_index] if 0 <= page_index < len(pages) else []:
        if l.item is not None:
            return l.item
    return None


def render(screen: Screen, w: int, h: int) -> tuple[Image.Image, int, int]:
    """Returns (image, page_index, page_count)."""
    m = metrics(w, h)
    pages = paginate(screen, w, h)
    if screen.page is not None:
        pi = max(0, min(screen.page, len(pages) - 1))
    else:
        pi = page_of_item(pages, screen.cursor)
    img = Image.new("1", (w, h), 255)
    d = ImageDraw.Draw(img)
    # header bar (inverted)
    d.rectangle((0, 0, w - 1, m.header_h - 1), fill=0)
    status_w = text_width(screen.status, m.small) if screen.status else 0
    title = screen.title
    while title and text_width(title, m.bold) > w - status_w - 3 * m.margin:
        title = title[:-1]
    d.text((m.margin, 1), title, font=m.bold, fill=255)
    if screen.status:
        d.text((w - m.margin - status_w, 1 + (line_height(m.bold) - line_height(m.small)) // 2),
               screen.status, font=m.small, fill=255)
    y = m.header_h + 1
    for line in pages[pi]:
        if line.kind == "item":
            sel = line.item == screen.cursor
            prefix_w = text_width("> ", line.font)
            if sel:
                d.rectangle((0, y, w - 1, y + line.height - 1), fill=0)
            first = not any(o is not line and o.item == line.item and pages[pi].index(o) < pages[pi].index(line) for o in pages[pi])
            if first:
                d.text((m.margin, y), ">" if sel else "-", font=line.font, fill=255 if sel else 0)
            d.text((m.margin + prefix_w, y), line.text, font=line.font, fill=255 if sel else 0)
        elif line.kind == "big":
            tw = text_width(line.text, line.font)
            d.text(((w - tw) / 2, y), line.text, font=line.font, fill=0)
        elif line.kind == "heading":
            d.text((m.margin, y), line.text, font=line.font, fill=0)
        elif line.kind == "text":
            d.text((m.margin, y), line.text, font=line.font, fill=0)
        y += line.height
    # input line
    fy = h - m.footer_h
    if screen.input_line is not None:
        iy = fy - line_height(m.body) - 2
        d.rectangle((0, iy, w - 1, iy + line_height(m.body) + 1), outline=0, fill=255)
        text = "> " + screen.input_line + "_"
        while text_width(text, m.body) > w - 2 * m.margin and len(text) > 3:
            text = "> …" + text[4:]
        d.text((m.margin, iy + 1), text, font=m.body, fill=0)
    # footer
    d.line((0, fy - 1, w - 1, fy - 1), fill=0)
    pg = f"{pi + 1}/{len(pages)}" if len(pages) > 1 else ""
    pg_w = text_width(pg, m.small)
    foot = screen.footer
    while foot and text_width(foot, m.small) > w - pg_w - 3 * m.margin:
        foot = foot[:-1]
    d.text((m.margin, fy), foot, font=m.small, fill=0)
    if pg:
        d.text((w - m.margin - pg_w, fy), pg, font=m.small, fill=0)
    return img, pi, len(pages)
