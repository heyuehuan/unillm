"""
The documentation wiki: markdown on disk, served to the console.

Covers the loader (front matter, slug from filename, ordering, bad input) and the
two routes that expose it: GET /api/documentation and the /documentation redirect
that turns a shareable URL into the console's hash route.
"""
import pytest
from fastapi.testclient import TestClient

from unillm.proxy import docs_pages
from unillm.proxy.proxy_server import app
from unillm.db.database import init_db, engine, SessionLocal
from unillm.db.models import Base
from unillm.db import crud
from unillm.proxy.api_routes import hash_password


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    session = SessionLocal()
    crud.create_user(db=session, username="docsreader",
                     hashed_password=hash_password("docspass123"), global_role="user")
    session.close()
    r = client.post("/api/auth/login", json={"username": "docsreader", "password": "docspass123"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


# ── Loader ────────────────────────────────────────────────

def test_shipped_pages_load():
    pages = docs_pages.reload()
    assert pages, "unillm/documentation should ship with pages"
    slugs = [p["slug"] for p in pages]
    assert "quickstart" in slugs
    assert "credits" in slugs
    # The numeric prefix orders the pages and is stripped from the slug.
    assert not any(s[0].isdigit() for s in slugs)
    assert slugs.index("quickstart") < slugs.index("credits")


def test_every_page_has_metadata_and_body():
    for page in docs_pages.load_pages():
        assert page["title"] and page["group"]
        assert page["body"].strip()
        # Front matter must not leak into the rendered body.
        assert not page["body"].lstrip().startswith("---")
        assert isinstance(page["keywords"], list)


def test_front_matter_parsing(tmp_path):
    (tmp_path / "020-second.md").write_text(
        "---\ntitle: Second\ngroup: Later\nkeywords: [a, b]\n---\n\nBody two.\n")
    (tmp_path / "010-first.md").write_text("---\ntitle: First\n---\n\nBody one.\n")
    # No front matter at all: the filename still has to produce a usable page.
    (tmp_path / "030-plain-page.md").write_text("Just prose.\n")

    pages = docs_pages._read_dir(str(tmp_path))
    assert [p["slug"] for p in pages] == ["first", "second", "plain-page"]
    assert pages[0]["group"] == "Documentation"      # default group
    assert pages[1]["keywords"] == ["a", "b"]
    assert pages[2]["title"] == "Plain page"          # derived from the filename
    assert pages[2]["body"].strip() == "Just prose."


def test_malformed_front_matter_does_not_break_the_wiki(tmp_path):
    (tmp_path / "010-ok.md").write_text("---\ntitle: Fine\n---\n\nGood.\n")
    (tmp_path / "020-broken.md").write_text("---\ntitle: [unclosed\n---\n\nStill served.\n")
    pages = docs_pages._read_dir(str(tmp_path))
    assert len(pages) == 2
    assert pages[1]["body"].strip() == "Still served."


def test_missing_directory_is_empty_not_fatal(tmp_path):
    assert docs_pages._read_dir(str(tmp_path / "nope")) == []


# ── Routes ────────────────────────────────────────────────

def test_documentation_requires_authentication(client):
    assert client.get("/api/documentation").status_code in (401, 403)


def test_documentation_returns_all_pages(client, token):
    r = client.get("/api/documentation", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert len(body) == len(docs_pages.load_pages())
    assert set(body[0]) == {"slug", "title", "group", "keywords", "body"}


def test_documentation_url_redirects_into_the_console(client):
    r = client.get("/documentation", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/#/documentation"

    r = client.get("/documentation/quickstart", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/#/documentation/quickstart"


# ── Settings-aware pages ──────────────────────────────────

def test_tokens_and_conditionals_resolve():
    text = (
        "Mode is {{ssh_mode}}.\n"
        "{{#if ssh_mode=enforce}}\n"
        "Signing required.\n"
        "{{/if}}\n"
        "{{#if ssh_mode=none,warning}}\n"
        "Signing optional.\n"
        "{{/if}}\n"
        "{{#if ssh_mode!=none}}\n"
        "Not the default.\n"
        "{{/if}}\n"
    )
    out = docs_pages.render(text, {"ssh_mode": "enforce"})
    assert "Mode is enforce." in out
    assert "Signing required." in out
    assert "Not the default." in out
    assert "Signing optional." not in out
    # Markers themselves never reach the markdown renderer.
    assert "{{" not in out

    out = docs_pages.render(text, {"ssh_mode": "none"})
    assert "Signing optional." in out
    assert "Signing required." not in out
    assert "Not the default." not in out


def test_fenced_code_is_left_alone():
    text = "Live: {{ssh_mode}}\n\n```\n{{#if ssh_mode=none}}\nexample {{ssh_mode}}\n{{/if}}\n```\n"
    out = docs_pages.render(text, {"ssh_mode": "enforce"})
    assert "Live: enforce" in out
    # The page documenting the syntax has to be able to show it.
    assert "{{#if ssh_mode=none}}" in out
    assert "example {{ssh_mode}}" in out


def test_unknown_name_stays_visible():
    assert docs_pages.render("{{nope}}", {"ssh_mode": "none"}) == "{{nope}}"
    # An unknown name in a condition drops the block rather than guessing.
    assert docs_pages.render("{{#if nope=yes}}\nhidden\n{{/if}}\n", {}) == ""


def test_load_pages_leaves_the_cache_raw():
    docs_pages.reload()
    rendered = docs_pages.load_pages({"ssh_mode": "enforce"})
    ssh = next(p for p in rendered if p["slug"] == "ssh-keys")
    assert "`enforce` mode" in ssh["body"]

    raw = next(p for p in docs_pages.load_pages() if p["slug"] == "ssh-keys")
    assert "{{#if ssh_mode=enforce}}" in raw["body"]


def test_ssh_page_reflects_the_server_mode(client, token):
    from unillm.proxy.auth import set_general_settings, get_general_settings

    before = get_general_settings()
    try:
        for mode in ("none", "warning", "enforce"):
            set_general_settings({"ssh_required": mode})
            r = client.get("/api/documentation", headers=_auth(token))
            body = next(p for p in r.json() if p["slug"] == "ssh-keys")["body"]
            assert f"This server runs in `{mode}` mode" in body
            for other in {"none", "warning", "enforce"} - {mode}:
                assert f"This server runs in `{other}` mode" not in body
    finally:
        set_general_settings(before)


def test_body_keeps_dashes_that_are_not_front_matter(tmp_path):
    # The maintainer page shows an example of front matter, and a page may use '---'
    # as a horizontal rule. Neither may end the body early.
    (tmp_path / "010-doc.md").write_text(
        "---\ntitle: Doc\n---\n\nIntro.\n\n```\n---\ntitle: Example\n---\n```\n\n---\n\nTail.\n")
    page = docs_pages._read_dir(str(tmp_path))[0]
    assert page["title"] == "Doc"
    assert page["body"].startswith("Intro.")
    assert "title: Example" in page["body"]
    assert page["body"].rstrip().endswith("Tail.")


def test_shipped_pages_are_not_truncated():
    import os

    directory = os.path.abspath(docs_pages.DOCS_DIR)
    pages = {p["slug"]: p for p in docs_pages.reload()}
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".md"):
            continue
        slug = docs_pages._PREFIX.sub("", os.path.splitext(filename)[0])
        with open(os.path.join(directory, filename), encoding="utf-8") as f:
            raw = f.read()
        # A page ends where its file ends, never mid-document on a stray '---'.
        assert pages[slug]["body"].rstrip().endswith(raw.rstrip()[-60:])
