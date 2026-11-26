"""团队互查（只读，机器人可用）：查同一个 Tailscale 团队网络里其他人共享的 AI 聊天。

python ops/team_chat_query.py --members              现在能查哪些人（自动从团队网络里找）
python ops/team_chat_query.py 张三 消息去重 --days 7  按关键词查（多个词要在同一条消息里都出现）
python ops/team_chat_query.py 张三 --recent --days 3  看最近的聊天
python ops/team_chat_query.py 张三 --session 编号 [编号…] --out spaces/me/outbox/草稿/任务号/正文.md
    用户明确要某个话题的全部对话时：把这几个会话（编号取自上面查询结果里的 session）整段取回，
    按时间顺序追加写进那个 .md（只能写在 spaces/me/outbox/ 下），截图嵌进去，再用 make_doc.py 转成 Word
python ops/team_chat_query.py 张三 --file spaces/me/raw/ai/files/… [更多] --to spaces/me/outbox/任务号/
    用户要对方发给 AI 的某个原文件时：路径取自查询结果里「[文件 名字] 可看：…」，存进任务文件夹随回复发出

不需要邀请文件或钥匙：请求经 Tailscale 发出，对方电脑认得出是谁在查。
"""
from __future__ import annotations
import argparse
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from collect_ai_chats import clean_text
from team_chat_server import STATE, private_json, read_json, run, tailscale_exe, title_of

CACHE = STATE/'team-members.json'
CACHE_SECONDS = 600
MAX_OUT = 1500        # 一次最多写多少条消息进文件
MAX_REPLY = 1024*1024
MAX_ATTACH_REPLY = 30*1024*1024   # 一个附件原件最大 20MB，转成文字传输会变大约三分之一
MAX_ATTACH_COUNT = 300            # 一次最多取多少个截图和文件
MAX_ATTACH_TOTAL = 200*1024*1024
FILE_PATH = re.compile(r'spaces/me/raw/ai/files/\d{4}-\d{2}-\d{2}/[^/\\]{1,200}')
ATTACH_MARK = re.compile(r'\[(截图|文件 [^\]\n]+)\] 可看：(spaces/me/raw/ai/(?:images|files)/[^\s）)]+)')


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
    big=body.get('mode')=='attachment'
    limit=MAX_ATTACH_REPLY if big else MAX_REPLY
    req=Request(url+'/v1/query',json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
    with opener().open(req,timeout=90 if big else timeout) as response:
        data=response.read(limit+1)
        if len(data)>limit:raise ValueError('对方返回的内容太大')
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
                    problem='「'+name+'」的 AgentOS 还是旧版，不支持取整段对话；请他拉一次更新（git pull，再重跑安装脚本）'
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


PLATFORM_NAMES = {'workbuddy':'WorkBuddy','claude':'Claude Code','codex':'Codex','grokbot':'Grok Bot'}


def readable(text):
    """对方的原话放进文档：保留 AI 回复里的列表、表格、加粗；标题改成加粗段落（免得打乱文档目录），
    插图语法拆开（不去嵌本机文件），去掉软件自动加的包装和凭据。"""
    text=clean_text(text)
    text=re.sub(r'<image_local_path>.*?</image_local_path>','[截图]',text,flags=re.S)  # WorkBuddy 贴图：只留占位，不露本机路径
    text=re.sub(r'@image#\d+:(\S+)',r'[图片 \1]',text)
    text=re.sub(r'@"[^"]*[\\/]([^"\\/]+)"',r'@\1',text)  # @"D:/…/文件.zip" 只留文件名
    out=[]
    for line in text.splitlines():
        line=re.sub(r'^\s*>\s?','',line)  # 引用块：Word 里显示不出来，去掉 > 号
        m=re.match(r'\s*#{1,6}\s+(.*)',line)
        if m:line='**'+m.group(1).strip().strip('*')+'**'
        out.append(line.replace('![','!\u200b['))
    return '\n'.join(out)


def topic_of(text):
    """会话标题：本人第一句话，去掉排版符号和插图语法。"""
    t=re.sub(r'!\[[^\]]*\]\([^)]*\)','',clean_text(text))
    t=re.sub(r'\[(截图|文件 [^\]\n]+)\](?: 可看：\S+|（[^）\n]*）)?','',t)  # 截图、文件不当标题
    return ' '.join(re.sub(r'[#*`|>]',' ',title_of(t)).split())


def safe_name(text):
    return re.sub(r'[\\/:*?"<>|()（）\[\]\s]+','_',text).strip('._') or '附件'


class Attachments:
    """整段对话里的截图：向对方取原图，存进草稿、嵌进 Word。文件只写名字，用户要了再用 --file 单独取。"""
    def __init__(self,name,m,days,out):
        self.name,self.m,self.days=name,m,days
        self.img_dir=out.parent/(safe_name(out.stem)+'-图片')
        self.cache={};self.total=0;self.images=0;self.failed=0;self.files_named=0

    def image(self,rel):
        if rel in self.cache:return self.cache[rel]
        saved=None
        if len(self.cache)<MAX_ATTACH_COUNT and self.total<MAX_ATTACH_TOTAL:
            try:
                r=ask(self.name,self.m,{'mode':'attachment','path':rel,'days':self.days})
                data=base64.b64decode(r.get('data') or '')
                saved=self.img_dir/safe_name(rel.rsplit('/',1)[-1])
                saved.parent.mkdir(parents=True,exist_ok=True);saved.write_bytes(data)
                self.total+=len(data);self.images+=1
            except (ValueError,OSError,binascii.Error):
                saved=None
        if saved is None:self.failed+=1
        self.cache[rel]=saved
        return saved

    def render(self,text):
        def one(m):
            label,rel=m.group(1),m.group(2)
            if label!='截图':
                self.files_named+=1
                return '['+label+']（原文件没随文档发送，要的话在飞书里说一声）'
            saved=self.image(rel)
            if not saved:return '[截图]（没取到原图）'
            return '\n\n![截图]('+saved.relative_to(Path.cwd().resolve()).as_posix()+')\n\n'
        return ATTACH_MARK.sub(one,text)


def session_markdown(owner,page_list,attach=None):
    """一个会话写成一节：同一个人连续说的几条并成一段（AI 常把一次回答拆成好几条）。"""
    first=page_list[0];rows=[r for page in page_list for r in page['results']]
    topic=next((t for t in (topic_of(r['text']) for r in rows if r['role']=='user') if t),'')
    lines=['## '+owner+' · '+PLATFORM_NAMES.get(first.get('platform'),str(first.get('platform') or ''))+
           ('：「'+topic+'」' if topic else ''),'',
           '会话 '+first['session']+'，'+first['since']+' ～ '+first['through']+' 内共 '+str(first['total'])+' 条'+
           ('' if len(rows)>=first['total'] else '，这里只写了前 '+str(len(rows))+' 条（一次最多 '+str(MAX_OUT)+' 条）')+
           '。「AI」的话是 AI 的回复，不代表事情已经做完。','']
    groups=[]
    for r in rows:
        text=readable(r['text'])
        text=attach.render(text) if attach else ATTACH_MARK.sub(lambda m:'['+m.group(1)+']',text)
        if not text.strip():continue
        if groups and groups[-1]['role']==r['role']:groups[-1]['items'].append((r,text))
        else:groups.append({'role':r['role'],'items':[(r,text)]})
    for g in groups:
        items=g['items'];start=items[0][0]['at'][:16].replace('T',' ');end=items[-1][0]['at'][11:16]
        when=start+('–'+end if end!=start[11:] else '')
        ids='、'.join(r['evidence_id'] for r,_ in items)
        cut=any(r.get('truncated') for r,_ in items)
        lines+=['---','','**'+when+' · '+(owner if g['role']=='user' else 'AI')+'**','']
        lines+=['\n\n'.join(t for _,t in items),'',
                '出处 evidence_id：'+ids+('；其中有原文过长，已截断' if cut else ''),'']
    return '\n'.join(lines)+'\n'


def fetch_sessions(name,m,args):
    path=out_path(args.out);written=0;summary=[]
    attach=Attachments(name,m,args.days,path)
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
            text=session_markdown(name,pages,attach)
            with path.open('a',encoding='utf-8') as f:f.write('\n'+text)
        if written>=MAX_OUT:break
    return {'owner':name,'out':str(path),'sessions':summary,
            'attachments':{'images':attach.images,'images_failed':attach.failed,'files_named':attach.files_named},
            'note':'已按时间顺序追加写进文件，截图已嵌进去；对话里提到的文件只写了名字（用户要原件再用 --file 取）。'
                   '下一步用 make_doc.py 转成 Word。'+
                   ('一次最多写 '+str(MAX_OUT)+' 条，后面的没写。' if written>=MAX_OUT else '')}


def fetch_files(name,m,paths,to,days):
    """用户点名要的原文件：向对方取，存进任务文件夹（outbox/<任务号>/ 里的文件会随回复发到飞书）。"""
    base=(Path.cwd()/'spaces'/'me'/'outbox').resolve()
    folder=(Path.cwd()/to).resolve()
    if folder.suffix or base not in folder.parents or '草稿' in folder.relative_to(base).parts:
        raise ValueError('--to 写任务文件夹，例如 spaces/me/outbox/25/（里面的文件会发到飞书）')
    out=[]
    for rel in paths:
        if not FILE_PATH.fullmatch(rel):
            raise ValueError('--file 写查询结果里「[文件 名字] 可看：」后面的路径（spaces/me/raw/ai/files/…）；截图已经在 Word 里')
        try:
            r=ask(name,m,{'mode':'attachment','path':rel,'days':days})
            data=base64.b64decode(r.get('data') or '')
            target=folder/re.sub(r'^[0-9a-f]{12}-','',rel.rsplit('/',1)[-1])
            folder.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            out.append({'path':rel,'saved':str(target),'size':len(data)})
        except (ValueError,binascii.Error) as exc:
            out.append({'path':rel,'error':str(exc) if '「' in str(exc) else '取不到：超出共享范围、对方删了原件，或对方还是旧版'})
    return {'owner':name,'files':out,'note':'取到的文件已放进任务文件夹，会随回复发到飞书。'}


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('member',nargs='?');ap.add_argument('terms',nargs='*')
    ap.add_argument('--members',action='store_true');ap.add_argument('--recent',action='store_true')
    ap.add_argument('--days',type=int);ap.add_argument('--limit',type=int,default=5)
    ap.add_argument('--platform')
    ap.add_argument('--session',nargs='+');ap.add_argument('--offset',type=int,default=0);ap.add_argument('--out')
    ap.add_argument('--file',nargs='+');ap.add_argument('--to')
    args=ap.parse_args(argv)
    if args.members:
        ms=members(refresh=True)
        print(json.dumps({'members':[{'name':k,'online':v.get('online'),'sharing':v.get('sharing'),'days':v.get('days')}
                                     for k,v in ms.items()]},ensure_ascii=False));return 0
    if not args.member:raise ValueError('要写查谁，例如：team_chat_query.py 张三 --recent')
    if args.days is None:args.days=15 if args.session or args.file else 7  # 整段会话、原文件默认取满对方共享的天数
    if args.file:
        if not args.to:raise ValueError('--file 要配 --to 任务文件夹，例如 --to spaces/me/outbox/25/')
        if len(args.file)>20:raise ValueError('一次最多取 20 个文件')
        name,m=find(args.member)
        print(json.dumps(fetch_files(name,m,args.file,args.to,args.days),ensure_ascii=False));return 0
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
