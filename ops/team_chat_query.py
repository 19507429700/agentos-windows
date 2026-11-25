"""Read-only peer query command permitted to the Agent. Credentials stay in STATE.

python ops/team_chat_query.py --members
python ops/team_chat_query.py 张三 消息去重 --days 7
python ops/team_chat_query.py 张三 --recent --days 3
"""
from __future__ import annotations
import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from team_chat_server import PEERS, read_json, validate_url


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kw):
        return None  # Never forward an access token to a redirect destination.


def fetch(peer,body,timeout=12):
    url=validate_url(peer['url'])+'/v1/query'
    req=Request(url,json.dumps(body).encode(),headers={
        'Authorization':'Bearer '+peer['token'],'Content-Type':'application/json'},method='POST')
    # Tailnet access is direct; do not send member traffic/tokens via system proxies.
    opener=build_opener(ProxyHandler({}),NoRedirect())
    with opener.open(req,timeout=timeout) as response:
        data=response.read(256*1024+1)
        if len(data)>256*1024:raise ValueError('成员响应过大')
        result=json.loads(data)
        if not isinstance(result,dict) or not isinstance(result.get('results'),list):
            raise ValueError('成员响应格式无效')
        return result


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('member',nargs='?');ap.add_argument('terms',nargs='*')
    ap.add_argument('--members',action='store_true');ap.add_argument('--recent',action='store_true')
    ap.add_argument('--days',type=int,default=7);ap.add_argument('--limit',type=int,default=5)
    ap.add_argument('--platform')
    args=ap.parse_args(argv)
    peers=read_json(PEERS) if PEERS.exists() else {}
    if args.members:
        print(json.dumps({'members':list(peers)},ensure_ascii=False));return 0
    if args.member not in peers:raise ValueError('成员未登记，先请本人交付邀请文件并由管理员 add-peer')
    if not 1<=args.days<=90 or not 1<=args.limit<=20:raise ValueError('days 范围1–90，limit 范围1–20')
    if not args.recent and not args.terms:raise ValueError('请给关键词，或使用 --recent')
    body={'mode':'recent' if args.recent else 'search','terms':args.terms,'days':args.days,'limit':args.limit}
    if args.platform:body['platform']=args.platform
    try:result=fetch(peers[args.member],body)
    except HTTPError as exc:
        raise ValueError({401:'成员授权已失效，需重新邀请',403:'成员未授权这个查询范围',
                          429:'查询过于频繁',503:'成员服务在线，但本地聊天库不可用'}.get(exc.code,'成员接口错误：'+str(exc.code))) from None
    except (URLError,TimeoutError,OSError):
        raise ValueError('成员不可达：检查双方 Tailscale、成员服务、休眠/关机；这不等于没有记录') from None
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);raise SystemExit(1)
