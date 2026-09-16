import re, json, time, hashlib
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
import fitz

BASE='https://www.meitetsu.co.jp'
INDEX=BASE+'/train/timetable/'
OUT=Path('data/timetables.json')
CACHE=Path('.cache/timetables')
CACHE.mkdir(parents=True, exist_ok=True)

TRAIN_RE=re.compile(r'^\d{2,5}[A-Za-z]{0,3}\*?$')
TIME_RE=re.compile(r'^(?:[01]?\d|2[0-3])[0-5]\d(?:[.:][0-5]\d)?$')
TIME4_RE=re.compile(r'^\d{3,4}$')
SKIP_LABELS={'列車番号','種別','行先','前のページ','始発','終着','記事','次のページ','備考','停車駅'}
TYPE_WORDS={'μS','μＳ','快特','特急','快急','急行','準急','普通'}

# Broad mapping for display; PDF title remains authoritative.
ROUTE_CODES={
 'NH':'名古屋本線','KG':'豊川線','GN':'西尾線・蒲郡線','MY':'三河線','TT':'豊田線・地下鉄鶴舞線',
 'TA':'常滑線・空港線・河和線・知多新線','CH':'築港線','ST':'瀬戸線','TB':'津島線','BS':'尾西線',
 'IY':'犬山線・各務原線','HM':'広見線','KM':'小牧線・地下鉄上飯田線','TH':'竹鼻線・羽島線'
}

def clean(s):
    return re.sub(r'\s+','',s or '').replace('　','').strip()

def norm_time(t):
    t=clean(t).replace('：',':')
    if re.fullmatch(r'\d{3,4}',t):
        if len(t)==3: t='0'+t
        return t[:2]+':'+t[2:]
    m=re.fullmatch(r'(\d{1,2})[:.](\d{2})(?::(\d{2}))?',t)
    if m:
        return f'{int(m.group(1)):02d}:{m.group(2)}'+(f':{m.group(3)}' if m.group(3) else '')
    return None

def is_time(t): return bool(norm_time(t))

def row_words(words, y, tol=3.5):
    return [w for w in words if abs(((w[1]+w[3])/2)-y)<=tol]

def center(w): return (w[0]+w[2])/2

def parse_page(page, meta):
    words=page.get_text('words')
    if not words: return []
    # Search for the header row containing 列車番号.
    label_words=[w for w in words if '列車番号' in clean(w[4])]
    if not label_words: return []
    hy=(label_words[0][1]+label_words[0][3])/2
    # Candidate train numbers near header row. Keep their x centers.
    candidates=[]
    for w in words:
        y=(w[1]+w[3])/2; txt=clean(w[4])
        if abs(y-hy)<=42 and TRAIN_RE.match(txt) and not txt.isdigit() or False:
            pass
        if abs(y-hy)<=42 and TRAIN_RE.match(txt):
            # avoid page numbers / years by requiring x away from station label column
            if center(w)>80:
                candidates.append(w)
    # de-duplicate by x and text
    trains=[]; seen=set()
    for w in sorted(candidates,key=lambda z:center(z)):
        key=(round(center(w),1),clean(w[4]).upper())
        if key in seen: continue
        seen.add(key); trains.append({'no':clean(w[4]).upper().replace('*',''),'x':center(w),'star':'*' in clean(w[4])})
    if not trains: return []
    # Find type/destination rows.
    def label_y(label):
        ws=[w for w in words if clean(w[4])==label]
        return (ws[0][1]+ws[0][3])/2 if ws else None
    ty=label_y('種別'); dy=label_y('行先')
    def value_at(y, x, window=22):
        if y is None: return ''
        ws=row_words(words,y,6)
        ws=[w for w in ws if abs(center(w)-x)<=window]
        if not ws: return ''
        return clean(min(ws,key=lambda w:abs(center(w)-x))[4])
    for tr in trains:
        tr['type']=value_at(ty,tr['x'])
        tr['destination']=value_at(dy,tr['x'])
    # Station/time rows. Use left-side text as station labels; skip header/footer labels.
    rows={}
    for w in words:
        y=round((w[1]+w[3])/2,1); txt=clean(w[4])
        if not txt or txt in SKIP_LABELS or txt in TYPE_WORDS: continue
        if center(w)>210: continue
        if TRAIN_RE.match(txt) or is_time(txt): continue
        # Avoid long explanatory text and page numbers.
        if len(txt)>18: continue
        # Japanese station-like text; also allow ASCII names.
        if not re.search(r'[一-龯ぁ-んァ-ヶ]',txt): continue
        rows.setdefault(y,[]).append(w)
    station_rows=[]
    for y,ws in rows.items():
        ws=sorted(ws,key=lambda w:w[0])
        # combine nearby words on the same row; choose the leftmost compact token.
        name=clean(''.join(w[4] for w in ws[:3]))
        if name in SKIP_LABELS or name in TYPE_WORDS: continue
        # For each train column, find a time-like word close to x at same y.
        vals=[]
        for tr in trains:
            rws=row_words(words,y,5)
            near=[w for w in rws if abs(center(w)-tr['x'])<=28]
            times=[]
            for q in near:
                nt=norm_time(q[4])
                if nt: times.append(nt)
            vals.append(times[0] if times else '')
        if any(vals): station_rows.append((y,name,vals))
    # Sort by page y. Remove duplicates with same station name and adjacent y.
    station_rows.sort(key=lambda z:z[0])
    ded=[]
    for r in station_rows:
        if ded and r[1]==ded[-1][1] and r[0]-ded[-1][0]<10:
            # Prefer row with more time values.
            if sum(bool(x) for x in r[2])>sum(bool(x) for x in ded[-1][2]): ded[-1]=r
        else: ded.append(r)
    out=[]
    for j,tr in enumerate(trains):
        stops=[]
        for _,station,vals in ded:
            if j<len(vals) and vals[j]: stops.append([station,vals[j]])
        if stops or tr.get('destination'):
            out.append({**tr,'stops':stops,'page':meta['page'],'pdf':meta['pdf'],'route':meta['route'],'day':meta['day'],'direction':meta['direction']})
    return out

def infer_meta(url, text=''):
    name=url.rsplit('/',1)[-1]
    m=re.search(r'20260314_([A-Z]+)_(W|H)(\d)',name)
    code=m.group(1) if m else ''
    day='weekday' if (m and m.group(2)=='W') else 'holiday' if m else ('weekday' if '平日' in text else 'holiday')
    direction='down' if '下り' in text else 'up' if '上り' in text else 'unknown'
    route=ROUTE_CODES.get(code,code or '名鉄')
    return route,day,direction

def main():
    s=requests.Session(); s.headers['User-Agent']='MeitetsuTimetableBuilder/1.0'
    html=s.get(INDEX,timeout=40).text
    soup=BeautifulSoup(html,'html.parser')
    links=[]
    for a in soup.find_all('a',href=True):
        href=a['href']
        if '.pdf' in href.lower():
            u=urljoin(BASE,href)
            if u not in [x[0] for x in links]: links.append((u,clean(a.get_text(' ',strip=True))))
    records=[]
    for n,(url,label) in enumerate(links,1):
        fn=hashlib.sha1(url.encode()).hexdigest()+'.pdf'; path=CACHE/fn
        try:
            if not path.exists() or path.stat().st_size<1000:
                r=s.get(url,timeout=60); r.raise_for_status(); path.write_bytes(r.content)
            doc=fitz.open(path)
            for pi,page in enumerate(doc,1):
                text=page.get_text()[:1200]
                route,day,direction=infer_meta(url,text)
                meta={'page':pi,'pdf':url,'route':route,'day':day,'direction':direction}
                for rec in parse_page(page,meta): records.append(rec)
            doc.close()
            print(f'[{n}/{len(links)}] {url}')
        except Exception as e:
            print('ERROR',url,e)
    # Merge records with same number/day/direction. Pages are continuations.
    merged={}
    for r in records:
        key=(r['no'],r['day'],r['direction'])
        if key not in merged:
            merged[key]={k:r.get(k) for k in ['no','type','destination','route','day','direction','pdf','star']}
            merged[key]['stops']=[]
        m=merged[key]
        if r.get('type') and not m.get('type'): m['type']=r['type']
        if r.get('destination') and not m.get('destination'): m['destination']=r['destination']
        for st,t in r.get('stops',[]):
            if not any(x[0]==st and x[1]==t for x in m['stops']): m['stops'].append([st,t])
    data={'generatedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'source':INDEX,'timetableRevision':'2026-03-14','trains':list(merged.values())}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')))
    print('wrote',OUT,'trains',len(data['trains']))

if __name__=='__main__': main()
