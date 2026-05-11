"""
scraper.py — searches official state unclaimed property databases.
Results are written to /tmp/ufm_results.json (never committed to the repo).
People are loaded from SEARCH_PEOPLE env var (GitHub secret).
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright

ROOT        = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.json"
RESULTS_PATH = Path("/tmp/ufm_results.json")  # temp only, never committed

STATES = {
    "TN": {
        "name": "Tennessee",
        "base_url": "https://unclaimedproperty.tn.gov",
        "search_page": "https://unclaimedproperty.tn.gov/app/claim-search",
        "api_path": "/SWS/properties",
        "claim_url": "https://unclaimedproperty.tn.gov/app/claim-search",
    },
    "IA": {
        "name": "Iowa",
        "base_url": "https://www.greatiowatreasurehunt.gov",
        "search_page": "https://www.greatiowatreasurehunt.gov/app/claim-search",
        "api_path": "/SWS/properties",
        "claim_url": "https://www.greatiowatreasurehunt.gov/app/claim-search",
    },
    "TX": {
        "name": "Texas",
        "base_url": "https://www.claimittexas.gov",
        "search_page": "https://www.claimittexas.gov/app/claim-search",
        "api_path": "/SWS/properties",
        "claim_url": "https://www.claimittexas.gov/app/claim-search",
    },
    "FL": {
        "name": "Florida",
        "base_url": "https://www.fltreasurehunt.gov",
        "search_page": "https://www.fltreasurehunt.gov/app/claim-search",
        "api_path": "/SWS/properties",
        "claim_url": "https://www.fltreasurehunt.gov/app/claim-search",
    },
    "CA": {
        "name": "California",
        "base_url": "https://claimit.ca.gov",
        "search_page": "https://claimit.ca.gov/app/claim-search",
        "api_path": "/SWS/properties",
        "claim_url": "https://claimit.ca.gov/app/claim-search",
    },
}


def load_people():
    raw = os.environ.get("SEARCH_PEOPLE", "").strip()
    if not raw:
        print("ERROR: SEARCH_PEOPLE environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    try:
        people = json.loads(raw)
        if not isinstance(people, list):
            raise ValueError("SEARCH_PEOPLE must be a JSON array")
        return people
    except json.JSONDecodeError as e:
        print(f"ERROR: SEARCH_PEOPLE is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)


def load_config():
    if not CONFIG_PATH.exists():
        return {"states": list(STATES.keys())}
    with open(CONFIG_PATH) as f:
        return json.load(f)


async def search_person_state(browser, state_cfg, person):
    api_path    = state_cfg["api_path"]
    search_page = state_cfg["search_page"]

    all_results = []
    total_expected = [0]   # mutable so the response handler can update it
    response_received = asyncio.Event()
    response_error = [None]

    ctx = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        viewport={"width": 1280, "height": 800},
    )
    page = await ctx.new_page()

    async def on_response(response):
        if api_path not in response.url:
            return
        try:
            data = await response.json()
        except Exception:
            return
        if data.get("status") in (400, 401, 403, 500):
            response_error[0] = f"API status {data.get('status')}: {data.get('clientMessage','')}"
            response_received.set()
            return
        batch = data.get("content") or data.get("properties") or []
        all_results.extend(batch)
        total_expected[0] = data.get("totalElements") or data.get("total") or len(all_results)
        response_received.set()

    page.on("response", on_response)

    try:
        await page.goto(search_page, wait_until="domcontentloaded", timeout=30_000)
        # Give Turnstile time to complete its invisible challenge in the real browser session
        await page.wait_for_timeout(4_000)

        # Fill the search form and submit — the browser carries its own valid session/token
        ln = page.locator(
            'input[name="lastName"], input[placeholder*="Last"], '
            'input[formcontrolname="lastName"]'
        ).first
        await ln.fill(person["last_name"], timeout=5_000)

        try:
            fn = page.locator(
                'input[name="firstName"], input[placeholder*="First"], '
                'input[formcontrolname="firstName"]'
            ).first
            await fn.fill(person.get("first_name", ""), timeout=3_000)
        except Exception:
            pass

        btn = page.locator('button[type="submit"], button:has-text("Search")').first
        await btn.click(timeout=5_000)

        # Wait for the browser's own API response to come back
        try:
            await asyncio.wait_for(response_received.wait(), timeout=15)
        except asyncio.TimeoutError:
            raise RuntimeError("Timed out waiting for search results from API")

        if response_error[0]:
            raise RuntimeError(response_error[0])

        # Paginate by clicking Next until we have all results
        while len(all_results) < total_expected[0]:
            response_received.clear()
            try:
                next_btn = page.locator(
                    'button:has-text("Next"), [aria-label="Next page"], '
                    '.pagination-next, li.page-item:last-child button'
                ).first
                await next_btn.click(timeout=3_000)
                try:
                    await asyncio.wait_for(response_received.wait(), timeout=10)
                except asyncio.TimeoutError:
                    break
                if response_error[0]:
                    break
            except Exception:
                break

    except Exception as e:
        print(f"    Exception: {e}")
        raise
    finally:
        await ctx.close()

    return all_results


async def run(people, config):
    enabled_states = config.get("states", list(STATES.keys()))
    searches = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )

        for person in people:
            for code in enabled_states:
                if code not in STATES:
                    print(f"  Unknown state '{code}', skipping")
                    continue
                cfg      = STATES[code]
                name_str = f"{person.get('first_name','')} {person['last_name']}".strip()
                print(f"  Searching {cfg['name']} for {name_str}...")
                try:
                    props = await search_person_state(browser, cfg, person)
                    print(f"    → {len(props)} result(s)")
                    searches.append({
                        "person":      {"first_name": person.get("first_name",""), "last_name": person["last_name"]},
                        "state":       code,
                        "state_name":  cfg["name"],
                        "claim_url":   cfg["claim_url"],
                        "count":       len(props),
                        "results":     props,
                        "searched_at": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception as e:
                    print(f"    FAILED: {e}")
                    searches.append({
                        "person":      {"first_name": person.get("first_name",""), "last_name": person["last_name"]},
                        "state":       code,
                        "state_name":  cfg["name"],
                        "claim_url":   cfg["claim_url"],
                        "count":       0,
                        "results":     [],
                        "error":       str(e),
                        "searched_at": datetime.now(timezone.utc).isoformat(),
                    })

        await browser.close()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "searches":     searches,
    }


def main():
    print("=== Unclaimed Funds Monitor ===")
    people = load_people()
    config = load_config()
    states = config.get("states", list(STATES.keys()))
    print(f"Searching {len(people)} people × {len(states)} states = {len(people)*len(states)} searches\n")

    results = asyncio.run(run(people, config))

    # Write to /tmp only — never touches the repo
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults written to {RESULTS_PATH} (temp, not committed)")

    total = sum(s["count"] for s in results["searches"])
    errors = [s for s in results["searches"] if "error" in s]
    print(f"Total matches: {total}")
    for h in [s for s in results["searches"] if s["count"] > 0]:
        p = h["person"]
        print(f"  MATCH: {p.get('first_name','')} {p['last_name']} in {h['state_name']}: {h['count']} match(es)")
    if errors:
        print(f"\nERROR: {len(errors)} search(es) failed:", file=sys.stderr)
        for s in errors:
            p = s["person"]
            print(f"  {p.get('first_name','')} {p['last_name']} / {s['state_name']}: {s['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
