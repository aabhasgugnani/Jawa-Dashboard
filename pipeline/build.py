#!/usr/bin/env python3
"""
Jawa performance dashboard: daily build from Google Drive.

  python build.py --inputs <dir> --live <live.json> --sources <sources.json> --out <dir>

  inputs/   (decrypted private bundle) frozen.json, bridge.json, mappings.json, shell.html, app.js,
            clpl_retails.json, city_tiers.csv
  live.json running archive of Drive leads (so a month never disappears when a rolling sheet is
            overwritten) plus the mobile ids handed out so far. Read and rewritten each run.
  sources   {"leads":[{"label":..,"id":..,"required":bool}], "retail":{"id":..,"gid":"0"}}

History rules
  * Lead months up to Apr'26 are frozen (validated earlier from the FY master; Drive has no Apr'26).
  * Lead months from May'26 come from the Drive "JAWA" tabs, keyed by enquiryId.
  * Retails come from the retail attribution sheet (Process = Jawa), matched by enquiryId.
  * Customer mobiles are encrypted in the sheets; they are only used as salted hashes for de-duplication.
"""
import sys, os, re, io, csv, json, time, hashlib, argparse, datetime, urllib.request, urllib.parse
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from make_client import make_client
import gzip
from payload import build_payload, build_daily
from segments import fetch_vahan, tier_lookup, build_seg

MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
LIVE_FROM = "May'26"
ALIAS = {'Yezdi Scrambler 350': 'Yezdi Scrambler', 'Yezdi Scrambler (2022-2025)': 'Yezdi Scrambler',
         '2024 Yezdi Adventure': 'Yezdi Adventure', '2025 Yezdi Adventure': 'Yezdi Adventure',
         '2024 Yezdi Roadster': 'Yezdi Roadster'}
BAD = {'None', 'nan', 'NaN', '#N/A', 'NULL'}
csv.field_size_limit(1 << 24)


def s(v):
    t = '' if v is None else str(v).strip()
    return '' if t in BAD else t
def mkey(m): return int(m[-2:]) * 12 + MON.index(m[:3])
def norm_month(v):
    t = s(v)
    m = re.match(r"^([A-Za-z]{3})[a-z]*'?\s?(?:20)?(\d{2})$", t)           # Oct'2026 / Oct'26
    if m and m.group(1).title() in MON: return f"{m.group(1).title()}'{m.group(2)}"
    m = re.match(r'^(\d{4})-(\d{2})', t)                                   # 2026-10-13
    if m and 1 <= int(m.group(2)) <= 12: return f"{MON[int(m.group(2)) - 1]}'{m.group(1)[2:]}"
    return ''
def norm_dealer(c): return re.sub(r'[\s_]+$', '', s(c))
def log(*a): print(*a, flush=True)


# ---------------------------------------------------------------- fetch
def fetch_csv(sheet_id, sheet_name=None, gid=None, tries=5):
    if sheet_name:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
    else:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid or 0}"
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=300) as r:
                raw = r.read()
            text = raw.decode('utf-8-sig')
            if text.lstrip()[:1] == '<': raise RuntimeError('got HTML (sheet not shared or tab missing)')
            return list(csv.DictReader(io.StringIO(text)))
        except Exception as e:
            err = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f'fetch failed after {tries} tries: {type(err).__name__}: {err}')


# ---------------------------------------------------------------- parse
def parse_leads(rows, salt):
    """Drive JAWA tab rows -> compact lead records keyed by enquiryId."""
    out = {}
    for r in rows:
        eid = s(r.get('enquiryId'))
        if not eid: continue
        date = s(r.get('Date'))[:10]
        lm = norm_month(r.get('Lead_Month')) or norm_month(date)
        if not lm: continue
        mob = s(r.get('encrypt_mobile_number'))
        model = s(r.get('model')); model = ALIAS.get(model, model) or 'Unspecified'
        dealer = norm_dealer(r.get('oem_crm_id')) or norm_dealer(r.get('verified_dealer'))
        rec = [lm, model, dealer, s(r.get('Dealer_Name')), s(r.get('City')), s(r.get('State')), s(r.get('Status_Name')),
               hashlib.sha256((salt + mob).encode()).hexdigest()[:20] if mob else '', date or 'Unknown', s(r.get('updateTime')),
               s(r.get('lead_type')) or 'Unknown', s(r.get('Medium')), s(r.get('utm_campaign'))]
        old = out.get(eid)
        if old is None or rec[9] > old[9]: out[eid] = rec
    return out


def parse_retails(rows):
    out = {}
    for r in rows:
        if s(r.get('Process')) != 'Jawa': continue
        eid = s(r.get('enquiryId'))
        if not eid: continue
        model = s(r.get('purchasedModel')); model = ALIAS.get(model, model)
        pmo = norm_month(r.get('Performance Month')) or norm_month(r.get('performanceMonth'))
        pm = norm_month(r.get('Retail Attribution Month')) or norm_month(r.get('Retail_Attribution_Date')) or pmo
        rec = {'model': model, 'pm': pm, 'pmo': pmo, 'ad': s(r.get('Retail_Attribution_Date'))[:10], 'src': 'DMS' if s(r.get('Call Type')).upper() == 'DMS' else 'VOC',
               'cm': norm_month(r.get('Create Month')) or norm_month(r.get('createTime')),
               'org': s(r.get('Organization')), 'att': s(r.get('Retail_Attribution_Date')), 'rd': s(r.get('Retail_Date'))[:10]}
        old = out.get(eid)
        if old is None or rec['att'] > old['att']: out[eid] = rec
    return out


MEDIUM_FALLBACK = {'Organic': 'Organic', 'Facebook': 'Facebook', 'Google': 'Google', 'Non-MS': 'Non CPS', 'Whatsapp': 'WhatsApp'}
def derive_source(medium, lt, camp, table):
    """Source as the FY master labels it. The sheets only carry Medium, so this extends the master's own attribution to
    new leads by matching on (Medium, lead type, campaign) learned from leads present in both (about 90% agreement)."""
    for k in (f"{medium}|{lt}|{camp}", f"{medium}|{lt}"):
        if k in table: return table[k]
    return MEDIUM_FALLBACK.get(medium, medium or 'Unknown')


def status_class(st, mp):
    if st in mp: return mp[st]
    t = st.lower()
    cat = 'Lost' if t.startswith('lost') else ('Converted' if ('book' in t or t.startswith('retail')) else 'Active')
    return [cat, 'Not Determined']


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--inputs', required=True); ap.add_argument('--live', required=True)
    ap.add_argument('--sources', required=True); ap.add_argument('--out', required=True)
    ap.add_argument('--offline', help='dir of pre-downloaded CSVs named <label>.csv and retail.csv (testing)')
    A = ap.parse_args()
    I = lambda n: os.path.join(A.inputs, n)
    src = json.load(open(A.sources)); bridge = json.load(open(I('bridge.json')))
    maps = json.load(open(I('mappings.json'))); frozen = json.load(open(I('frozen.json')))
    salt = bridge['salt']; mob_idx = dict(bridge['map'])
    live = json.load(open(A.live)) if os.path.exists(A.live) else {'leads': {}, 'newmob': {}}
    mob_idx.update(live.get('newmob', {}))
    next_idx = max([bridge['next_idx']] + [v + 1 for v in mob_idx.values()])

    # ---- 1. leads from Drive, merged into the running archive
    leads = {k: v for k, v in live.get('leads', {}).items()}
    log(f"archive carried in: {len(leads):,} leads")
    for sh in src['leads']:
        try:
            if A.offline: rows = list(csv.DictReader(open(os.path.join(A.offline, sh['label'] + '.csv'), encoding='utf-8')))
            else: rows = fetch_csv(sh['id'], sheet_name='JAWA')
        except Exception as e:
            if sh.get('required'): sys.exit(f"required sheet '{sh['label']}' failed: {e}")
            log(f"  WARN sheet '{sh['label']}' skipped: {e}"); continue
        got = parse_leads(rows, salt); n = 0
        for eid, rec in got.items():
            if mkey(rec[0]) < mkey(LIVE_FROM): continue
            old = leads.get(eid)
            if old is None or rec[9] >= old[9]: leads[eid] = rec; n += 1
        log(f"  {sh['label']:10} {len(rows):7,} rows -> {n:7,} leads kept/updated")
    if not leads: sys.exit('no live leads at all; refusing to publish')

    # ---- 2. retails
    rs = src['retail']
    try:
        rrows = list(csv.DictReader(open(os.path.join(A.offline, 'retail.csv'), encoding='utf-8'))) if A.offline \
            else fetch_csv(rs['id'], gid=rs.get('gid'))
    except Exception as e:
        sys.exit(f'retail sheet failed: {e}')
    retails = parse_retails(rrows)
    log(f"retail sheet: {len(rrows):,} rows, {len(retails):,} Jawa retails")

    # ---- 3. dealers
    ct = {}
    for r in csv.DictReader(open(I('city_tiers.csv'), encoding='utf-8')): ct[s(r['city']).lower()] = s(r['Mapping'])
    dealers_all = frozen['dealers']; fmeta = {}
    for d, m in zip(dealers_all, frozen['dealerMeta']):
        k = norm_dealer(d)
        if k not in fmeta or (fmeta[k][4] is None and m[4] is not None): fmeta[k] = m
    latest = {}
    for rec in sorted(leads.values(), key=lambda x: x[9]): latest[rec[2]] = rec

    # ---- 4. assemble every row as a dict
    allrows = []
    F = frozen
    for r in F['rows']:       # [lm, model, dealer, sc, q, cs, isRetail, mob, date, retailModel, perfMonth, retailSource]
        allrows.append({'lm': F['months'][r[0]], 'model': F['models'][r[1]], 'dealer': norm_dealer(F['dealers'][r[2]]),
                        'sc': F['statuscats'][r[3]], 'q': F['qualstatus'][r[4]], 'cs': F['currstatus'][r[5]], 'mob': r[7],
                        'date': F['dates'][r[8]], 'rm': F['retailModels'][r[9]] if r[9] >= 0 else '',
                        'pm': F['months'][r[10]] if r[10] >= 0 else '', 'rsrc': {0: 'DMS', 1: 'VOC'}.get(r[11], '') if r[6] else '',
                        'lid': 'F%d' % len(allrows), 'src': ('WhatsApp' if F['sources'][r[12]] == 'CTWA' else F['sources'][r[12]]) if len(r) > 13 else 'Unknown',
                        'lt': F['leadtypes'][r[13]] if len(r) > 13 else 'Unknown', 'rd': '', 'ad': ''})
    used = set(); nlive = 0
    for eid, rec in leads.items():
        lm, model, dealer, dname, city, state, st, mh, date, upd = rec[:10]
        ltype, medium, camp = (rec[10:13] + ['', '', ''])[:3] if len(rec) >= 13 else ('Unknown', '', '')
        if mh:
            if mh not in mob_idx: mob_idx[mh] = next_idx; live.setdefault('newmob', {})[mh] = next_idx; next_idx += 1
            mob = mob_idx[mh]
        else:
            mob = next_idx; next_idx += 1                                 # no mobile: never merge with another lead
        cat, q = status_class(st, maps['status'])
        row = {'lm': lm, 'model': model, 'dealer': dealer, 'sc': cat, 'q': q, 'cs': st, 'mob': mob, 'date': date,
               'rm': '', 'pm': '', 'rsrc': '', 'lid': eid, 'src': derive_source(medium, ltype, camp, maps.get('source', {})),
               'lt': ltype, 'rd': '', 'ad': '', 'dname': dname, 'city': city, 'state': state}
        rt = retails.get(eid)
        if rt:
            used.add(eid)
            row.update(rm=rt['model'] or model, pm=rt['pm'], rsrc=rt['src'], rd=rt['rd'], ad=rt['ad'])
        allrows.append(row); nlive += 1
    # retails whose lead is not in Drive: new-month ones become retail-only rows, older ones belong to frozen leads
    orphan_new = orphan_frozen = 0; fro_rets = []
    name2code = {m[0].strip().lower(): k for k, m in fmeta.items()}
    for eid, rt in retails.items():
        if eid in used: continue
        if rt['cm'] and mkey(rt['cm']) >= mkey(LIVE_FROM):
            code = name2code.get(rt['org'].strip().lower(), 'UNKNOWN')
            if code == 'UNKNOWN' and 'UNKNOWN' not in fmeta: fmeta['UNKNOWN'] = [rt['org'] or 'Unknown dealer', 'Unknown', 'Unknown', 'ROI', None, 0]
            allrows.append({'lm': rt['cm'], 'model': rt['model'] or 'Unspecified', 'dealer': code, 'sc': 'Converted',
                            'q': 'Qualified', 'cs': 'Retail Attribution', 'mob': next_idx, 'date': 'Unknown',
                            'rm': rt['model'], 'pm': rt['pm'], 'rsrc': rt['src'], 'lid': eid, 'src': 'Unknown', 'lt': 'Unknown',
                            'rd': rt['rd'], 'ad': rt['ad']})
            next_idx += 1; orphan_new += 1
        else:
            orphan_frozen += 1; fro_rets.append(rt)

    # Leads up to Apr'26 are frozen without enquiry ids, so their retails are re-bucketed on the attribution month by
    # matching each sheet retail to a frozen retailed lead (lead month + performance month + purchased model + dealer).
    cand = defaultdict(list)
    for x in allrows:
        if x['lid'].startswith('F') and x['rm'] and x['pm'] and mkey(x['pm']) >= mkey("Apr'26"):
            dn = (fmeta.get(x['dealer']) or [''])[0].strip().lower()
            for k in ((x['lm'], x['pm'], x['rm'], dn), (x['lm'], x['pm'], x['rm']), (x['lm'], x['pm'])):
                cand[k].append(x)
    taken, done, mstats = set(), set(), Counter()
    for tier in (4, 3, 2):
        for rt in fro_rets:
            if id(rt) in done: continue
            k = (rt['cm'], rt['pmo'], rt['model'], rt['org'].strip().lower())[:tier]
            for x in cand.get(k, ()):
                if id(x) in taken: continue
                taken.add(id(x)); done.add(id(rt)); x['pm'] = rt['pm']; x['ad'] = rt['ad']; x['rd'] = rt['rd']
                mstats[tier] += 1; break
    log(f"frozen-lead retails re-bucketed on attribution month: {len(done)} of {len(fro_rets)} sheet rows (by match tier {dict(mstats)})")
    log(f"live leads {nlive:,}; retail-only rows {orphan_new}; retails on frozen months (already in frozen data, not re-added) {orphan_frozen}")

    # ---- 5. encode
    months = sorted({x['lm'] for x in allrows} | {x['pm'] for x in allrows if x['pm']}, key=mkey)
    def enc(vals): v = sorted(set(vals)); return v, {x: i for i, x in enumerate(v)}
    models, mo = enc(x['model'] for x in allrows); scs, sc = enc(x['sc'] for x in allrows)
    qs, qu = enc(x['q'] for x in allrows); css, cu = enc(x['cs'] for x in allrows)
    dates, da = enc(x['date'] for x in allrows); rms, rmi = enc(x['rm'] for x in allrows if x['rm'])
    dealers, di = enc(x['dealer'] for x in allrows); mi = {m: i for i, m in enumerate(months)}
    meta = []; newd = []
    for dc in dealers:
        if dc in fmeta: meta.append(fmeta[dc]); continue
        l = latest.get(dc)
        if l: meta.append([l[3] or dc, l[4] or 'Unknown', l[5] or 'Unknown', ct.get((l[4] or '').lower(), 'ROI'), None, 0]); newd.append(dc)
        else: meta.append([dc, 'Unknown', 'Unknown', 'ROI', None, 0])
    rows = []
    for x in allrows:
        isr = 1 if x['rm'] else 0
        rows.append([mi[x['lm']], mo[x['model']], 0, 0, di[x['dealer']], sc[x['sc']], qu[x['q']], cu[x['cs']], isr, x['mob'],
                     da[x['date']], rmi[x['rm']] if isr else -1, mi[x['pm']] if (isr and x['pm']) else -1,
                     {'DMS': 0, 'VOC': 1}.get(x['rsrc'], 2) if isr else 2])
    DATA = {'months': months, 'models': models, 'sources': ['Total'], 'leadtypes': ['All'], 'statuscats': scs, 'qualstatus': qs,
            'currstatus': css, 'dealers': dealers, 'dealerMeta': meta, 'dates': dates, 'rows': rows, 'retailModels': rms,
            'retailSources': ['DMS', 'VOC', '']}

    # ---- 5b. payload for the New view (TVS-format lead-disposition payload)
    def dealer_info(x):
        m = fmeta.get(x['dealer'])
        if m: return m[0], m[1], m[2]
        return x.get('dname') or x['dealer'], x.get('city') or 'Unknown', x.get('state') or 'Unknown'
    p_leads, p_ret = [], {}
    for x in allrows:
        dn, ci, stt = dealer_info(x)
        p_leads.append({'lid': x['lid'], 'lm': x['lm'], 'src': x['src'], 'lt': x['lt'], 'mdl': x['model'], 'st': stt, 'city': ci,
                        'cd': x['date'], 'dl': dn, 'sn': x['cs']})
        if x['rm']:
            p_ret[x['lid']] = {'rtype': 'DMS' if x['rsrc'] == 'DMS' else 'Call Out', 'rm': x['pm'], 'pm': x['rm'], 'rd': x.get('rd', ''), 'ad': x.get('ad', '')}
    built_iso = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=5, minutes=30)).isoformat()
    payload = build_payload(p_leads, p_ret, built_iso)
    # Unique-leads version: one enquiry per customer (mobile) per lead month, as the Old view does with a single month
    # selected: the retailing enquiry if there is one, else the earliest created. A customer who enquires in two
    # different months counts once in each of them.
    by_mob = {}
    for x, pl in zip(allrows, p_leads):
        by_mob.setdefault((x['mob'], x['lm']), []).append((x, pl))   # unique per customer per lead month
    u_leads, u_ret = [], {}
    for lst in by_mob.values():
        rep_x, rep_pl = next(((x, pl) for x, pl in lst if x['rm']), None) or min(lst, key=lambda t: t[0]['date'])
        u_leads.append(rep_pl)
        if rep_x['rm']: u_ret[rep_x['lid']] = p_ret[rep_x['lid']]
    payload_u = build_payload(u_leads, u_ret, built_iso)
    daily, daily_u = build_daily(p_leads, p_ret, built_iso), build_daily(u_leads, u_ret, built_iso)
    payload['uq'], payload_u['uq'] = 0, 1
    # CLPL segments (Top 40 / Next 40 / Rest of India): our retails vs Vahan registrations
    live['vahan'] = fetch_vahan(live.get('vahan'), log)
    tier_of = tier_lookup(ct)
    payload['seg'], payload_u['seg'] = build_seg(payload, live['vahan'], tier_of), build_seg(payload_u, live['vahan'], tier_of)
    log(f"new-view unique payload: {len(u_leads):,} customers, {len(u_ret):,} retailed")
    log(f"new-view payload: {len(p_leads):,} leads, {len(p_ret):,} retails, ageing {payload['ram_meta']}")

    # ---- 6. render (client copy: Source / Lead Type already collapsed above)
    shell = open(I('shell.html'), encoding='utf-8').read(); app = open(I('app.js'), encoding='utf-8').read()
    clpl = {k: v for k, v in json.load(open(I('clpl_retails.json'))).items() if not k.startswith('_')}
    a = app.index('const CLPL_RETAILS = {'); b = app.index('};', a) + 2
    app = app[:a] + 'const CLPL_RETAILS = ' + json.dumps(clpl) + ';' + app[b:]
    today = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=5, minutes=30)
    period = f"{months[0]}–{months[-1]}"
    shell, n = re.subn(r"Jawa Performance Dashboard \(May'25–[A-Z][a-z]{2}'\d\d\)", f"Jawa Performance Dashboard ({period})", shell)
    assert n >= 1, 'title pattern not found in shell.html'
    shell = shell.replace("May'25 to Jun'26<br>", f"{months[0]} to {months[-1]}<br>").replace('Source: Master Lead File', 'Source: Bike CPS LMS sheets')
    app, n = re.subn(r"document\.getElementById\('genDate'\)\.textContent = [^\n]*",
                     "document.getElementById('genDate').textContent = " + json.dumps('Data as of ' + today.strftime('%d %b %Y, %H:%M IST')) + ';', app, count=1)
    assert n == 1, 'genDate line not found in app.js'
    ch, cdata, capp = make_client(shell, DATA, app)
    auth_on = os.environ.get('JAWA_AUTH', '').lower() != 'off'
    build_id = today.strftime('%Y%m%d%H%M%S'); built = today.strftime('%d %b %H:%M IST')
    repo = os.environ.get('GITHUB_REPOSITORY', 'yashtayal-a11y/Jawa-Dashboard')
    rjs = (open(os.path.join(HERE, 'refresh.js'), encoding='utf-8').read().replace('__BUILD_ID__', build_id).replace('__BUILT__', built)
           .replace('__RUN_URL__', f'https://github.com/{repo}/actions/workflows/deploy.yml').replace('__AUTH_ON__', 'true' if auth_on else 'false'))
    tjs = open(os.path.join(HERE, 'toggle.js'), encoding='utf-8').read()
    REFRESH = '<script>\n' + rjs + '\n</script>\n<script>\n' + tjs + '\n</script>\n'
    data_js = json.dumps(cdata, separators=(',', ':'))
    if not auth_on:                                            # an outer login (Cloudflare Access) is in place
        html = ch + '<script>\nconst DATA = ' + data_js + ';\n</script>\n<script>\n' + capp + '\n</script>\n' + REFRESH + '</body>\n</html>'
    else:
        # the dashboard script is kept inert (text/plain) and started by auth.js once a user is signed in
        auth = open(os.path.join(HERE, 'auth.js'), encoding='utf-8').read()
        html = (ch + '<script>\nconst DATA = ' + data_js + ';\n</script>\n'
                '<script type="text/plain" id="jawa-app">\n' + capp.replace('</script', '<\\/script') + '\n</script>\n'
                '<script>\n' + auth + '\n</script>\n' + REFRESH + '</body>\n</html>')
    os.makedirs(A.out, exist_ok=True)
    open(os.path.join(A.out, 'client.html'), 'w', encoding='utf-8').write(html)
    os.makedirs(os.path.join(A.out, 'data'), exist_ok=True)
    with gzip.open(os.path.join(A.out, 'data', 'jawa_payload.json.gz'), 'wt', encoding='utf-8', compresslevel=6) as fh:
        json.dump(payload, fh, separators=(',', ':'))
    with gzip.open(os.path.join(A.out, 'data', 'jawa_payload_unique.json.gz'), 'wt', encoding='utf-8', compresslevel=6) as fh:
        json.dump(payload_u, fh, separators=(',', ':'))
    for nm, dd_ in (('jawa_daily', daily), ('jawa_daily_unique', daily_u)):
        with gzip.open(os.path.join(A.out, 'data', nm + '.json.gz'), 'wt', encoding='utf-8', compresslevel=6) as fh:
            json.dump(dd_, fh, separators=(',', ':'))
    newapp = open(os.path.join(HERE, 'new_app.html'), encoding='utf-8').read().replace('__BUILD_ID__', build_id)
    open(os.path.join(A.out, 'new.html'), 'w', encoding='utf-8').write(newapp)

    live['leads'] = leads
    json.dump(live, open(A.live, 'w'), separators=(',', ':'))

    # ---- report
    agg = defaultdict(lambda: [0, 0, 0, 0])
    for r in rows:
        agg[months[r[0]]][0] += 1
        if r[8]:
            agg[months[r[0]]][1] += 1; k = months[r[12]] if r[12] >= 0 else '(no perf month)'
            agg[k][2] += 1; agg[k][3] += r[13] == 0
    log(f"\nTOTAL leads {len(rows):,} | unique mobiles {len({r[9] for r in rows}):,} | retails {sum(r[8] for r in rows):,} | dealers {len(dealers)} | models {len(models)}")
    if newd: log(f"new dealers (OEM tier = Unclassified): {len(newd)}")
    log(f"{'Month':14}{'Leads':>9}{'Ret(lead mo)':>14}{'Ret(perf mo)':>14}{'DMS':>7}")
    for m in sorted(agg, key=lambda m: mkey(m) if "'" in m else 99999):
        log(f"{m:14}{agg[m][0]:9,}{agg[m][1]:14,}{agg[m][2]:14,}{agg[m][3]:7,}")
    json.dump({'built_ist': today.strftime('%Y-%m-%d %H:%M'), 'leads': len(rows), 'months': months[-1]},
              open(os.path.join(A.out, 'summary.json'), 'w'))
    json.dump({'id': build_id, 'built': built}, open(os.path.join(A.out, 'version.json'), 'w'))


if __name__ == '__main__':
    main()
