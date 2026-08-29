#!/usr/bin/env python3
"""Deterministically build and validate one Support site's locale surfaces."""

from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
import re
import sys
from typing import Any
from urllib.parse import unquote, urlparse
import xml.sax.saxutils


ROOT = Path(__file__).resolve().parent
SOURCE_PATH = ROOT / "support_surface_source.json"
BUILD_MANIFEST_PATH = ROOT / "support_surface_build_manifest.json"
EMAIL = "hourstag.app@gmail.com"
SURFACES = ("index", "support", "privacy")
RTL_LOCALES = frozenset({"ar-SA", "he", "ur-PK"})
FAMILY_RE = re.compile(
    r"<!--\s*ls-family:start\s*-->.*?<!--\s*ls-family:end\s*-->",
    re.IGNORECASE | re.DOTALL,
)
SCHEMA_RE = re.compile(
    r"\s*<script\b[^>]*\btype=[\"']application/ld\+json[\"'][^>]*>"
    r".*?</script>",
    re.IGNORECASE | re.DOTALL,
)
OWN_SCHEMA_RE = re.compile(
    r"\s*<script\b(?=[^>]*\btype=[\"']application/ld\+json[\"'])"
    r"(?=[^>]*\bdata-support-surface-schema=[\"']v1[\"'])[^>]*>"
    r".*?</script>",
    re.IGNORECASE | re.DOTALL,
)
CANONICAL_OR_ALTERNATE_RE = re.compile(
    r"\s*<link\b(?=[^>]*\brel=[\"'](?:canonical|alternate)[\"'])[^>]*>",
    re.IGNORECASE,
)
OG_URL_OR_LOCALE_RE = re.compile(
    r"\s*<meta\b(?=[^>]*\bproperty=[\"']og:(?:url|locale)[\"'])[^>]*>",
    re.IGNORECASE,
)
CONTRACT_META_RE = re.compile(
    r"\s*<meta\b(?=[^>]*\bname=[\"'](?:support-surface-contract|"
    r"support-surface-source|privacy-authority-digest|app-purchase-model|"
    r"audience)[\"'])[^>]*>",
    re.IGNORECASE,
)
RAW_KEY_RE = re.compile(
    r"(?:\{\{[^{}]+\}\}|\{(?:app|email|locale|title|description)\}|"
    r"\b(?:TODO|TBD|LOREM IPSUM)\b|"
    r"(?i:\b(?:nav|privacy|support|index)_[a-z0-9_.-]+\b))",
)
EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
APP_ID_RE = re.compile(r"apps\.apple\.com/[^\"'<>\s]*?id(\d+)", re.IGNORECASE)


def load_source() -> dict[str, Any]:
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    if source.get("schema") != "support-surface-source/v1":
        raise SystemExit("unsupported support_surface_source.json schema")
    locales = tuple(source.get("official_locales") or ())
    if len(locales) != 50 or len(set(locales)) != 50:
        raise SystemExit("source must contain the exact 50 unique Apple locales")
    if tuple(sorted(source.get("required_surfaces") or ())) != tuple(sorted(SURFACES)):
        raise SystemExit("source required_surfaces does not match the contract")
    return source


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalized_text(markup: str) -> str:
    text = re.sub(
        r"(?is)<(?:script|style|template)\b[^>]*>.*?</(?:script|style|template)>",
        " ",
        markup,
    )
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def locale_path(locale: str, surface: str) -> Path:
    filename = "index.html" if surface == "index" else f"{surface}.html"
    if locale == "en-US":
        return Path(filename)
    return Path(locale) / filename


def route_for(locale: str, surface: str) -> str:
    if locale == "en-US":
        return "" if surface == "index" else f"{surface}.html"
    return f"{locale}/" if surface == "index" else f"{locale}/{surface}.html"


def canonical_for(source: dict[str, Any], locale: str, surface: str) -> str:
    return source["base_url"] + route_for(locale, surface)


def blocked_reason(
    source: dict[str, Any], locale: str, surface: str
) -> str | None:
    return (source.get("blocked_cells") or {}).get(surface, {}).get(locale)


def available_locales(source: dict[str, Any], surface: str) -> list[str]:
    return [
        locale
        for locale in source["official_locales"]
        if not blocked_reason(source, locale, surface)
    ]


def public_purchase_model(source: dict[str, Any]) -> str:
    model = source["app"]["purchase_model"]
    if model == "free_with_lifetime_unlock":
        return "free_with_one_time_unlock"
    return model


def html_attributes(markup: str, locale: str) -> str:
    direction = "rtl" if locale in RTL_LOCALES else "ltr"

    def replace(match: re.Match[str]) -> str:
        attrs = match.group(1)
        attrs = re.sub(
            r"\s+(?:lang|dir)\s*=\s*(?:[\"'][^\"']*[\"']|[^\s>]+)",
            "",
            attrs,
            flags=re.IGNORECASE,
        )
        return f'<html{attrs} lang="{locale}" dir="{direction}">'

    changed, count = re.subn(
        r"<html\b([^>]*)>",
        replace,
        markup,
        count=1,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise ValueError("page has no unique <html> element")
    return changed


def alternates_html(
    source: dict[str, Any], surface: str, locales: list[str] | None = None
) -> str:
    locales = locales or available_locales(source, surface)
    rows = [
        '<link rel="alternate" '
        f'hreflang="{html.escape(locale, quote=True)}" '
        f'href="{html.escape(canonical_for(source, locale, surface), quote=True)}">'
        for locale in locales
    ]
    rows.append(
        '<link rel="alternate" hreflang="x-default" '
        f'href="{html.escape(canonical_for(source, "en-US", surface), quote=True)}">'
    )
    return "\n".join(rows)


def schema_payload(
    source: dict[str, Any],
    locale: str,
    surface: str,
    title: str,
    canonical: str,
) -> dict[str, Any]:
    record = source["catalog_locales"][locale]
    app = source["app"]
    payload: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "url": canonical,
        "inLanguage": locale,
        "isPartOf": {
            "@type": "WebSite",
            "name": f'{record["app_name"]} Support',
            "url": source["base_url"],
        },
        "about": {
            "@type": "SoftwareApplication",
            "name": record["app_name"],
            "operatingSystem": "iOS",
            "identifier": app["app_store_id"],
            "downloadUrl": record["canonical_app_store_url"],
            "additionalProperty": {
                "@type": "PropertyValue",
                "name": "purchaseModel",
                "value": public_purchase_model(source),
            },
        },
        "publisher": {
            "@type": "Organization",
            "name": "Lumi Studio",
        },
    }
    if surface == "privacy":
        payload["genre"] = "Privacy policy"
    elif surface == "support":
        payload["genre"] = "Customer support"
    return payload


def head_contract_block(
    source: dict[str, Any],
    locale: str,
    surface: str,
    title: str,
    canonical: str,
    *,
    help_locales: list[str] | None = None,
) -> str:
    contract = source["contract"]
    if surface == "help":
        alternates = alternates_html_for_help(source, help_locales or [], locale)
    else:
        alternates = alternates_html(source, surface)
    rows = [
        f'<link rel="canonical" href="{html.escape(canonical, quote=True)}">',
        alternates,
        '<meta name="support-surface-contract" '
        f'content="{html.escape(contract["schema"], quote=True)}">',
        '<meta name="support-surface-source" '
        f'content="{html.escape(source["source_digest"], quote=True)}">',
        '<meta name="app-purchase-model" '
        f'content="{html.escape(public_purchase_model(source), quote=True)}">',
    ]
    if source["app"].get("kids"):
        rows.append('<meta name="audience" content="parents and caregivers">')
    if surface == "privacy":
        authority = source["privacy_authority"]
        rows.append(
            '<meta name="privacy-authority-digest" '
            f'content="{html.escape(authority["digest"], quote=True)}">'
        )
    rows.extend(
        [
            f'<meta property="og:locale" content="{html.escape(locale.replace("-", "_"), quote=True)}">',
            f'<meta property="og:url" content="{html.escape(canonical, quote=True)}">',
            '<script type="application/ld+json" data-support-surface-schema="v1">'
            + json.dumps(
                schema_payload(source, locale, surface, title, canonical),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).replace("</", "<\\/")
            + "</script>",
        ]
    )
    return "\n".join(rows)


def extract_title(markup: str) -> str:
    match = re.search(r"(?is)<title\b[^>]*>(.*?)</title>", markup)
    return normalized_text(match.group(1)) if match else ""


def extract_description(markup: str) -> str:
    match = re.search(
        r"<meta\b(?=[^>]*\bname=[\"']description[\"'])"
        r"(?=[^>]*\bcontent=[\"']([^\"']*)[\"'])[^>]*>",
        markup,
        flags=re.IGNORECASE,
    )
    return html.unescape(match.group(1)).strip() if match else ""


def ensure_description(
    markup: str, source: dict[str, Any], locale: str, surface: str
) -> str:
    if extract_description(markup):
        return markup
    record = source["catalog_locales"][locale]
    if surface == "privacy":
        localized = (source.get("privacy_locales") or {}).get(locale) or {}
        description = localized.get("description")
        if not description:
            description = f'{record["app_name"]} privacy information.'
    elif surface == "support":
        description = (
            f'{record["app_name"]} support, troubleshooting, and public contact.'
            if locale.startswith("en-")
            else record["decision_context"]
        )
    else:
        description = record["decision_context"]
    tag = f'<meta name="description" content="{html.escape(description, quote=True)}">'
    return re.sub(
        r"</head>",
        tag + "\n</head>",
        markup,
        count=1,
        flags=re.IGNORECASE,
    )


def locale_picker(
    source: dict[str, Any], current: str, surface: str, locales: list[str]
) -> str:
    links = []
    for locale in locales:
        current_attr = ' aria-current="true"' if locale == current else ""
        links.append(
            f'<a href="{html.escape(canonical_for(source, locale, surface), quote=True)}" '
            f'hreflang="{html.escape(locale, quote=True)}" '
            f'lang="{html.escape(locale, quote=True)}"{current_attr}>'
            f'{html.escape(source["locale_names"][locale])}</a>'
        )
    return "".join(links)


def patch_language_panel(
    markup: str,
    source: dict[str, Any],
    current: str,
    surface: str,
    locales: list[str],
) -> str:
    content = locale_picker(source, current, surface, locales)
    return re.sub(
        r"(<div\b[^>]*\bclass=[\"'][^\"']*\blanguage-panel\b[^\"']*[\"'][^>]*>)"
        r".*?(</div>)",
        lambda match: match.group(1) + content + match.group(2),
        markup,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )


def patch_primary_nav(
    markup: str, source: dict[str, Any], locale: str, surface: str
) -> str:
    if blocked_reason(source, locale, "privacy"):
        return markup

    def replace_nav(match: re.Match[str]) -> str:
        nav = match.group(0)
        anchors = list(re.finditer(r"<a\b[^>]*>.*?</a>", nav, re.I | re.S))
        if len(anchors) < 3:
            return nav
        replacements = {
            0: "index.html",
            1: "support.html",
            2: "privacy.html",
        }
        parts: list[str] = []
        cursor = 0
        for index, anchor in enumerate(anchors):
            parts.append(nav[cursor : anchor.start()])
            value = anchor.group(0)
            if index in replacements:
                target = replacements[index]
                value, count = re.subn(
                    r"\bhref\s*=\s*[\"'][^\"']*[\"']",
                    f'href="{target}"',
                    value,
                    count=1,
                    flags=re.I,
                )
                if count == 0:
                    value = value.replace("<a", f'<a href="{target}"', 1)
            parts.append(value)
            cursor = anchor.end()
        parts.append(nav[cursor:])
        return "".join(parts)

    return re.sub(
        r"<nav\b[^>]*\bclass=[\"'][^\"']*\bprimary-nav\b[^\"']*[\"'][^>]*>"
        r".*?</nav>",
        replace_nav,
        markup,
        count=1,
        flags=re.I | re.S,
    )


def patch_required_page(
    path: Path,
    source: dict[str, Any],
    locale: str,
    surface: str,
) -> None:
    markup = path.read_text(encoding="utf-8")
    markup = html_attributes(markup, locale)
    markup = ensure_description(markup, source, locale, surface)
    title = extract_title(markup)
    if not title:
        raise ValueError(f"{path}: missing title")
    canonical = canonical_for(source, locale, surface)
    markup = SCHEMA_RE.sub("", markup)
    markup = CANONICAL_OR_ALTERNATE_RE.sub("", markup)
    markup = OG_URL_OR_LOCALE_RE.sub("", markup)
    markup = CONTRACT_META_RE.sub("", markup)
    block = head_contract_block(source, locale, surface, title, canonical)
    markup, count = re.subn(
        r"</head>",
        block + "\n</head>",
        markup,
        count=1,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise ValueError(f"{path}: missing </head>")
    markup = patch_language_panel(
        markup,
        source,
        locale,
        surface,
        available_locales(source, surface),
    )
    markup = patch_primary_nav(markup, source, locale, surface)
    path.write_text(markup, encoding="utf-8", newline="\n")


def help_locale_from_path(path: Path) -> str | None:
    if path.name == "help.html":
        return "en-US"
    match = re.fullmatch(r"help\.([A-Za-z]{2}(?:-[A-Za-z]{2,4})?)\.html", path.name)
    return match.group(1) if match else None


def help_files(source: dict[str, Any]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    official = set(source["official_locales"])
    for path in sorted(ROOT.glob("help*.html")):
        locale = help_locale_from_path(path)
        if locale in official:
            result[locale] = path.relative_to(ROOT)
    return result


def help_canonical(source: dict[str, Any], relative: Path) -> str:
    return source["base_url"] + relative.as_posix()


def alternates_html_for_help(
    source: dict[str, Any], locales: list[str], current: str
) -> str:
    files = help_files(source)
    rows = []
    for locale in locales:
        relative = files[locale]
        rows.append(
            '<link rel="alternate" '
            f'hreflang="{html.escape(locale, quote=True)}" '
            f'href="{html.escape(help_canonical(source, relative), quote=True)}">'
        )
    default = files.get("en-US")
    if default:
        rows.append(
            '<link rel="alternate" hreflang="x-default" '
            f'href="{html.escape(help_canonical(source, default), quote=True)}">'
        )
    return "\n".join(rows)


def patch_help_pages(source: dict[str, Any]) -> None:
    files = help_files(source)
    locales = [locale for locale in source["official_locales"] if locale in files]
    for locale, relative in files.items():
        path = ROOT / relative
        markup = path.read_text(encoding="utf-8")
        markup = html_attributes(markup, locale)
        markup = ensure_description(markup, source, locale, "support")
        title = extract_title(markup)
        if not title:
            raise ValueError(f"{relative}: missing title")
        canonical = help_canonical(source, relative)
        markup = OWN_SCHEMA_RE.sub("", markup)
        markup = CANONICAL_OR_ALTERNATE_RE.sub("", markup)
        markup = OG_URL_OR_LOCALE_RE.sub("", markup)
        markup = CONTRACT_META_RE.sub("", markup)
        block = head_contract_block(
            source,
            locale,
            "help",
            title,
            canonical,
            help_locales=locales,
        )
        markup, count = re.subn(
            r"</head>",
            block + "\n</head>",
            markup,
            count=1,
            flags=re.IGNORECASE,
        )
        if count != 1:
            raise ValueError(f"{relative}: missing </head>")
        if "language-panel" in markup:
            links = []
            for target in locales:
                current_attr = ' aria-current="true"' if target == locale else ""
                links.append(
                    f'<a href="{html.escape(help_canonical(source, files[target]), quote=True)}" '
                    f'hreflang="{target}" lang="{target}"{current_attr}>'
                    f'{html.escape(source["locale_names"][target])}</a>'
                )
            markup = re.sub(
                r"(<div\b[^>]*\bclass=[\"'][^\"']*\blanguage-panel\b[^\"']*[\"'][^>]*>)"
                r".*?(</div>)",
                lambda match: match.group(1) + "".join(links) + match.group(2),
                markup,
                count=1,
                flags=re.I | re.S,
            )
        path.write_text(markup, encoding="utf-8", newline="\n")


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def generated_css(source: dict[str, Any]) -> str:
    accent = source["brand"]["accent"]
    return f"""
:root{{--accent:{accent};--ink:#1f2430;--muted:#626a78;--line:color-mix(in srgb,{accent} 18%,transparent)}}
*{{box-sizing:border-box}}
html{{background:#f8f9fc}}
body{{margin:0;color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans","Noto Sans Arabic","Noto Sans Hebrew","Noto Sans Devanagari","Noto Sans Bengali","Noto Sans Gujarati","Noto Sans Gurmukhi","Noto Sans Kannada","Noto Sans Malayalam","Noto Sans Oriya","Noto Sans Tamil","Noto Sans Telugu","Noto Sans Thai",sans-serif;line-height:1.65;background:radial-gradient(circle at 8% 0%,color-mix(in srgb,{accent} 13%,transparent),transparent 34rem),#f8f9fc}}
a{{color:var(--accent);text-decoration:none}}
a:hover{{text-decoration:underline}}
.shell{{width:min(1040px,calc(100% - 32px));margin:auto}}
.site-header{{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:20px 0;flex-wrap:wrap}}
.brand{{display:flex;align-items:center;gap:10px;color:var(--ink);font-weight:700}}
.brand img{{width:38px;height:38px;border-radius:12px}}
.primary-nav{{display:flex;gap:8px;flex-wrap:wrap}}
.primary-nav a{{padding:8px 12px;border:1px solid var(--line);border-radius:12px;background:#ffffffc9;white-space:nowrap}}
.primary-nav a[aria-current="page"]{{background:color-mix(in srgb,var(--accent) 12%,white)}}
.language{{position:relative}}
.language summary{{cursor:pointer;min-height:44px;display:flex;align-items:center;padding:8px 12px;border:1px solid var(--line);border-radius:12px;background:#fff;list-style:none}}
.language-panel{{position:absolute;z-index:5;inset-inline-end:0;top:calc(100% + 8px);width:min(620px,calc(100vw - 32px));max-height:62vh;overflow:auto;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px;padding:12px;border:1px solid var(--line);border-radius:18px;background:#fff;box-shadow:0 18px 50px #1f243020}}
.language-panel a{{padding:8px;border-radius:9px;font-size:13px}}
.language-panel a[aria-current="true"]{{background:color-mix(in srgb,var(--accent) 12%,white);font-weight:700}}
.hero{{padding:54px 0 24px}}
.eyebrow{{margin:0 0 10px;color:var(--accent);font-size:13px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}}
h1{{margin:0;font-size:clamp(34px,6vw,62px);line-height:1.08;font-weight:650;letter-spacing:-.035em}}
.lead{{max-width:70ch;margin:18px 0 0;color:var(--muted);font-size:18px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;padding:18px 0 42px}}
.card{{padding:22px;border:1px solid var(--line);border-radius:20px;background:#ffffffd9;box-shadow:0 18px 44px #1f24300d}}
.card.wide{{grid-column:1/-1}}
.card h2{{margin:0 0 8px;font-size:20px}}
.card p{{margin:0;color:var(--muted)}}
.card p+p{{margin-top:9px}}
.card ol{{margin:8px 0 0;padding-inline-start:22px;color:var(--muted)}}
.card li+li{{margin-top:6px}}
.cta{{display:inline-flex;min-height:44px;align-items:center;margin-top:16px;padding:10px 17px;border-radius:13px;background:var(--accent);color:#fff;font-weight:700}}
.cta:hover{{color:#fff;text-decoration:none;filter:brightness(.96)}}
.parent-note{{border-inline-start:4px solid var(--accent)}}
.site-footer{{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:28px 0 42px;border-top:1px solid var(--line);color:var(--muted);font-size:14px}}
@media(max-width:720px){{.language-panel{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
""".strip()


def generated_language_picker(
    source: dict[str, Any], locale: str, surface: str
) -> str:
    return (
        '<details class="language"><summary aria-label="'
        + esc(source["ui_locales"][locale]["language_label"])
        + '">🌐&nbsp;'
        + esc(source["locale_names"][locale])
        + '</summary><div class="language-panel">'
        + locale_picker(
            source, locale, surface, available_locales(source, surface)
        )
        + "</div></details>"
    )


def generated_nav(
    source: dict[str, Any], locale: str, surface: str
) -> str:
    ui = source["ui_locales"][locale]
    items = [
        ("index", ui["nav_home"]),
        ("support", ui["nav_support"]),
    ]
    if not blocked_reason(source, locale, "privacy"):
        items.append(("privacy", ui["nav_privacy"]))
    links = []
    for target, label in items:
        current = ' aria-current="page"' if target == surface else ""
        href = "index.html" if target == "index" else f"{target}.html"
        links.append(f'<a href="{href}"{current}>{esc(label)}</a>')
    return '<nav class="primary-nav">' + "".join(links) + "</nav>"


def generated_header(
    source: dict[str, Any], locale: str, surface: str, app_name: str
) -> str:
    icon = source["brand"].get("icon")
    icon_markup = ""
    if icon:
        src = icon if locale == "en-US" else "../" + icon
        icon_markup = f'<img src="{esc(src)}" alt="" width="38" height="38">'
    return (
        '<header class="site-header">'
        f'<a class="brand" href="index.html">{icon_markup}<span>{esc(app_name)}</span></a>'
        + generated_nav(source, locale, surface)
        + generated_language_picker(source, locale, surface)
        + "</header>"
    )


def generated_footer(
    source: dict[str, Any], locale: str, app_name: str, app_store_url: str
) -> str:
    ui = source["ui_locales"][locale]
    return (
        '<footer class="site-footer">'
        f"<span>© 2026 {esc(app_name)}</span>"
        '<span><a href="'
        + esc(app_store_url)
        + '" rel="noopener">'
        + esc(ui["app_store_label"])
        + '</a> · <a href="mailto:'
        + EMAIL
        + '">'
        + EMAIL
        + "</a></span>"
        "</footer>"
    )


def generated_document(
    source: dict[str, Any],
    locale: str,
    surface: str,
    title: str,
    description: str,
    body: str,
) -> str:
    record = source["catalog_locales"][locale]
    canonical = canonical_for(source, locale, surface)
    direction = "rtl" if locale in RTL_LOCALES else "ltr"
    authority_meta = ""
    if surface == "privacy":
        authority_meta = (
            '<meta name="privacy-authority-digest" content="'
            + esc(source["privacy_authority"]["digest"])
            + '">\n'
        )
    audience_meta = (
        '<meta name="audience" content="parents and caregivers">\n'
        if source["app"].get("kids")
        else ""
    )
    schema = json.dumps(
        schema_payload(source, locale, surface, title, canonical),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="{esc(locale)}" dir="{direction}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<meta name="robots" content="index,follow,max-image-preview:large">
<link rel="canonical" href="{esc(canonical)}">
{alternates_html(source, surface)}
<meta name="support-surface-contract" content="{esc(source["contract"]["schema"])}">
<meta name="support-surface-source" content="{esc(source["source_digest"])}">
<meta name="app-purchase-model" content="{esc(public_purchase_model(source))}">
{audience_meta}{authority_meta}<meta property="og:type" content="website">
<meta property="og:locale" content="{esc(locale.replace("-", "_"))}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(canonical)}">
<script type="application/ld+json" data-support-surface-schema="v1">{schema}</script>
<style>{generated_css(source)}</style>
</head>
<body data-generated-support-surface="v1" data-purchase-model="{esc(public_purchase_model(source))}">
<div class="shell">
{generated_header(source, locale, surface, record["app_name"])}
{body}
{generated_footer(source, locale, record["app_name"], record["app_store_url"])}
</div>
</body>
</html>
"""


def render_index(source: dict[str, Any], locale: str) -> str:
    ui = source["ui_locales"][locale]
    record = source["catalog_locales"][locale]
    app = record["app_name"]
    parent = ""
    if source["app"].get("kids"):
        parent = (
            '<section class="card wide parent-note"><h2>'
            + esc(ui["parents_heading"])
            + "</h2><p>"
            + esc(ui["parents_note"])
            + "</p></section>"
        )
    privacy = ""
    if not blocked_reason(source, locale, "privacy"):
        privacy = (
            '<section class="card"><h2>'
            + esc(ui["privacy_heading"])
            + '</h2><p><a href="privacy.html">'
            + esc(ui["privacy_link"])
            + "</a></p></section>"
        )
    body = (
        '<main><section class="hero"><p class="eyebrow">'
        + esc(ui["index_eyebrow"])
        + "</p><h1>"
        + esc(app)
        + '</h1><p class="lead">'
        + esc(record["decision_context"])
        + '</p></section><div class="grid">'
        + parent
        + '<section class="card wide"><h2>'
        + esc(ui["verified_heading"])
        + "</h2><p>"
        + esc(ui["verified_body"].format(app=app))
        + "</p></section>"
        + '<section class="card"><h2>'
        + esc(ui["purchase_heading"])
        + "</h2><p>"
        + esc(ui["purchase_label"])
        + "</p></section>"
        + '<section class="card"><h2>'
        + esc(ui["support_heading"])
        + '</h2><p><a href="support.html">'
        + esc(ui["support_link"])
        + "</a></p></section>"
        + privacy
        + '<section class="card wide"><h2>'
        + esc(ui["app_store_heading"])
        + "</h2><p>"
        + esc(ui["app_store_note"])
        + '</p><a class="cta" href="'
        + esc(record["app_store_url"])
        + '" rel="noopener">'
        + esc(ui["app_store_label"])
        + "</a></section></div></main>"
    )
    return generated_document(
        source,
        locale,
        "index",
        ui["index_title"].format(app=app),
        record["decision_context"],
        body,
    )


def render_support(source: dict[str, Any], locale: str) -> str:
    ui = source["ui_locales"][locale]
    record = source["catalog_locales"][locale]
    app = record["app_name"]
    parent = ""
    if source["app"].get("kids"):
        parent = (
            '<section class="card wide parent-note"><h2>'
            + esc(ui["parents_heading"])
            + "</h2><p>"
            + esc(ui["parents_note"])
            + "</p></section>"
        )
    steps = "".join(f"<li>{esc(step.format(app=app))}</li>" for step in ui["steps"])
    body = (
        '<main><section class="hero"><p class="eyebrow">'
        + esc(ui["support_eyebrow"])
        + "</p><h1>"
        + esc(app)
        + '</h1><p class="lead">'
        + esc(ui["support_intro"].format(app=app))
        + '</p></section><div class="grid">'
        + parent
        + '<section class="card wide"><h2>'
        + esc(ui["steps_heading"])
        + "</h2><ol>"
        + steps
        + "</ol></section>"
        + '<section class="card"><h2>'
        + esc(ui["purchase_heading"])
        + "</h2><p>"
        + esc(ui["purchase_label"])
        + "</p></section>"
        + '<section class="card"><h2>'
        + esc(ui["contact_heading"])
        + "</h2><p>"
        + esc(ui["contact_body"].format(app=app))
        + ' <a href="mailto:'
        + EMAIL
        + '">'
        + EMAIL
        + "</a></p></section>"
        + '<section class="card wide"><h2>'
        + esc(ui["app_store_heading"])
        + "</h2><p>"
        + esc(ui["app_store_note"])
        + '</p><a class="cta" href="'
        + esc(record["app_store_url"])
        + '" rel="noopener">'
        + esc(ui["app_store_label"])
        + "</a></section></div></main>"
    )
    return generated_document(
        source,
        locale,
        "support",
        ui["support_title"].format(app=app),
        ui["support_intro"].format(app=app),
        body,
    )


def render_privacy(source: dict[str, Any], locale: str) -> str:
    localized = source["privacy_locales"][locale]
    record = source["catalog_locales"][locale]
    app = record["app_name"]
    sections = []
    for section in localized["sections"]:
        raw_body = section["body"].format(app=app, email=EMAIL)
        if (
            section["topic"] == "purchase"
            and source["app"]["purchase_model"] == "paid_upfront"
        ):
            raw_body = strip_answer_prefix(raw_body)
        body = esc(raw_body)
        body = body.replace(EMAIL, f'<a href="mailto:{EMAIL}">{EMAIL}</a>')
        sections.append(
            '<section class="card" data-privacy-topic="'
            + esc(section["topic"])
            + '"><h2>'
            + esc(section["heading"])
            + "</h2><p>"
            + body
            + "</p></section>"
        )
    note = ""
    if localized.get("translation_note"):
        note = (
            '<section class="card wide"><p>'
            + esc(localized["translation_note"])
            + "</p></section>"
        )
    body = (
        '<main><section class="hero"><p class="eyebrow">'
        + esc(localized["eyebrow"])
        + "</p><h1>"
        + esc(app)
        + '</h1><p class="lead">'
        + esc(localized["lead"].format(app=app))
        + '</p><p class="lead">'
        + esc(localized["updated_label"])
        + ": "
        + esc(source["privacy_authority"]["updated"])
        + '</p></section><div class="grid">'
        + "".join(sections)
        + note
        + "</div></main>"
    )
    return generated_document(
        source,
        locale,
        "privacy",
        localized["title"].format(app=app),
        localized["description"].format(app=app),
        body,
    )


def apply_exact_replacements(source: dict[str, Any]) -> None:
    for rule in source.get("exact_replacements") or []:
        path = ROOT / rule["path"]
        if not path.is_file():
            raise ValueError(f"replacement target missing: {rule['path']}")
        markup = path.read_text(encoding="utf-8")
        old = rule["old"]
        new = rule["new"]
        if old in markup:
            markup = markup.replace(old, new)
            path.write_text(markup, encoding="utf-8", newline="\n")
        elif new not in markup:
            raise ValueError(
                f"replacement drift in {rule['path']}: neither old nor new text exists"
            )


def strip_answer_prefix(value: str) -> str:
    """Remove the short locale-specific “No.” used by subscription FAQ answers."""
    stripped = re.sub(
        r"^\s*[^.!?。！？؟।]{1,24}[.!?。！？؟।]\s*",
        "",
        value,
        count=1,
    )
    return stripped or value


def remove_paid_upfront_faqs(source: dict[str, Any]) -> None:
    if source["app"]["purchase_model"] != "paid_upfront":
        return
    questions_by_locale = source.get("paid_cleanup_questions") or {}
    for locale in source["official_locales"]:
        path = ROOT / locale_path(locale, "support")
        if not path.is_file():
            continue
        questions = {
            normalized_text(question).casefold()
            for question in questions_by_locale.get(locale, [])
        }
        if not questions:
            continue
        markup = path.read_text(encoding="utf-8")

        def replace(match: re.Match[str]) -> str:
            block = match.group(0)
            summary = re.search(
                r"(?is)<summary\b[^>]*>(.*?)</summary>", block
            )
            if not summary:
                return block
            question = normalized_text(summary.group(1)).casefold()
            return "" if question in questions else block

        markup = re.sub(
            r"(?is)<details\b[^>]*>.*?</details>",
            replace,
            markup,
        )
        path.write_text(markup, encoding="utf-8", newline="\n")

    locked_id = "q-i-paid-but-the-app-still-shows-the-locked-version"
    for relative in help_files(source).values():
        path = ROOT / relative
        markup = path.read_text(encoding="utf-8")
        removed_questions: set[str] = set()

        def remove_help_pair(match: re.Match[str]) -> str:
            removed_questions.add(normalized_text(match.group(1)))
            return ""

        markup = re.sub(
            r"(?is)<p\b[^>]*\bclass=[\"'][^\"']*\bhd-q\b[^\"']*[\"']"
            r"[^>]*\bid=[\"']"
            + re.escape(locked_id)
            + r"[\"'][^>]*>(.*?)</p>\s*"
            r"<p\b[^>]*\bclass=[\"'][^\"']*\bhd-a\b[^\"']*[\"'][^>]*>"
            r".*?</p>",
            remove_help_pair,
            markup,
        )
        if removed_questions:
            def filter_faq_schema(match: re.Match[str]) -> str:
                raw = match.group(1)
                try:
                    payload = json.loads(raw.replace("<\\/", "</"))
                except (TypeError, ValueError):
                    return match.group(0)
                if payload.get("@type") != "FAQPage":
                    return match.group(0)
                entities = payload.get("mainEntity")
                if not isinstance(entities, list):
                    return match.group(0)
                payload["mainEntity"] = [
                    entity
                    for entity in entities
                    if normalized_text(str(entity.get("name") or ""))
                    not in removed_questions
                ]
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).replace("</", "<\\/")
                return (
                    '<script type="application/ld+json">'
                    + encoded
                    + "</script>"
                )

            markup = re.sub(
                r"(?is)<script\b[^>]*\btype=[\"']application/ld\+json[\"']"
                r"[^>]*>(.*?)</script>",
                filter_faq_schema,
                markup,
            )
        path.write_text(markup, encoding="utf-8", newline="\n")


def replace_paid_privacy_copy(source: dict[str, Any]) -> None:
    if source["app"]["purchase_model"] != "paid_upfront":
        return
    overrides = source.get("paid_privacy_overrides") or {}
    for locale, override in overrides.items():
        path = ROOT / locale_path(locale, "privacy")
        if not path.is_file():
            continue
        markup = path.read_text(encoding="utf-8")
        old = override["old"]
        raw_new = override["new"]
        unstripped_new = raw_new.format(
            app=source["catalog_locales"][locale]["app_name"],
            email=EMAIL,
        )
        new = strip_answer_prefix(unstripped_new)
        if old in markup:
            markup = markup.replace(old, new)
            path.write_text(markup, encoding="utf-8", newline="\n")
        elif raw_new in markup:
            markup = markup.replace(raw_new, new)
            path.write_text(markup, encoding="utf-8", newline="\n")
        elif unstripped_new in markup:
            markup = markup.replace(unstripped_new, new)
            path.write_text(markup, encoding="utf-8", newline="\n")
        elif new not in markup:
            raise ValueError(
                f"{path.relative_to(ROOT)}: paid privacy copy drifted"
            )


def generate_managed_cells(source: dict[str, Any]) -> None:
    for surface, locales in source["managed_cells"].items():
        for locale in locales:
            if blocked_reason(source, locale, surface):
                raise ValueError(f"managed cell is also blocked: {surface}/{locale}")
            if locale not in source["ui_locales"]:
                raise ValueError(f"missing UI source for {locale}")
            if surface == "privacy" and locale not in source["privacy_locales"]:
                raise ValueError(f"missing privacy source for {locale}")
            path = ROOT / locale_path(locale, surface)
            path.parent.mkdir(parents=True, exist_ok=True)
            if surface == "index":
                markup = render_index(source, locale)
            elif surface == "support":
                markup = render_support(source, locale)
            elif surface == "privacy":
                markup = render_privacy(source, locale)
            else:
                raise AssertionError(surface)
            path.write_text(markup, encoding="utf-8", newline="\n")


def all_html_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.html")
        if ".git" not in path.parts
    )


def sitemap_url(source: dict[str, Any], relative: Path) -> str:
    posix = relative.as_posix()
    if posix == "index.html":
        return source["base_url"]
    if posix.endswith("/index.html"):
        return source["base_url"] + posix[: -len("index.html")]
    return source["base_url"] + posix


def write_sitemap(source: dict[str, Any]) -> None:
    urls = [sitemap_url(source, path.relative_to(ROOT)) for path in all_html_files()]
    rows = [
        "  <url><loc>"
        + xml.sax.saxutils.escape(url)
        + "</loc><changefreq>monthly</changefreq></url>"
        for url in sorted(set(urls))
    ]
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(rows)
        + "\n</urlset>\n"
    )
    (ROOT / "sitemap.xml").write_text(body, encoding="utf-8", newline="\n")


def required_files(source: dict[str, Any]) -> dict[str, str]:
    files: dict[str, str] = {}
    for surface in SURFACES:
        for locale in source["official_locales"]:
            if blocked_reason(source, locale, surface):
                continue
            path = ROOT / locale_path(locale, surface)
            if path.is_file():
                files[path.relative_to(ROOT).as_posix()] = sha256_file(path)
    return dict(sorted(files.items()))


def build_manifest(source: dict[str, Any]) -> dict[str, Any]:
    required = required_files(source)
    optional = {
        relative.as_posix(): sha256_file(ROOT / relative)
        for relative in help_files(source).values()
    }
    blocked_count = sum(
        len(locales)
        for locales in (source.get("blocked_cells") or {}).values()
    )
    digest_input = "".join(
        f"{path}\0{digest}\n" for path, digest in sorted(required.items())
    ).encode("utf-8")
    manifest = {
        "schema": "support-surface-build-manifest/v1",
        "site_key": source["site_key"],
        "base_sha": source["base_sha"],
        "source_digest": source["source_digest"],
        "contract_digest": source["contract"]["digest"],
        "catalog_digest": source["contract"]["catalog_digest"],
        "official_locale_count": len(source["official_locales"]),
        "required_expected": len(source["official_locales"]) * len(SURFACES),
        "required_present": len(required),
        "required_blocked": blocked_count,
        "required_files": required,
        "optional_help_files": dict(sorted(optional.items())),
        "content_digest": sha256_bytes(digest_input),
    }
    BUILD_MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def build(source: dict[str, Any]) -> dict[str, Any]:
    apply_exact_replacements(source)
    remove_paid_upfront_faqs(source)
    replace_paid_privacy_copy(source)
    generate_managed_cells(source)
    for surface in SURFACES:
        for locale in source["official_locales"]:
            path = ROOT / locale_path(locale, surface)
            reason = blocked_reason(source, locale, surface)
            if reason:
                if path.exists():
                    raise ValueError(
                        f"blocked cell unexpectedly exists: {locale}/{surface}"
                    )
                continue
            if not path.is_file():
                raise ValueError(f"required page missing: {locale}/{surface}")
            patch_required_page(path, source, locale, surface)
    patch_help_pages(source)
    write_sitemap(source)
    return build_manifest(source)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        if tag in {"a", "link"} and values.get("href"):
            self.links.append(("href", values["href"] or ""))
        if tag in {"img", "script", "source"} and values.get("src"):
            self.links.append(("src", values["src"] or ""))


def page_prefix(markup: str) -> str:
    match = FAMILY_RE.search(markup)
    return markup[: match.start()] if match else markup


def meta_value(markup: str, name: str) -> list[str]:
    values = []
    for tag in re.findall(r"<meta\b[^>]*>", markup, re.I):
        if not re.search(
            rf"\bname=[\"']{re.escape(name)}[\"']", tag, re.I
        ):
            continue
        match = re.search(r"\bcontent=[\"']([^\"']*)[\"']", tag, re.I)
        if match:
            values.append(html.unescape(match.group(1)))
    return values


def tag_attribute_values(
    markup: str, tag_name: str, attribute: str
) -> list[str]:
    values = []
    for tag in re.findall(rf"<{tag_name}\b[^>]*>", markup, re.I):
        match = re.search(
            rf"\b{attribute}=[\"']([^\"']*)[\"']", tag, re.I
        )
        if match:
            values.append(html.unescape(match.group(1)))
    return values


def schema_blocks(markup: str) -> list[dict[str, Any]]:
    payloads = []
    for raw in re.findall(
        r"(?is)<script\b[^>]*\btype=[\"']application/ld\+json[\"'][^>]*>"
        r"(.*?)</script>",
        markup,
    ):
        payloads.append(json.loads(raw.replace("<\\/", "</")))
    return payloads


def validate_language_identity(
    locale: str, markup: str, relative: str, errors: list[str]
) -> None:
    script_ranges = {
        "ar-SA": r"[\u0600-\u06ff]",
        "bn-BD": r"[\u0980-\u09ff]",
        "zh-Hans": r"[\u3400-\u9fff]",
        "zh-Hant": r"[\u3400-\u9fff]",
        "el": r"[\u0370-\u03ff]",
        "gu-IN": r"[\u0a80-\u0aff]",
        "he": r"[\u0590-\u05ff]",
        "hi": r"[\u0900-\u097f]",
        "ja": r"[\u3040-\u30ff\u3400-\u9fff]",
        "kn-IN": r"[\u0c80-\u0cff]",
        "ko": r"[\uac00-\ud7af]",
        "ml-IN": r"[\u0d00-\u0d7f]",
        "mr-IN": r"[\u0900-\u097f]",
        "or-IN": r"[\u0b00-\u0b7f]",
        "pa-IN": r"[\u0a00-\u0a7f]",
        "ru": r"[\u0400-\u04ff]",
        "ta-IN": r"[\u0b80-\u0bff]",
        "te-IN": r"[\u0c00-\u0c7f]",
        "th": r"[\u0e00-\u0e7f]",
        "uk": r"[\u0400-\u04ff]",
        "ur-PK": r"[\u0600-\u06ff]",
    }
    pattern = script_ranges.get(locale)
    if not pattern:
        return
    visible = normalized_text(page_prefix(markup))
    if len(re.findall(pattern, visible)) < 8:
        errors.append(f"language: {relative} lacks expected {locale} script")


def resolve_local_link(
    source: dict[str, Any], page: Path, value: str
) -> Path | None:
    raw = html.unescape(value).strip()
    if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        site_path = "/" + source["site_key"] + "/"
        if parsed.netloc != "alice51849.github.io" or not parsed.path.startswith(
            site_path
        ):
            return None
        relative = unquote(parsed.path[len(site_path) :])
        target = ROOT / relative
    elif parsed.scheme:
        return None
    else:
        relative = unquote(parsed.path)
        if not relative:
            return None
        target = page.parent / relative
    if str(target).endswith("/"):
        target = target / "index.html"
    elif target.is_dir():
        target = target / "index.html"
    try:
        target.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return None
    return target


def validate(source: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    gate_counts = {
        "coverage": 0,
        "links": 0,
        "schema": 0,
        "language": 0,
        "contact": 0,
        "privacy": 0,
    }
    official = source["official_locales"]
    own_id = source["app"]["app_store_id"]
    dynamic_link_exempt = set(source.get("dynamic_app_link_exempt") or [])
    required_expected = len(official) * len(SURFACES)
    present = 0
    blocked = 0

    for surface in SURFACES:
        expected_alternates = set(available_locales(source, surface))
        for locale in official:
            relative_path = locale_path(locale, surface)
            relative = relative_path.as_posix()
            path = ROOT / relative_path
            reason = blocked_reason(source, locale, surface)
            if reason:
                blocked += 1
                if path.exists():
                    errors.append(f"coverage: blocked page exists: {relative}")
                continue
            if not path.is_file():
                errors.append(f"coverage: missing {relative}")
                continue
            present += 1
            gate_counts["coverage"] += 1
            try:
                markup = path.read_text(encoding="utf-8", errors="strict")
            except UnicodeError as error:
                errors.append(f"language: {relative}: invalid UTF-8: {error}")
                continue
            html_lang = tag_attribute_values(markup, "html", "lang")
            html_dir = tag_attribute_values(markup, "html", "dir")
            wanted_dir = "rtl" if locale in RTL_LOCALES else "ltr"
            if html_lang != [locale]:
                errors.append(
                    f"language: {relative}: html lang {html_lang!r}, expected {locale}"
                )
            if html_dir != [wanted_dir]:
                errors.append(
                    f"language: {relative}: html dir {html_dir!r}, expected {wanted_dir}"
                )
            title = extract_title(markup)
            description = extract_description(markup)
            if not title or not description:
                errors.append(f"language: {relative}: missing title/description")
            if RAW_KEY_RE.search(normalized_text(page_prefix(markup))):
                errors.append(f"language: {relative}: raw key or placeholder visible")
            validate_language_identity(locale, markup, relative, errors)
            gate_counts["language"] += 1

            canonical_values = []
            alternate_values: dict[str, list[str]] = {}
            for tag in re.findall(r"<link\b[^>]*>", markup, re.I):
                rel_match = re.search(r"\brel=[\"']([^\"']+)[\"']", tag, re.I)
                href_match = re.search(r"\bhref=[\"']([^\"']+)[\"']", tag, re.I)
                if not rel_match or not href_match:
                    continue
                rel = rel_match.group(1).lower().split()
                if "canonical" in rel:
                    canonical_values.append(html.unescape(href_match.group(1)))
                if "alternate" in rel:
                    lang_match = re.search(
                        r"\bhreflang=[\"']([^\"']+)[\"']", tag, re.I
                    )
                    if lang_match:
                        alternate_values.setdefault(
                            lang_match.group(1), []
                        ).append(html.unescape(href_match.group(1)))
            wanted_canonical = canonical_for(source, locale, surface)
            if canonical_values != [wanted_canonical]:
                errors.append(
                    f"links: {relative}: canonical {canonical_values!r}, "
                    f"expected {wanted_canonical}"
                )
            if set(alternate_values) != expected_alternates | {"x-default"}:
                errors.append(
                    f"links: {relative}: hreflang set is not authoritative"
                )
            for lang, values in alternate_values.items():
                if len(values) != 1:
                    errors.append(
                        f"links: {relative}: duplicate hreflang {lang}"
                    )
            gate_counts["links"] += 1

            try:
                schemas = schema_blocks(markup)
            except (ValueError, TypeError) as error:
                errors.append(f"schema: {relative}: invalid JSON-LD: {error}")
                schemas = []
            owned = [
                item
                for item in schemas
                if item.get("@type") == "WebPage"
                and item.get("url") == wanted_canonical
            ]
            if len(owned) != 1:
                errors.append(f"schema: {relative}: missing unique surface schema")
            else:
                schema = owned[0]
                about = schema.get("about") or {}
                if schema.get("inLanguage") != locale:
                    errors.append(f"schema: {relative}: wrong inLanguage")
                if str(about.get("identifier")) != own_id:
                    errors.append(f"schema: {relative}: wrong App Store identifier")
                download = str(about.get("downloadUrl") or "")
                if f"id{own_id}" not in download:
                    errors.append(f"schema: {relative}: wrong downloadUrl")
                prop = about.get("additionalProperty") or {}
                if prop.get("value") != public_purchase_model(source):
                    errors.append(f"schema: {relative}: wrong purchase model")
            gate_counts["schema"] += 1

            prefix = page_prefix(markup)
            if surface in {"index", "support"} and relative not in dynamic_link_exempt:
                ids = set(APP_ID_RE.findall(prefix))
                if own_id not in ids:
                    errors.append(f"links: {relative}: own App Store CTA missing")
                if ids - {own_id}:
                    errors.append(
                        f"links: {relative}: cross-App ID before family module"
                    )
            emails = set(EMAIL_RE.findall(markup))
            if emails - {EMAIL}:
                errors.append(f"contact: {relative}: non-public email {sorted(emails)}")
            if surface == "support":
                visible = normalized_text(prefix)
                if len(visible) < 80 or EMAIL not in visible:
                    errors.append(
                        f"contact: {relative}: support equivalence gate failed"
                    )
                gate_counts["contact"] += 1
            if surface == "privacy":
                authority = meta_value(markup, "privacy-authority-digest")
                if authority != [source["privacy_authority"]["digest"]]:
                    errors.append(
                        f"privacy: {relative}: authority digest mismatch"
                    )
                purchase = meta_value(markup, "app-purchase-model")
                if purchase != [public_purchase_model(source)]:
                    errors.append(
                        f"privacy: {relative}: purchase model metadata mismatch"
                    )
                gate_counts["privacy"] += 1
            if "free_with_lifetime_unlock" in markup:
                errors.append(
                    f"privacy: {relative}: internal lifetime enum leaked publicly"
                )

    for page in all_html_files():
        relative = page.relative_to(ROOT).as_posix()
        markup = page.read_text(encoding="utf-8", errors="strict")
        parser = LinkParser()
        parser.feed(markup)
        for kind, value in parser.links:
            target = resolve_local_link(source, page, value)
            if target is not None and not target.exists():
                errors.append(
                    f"links: {relative}: broken {kind} {value!r}"
                )
        for payload in re.findall(
            r"(?is)<script\b[^>]*\btype=[\"']application/ld\+json[\"'][^>]*>"
            r"(.*?)</script>",
            markup,
        ):
            try:
                json.loads(payload.replace("<\\/", "</"))
            except (ValueError, TypeError) as error:
                errors.append(f"schema: {relative}: invalid JSON-LD: {error}")
        emails = set(EMAIL_RE.findall(markup))
        if emails - {EMAIL}:
            errors.append(f"contact: {relative}: non-public email {sorted(emails)}")

    if source["app"]["purchase_model"] == "paid_upfront":
        questions = source.get("paid_cleanup_questions") or {}
        for locale in official:
            path = ROOT / locale_path(locale, "support")
            if not path.is_file():
                continue
            visible = normalized_text(page_prefix(path.read_text(encoding="utf-8"))).casefold()
            for question in questions.get(locale, []):
                if normalized_text(question).casefold() in visible:
                    errors.append(
                        f"privacy: {path.relative_to(ROOT)}: free/IAP FAQ remains"
                    )
        for path in all_html_files():
            prefix = normalized_text(page_prefix(path.read_text(encoding="utf-8")))
            if re.search(r"\bUS\$\s*(?:4\.99|5\.99)\b", prefix):
                errors.append(
                    f"privacy: {path.relative_to(ROOT)}: uncontracted fixed price remains"
                )
    elif source["app"]["app_key"] == "scanto":
        for relative in ("index.html", "privacy.html"):
            text = normalized_text(
                page_prefix((ROOT / relative).read_text(encoding="utf-8"))
            )
            if re.search(
                r"\bpaid(?:,| )+one-time|no in-app purchases|every feature is included",
                text,
                re.I,
            ):
                errors.append(
                    f"privacy: {relative}: stale paid-upfront ScanTo claim remains"
                )

    help = help_files(source)
    optional_present = len(help)
    optional_equivalent = 0
    for locale in official:
        if locale in help:
            continue
        path = ROOT / locale_path(locale, "support")
        if path.is_file():
            visible = normalized_text(page_prefix(path.read_text(encoding="utf-8")))
            if len(visible) >= 80 and EMAIL in visible:
                optional_equivalent += 1
            else:
                errors.append(
                    f"contact: {locale}: optional help equivalent gate failed"
                )

    if present + blocked != required_expected:
        errors.append(
            f"coverage: accounting mismatch {present}+{blocked}!={required_expected}"
        )
    manifest = json.loads(BUILD_MANIFEST_PATH.read_text(encoding="utf-8"))
    current = required_files(source)
    if manifest.get("required_files") != current:
        errors.append("coverage: build manifest hashes are stale")

    categories = {name: 0 for name in gate_counts}
    for error in errors:
        category = error.split(":", 1)[0]
        categories[category] = categories.get(category, 0) + 1
    return {
        "site_key": source["site_key"],
        "status": "passed" if not errors else "failed",
        "required_expected": required_expected,
        "required_present": present,
        "required_blocked": blocked,
        "optional_help_present": optional_present,
        "optional_help_equivalent": optional_equivalent,
        "gate_checks": gate_counts,
        "error_counts": categories,
        "errors": errors,
        "content_digest": manifest["content_digest"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    source = load_source()
    if args.test:
        result = validate(source)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(
                f'{result["site_key"]}: {result["status"]}; '
                f'required={result["required_present"]}/'
                f'{result["required_expected"]}; '
                f'blocked={result["required_blocked"]}; '
                f'digest={result["content_digest"]}'
            )
            for error in result["errors"][:40]:
                print(error, file=sys.stderr)
        return 0 if result["status"] == "passed" else 1
    manifest = build(source)
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f'{manifest["site_key"]}: required='
            f'{manifest["required_present"]}/{manifest["required_expected"]}; '
            f'blocked={manifest["required_blocked"]}; '
            f'digest={manifest["content_digest"]}'
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
