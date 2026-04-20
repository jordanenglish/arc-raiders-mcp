"""Arc Raiders MCP server."""

import asyncio
import datetime
import re

from mcp.server.fastmcp import FastMCP

from . import client

mcp = FastMCP("Arc Raiders")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _coins(value: int) -> str:
    return f"{value:,} coins"


async def _resolve_item(name: str) -> tuple[dict | None, dict | None, str]:
    """
    Finds an item by name using ARDB (which has all names inline), then fetches:
      - arcdata detail (economy, recipe, mod slots, effects)
      - ARDB detail (weaponSpecs with bonuses for weapons)

    Returns (arcdata_data, ardb_detail, item_id).
    ARDB uses different ID schemes (e.g. "stitcher_t2" vs arcdata "stitcher_ii"),
    so we try a name-derived ID as a fallback for arcdata.
    """
    ardb_list = await client.ardb_items()
    match = client.find_best_match(name, ardb_list)
    if not match:
        return None, None, ""

    ardb_id = match["id"]

    # Fetch arcdata (try ARDB id, then name-derived id)
    arcdata = await client.arcdata_item(ardb_id)
    item_id = ardb_id
    if arcdata is None:
        name_derived_id = client.name_en(match).lower().replace(" ", "_").replace("-", "_")
        if name_derived_id != ardb_id:
            arcdata = await client.arcdata_item(name_derived_id)
            if arcdata:
                item_id = name_derived_id

    # Always fetch ARDB detail in parallel for weaponSpecs
    ardb_detail = await client.ardb_item(ardb_id)

    # If arcdata failed entirely, fall back to ARDB detail as primary
    if arcdata is None:
        arcdata = ardb_detail

    return arcdata, ardb_detail, item_id


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_blueprint(name: str) -> str:
    """
    Find out how to obtain a crafting blueprint for a weapon or item.
    Blueprints are separate items that unlock crafting recipes at hideout stations.

    Most blueprints are found as loot in-raid. This tool checks known
    non-loot sources (quests, traders) and clearly notes when in-raid
    loot is the only known acquisition method.

    Use get_crafting_recipe() if you want to know what materials are
    needed to craft the item once you have the blueprint.
    """
    # Normalize: strip "blueprint" suffix and weapon tier (I-IV) if present
    base_name = name.strip()
    if base_name.lower().endswith("blueprint"):
        base_name = base_name[: -len("blueprint")].strip()
    base_name = re.sub(r"\s+[IVX]+$", "", base_name, flags=re.IGNORECASE).strip()

    # Look up the blueprint item directly by searching for "{base_name} Blueprint"
    ardb_list = await client.ardb_items()
    bp_query = f"{base_name} Blueprint"
    bp_match = client.find_best_match(bp_query, ardb_list)

    # Must be an actual blueprint item, not just a fuzzy weapon match
    if not bp_match or "blueprint" not in client.name_en(bp_match).lower():
        # Fall back: confirm the weapon exists so we can name it
        weapon_data, _, _ = await _resolve_item(base_name)
        if not weapon_data or "blueprint" in client.name_en(weapon_data).lower():
            return f"Item '{base_name}' not found. Try search_items() to find the correct name."
        weapon_name = client.name_en(weapon_data)
        return (
            f"## Blueprint: {weapon_name}\n\n"
            "Blueprint item not found in data sources.\n\n"
            "**Loot in-raid:**\n"
            "Blueprints are found as loot while raiding. "
            "Specific drop locations are not tracked in this server's data sources."
        )

    item_name = client.name_en(bp_match).replace(" Blueprint", "")
    lines = [f"## Blueprint: {item_name}", ""]

    # Check trader vendors on the blueprint item itself
    bp_full = await client.arcdata_item(bp_match["id"])
    vendor_offers: list[str] = []
    if bp_full:
        for v in bp_full.get("vendors", []):
            cost = v.get("cost", {})
            cost_str = f"{cost.get('coins', 0):,} coins" if "coins" in cost else str(cost)
            extras = []
            if v.get("limit"):
                extras.append(f"limit {v['limit']}/day")
            if v.get("requiredLevel"):
                extras.append(f"level {v['requiredLevel']}+")
            extra_str = f" ({', '.join(extras)})" if extras else ""
            vendor_offers.append(f"  - **{v['trader']}:** {cost_str}{extra_str}")

    # Check quests for blueprint rewards
    all_quests = await client.get_all_quests()
    quest_rewards: list[tuple[str, str, int]] = []
    for quest in all_quests.values():
        quest_name = client.name_en(quest)
        trader = quest.get("trader", "?")
        for r in quest.get("rewardItemIds", []):
            if r.get("itemId") == bp_match["id"]:
                quest_rewards.append((quest_name, trader, r.get("quantity", 1)))

    if vendor_offers:
        lines.append("**Buy from trader:**")
        lines.extend(vendor_offers)
        lines.append("")

    if quest_rewards:
        lines.append("**Quest reward:**")
        for quest_name, trader, qty in quest_rewards:
            lines.append(f"  - {qty}x from **{quest_name}** ({trader})")
        lines.append("")

    lines.append("**Loot in-raid:**")
    lines.append(
        "Blueprints are found as loot while raiding. "
        "Specific drop locations and loot pool data are not tracked in this server's data sources."
    )

    if not vendor_offers and not quest_rewards:
        lines.append("")
        lines.append(
            "_No trader or quest source found. In-raid loot is likely the only known acquisition method._"
        )

    return "\n".join(lines)


@mcp.tool()
async def get_item(name: str) -> str:
    """
    Get full details for an item: description, type, rarity, weight,
    sell value, recycle value, salvage value, and a recommendation
    for the highest-value disposal method. Also shows vendor prices
    and crafting recipe if applicable.
    """
    data, ardb_detail, item_id = await _resolve_item(name)
    if not data:
        return f"Item '{name}' not found. Try search_items() to find the correct name."

    item_name = client.name_en(data)
    desc = data.get("description", {})
    if isinstance(desc, dict):
        desc = desc.get("en", "")

    lines = [
        f"## {item_name}",
        f"**Type:** {data.get('type', '?')}  |  **Rarity:** {data.get('rarity', '?')}",
        f"**Weight:** {data.get('weightKg', '?')} kg  |  **Stack size:** {data.get('stackSize', 1)}",
    ]
    if desc:
        lines += ["", f"_{desc}_"]

    # --- Economy ---
    sell_value = data.get("value", 0)

    # Recycle
    recycles_into = data.get("recyclesInto", {})
    recycle_total = 0
    recycle_lines = []
    for sub_id, qty in recycles_into.items():
        sub = await client.arcdata_item(sub_id)
        sub_name = client.name_en(sub) if sub else sub_id
        sub_val = sub.get("value", 0) if sub else 0
        subtotal = sub_val * qty
        recycle_total += subtotal
        recycle_lines.append(f"    - {qty}x {sub_name} ({_coins(sub_val)} each) = {_coins(subtotal)}")

    # Salvage (in-raid only)
    salvages_into = data.get("salvagesInto", {})
    salvage_total = 0
    salvage_lines = []
    for sub_id, qty in salvages_into.items():
        sub = await client.arcdata_item(sub_id)
        sub_name = client.name_en(sub) if sub else sub_id
        sub_val = sub.get("value", 0) if sub else 0
        subtotal = sub_val * qty
        salvage_total += subtotal
        salvage_lines.append(f"    - {qty}x {sub_name} ({_coins(sub_val)} each) = {_coins(subtotal)}")

    lines += ["", "### Economy"]
    lines.append(f"**Sell:** {_coins(sell_value)}")

    if recycle_lines:
        lines.append(f"**Recycle:** {_coins(recycle_total)}")
        lines.extend(recycle_lines)

    if salvage_lines:
        lines.append(f"**Salvage (in-raid):** {_coins(salvage_total)}")
        lines.extend(salvage_lines)

    # Recommendation
    is_blueprint = "blueprint" in data.get("type", "").lower() or "blueprint" in item_name.lower()

    lines.append("")
    if is_blueprint:
        lines.append("**Recommendation:** Learn the blueprint to unlock crafting. Blueprints cannot be purchased from traders — they are found in-raid or earned as quest rewards.")
    else:
        options: dict[str, int] = {"Sell": sell_value}
        if recycle_total:
            options["Recycle"] = recycle_total
        if salvage_total:
            options["Salvage (in-raid)"] = salvage_total

        best = max(options, key=lambda k: options[k])
        best_val = options[best]

        if best == "Sell" or len(options) == 1:
            lines.append(f"**Recommendation:** Sell for {_coins(sell_value)}")
        else:
            gain = best_val - sell_value
            lines.append(f"**Recommendation: {best}** (+{_coins(gain)} vs selling)")

    # Vendor buy prices
    vendors = data.get("vendors", [])
    if vendors:
        lines += ["", "### Vendor Prices (buy from trader)"]
        for v in vendors:
            cost = v.get("cost", {})
            cost_parts = []
            for currency, amount in cost.items():
                if currency in ("coins", "creds"):
                    cost_parts.append(f"{amount:,} {currency}")
                else:
                    cost_item = await client.arcdata_item(currency)
                    cost_name = client.name_en(cost_item) if cost_item else currency
                    cost_parts.append(f"{amount}x {cost_name}")
            cost_str = ", ".join(cost_parts)

            extras = []
            if v.get("limit"):
                extras.append(f"limit {v['limit']}/day")
            if v.get("requiredLevel"):
                extras.append(f"level {v['requiredLevel']}+")
            extra_str = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"  - **{v['trader']}:** {cost_str}{extra_str}")

    # Crafting recipe
    recipe = data.get("recipe", {})
    if recipe:
        bench = data.get("craftBench", [])
        if isinstance(bench, str):
            bench = [bench]
        level = data.get("stationLevelRequired", 1)
        bench_str = ", ".join(bench) if bench else "unknown station"
        lines += ["", f"### Craft at: {bench_str} (station level {level})"]
        for ing_id, qty in recipe.items():
            ing = await client.arcdata_item(ing_id)
            ing_name = client.name_en(ing) if ing else ing_id
            lines.append(f"  - {qty}x {ing_name}")

    # Weapon stats (numeric, from MetaForge) + qualitative effects (from arcdata)
    item_type_upper = data.get("type", "").upper()
    is_weapon = data.get("isWeapon", False) or any(
        wt in item_type_upper
        for wt in ("SMG", "AR", "LMG", "SHOTGUN", "SNIPER", "PISTOL", "LAUNCHER", "MELEE", "RIFLE")
    )
    if is_weapon:
        weapon_specs = ardb_detail.get("weaponSpecs", {}) if ardb_detail else {}
        stats = weapon_specs.get("stats", {})
        bonuses = weapon_specs.get("bonuses", {})
        wiki_weapon = await client.wiki_weapon(item_name)

        lines += ["", "### Weapon Stats"]

        # Durability from arcdata effects (absolute value, varies by tier)
        durability_effect = data.get("effects", {}).get("Durability", {})
        dur_raw = durability_effect.get("value", "") if isinstance(durability_effect, dict) else ""
        if dur_raw:
            dur_max = dur_raw.split("/")[-1]
            lines.append(f"  - **Durability:** {dur_max}")

        # Base stats from ARDB weaponSpecs
        stat_map = [
            ("damage", "Damage"),
            ("fireRate", "Fire Rate"),
            ("range", "Range"),
            ("stability", "Stability"),
            ("agility", "Agility"),
            ("stealth", "Stealth"),
        ]
        for field, label in stat_map:
            val = stats.get(field)
            if val is not None and val != 0:
                lines.append(f"  - **{label}:** {val}")

        # Headshot multiplier from wiki
        if wiki_weapon and wiki_weapon.get("headshotmultiplier"):
            lines.append(f"  - **Headshot Multiplier:** {wiki_weapon['headshotmultiplier']}")

        mag = weapon_specs.get("magSize") or stats.get("magazineSize")
        if mag:
            lines.append(f"  - **Magazine Size:** {mag}")
        if weapon_specs.get("firingMode"):
            lines.append(f"  - **Firing Mode:** {weapon_specs['firingMode']}")
        if weapon_specs.get("ammoType"):
            lines.append(f"  - **Ammo Type:** {weapon_specs['ammoType']}")
        if weapon_specs.get("armorPenetration"):
            lines.append(f"  - **Armor Penetration:** {weapon_specs['armorPenetration']}")

        # Tier bonuses vs base
        lines.append("")
        if bonuses:
            lines.append("  **Tier bonuses vs base:**")
            h_recoil = bonuses.get("horizontalRecoilPercent")
            reload_time = bonuses.get("reloadTimePercent")
            dur_bonus = bonuses.get("durability")
            if h_recoil:
                lines.append(f"  - {abs(h_recoil)}% Reduced Horizontal Recoil")
            if reload_time:
                lines.append(f"  - {abs(reload_time)}% Reduced Reload Time")
            if dur_bonus:
                lines.append(f"  - +{dur_bonus} Durability")
        else:
            lines.append("  No tier bonuses (base weapon)")

        # Mod slots
        mod_slots = data.get("modSlots", {})
        if mod_slots:
            lines += ["", "**Mod slots:** " + ", ".join(mod_slots.keys())]

        # Upgrade cost to reach this tier (from previous tier)
        upgrade_cost = data.get("upgradeCost", {})
        if upgrade_cost:
            upgrade_parts = []
            for iid, qty in upgrade_cost.items():
                ing = await client.arcdata_item(iid)
                ing_name = client.name_en(ing) if ing else iid
                upgrade_parts.append(f"{qty}x {ing_name}")
            lines.append(f"**Upgrade cost (from previous tier):** {', '.join(upgrade_parts)}")

        # Upgrade chain
        upgrades_to = data.get("upgradesTo")
        if upgrades_to:
            up_item = await client.arcdata_item(upgrades_to)
            up_name = client.name_en(up_item) if up_item else upgrades_to
            next_upgrade_cost = up_item.get("upgradeCost", {}) if up_item else {}
            if next_upgrade_cost:
                next_parts = []
                for iid, qty in next_upgrade_cost.items():
                    ing = await client.arcdata_item(iid)
                    ing_name = client.name_en(ing) if ing else iid
                    next_parts.append(f"{qty}x {ing_name}")
                lines.append(f"**Upgrades to:** {up_name} (upgrade cost: {', '.join(next_parts)})")
            else:
                lines.append(f"**Upgrades to:** {up_name}")

        # Repair cost
        repair_cost = data.get("repairCost", {})
        if repair_cost:
            repair_parts = ", ".join(f"{qty}x {iid}" for iid, qty in repair_cost.items())
            lines.append(f"**Repair cost:** {repair_parts} (restores {int(data.get('repairDurability', 0) * 100)}% durability)")

    # Effects (quick-use items)
    effects = data.get("effects", {})
    if effects and not is_weapon:
        lines += ["", "### Effects"]
        for effect_name, effect_data in effects.items():
            val = effect_data.get("value", "") if isinstance(effect_data, dict) else str(effect_data)
            lines.append(f"  - **{effect_name}:** {val}")

    return "\n".join(lines)


@mcp.tool()
async def search_items(
    query: str,
    rarity: str = "",
    item_type: str = "",
    limit: int = 15,
) -> str:
    """
    Search for items by name. Optionally filter by rarity or type.

    rarity: Common, Uncommon, Rare, Epic, Legendary (case-insensitive)
    item_type: Basic Material, Topside Material, Refined Material, Quick Use,
               Weapon, Gear, Recyclable, Nature, Misc, Trinket (partial match)
    """
    all_items = await client.ardb_items()
    matches = client.find_all_matches(query, all_items)

    if rarity:
        matches = [i for i in matches if i.get("rarity", "").lower() == rarity.lower()]
    if item_type:
        matches = [i for i in matches if item_type.lower() in i.get("type", "").lower()]

    if not matches:
        filters = []
        if rarity:
            filters.append(f"rarity '{rarity}'")
        if item_type:
            filters.append(f"type '{item_type}'")
        filter_str = " with " + " and ".join(filters) if filters else ""
        return f"No items found matching '{query}'{filter_str}."

    header = f"Found {len(matches)} item(s) matching '{query}'"
    if len(matches) > limit:
        header += f" (showing first {limit})"
    lines = [header + ":"]

    for item in matches[:limit]:
        item_name = client.name_en(item) or item.get("id", "?")
        rarity_str = (item.get("rarity") or "?").capitalize()
        type_str = item.get("type", "?")
        val = item.get("value", 0)
        lines.append(f"  - **{item_name}** ({rarity_str} {type_str}) - {_coins(val)}")

    return "\n".join(lines)


@mcp.tool()
async def get_crafting_recipe(name: str) -> str:
    """
    Get the crafting recipe for an item: ingredients, station, level required,
    and whether crafting is profitable vs buying ingredients.
    """
    data, _, _ = await _resolve_item(name)
    if not data:
        return f"Item '{name}' not found."

    # Station ID -> display name mapping
    _STATION_NAMES = {
        "in_raid": "In-Raid",
        "med_station": "Medical Lab",
        "weapon_bench": "Gunsmith",
        "equipment_bench": "Gear Bench",
        "explosives_bench": "Explosives Station",
        "utility_bench": "Utility Station",
        "workbench": "Workbench",
        "refiner": "Refiner",
        "scrappy": "Scrappy",
        "stash": "Stash",
    }

    item_name = client.name_en(data)
    recipe = data.get("recipe", {})
    upgrade_cost = data.get("upgradeCost", {})
    if not recipe:
        if upgrade_cost:
            # Item is obtained by upgrading from the previous tier
            upgrades_from = data.get("upgradesFrom")
            from_name = item_name.rsplit(" ", 1)[0] + " " + (
                {"II": "I", "III": "II", "IV": "III"}.get(item_name.rsplit(" ", 1)[-1], "")
            ) if " " in item_name else item_name
            if upgrades_from:
                from_item = await client.arcdata_item(upgrades_from)
                from_name = client.name_en(from_item) if from_item else from_name
            lines = [f"## {item_name}", "", f"**{item_name}** cannot be crafted directly. It is obtained by upgrading from the previous tier.", ""]
            lines.append("**Upgrade cost:**")
            for iid, qty in upgrade_cost.items():
                ing = await client.arcdata_item(iid)
                ing_name = client.name_en(ing) if ing else iid
                ing_val = ing.get("value", 0) if ing else 0
                lines.append(f"  - {qty}x **{ing_name}**" + (f" ({_coins(ing_val)} each)" if ing_val else ""))
            lines += ["", f"_To get {item_name}: first craft {from_name}, then upgrade it._"]
            return "\n".join(lines)
        return f"**{item_name}** cannot be crafted (no recipe found)."

    bench = data.get("craftBench", [])
    if isinstance(bench, str):
        bench = [bench]
    level = data.get("stationLevelRequired", 1)

    # Separate in-raid flag from station names
    can_craft_in_raid = "in_raid" in bench
    stations = [_STATION_NAMES.get(b, b) for b in bench if b != "in_raid"]
    station_str = ", ".join(stations) if stations else "Unknown"
    if can_craft_in_raid:
        station_str += " (or In-Raid)"

    lines = [
        f"## Crafting Recipe: {item_name}",
        f"**Station:** {station_str}  |  **Level required:** {level}",
        "",
        "**Ingredients:**",
    ]

    ingredient_total = 0
    for ing_id, qty in recipe.items():
        ing = await client.arcdata_item(ing_id)
        ing_name = client.name_en(ing) if ing else ing_id
        ing_val = ing.get("value", 0) if ing else 0
        subtotal = ing_val * qty
        ingredient_total += subtotal
        lines.append(f"  - {qty}x **{ing_name}** ({_coins(ing_val)} each) = {_coins(subtotal)}")

    sell_val = data.get("value", 0)
    vendors = data.get("vendors", [])
    cheapest_vendor = min((v.get("cost", {}).get("coins", 0) for v in vendors if "coins" in v.get("cost", {})), default=0)

    lines += ["", f"**Craft cost (ingredients):** {_coins(ingredient_total)}"]

    if cheapest_vendor:
        savings = cheapest_vendor - ingredient_total
        if savings > 0:
            lines.append(f"**Cheapest vendor price:** {_coins(cheapest_vendor)} (craft saves {_coins(savings)})")
        else:
            lines.append(f"**Cheapest vendor price:** {_coins(cheapest_vendor)} (buying is cheaper by {_coins(-savings)})")

    if sell_val:
        profit = sell_val - ingredient_total
        sign = "+" if profit >= 0 else ""
        verdict = "profitable to craft and sell" if profit >= 0 else "not worth crafting for profit"
        lines.append(f"**Sell value:** {_coins(sell_val)} ({sign}{_coins(profit)} vs craft cost, {verdict})")

    return "\n".join(lines)


@mcp.tool()
async def find_uses_for_item(name: str) -> str:
    """
    Find all crafting recipes that require this item as an ingredient.
    Also shows which quests reward this item.

    Note: First call builds the full item catalog (~529 items) and may take
    10-20 seconds. Subsequent calls are instant.
    """
    ardb_list = await client.ardb_items()
    match = client.find_best_match(name, ardb_list)
    if not match:
        return f"Item '{name}' not found."

    target_id = match["id"]
    target_name = client.name_en(match)

    catalog = await client.build_item_catalog()

    crafting_uses = []
    for item_id, item_data in catalog.items():
        recipe = item_data.get("recipe", {})
        if target_id in recipe:
            qty_needed = recipe[target_id]
            crafting_uses.append((
                client.name_en(item_data),
                qty_needed,
                item_data.get("value", 0),
            ))

    lines = [f"## Uses for: {target_name}", ""]

    if crafting_uses:
        lines.append(f"**Used in {len(crafting_uses)} crafting recipe(s):**")
        for craft_name, qty, val in sorted(crafting_uses, key=lambda x: x[0]):
            lines.append(f"  - {qty}x needed to craft **{craft_name}** (sells for {_coins(val)})")
    else:
        lines.append("Not used in any known crafting recipes.")

    # Check trades (barter uses)
    trades = await client.arcdata_trades()
    barter_uses = [t for t in trades if t.get("cost", {}).get("itemId") == target_id]
    if barter_uses:
        lines += ["", f"**Used in {len(barter_uses)} barter trade(s):**"]
        for trade in barter_uses:
            recv_item = await client.arcdata_item(trade["itemId"])
            recv_name = client.name_en(recv_item) if recv_item else trade["itemId"]
            qty_needed = trade["cost"].get("quantity", 1)
            recv_qty = trade.get("quantity", 1)
            trader = trade.get("trader", "?")
            lines.append(f"  - Trade {qty_needed}x to **{trader}** for {recv_qty}x {recv_name}")

    # Check hideout module requirements
    stubs = await client.arcdata_hideout_list()
    modules = [m for m in await asyncio.gather(*[client.arcdata_hideout(s["id"]) for s in stubs]) if m]
    hideout_uses = []
    for module in modules:
        module_name = client.name_en(module) or module.get("id", "?")
        for level_data in module.get("levels", []):
            for req in level_data.get("requirementItemIds", []):
                if req.get("itemId") == target_id:
                    hideout_uses.append((module_name, level_data.get("level", "?"), req.get("quantity", 1)))

    if hideout_uses:
        lines += ["", f"**Required for {len(hideout_uses)} hideout upgrade(s):**"]
        for module_name, level, qty in hideout_uses:
            lines.append(f"  - {qty}x for **{module_name}** level {level}")

    return "\n".join(lines)


@mcp.tool()
async def list_quests(trader: str = "") -> str:
    """
    List all quests, optionally filtered by trader.
    Shows quest name, XP reward, and whether it has item rewards.

    Traders: Lance, Celeste, Shani, Tian Wen, Apollo
    Leave blank to list all quests grouped by trader.
    """
    all_quests = await client.get_all_quests()

    # Group by trader
    by_trader: dict[str, list] = {}
    for quest in all_quests.values():
        t = quest.get("trader", "Unknown")
        by_trader.setdefault(t, []).append(quest)

    if trader:
        filtered = {t: qs for t, qs in by_trader.items() if trader.lower() in t.lower()}
        if not filtered:
            available = ", ".join(sorted(by_trader))
            return f"Trader '{trader}' not found. Available: {available}"
        by_trader = filtered

    lines = []
    for t in sorted(by_trader):
        lines.append(f"## {t}")
        for q in by_trader[t]:
            quest_name = client.name_en(q)
            xp = q.get("xp", 0)
            rewards = q.get("rewardItemIds", [])
            reward_str = f" | {len(rewards)} item reward(s)" if rewards else ""
            xp_str = f" | {xp:,} XP" if xp else ""
            lines.append(f"  - {quest_name}{xp_str}{reward_str}")
        lines.append("")

    total = sum(len(qs) for qs in by_trader.values())
    lines.insert(0, f"**{total} quest(s)**\n")
    return "\n".join(lines)


@mcp.tool()
async def get_quest(name: str) -> str:
    """
    Get details for a quest: objectives, item rewards, XP, trader,
    and where it sits in the quest chain.
    """
    all_quests = await client.get_all_quests()
    quest_list = list(all_quests.values())

    match = client.find_best_match(name, quest_list)
    if not match:
        return (
            f"Quest '{name}' not found.\n"
            "Tip: Quest names are case-insensitive partial matches, e.g. 'bad feeling' finds 'A Bad Feeling'."
        )

    quest_name = client.name_en(match)
    desc = match.get("description", {})
    if isinstance(desc, dict):
        desc = desc.get("en", "")

    lines = [
        f"## {quest_name}",
        f"**Trader:** {match.get('trader', '?')}  |  **XP reward:** {match.get('xp', 0):,}",
    ]
    if desc:
        lines += ["", f"_{desc}_"]

    objectives = match.get("objectives", [])
    if objectives:
        lines += ["", "**Objectives:**"]
        for obj in objectives:
            obj_text = obj.get("en", obj) if isinstance(obj, dict) else str(obj)
            lines.append(f"  - {obj_text}")

    rewards = match.get("rewardItemIds", [])
    if rewards:
        lines += ["", "**Item rewards:**"]
        for r in rewards:
            item = await client.arcdata_item(r["itemId"])
            item_name = client.name_en(item) if item else r["itemId"]
            val = item.get("value", 0) if item else 0
            lines.append(f"  - {r['quantity']}x {item_name} ({_coins(val)})")

    prev_ids = match.get("previousQuestIds", [])
    next_ids = match.get("nextQuestIds", [])
    if prev_ids or next_ids:
        lines.append("")
    if prev_ids:
        prev_names = [client.name_en(all_quests[qid]) if qid in all_quests else qid for qid in prev_ids]
        lines.append(f"**Requires completing:** {', '.join(prev_names)}")
    if next_ids:
        next_names = [client.name_en(all_quests[qid]) if qid in all_quests else qid for qid in next_ids]
        lines.append(f"**Unlocks:** {', '.join(next_names)}")

    return "\n".join(lines)


@mcp.tool()
async def get_enemy(name: str) -> str:
    """
    Get info about an ARC enemy: type, threat level, HP, armor, weakness,
    XP rewards, attack type, which maps they appear on, and loot drops.
    """
    bots, enemies, wiki, rt_bots = await asyncio.gather(
        client.arcdata_bots(),
        client.ardb_enemies(),
        client.wiki_enemy(name),
        client.raidtheory_bots(),
    )
    bot = client.find_best_match(name, bots)
    enemy = client.find_best_match(name, enemies)
    rt_bot = client.find_best_match(name, rt_bots)

    if not bot and not enemy and not wiki and not rt_bot:
        return f"Enemy '{name}' not found."

    display_name = (
        bot.get("name") if bot
        else rt_bot.get("name") if rt_bot
        else enemy.get("name") if enemy
        else wiki.get("name", name) if wiki
        else name
    )

    lines = [f"## {display_name}"]

    # --- Core stats (merge arcdata + raidtheory + wiki) ---
    threat = (
        bot.get("threat") or (rt_bot.get("threat") if rt_bot else None)
        or (wiki.get("threatLevel") if wiki else None)
    )
    bot_type = (bot.get("type") if bot else None) or (rt_bot.get("type") if rt_bot else None)
    hp = wiki.get("health") if wiki else None
    armor = wiki.get("armor") if wiki else None

    stat_parts = []
    if bot_type:
        stat_parts.append(f"**Type:** {bot_type}")
    if threat:
        stat_parts.append(f"**Threat:** {threat}")
    if hp and hp.strip():
        stat_parts.append(f"**HP:** {hp}")
    if armor and armor.lower() != "none":
        stat_parts.append(f"**Armor:** {armor}")
    if stat_parts:
        lines.append("  |  ".join(stat_parts))

    # XP
    if bot:
        lines.append(f"**XP on destroy:** {bot.get('destroyXp', 0):,}  |  **XP on loot:** {bot.get('lootXp', 0):,}")
    elif rt_bot and (rt_bot.get("destroyXp") or rt_bot.get("lootXp")):
        lines.append(f"**XP on destroy:** {rt_bot.get('destroyXp', 0):,}  |  **XP on loot:** {rt_bot.get('lootXp', 0):,}")
    elif wiki and wiki.get("xp"):
        lines.append(f"**XP:** {wiki['xp']}")

    # Attack type
    if wiki and wiki.get("pAttack"):
        lines.append(f"**Attack type:** {wiki['pAttack']}")

    # Weakness (prefer RaidTheory — has detailed weak point descriptions)
    weakness = (
        (rt_bot.get("weakness") if rt_bot else None)
        or (bot.get("weakness") if bot else None)
        or (wiki.get("weakness") if wiki else None)
    )
    if weakness:
        lines += ["", f"**Weakness:** {weakness}"]

    # Description from RaidTheory (behavioral overview)
    rt_desc = rt_bot.get("description", "") if rt_bot else ""
    if rt_desc:
        lines.append(f"**Description:** {rt_desc}")

    # Maps
    maps = (bot.get("maps", []) if bot else []) or (rt_bot.get("maps", []) if rt_bot else [])
    if maps:
        map_names = [m.replace("_", " ").title() for m in maps]
        lines.append(f"**Found on:** {', '.join(map_names)}")

    # Loot drops
    drops_shown = False
    if bot and bot.get("drops"):
        lines += ["", "**Loot drops:**"]
        drops_shown = True
        for drop_id in bot["drops"]:
            item = await client.arcdata_item(drop_id)
            drop_name = client.name_en(item) if item else drop_id
            val = item.get("value", 0) if item else 0
            lines.append(f"  - {drop_name} ({_coins(val)})")

    if not drops_shown and enemy:
        enemy_detail = await client.ardb_enemy(enemy["id"])
        drop_table = (enemy_detail or {}).get("dropTable", [])
        if drop_table:
            lines += ["", "**Loot drops:**"]
            for drop in drop_table:
                drop_name = drop.get("name", drop.get("id", "?"))
                val = drop.get("value", 0)
                rarity = drop.get("rarity", "").capitalize()
                lines.append(f"  - {drop_name} ({rarity}, {_coins(val)})")

    # Behavior and combat tips from wiki
    behavior = (wiki or {}).get("_section_behavior", "")
    combat_tips = (wiki or {}).get("_section_combat_tips", "") or (wiki or {}).get("_section_combat tips", "")
    if behavior:
        lines += ["", "### Behavior", behavior]
    if combat_tips:
        lines += ["", "### Combat Tips", combat_tips]

    return "\n".join(lines)


@mcp.tool()
async def find_quests_for_item(name: str) -> str:
    """
    Find which quests reward a given item, and which quests mention it
    in their objectives (so you know whether to keep it 'found in raid').

    Useful for "should I keep this item?" questions.
    """
    ardb_list = await client.ardb_items()
    match = client.find_best_match(name, ardb_list)
    if not match:
        return f"Item '{name}' not found."

    target_id = match["id"]
    target_name = client.name_en(match)
    target_lower = target_name.lower()

    all_quests = await client.get_all_quests()

    reward_quests = []
    objective_quests = []

    for quest_id, quest in all_quests.items():
        quest_name = client.name_en(quest)
        trader = quest.get("trader", "?")

        # Check rewards (exact item ID match)
        for r in quest.get("rewardItemIds", []):
            if r.get("itemId") == target_id:
                reward_quests.append((quest_name, trader, r.get("quantity", 1)))
                break

        # Check objectives (text match on English string)
        for obj in quest.get("objectives", []):
            obj_text = obj.get("en", "") if isinstance(obj, dict) else str(obj)
            if target_lower in obj_text.lower():
                objective_quests.append((quest_name, trader, obj_text))
                break

    lines = [f"## Quests for: {target_name}", ""]

    if objective_quests:
        lines.append(f"**Required in {len(objective_quests)} quest objective(s):**")
        for quest_name, trader, obj_text in objective_quests:
            lines.append(f"  - **{quest_name}** ({trader}): _{obj_text}_")
    else:
        lines.append("Not mentioned in any quest objectives.")

    lines.append("")

    if reward_quests:
        lines.append(f"**Rewarded by {len(reward_quests)} quest(s):**")
        for quest_name, trader, qty in reward_quests:
            lines.append(f"  - **{quest_name}** ({trader}): {qty}x")
    else:
        lines.append("Not given as a reward by any quest.")

    return "\n".join(lines)


@mcp.tool()
async def get_hideout_module(name: str) -> str:
    """
    Get the upgrade requirements for a hideout module: what items are needed
    for each level and their combined coin value.

    Available modules: Gunsmith, Workbench, Gear Bench, Medical Lab,
    Explosives Station, Utility Station, Refiner, Scrappy, Stash
    """
    stubs = await client.arcdata_hideout_list()
    # Fetch all 9 module details so we can search by proper name
    details = [d for d in await asyncio.gather(*[client.arcdata_hideout(s["id"]) for s in stubs]) if d]

    match = client.find_best_match(name, details)
    # Also try matching by ID substring (e.g. "med_station" matches "medical")
    if not match:
        q = name.lower().replace(" ", "_")
        for m in details:
            if q in m.get("id", ""):
                match = m
                break
    if not match:
        q = name.lower()
        for m in details:
            if q in m.get("id", "").replace("_", " "):
                match = m
                break

    if not match:
        module_names = [client.name_en(m) or m.get("id", "?") for m in details]
        return (
            f"Module '{name}' not found.\n"
            f"Available: {', '.join(module_names)}"
        )

    module = match
    if not module:
        return f"Could not load details for module '{name}'."

    module_name = client.name_en(module) or module.get("id", name)
    max_level = module.get("maxLevel", "?")

    lines = [
        f"## {module_name}",
        f"**Max level:** {max_level}",
        "_Note: what each level unlocks is not available from the data source. Check arcraiders.wiki for crafting station unlock details._",
    ]

    for level_data in module.get("levels", []):
        level = level_data.get("level", "?")
        reqs = level_data.get("requirementItemIds", [])
        lines += ["", f"**Level {level} requirements:**"]
        level_total = 0
        for req in reqs:
            item = await client.arcdata_item(req["itemId"])
            item_name = client.name_en(item) if item else req["itemId"]
            val = item.get("value", 0) if item else 0
            qty = req["quantity"]
            subtotal = val * qty
            level_total += subtotal
            lines.append(f"  - {qty}x {item_name} ({_coins(subtotal)} value)")
        lines.append(f"  Total material value: {_coins(level_total)}")

    return "\n".join(lines)


@mcp.tool()
async def get_trader_inventory(trader_name: str = "") -> str:
    """
    List what a trader offers in barter exchanges.
    If no trader_name given, lists available traders.

    Traders: Lance, Celeste, Shani, Tian Wen, Apollo
    """
    trades = await client.arcdata_trades()
    if not trades:
        return "Could not load trader data."

    all_traders = sorted({t.get("trader", "") for t in trades if t.get("trader")})

    if not trader_name:
        return "**Available traders:** " + ", ".join(all_traders) + "\n\nUse get_trader_inventory('TraderName') to see their stock."

    filtered = [t for t in trades if trader_name.lower() in t.get("trader", "").lower()]
    if not filtered:
        return f"Trader '{trader_name}' not found. Available: {', '.join(all_traders)}"

    trader = filtered[0].get("trader", trader_name)

    # Group trades by received item ID so multiple acquisition methods collapse together
    grouped: dict[str, dict] = {}
    for trade in filtered:
        recv_id = trade.get("itemId", "")
        if recv_id not in grouped:
            grouped[recv_id] = {"trade": trade, "costs": []}
        grouped[recv_id]["costs"].append(trade)

    lines = [f"## {trader}'s Barter Trades", ""]

    for recv_id, group in grouped.items():
        recv_qty = group["trade"].get("quantity", 1)
        recv_item = await client.arcdata_item(recv_id)
        recv_name = client.name_en(recv_item) if recv_item else recv_id
        recv_val = recv_item.get("value", 0) if recv_item else 0

        # Collect extras from any trade in the group
        req_level = next((t.get("requiredLevel") for t in group["costs"] if t.get("requiredLevel")), None)
        daily_limit = next((t.get("dailyLimit") for t in group["costs"] if t.get("dailyLimit")), None)
        extras = []
        if daily_limit:
            extras.append(f"limit {daily_limit}/day")
        if req_level:
            extras.append(f"level {req_level}+")
        extra_str = f" [{', '.join(extras)}]" if extras else ""

        lines.append(f"  - **{recv_qty}x {recv_name}** ({_coins(recv_val)}){extra_str}")

        for trade in group["costs"]:
            cost = trade.get("cost", {})
            cost_id = cost.get("itemId", "")
            cost_qty = cost.get("quantity", 1)
            if cost_id in ("coins", "creds"):
                cost_str = f"{cost_qty:,} {cost_id}"
            else:
                cost_item = await client.arcdata_item(cost_id)
                cost_name = client.name_en(cost_item) if cost_item else cost_id
                cost_val = cost_item.get("value", 0) if cost_item else 0
                cost_str = f"{cost_qty}x {cost_name} ({_coins(cost_qty * cost_val)} value)"
            lines.append(f"    - {cost_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TTK calculator
# ---------------------------------------------------------------------------

# Shield stats sourced from arcraiders.wiki/wiki/Shields
_SHIELDS = {
    "none":   {"charge": 0,  "mitigation": 0.0},
    "light":  {"charge": 40, "mitigation": 0.40},
    "medium": {"charge": 70, "mitigation": 0.425},
    "heavy":  {"charge": 80, "mitigation": 0.525},
}

_PLAYER_HP = 100  # Standard player HP (wiki: 4-5 bandages × 20 HP/bandage)


def _calc_shots_and_ttk(
    damage: float,
    hs_mult: float,
    fire_rate: float,
    shield_charge: int,
    mitigation: float,
    headshots: bool,
) -> tuple[int, float]:
    """
    Returns (shots_to_kill, ttk_seconds).

    Shield mechanic (arcraiders.wiki):
      - While shield charge > 0: HP damage is mitigated, shield takes full base damage.
      - Headshot multiplier applies only to HP, not shield charge.
      - Mitigation applies on every shot while charge > 0, even if charge < incoming damage.
    ARC armor penetration does NOT affect player shields.
    """
    hp = float(_PLAYER_HP)
    charge = float(shield_charge)
    shots = 0

    while hp > 0:
        shots += 1
        mit = mitigation if charge > 0 else 0.0
        mult = hs_mult if headshots else 1.0
        hp_dmg = damage * (1.0 - mit) * mult
        hp -= hp_dmg
        if charge > 0:
            charge = max(0.0, charge - damage)

    ttk = (shots - 1) * (60.0 / fire_rate) if fire_rate > 0 else 0.0
    return shots, ttk


@mcp.tool()
async def explain_shields() -> str:
    """
    Explain how player shields work in Arc Raiders: shield types, mitigation
    percentages, charge values, and how damage is calculated. Useful for
    understanding which shield to use and how TTK changes against shielded players.
    """
    lines = [
        "## How Player Shields Work",
        "",
        "Shields in Arc Raiders add a rechargeable buffer in front of your 100 HP.",
        "They recharge automatically over time; HP requires bandages to restore.",
        "",
        "### Shield Types",
        "",
        "| Shield | Charge | Mitigation | Notes |",
        "|--------|--------|-----------|-------|",
        "| None | 0 | 0% | Bare HP only |",
        "| Light | 40 | 40% | Low barrier, fast recharge |",
        "| Medium | 70 | 42.5% | Most common in PvP |",
        "| Heavy | 80 | 52.5% | Highest mitigation, slowest recharge |",
        "",
        "### Damage Mechanics",
        "",
        "While your shield charge is above 0:",
        "- Your **HP takes mitigated damage**: `base_damage x (1 - mitigation)`",
        "- Your **shield charge drops by the full base damage** (no mitigation on the shield itself)",
        "- The shield depletes faster than your HP loses health",
        "",
        "Once charge hits 0, the shield is gone and all damage goes straight to HP at full value.",
        "",
        "**Headshot multiplier** applies only to HP damage, not to shield charge drain.",
        "",
        "### Key Insight: ARC Armor Penetration",
        "",
        "Weapon armor penetration (None/Low/Medium/High/Very High) **only affects ARC robot armor**.",
        "It does nothing against player shields. A weapon with 'Very High' armor pen",
        "has the same shield performance as one with 'None'.",
        "",
        "### Example: Anvil IV (40 damage) vs Medium Shield",
        "",
        "- Shot 1: HP takes 40 x (1 - 0.425) = 23 damage. Shield drops 40 (charge: 70 -> 30).",
        "- Shot 2: HP takes 23 damage. Shield drops 40 (charge: 30 -> 0, shield breaks).",
        "- Shot 3+: Full 40 damage to HP per shot. 2 more shots to kill.",
        "- Total: 4 shots to kill (body). With headshots (2.5x mult): 2 shots.",
        "",
        "Use `get_ttk` for exact shots-to-kill and timing against any weapon.",
    ]
    return "\n".join(lines)


@mcp.tool()
async def get_ttk(name: str) -> str:
    """
    Calculate time-to-kill (TTK) for a weapon against every shield type
    (none, light, medium, heavy). Shows shots-to-kill and time for both
    body shots and headshots.

    Player HP is assumed to be 100. Shield damage formula from arcraiders.wiki.
    Note: ARC armor penetration does not affect player shields - that stat only
    applies to ARC robot armor.
    """
    data, ardb_detail, _ = await _resolve_item(name)
    if not data:
        return f"Weapon '{name}' not found."

    item_name = client.name_en(data)

    item_type_upper = data.get("type", "").upper()
    is_weapon = data.get("isWeapon", False) or any(
        wt in item_type_upper
        for wt in ("SMG", "AR", "LMG", "SHOTGUN", "SNIPER", "PISTOL", "LAUNCHER", "MELEE", "RIFLE",
                   "HAND CANNON", "BATTLE RIFLE", "ASSAULT RIFLE")
    )
    if not is_weapon:
        return f"'{item_name}' is not a weapon."

    specs = (ardb_detail or {}).get("weaponSpecs", {})
    stats = specs.get("stats", {})
    damage = stats.get("damage", 0)
    fire_rate = stats.get("fireRate", 0)

    wiki = await client.wiki_weapon(item_name)
    raw_hs = (wiki or {}).get("headshotmultiplier", "")
    try:
        hs_mult = float(str(raw_hs).rstrip("×x").strip())
    except (ValueError, AttributeError):
        hs_mult = None

    if not damage or not fire_rate:
        return f"Stat data unavailable for '{item_name}'."

    lines = [
        f"## TTK: {item_name}",
        f"**Damage:** {damage}  |  "
        f"**Headshot Multiplier:** {raw_hs if hs_mult else 'unknown'}  |  "
        f"**Fire Rate:** {fire_rate} RPM",
        f"**Player HP assumed:** {_PLAYER_HP}",
        "",
        "| Shield | Body Shots | Body TTK | HS Shots | HS TTK |",
        "|--------|-----------|----------|---------|--------|",
    ]

    for shield_name, shield in _SHIELDS.items():
        body_shots, body_ttk = _calc_shots_and_ttk(
            damage, 1.0, fire_rate, shield["charge"], shield["mitigation"], False
        )
        body_ttk_str = f"{body_ttk:.2f}s" if body_ttk > 0 else "instant"

        if hs_mult:
            hs_shots, hs_ttk = _calc_shots_and_ttk(
                damage, hs_mult, fire_rate, shield["charge"], shield["mitigation"], True
            )
            hs_ttk_str = f"{hs_ttk:.2f}s" if hs_ttk > 0 else "instant"
            hs_col = f"{hs_shots} ({hs_ttk_str})"
        else:
            hs_col = "unknown"

        lines.append(
            f"| {shield_name.capitalize()} "
            f"({shield['charge']} charge, {int(shield['mitigation']*100)}% mit) "
            f"| {body_shots} ({body_ttk_str}) | | {hs_col} | |"
        )

    lines += [
        "",
        "> Body TTK = time from first shot to kill. Headshot TTK assumes every shot is a headshot.",
        "> ARC armor penetration does not reduce player shield mitigation.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Weapon comparison
# ---------------------------------------------------------------------------

_WEAPON_TYPES = {"smg", "lmg", "assault rifle", "battle rifle", "hand cannon",
                 "pistol", "shotgun", "sniper rifle"}


@mcp.tool()
async def list_weapons(weapon_type: str = "") -> str:
    """
    List all weapons with key combat stats and sell value for comparison.
    Useful for "what's the best weapon" or "bang for buck" questions.

    weapon_type: assault rifle, battle rifle, SMG, LMG, shotgun,
                 sniper rifle, pistol, hand cannon (partial match, case-insensitive).
    Leave blank to list all weapon types.
    """
    all_items = await client.ardb_items()
    weapons = [
        i for i in all_items
        if i.get("type", "").lower() in _WEAPON_TYPES
        and (not weapon_type or weapon_type.lower() in i.get("type", "").lower())
    ]

    if not weapons:
        return f"No weapons found matching type '{weapon_type}'."

    # Fetch ARDB detail + wiki data for each weapon in parallel
    semaphore = asyncio.Semaphore(10)

    async def fetch_one(item: dict) -> tuple[dict, dict | None, dict | None]:
        async with semaphore:
            name = client.name_en(item) or item.get("id", "")
            detail, wiki = await asyncio.gather(
                client.ardb_item(item["id"]),
                client.wiki_weapon(name),
            )
            return item, detail, wiki

    results = await asyncio.gather(*[fetch_one(w) for w in weapons])

    # Group by weapon type for readability
    by_type: dict[str, list] = {}
    for stub, detail, wiki in results:
        specs = (detail or {}).get("weaponSpecs", {})
        stats = specs.get("stats", {})
        hs_mult = (wiki or {}).get("headshotmultiplier", "?")
        row = {
            "name": client.name_en(stub) or stub.get("id", "?"),
            "type": stub.get("type", "?").title(),
            "rarity": (stub.get("rarity") or "?").capitalize(),
            "damage": stats.get("damage", 0),
            "hs_mult": hs_mult,
            "fire_rate": stats.get("fireRate", 0),
            "range": stats.get("range", 0),
            "stability": stats.get("stability", 0),
            "armor_pen": specs.get("armorPenetration", "?"),
            "ammo": specs.get("ammoType", "?"),
            "value": stub.get("value", 0),
        }
        by_type.setdefault(row["type"], []).append(row)

    lines = ["# Weapons Overview", ""]

    for wtype in sorted(by_type):
        lines.append(f"## {wtype}")
        # Sort within type: primary sort by rarity tier, secondary by damage
        rarity_order = {"Common": 0, "Uncommon": 1, "Rare": 2, "Epic": 3, "Legendary": 4, "?": -1}
        rows = sorted(by_type[wtype], key=lambda r: (rarity_order.get(r["rarity"], -1), r["damage"]))
        for r in rows:
            lines.append(
                f"  - **{r['name']}** ({r['rarity']}) | "
                f"DMG {r['damage']} | HS {r['hs_mult']} | "
                f"FR {r['fire_rate']} | RNG {r['range']} | "
                f"STB {r['stability']} | Pen: {r['armor_pen']} | "
                f"{r['ammo']} ammo | Sell: {_coins(r['value'])}"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Map events
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_map_events(map_name: str = "") -> str:
    """
    Show current and upcoming map events for Arc Raiders maps.
    Data is sourced from arcraidershub.com and updated hourly.

    Maps: Dam, Buried City, Spaceport, Blue Gate, Stella Montis
    Leave map_name blank to show all maps at the current UTC hour.
    """
    data = await client.arcraidershub_map_events()
    if not data:
        return "Map event data unavailable."

    schedule = data.get("schedule", [])
    map_names = data.get("maps", ["Dam", "Buried City", "Spaceport", "Blue Gate", "Stella Montis"])

    now_utc = datetime.datetime.utcnow()
    current_hour = now_utc.hour

    # Build hour->entry lookup
    by_hour = {entry["hour"]: entry for entry in schedule if "hour" in entry}

    # Filter maps if requested
    filtered_maps = map_names
    if map_name:
        filtered_maps = [m for m in map_names if map_name.lower() in m.lower()]
        if not filtered_maps:
            return f"Map '{map_name}' not found. Available: {', '.join(map_names)}"

    lines = [
        f"## Map Events — {now_utc.strftime('%H:%M')} UTC",
        "",
    ]

    for map_key in filtered_maps:
        lines.append(f"### {map_key}")

        current = by_hour.get(current_hour, {}).get(map_key, {})
        major = current.get("major") or ""
        minor = current.get("minor") or ""

        if major:
            lines.append(f"  **Major:** {major}")
        if minor:
            lines.append(f"  **Minor:** {minor}")
        if not major and not minor:
            lines.append("  No event this hour.")

        # Next 3 upcoming non-empty hours
        upcoming = []
        for offset in range(1, 25):
            h = (current_hour + offset) % 24
            entry = by_hour.get(h, {}).get(map_key, {})
            nm = entry.get("major") or ""
            n_minor = entry.get("minor") or ""
            if nm or n_minor:
                label = " + ".join(filter(None, [nm, n_minor]))
                upcoming.append(f"    {h:02d}:00 UTC: {label}")
            if len(upcoming) >= 3:
                break

        if upcoming:
            lines.append("  Upcoming:")
            lines.extend(upcoming)

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Skill tree
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_skills(category: str = "") -> str:
    """
    List all skill tree nodes, optionally filtered by category.
    Shows each skill's description, max points, and what stat it impacts.

    Categories: Conditioning, Mobility, Survival (case-insensitive).
    Leave blank to list all three categories.
    """
    nodes = await client.raidtheory_skill_nodes()
    if not nodes:
        return "Skill data unavailable."

    if category:
        nodes = [n for n in nodes if category.lower() in n.get("category", "").lower()]
        if not nodes:
            return f"No skills found for category '{category}'. Try: Conditioning, Mobility, Survival."

    by_category: dict[str, list] = {}
    for node in nodes:
        cat = node.get("category", "?").title()
        by_category.setdefault(cat, []).append(node)

    lines = []
    for cat in sorted(by_category):
        lines.append(f"## {cat}")
        for node in by_category[cat]:
            name = node.get("name", {}).get("en", "?")
            desc = node.get("description", {}).get("en", "")
            impacted = node.get("impactedSkill", {}).get("en", "")
            max_pts = node.get("maxPoints", 1)
            known = node.get("knownValue", [])
            is_major = node.get("isMajor", False)
            prereqs = node.get("prerequisiteNodeIds", [])

            tag = " *(Major)*" if is_major else ""
            lines.append(f"  - **{name}**{tag} (max {max_pts} pts)")
            if desc:
                lines.append(f"    _{desc}_")
            if impacted:
                lines.append(f"    Improves: {impacted}")
            if known:
                lines.append(f"    Value: {', '.join(known)}")
            if prereqs:
                # Resolve prereq IDs to names
                id_to_name = {n["id"]: n.get("name", {}).get("en", n["id"]) for n in nodes}
                prereq_names = [id_to_name.get(p, p) for p in prereqs]
                lines.append(f"    Requires: {', '.join(prereq_names)}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Augments
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_augments(shield: str = "", sort_by: str = "") -> str:
    """
    List all augments with their inventory slots, weight limit, shield
    compatibility, and perk.

    shield: filter by shield type — Light, Medium, or Heavy (case-insensitive).
    sort_by: 'slots' to sort by main inventory count, 'weight' to sort by
             weight limit. Default: grouped by tier (Combat, Looting, Tactical).
    """
    augments = list(client.AUGMENTS)

    # Filter by shield compatibility
    if shield:
        shield_q = shield.strip().title()
        augments = [a for a in augments if shield_q in a["shields"]]
        if not augments:
            return f"No augments found compatible with '{shield}' shield."

    # Sort
    if sort_by.lower() == "slots":
        augments = sorted(augments, key=lambda a: a["slots"], reverse=True)
    elif sort_by.lower() == "weight":
        augments = sorted(augments, key=lambda a: a["weight_limit"], reverse=True)

    lines = ["## Augments"]
    if shield:
        lines[0] += f" (compatible with {shield.title()} shield)"
    if sort_by:
        lines[0] += f" — sorted by {sort_by}"

    for aug in augments:
        shields_str = ", ".join(aug["shields"])
        slot_str = f"{aug['slots']} slots  |  {aug['quick_use']} quick-use"
        if aug["safe_pocket"]:
            slot_str += f"  |  {aug['safe_pocket']} safe pocket"
        if aug["special"]:
            slot_str += f"  |  {aug['special']}"

        lines.append(f"\n### {aug['name']}")
        lines.append(f"**Weight limit:** {aug['weight_limit']} kg  |  **Shields:** {shields_str}")
        lines.append(f"**Inventory:** {slot_str}")
        if aug["perk"]:
            lines.append(f"**Perk:** {aug['perk']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_projects() -> str:
    """
    List all community projects with their current status and phase count.
    Projects are shared goals that require contributing items across multiple phases.

    Examples: Expedition, High-Gain Antenna, Weather Monitor System.
    Use get_project() for full phase requirements.
    """
    projects = await client.raidtheory_projects()
    if not projects:
        return "Project data unavailable."

    now = datetime.datetime.utcnow().timestamp()
    lines = ["## Community Projects", ""]

    for p in projects:
        if p.get("disabled"):
            continue
        name = p.get("name", {}).get("en", p.get("id", "?"))
        phases = p.get("phases", [])
        start = p.get("startDate")
        end = p.get("endDate")

        status_parts = []
        if start and end:
            if now < start:
                status_parts.append("upcoming")
            elif now > end:
                status_parts.append("ended")
            else:
                status_parts.append("active")
        status_str = f" [{', '.join(status_parts)}]" if status_parts else ""

        phase_str = f"{len(phases)} phase(s)" if phases else "no phase data"
        lines.append(f"  - **{name}**{status_str} — {phase_str}")

    return "\n".join(lines)


@mcp.tool()
async def get_project(name: str) -> str:
    """
    Get full details for a community project: description, all phases,
    and the items required for each phase.

    Examples: 'expedition', 'high-gain antenna', 'weather monitor'
    """
    projects = await client.raidtheory_projects()
    if not projects:
        return "Project data unavailable."

    active = [p for p in projects if not p.get("disabled")]
    match = client.find_best_match(name, active)
    if not match:
        names = [p.get("name", {}).get("en", p.get("id", "?")) for p in active]
        return f"Project '{name}' not found. Available: {', '.join(names)}"

    project_name = match.get("name", {}).get("en", match.get("id", "?"))
    desc = match.get("description", {}).get("en", "")
    phases = match.get("phases", [])

    now = datetime.datetime.utcnow().timestamp()
    start = match.get("startDate")
    end = match.get("endDate")

    lines = [f"## {project_name}"]

    if start and end:
        start_dt = datetime.datetime.utcfromtimestamp(start).strftime("%Y-%m-%d")
        end_dt = datetime.datetime.utcfromtimestamp(end).strftime("%Y-%m-%d")
        if now < start:
            lines.append(f"**Status:** Upcoming (starts {start_dt})")
        elif now > end:
            lines.append(f"**Status:** Ended ({end_dt})")
        else:
            lines.append(f"**Status:** Active (ends {end_dt})")

    if desc:
        lines += ["", f"_{desc}_"]

    if not phases:
        lines += ["", "_Phase requirement data not available for this project._"]
        return "\n".join(lines)

    # Fetch wiki rewards in parallel with phase rendering
    wiki = await client.wiki_project(project_name)
    phase_rewards = (wiki or {}).get("phase_rewards", {})
    wiki_url = (wiki or {}).get("url")

    if wiki_url:
        lines.append(f"_Wiki: {wiki_url}_")

    lines += [""]
    for phase in phases:
        phase_num = phase.get("phase", "?")
        phase_name = phase.get("name", {}).get("en", f"Phase {phase_num}")
        phase_desc = phase.get("description", {}).get("en", "")
        reqs = phase.get("requirementItemIds", [])

        lines.append(f"### Phase {phase_num}: {phase_name}")
        if phase_desc:
            lines.append(f"_{phase_desc}_")

        if reqs:
            phase_total = 0
            lines.append("")
            lines.append("**Required items:**")
            for req in reqs:
                item = await client.arcdata_item(req["itemId"])
                item_name = client.name_en(item) if item else req["itemId"]
                val = item.get("value", 0) if item else 0
                qty = req["quantity"]
                subtotal = val * qty
                phase_total += subtotal
                lines.append(f"  - {qty}x **{item_name}** ({_coins(subtotal)} value)")
            lines.append(f"  _Phase material value: {_coins(phase_total)}_")

        rewards = phase_rewards.get(phase_num, [])
        if rewards:
            lines.append("")
            lines.append("**Rewards:**")
            for r in rewards:
                lines.append(f"  - {r}")

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
