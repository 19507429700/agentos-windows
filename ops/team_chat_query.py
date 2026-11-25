"""团队互查（只读，机器人可用）：查同一个 Tailscale 团队网络里其他人共享的 AI 聊天。

python ops/team_chat_query.py --members              现在能查哪些人（自动从团队网络里找）
python ops/team_chat_query.py 张三 消息去重 --days 7  按关键词查（多个词要在同一条消息里都出现）
python ops/team_chat_query.py 张三 --recent --days 3  看最近的聊天

不需要邀请文件或钥匙：请求经 Tailscale 发出，对方电脑认得出是谁在查。
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from team_chat_server import STATE, private_json, read_json, run, tailscale_exe

CACHE = STATE/'team-members.json'
CACHE_SECONDS = 600


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kw):
        return None


def opener():
    # 团队网络里的地址直连，不走系统代理（翻墙软件会把 .ts.net 送错地方）
    return build_opener(ProxyHandler({}),NoRedirect())


def tailnet_devices():
    """团队网络里的设备：[(地址, 在线, 设备名, 账号)]，包括自己这台。"""
    status=json.loads(run([tailscale_exe(),'status','--json']).stdout or '{}')
    users={str(k):v for k,v in (status.get('User') or {}).items()}
    nodes=[status.get('Self') or {}]+list((status.get('Peer') or {}).values())
    out=[]
    for n in nodes:
        dns=(n.get('DNSName') or '').rstrip('.')
        if not dns or 'tagged-devices' in str(n.get('Tags') or ''):continue
        login=(users.get(str(n.get('UserID'))) or {}).get('LoginName','')
        out.append(('https://'+dns,bool(n.get('Online')),n.get('HostName',''),login))
    return out


def probe(url,timeout=4):
    try:
        with opener().open(url+'/health',timeout=timeout) as r:
            h=json.loads(r.read(4096))
        if h.get('service')=='agentos-team-chat' and h.get('name'):return h
    except (OSError,ValueError):
        pass
    return None


def discover():
    """问每台在线设备「你开了团队互查吗、叫什么名字」，结果缓存 10 分钟。"""
    devices=tailnet_devices()
    online=[d for d in devices if d[1]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        healths=list(pool.map(lambda d:probe(d[0]),online))
    members={}
    old=(read_json(CACHE).get('members',{}) if CACHE.exists() else {})
    for (url,_,host,login),h in zip(online,healths):
        if h:members[h['name']]={'url':url,'host':host,'login':login,'sharing':bool(h.get('sharing')),
                                 'days':h.get('days'),'online':True}
    for name,m in old.items():  # 之前见过、现在离线的人也列出来，标明离线
        if name not in members and any(m.get('url')==d[0] for d in devices):
            members[name]=dict(m,online=False)
    private_json(CACHE,{'at':time.time(),'members':members})
    return members


def members(refresh=False):
    if not refresh and CACHE.exists():
        cache=read_json(CACHE)
        if time.time()-cache.get('at',0)<CACHE_SECONDS:return cache.get('members',{})
    return discover()


def find(name):
    for refresh in (False,True):
        ms=members(refresh)
        if name in ms:return name,ms[name]
        hits=[k for k in ms if name in k or k in name]
        if len(hits)==1:return hits[0],ms[hits[0]]
    known='、'.join(ms) or '（暂时一个都没有：别人还没运行 team_chat_server.py on，或电脑没开）'
    raise ValueError('团队里没找到「'+name+'」。现在能查的人：'+known)


def fetch(url,body,timeout=12):
    req=Request(url+'/v1/query',json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
    with opener().open(req,timeout=timeout) as response:
        data=response.read(256*1024+1)
        if len(data)>256*1024:raise ValueError('对方返回的内容太大')
        result=json.loads(data)
        if not isinstance(result,dict) or not isinstance(result.get('results'),list):raise ValueError('对方返回的格式不对')
        return result


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('member',nargs='?');ap.add_argument('terms',nargs='*')
    ap.add_argument('--members',action='store_true');ap.add_argument('--recent',action='store_true')
    ap.add_argument('--days',type=int,default=7);ap.add_argument('--limit',type=int,default=5)
    ap.add_argument('--platform')
    args=ap.parse_args(argv)
    if args.members:
        ms=members(refresh=True)
        print(json.dumps({'members':[{'name':k,'online':v.get('online'),'sharing':v.get('sharing'),'days':v.get('days')}
                                     for k,v in ms.items()]},ensure_ascii=False));return 0
    if not args.member:raise ValueError('要写查谁，例如：team_chat_query.py 张三 --recent')
    if not 1<=args.days<=90 or not 1<=args.limit<=20:raise ValueError('days 范围1–90，limit 范围1–20')
    if not args.recent and not args.terms:raise ValueError('请给关键词，或使用 --recent')
    name,m=find(args.member)
    if not m.get('online'):raise ValueError('「'+name+'」的电脑现在不在线（关机、睡眠或断网），查不到；这不等于没有记录')
    body={'mode':'recent' if args.recent else 'search','terms':args.terms,'days':args.days,'limit':args.limit}
    if args.platform:body['platform']=args.platform
    try:result=fetch(m['url'],body)
    except HTTPError as exc:
        raise ValueError({401:'请求没有经过团队网络，对方拒绝了',403:'「'+name+'」关掉了共享',
                          429:'查得太频繁，过一分钟再试',503:'「'+name+'」的电脑在线，但聊天库暂时读不了'
                          }.get(exc.code,'对方返回错误：'+str(exc.code))) from None
    except (URLError,TimeoutError,OSError):
        raise ValueError('连不上「'+name+'」：可能刚关机或断网；这不等于没有记录') from None
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);raise SystemExit(1)
