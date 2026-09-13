# How to Reverse-Engineer a Shopping Site's API (Step-by-Step Guide)

(see `API.md` in this repo for the concrete worked example). It takes you from
"here's a URL whose products I want" to "a no-browser Python client hitting the
site's JSON API".

**Tooling:** a browser with DevTools network panel (or the `chrome-devtools` MCP),
`curl`, Python. The final deliverable needs none of these except Python + `requests`.

---

## Phase 0 — Frame the target

1. Record the **exact page URL** including query string. Every query param is a
   hint about an API parameter (`store=604`, `x1=ast-id-level-1&q1=DC0000001`,
   `sort=bestseller+desc`).
2. Write down what you need: which items, which fields (price, stock, rating,
   image, link), how many, how often, and whether data is **store/region/account
   scoped** (clearance often is).

## Phase 1 — Load the page and capture traffic

1. Navigate the browser to the target URL.
2. **Wait for real content.** E-commerce pages sit on skeleton loaders for a long
   time; don't conclude anything from a half-loaded page. Wait until prices/SKUs
   appear (up to 20–30 s).
3. List network requests, **filter to `xhr` + `fetch`** — removes ~95% noise.
4. Ignore tracking/analytics domains: `googletagmanager`, `google-analytics`,
   `doubleclick.net`, `adobedtm`, `cdn.cookielaw.org` (OneTrust), `evergage`,
   `braze`, `gigya` (login SDK), `curalate`, `fonts`, CDN assets, `akamai`.
5. Look for requests to the site's **own origin** whose paths contain
   `api`, `search`, `product`, `graphql`, `/v1/`–`/v3/`. That's the data layer.

Identify which architecture you have:

| Pattern | Signal | Meaning |
|---|---|---|
| **A. Client-side fetch** | Grid starts as skeleton; an XHR returns JSON; DOM fills in | The XHR *is* the API — replay it directly |
| **B. Server-side render** | First HTML already contains products (grep for SKUs, `"price"`, `application/ld+json`) | The HTML is the API (scrape it), but it usually embeds JSON state that reveals the real API too |

If the page is stuck on a skeleton: check console errors. The data call may be
gated on cookie consent / geolocation / a login SDK that fails in headless mode.
Usually the API itself doesn't need what the page was waiting for.

## Phase 2 — Mine the page HTML for API config

E-commerce sites ship their backend config to the browser. Save the initial HTML
response and grep for:

- `api`, `endpoint`, `apim`, `baseUrl`, `apiUrl`
- `key`, `token`, `subscription`, `secret`
- `window.…=`, `data-config`, `data-…` attributes,
  `<script id="__NEXT_DATA__">`, `window.__NUXT__`, Apollo/Redux state

Watch for **HTML-entity-encoded JSON** (quotes `&#34;`) inside `data-...="..."`
attributes: extract, `html.unescape()`, then `json.loads()`.

Canadian Tire's `data-configs` attribute yielded:
```
apim-domain          = https://www.canadiantire.ca/api
apim-subscriptionkey = c01ef3612328420c9f5cd9277e815a0e   ← public API key
apimEndPointSearch   = /v1/search/v2/search
```
~180 endpoints, base URL, and the API key in one blob. Look for a **search**
endpoint first — that's what powers grids on category/promo pages.

Also mine the config + nav/banner content for **category IDs**: Canadian Tire's
mega-nav blob had `categoriesL1` arrays mapping names to `categoryId` codes
(`Home → DC0000001`, `Automotive → DC0000006`, ...). These IDs are the `q1=`
values you need.

## Phase 3 — Replay the API (curl first, then bisect headers)

1. Try a minimal call: the guessed endpoint + discovered key + a Chrome
   User-Agent.
   - `200` JSON → done, skip to Phase 4.
   - `403` / HTML error page → it's behind a bot manager (Akamai, Cloudflare,
     PerimeterX…). Go to step 2.
2. **Add browser-like headers one group at a time** until it passes:
   `Referer` + `Origin`, `Accept-Language`, `sec-ch-ua` / `sec-ch-ua-mobile` /
   `sec-ch-ua-platform`, `sec-fetch-dest/mode/site`, cookies.
3. **Distinguish "bad headers" from "flagged IP":** if even a plain GET of the
   site's homepage starts returning 403, your IP is temporarily flagged
   (bursts of rapid requests trigger this). Back off several minutes. Test
   again from the *browser context* (in-page `fetch`, which has real cookies and
   fingerprint) to confirm the API call shape itself is correct.
4. Bot-manager cookie flow that worked: a `requests.Session` that first does a
   normal page GET (collects Akamai/OneTrust cookies), then calls the API with
   the full header set, small delay between calls.

## Phase 4 — Map URL params → API params

Now enumerate every parameter the site itself uses:

1. **Query params on the target URL** are facet params: `x1`/`q1` = category
   level + ID, `store` = store, `sort` = sort key.
2. **Facet values in the response carry ready-made URLs.** Each value in
   `facets[]` / `sortBy[]` / `pagination` includes a `url` string you can re-query.
   Copy those URLs and diff them against yours — the new query params reveal the
   filter vocabulary, e.g. `x2=deals&q2=CLEARANCE`, `x2=availability&q2=store_available`.
3. **Probe pagination/page-size params** (`page`, `count`, `rows`, `size`,
   `pageSize`). Send big values on purpose: `count=500` returning HTTP 400 vs
   `count=200` returning 200 reveals the documented max. `rows`/`size` were
   silently ignored (stuck at 10/page) — only `count` worked.
4. **Use echo fields to confirm semantics.** The response included an
   `eventData.filter` string showing the exact server-side filter DSL
   (`... AND inventory(604, attributes.clearanceBadge)=true`). Debug gold.

## Phase 5 — Document the schema

1. Dump one full item + the envelope: `json.dumps(...)[:3000]`, then list
   top-level keys and product keys.
2. Record per-field: name, type (str/int/float/bool/null/object/array),
   meaning, example. Include units and edge cases (family products where price
   lives in `minPrice`/`maxPrice`, `null` "was" price when not discounted).
3. Note identifiers: which field is the stable SKU/code to track over time.
4. Save a couple of raw JSON responses in the repo as fixtures for offline dev.
5. Note the anti-bot rules you learned (headers, warm-up, backoff, request-rate
   ceiling) in a dedicated section.

## Phase 6 — Build the typed client (Python + uv)

1. One file, **PEP 723 inline metadata**:
   ```python
   # /// script
   # requires-python = ">=3.10"
   # dependencies = ["requests>=2.32"]
   # ///
   ```
   Run: `uv run client.py` — no venv to manage, deps embedded in the file.
2. Structure: dataclasses for `Price` / `Product` / envelope; a `Session`
   with the full browser headers; `warmup()` → page GET for cookies; a
   `fetch()` with **retry + exponential backoff on 403**; `count`/`page`
   params; polite `time.sleep()` between requests.
3. Output: print a readable table AND write a dated snapshot
   (`data/YYYY-MM-DD.json` + `.csv`) so day-over-day diffs (new/dropped/price
   changed) are trivial.

## Phase 7 — Verify against the real page

1. Run the client and compare its top items to what the page/browser actually
   showed (same SKUs, same order). For Canadian Tire the top-5 matched exactly.
2. Re-run on the next day to confirm the data changes are real (not caching).

## Phase 8 — Automate safely

1. Once/day via cron or a systemd timer; log output to a file.
2. Keep request volume minimal (1 warm-up + 1 API call for the top-20 case).
3. Never hammer on errors — on 403 sleep 30–60 s, max 3 attempts.
4. Respect the site: read `robots.txt`, check Terms of Service for API/ToS
   restrictions, keep volume human-scale, don't resell the data, add a
   descriptive User-Agent. This is reverse-engineering for personal automation
   — stay reasonable so you don't get IP-banned or violate the site's terms.

---

## Checklist of common platform signatures

| Platform | Tell-tale | Data layer to look for |
|---|---|---|
| Next.js | `<script id="__NEXT_DATA__">` | `/_next/data/<build>/...json` or `/api/...` |
| Nuxt | `window.__NUXT__` | `/_nuxt/...` + API calls |
| Remix | `window.__remixContext` | loader fetches |
| Gatsby | `window.___GATSBY` | `/page-data/.../page-data.json` |
| Shopify | `/products.json`, `/collections/all/products.json`, storefront GraphQL | unauthenticated storefront JSON |
| Algolia-backed | requests to `*.algolia.net` | InstantSearch — instant results API |
| Adobe AEM (what CT uses) | `data-configs`, `etc.clientlibs` | `/api/...` with subscription key |
| Generic SPA | skeleton grid + XHR | the `/v1/...` or `/graphql` XHR |

**Golden rules**
- The page's own URLs are your API docs — copy their query strings.
- Facet/sort/pagination URLs inside responses teach you new parameters for free.
- Public API keys embedded in page config are common and fine for personal
  automation — but treat them as public (don't leak secrets that *aren't*
  public; these keys are meant for the browser).
- If a request fails, the two suspects are *headers* and *IP reputation* —
  bisect headers first, then test from the browser to isolate.
- When in doubt, a text snapshot of the accessibility tree (not a screenshot)
  tells you what actually rendered.
