"""重建 RSS 全量輸入及自由整合的研究結論，無新聞篇數投票。"""
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from etf_agent.core import canonical_sha256, parse_aware_time
from .contracts import MarketNewsError, SOURCES, ARCHIVE_SOURCES, NEWS_SOURCES, seal, check_fields

CAPTURE_FIELDS = {'source','url','raw_text','raw_sha256','available_at','status','error'}
LABEL_FIELDS = {'item_id','canonical_content_id','relevance','stance','reason'}
PACK_FIELDS = {'schema_version','snapshot_id','decision_cutoff','window_start','captures','labels','synthesis','source_approvals','content_sha256'}

def prepare(captures, window_start, decision_cutoff):
    if not isinstance(captures, list): raise MarketNewsError('RSS 原始回應必須是陣列')
    start = parse_aware_time(window_start,'window_start',error=MarketNewsError)
    cutoff = parse_aware_time(decision_cutoff,'decision_cutoff',error=MarketNewsError)
    if start > cutoff: raise MarketNewsError('查詢開始時間晚於截止時間')
    items, gaps, seen_sources = [], [], set()
    for capture in captures:
        check_fields(capture, CAPTURE_FIELDS, 'RSS 原始回應')
        source = capture['source']
        if source in ARCHIVE_SOURCES:
            parsed = urlsplit(capture['url'])
            if parsed.scheme != 'https' or parsed.hostname not in ARCHIVE_SOURCES[source]['hosts'] or not re.fullmatch(ARCHIVE_SOURCES[source]['path_pattern'], parsed.path) or parsed.query or parsed.fragment:
                raise MarketNewsError('歷史新聞原頁來源不符')
            source_key = (source, capture['url'])
        elif source in SOURCES and capture['url'] == SOURCES[source]['url']:
            source_key = source
        else:
            raise MarketNewsError('新聞來源不符')
        if source_key in seen_sources: raise MarketNewsError('新聞來源回應重複')
        seen_sources.add(source_key)
        available = parse_aware_time(capture['available_at'],'available_at',error=MarketNewsError)
        if available > cutoff: raise MarketNewsError('RSS 取得時間晚於截止時間')
        if not isinstance(capture['raw_text'], str) or capture['raw_sha256'] != canonical_sha256(capture['raw_text']): raise MarketNewsError('RSS 原始內容遭修改')
        if capture['status'] == 'failed':
            gaps.append(NEWS_SOURCES[source]['publisher'] + '來源擷取失敗：' + capture['url']); continue
        if capture['status'] != 'fetched': raise MarketNewsError('RSS 取得狀態無效')
        if source in ARCHIVE_SOURCES:
            from etf_agent.perception.news_preparation import extract_news_metadata
            metadata = extract_news_metadata(capture['raw_text'])
            if not metadata['published_at'] or not metadata['headline']:
                raise MarketNewsError('歷史新聞缺少帶時區發布時間或標題')
            published = parse_aware_time(metadata['published_at'], 'published_at', error=MarketNewsError)
            if published > available: raise MarketNewsError('歷史新聞發布時間晚於取得時間')
            if not start <= published <= cutoff: continue
            title = metadata['headline']
            content_id = canonical_sha256({'title':re.sub(r'\s+', '', title),'day':published.date().isoformat()})
            items.append({'item_id':'market-news:' + canonical_sha256({'source':source,'link':capture['url']})[:24],
                          'canonical_content_id':content_id,'source':source,'publisher':NEWS_SOURCES[source]['publisher'],
                          'title':title,'summary':metadata['description'] or '', 'link':capture['url'],
                          'published_at':published.isoformat(),'available_at':capture['available_at'],
                          'content_extent':'page_title_and_description'})
            continue
        if '<!DOCTYPE' in capture['raw_text'].upper() or '<!ENTITY' in capture['raw_text'].upper():
            raise MarketNewsError('RSS 含不允許的 XML 宣告')
        try: root = ET.fromstring(capture['raw_text'])
        except ET.ParseError as exc: raise MarketNewsError('RSS 格式錯誤') from exc
        rows = root.findall('./channel/item')
        if root.tag != 'rss' or not rows: raise MarketNewsError('RSS 缺少新聞項目')
        for row in rows:
            title, link, raw_time = [(row.findtext(key) or '').strip() for key in ('title','link','pubDate')]
            description = row.findtext('description') or ''
            if not title or urlsplit(link).scheme != 'https' or urlsplit(link).hostname not in SOURCES[source]['hosts']:
                raise MarketNewsError('RSS 新聞標題或原文連結不符')
            try: published = parse_aware_time(parsedate_to_datetime(raw_time).isoformat(),'published_at',error=MarketNewsError)
            except (ValueError, TypeError) as exc: raise MarketNewsError('RSS 發布時間缺少時區或格式錯誤') from exc
            if published > available: raise MarketNewsError('RSS 新聞發布時間晚於取得時間')
            if not start <= published <= cutoff: continue
            # 只用 RSS 文字；去 HTML 後仍保存原始 RSS，摘要不冒充全文。
            summary = re.sub('<[^>]+>', '', description).strip()
            normalized = re.sub(r'\s+', '', title)
            content_id = canonical_sha256({'title':normalized,'day':published.date().isoformat()})
            item_id = 'market-news:' + canonical_sha256({'source':source,'link':link})[:24]
            items.append({'item_id':item_id,'canonical_content_id':content_id,'source':source,
                          'publisher':SOURCES[source]['publisher'],'title':title,'summary':summary,'link':link,
                          'published_at':published.isoformat(),'available_at':capture['available_at'],
                          'content_extent':'rss_title_and_summary'})
    ids = [row['item_id'] for row in items]
    if len(ids) != len(set(ids)): raise MarketNewsError('RSS 含重複新聞識別，需先修正來源')
    return {'items':items,'data_gaps':gaps,'decision_cutoff':decision_cutoff,'window_start':window_start,
            'coverage':'本次 RSS 與指定歷史目錄原頁在查詢視窗內的全部取得項目；取得範圍由來源及查詢紀錄界定。'}

def evaluate(pack):
    check_fields(pack, PACK_FIELDS, '市場新聞資料包')
    if pack['schema_version'] not in {'market-news-1.0', 'market-news-1.1'} or not isinstance(pack['snapshot_id'],str) or not pack['snapshot_id']:
        raise MarketNewsError('市場新聞資料包身分無效')
    if seal(pack) != pack: raise MarketNewsError('市場新聞資料包雜湊不符')
    if not isinstance(pack['labels'], list) or not isinstance(pack['source_approvals'], list):
        raise MarketNewsError('標籤與來源核對必須是陣列')
    legacy = pack['schema_version'] == 'market-news-1.0'
    if legacy and any(c.get('source') in ARCHIVE_SOURCES for c in pack['captures']):
        raise MarketNewsError('歷史原頁接入需要資料包格式 1.1')
    prepared = prepare(pack['captures'],pack['window_start'],pack['decision_cutoff'])
    items = {r['item_id']:r for r in prepared['items']}
    labels, groups = {}, {}
    for row in pack['labels']:
        check_fields(row,LABEL_FIELDS,'市場新聞標籤')
        key = row['item_id']
        if key not in items or key in labels: raise MarketNewsError('新聞標籤引用不存在或重複')
        if row['canonical_content_id'] != items[key]['canonical_content_id']: raise MarketNewsError('新聞標籤內容識別不符')
        if row['relevance'] not in {'market','company','unrelated','ambiguous'} or row['stance'] not in {'positive','negative','neutral','mixed'}:
            raise MarketNewsError('新聞相關性或看法無效')
        if not isinstance(row['reason'],str) or not row['reason'].strip(): raise MarketNewsError('新聞標籤缺少判讀理由')
        previous = groups.setdefault(row['canonical_content_id'],(row['relevance'],row['stance']))
        if previous != (row['relevance'],row['stance']): raise MarketNewsError('重複內容標籤不一致')
        labels[key] = row
    if set(labels) != set(items): raise MarketNewsError('新聞標籤未覆蓋查詢視窗全部項目')
    synthesis = pack['synthesis']
    check_fields(synthesis,{'outlook','findings'},'市場綜合判讀')
    if synthesis['outlook'] not in {'positive','neutral','negative','unknown'}: raise MarketNewsError('市場綜合方向無效')
    if not isinstance(synthesis['findings'],list): raise MarketNewsError('市場發現必須是陣列')
    relevant = {k for k,l in labels.items() if l['relevance'] == 'market'}
    for row in synthesis['findings']:
        check_fields(row,{'text','evidence_ids'},'市場發現')
        if not isinstance(row['text'],str) or not row['text'].strip() or not isinstance(row['evidence_ids'],list) or not row['evidence_ids'] or not set(row['evidence_ids']) <= relevant:
            raise MarketNewsError('市場發現只能引用整體市場新聞')
    if synthesis['outlook'] != 'unknown' and (not relevant or not synthesis['findings']): raise MarketNewsError('市場結論缺少相關證據')
    approvals = {}
    for row in pack['source_approvals']:
        check_fields(row,{'source','license_status','terms_url','usage_scope','reviewed_at','evidence'},'來源使用核對')
        if row['source'] not in NEWS_SOURCES or row['source'] in approvals or row['terms_url'] != NEWS_SOURCES[row['source']]['terms_url']:
            raise MarketNewsError('來源使用核對身分無效')
        if row['license_status'] not in {'approved','unverified','restricted'}: raise MarketNewsError('來源使用狀態無效')
        if parse_aware_time(row['reviewed_at'],'reviewed_at',error=MarketNewsError) > parse_aware_time(pack['decision_cutoff'],'decision_cutoff'):
            raise MarketNewsError('來源使用核對晚於截止時間')
        if row['license_status']=='approved' and (row['usage_scope']!='competition_research' or not isinstance(row['evidence'],str) or not row['evidence'].strip()):
            raise MarketNewsError('核准來源缺少比賽研究使用範圍與授權依據')
        approvals[row['source']]=row
    unique = {items[k]['canonical_content_id'] for k in relevant}
    sources = {items[k]['source'] for k in relevant}
    gaps = list(prepared['data_gaps'])
    for source in sorted(sources):
        if approvals.get(source,{}).get('license_status') != 'approved':
            if legacy:
                gaps.append(SOURCES[source]['publisher']+'已取得 RSS 標題及前言；比賽研究使用權尚未確認，暫只作診斷。')
            elif source in ARCHIVE_SOURCES:
                gaps.append(NEWS_SOURCES[source]['publisher']+'歷史新聞已取得；原頁標題與摘要的比賽研究使用範圍尚未確認，未納入正式計算。')
            else:
                gaps.append(NEWS_SOURCES[source]['publisher']+'RSS 新聞已取得；比賽研究使用範圍尚未確認，未納入正式計算。')
    if len(unique)<5: gaps.append('整體市場相關新聞不足五則獨立內容，正式情緒樣本不足。')
    publishers = {NEWS_SOURCES[s]['publisher'] for s in sources}
    if len(publishers)<2: gaps.append('整體市場相關新聞未涵蓋兩個獨立來源。' if legacy else '整體市場相關新聞未涵蓋兩個獨立媒體。')
    if legacy:
        gaps.append('RSS 只提供最近項目；尚不能宣稱涵蓋整段歷史期間全部新聞。')
    usable = not prepared['data_gaps'] and len(unique)>=5 and len(publishers)>=2 and all(approvals.get(s,{}).get('license_status')=='approved' for s in sources) and synthesis['outlook']!='unknown'
    return seal({'schema_version':'market-news-result-1.0' if legacy else 'market-news-result-1.1','snapshot_id':pack['snapshot_id'],'decision_cutoff':pack['decision_cutoff'],
                 'input_sha256':pack['content_sha256'],'status':'available' if usable else 'unavailable',
                 'outlook':synthesis['outlook'] if usable else 'unknown','findings':synthesis['findings'] if usable else [],
                 'diagnostic_outlook':synthesis['outlook'],'diagnostic_findings':synthesis['findings'],
                 'item_count':len(items),'market_unique_count':len(unique),'market_source_count':len(publishers),
                 'data_gaps':gaps,'coverage':'本次取得的兩個財經 RSS 在查詢視窗內的全部項目，不保證涵蓋期間內所有新聞。' if legacy else prepared['coverage']})
