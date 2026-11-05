"""Retail contribution by city segment (Top 40 / Next 40 / Rest of India): our retails against Jawa's Vahan registrations.

Vahan registrations come from the CLPL contribution site (state + city, by month). Its own RTO -> city mapping and its
BikeDekho -> Vahan city names are kept so a segment city is found under whichever spelling Vahan uses.
"""
import re, json, urllib.request

from payload import normalize_city

SITE = 'https://bd-contribution-site.vercel.app/clpl'
TIERS = ['Top 40 Cities', 'Next 40 Cities', 'Rest of India']
_MN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def _iso(txt):
    d, m, y = txt.split()
    return f"{int(y):04d}-{_MN.index(m[:3].title()) + 1:02d}-{int(d):02d}"


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
        txt = re.sub(r'<[^>]+>', ' ', h)
        mm = re.search(r'Data\s+(\d{1,2}\s+\w{3}\s+\d{4})\D{1,12}?(\d{1,2}\s+\w{3}\s+\d{4})\D{1,12}?Last updated\s+(\d{1,2}\s+\w{3}\s+\d{4})', txt)
        dates = [_iso(x) for x in mm.groups()] if mm else [None, None, None]
        out = {'asof': D.get('generated', ''), 'months': months, 'from': dates[0], 'through': dates[1], 'updated': dates[2],
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


def build_vahan(payload, vahan):
    """Block for the Vahan Gap tab: per state and per city, Vahan registrations, our leads (by lead month) and our retails
    (by the month attributed) for every Vahan month. Arrays per month, wrapped as [ICE, EV, XL100] (Jawa has ICE only)."""
    if not vahan: return None
    months = vahan['months']; nm = len(months); mi = {m: i for i, m in enumerate(months)}
    mkeys = [f"20{m[-2:]}-{_MN.index(m[:3]) + 1:02d}" for m in months]
    eff = {(st, c) for st, c, _ in vahan['cities']}
    states = {st.lower(): st for st, _, _ in vahan['cities']}
    bdmap = {}
    for bn, st, dc in vahan['bd']:
        bdmap.setdefault(bn.lower(), []).append((st, dc))
    mp = payload['maps']; lm, city, cst, stn = mp['lm'], mp['city'], mp['city_state'], mp['st']

    def locate(ci):
        name = city[ci]; ours = (stn[cst[ci]] if cst[ci] is not None else '').lower()
        c = bdmap.get(name.lower(), [])
        pick = next((x for x in c if x[0].lower() == ours), c[0] if c else None)
        if pick:
            st, dc = pick
            return (st, dc if (st, dc) in eff else 'Rest of ' + st)
        st = states.get(ours)
        if st: return (st, name if (st, name) in eff else 'Rest of ' + st)
        return None

    cache = {}
    def loc(ci):
        if ci not in cache: cache[ci] = locate(ci)
        return cache[ci]

    V, L, R = {}, {}, {}
    z = lambda: [0] * nm
    for st, c, v in vahan['cities']: V[(st, c)] = list(v)
    totL, totR = z(), z()
    for r in payload['cm']:                          # [city, lead month, leads, retails, DMS, Call Out]
        i = mi.get(lm[r[1]])
        if i is None: continue
        totL[i] += r[2]
        k = loc(r[0])
        if k: L.setdefault(k, z())[i] += r[2]
    for r in payload['u_cm']:                        # retails sit on the month attributed
        i = mi.get(lm[r[1]])
        if i is None or not r[3]: continue
        totR[i] += r[3]
        k = loc(r[0])
        if k: R.setdefault(k, z())[i] += r[3]
    three = lambda a: [list(a), z(), z()]
    totV = [sum(v[i] for v in V.values()) for i in range(nm)]
    ct = [[st, c, three(V.get((st, c), z())), three(L.get((st, c), z())), three(R.get((st, c), z()))]
          for st, c in sorted(set(V) | set(L) | set(R))]
    stt = {}
    for st, c, v, l, r in ct:
        e = stt.setdefault(st, [z(), z(), z()])
        for j in range(nm): e[0][j] += v[0][j]; e[1][j] += l[0][j]; e[2][j] += r[0][j]
    return {'months': months, 'mkeys': mkeys, 'from': vahan.get('from') or mkeys[0] + '-01', 'through': vahan.get('through') or mkeys[-1] + '-28',
            'updated': vahan.get('updated') or vahan.get('through') or mkeys[-1] + '-28', 'cap': {}, 'xlm': {},
            'tot': {'V': three(totV), 'L': three(totL), 'R': three(totR)},
            'st': [[st, three(v), three(l), three(r)] for st, (v, l, r) in sorted(stt.items())], 'ct': ct}
