"""The review sheet, `review.html`.

One self-contained HTML file beside the manifest: no scripts, no links to anything
outside it and no external fonts, so it opens offline in any browser on a laptop or a
phone. Every source image is listed, flagged ones first, the most flagged first. For
each: a small preview of the original turned upright, with the cut and each page's
page box and content box drawn on it; a small preview of each prepared page; and for
every step its value, where it came from, the confidence, the evidence and every flag,
in plain words, with the exact lines to put in an overrides file to change it.

Previews are reduced copies no longer than the `preview_long_side_px` setting, stored
in the file as JPEG to keep it small. They are only for looking; no prepared page is
ever lossy. The same run gives the same bytes.
"""

from __future__ import annotations

import base64
import html
import io
import json
import os
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from pagekit.answer import PAGE_STEPS, SOURCE_STEPS
from pagekit.geometry import apply, upright_image

REVIEW_NAME = "review.html"
# Colours that stay apart for most kinds of colour blindness.
CUT_COLOUR = (213, 94, 0)  # vermilion
PAGE_BOX_COLOUR = (0, 114, 178)  # blue
CONTENT_BOX_COLOUR = (0, 158, 115)  # green
_DETECTOR_THRESHOLDS = ("thresholds_split.toml", "thresholds_skew.toml", "thresholds.toml")
_STEP_NAMES = {
    "orientation": "Orientation",
    "split": "Pages and cut",
    "skew": "Skew (levelling)",
    "page_box": "Page box (the paper)",
    "content_box": "Content box (what is kept)",
    "margin": "Margin",
    "resolution": "Resolution",
    "batch": "Compared with the batch",
    "orientation_tag": "Orientation tag in the file",
}
_ORIGIN_WORDS = {
    "detected": "found by pagekit",
    "manual": "set by hand",
    "locked": "set by hand and locked",
}
_TURNS = {
    0: "upright as scanned (no turn)",
    1: "one quarter turn clockwise",
    2: "a half turn",
    3: "three quarter turns clockwise",
}


# --- Previews ---------------------------------------------------------------------------


def _jpeg(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.convert("L" if image.mode == "L" else "RGB").save(buffer, "JPEG", quality=60)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _reduced(image: Image.Image, long_side: int) -> Image.Image:
    scale = min(1.0, long_side / max(image.size))
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image if size == image.size else image.resize(size, Image.Resampling.BOX)


def page_preview(page: Image.Image, long_side: int) -> dict[str, Any]:
    """A small preview of a prepared page, ready to embed."""
    small = _reduced(page, long_side)
    return {"data": _jpeg(small), "size": small.size}


def _to_upright(page_plan: Any, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Points in the page's levelled grid, in the upright frame of its source."""
    chain = page_plan.chain
    output = apply(chain.levelled_to_output(), points)
    return apply(chain.source_to_upright(), chain.inverse(output))


def _corners(box: list[int]) -> list[tuple[float, float]]:
    left, top, right, bottom = box
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


def source_preview(source: Image.Image, pages: list[Any], long_side: int) -> dict[str, Any]:
    """A small preview of the original turned upright, with the cut, page boxes and
    content boxes of its pages drawn on it."""
    first = pages[0]
    turns = first.steps["orientation"]["value"]
    upright = upright_image(source, first.chain.tag, turns)
    small = _reduced(upright, long_side).convert("RGB")
    width, height = first.chain.upright_size
    sx, sy = small.width / width, small.height / height
    draw = ImageDraw.Draw(small)
    line = max(2, round(max(small.size) / 200))

    def scaled(points):
        return [(x * sx, y * sy) for x, y in points]

    for page in pages:
        for step, colour in (("page_box", PAGE_BOX_COLOUR), ("content_box", CONTENT_BOX_COLOUR)):
            box = page.steps[step]["value"]
            if box is not None:
                corners = scaled(_to_upright(page, _corners(box)))
                draw.line([*corners, corners[0]], fill=colour, width=line)
    split = first.steps["split"]["value"]
    if split["pages"] == 2:
        (x0, y0), (x1, y1) = split["cut"]

        def x_at(y: float) -> float:
            return x0 + (x1 - x0) * (y - y0) / (y1 - y0)

        draw.line(scaled([(x_at(0), 0), (x_at(height), height)]), fill=CUT_COLOUR, width=line)
    return {"data": _jpeg(small), "size": small.size}


# --- Words ------------------------------------------------------------------------------


def _value_words(step: str, value: Any) -> str:
    if step == "orientation":
        return _TURNS[value]
    if step == "split":
        if value["pages"] == 1:
            return "one page"
        (x0, y0), (x1, y1) = value["cut"]
        return f"two pages, cut from ({x0:g}, {y0:g}) to ({x1:g}, {y1:g}) in the upright image"
    if step == "skew":
        if value == 0:
            return "no rotation"
        way = "counterclockwise" if value > 0 else "clockwise"
        return f"turned {abs(value):.2f} degrees {way} to level the lines"
    if step in ("page_box", "content_box"):
        if value is None:
            return "none: a blank page (the whole page box is kept)"
        left, top, right, bottom = value
        return (
            f"left {left}, top {top}, right {right}, bottom {bottom} "
            f"({right - left} by {bottom - top} pixels of the levelled page)"
        )
    if step == "margin":
        return f"{value:g} mm around the content"
    return json.dumps(value)


def _override_line(source_ref: str, step: str, page: int | None, value: Any) -> str:
    entry: dict[str, Any] = {"source": source_ref, "step": step}
    if page is not None:
        entry["page"] = page
    entry["value"] = json.loads(json.dumps(value, sort_keys=True))  # one text per value
    return json.dumps(entry, ensure_ascii=False)


def _unmeasured(settings: dict[str, dict[str, Any]]) -> list[tuple[str, list[str]]]:
    groups = [
        (
            "thresholds_prepare.toml",
            sorted(name for name, entry in settings.items() if entry["status"] != "MEASURED"),
        )
    ]
    for name in _DETECTOR_THRESHOLDS:
        with Path(__file__).with_name(name).open("rb") as handle:
            table = tomllib.load(handle)
        groups.append(
            (name, sorted(key for key, entry in table.items() if entry["status"] != "MEASURED"))
        )
    return [(name, names) for name, names in groups if names]


# --- The sheet ----------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light dark; --ink: #1b1b1b; --paper: #fdfcf9; --soft: #f1eee6;
  --line: #d6d1c4; --flag: #a33a00; --flag-bg: #fff1e6; --ok: #22663f; --code: #f4f2ec; }
@media (prefers-color-scheme: dark) {
  :root { --ink: #ece9e2; --paper: #181816; --soft: #24231f; --line: #45423a;
    --flag: #ffb07a; --flag-bg: #3a2416; --ok: #8fd3a8; --code: #22211d; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 60rem; margin: 0 auto; padding: 1rem; }
h1 { font-size: 1.5rem; margin: 0.5rem 0; }
h2 { font-size: 1.2rem; margin: 0 0 0.5rem; overflow-wrap: anywhere; }
h3 { font-size: 1.05rem; margin: 1.25rem 0 0.5rem; border-top: 1px solid var(--line);
  padding-top: 0.75rem; overflow-wrap: anywhere; }
h4 { font-size: 1rem; margin: 0 0 0.25rem; }
.box { background: var(--soft); border: 1px solid var(--line); border-radius: 8px;
  padding: 0.75rem 1rem; margin: 1rem 0; }
.source { border: 1px solid var(--line); border-radius: 10px; padding: 1rem; margin: 1.5rem 0; }
.source.flagged { border: 2px solid var(--flag); }
.badge { display: inline-block; border-radius: 999px; padding: 0 0.6rem; font-size: 0.9rem;
  font-weight: 600; }
.badge.flagged { background: var(--flag-bg); color: var(--flag); }
.badge.clear { color: var(--ok); border: 1px solid var(--ok); }
.previews { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: flex-start; }
figure { margin: 0; max-width: 100%; }
figure img { display: block; max-width: 100%; height: auto; border: 1px solid var(--line);
  background: #fff; }
figcaption { font-size: 0.85rem; margin-top: 0.25rem; }
.key { display: inline-block; width: 1.2em; height: 0.35em; vertical-align: middle;
  margin: 0 0.2em 0 0.6em; }
.step { border-left: 4px solid var(--line); padding: 0.25rem 0 0.25rem 0.75rem;
  margin: 0.75rem 0; }
.step.flagged { border-left-color: var(--flag); }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.1rem 0.75rem; margin: 0; }
dt { font-weight: 600; }
dd { margin: 0; overflow-wrap: anywhere; }
ul.flags { margin: 0.4rem 0; padding-left: 1.2rem; color: var(--flag); }
pre { background: var(--code); border: 1px solid var(--line); border-radius: 6px;
  padding: 0.5rem; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 0.85rem;
  user-select: all; -webkit-user-select: all; margin: 0.25rem 0; }
.fix { font-size: 0.9rem; margin: 0.4rem 0 0; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { text-align: left; padding: 0.25rem 0.4rem; border-bottom: 1px solid var(--line); }
"""


def _escape(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _figure(preview: dict[str, Any], alt: str, caption: str) -> str:
    width, height = preview["size"]
    return (
        f'<figure><img loading="lazy" decoding="async" '
        f'src="data:image/jpeg;base64,{preview["data"]}" width="{width}" '
        f'height="{height}" alt="{_escape(alt)}"><figcaption>{caption}</figcaption></figure>'
    )


def _flags_list(reasons: list[str]) -> str:
    if not reasons:
        return ""
    items = "".join(f"<li>{_escape(reason)}</li>" for reason in reasons)
    return f'<ul class="flags">{items}</ul>'


def _origin_words(step: str, origin: str) -> str:
    if step == "margin" and origin == "detected":
        return "the margin_mm setting"  # a setting, not a detection
    return _ORIGIN_WORDS[origin]


def _step_block(step: str, entry: dict[str, Any], source_ref: str, page: int | None) -> str:
    flagged = bool(entry["flags"])
    confidence = (
        "none (set by hand)" if entry["confidence"] is None else f"{entry['confidence']:.2f}"
    )
    line = _override_line(source_ref, step, page, entry["value"])
    return (
        f'<section class="step{" flagged" if flagged else ""}">'
        f"<h4>{_escape(_STEP_NAMES[step])}</h4><dl>"
        f"<dt>Value</dt><dd>{_escape(_value_words(step, entry['value']))}</dd>"
        f"<dt>From</dt><dd>{_escape(_origin_words(step, entry['origin']))}</dd>"
        f"<dt>Confidence</dt><dd>{_escape(confidence)}</dd>"
        f"<dt>Evidence</dt><dd>{_escape(entry['evidence'])}</dd></dl>"
        f"{_flags_list(entry['flags'])}"
        '<p class="fix">To change it, edit the value in this line and add it to the '
        "overrides file:</p>"
        f'<pre class="override" data-step="{step}" data-page="{"" if page is None else page}">'
        f"{_escape(line)}</pre></section>"
    )


_CROP_WORDS = {
    "none": "off (the whole levelled side of the cut is kept; turn it on with --crop)",
    "page": "to the page box",
    "content": "to the content box, with the margin",
}
_MODE_SET_BY = {
    "default": "the default",
    "run": "chosen for this batch",
    "manual": "set by hand",
    "locked": "set by hand and locked",
}


def _mode_block(mode: dict[str, Any], flags: list[str], source_ref: str, page: int) -> str:
    """The page's output mode: what was chosen and by whom, what was written, the grey
    rule and whether the conversion was exact, any colour flag, and override lines."""
    written = "grey" if mode["mode"] == "grey" else "as scanned (source mode)"
    rows = [
        ("Written", written),
        ("Chosen", f"{mode['chosen']} ({_MODE_SET_BY[mode['set_by']]})"),
    ]
    if mode["chosen"] == "grey":
        rows.append(("Grey rule", mode["rule_words"]))
        rows.append(
            (
                "Conversion",
                "exact: every pixel had equal channels, so the common channel was kept"
                if mode["exact"]
                else "reviewed: the channels differ, so the rule changes values",
            )
        )
    if mode["colour"] is not None:
        colour = mode["colour"]
        rows.append(
            (
                "Colour",
                f"{colour['coloured_mm2']:g} mm² above the paper's chroma noise "
                f"({colour['paper_chroma_noise']} levels; threshold "
                f"{colour['chroma_threshold']})",
            )
        )
    cells = "".join(f"<dt>{_escape(name)}</dt><dd>{_escape(text)}</dd>" for name, text in rows)
    lines = "".join(
        f'<pre class="override" data-step="output_mode" data-page="{page}">'
        f"{_escape(_override_line(source_ref, 'output_mode', page, value))}</pre>"
        for value in ("source", "grey")
    )
    return (
        f'<section class="step{" flagged" if flags else ""}"><h4>Output mode</h4>'
        f"<dl>{cells}</dl>{_flags_list(flags)}"
        '<p class="fix">To keep this page as scanned, or to make it grey whatever its '
        "colour, add one of these lines to the overrides file:</p>"
        f"{lines}</section>"
    )


def _other_block(title: str, reasons: list[str], extra: str = "") -> str:
    if not reasons:
        return ""
    return (
        f'<section class="step flagged"><h4>{_escape(title)}</h4>'
        f"{_flags_list(reasons)}{extra}</section>"
    )


def _skipped_block(skipped: list[dict[str, Any]]) -> str:
    """The source files that could not be used, named with the reason, or nothing."""
    if not skipped:
        return ""
    items = "".join(
        f"<li><b>{_escape(entry['name'])}</b> ({_escape(entry['path'])}): "
        f"{_escape(entry['reason'])}</li>"
        for entry in skipped
    )
    return (
        '<section class="box"><h2>Files not prepared</h2>'
        f"<p>{len(skipped)} source file(s) could not be used and were skipped. They have no "
        "pages here; fix or replace them and run pagekit again, and they will be tried "
        f'again.</p><ul class="flags">{items}</ul></section>'
    )


def _contents(cards: list) -> str:
    """A short table of every source, in the sheet's order, linking to its section."""
    rows = []
    for number, (_, relative, _, pages, flags) in enumerate(cards, start=1):
        rows.append(
            f'<tr><td><a href="#source-{number}">{_escape(relative)}</a></td>'
            f"<td>{len(pages)}</td><td>{len(flags) or ''}</td></tr>"
        )
    return (
        '<section class="box"><h2>Sources</h2><table><tr><th>Source image</th>'
        "<th>Pages</th><th>Flags</th></tr>" + "".join(rows) + "</table></section>"
    )


def _batch_table(batch: dict[str, Any]) -> str:
    from pagekit.volume import MEASUREMENTS

    rows = []
    for name, (words, unit, _, _) in MEASUREMENTS.items():
        entry = batch["measurements"].get(name, {"pages": 0, "compared": False})
        if entry["compared"]:
            result = (
                f"median {entry['median']:g} {unit}, usual spread {entry['spread']:g} {unit}; "
                f"{entry['flagged']} page(s) flagged"
            )
        else:
            result = f"not compared: fewer than {batch['min_pages']} pages have it"
        rows.append(
            f"<tr><td>{_escape(words)}</td><td>{entry['pages']}</td><td>{_escape(result)}</td></tr>"
        )
    return (
        '<section class="box"><h2>The batch compared</h2><p>Each page is compared with the '
        f"rest of the batch, and a page more than {batch['distance']:g} times the usual "
        "spread from the middle is flagged. Blank pages are left out.</p>"
        "<table><tr><th>Measurement</th><th>Pages</th><th>Result</th></tr>"
        + "".join(rows)
        + "</table></section>"
    )


def _setting_options(settings: dict[str, dict[str, Any]]) -> list[str]:
    """The command-line options that repeat this run's settings, so a correction run
    prepares every page as this run did."""
    value = {name: entry["value"] for name, entry in settings.items()}
    given = {name for name, entry in settings.items() if entry["source"] == "override"}
    options: list[str] = []
    if "crop" in given:
        options += ["--crop", value["crop"]]
    if "output_format" in given:
        options += ["--format", value["output_format"]]
    if "max_output_dpi" in given:
        options += ["--max-dpi", f"{value['max_output_dpi']:g}"]
    if "grey_rule" in given:
        options += ["--grey-rule", value["grey_rule"]]
    if "padding_mm" in given and value["padding_mm"] > 0:
        options += ["--padding", f"{value['padding_mm']:g}mm"]
    if "padding_px" in given and value["padding_px"] > 0:
        options += ["--padding", f"{value['padding_px']}px"]
    if "stage_cache" in given and not value["stage_cache"]:
        options += ["--no-cache"]
    if "stage_cache_full" in given and value["stage_cache_full"]:
        options += ["--cache-full"]
    if "stage_cache_folder" in given and value["stage_cache_folder"]:
        options += ["--cache", value["stage_cache_folder"]]
    return options


def correction_command(plan: Any) -> str:
    """The shell command that applies `overrides.json` in the output folder.

    It uses absolute paths and the Python that ran prepare, and names the folder that
    holds pagekit as PYTHONPATH for that one command, so it works from any folder,
    whether or not pagekit is installed and whatever PYTHONSAFEPATH says. It is one line
    for macOS and Linux shells (sh, bash, zsh); the Windows form is for later. It
    repeats the run's own settings (crop, format, shrinking, grey rule, padding,
    cache) and the tone view."""
    home = Path(__file__).resolve().parent.parent
    words = [
        sys.executable,
        "-m",
        "pagekit",
        "prepare",
        "--output",
        str(plan.output_dir),
        "--overrides",
        str(plan.output_dir / "overrides.json"),
    ]
    if plan.project_path != plan.output_dir / "pagekit-project.json":
        words += ["--project", str(plan.project_path)]
    words += _setting_options(plan.settings)
    if plan.tone_view:
        words.append("--tone-view")
    return f"PYTHONPATH={shlex.quote(str(home))} " + " ".join(shlex.quote(word) for word in words)


def build(plan: Any, entries: list[dict[str, Any]], previews: dict[str, Any]) -> str:
    """The review sheet's HTML for `plan`, its manifest page `entries` (same order as
    `plan.pages`) and `previews` ({"sources": {relative: preview}, "pages": [preview]})."""
    sources: dict[str, list[int]] = {}
    for index, page in enumerate(plan.pages):
        sources.setdefault(page.source.relative, []).append(index)
    shas = [plan.pages[indexes[0]].source.sha256 for indexes in sources.values()]

    def reference(source: Any) -> str:
        # The sha256 names the source wherever the overrides file is kept; a file
        # present twice is named by its path from the output folder instead.
        if shas.count(source.sha256) == 1:
            return source.sha256
        return Path(os.path.relpath(source.path, plan.output_dir)).as_posix()

    cards = []
    for relative, indexes in sources.items():
        pages = [plan.pages[index] for index in indexes]
        flags = sorted({(flag["step"], flag["reason"]) for page in pages for flag in page.flags})
        cards.append((-len(flags), relative, indexes, pages, flags))
    cards.sort(key=lambda card: (card[0], card[1]))

    total_pages = len(plan.pages)
    flagged_pages = sum(1 for entry in entries if entry["flags"])
    flagged_sources = sum(1 for card in cards if card[0])
    settings = plan.settings
    unmeasured = _unmeasured(settings)
    unmeasured_count = sum(len(names) for _, names in unmeasured)
    unmeasured_lists = "".join(
        f"<p><b>{_escape(name)}</b>: {_escape(', '.join(names))}</p>" for name, names in unmeasured
    )
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>pagekit review</title>",
        f"<style>{_STYLE}</style></head><body><main>",
        "<h1>Review of prepared pages</h1>",
        f"<p>{len(cards)} source image(s), {total_pages} prepared page(s). "
        f"<b>{flagged_pages} page(s) from {flagged_sources} source image(s) need a look</b>; "
        "they are listed first, the most flagged first.</p>",
        _skipped_block(plan.skipped),
        _contents(cards),
        '<section class="box"><h2>Before you trust this sheet</h2>',
        (
            f"<p>{unmeasured_count} settings are starting guesses that have not yet been "
            "measured on real pages. Until they are, a flag means <b>look at this page</b>, "
            "and no flag is not proof that the page is right.</p>"
            if unmeasured_count
            else "<p>Every setting has been measured on real pages.</p>"
        ),
        f"<details><summary>The settings not yet measured</summary>{unmeasured_lists}</details>",
        "</section>",
        '<section class="box"><h2>How to correct a page</h2>',
        "<p>Under every step there is a line ready to copy. Change the value in it, then put "
        "the line in a file named <code>overrides.json</code> in this folder, inside the "
        "list, with a comma between lines:</p>",
        '<pre>{"schema": "pagekit-overrides.v1", "overrides": [\n'
        "  ...the lines you copied, separated by commas...\n]}</pre>",
        "<p>A value you set is kept on every run. If something it was set on changes later "
        "(the cut moves, say), pagekit still keeps it but flags it for a check; add "
        '<code>"lock": true</code> to its line to keep it without that flag.</p>',
        "<p>Then run pagekit again by pasting this command into a terminal; it works from "
        "any folder. It keeps every other value and redoes only what depends on your "
        "change:</p>",
        f'<pre class="command">{_escape(correction_command(plan))}</pre>',
        "<p>On the previews: "
        f'<span class="key" style="background:rgb{CUT_COLOUR}"></span>cut between pages '
        f'<span class="key" style="background:rgb{PAGE_BOX_COLOUR}"></span>page box (the paper) '
        f'<span class="key" style="background:rgb{CONTENT_BOX_COLOUR}"></span>content box '
        "(what is kept, before the margin).</p></section>",
    ]
    if plan.batch:
        parts.append(_batch_table(plan.batch))

    for number, (_, relative, indexes, pages, flags) in enumerate(cards, start=1):
        first = pages[0]
        ref = reference(first.source)
        badge = (
            f'<span class="badge flagged">{len(flags)} flag(s)</span>'
            if flags
            else '<span class="badge clear">no flags</span>'
        )
        parts.append(
            f'<article class="source{" flagged" if flags else ""}" id="source-{number}">'
            f"<h2>{_escape(relative)} {badge}</h2>"
        )
        figures = [
            _figure(
                previews["sources"][relative],
                f"{relative}, turned upright, with the cut and boxes drawn",
                "The original, turned upright",
            )
        ]
        for index, page in zip(indexes, pages, strict=True):
            figures.append(
                _figure(
                    previews["pages"][index],
                    f"prepared page {page.number}: {page.output_name}",
                    f"Page {page.number}: {_escape(page.output_name)}",
                )
            )
        parts.append(f'<div class="previews">{"".join(figures)}</div>')
        stage_links = previews.get("cache", {}).get(first.source.sha256, [])
        if stage_links:
            names = {"opened": "as opened", "upright": "upright, with the cut"}
            items = []
            for link in stage_links:
                label = names.get(link["stage"]) or f"page {link['page']} {link['stage']}"
                items.append(f'<a href="{_escape(link["href"])}">{_escape(label)}</a>')
            parts.append(
                '<p class="fix">Each step\'s image (in the stage cache, for looking only): '
                + " · ".join(items)
                + "</p>"
            )

        parts.append("<h3>The whole image</h3>")
        entry = entries[indexes[0]]
        shown = set()
        for step in SOURCE_STEPS:
            parts.append(_step_block(step, entry["steps"][step], ref, None))
            shown |= {(step, reason) for reason in entry["steps"][step]["flags"]}
        resolution = [flag["reason"] for flag in first.flags if flag["step"] == "resolution"]
        fix = ""
        if resolution:
            line = _override_line(ref, "resolution", None, [300, 300])
            fix = (
                '<p class="fix">To give the true resolution, in dots per inch across and '
                "down, edit and add this line:</p>"
                f'<pre class="override" data-step="resolution" data-page="">{_escape(line)}</pre>'
            )
        parts.append(_other_block("Resolution", resolution, fix))
        split_extra = [
            flag["reason"]
            for flag in first.flags
            if flag["step"] == "split" and (flag["step"], flag["reason"]) not in shown
        ]
        parts.append(_other_block("Pages set by hand that no longer exist", split_extra))

        for index, page in zip(indexes, pages, strict=True):
            entry = entries[index]
            parts.append(f"<h3>Page {page.number}: {_escape(page.output_name)}</h3>")
            crop = page.applied.get("crop", "content")
            parts.append(f"<p>Cropping: {_escape(_CROP_WORDS[crop])}</p>")
            page_shown = set(shown)
            for step in PAGE_STEPS:
                parts.append(_step_block(step, entry["steps"][step], ref, page.number))
                page_shown |= {(step, reason) for reason in entry["steps"][step]["flags"]}
            mode_flags = [f["reason"] for f in page.flags if f["step"] == "output_mode"]
            parts.append(_mode_block(entry["output_mode"], mode_flags, ref, page.number))
            page_shown |= {("output_mode", reason) for reason in mode_flags}
            others: dict[str, list[str]] = {}
            for flag in page.flags:
                key = (flag["step"], flag["reason"])
                if key in page_shown or flag["step"] in ("resolution", "split"):
                    continue
                others.setdefault(flag["step"], []).append(flag["reason"])
            for step, reasons in others.items():
                parts.append(_other_block(_STEP_NAMES.get(step, step), reasons))
        parts.append("</article>")
    parts.append("</main></body></html>\n")
    return "\n".join(parts)
