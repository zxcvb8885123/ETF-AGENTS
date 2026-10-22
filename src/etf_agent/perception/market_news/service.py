"""補抓、保存及接入市場新聞，不直接呼叫模型或建立交易決策。"""
from datetime import datetime, timezone
from pathlib import Path
from etf_agent.core import canonical_sha256, parse_aware_time
from etf_agent.core.artifact_store import ImmutableRunStore
from .contracts import SOURCES, MarketNewsError, seal
from .provider import MarketNewsRSSProvider
from .tools import prepare, evaluate
from .validator import validate_pair


def capture_market_news(output_root, window_start, provider=None, database_path=None):
    provider = provider or MarketNewsRSSProvider()
    captures = [provider.capture(source) for source in SOURCES]
    cutoff = datetime.now(timezone.utc).isoformat()
    prepared = prepare(captures,window_start,cutoff)
    if database_path is not None:
        from .repository import MarketNewsRepository
        MarketNewsRepository(database_path).save_captures(captures)
    artifacts = {'captures':{'items':captures},'prepared':prepared}
    store = ImmutableRunStore(Path(output_root),schema_version='market-news-capture-1.0',error=MarketNewsError)
    run_id = 'market-news-' + canonical_sha256(artifacts)[:20]
    path = store.save(run_id,artifacts); store.verify(run_id)
    return {'run_path':str(path),'decision_cutoff':cutoff,'item_count':len(prepared['items'])}


def attach_market_news(decision_input, pair):
    from etf_agent.decision.contracts import decision_bundle_sha256
    validate_pair(pair,decision_input['snapshot_id'],decision_input['decision_cutoff'])
    output = dict(decision_input,market_news_input=pair)
    output['bundle_sha256'] = decision_bundle_sha256(output)
    return output


def save_market_news_result(output_root, pack):
    result = evaluate(pack)
    store = ImmutableRunStore(Path(output_root),schema_version='market-news-research-1.0',error=MarketNewsError)
    artifacts = {'input':pack,'result':result}
    path = store.save('market-news-'+canonical_sha256(artifacts)[:20], artifacts)
    store.verify(path.name)
    return {'run_path':str(path),'result':result}


def apply_database_source_reviews(pack, database_path):
    """接入當時可得的使用核對紀錄，不改新聞、標籤或模型結論。"""
    from .repository import MarketNewsRepository
    evaluate(pack)
    stored = MarketNewsRepository(database_path).load_source_reviews(pack['decision_cutoff'])
    sources = {capture['source'] for capture in pack['captures']}
    approvals = {row['source']:dict(row) for row in pack['source_approvals']}
    for row in stored:
        if row['source'] in sources:
            current = approvals.get(row['source'])
            if current is None or parse_aware_time(row['reviewed_at'], 'reviewed_at', error=MarketNewsError) >= parse_aware_time(current['reviewed_at'], 'reviewed_at', error=MarketNewsError):
                approvals[row['source']] = row
    updated = seal(dict(pack,source_approvals=[approvals[s] for s in sorted(approvals)]))
    evaluate(updated)
    return updated


def backfill_market_news(output_root, start_date, end_date, database_path=None, provider=None):
    from .history import MarketNewsHistoryProvider
    from .repository import MarketNewsRepository
    provider = provider or MarketNewsHistoryProvider()
    history = provider.collect(start_date, end_date)
    cutoff = datetime.now(timezone.utc).isoformat()
    prepared = prepare(history['captures'], start_date+'T00:00:00+08:00', cutoff)
    if database_path is not None:
        MarketNewsRepository(database_path).save_captures(history['captures'])
    artifacts = {'history':history,'prepared':prepared}
    store = ImmutableRunStore(Path(output_root),schema_version='market-news-history-1.0',error=MarketNewsError)
    run_id = 'history-'+canonical_sha256(artifacts)[:20]
    path = store.save(run_id,artifacts); store.verify(run_id)
    return {'run_path':str(path),'decision_cutoff':cutoff,'item_count':len(prepared['items']),
            'pagination_completed':history['ltn_pagination_completed']}


def record_source_reviews(database_path, sources, license_status, evidence, provider=None):
    """抓取規範原文後保存一次使用核對；只在保存時間之後的決策截止時間生效，不回溯。"""
    from .contracts import NEWS_SOURCES
    from .repository import MarketNewsRepository
    if not sources or any(source not in NEWS_SOURCES for source in sources):
        raise MarketNewsError('來源使用核對身分無效')
    provider = provider or MarketNewsRSSProvider()
    urls = sorted({NEWS_SOURCES[source]['terms_url'] for source in sources})
    terms = [provider.capture_terms(url) for url in urls]
    reviewed_at = datetime.now(timezone.utc).isoformat()
    reviews = [{'source':source,'license_status':license_status,'terms_url':NEWS_SOURCES[source]['terms_url'],
                'usage_scope':'competition_research','reviewed_at':reviewed_at,'evidence':evidence}
               for source in sorted(set(sources))]
    MarketNewsRepository(database_path).save_source_reviews(reviews, terms, reviewed_at)
    return {'reviewed_at':reviewed_at,'sources':[r['source'] for r in reviews],'license_status':license_status,
            'terms':[{'terms_url':t['terms_url'],'raw_sha256':t['raw_sha256']} for t in terms]}


def latest_market_captures(output_root, decision_cutoff):
    """截止前最後一次完整保存的 RSS 抓取；全部 available_at 都必須不晚於截止時間，沒有則回傳 None。"""
    import json
    cutoff = parse_aware_time(decision_cutoff, 'decision_cutoff', error=MarketNewsError)
    store = ImmutableRunStore(Path(output_root), schema_version='market-news-capture-1.0', error=MarketNewsError)
    best = None
    root = Path(output_root)
    # 抓取 run 不分日期資料夾；也接受日期資料夾，兩種排列都找。
    for manifest in sorted(set(root.glob('market-news-*/manifest.json')) | set(root.glob('*/market-news-*/manifest.json'))):
        store.verify(manifest.parent.name)
        items = json.loads((manifest.parent / 'captures.json').read_text(encoding='utf-8'))['items']
        if not items:
            continue
        latest = max(parse_aware_time(c['available_at'], 'available_at', error=MarketNewsError) for c in items)
        if latest <= cutoff and (best is None or latest > best[0]):
            best = (latest, items)
    return best[1] if best else None


def build_market_news_pack(captures, snapshot_id, decision_cutoff, window_start, labels, synthesis, database_path=None):
    """以完整標籤、綜合判讀與截止前已保存的來源核對組成 1.1 資料包；不改寫任何標籤或結論。"""
    approvals = []
    if database_path is not None:
        from .repository import MarketNewsRepository
        sources = {capture['source'] for capture in captures}
        approvals = [row for row in MarketNewsRepository(database_path).load_source_reviews(decision_cutoff) if row['source'] in sources]
    return seal({'schema_version':'market-news-1.1','snapshot_id':snapshot_id,'decision_cutoff':decision_cutoff,
                 'window_start':window_start,'captures':list(captures),'labels':list(labels),'synthesis':dict(synthesis),
                 'source_approvals':approvals})
