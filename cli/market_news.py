"""全市場 RSS 補抓及已判讀輸入接入的薄 CLI。"""
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from etf_agent.perception.market_news import capture_market_news, attach_market_news, save_market_news_result, backfill_market_news, apply_database_source_reviews

def main():
    parser=argparse.ArgumentParser(description='補抓全市場新聞或接入完整標籤與研究結論')
    sub=parser.add_subparsers(dest='action',required=True)
    capture=sub.add_parser('capture'); capture.add_argument('--window-start',required=True)
    capture.add_argument('--output-root',type=Path,required=True)
    capture.add_argument('--database',type=Path)
    history=sub.add_parser('backfill'); history.add_argument('--start-date',required=True); history.add_argument('--end-date',required=True)
    history.add_argument('--database',type=Path); history.add_argument('--output-root',type=Path,required=True)
    integrate=sub.add_parser('integrate'); integrate.add_argument('--decision-input',type=Path,required=True)
    integrate.add_argument('--market-pack',type=Path,required=True); integrate.add_argument('--output-root',type=Path,required=True)
    integrate.add_argument('--database',type=Path,help='讀取截止時間前已保存的來源使用核對')
    args=parser.parse_args()
    if args.action=='capture':
        output=capture_market_news(args.output_root,args.window_start,database_path=args.database)
    elif args.action=='backfill':
        output=backfill_market_news(args.output_root,args.start_date,args.end_date,args.database)
    else:
        decision=json.loads(args.decision_input.read_text(encoding='utf-8'))
        pack=json.loads(args.market_pack.read_text(encoding='utf-8'))
        if args.database is not None:
            pack=apply_database_source_reviews(pack,args.database)
        output=save_market_news_result(args.output_root,pack)
        attached=attach_market_news(decision,{'bundle':pack,'result':output['result']})
        from etf_agent.decision.contracts import DecisionInputValidator
        errors=DecisionInputValidator(attached).validate()
        if errors: raise ValueError('接入驗證失敗：'+'；'.join(errors))
        # 接入結果另存不可變 run，不覆蓋既有決策輸入。
        from etf_agent.core.artifact_store import ImmutableRunStore
        store=ImmutableRunStore(args.output_root/'decision_inputs',schema_version='market-news-decision-input-1.0')
        path=store.save('input-'+attached['bundle_sha256'][:20],{'decision_input':attached})
        store.verify(path.name); output['decision_input_path']=str(path/'decision_input.json')
    print(json.dumps(output,ensure_ascii=False))

if __name__=='__main__': main()
