"""Local offers, bounded read-only market queries and separated delivery bundles."""
from pathlib import Path
import argparse
import asyncio
import json
import sys

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from console.commerce import portfolio, build_bundles, search_market, read_catalog
from console.paths import DEFAULT_ACCOUNT
from console.store import Store


async def scan(store,account,keywords):
    results=[]
    for n,word in enumerate(dict.fromkeys(keywords)):
        if n:
            await asyncio.sleep(3)
        result=await search_market(store,account,word)
        results.append(result)
        print(json.dumps({k:result.get(k) for k in ('id','keyword','status','captured_at','price_summary','error_code','message')},ensure_ascii=False),flush=True)
        if result['status']!='observed':
            print('本轮已停止追加查询，保留此前取得的数据。',flush=True)
            break
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account',default=DEFAULT_ACCOUNT)
    commands=parser.add_subparsers(dest='action',required=True)
    commands.add_parser('list')
    b=commands.add_parser('bundle')
    b.add_argument('slug',help='商品标识，或 all')
    s=commands.add_parser('search')
    s.add_argument('keyword')
    scan_parser=commands.add_parser('scan')
    scan_parser.add_argument('keywords',nargs='+',help='本轮去重后最多 15 个词；首次失败后停止')
    a=parser.parse_args()
    store=Store()
    if not store.get('account',a.account):
        parser.error('账号不存在')
    if a.action=='list':
        rows=portfolio(store,a.account)['products']
        print(json.dumps([{k:r.get(k) for k in ('slug','name','sale_type','state','missing_files','limitations')} for r in rows],ensure_ascii=False,indent=2))
    elif a.action=='bundle':
        slugs=[p['slug'] for p in read_catalog()['products']] if a.slug=='all' else [a.slug]
        print(json.dumps([build_bundles(store,a.account,s) for s in slugs],ensure_ascii=False,indent=2))
    elif a.action=='search':
        print(json.dumps(asyncio.run(search_market(store,a.account,a.keyword)),ensure_ascii=False,indent=2))
    else:
        if len(set(a.keywords))>15:
            parser.error('单轮最多 15 个不同搜索词')
        asyncio.run(scan(store,a.account,a.keywords))


if __name__=='__main__':
    main()
