"""团队互查（只读，机器人可用）：查同一个 Tailscale 团队网络里其他人共享的 AI 聊天。

python ops/team_chat_query.py --members              现在能查哪些人（自动从团队网络里找）
python ops/team_chat_query.py 张三 消息去重 --days 7  按关键词查（多个词要在同一条消息里都出现）
python ops/team_chat_query.py 张三 --recent --days 3  看最近的聊天
python ops/team_chat_query.py 张三 --session 编号 [编号…] --out spaces/me/outbox/草稿/任务号/正文.md
    用户明确要某个话题的全部对话时：把这几个会话（编号取自上面查询结果里的 session）整段取回，
    按时间顺序追加写进那个 .md（只能写在 spaces/me/outbox/ 下），再用 make_doc.py 转成 Word

不需要邀请文件或钥匙：请求经 Tailscale 发出，对方电脑认得出是谁在查。
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from team_chat_server import STATE, private_json, read_json, run, tailscale_exe

CACHE = STATE/'team-members.json'
CACHE_SECONDS = 600
MAX_OUT = 1500        # 一次最多写多少条消息进文件
MAX_REPLY = 1024*1024


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
    for name,m in old.items():  # 之前见过、这次没应答的人也列出来
        device=next((d for d in devices if d[0]==m.get('url')),None)
        if name in members or not device:continue
        # 电脑在团队网络里在线、却没应答：是他关掉了共享（off 会停掉服务）；电脑不在线才是关机/断网
        members[name]=dict(m,online=device[1],sharing=False)
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
        data=response.read(MAX_REPLY+1)
        if len(data)>MAX_REPLY:raise ValueError('对方返回的内容太大')
        result=json.loads(data)
        if not isinstance(result,dict) or not isinstance(result.get('results'),list):raise ValueError('对方返回的格式不对')
        return result


def ask(name,m,body):
    """先用缓存；缓存说他离线、关了共享或连不上时，都重新问一遍再下结论（他可能刚开机、刚重新打开共享）。"""
    for retry in (False,True):
        if retry:m=members(refresh=True).get(name) or {}
        if not m.get('online'):problem='「'+name+'」的电脑现在不在线（关机、睡眠或断网），查不到；这不等于没有记录'
        elif not m.get('sharing'):problem='「'+name+'」关掉了共享（或查询服务没在运行），查不到；这不等于没有记录'
        else:
            try:return fetch(m['url'],body)
            except HTTPError as exc:
                problem={401:'请求没有经过团队网络，对方拒绝了',403:'「'+name+'」关掉了共享，查不到；这不等于没有记录',
                         429:'查得太频繁，过一分钟再试',503:'「'+name+'」的电脑在线，但聊天库暂时读不了'
                         }.get(exc.code,'对方返回错误：'+str(exc.code))
                if exc.code==400 and body.get('mode')=='session':
                    problem='「'+name+'」的 AgentOS 还是旧版，不支持取整段对话；请他拉一次更新（git pull + 重跑 install.ps1）'
                if exc.code!=403:raise ValueError(problem) from None
            except (URLError,TimeoutError,OSError):
                problem='连不上「'+name+'」：可能刚关机或断网；这不等于没有记录'
        if retry:raise ValueError(problem)


def out_path(text):
    base=(Path.cwd()/'spaces'/'me'/'outbox').resolve()
    path=(Path.cwd()/text).resolve()
    if path.suffix.lower()!='.md' or base not in path.parents:
        raise ValueError('--out 只能写到 spaces/me/outbox/ 下的 .md 文件，例如 spaces/me/outbox/草稿/任务号/正文.md')
    return path


def plain(text):
    """对方的原话照原样放进文档：每行前加一个看不见的字符，免得被当成标题、列表、表格或插图。"""
    return '\n'.join('\u200b'+line.replace('![','!\u200b[') if line.strip() else '' for line in text.splitlines())


def session_markdown(owner,page_list):
    first=page_list[0];rows=[r for page in page_list for r in page['results']]
    lines=['## 原文：'+owner+' · '+str(first.get('platform') or '')+' · 会话 '+first['session'],'',
           first['since']+' ～ '+first['through']+' 内共 '+str(first['total'])+' 条'+
           ('' if len(rows)>=first['total'] else '，这里只写了前 '+str(len(rows))+' 条（一次最多 '+str(MAX_OUT)+' 条）')+
           '。「AI」的话是 AI 的回复，不代表事情已经做完。','']
    for r in rows:
        who=owner if r['role']=='user' else 'AI'
        lines+=['**'+r['at'][:16].replace('T',' ')+' · '+who+'**（evidence_id '+r['evidence_id']+
                ('，原文过长，已截断' if r.get('truncated') else '')+'）','',plain(r['text']),'']
    return '\n'.join(lines)+'\n'


def fetch_sessions(name,m,args):
    path=out_path(args.out);written=0;summary=[]
    for sid in args.session:
        pages=[];offset=0
        while True:
            page=ask(name,m,{'mode':'session','session':sid,'days':args.days,'offset':offset,
                             'limit':min(50,MAX_OUT-written)})
            pages.append(page);written+=len(page['results'])
            offset=page.get('next_offset')
            if offset is None or written>=MAX_OUT:break
        got=sum(len(p['results']) for p in pages)
        summary.append({'session':sid,'platform':pages[0].get('platform'),'total':pages[0]['total'],'written':got,
                        **({} if got else {'note':'没有这个会话：编号不对、超出共享天数，或对方设为不共享'})})
        if got:
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('a',encoding='utf-8') as f:f.write('\n'+session_markdown(name,pages))
        if written>=MAX_OUT:break
    return {'owner':name,'out':str(path),'sessions':summary,
            'note':'已按时间顺序追加写进文件；下一步用 make_doc.py 转成 Word。'+
                   ('一次最多写 '+str(MAX_OUT)+' 条，后面的没写。' if written>=MAX_OUT else '')}


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('member',nargs='?');ap.add_argument('terms',nargs='*')
    ap.add_argument('--members',action='store_true');ap.add_argument('--recent',action='store_true')
    ap.add_argument('--days',type=int);ap.add_argument('--limit',type=int,default=5)
    ap.add_argument('--platform')
    ap.add_argument('--session',nargs='+');ap.add_argument('--offset',type=int,default=0);ap.add_argument('--out')
    args=ap.parse_args(argv)
    if args.members:
        ms=members(refresh=True)
        print(json.dumps({'members':[{'name':k,'online':v.get('online'),'sharing':v.get('sharing'),'days':v.get('days')}
                                     for k,v in ms.items()]},ensure_ascii=False));return 0
    if not args.member:raise ValueError('要写查谁，例如：team_chat_query.py 张三 --recent')
    if args.days is None:args.days=15 if args.session else 7  # 整段会话默认取满对方共享的天数
    if args.session:
        if args.terms or args.recent:raise ValueError('--session 不和关键词、--recent 一起用')
        if len(args.session)>10 or not all(len(x)==16 for x in args.session):raise ValueError('--session 写 1–10 个 16 位会话编号')
        if not 1<=args.days<=90:raise ValueError('days 范围1–90')
        name,m=find(args.member)
        if args.out:result=fetch_sessions(name,m,args)
        else:result=ask(name,m,{'mode':'session','session':args.session[0],'days':args.days,'offset':args.offset,'limit':50})
        print(json.dumps(result,ensure_ascii=False));return 0
    if args.out:raise ValueError('--out 只和 --session 一起用')
    if not 1<=args.days<=90 or not 1<=args.limit<=20:raise ValueError('days 范围1–90，limit 范围1–20')
    if not args.recent and not args.terms:raise ValueError('请给关键词，或使用 --recent')
    name,m=find(args.member)
    body={'mode':'recent' if args.recent else 'search','terms':args.terms,'days':args.days,'limit':args.limit}
    if args.platform:body['platform']=args.platform
    print(json.dumps(ask(name,m,body),ensure_ascii=False));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);raise SystemExit(1)
