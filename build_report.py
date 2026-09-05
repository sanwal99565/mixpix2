import json

DATA_FILE = r'c:\Users\Chahat\Downloads\Telegram Desktop\demo\mixpix\endpoint_data.json'
REPORT_FILE = r'c:\Users\Chahat\Downloads\Telegram Desktop\demo\mixpix\endpoint_catalog.txt'

def parse_method_url(key):
    parts = key.split(' ', 1)
    return parts[0], parts[1]

def format_val(v, indent=0):
    prefix = "  " * indent
    lines = []
    if isinstance(v, dict):
        for k2, v2 in v.items():
            if isinstance(v2, (dict, list)):
                lines.append(f"{prefix}{k2}:")
                lines.extend(format_val(v2, indent + 1))
            else:
                vs = str(v2)
                if len(vs) > 300:
                    vs = vs[:300] + "..."
                lines.append(f"{prefix}{k2}: {vs}")
    elif isinstance(v, list):
        if len(v) > 0 and isinstance(v[0], str):
            lines.append(f"{prefix}{v}")
        else:
            for i, item in enumerate(v[:5]):
                lines.append(f"{prefix}[{i}]:")
                lines.extend(format_val(item, indent + 1))
            if len(v) > 5:
                lines.append(f"{prefix}... and {len(v) - 5} more")
    else:
        vs = str(v)
        if len(vs) > 300:
            vs = vs[:300] + "..."
        lines.append(f"{prefix}{vs}")
    return lines

with open(DATA_FILE, 'r', encoding='utf-8') as f:
    data = json.load(f)

api_endpoints = {}  # key -> data, for api.minipix.co
cdn_endpoints = {}  # for content.minipix.co (CDN)
other_endpoints = {}

for key, val in data.items():
    method, url = parse_method_url(key)
    if "api.minipix.co" in url:
        api_endpoints[key] = val
    elif "content.minipix.co" in url:
        cdn_endpoints[key] = val
    else:
        other_endpoints[key] = val

lines = []
a = lines.append

a("=" * 120)
a("MINIPIX ENDPOINT CATALOG - COMPREHENSIVE REPORT")
a("=" * 120)
a(f"Folders scanned: 1 to 260")
a(f"Total unique (method+URL) endpoints: {len(data)}")
a(f"  - API endpoints (api.minipix.co):   {len(api_endpoints)}")
a(f"  - CDN endpoints (content.minipix.co): {len(cdn_endpoints)}")
a(f"  - Other hosts:                      {len(other_endpoints)}")

# Group API endpoints by category
cats = {}
for key, val in api_endpoints.items():
    c = val['category']
    if c not in cats:
        cats[c] = []
    cats[c].append((key, val))

category_order = [
    "Login/OTP",
    "User/Profile",
    "Balance/Coin",
    "Quiz/Question",
    "Watch/Progress",
    "Series/Episode Listing",
    "Series/Episode",
    "Unlock/Subscription",
    "Campaign/Ads/Offers",
    "App/Config",
    "Other"
]

a("")
a("#" * 120)
a("## PART 1: API ENDPOINTS (api.minipix.co) - CATEGORIZED")
a("#" * 120)

for cat in category_order:
    if cat not in cats or len(cats[cat]) == 0:
        continue
    items = sorted(cats[cat], key=lambda x: -len(x[1]['folders']))
    a("")
    a("=" * 120)
    a(f"  CATEGORY: {cat.upper()}  ({len(items)} unique endpoint(s), "
      f"{sum(len(x[1]['folders']) for x in items)} total occurrences)")
    a("=" * 120)

    for idx, (key, val) in enumerate(items, 1):
        method, url = parse_method_url(key)
        folder_count = len(val['folders'])
        a("")
        a(f"  [{cat}] #{idx}: {method} {url}")
        a(f"    Occurrences : {folder_count} folder(s)")
        a(f"    Folders     : {sorted(val['folders'])}")
        if val.get('query_params'):
            a(f"    Query params: {sorted(val['query_params'])}")

        if val.get('request_bodies'):
            a(f"    Request body samples ({len(val['request_bodies'])}):")
            for bi, body in enumerate(val['request_bodies'], 1):
                a(f"      --- Sample #{bi} ---")
                for ln in format_val(body, 4):
                    a(ln)

        if val.get('response_samples'):
            a(f"    Response samples ({len(val['response_samples'])}):")
            for si, sample in enumerate(val['response_samples'], 1):
                code = sample.get('code') or '?'
                fields = sample.get('fields')
                a(f"      --- Sample #{si} [HTTP {code}] ---")
                for ln in format_val(fields, 4):
                    a(ln)

# Now handle CDN endpoints - group them
a("")
a("")
a("#" * 120)
a("## PART 2: CDN / MEDIA DELIVERY ENDPOINTS (content.minipix.co)")
a("#" * 120)

# Group CDN endpoints by pattern
cdn_groups = {}
for key, val in cdn_endpoints.items():
    method, url = parse_method_url(key)
    # Classify CDN URLs
    if "images/" in url:
        group = "Images (PNG/JPEG/etc)"
    elif "/shorts/" in url and "seg-" in url:
        group = "Short-form video DASH segments (.m4s)"
    elif "/shorts/" in url and "stream.mpd" in url:
        group = "Short-form video DASH manifest (.mpd)"
    elif "/shorts/" in url and "init.mp4" in url:
        group = "Short-form video init segments (init.mp4)"
    else:
        group = "Other CDN content"
    if group not in cdn_groups:
        cdn_groups[group] = []
    cdn_groups[group].append((key, val))

for group_name, items in cdn_groups.items():
    all_folders = sorted(set(f for _, v in items for f in v['folders']))
    a("")
    a(f"  --- {group_name} ---")
    a(f"    Unique URLs : {len(items)}")
    a(f"    Folders used: {sorted(all_folders)}")
    if len(items) <= 10:
        for key, val in items:
            method, url = parse_method_url(key)
            a(f"      {method} {url}  [folders: {val['folders']}]")
    else:
        # Show first 5 and last 5, count in between
        for key, val in items[:3]:
            method, url = parse_method_url(key)
            a(f"      {method} {url}  [folders: {val['folders']}]")
        a(f"      ... ({len(items) - 6} more similar segment URLs ...)")
        for key, val in items[-3:]:
            method, url = parse_method_url(key)
            a(f"      {method} {url}  [folders: {val['folders']}]")

# Summary stats
a("")
a("")
a("#" * 120)
a("## PART 3: SUMMARY STATISTICS")
a("#" * 120)
a("")
a("  API ENDPOINTS BY CATEGORY:")
a("  " + "-" * 60)
total_api = 0
total_api_occ = 0
for cat in category_order:
    if cat in cats:
        n = len(cats[cat])
        occ = sum(len(v[1]['folders']) for v in cats[cat])
        total_api += n
        total_api_occ += occ
        a(f"    {cat:<28s} {n:>3d} endpoints | {occ:>4d} occurrences")
a("  " + "-" * 60)
a(f"    {'API TOTAL':<28s} {total_api:>3d} endpoints | {total_api_occ:>4d} occurrences")
a("")
a("  CDN ENDPOINTS BY TYPE:")
a("  " + "-" * 60)
total_cdn = 0
for gn, items in sorted(cdn_groups.items()):
    n = len(items)
    occ = sum(len(v[1]['folders']) for v in items)
    total_cdn += n
    a(f"    {gn:<28s} {n:>3d} unique URLs | {occ:>4d} requests")
a("  " + "-" * 60)
a(f"    {'CDN TOTAL':<28s} {total_cdn:>3d} unique URLs")
a("")
a("  GRAND TOTAL:")
a(f"    Unique endpoints (all):   {len(data)}")
a(f"    Total request occurrences: {sum(len(v['folders']) for v in data.values())}")

# Master list of all API endpoints (quick reference)
a("")
a("")
a("#" * 120)
a("## PART 4: QUICK REFERENCE - ALL API ENDPOINTS (sorted by URL)")
a("#" * 120)
a("")
sorted_api = sorted(api_endpoints.items(), key=lambda x: parse_method_url(x[0])[1])
for key, val in sorted_api:
    method, url = parse_method_url(key)
    a(f"  [{val['category']:<25s}] {method:<6s} {url}")
    a(f"      Folders ({len(val['folders'])}): {sorted(val['folders'])}")

with open(REPORT_FILE, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(f"Report written to {REPORT_FILE}")
print(f"  - {len(api_endpoints)} API endpoints, {len(cdn_endpoints)} CDN endpoints")
print(f"  - {len(lines)} lines in report")
