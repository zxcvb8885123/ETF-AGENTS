"""依明確日期與公開目錄補抓歷史新聞，保留實際取得時間。"""
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date,datetime,timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit,urljoin,quote,urlencode,parse_qs
from etf_agent.core import canonical_sha256
from etf_agent.perception.news_preparation import NewsMetadataParser
from .contracts import MarketNewsError,ARCHIVE_SOURCES

class _Links(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[]
    def handle_starttag(self,tag,attrs):
        value=dict(attrs)
        if tag=='a' and value.get('href'):self.links.append(value)

class MarketNewsHistoryProvider:
    """中央社台股／證券目錄，自由時報台股日期查詢；不搜尋任意主機。"""
    def __init__(self,opener=None):
        self.opener=opener or urllib.request.urlopen

    def _capture(self,url):
        url=quote(url,safe=':/?=&%');parsed=urlsplit(url)
        allowed=(parsed.scheme=='https' and not parsed.fragment and (
            parsed.hostname=='www.cna.com.tw' and (parsed.path in {'/tag/21566/','/list/asc.aspx'} or re.fullmatch(ARCHIVE_SOURCES['CNA_ARCHIVE']['path_pattern'],parsed.path))
            or parsed.hostname=='ec.ltn.com.tw' and re.fullmatch(ARCHIVE_SOURCES['LTN_ARCHIVE']['path_pattern'],parsed.path)
            or parsed.hostname=='search.ltn.com.tw' and parsed.path=='/list'))
        if not allowed:raise MarketNewsError('歷史目錄連結超出允許範圍')
        if parsed.hostname=='search.ltn.com.tw':
            query=parse_qs(parsed.query)
            if set(query)-{'keyword','start_time','end_time','sort','type','page'} or query.get('keyword')!=['台股'] or query.get('sort')!=['date'] or query.get('type')!=['all']:
                raise MarketNewsError('歷史目錄查詢參數不符')
        raw,status,error='','fetched',''
        try:
            with self.opener(urllib.request.Request(url,headers={'User-Agent':'ETF-Agent/0.1'}),timeout=20) as response:
                if urlsplit(response.geturl()).hostname!=parsed.hostname:raise MarketNewsError('歷史來源重新導向未允許主機')
                body=response.read(2_000_001)
                if len(body)>2_000_000:raise MarketNewsError('歷史來源回應過大')
                raw=body.decode('utf-8-sig')
        except (OSError,ValueError) as exc:status,error='failed',str(exc)
        return {'url':url,'raw_text':raw,'raw_sha256':canonical_sha256(raw),
                'available_at':datetime.now(timezone.utc).isoformat(),'status':status,'error':error}

    def collect(self,start_date,end_date):
        start,end=date.fromisoformat(start_date),date.fromisoformat(end_date)
        if start>end or (end-start).days>30 or end>datetime.now(timezone.utc).date():
            raise MarketNewsError('歷史查詢需為過去日期且範圍不超過三十一日')
        cna=[self._capture(url) for url in ['https://www.cna.com.tw/tag/21566/','https://www.cna.com.tw/list/asc.aspx']]
        query={'keyword':'台股','start_time':start.strftime('%Y%m%d'),'end_time':end.strftime('%Y%m%d'),'sort':'date','type':'all'}
        url='https://search.ltn.com.tw/list?'+urlencode(query)
        pages,visited=[],set();completed=False
        for _ in range(20):
            if url in visited:raise MarketNewsError('歷史目錄分頁循環')
            visited.add(url);row=self._capture(url);pages.append(row)
            if row['status']!='fetched':break
            parser=_Links();parser.feed(row['raw_text'])
            next_url=next((urljoin(url,l['href']) for l in parser.links if 'p_next' in l.get('class','').split()),None)
            if not next_url:completed=True;break
            url=quote(next_url,safe=':/?=&%')
            next_query=parse_qs(urlsplit(url).query)
            if any(next_query.get(k)!=[v] for k,v in query.items()):raise MarketNewsError('分頁改變查詢日期或條件')
        links={}
        for row in cna:
            if row['status']!='fetched':continue
            parser=NewsMetadataParser();parser.feed(row['raw_text'])
            for obj in parser.scripts:
                for value in obj if isinstance(obj,list) else [obj]:
                    if isinstance(value,dict) and value.get('@type')=='ItemList':
                        for item in value.get('itemListElement',[]):
                            match=re.search(r'/(\d{8})\d{4}\.aspx$',str(item.get('url','')))
                            if match and start.strftime('%Y%m%d')<=match.group(1)<=end.strftime('%Y%m%d'):
                                links[item['url']]='CNA_ARCHIVE'
        for row in pages:
            if row['status']!='fetched':continue
            parser=_Links();parser.feed(row['raw_text'])
            for link in parser.links:
                if 'tit' in link.get('class','').split() and urlsplit(link['href']).hostname=='ec.ltn.com.tw':
                    links[link['href']]='LTN_ARCHIVE'
        if len(links)>400:raise MarketNewsError('歷史新聞原頁數超過安全上限')
        with ThreadPoolExecutor(max_workers=4) as pool:
            captures=[dict(row,source=links[row['url']]) for row in pool.map(self._capture,sorted(links))]
        return {'start_date':start_date,'end_date':end_date,'query_keyword':'台股','listings':cna+pages,'captures':captures,
                'ltn_pagination_completed':completed,'article_count':len(captures),
                'scope_note':'中央社台股標籤／目前證券目錄與自由時報台股日期搜尋；不等於所有媒體所有新聞。'}
