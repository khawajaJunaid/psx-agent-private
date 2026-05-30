"""Finite watchlist: names the agent may analyse (held + candidate ENTER targets).

Sector tags support thematic reasoning (e.g. reconstruction / infra demand
favouring cement; fertilizer policy; oil prices and E&P). The LLM still must
justify ENTER with price, filings, and news — sectors are context, not a buy list.
"""

WATCHLIST = {
    # --- currently held (from profile.yaml) ---
    "MEBL": "Meezan Bank",
    "BOP": "Bank of Punjab",
    "TGL": "Tariq Glass",
    "OGDC": "Oil and Gas Development Company",
    "WTL": "WorldCall Telecom",
    # --- banking (conventional + liquidity / policy themes) ---
    "HBL": "Habib Bank Limited",
    "UBL": "United Bank Limited",
    # --- cement (construction, floods / reconstruction, infra spend) ---
    "LUCK": "Lucky Cement",
    "DGKC": "D.G. Khan Cement",
    # --- fertilizer (crop cycle, subsidy, gas availability) ---
    "FFC": "Fauji Fertilizer Company",
    "EFERT": "Engro Fertilizers",
    # ENGRO (Engro Corp) — Yahoo often returns no .KA series; EPCL is Engro-group chemicals
    "EPCL": "Engro Polymer & Chemicals Limited",
    # --- E&P / OMC (oil price, exploration, circular debt narratives) ---
    "PPL": "Pakistan Petroleum Limited",
    "MARI": "Mari Petroleum Company",
    "PSO": "Pakistan State Oil",
    # --- power (tariff, capacity, fuel mix) ---
    "HUBC": "Hub Power Company",
    # --- pharma (healthcare, branded generics) ---
    "SEARL": "The Searle Company",
    "ABOT": "Abbott Laboratories Pakistan",
    # --- banking addition: strong dividend payer ---
    "MCB": "MCB Bank Limited",
    # --- growth / quality tilt ---
    "SYS": "Systems Limited",
}

# One sector label per ticker — used in prompts for macro / theme linking.
TICKER_SECTOR = {
    "MEBL": "Banking (Islamic)",
    "BOP": "Banking",
    "HBL": "Banking",
    "UBL": "Banking",
    "TGL": "Glass / industrial",
    "OGDC": "E&P",
    "WTL": "Telecom / cable",
    "LUCK": "Cement",
    "DGKC": "Cement",
    "FFC": "Fertilizer",
    "EFERT": "Fertilizer",
    "EPCL": "Chemicals (Engro group)",
    "PPL": "E&P",
    "MARI": "E&P",
    "PSO": "Oil marketing",
    "HUBC": "Independent power",
    "SYS": "IT / services",
    "SEARL": "Pharma",
    "ABOT": "Pharma",
    "MCB": "Banking",
}


def watchlist_sector_map_lines():
    """Compact multi-line string for the LLM user prompt (sector → tickers)."""
    from collections import defaultdict

    by = defaultdict(list)
    for t in sorted(WATCHLIST.keys()):
        by[TICKER_SECTOR.get(t, "Other")].append(t)
    return "\n".join(
        f"  - {sec}: {', '.join(sorted(tickers))}"
        for sec, tickers in sorted(by.items())
    )
