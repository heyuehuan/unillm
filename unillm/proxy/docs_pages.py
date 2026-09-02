"""
The documentation wiki, loaded from markdown on disk.

The pages under `/documentation` in the console are plain markdown files in
`unillm/documentation/`, not JSX. Writing docs should not mean editing React and
running a frontend build: an operator or a maintainer edits a file, restarts the
server, and the console shows the new text.

One file is one page. The filename carries both the ordering and the URL:

    130-dev-codebase.md  ->  slug "dev-codebase", sorted by the 130

Front matter gives the rest:

    ---
    title: Codebase and maintenance
    group: Developer guide
    keywords: [layout, tests, migrations]
    ---

Files are read once and cached, because they only change when the deployment is
redeployed. `reload()` exists for tests.

A page can also say something different depending on how the server is configured,
so that the docs describe *this* deployment instead of every possible one:

    {{ssh_mode}}                     inserts the value

    {{#if ssh_mode=enforce}}         keeps the block only in enforce mode
    Unsigned requests are rejected.
    {{/if}}

`context()` builds the values. Substitution happens per request on a copy, because
the cached page text has to stay the raw file. Fenced code is skipped, blocks do not
nest, and an unknown name leaves `{{...}}` visible rather than hiding the mistake.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import yaml

from unillm._logging import verbose_proxy_logger

DOCS_DIR = os.path.join(os.path.dirname(__file__), "..", "documentation")

# "130-dev-codebase" -> "dev-codebase". A file with no numeric prefix keeps its
# whole name as the slug, so both conventions work.
_PREFIX = re.compile(r"^\d+[-_]")

_cache: Optional[List[Dict[str, Any]]] = None


def _split_front_matter(text: str) -> tuple[Dict[str, Any], str]:
    """
    Return (metadata, body). A file without front matter is all body.

    Malformed YAML is reported and treated as absent rather than raising: one bad
    page should not take the whole wiki (and with it the console) down.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    # The closing delimiter is the next line that is exactly '---'. Splitting on the
    # substring instead would end the body at the first '---' anywhere below, and the
    # page that shows an example of front matter would lose everything after it.
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    raw_meta = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    try:
        meta = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError as e:
        verbose_proxy_logger.warning(f"Ignoring malformed doc front matter: {e}")
        return {}, body
    return (meta if isinstance(meta, dict) else {}), body


def _title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").capitalize()


def _read_dir(directory: str) -> List[Dict[str, Any]]:
    if not os.path.isdir(directory):
        verbose_proxy_logger.warning(f"Documentation directory not found: {directory}")
        return []

    pages: List[Dict[str, Any]] = []
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            verbose_proxy_logger.warning(f"Could not read doc page {filename}: {e}")
            continue

        meta, body = _split_front_matter(text)
        slug = _PREFIX.sub("", os.path.splitext(filename)[0])
        keywords = meta.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",") if k.strip()]

        pages.append({
            "slug": slug,
            "title": str(meta.get("title") or _title_from_slug(slug)),
            "group": str(meta.get("group") or "Documentation"),
            "keywords": [str(k) for k in keywords],
            "body": body,
        })
    return pages


_TOKEN = re.compile(r"\{\{ *([a-z0-9_]+) *\}\}")

# The markers each own a line, so they never end up inside the markdown the renderer
# sees, and a block can wrap anything, including a fenced example.
_IF_OPEN = re.compile(r"^\{\{#if +([a-z0-9_]+) *(!?=) *([^}\n]*)\}\}[ \t]*$")
_IF_CLOSE = re.compile(r"^\{\{/if\}\}[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


def render(text: str, values: Dict[str, str]) -> str:
    """
    Resolve `{{#if}}` blocks and `{{name}}` tokens.

    Fenced code is left alone, so the page that documents this syntax can show it.
    """
    out: List[str] = []
    in_fence = False
    keeping = True

    for line in text.split("\n"):
        if _FENCE.match(line):
            # Toggled even inside a dropped block, so the fence state stays honest.
            in_fence = not in_fence
            if keeping:
                out.append(line)
            continue

        if not in_fence:
            opened = _IF_OPEN.match(line)
            if opened:
                name, operator, wanted = opened.groups()
                options = [w.strip() for w in wanted.split(",") if w.strip()]
                hit = str(values.get(name, "")) in options
                keeping = hit if operator == "=" else not hit
                continue
            if _IF_CLOSE.match(line):
                keeping = True
                continue

        if not keeping:
            continue
        if in_fence:
            out.append(line)
        else:
            out.append(_TOKEN.sub(lambda m: str(values.get(m.group(1), m.group(0))), line))

    return "\n".join(out)


def context() -> Dict[str, str]:
    """
    The deployment facts pages are allowed to talk about.

    Read at request time, not at import, so a page reflects the config the server
    is actually running with.
    """
    from unillm.proxy.auth import get_general_settings
    from unillm.proxy.ssh_auth import get_ssh_mode
    from unillm.proxy.gcp_adk import load_config

    return {
        "ssh_mode": get_ssh_mode(get_general_settings()),
        "gcp_adk": "enabled" if load_config().enabled else "disabled",
    }


def load_pages(values: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """
    All documentation pages, in file order. Read from disk once per process.

    With `values`, each body comes back rendered against them; without, raw.
    """
    global _cache
    if _cache is None:
        _cache = _read_dir(os.path.abspath(DOCS_DIR))
    if not values:
        return _cache
    return [dict(page, body=render(page["body"], values)) for page in _cache]


def reload() -> List[Dict[str, Any]]:
    """Drop the cache and re-read the directory."""
    global _cache
    _cache = None
    return load_pages()
