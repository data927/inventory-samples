"""Notion HTML to Word conversion using html2docx."""

from __future__ import annotations

import base64
import binascii
import html as html_module
import io
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

# CSS ``width`` / ``max-width`` / ``height`` in ``px`` (Notion uses ``style="width:640px"``).
_STYLE_PX_DIM = re.compile(
    r"(?:^|;)\s*(?:max-)?width\s*:\s*(\d+)\s*px",
    re.IGNORECASE,
)
_STYLE_PX_HEIGHT = re.compile(
    r"(?:^|;)\s*height\s*:\s*(\d+)\s*px",
    re.IGNORECASE,
)

_COLOR_RGBA_RE = re.compile(
    r"(?<![a-z-])color\s*:\s*rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([0-9.]+))?\s*\)",
    re.IGNORECASE,
)
_COLOR_HEX_RE = re.compile(r"(?<![a-z-])color\s*:\s*#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", re.IGNORECASE)

_BG_RGBA_RE = re.compile(
    r"background-color\s*:\s*rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([0-9.]+))?\s*\)",
    re.IGNORECASE,
)
_BG_HEX_RE = re.compile(r"background-color\s*:\s*#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", re.IGNORECASE)

_FONT_SIZE_PT_RE = re.compile(r"font-size\s*:\s*([0-9.]+)\s*pt\b", re.IGNORECASE)
_FONT_SIZE_PX_RE = re.compile(r"font-size\s*:\s*([0-9.]+)\s*px\b", re.IGNORECASE)
_FONT_WEIGHT_BOLD_RE = re.compile(r"font-weight\s*:\s*(bold|[6-9]\d{2}|[1-9]\d{3})\b", re.IGNORECASE)
_FONT_STYLE_ITALIC_RE = re.compile(r"font-style\s*:\s*italic\b", re.IGNORECASE)

# Notion export palette (from embedded CSS in typical exports).
_NOTION_BLOCK_COLORS: dict[str, str] = {
    "block-color-default": "color: inherit",
    "block-color-gray": "color: rgba(120, 119, 116, 1)",
    "block-color-brown": "color: rgba(159, 107, 83, 1)",
    "block-color-orange": "color: rgba(217, 115, 13, 1)",
    "block-color-yellow": "color: rgba(203, 145, 47, 1)",
    "block-color-teal": "color: rgba(68, 131, 97, 1)",
    "block-color-blue": "color: rgba(51, 126, 169, 1)",
    "block-color-purple": "color: rgba(144, 101, 176, 1)",
    "block-color-pink": "color: rgba(193, 76, 138, 1)",
    "block-color-red": "color: rgba(212, 76, 71, 1)",
}

_NOTION_BLOCK_BACKGROUNDS: dict[str, str] = {
    "block-color-gray_background": "background-color: rgba(241, 241, 239, 1)",
    "block-color-brown_background": "background-color: rgba(244, 238, 238, 1)",
    "block-color-orange_background": "background-color: rgba(251, 236, 221, 1)",
    "block-color-yellow_background": "background-color: rgba(251, 243, 219, 1)",
    "block-color-teal_background": "background-color: rgba(237, 243, 236, 1)",
    "block-color-blue_background": "background-color: rgba(231, 243, 248, 1)",
    "block-color-purple_background": "background-color: rgba(244, 240, 247, 0.8)",
    "block-color-pink_background": "background-color: rgba(249, 238, 243, 0.8)",
    "block-color-red_background": "background-color: rgba(253, 235, 236, 1)",
}

_NOTION_SELECT_BGS: dict[str, str] = {
    "select-value-color-interactiveBlue": "background-color: rgba(35, 131, 226, .07)",
    "select-value-color-pink": "background-color: rgba(245, 224, 233, 1)",
    "select-value-color-purple": "background-color: rgba(232, 222, 238, 1)",
    "select-value-color-green": "background-color: rgba(219, 237, 219, 1)",
    "select-value-color-gray": "background-color: rgba(227, 226, 224, 1)",
    "select-value-color-translucentGray": "background-color: rgba(255, 255, 255, 0.0375)",
    "select-value-color-orange": "background-color: rgba(250, 222, 201, 1)",
    "select-value-color-brown": "background-color: rgba(238, 224, 218, 1)",
    "select-value-color-red": "background-color: rgba(255, 226, 221, 1)",
    "select-value-color-yellow": "background-color: rgba(253, 236, 200, 1)",
    "select-value-color-blue": "background-color: rgba(211, 229, 239, 1)",
}

_HTML2DOCX_COLOR_PATCHED = False


def _merge_style_declarations(existing: str, *fragments: str) -> str:
    parts = [p.strip() for p in (existing or "").split(";") if p.strip()]
    for frag in fragments:
        f = (frag or "").strip().rstrip(";")
        if not f:
            continue
        # naive de-dupe by property name
        prop = f.split(":", 1)[0].strip().lower()
        parts = [p for p in parts if not p.strip().lower().startswith(prop + ":")]
        parts.append(f)
    return "; ".join(parts)


def _notion_inline_block_color_classes(node) -> None:
    """Copy Notion ``block-color-*`` / ``select-value-color-*`` classes into inline ``style``."""

    for el in node.xpath(".//*[@class]"):
        cls = (el.get("class") or "").strip()
        if not cls:
            continue
        merges: list[str] = []
        for tok in cls.split():
            if tok in _NOTION_BLOCK_COLORS:
                merges.append(_NOTION_BLOCK_COLORS[tok])
            elif tok in _NOTION_BLOCK_BACKGROUNDS:
                merges.append(_NOTION_BLOCK_BACKGROUNDS[tok])
            elif tok in _NOTION_SELECT_BGS:
                merges.append(_NOTION_SELECT_BGS[tok])
        if not merges:
            continue
        cur = el.get("style") or ""
        merged = cur
        for frag in merges:
            merged = _merge_style_declarations(merged, frag)
        el.set("style", merged)


def _notion_lift_heading_color_styles_into_spans(node) -> None:
    """``html2docx`` ignores attributes on ``<h*>``; wrap color styles in an inner ``<span>``."""

    try:
        from lxml import etree
    except ImportError:
        return

    for h in node.xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6"):
        style = (h.get("style") or "").strip()
        if not style:
            continue
        decls = [d.strip() for d in style.split(";") if d.strip()]
        color_decls = [
            d
            for d in decls
            if d.lower().startswith("color:")
            or d.lower().startswith("background-color:")
            or d.lower().startswith("background:")
        ]
        if not color_decls:
            continue
        rest = [d for d in decls if d not in color_decls]
        if rest:
            h.set("style", "; ".join(rest))
        else:
            h.attrib.pop("style", None)

        span = etree.Element("span")
        span.set("style", "; ".join(color_decls))
        lead = h.text
        h.text = None
        if lead:
            span.text = lead
        for ch in list(h):
            span.append(ch)
        h.insert(0, span)


def _hex_to_rgb(hx: str) -> tuple[int, int, int]:
    hx = hx.strip()
    if len(hx) == 3:
        hx = "".join(ch * 2 for ch in hx)
    return int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)


def _first_rgb_from_style(style: str) -> tuple[int, int, int] | None:
    s = style or ""
    m = _COLOR_RGBA_RE.search(s)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))
    m = _COLOR_HEX_RE.search(s)
    if m:
        try:
            return _hex_to_rgb(m.group(1))
        except Exception:
            return None
    return None


def _first_bg_rgb_from_style(style: str) -> tuple[int, int, int] | None:
    s = style or ""
    m = _BG_RGBA_RE.search(s)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))
    m = _BG_HEX_RE.search(s)
    if m:
        try:
            return _hex_to_rgb(m.group(1))
        except Exception:
            return None
    return None


def _font_size_pt_from_style(style: str) -> float | None:
    s = style or ""
    m = _FONT_SIZE_PT_RE.search(s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = _FONT_SIZE_PX_RE.search(s)
    if m:
        try:
            return round(float(m.group(1)) * 0.75, 2)
        except ValueError:
            return None
    return None


_HTML_VOID_ELEMENTS = frozenset([
    "area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "param", "source", "track", "wbr",
])


def _apply_shading(run, r: int, g: int, b: int) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    hex_fill = f"{r:02X}{g:02X}{b:02X}"
    rPr = run._r.get_or_add_rPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    rPr.append(shd)


def _ensure_html2docx_color_support() -> None:
    """Monkeypatch html2docx to inherit color/bg from any element (not just spans).

    Strategy:
    - Patch ``html_attrs_to_font_style`` to extract color, background-color, font-size,
      bold, and italic from <span> style attributes → stored in ``self.attrs`` as
      sentinel keys (``notion_color_rgb``, ``notion_bg_rgb``, ``notion_font_size_pt``).
    - Patch ``handle_starttag`` / ``handle_endtag`` to maintain a ``_color_stack`` that
      propagates inherited color/bg through ANY element (div, ul, li, strong, …), not
      only spans.
    - Patch ``add_text`` to apply both the explicit span-level attrs and the inherited
      stack color when no explicit color is present on the current run.
    """

    global _HTML2DOCX_COLOR_PATCHED
    if _HTML2DOCX_COLOR_PATCHED:
        return

    import importlib
    m = importlib.import_module("html2docx.html2docx")

    orig_font_style = m.html_attrs_to_font_style
    orig_handle_starttag = m.HTML2Docx.handle_starttag
    orig_handle_endtag = m.HTML2Docx.handle_endtag

    def html_attrs_to_font_style(attrs):  # type: ignore[no-redef]
        styles = orig_font_style(attrs)
        try:
            style = m.get_attr(attrs, "style")
        except Exception:
            style = ""
        rgb = _first_rgb_from_style(style)
        if rgb and not any(k == "notion_color_rgb" for k, _ in styles):
            styles = list(styles) + [("notion_color_rgb", rgb)]
        bg_rgb = _first_bg_rgb_from_style(style)
        if bg_rgb and not any(k == "notion_bg_rgb" for k, _ in styles):
            styles = list(styles) + [("notion_bg_rgb", bg_rgb)]
        font_size_pt = _font_size_pt_from_style(style)
        if font_size_pt and not any(k == "notion_font_size_pt" for k, _ in styles):
            styles = list(styles) + [("notion_font_size_pt", font_size_pt)]
        if _FONT_WEIGHT_BOLD_RE.search(style) and not any(k == "bold" for k, _ in styles):
            styles = list(styles) + [("bold", True)]
        if _FONT_STYLE_ITALIC_RE.search(style) and not any(k == "italic" for k, _ in styles):
            styles = list(styles) + [("italic", True)]
        return styles

    def handle_starttag(self, tag, attrs):  # type: ignore[no-redef]
        if not hasattr(self, "_color_stack"):
            self._color_stack: list[tuple] = []

        try:
            style_str = m.get_attr(attrs, "style")
        except Exception:
            style_str = ""
        own_color = _first_rgb_from_style(style_str)
        own_bg = _first_bg_rgb_from_style(style_str)

        inh_color, inh_bg = self._color_stack[-1] if self._color_stack else (None, None)
        eff_color = own_color or inh_color
        eff_bg = own_bg or inh_bg

        if tag not in _HTML_VOID_ELEMENTS:
            self._color_stack.append((eff_color, eff_bg))

        orig_handle_starttag(self, tag, attrs)

    def handle_endtag(self, tag):  # type: ignore[no-redef]
        orig_handle_endtag(self, tag)
        if tag not in _HTML_VOID_ELEMENTS:
            if hasattr(self, "_color_stack") and self._color_stack:
                self._color_stack.pop()

    def add_text(self, data: str):  # type: ignore[no-redef]
        if self.p is None:
            self.p = self.prepare_p()
        if self.r is None:
            self.r = self.p.add_run()
            from docx.shared import Pt, RGBColor

            has_explicit_color = False
            has_explicit_bg = False

            for attrs in self.attrs:
                for font_attr, value in attrs:
                    if font_attr == "notion_color_rgb":
                        has_explicit_color = True
                        r, g, b = value
                        self.r.font.color.rgb = RGBColor(r, g, b)
                    elif font_attr == "notion_bg_rgb":
                        has_explicit_bg = True
                        _apply_shading(self.r, *value)
                    elif font_attr == "notion_font_size_pt":
                        self.r.font.size = Pt(value)
                    else:
                        setattr(self.r.font, font_attr, value)

            # Fall back to inherited color/bg from the tag stack
            stack = getattr(self, "_color_stack", None)
            if stack:
                inh_color, inh_bg = stack[-1]
                if inh_color and not has_explicit_color:
                    self.r.font.color.rgb = RGBColor(*inh_color)
                if inh_bg and not has_explicit_bg:
                    _apply_shading(self.r, *inh_bg)

        self.r.add_text(data)

    m.html_attrs_to_font_style = html_attrs_to_font_style
    m.HTML2Docx.handle_starttag = handle_starttag
    m.HTML2Docx.handle_endtag = handle_endtag
    m.HTML2Docx.add_text = add_text
    _HTML2DOCX_COLOR_PATCHED = True


def _class_blob(el) -> str:
    return f" {(el.get('class') or '').strip()} "


def _notion_unwrap_layout_lists(node) -> None:
    """Notion uses <ul class='toggle'> and <ul class='to-do-list'> without list bullets.

    html2docx maps every <ul> to Word list styles, so toggles look like giant bullet
    lists. Convert those containers to <div> and their <li> to <div>. Preserve
    ``bulleted-list`` / ``numbered-list`` (and plain <ol>).
    """

    def depth(el) -> int:
        d = 0
        p = el
        while p is not None:
            d += 1
            p = p.getparent()
        return d

    uls = node.xpath(".//ul")
    uls_sorted = sorted(uls, key=depth, reverse=True)

    for ul in uls_sorted:
        cls = _class_blob(ul)
        if " bulleted-list " in cls or " numbered-list " in cls:
            continue
        if " toggle " not in cls and " to-do-list " not in cls:
            continue
        ul.tag = "div"
        for child in list(ul):
            if child.tag == "li":
                child.tag = "div"


def _notion_merge_header_icon_and_title(node) -> None:
    """Notion puts emoji in a span and the title in h1; html2docx glues them. Collapse to one h1."""
    try:
        from lxml import etree
    except ImportError:
        return

    for header in node.xpath(".//header"):
        h1s = header.xpath(
            ".//h1[contains(concat(' ', normalize-space(@class), ' '), ' page-title ')]"
        )
        if not h1s:
            continue
        h1 = h1s[0]
        icon_text = "".join(
            t.strip()
            for el in header.xpath('.//span[contains(@class, "icon")]')
            for t in el.itertext()
        ).strip()
        title_text = "".join(h1.itertext()).strip()
        merged = f"{icon_text} {title_text}".strip() if icon_text else title_text
        for ch in list(header):
            header.remove(ch)
        new_h1 = etree.SubElement(header, "h1")
        new_h1.set("class", "page-title")
        new_h1.text = merged


def _notion_anchor_embedded_media_to_underlined_label(node) -> None:
    """Turn ``<a href>…<img/></a>`` bookmark-style links into plain underlined text.

    ``html2docx`` + Word often render remote / missing embeds as a large black box.
    Notion bookmark rows are usually ``<a>`` wrapping preview images; we keep **only**
    a short visible label (alt text, visible text, or host) as ``<u>…</u>``.

    Standalone ``<img>`` / ``<picture>`` outside anchors is untouched (still handled
    by the existing image pipeline).
    """

    try:
        from lxml import etree
    except ImportError:
        return

    def depth(el) -> int:
        d = 0
        p = el
        while p is not None:
            d += 1
            p = p.getparent()
        return d

    anchors = sorted(node.xpath(".//a[@href]"), key=depth, reverse=True)
    for a in anchors:
        if not a.xpath(".//img | .//picture"):
            continue

        visible = "".join(t for t in a.itertext() if t and str(t).strip()).strip()
        alts: list[str] = []
        for img in a.xpath(".//img"):
            alt = (img.get("alt") or "").strip()
            if alt:
                alts.append(alt)
        label = visible
        if not label and alts:
            label = " · ".join(dict.fromkeys(alts))
        if not label:
            href = (a.get("href") or "").strip()
            try:
                label = urlparse(href).netloc or href
            except Exception:
                label = href or "Link"
        if len(label) > 500:
            label = label[:497] + "…"

        parent = a.getparent()
        if parent is None:
            continue
        u = etree.Element("u")
        u.text = label
        parent.replace(a, u)


def _notion_links_to_underline(node) -> None:
    """Replace ``<a href>`` with ``<u>``: show only the link label, underlined.

    ``html2docx`` appends the raw ``href`` after ``<a>`` text (see its
    ``handle_data``); retagging as ``<u>`` preserves nested markup and maps to
    Word underline via ``HTML2Docx``.
    """

    def depth(el) -> int:
        d = 0
        p = el
        while p is not None:
            d += 1
            p = p.getparent()
        return d

    # Innermost anchors first (nested links are rare but safer).
    anchors = sorted(node.xpath(".//a[@href]"), key=depth, reverse=True)
    for a in anchors:
        a.tag = "u"
        for k in list(a.attrib):
            del a.attrib[k]


def _notion_prepare_for_html2docx(node) -> None:
    _notion_unwrap_layout_lists(node)
    _notion_merge_header_icon_and_title(node)
    _notion_inline_block_color_classes(node)
    _notion_lift_heading_color_styles_into_spans(node)
    _notion_anchor_embedded_media_to_underlined_label(node)
    _notion_links_to_underline(node)


def _parse_srcset_best_url(srcset: str) -> str | None:
    """Pick a URL from ``srcset`` (prefer largest ``NNNw`` descriptor)."""
    best_url: str | None = None
    best_w = -1
    for part in srcset.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        tokens = chunk.split()
        url = tokens[0].strip() if tokens else ""
        if not url:
            continue
        w = 0
        for t in tokens[1:]:
            t = t.strip().lower()
            if t.endswith("w") and t[:-1].isdigit():
                w = max(w, int(t[:-1], 10))
        if w > best_w:
            best_w = w
            best_url = url
    if best_url is not None:
        return best_url
    # Fallback: first URL in srcset
    for part in srcset.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        url = chunk.split()[0].strip()
        if url:
            return url
    return None


def _flatten_picture_elements(node) -> None:
    """Replace ``<picture>`` with a single ``<img>`` (html2docx ignores ``picture``)."""
    try:
        from lxml import etree
    except ImportError:
        return

    def depth(el) -> int:
        d = 0
        p = el
        while p is not None:
            d += 1
            p = p.getparent()
        return d

    pictures = sorted(node.xpath(".//picture"), key=depth, reverse=True)
    for picture in pictures:
        parent = picture.getparent()
        if parent is None:
            continue

        chosen: str | None = None
        imgs = picture.xpath(".//img")
        inner_img = imgs[0] if imgs else None
        if inner_img is not None:
            s = (inner_img.get("src") or "").strip()
            if s:
                chosen = s
        if not chosen:
            for src_el in picture.xpath("./source[@src]"):
                s = (src_el.get("src") or "").strip()
                if s:
                    chosen = s
                    break
        if not chosen:
            for src_el in picture.xpath("./source[@srcset]"):
                ss = src_el.get("srcset") or ""
                u = _parse_srcset_best_url(ss)
                if u:
                    chosen = u
                    break

        if not chosen:
            parent.remove(picture)
            continue

        new_img = etree.Element("img")
        new_img.set("src", chosen)
        if inner_img is not None:
            for attr in ("style", "class", "width", "height", "alt"):
                v = inner_img.get(attr)
                if v:
                    new_img.set(attr, v)
        parent.replace(picture, new_img)


def _notion_img_style_px_to_attributes(node) -> None:
    """Copy ``width`` / ``height`` from ``style="...px"`` to attributes (html2docx reads attrs only)."""
    for img in node.xpath(".//img"):
        style = (img.get("style") or "").strip()
        if not style:
            continue
        if not img.get("width"):
            m = _STYLE_PX_DIM.search(style)
            if m:
                img.set("width", m.group(1))
        if not img.get("height"):
            m = _STYLE_PX_HEIGHT.search(style)
            if m:
                img.set("height", m.group(1))


def _lazy_src_to_src(img) -> None:
    src = (img.get("src") or "").strip()
    if src:
        return
    for attr in ("data-src", "data-lazy-src", "data-original"):
        v = (img.get(attr) or "").strip()
        if v:
            img.set("src", v)
            return


def _maybe_transcode_to_png_bytes(raw: bytes) -> bytes | None:
    """If ``python-docx`` cannot decode the image, transcode to PNG via Pillow."""
    try:
        from docx.image.exceptions import UnrecognizedImageError
        from docx.image.image import Image
    except ImportError:
        return None

    try:
        Image.from_blob(memoryview(raw))
        return None
    except UnrecognizedImageError:
        pass
    try:
        from PIL import Image as PILImage
    except ImportError:
        return None

    try:
        im = PILImage.open(io.BytesIO(raw))
        rgba = ("RGBA", "P")
        if im.mode in rgba:
            im = im.convert("RGBA")
        else:
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return None


def _data_url_for_raster_bytes(data: bytes, path: Path) -> str:
    """Inline image bytes so ``html2docx`` never relies on ``file:`` URL handling (fragile across Python versions)."""
    ext = path.suffix.lower()
    mime = "image/png"
    if ext in {".jpg", ".jpeg"}:
        mime = "image/jpeg"
    elif ext == ".gif":
        mime = "image/gif"
    elif ext == ".png":
        mime = "image/png"
    elif ext == ".webp":
        mime = "image/webp"
    b64 = base64.standard_b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _export_search_roots(base_dir: Path, *, max_first_level: int = 64, max_second_level: int = 8) -> list[Path]:
    """``base_dir`` plus shallow nested dirs (typical when a page is copied beside a Notion export folder)."""
    roots: list[Path] = []
    seen: set[Path] = set()
    base_r = base_dir.resolve()
    for p in (base_r,):
        if p not in seen:
            seen.add(p)
            roots.append(p)
    try:
        subs = sorted((p for p in base_dir.iterdir() if p.is_dir()), key=lambda x: x.name.lower())
    except OSError:
        subs = []
    for p in subs[:max_first_level]:
        r = p.resolve()
        if r not in seen:
            seen.add(r)
            roots.append(r)
        try:
            inner = [c for c in p.iterdir() if c.is_dir()]
        except OSError:
            continue
        for c in inner[:max_second_level]:
            rc = c.resolve()
            if rc not in seen:
                seen.add(rc)
                roots.append(rc)
    return roots


def _resolve_local_file_under_base_dir(base_dir: Path, rel: str) -> Path | None:
    """Resolve a filesystem-relative ``src`` to an existing file (Notion ``../../…`` + relocated HTML)."""
    base_dir = base_dir.resolve()
    rel_stripped = unquote((rel or "").strip()).lstrip("/")
    if not rel_stripped:
        return None

    relp = Path(rel_stripped)
    parts = relp.parts
    anchor = base_dir
    i = 0
    while i < len(parts) and parts[i] == "..":
        anchor = anchor.parent
        i += 1
    remainder = Path(*parts[i:]) if i < len(parts) else Path()
    if not remainder.parts:
        return None

    primary = (anchor / remainder).resolve()
    if primary.is_file():
        return primary

    # Page moved to repo root while ``src`` still says ``../../ExportRoot/...``: try the
    # path suffix under ``base_dir`` and under a bounded set of subfolders.
    for root in _export_search_roots(base_dir):
        hit = (root / remainder).resolve()
        if hit.is_file():
            return hit
    return None


def _read_local_image_uri_candidate(src: str, base_dir: Path) -> str | None:
    """Resolve ``src`` to a ``data:`` URL if a local file exists (PNG/JPEG/GIF/WebP)."""
    raw = src.strip()
    if not raw or raw.startswith("data:"):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        return None

    path: Path | None = None
    if parsed.scheme == "file":
        p = Path(parsed.path)
        if p.is_file():
            path = p
    elif not parsed.scheme and not parsed.netloc:
        rel = parsed.path or raw
        path = _resolve_local_file_under_base_dir(base_dir, rel)

    if path is None:
        return None

    try:
        data = path.read_bytes()
    except OSError:
        return None

    png = _maybe_transcode_to_png_bytes(data)
    if png is not None:
        b64 = base64.standard_b64encode(png).decode("ascii")
        return f"data:image/png;base64,{b64}"
    return _data_url_for_raster_bytes(data, path)


def _resolve_img_sources_for_html2docx(node, base_dir: Path) -> None:
    """Turn relative image paths into ``data:`` URLs so html2docx can load bytes."""
    for img in node.xpath(".//img"):
        _lazy_src_to_src(img)
        src = (img.get("src") or "").strip()
        if not src:
            continue
        if src.startswith("data:"):
            # Inline data may be WebP etc.; transcode if needed.
            header, _, b64rest = src.partition(",")
            if "base64" not in header.lower():
                continue
            try:
                raw = base64.standard_b64decode(b64rest, validate=True)
            except (ValueError, binascii.Error):
                continue
            png = _maybe_transcode_to_png_bytes(raw)
            if png is not None:
                b64 = base64.standard_b64encode(png).decode("ascii")
                img.set("src", f"data:image/png;base64,{b64}")
            continue

        if src.startswith("//"):
            img.set("src", "https:" + src)
            continue

        new_src = _read_local_image_uri_candidate(src, base_dir)
        if new_src:
            img.set("src", new_src)


def _body_html_fragment(html: str, base_dir: Path | None = None) -> str:
    """Return HTML string for the main page content only (no head/styles)."""
    try:
        from lxml import html as lh
    except ImportError:
        return re.sub(r"(?is)<head\b[^>]*>.*?</head>\s*", "", html, count=1)

    root = lh.document_fromstring(html.encode("utf-8"))
    for el in root.xpath("//script|//style"):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)

    articles = root.xpath("//article")
    if articles:
        node = articles[0]
    else:
        bodies = root.xpath("//body")
        node = bodies[0] if bodies else root

    _notion_prepare_for_html2docx(node)
    _flatten_picture_elements(node)
    _notion_img_style_px_to_attributes(node)
    if base_dir is not None:
        _resolve_img_sources_for_html2docx(node, base_dir.resolve())

    return lh.tostring(node, encoding="unicode", method="html")


def _raw_html_prepare_fragment(html: str, base_dir: Path | None) -> str:
    """When ``raw=True``, optionally still resolve images using ``base_dir``."""
    if base_dir is None:
        return html
    try:
        from lxml import html as lh
    except ImportError:
        return html

    root = lh.document_fromstring(html.encode("utf-8"))
    _notion_inline_block_color_classes(root)
    _notion_lift_heading_color_styles_into_spans(root)
    _notion_anchor_embedded_media_to_underlined_label(root)
    _notion_links_to_underline(root)
    _flatten_picture_elements(root)
    _notion_img_style_px_to_attributes(root)
    _resolve_img_sources_for_html2docx(root, base_dir.resolve())
    return lh.tostring(root, encoding="unicode", method="html")


def title_from_html(html: str, fallback: str) -> str:
    m = re.search(r"<title[^>]*>([^<]*)</title>", html, flags=re.I | re.S)
    if m:
        t = re.sub(r"\s+", " ", m.group(1)).strip()
        if t:
            return t
    return fallback


def html_string_to_docx_bytes(
    html: str,
    *,
    title: str | None = None,
    raw: bool = False,
    fallback_title: str = "document",
    base_dir: Path | None = None,
) -> io.BytesIO:
    """Convert HTML string to a ``.docx`` in memory (same pipeline as ``tools/html_to_docx``).

    When ``base_dir`` is set, relative ``<img src>`` paths are resolved against that
    directory (typically the ``.html`` file's parent) so images embed instead of
    html2docx's broken placeholder.
    """
    _ensure_html2docx_color_support()
    from html2docx import html2docx

    unescaped = html_module.unescape(
        (title or "").strip() or title_from_html(html, fallback_title)
    )
    # OOXML core ``dc:title`` is limited to 255 chars; long Notion titles otherwise crash.
    if len(unescaped) > 255:
        unescaped = unescaped[:252].rstrip() + "..."
    if raw:
        fragment = _raw_html_prepare_fragment(html, base_dir)
    else:
        fragment = _body_html_fragment(html, base_dir=base_dir)
    buf = html2docx(fragment, unescaped)
    raw_bytes = buf.getvalue()
    try:
        from extractors.docx_table_borders import docx_bytes_with_visible_table_borders

        raw_bytes = docx_bytes_with_visible_table_borders(raw_bytes)
    except Exception:
        pass
    return io.BytesIO(raw_bytes)


def html_path_to_docx_bytes(
    path: Path,
    *,
    raw: bool = False,
    title: str | None = None,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> io.BytesIO:
    """Read an HTML file and return ``.docx`` bytes."""
    html = path.read_text(encoding=encoding, errors=errors)
    return html_string_to_docx_bytes(
        html,
        title=title,
        raw=raw,
        fallback_title=path.stem,
        base_dir=path.parent,
    )
