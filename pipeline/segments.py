"""Retail contribution by city segment (Top 40 / Next 40 / Rest of India): our retails against Jawa's Vahan registrations.

Vahan registrations come from the CLPL contribution site (state + city, by month). Its own RTO -> city mapping and its
BikeDekho -> Vahan city names are kept so a segment city is found under whichever spelling Vahan uses.
"""
import re, json, urllib.request

from payload import normalize_city

SITE = 'https://bd-contribution-site.vercel.app/clpl'
TIERS = ['Top 40 Cities', 'Next 40 Cities', 'Rest of India']
_MN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def fetch_vahan(prev=None, log=print):
    """Compact copy of the site's data: months, Vahan registrations per (state, city), BikeDekho -> Vahan city names.
    Falls back to the previous run's copy when the site cannot be read."""
    try:
        req = urllib.request.Request(SITE, headers={'User-Agent': 'Mozilla/5.0 (compatible; jawa-dashboard-build)'})
        h = urllib.request.urlopen(req, timeout=90).read().decode('utf-8', 'replace')
        D = json.loads(re.search(r'<script[^>]*id="data"[^>]*>(.*?)</script>', h, re.S).group(1))
        bdc, rtos = D['bdc'], D['rtos']
        linked = {(b[0], b[2]) for b in bdc if not b[2].startswith('Rest of')}
        eff = [(r[0], r[3] if (r[0], r[3]) in linked else 'Rest of ' + r[0]) for r in rtos]
        nm = len(D['months'])
        regs = {}
        for i, mo, f, v in D['rv']:
            regs.setdefault(eff[i], [0] * nm)[mo] += v
        months = [f"{_MN[int(m[5:7]) - 1]}'{m[2:4]}" for m in D['months']]
        out = {'asof': D.get('generated', ''), 'months': months,
               'cities': [[st, c, v] for (st, c), v in sorted(regs.items())],
               'bd': [[b[1], b[0], b[2]] for b in bdc]}
        log(f"vahan: {sum(sum(v) for v in regs.values()):,} registrations, {len(regs)} cities, {months[0]}..{months[-1]}")
        return out
    except Exception as e:
        log(f"vahan: could not read {SITE} ({e}); " + ('using the previous copy' if prev else 'no data'))
        return prev


def tier_lookup(ct):
    """ct: {city (lower case, as written in the tier sheet): 'Top_40_Cities' | 'Next_40_Cities' | 'ROI'} -> name -> segment."""
    norm = {}
    for k, v in ct.items():
        seg = TIERS[0] if v.startswith('Top') else TIERS[1] if v.startswith('Next') else TIERS[2]
        norm[normalize_city(k).lower()] = seg
        norm[k.lower()] = seg
    def of(name):
        n = str(name or '').strip().lower()
        return norm.get(n) or norm.get(normalize_city(name).lower()) or TIERS[2]
    return of


def build_seg(payload, vahan, tier_of):
    """Our retails (by the month they were attributed) and Vahan registrations, per segment and month."""
    if not vahan: return None
    months = vahan['months']; mi = {m: i for i, m in enumerate(months)}
    # Vahan city -> segment: through the BikeDekho city names that map onto it, else the Vahan name itself
    names = {}
    for bn, st, dc in vahan['bd']:
        names.setdefault((st, dc), []).append(bn)
    vah = {t: [0] * len(months) for t in TIERS}
    ncity = {t: 0 for t in TIERS}
    for st, c, v in vahan['cities']:
        if c.startswith('Rest of'): t = TIERS[2]
        else:
            ts = [tier_of(n) for n in names.get((st, c), [])] + [tier_of(c)]
            t = next((x for x in ts if x != TIERS[2]), TIERS[2])
        ncity[t] += 1
        for i, x in enumerate(v): vah[t][i] += x
    ours = {t: [[0, 0, 0] for _ in months] for t in TIERS}
    mp = payload['maps']; lm, city = mp['lm'], mp['city']
    for r in payload['u_cm']:                      # [city, retail month, leads, retails, DMS, Call Out]; retails sit on the month attributed
        i = mi.get(lm[r[1]])
        if i is None or not r[3]: continue
        v = ours[tier_of(city[r[0]])][i]
        v[0] += r[3]; v[1] += r[4]; v[2] += r[5]
    return {'asof': vahan.get('asof', ''), 'months': months, 'tiers': TIERS, 'ours': ours, 'vahan': vah, 'cities': ncity}
