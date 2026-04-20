"""HTTP clients for Arc Raiders data APIs with in-memory caching."""

import asyncio
import re
import time
from typing import Any

import httpx

ARCDATA_BASE = "https://arcdata.mahcks.com/v1"
ARDB_BASE = "https://ardb.app/api"
METAFORGE_BASE = "https://metaforge.app/api/arc-raiders"
WIKI_API = "https://arcraiders.wiki/w/api.php"
RAIDTHEORY_BASE = "https://raw.githubusercontent.com/RaidTheory/arcraiders-data/main"
ENEMY_CATEGORY = "ARC"
CACHE_TTL = 3600  # 1 hour

_cache: dict[str, tuple[Any, float]] = {}

# Full item catalog for reverse lookups (built lazily)
_item_catalog: dict[str, dict] | None = None
_catalog_lock = asyncio.Lock()

# MetaForge item catalog (built lazily, keyed by dash-format id)
_metaforge_catalog: dict[str, dict] | None = None
_metaforge_lock = asyncio.Lock()


async def _get(url: str) -> Any:
    now = time.monotonic()
    if url in _cache:
        data, ts = _cache[url]
        if now - ts < CACHE_TTL:
            return data
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    _cache[url] = (data, now)
    return data


# ---------------------------------------------------------------------------
# arcdata.mahcks.com
# ---------------------------------------------------------------------------

async def arcdata_item(item_id: str) -> dict | None:
    try:
        return await _get(f"{ARCDATA_BASE}/items/{item_id}")
    except Exception:
        return None


async def arcdata_items_list() -> list[dict]:
    try:
        data = await _get(f"{ARCDATA_BASE}/items")
        if isinstance(data, list):
            return data
        return data.get("items", [])
    except Exception:
        return []


async def arcdata_quest(quest_id: str) -> dict | None:
    try:
        return await _get(f"{ARCDATA_BASE}/quests/{quest_id}")
    except Exception:
        return None


async def arcdata_quests_list() -> list[dict]:
    try:
        data = await _get(f"{ARCDATA_BASE}/quests")
        if isinstance(data, list):
            return data
        return data.get("items", data.get("quests", []))
    except Exception:
        return []


async def arcdata_bots() -> list[dict]:
    try:
        return await _get(f"{ARCDATA_BASE}/bots")
    except Exception:
        return []


async def arcdata_trades() -> list[dict]:
    try:
        return await _get(f"{ARCDATA_BASE}/trades")
    except Exception:
        return []


async def arcdata_hideout_list() -> list[dict]:
    try:
        data = await _get(f"{ARCDATA_BASE}/hideout")
        if isinstance(data, list):
            return data
        for key in ("items", "modules", "hideout"):
            if key in data:
                return data[key]
        return []
    except Exception:
        return []


async def arcdata_hideout(module_id: str) -> dict | None:
    try:
        return await _get(f"{ARCDATA_BASE}/hideout/{module_id}")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ardb.app
# ---------------------------------------------------------------------------

async def ardb_items() -> list[dict]:
    """Returns all items with names (for search)."""
    try:
        return await _get(f"{ARDB_BASE}/items")
    except Exception:
        return []


async def ardb_item(item_id: str) -> dict | None:
    try:
        return await _get(f"{ARDB_BASE}/items/{item_id}")
    except Exception:
        return None


async def ardb_enemies() -> list[dict]:
    try:
        return await _get(f"{ARDB_BASE}/arc-enemies")
    except Exception:
        return []


async def ardb_enemy(enemy_id: str) -> dict | None:
    try:
        return await _get(f"{ARDB_BASE}/arc-enemies/{enemy_id}")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Name search utilities
# ---------------------------------------------------------------------------

def name_en(item: dict) -> str:
    name = item.get("name", "")
    if isinstance(name, dict):
        return name.get("en", "")
    return str(name)


def find_best_match(query: str, items: list[dict]) -> dict | None:
    """Find the single best matching item by name (case-insensitive)."""
    q = query.lower().strip()

    # 1. Exact match
    for item in items:
        if name_en(item).lower() == q:
            return item

    # 2. Starts with
    starts = [i for i in items if name_en(i).lower().startswith(q)]
    if starts:
        return starts[0]

    # 3. Substring
    subs = [i for i in items if q in name_en(i).lower()]
    if subs:
        return subs[0]

    return None


def find_all_matches(query: str, items: list[dict]) -> list[dict]:
    """Find all items whose name contains the query string (case-insensitive)."""
    q = query.lower().strip()
    return [i for i in items if q in name_en(i).lower()]


# ---------------------------------------------------------------------------
# Full item catalog (for find_uses_for_item reverse lookup)
# ---------------------------------------------------------------------------

async def build_item_catalog() -> dict[str, dict]:
    """
    Fetch all item details from arcdata. Cached for the session lifetime.
    First call may take 5-15 seconds depending on API latency.
    """
    global _item_catalog

    async with _catalog_lock:
        if _item_catalog is not None:
            return _item_catalog

        stubs = await arcdata_items_list()
        item_ids = [s["id"] for s in stubs if "id" in s]

        # Fetch with bounded concurrency (20 at a time) to avoid overwhelming the API
        semaphore = asyncio.Semaphore(20)

        async def fetch_one(iid: str) -> tuple[str, dict | None]:
            async with semaphore:
                return iid, await arcdata_item(iid)

        results = await asyncio.gather(*[fetch_one(iid) for iid in item_ids])
        _item_catalog = {iid: data for iid, data in results if data}

    return _item_catalog


# ---------------------------------------------------------------------------
# MetaForge catalog (weapon numeric stats)
# ---------------------------------------------------------------------------

def _to_metaforge_id(item_id: str) -> str:
    """Convert underscore item IDs to MetaForge dash format (stitcher_i -> stitcher-i)."""
    return item_id.replace("_", "-")


async def metaforge_item(item_id: str) -> dict | None:
    """Look up a single item in the MetaForge catalog by arcdata-style ID."""
    catalog = await build_metaforge_catalog()
    mf_id = _to_metaforge_id(item_id)
    return catalog.get(mf_id)


async def build_metaforge_catalog() -> dict[str, dict]:
    """
    Fetch all MetaForge items (paginated). Cached for the session lifetime.
    MetaForge has no per-item endpoint so we must load all pages upfront.
    Response shape: {data: [...], pagination: {totalPages, ...}}
    """
    global _metaforge_catalog

    async with _metaforge_lock:
        if _metaforge_catalog is not None:
            return _metaforge_catalog

        # Fetch first page to get total page count
        try:
            first = await _get(f"{METAFORGE_BASE}/items?page=1&limit=50")
        except Exception:
            _metaforge_catalog = {}
            return _metaforge_catalog

        total_pages = first.get("pagination", {}).get("totalPages", 1)

        # Fetch remaining pages in parallel
        pages = [first] + list(await asyncio.gather(*[
            _get(f"{METAFORGE_BASE}/items?page={p}&limit=50")
            for p in range(2, total_pages + 1)
        ]))

        catalog = {}
        for page_data in pages:
            for item in page_data.get("data", []):
                iid = item.get("id", "")
                if iid:
                    catalog[iid] = item

        _metaforge_catalog = catalog

    return _metaforge_catalog


# Base weapon stat fields (always shown when non-zero/non-empty)
WEAPON_STAT_FIELDS = [
    ("damage", "Damage"),
    ("fireRate", "Fire Rate"),
    ("range", "Range"),
    ("stability", "Stability"),
    ("agility", "Agility"),
    ("stealth", "Stealth"),
    ("magazineSize", "Magazine Size"),
    ("firingMode", "Firing Mode"),
    ("ammo", "Ammo Type"),
]

# Modifier fields shown as "X% Label" — only displayed when non-zero
WEAPON_MODIFIER_FIELDS = [
    ("reducedReloadTime", "Reduced Reload Time"),
    ("reducedVerticalRecoil", "Reduced Vertical Recoil"),
    ("increasedVerticalRecoil", "Increased Vertical Recoil"),
    ("reducedRecoilRecoveryTime", "Reduced Recoil Recovery Time"),
    ("increasedRecoilRecoveryTime", "Increased Recoil Recovery Time"),
    ("reducedMaxShotDispersion", "Reduced Max Shot Dispersion"),
    ("reducedPerShotDispersion", "Reduced Per-Shot Dispersion"),
    ("reducedDispersionRecoveryTime", "Reduced Dispersion Recovery Time"),
    ("increasedFireRate", "Increased Fire Rate"),
    ("increasedBulletVelocity", "Increased Bullet Velocity"),
    ("reducedDurabilityBurnRate", "Reduced Durability Burn Rate"),
    ("reducedNoise", "Reduced Noise"),
    ("damageMult", "Damage Multiplier"),
    ("movementPenalty", "Movement Penalty"),
    ("increasedADSSpeed", "Increased ADS Speed"),
    ("reducedEquipTime", "Reduced Equip Time"),
    ("reducedUnequipTime", "Reduced Unequip Time"),
    ("increasedUnequipTime", "Increased Unequip Time"),
]


# ---------------------------------------------------------------------------
# Wiki enemy catalog (health, armor, attack type from arcraiders.wiki)
# ---------------------------------------------------------------------------

_wiki_enemy_catalog: dict[str, dict] | None = None
_wiki_enemy_lock = asyncio.Lock()


def _parse_infobox(wikitext: str) -> dict:
    """Extract key-value pairs from any {{Infobox ...}} template."""
    match = re.search(r"\{\{Infobox\s+\w+(.*?)\}\}", wikitext, re.DOTALL | re.IGNORECASE)
    if not match:
        return {}
    result = {}
    for line in match.group(1).split("\n"):
        if "|" in line and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip().lstrip("|").strip()
            value = re.sub(r"<br\s*/?>", " | ", value)       # <br> -> separator
            value = re.sub(r"<!--.*?-->", "", value)          # strip HTML comments
            value = re.sub(r"<[^>]+>", "", value).strip()    # strip remaining HTML tags
            if key and value:
                result[key] = value
    return result


def _parse_wiki_section(wikitext: str, section_name: str) -> str:
    """
    Extract the text content of a named == Section == from wikitext.
    Strips wiki markup: [[links]], '''bold''', ''italic'', templates, HTML tags.
    """
    pattern = rf"==\s*{re.escape(section_name)}\s*==\s*(.*?)(?=\n==|\Z)"
    match = re.search(pattern, wikitext, re.DOTALL | re.IGNORECASE)
    if not match:
        return ""
    raw = match.group(1).strip()

    # Strip wiki markup
    raw = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", raw)  # [[Link|Text]] -> Text
    raw = re.sub(r"'{2,3}", "", raw)                               # bold/italic markers
    raw = re.sub(r"\{\{[^}]*\}\}", "", raw)                        # templates {{...}}
    raw = re.sub(r"<[^>]+>", "", raw)                              # HTML tags
    raw = re.sub(r"\[\[|\]\]", "", raw)                            # leftover brackets
    raw = re.sub(r"^[*#]\s*", "- ", raw, flags=re.MULTILINE)      # bullets -> dashes
    raw = re.sub(r"\n{3,}", "\n\n", raw)                           # collapse blank lines
    return raw.strip()


async def _fetch_wiki_enemy(page_title: str) -> dict | None:
    """Fetch and parse infobox + combat sections for one wiki enemy/weapon page."""
    try:
        url = (
            f"{WIKI_API}?action=parse&page={page_title.replace(' ', '%20')}"
            f"&prop=wikitext&format=json"
        )
        data = await _get(url)
        wikitext = data.get("parse", {}).get("wikitext", {}).get("*", "")
        if not wikitext:
            return None
        info = _parse_infobox(wikitext)
        if not info:
            return None
        info["_page"] = page_title

        # Extract combat-relevant prose sections
        for section in ("Behavior", "Combat tips", "Combat Tips"):
            content = _parse_wiki_section(wikitext, section)
            if content:
                info[f"_section_{section.lower().replace(' ', '_')}"] = content

        return info
    except Exception:
        return None


async def build_wiki_enemy_catalog() -> dict[str, dict]:
    """
    Fetch all enemy pages from Category:ARC on arcraiders.wiki.
    Returns a dict keyed by lowercase name for easy lookup.
    """
    global _wiki_enemy_catalog

    async with _wiki_enemy_lock:
        if _wiki_enemy_catalog is not None:
            return _wiki_enemy_catalog

        # Get all pages in the ARC category
        try:
            data = await _get(
                f"{WIKI_API}?action=query&list=categorymembers"
                f"&cmtitle=Category:{ENEMY_CATEGORY}&format=json&cmlimit=100"
            )
            members = data.get("query", {}).get("categorymembers", [])
        except Exception:
            _wiki_enemy_catalog = {}
            return _wiki_enemy_catalog

        # Filter out sub-categories
        page_titles = [m["title"] for m in members if m.get("ns", 0) == 0]

        # Fetch all pages in parallel
        semaphore = asyncio.Semaphore(10)

        async def fetch_one(title: str) -> tuple[str, dict | None]:
            async with semaphore:
                return title, await _fetch_wiki_enemy(title)

        results = await asyncio.gather(*[fetch_one(t) for t in page_titles])

        catalog = {}
        for title, info in results:
            if info:
                # Key by lowercase name for fuzzy matching
                key = info.get("name", title).lower()
                catalog[key] = info
                # Also key by page title (e.g. "ARC Orbiter")
                catalog[title.lower()] = info

        _wiki_enemy_catalog = catalog

    return _wiki_enemy_catalog


async def wiki_enemy(name: str) -> dict | None:
    """Look up wiki enemy data by name (case-insensitive)."""
    catalog = await build_wiki_enemy_catalog()
    return catalog.get(name.lower())


_TIER_SUFFIX = re.compile(r"\s+(?:I{1,3}V?|VI{0,3}|IV|IX|X)$")


async def wiki_weapon(name: str) -> dict | None:
    """
    Look up wiki weapon data by display name.
    Strips tier suffix before fetching (e.g. 'Anvil II' -> fetches 'Anvil' page).
    Returns parsed infobox fields including headshotmultiplier.
    """
    base_name = _TIER_SUFFIX.sub("", name).strip()
    return await _fetch_wiki_enemy(base_name)


# ---------------------------------------------------------------------------
# Quest search helper (fetches all 94 quests to search by name)
# ---------------------------------------------------------------------------

_quests_by_name: dict[str, dict] | None = None
_quests_lock = asyncio.Lock()


async def get_all_quests() -> dict[str, dict]:
    """Returns a dict of quest_id -> quest_data, fetched and cached."""
    global _quests_by_name

    async with _quests_lock:
        if _quests_by_name is not None:
            return _quests_by_name

        stubs = await arcdata_quests_list()
        quest_ids = [s["id"] for s in stubs if "id" in s]

        semaphore = asyncio.Semaphore(20)

        async def fetch_one(qid: str) -> tuple[str, dict | None]:
            async with semaphore:
                return qid, await arcdata_quest(qid)

        results = await asyncio.gather(*[fetch_one(qid) for qid in quest_ids])
        _quests_by_name = {qid: data for qid, data in results if data}

    return _quests_by_name


# ---------------------------------------------------------------------------
# RaidTheory skill nodes
# ---------------------------------------------------------------------------

async def raidtheory_skill_nodes() -> list[dict]:
    """Fetch all 45 skill nodes from RaidTheory/arcraiders-data."""
    try:
        data = await _get(f"{RAIDTHEORY_BASE}/skillNodes.json")
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def raidtheory_map_events() -> dict:
    """Fetch map event schedule from RaidTheory/arcraiders-data."""
    try:
        return await _get(f"{RAIDTHEORY_BASE}/map-events/map-events.json")
    except Exception:
        return {}
