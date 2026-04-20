# Arc Raiders MCP

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that gives Claude tools to look up Arc Raiders game data: items, crafting recipes, quests, enemies, hideout modules, and trader inventories.

> **Heads up:** This project started as a learning exercise for building MCP servers - not a production tool. It's genuinely useful if you play Arc Raiders, but the data comes from community-maintained APIs that can lag behind patches, and some gaps exist (see [Known Limitations](#known-limitations)). Take the numbers as a solid starting point, not gospel. If something looks off, the game is probably right.
>
> If you're here to learn how MCP servers work, the [Developer Walkthrough](#how-it-works-a-developer-walkthrough) section breaks down every design decision from scratch.

---

## Example Conversations

### "What's the best bang for buck weapon for PVP?"

> The **Ferro I** stands out immediately. 475 coins, 40 damage per shot, strong armor penetration, 2.5× headshot multiplier. A headshot does 100 damage - one-shot kill on an unshielded player. The tradeoff is a 6.6 RPM fire rate (break-action), so every miss is painful. For players who want reliability over skill ceiling, the **Renegade IV** has a faster fire rate and kills any shield type in 3 headshots.

---

### "Compare TTK: Anvil IV vs Renegade IV against a medium shield"

| | Anvil IV | Renegade IV |
|--|---------|------------|
| Damage | 40 | 35 |
| Headshot Mult | 2.5× | 2.25× |
| Fire Rate | 16.3 RPM | 21 RPM |
| **Body TTK** | 4 shots, 11.04s | 4 shots, 8.57s |
| **Headshot TTK** | **2 shots, 3.68s** | 3 shots, 5.71s |

> Claude's take: *The Anvil IV is the better headshot weapon - 2 shots kills a medium shield player in 3.68s vs the Renegade's 3 shots at 5.71s. But the Renegade wins body shots by a wide margin. Two very different playstyles, both viable at tier IV.*

---

### "What's the best weapon vs a medium shield specifically?"

> Ranked by headshot TTK against 70 charge / 42.5% mitigation:
>
> | Weapon | Cost | HS Shots | HS TTK |
> |--------|------|---------|--------|
> | Osprey I | 7,000 | 2 | **3.41s** |
> | Anvil IV | 13,000 | 2 | 3.68s |
> | Jupiter | 27,500 | 2 | 5.00s |
> | Renegade IV | 17,000 | 3 | 5.71s |
> | Il Toro I | 2,900 | ? | ? |
>
> The **Osprey I** is the surprise winner - cheaper than Anvil IV, faster TTK than Jupiter, and 2 headshots kills any medium shield player. The **Il Toro I** (shotgun) wins on body TTK at just 2,900 coins, but only works within its 20m range.

---

### "Should I keep this ARC Alloy or recycle it?"

> Keep it. ARC Alloy is required by **Clearer Skies** (Shani) - you need 3 for that quest objective. It's also used in several crafting recipes. If you don't need it right now, it recycles into components worth more than its 200-coin sell value.

---

### "Does armor penetration affect player shields?"

> No. ARC armor penetration only applies to ARC robot armor. Against player shields, what matters is base damage, headshot multiplier, and fire rate. The Bettina's "strong" armor penetration does nothing in PVP - which is why despite its high fire rate, it takes 10 shots and 16.88s to kill a medium shield player.

---

## Table of Contents

- [Quickstart](#quickstart)
- [Available Tools](#available-tools)
- [Data Sources](#data-sources)
- [Known Limitations](#known-limitations)
- [How It Works: A Developer Walkthrough](#how-it-works-a-developer-walkthrough)
  - [What is MCP?](#what-is-mcp)
  - [Project Structure](#project-structure)
  - [The Server (FastMCP + Tools)](#the-server-fastmcp--tools)
  - [The Client (APIs + Caching)](#the-client-apis--caching)
  - [How Data Flows](#how-data-flows)
  - [Fuzzy Name Matching](#fuzzy-name-matching)
  - [Caching Strategy](#caching-strategy)
  - [The Wiki Scraper](#the-wiki-scraper)
- [Docker](#docker)
- [Development Setup](#development-setup)

---

## Quickstart

### Claude Desktop

Add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "arc-raiders": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/jordanenglish/arc-raiders-mcp", "arc-raiders-mcp"]
    }
  }
}
```

> `uvx` runs the package directly without a manual install step. If you don't have `uv`, install it with `brew install uv` or see [uv docs](https://docs.astral.sh/uv/).

### Claude Code (CLI)

```bash
claude mcp add arc-raiders -- uvx --from git+https://github.com/jordanenglish/arc-raiders-mcp arc-raiders-mcp
```

Or add it manually to your Claude Code config:

```json
{
  "mcpServers": {
    "arc-raiders": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/jordanenglish/arc-raiders-mcp", "arc-raiders-mcp"]
    }
  }
}
```

---

## Available Tools

| Tool | What it does |
|------|-------------|
| `search_items` | Search items by name. Optional filters for rarity and type. |
| `get_item` | Full item details: description, sell/recycle/salvage values, vendor prices, crafting recipe, weapon stats, and headshot multiplier. |
| `get_crafting_recipe` | Ingredients, station, level required, and profitability vs buying raw materials. |
| `find_uses_for_item` | Reverse lookup: which recipes need this item, which traders accept it in barters. |
| `find_quests_for_item` | Which quests require this item in objectives, and which quests reward it. Great for "should I keep this?" decisions. |
| `list_quests` | All quests grouped by trader, with XP and reward counts. Filter by trader name. |
| `get_quest` | Objectives, item rewards, XP, and position in the quest chain. |
| `get_enemy` | Enemy stats (HP, armor, threat, weakness), XP rewards, spawn maps, and loot drops. |
| `get_hideout_module` | Per-level upgrade requirements and total material cost for each hideout station. |
| `get_trader_inventory` | Barter trades for a given trader (Lance, Celeste, Shani, Tian Wen, Apollo). |
| `list_weapons` | All weapons with damage, headshot multiplier, fire rate, range, stability, armor pen, and sell value. Filter by type. |
| `get_ttk` | Time-to-kill for any weapon against all four shield types (none/light/medium/heavy), for both body shots and headshots. Uses the exact damage formula from arcraiders.wiki. |
| `explain_shields` | How shields work: mitigation percentages, charge mechanics, damage formula, and why ARC armor penetration does nothing in PvP. |

---

## Data Sources

The server pulls from three external APIs and one wiki:

| Source | URL | What it provides |
|--------|-----|-----------------|
| **arcdata** | `arcdata.mahcks.com/v1` | Economy data: item sell values, recipes, vendor prices, quests, bots, trades, hideout modules |
| **ARDB** | `ardb.app/api` | Item and enemy catalog with display names (used as the search index) |
| **arcraiders.wiki** | `arcraiders.wiki/w/api.php` | Weapon headshot multipliers, enemy HP/armor/attack type, shield damage formulas (MediaWiki API, parsed from infobox templates) |

The APIs are community-maintained and unofficial. No API key is required.

---

## Known Limitations

Not everything can be answered. Here's where the data runs out:

**Enemy spawns**
- The server can tell you *which maps* an enemy appears on, but not *where on the map* it spawns or how frequently.
- Example: "Which map has the best chance to spawn a Bombardier?" - we can list Dam Battlegrounds, The Spaceport, The Blue Gate, and Buried City, but spawn weights and specific locations aren't tracked by any of our data sources.
- For spawn locations, check community resources: interactive maps, Reddit guides, or YouTube.

**Headshot multipliers for some weapons**
- Multipliers are scraped from arcraiders.wiki. Weapons without a wiki page (or with incomplete infoboxes) return `?`.
- Affected weapons currently include: Rattler, Bettina, Torrente, Il Toro, Vulcano, Dolabra, Canto, Bobcat.

**Shotgun damage**
- Shotgun damage values represent per-pellet damage. The total damage per shot depends on how many pellets connect, which isn't tracked.

**Live/patch data**
- All data sources are community-maintained and may lag behind game patches. Stats shown reflect the most recently updated community data, not necessarily the current live game.

---

## How It Works: A Developer Walkthrough

### What is MCP?

MCP is a protocol that lets AI models call external tools in a standardized way. Think of it like a USB-C standard for AI tool use: any MCP-compatible client (Claude Desktop, Claude Code, other LLMs) can connect to any MCP server and call its tools without custom integration code.

The protocol has two sides:

- **MCP Server** (this project): exposes tools as callable functions. Runs as a subprocess.
- **MCP Client** (Claude Desktop / Claude Code): discovers available tools and calls them when relevant.

Communication happens over **stdio** (standard input/output). Claude spawns the server process and sends JSON-RPC messages through stdin; the server responds through stdout. You never see this directly - it's all handled by the MCP library.

```
Claude Desktop
     |
     | spawns subprocess, communicates via stdin/stdout (JSON-RPC over stdio)
     v
arc-raiders-mcp process
     |
     | makes HTTP requests
     v
arcdata.mahcks.com / ardb.app / arcraiders.wiki
```

### Project Structure

```
arc_raiders_mcp/
├── __init__.py     # empty, marks this as a Python package
├── server.py       # MCP tools + response formatting
└── client.py       # HTTP clients, caching, name search, wiki parser
pyproject.toml      # package metadata and entry point
Dockerfile          # containerized deployment
```

### The Server (FastMCP + Tools)

`server.py` uses [FastMCP](https://github.com/jlowin/fastmcp), a Python framework that handles the MCP protocol boilerplate. You just write async Python functions and decorate them with `@mcp.tool()`.

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Arc Raiders")

@mcp.tool()
async def get_item(name: str) -> str:
    """Get full details for an item..."""
    # ... your logic ...
    return formatted_string
```

That decorator does several things automatically:

1. Registers the function as a tool Claude can call
2. Uses the function's type hints (`name: str`) to generate a JSON schema for the tool's parameters
3. Uses the docstring as the tool description Claude sees when deciding whether to call it
4. Handles serialization/deserialization of inputs and outputs

The entry point is:

```python
def main() -> None:
    mcp.run(transport="stdio")
```

This starts the server listening on stdin and writing to stdout. `pyproject.toml` maps the `arc-raiders-mcp` CLI command to this `main()` function:

```toml
[project.scripts]
arc-raiders-mcp = "arc_raiders_mcp.server:main"
```

### The Client (APIs + Caching)

`client.py` is responsible for all HTTP communication with the three external APIs. Every request goes through a single `_get()` function:

```python
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
```

All the API functions are thin wrappers around `_get()`:

```python
async def arcdata_item(item_id: str) -> dict | None:
    try:
        return await _get(f"{ARCDATA_BASE}/items/{item_id}")
    except Exception:
        return None
```

Note the `try/except` - if any individual item fetch fails, the function returns `None` rather than crashing. The server handles `None` gracefully throughout.

### How Data Flows

Here is what happens when Claude calls `get_item("Stitcher T2")`:

1. **`_resolve_item("Arpeggio II")`** is called first. This is a shared helper that:
   - Fetches the full ARDB item list (which has display names)
   - Runs fuzzy name matching to find `arpeggio_ii`
   - Fetches the arcdata record for that ID (has economy/recipe data)
   - Fetches the ARDB detail record for that ID (has weapon specs)
   - Falls back between ID formats if needed if the IDs differ between the two APIs

2. **`get_item()`** takes the resolved data and builds a formatted Markdown string covering economy, vendor prices, crafting, and weapon stats.

3. Claude receives the Markdown string and uses it to answer the user.

The reason two APIs are needed: ARDB has item names (good for search) but limited economy data. arcdata has rich economy/recipe data but inconsistent display names. The server uses ARDB as the search index and arcdata as the data source.

### Fuzzy Name Matching

Users type item names imprecisely. The `find_best_match()` function handles this with a three-tier priority system:

```python
def find_best_match(query: str, items: list[dict]) -> dict | None:
    q = query.lower().strip()

    # 1. Exact match (highest priority)
    for item in items:
        if name_en(item).lower() == q:
            return item

    # 2. Starts with (e.g. "stitch" finds "Stitcher T2")
    starts = [i for i in items if name_en(i).lower().startswith(q)]
    if starts:
        return starts[0]

    # 3. Substring (e.g. "t2" finds "Stitcher T2")
    subs = [i for i in items if q in name_en(i).lower()]
    if subs:
        return subs[0]

    return None
```

This is intentionally simple - no fuzzy/typo tolerance, but exact/prefix/substring covers most real-world cases without false positives.

`name_en()` handles the fact that some APIs return names as a plain string while others return a localization dict `{"en": "...", "de": "..."}`:

```python
def name_en(item: dict) -> str:
    name = item.get("name", "")
    if isinstance(name, dict):
        return name.get("en", "")
    return str(name)
```

### Caching Strategy

There are two levels of caching:

**1. URL-level cache (`_cache` dict):** Every HTTP response is cached for 1 hour. This is a simple in-memory dict mapping URL -> (data, timestamp). All reads go through `_get()`, which checks this cache first.

**2. Full catalog cache (module-level globals):** Some tools need to iterate over every item (e.g., `find_uses_for_item` has to scan all recipes). Fetching 500+ items one-by-one every call would be too slow. So the first call builds a complete catalog in memory and stores it in a module-level variable:

```python
_item_catalog: dict[str, dict] | None = None
_catalog_lock = asyncio.Lock()

async def build_item_catalog() -> dict[str, dict]:
    global _item_catalog
    async with _catalog_lock:
        if _item_catalog is not None:
            return _item_catalog
        # ... fetch all items with bounded concurrency ...
        _item_catalog = {iid: data for iid, data in results if data}
    return _item_catalog
```

The `asyncio.Lock()` prevents a race condition where multiple concurrent tool calls could each try to build the catalog at the same time. Only the first one does the work; the rest wait and then reuse the result.

Bounded concurrency (`asyncio.Semaphore(20)`) prevents flooding the API with 500 parallel requests:

```python
semaphore = asyncio.Semaphore(20)

async def fetch_one(iid: str) -> tuple[str, dict | None]:
    async with semaphore:
        return iid, await arcdata_item(iid)

results = await asyncio.gather(*[fetch_one(iid) for iid in item_ids])
```

`asyncio.gather()` runs all coroutines concurrently, but the semaphore ensures no more than 20 are in-flight at once.

### The Wiki Scraper

The Arc Raiders wiki uses MediaWiki. The wiki API returns raw wikitext (the markup language used to write wiki pages), which has to be parsed.

Enemy pages have infobox templates like:

```
{{Infobox arc
| name = Sentinel
| health = 450
| armor = Heavy
| pAttack = Ranged
| threat = High
}}
```

`_parse_infobox()` extracts these key-value pairs using a regex:

```python
def _parse_infobox(wikitext: str) -> dict:
    match = re.search(r"\{\{Infobox arc(.*?)\}\}", wikitext, re.DOTALL | re.IGNORECASE)
    if not match:
        return {}
    result = {}
    for line in match.group(1).split("\n"):
        if "|" in line and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip().lstrip("|").strip()
            value = re.sub(r"<br\s*/?>", " | ", value)     # <br> -> separator
            value = re.sub(r"<!--.*?-->", "", value)        # strip HTML comments
            value = re.sub(r"<[^>]+>", "", value).strip()  # strip remaining HTML tags
            if key and value:
                result[key] = value
    return result
```

The wiki catalog is built the same way as the item catalog: fetch all pages in the `Category:ARC` category, parse each one, cache the results.

---

## Docker

```bash
docker build -t arc-raiders-mcp .
docker run --rm -i arc-raiders-mcp
```

The `-i` flag keeps stdin open, which is required for the stdio transport.

To use the Docker image with Claude Desktop:

```json
{
  "mcpServers": {
    "arc-raiders": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "arc-raiders-mcp"]
    }
  }
}
```

---

## Development Setup

```bash
git clone https://github.com/jordanenglish/arc-raiders-mcp
cd arc-raiders-mcp
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Test a tool directly using the MCP development server:

```bash
mcp dev arc_raiders_mcp/server.py
```

This opens an interactive inspector in your browser where you can call tools without needing Claude Desktop.
