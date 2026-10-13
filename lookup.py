"""Domain -> HQ address + postcode. Google Places first, the brand's own site as fallback."""

import html
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

import requests

TIMEOUT = 15
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
SITE_PATHS = [
    "/policies/terms-of-service",
    "/policies/refund-policy",
    "/policies/privacy-policy",
    "/policies/contact-information",
    "/pages/contact",
    "/pages/contact-us",
    "/",
]
UK_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b")
US_ZIP = re.compile(
    r"\b(A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|"
    r"O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\s+(\d{5})(?:-\d{4})?\b"
)
SHOP_TYPES = {"store", "clothing_store", "shoe_store", "jewelry_store",
              "shopping_mall", "department_store", "furniture_store"}


def clean_domain(raw):
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    host = parsed.netloc or parsed.path
    return host.removeprefix("www.").split("/")[0].split(":")[0]


def same_site(url, domain):
    host = clean_domain(url)
    if not host:
        return False
    return host == domain or host.endswith("." + domain) or domain.endswith("." + host)


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and "text/html" in r.headers.get("Content-Type", ""):
            return r.text
    except requests.RequestException:
        pass
    return ""


def page_text(raw_html):
    raw_html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw_html)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", "\n", raw_html))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text))


def brand_from_title(homepage_html, domain=""):
    """Pick the part of the page title that looks most like the domain name."""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", homepage_html or "")
    if not m:
        return ""
    title = html.unescape(m.group(1)).strip()
    parts = [p.strip() for p in re.split(r"\s[|\-–—:]\s", title) if p.strip()]
    if not parts:
        return ""
    stem = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])

    def score(part):
        return SequenceMatcher(None, re.sub(r"[^a-z0-9]", "", part.lower()), stem).ratio()

    return max(parts, key=score) if stem else parts[0]


def validate_uk(postcode):
    try:
        r = requests.get("https://api.postcodes.io/postcodes/" + postcode.replace(" ", ""),
                         timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()["result"]["postcode"]
    except requests.RequestException:
        pass
    return None


def snippet_around(text, pos, span=120):
    start = max(0, pos - span)
    end = text.find("\n", pos)
    chunk = text[start:end if end != -1 else pos + 40].replace("\n", ", ")
    return re.sub(r"\s*,\s*(,\s*)+", ", ", chunk).strip(" ,")


# ---------- Google Places ----------

def google_places(queries, domain, api_key, region=None):
    """Try each query until a result's website matches the domain. Returns (result, calls_used)."""
    calls = 0
    for q in queries:
        body = {"textQuery": q, "pageSize": 5}
        if region:
            body["regionCode"] = region
        try:
            r = requests.post(
                "https://places.googleapis.com/v1/places:searchText",
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": "places.displayName,places.formattedAddress,"
                                        "places.addressComponents,places.websiteUri,places.types",
                },
                json=body,
                timeout=TIMEOUT,
            )
            calls += 1
            if r.status_code in (400, 401, 403):
                msg = r.json().get("error", {}).get("message", r.text[:200])
                raise RuntimeError(f"Google Places rejected the request: {msg}")
            r.raise_for_status()
        except requests.RequestException:
            continue

        for place in r.json().get("places", []):
            if not same_site(place.get("websiteUri"), domain):
                continue
            postcode = next((c.get("longText", "") for c in place.get("addressComponents", [])
                             if "postal_code" in c.get("types", [])), "")
            is_shop = bool(SHOP_TYPES & set(place.get("types", [])))
            return {
                "postcode": postcode,
                "address": place.get("formattedAddress", ""),
                "source": "Google Maps",
                "check_link": place.get("websiteUri", ""),
                "confidence": "Check" if is_shop or not postcode else "High",
                "notes": "Google listing looks like a shop, may not be HQ" if is_shop else "",
            }, calls
    return None, calls


# ---------- the brand's own website ----------

def from_website(domain, homepage_html):
    for path in SITE_PATHS:
        url = f"https://{domain}{path}"
        raw = homepage_html if path == "/" else fetch(url)
        if not raw:
            continue
        text = page_text(raw)

        uk = UK_POSTCODE.search(text.upper())
        if uk:
            valid = validate_uk(f"{uk.group(1)} {uk.group(2)}")
            if valid:
                return {
                    "postcode": valid,
                    "address": snippet_around(text, uk.start()),
                    "source": "Brand website",
                    "check_link": url,
                    "confidence": "High" if "/policies/" in path else "Check",
                    "notes": "UK postcode confirmed real",
                }

        us = US_ZIP.search(text)
        if us:
            return {
                "postcode": us.group(2),
                "address": snippet_around(text, us.start()),
                "source": "Brand website",
                "check_link": url,
                "confidence": "Check",
                "notes": f"US ZIP, {us.group(1)}",
            }
    return None


# ---------- one domain ----------

def find_hq(raw_domain, api_key=None, region=None, brand_hint=""):
    domain = clean_domain(raw_domain)
    row = {"domain": domain, "postcode": "", "address": "", "source": "",
           "confidence": "", "check_link": "", "notes": "", "google_calls": 0}
    if not domain or "." not in domain:
        row.update(source="Skipped", notes="Doesn't look like a domain")
        return row

    homepage = fetch(f"https://{domain}/")
    stem = domain.split(".")[0].replace("-", " ")
    queries = [q for q in dict.fromkeys(
        [brand_hint.strip(), brand_from_title(homepage, domain), domain, stem]) if q]

    result = None
    if api_key:
        result, row["google_calls"] = google_places(queries, domain, api_key, region)

    # Google gave a shop or no postcode: a policy-page address wins if there is one
    if result is None or result["confidence"] == "Check":
        site = from_website(domain, homepage)
        if site and (result is None or site["confidence"] == "High"):
            if result is not None:
                site["notes"] += "; Google only had a shop listing"
            result = site

    if result:
        row.update(result)
    else:
        row.update(source="Not found", check_link=f"https://{domain}/policies/terms-of-service",
                   notes="Not on Google Maps and no address on the site's policy pages")
    return row
