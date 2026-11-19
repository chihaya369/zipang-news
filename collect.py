"""Build a compact, attributed event index from municipal RSS/Atom feeds."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

JST = timezone(timedelta(hours=9))
BASE = Path(__file__).resolve().parent
# The public feed uses factual topic/venue labels written for ZIPANG.
RULES = [
    ("温泉・足湯", [("足湯","足湯"),("温泉","温泉"),("銭湯","銭湯"),("サウナ","サウナ")]),
    ("歴史・文化", [("縄文","縄文"),("発掘","発掘調査"),("土器","土器"),("遺跡","遺跡"),("史跡","史跡"),("山城","山城"),("古写経","古写経"),("陶芸","陶芸"),("陶器","陶器"),("企画展","企画展示"),("特別展","特別展示"),("作品展","作品展示"),("博物館","博物館"),("美術展","美術展示"),("美術館","美術館")]),
    ("自然・旬の味", [("紅葉","紅葉"),("もみじ","紅葉"),("桜","桜"),("野鳥","野鳥観察"),("バードウォッチ","野鳥観察"),("バードウォー","野鳥観察"),("里山","里山"),("朝市","朝市"),("収穫","収穫"),("味覚","旬の食"),("花植え","花植え"),("グルメ","地元の食")]),
    ("祭り・おでかけ", [("花火","花火"),("祭り","祭り"),("まつり","まつり"),("音楽祭","音楽祭"),("ライトアップ","ライトアップ"),("フェスタ","地域フェスタ"),("体験イベント","体験イベント"),("ガイドツアー","ガイドツアー"),("散策","まち歩き"),("クルーズ","クルーズ"),("鉄道","鉄道"),("トレイン","鉄道"),("講座","講座")]),
]
VENUES = ("斎宮歴史博物館","東山動植物園","荒川豊蔵資料館","名古屋城本丸御殿","庄内緑地","蓬左文庫","なばなの里","文化のみち二葉館","文化のみち橦木館","名古屋市科学館","名古屋港","三重県総合博物館")
SKIP = re.compile("受付終了|募集.{0,3}終了|中止|延期|審議会|委員会|職員募集|選定結果|公募型|被害|被災|感染症|調達|入札")
def fetch(url):
    req = Request(url,headers={"User-Agent":"ZIPANG local news index/1.0","Accept":"application/rss+xml, application/atom+xml, application/xml, */*"})
    with urlopen(req,timeout=18) as resp:
        data = resp.read(2_000_001)
        if len(data)>2_000_000: raise ValueError("feed exceeds 2 MB")
        return data

def parse_date(raw):
    try:
        dt = datetime.fromisoformat(raw.replace("Z","+00:00"))
    except ValueError:
        try: dt=parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError): return None
    if dt is None: return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=JST)).astimezone(JST)

def tag(e): return e.tag.rsplit("}",1)[-1]

def event_day(fields,title,published):
    for key in ("startDate","start","dtstart"):
        value=parse_date(fields.get(key,""))
        if value: return value.date().isoformat()
    # Only a date explicitly described as the event day, never the posting date.
    text=unicodedata.normalize("NFKC",title)
    hit=re.search(r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日(?:[（(][月火水木金土日祝・]+[）)])?(?:に開催|開催)",text)
    if not hit: return ""
    year=int(hit[1]) if hit[1] else published.year
    try:
        day=datetime(year,int(hit[2]),int(hit[3]),tzinfo=JST).date()
        if not hit[1] and published.month>=10 and day.month<=3:
            day=day.replace(year=year+1)
        return day.isoformat()
    except ValueError: return ""

def parse(data, src, now):
    root=ET.fromstring(data)
    if tag(root) not in ("RDF","rss","feed"): raise ValueError("response is not an RSS/Atom feed")
    found={}
    for entry in root.iter():
        if tag(entry) not in ("item","entry"): continue
        fields={tag(el):(el.text or "").strip() for el in entry}
        title=fields.get("title","")
        if SKIP.search(title): continue
        words=[]; topics=[]
        for topic,rules in RULES:
            matches=list(dict.fromkeys(label for needle,label in rules if needle in title))
            if matches: topics.append(topic);words.extend(matches)
        if not topics: continue
        dt=parse_date(fields.get("pubDate") or fields.get("date") or fields.get("published") or fields.get("updated") or "")
        if not dt or not now-timedelta(days=45)<=dt<=now: continue
        link=fields.get("link") or next((e.get("href","") for e in entry if tag(e)=="link" and e.get("rel","alternate")=="alternate"),"")
        if not link: continue
        link=urldefrag(urljoin(src["feed"],link))[0]
        loc=urlsplit(link)
        if loc.scheme not in ("https","http") or loc.hostname!=urlsplit(src["feed"]).hostname: continue
        venue=next((v for v in VENUES if v in title),src["area"])
        descriptors=[word for word in dict.fromkeys(words) if word not in venue]
        subject="・".join(descriptors[:2]) or "催し"
        if "花火" in words and "クルーズ" in words: subject="花火を楽しむクルーズ"
        if "図書館" in title and "ガイドツアー" in words: subject="図書館見学"
        key=sha256(link.encode()).hexdigest()[:20]
        item=dict(id=key,sourceId=src["id"],source=src["source"],region=src["region"],pref=src["pref"],place=src["area"],
                  topics=topics,title=venue+"　"+subject,published=dt.isoformat(timespec="minutes"),url=link)
        item["day"]=event_day(fields,title,dt)
        if item["day"] and item["day"]<now.date().isoformat(): continue
        if key not in found or found[key]["published"]<item["published"]: found[key]=item
    return sorted(found.values(),key=lambda x:(x["published"],x["id"]),reverse=True)[:10]

def assemble(sources,previous,now,loader=fetch):
    stamp=now.isoformat(timespec="minutes");rows=[];statuses=[];good=0
    def one(s):
        try: return parse(loader(s["feed"]),s,now),None
        except Exception as e: return None,str(e)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for s,(items,error) in zip(sources,pool.map(one,sources)):
            checked=stamp
            if error:
                print(s["id"],error,file=sys.stderr)
                old=next((x for x in previous.get("sources",[]) if x["id"]==s["id"]),{})
                checked=old.get("checked","")
                t=parse_date(checked)
                items=[i for i in previous.get("items",[]) if i["sourceId"]==s["id"] and parse_date(i["published"]) and parse_date(i["published"])>=now-timedelta(days=45)] if t and t>=now-timedelta(days=2) else []
            else: good+=1
            statuses.append(dict(id=s["id"],name=s["source"],checked=checked,ok=error is None))
            rows.extend(items)
    rows.sort(key=lambda x:(x["published"],x["id"]),reverse=True)
    seen=set();items=[]
    for i in rows:
        if i["id"] not in seen: seen.add(i["id"]);items.append(i)
    picks=[];regions=set()
    for i in items:
        if i["region"] not in regions and parse_date(i["published"]) >= now-timedelta(days=7):
            picks.append(i["id"]);regions.add(i["region"])
    return dict(schema="zipang-news/1",updated=stamp,partial=good!=len(sources),sources=statuses,picks=picks,items=items),good

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--out",type=Path,default=BASE/"public/news.json");ap.add_argument("--previous",type=Path)
    args=ap.parse_args();prev=args.previous or args.out
    old=json.loads(prev.read_text(encoding="utf-8")) if prev.exists() else {}
    data,count=assemble(json.loads((BASE/"sources.json").read_text(encoding="utf-8")),old,datetime.now(JST))
    if not count: raise SystemExit("All sources failed; keep the published revision.")
    args.out.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.out.with_suffix(".tmp");tmp.write_text(json.dumps(data,ensure_ascii=False,separators=(",",":")),encoding="utf-8");tmp.replace(args.out)
    print(f"{count}/{len(data['sources'])} feeds, {len(data['items'])} cards")
if __name__=="__main__": main()

