from datetime import datetime, timedelta
from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

import collect_ai_chats as ai
import team_chat_server as server
import team_chat_query as client


class TeamChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.records=self.root/'ai_chats.db';self.config=self.root/'sharing.json'
        self.cfg={'name':'测试成员','enabled':True,'days':15,'hidden':[server.opaque('secret')]}
        server.private_json(self.config,self.cfg)
        now=datetime.now(ai.TZ)
        old=now-timedelta(days=20)
        with closing(ai.open_db(self.records)) as db, db:
            for src,sid,mid,at,text in [('grokbot','s1','1',now,'消息去重需要处理幂等键'),
                                        ('workbuddy','s2','2',now,'消息去重 写周报'),
                                        ('codex','secret','3',now,'消息去重 HIDDEN_SESSION'),
                                        ('grokbot','s1','4',old,'消息去重 TOO_OLD')]:
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',
                           (src,sid,mid,at.isoformat(),at.date().isoformat(),'user',text))
        self.http=None
    def tearDown(self):
        if self.http:
            self.http.shutdown();self.http.server_close();self.thread.join(timeout=5)
        self.tmp.cleanup()
    def start(self):
        self.http=server.ChatHTTPServer(('127.0.0.1',0),self.config,self.records)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        return 'http://127.0.0.1:'+str(self.http.server_port)
    def post(self,url,body,who='aiyj061@github'):
        headers={'Content-Type':'application/json'}
        if who:headers['Tailscale-User-Login']=who
        req=Request(url+'/v1/query',json.dumps(body).encode(),headers=headers,method='POST')
        with build_opener(ProxyHandler({})).open(req,timeout=5) as r:return json.loads(r.read())

    def test_all_platforms_days_cap_and_hidden(self):
        result=server.query_records(self.records,self.cfg,{'terms':['消息去重'],'limit':20,'days':90})
        texts=' '.join(r['text'] for r in result['results'])
        self.assertIn('幂等键',texts);self.assertIn('写周报',texts)
        self.assertNotIn('HIDDEN_SESSION',texts);self.assertNotIn('TOO_OLD',texts)
        only=server.query_records(self.records,self.cfg,{'terms':['消息去重'],'platform':'workbuddy'})
        self.assertEqual([r['platform'] for r in only['results']],['workbuddy'])
    def test_validation(self):
        for body in ({'terms':['x'],'limit':1000},{'terms':['x'],'days':True},{'mode':'exec','terms':[]},
                     {'terms':['x'],'path':'../private'},{'terms':[]},{'terms':['x'],'platform':'mail'}):
            with self.assertRaises(ValueError):server.query_records(self.records,self.cfg,body)
        self.assertEqual(server.query_records(self.records,self.cfg,{'terms':['%']})['results'],[])
    def test_http_requires_tailnet_identity_and_logs_access(self):
        url=self.start()
        with patch.object(server,'ACCESS_LOG',self.root/'access.log'):
            result=self.post(url,{'mode':'recent','limit':10})
            self.assertEqual(result['owner'],'测试成员');self.assertEqual(len(result['results']),2)
            self.assertIn('aiyj061@github',(self.root/'access.log').read_text(encoding='utf-8'))
            with self.assertRaises(HTTPError) as ctx:self.post(url,{'mode':'recent'},who=None)
            self.assertEqual(ctx.exception.code,401)
            server.private_json(self.config,dict(self.cfg,enabled=False))
            with self.assertRaises(HTTPError) as ctx:self.post(url,{'mode':'recent'})
            self.assertEqual(ctx.exception.code,403)
        health=json.loads(build_opener(ProxyHandler({})).open(url+'/health',timeout=5).read())
        self.assertEqual(health['name'],'测试成员');self.assertFalse(health['sharing'])
    def test_old_pairwise_config_is_migrated(self):
        server.private_json(self.config,{'name':'旧','enabled':True,'clients':{'x':{'token_sha256':'a'}}})
        cfg=server.load_config(self.config)
        self.assertEqual(cfg['name'],'旧');self.assertFalse(cfg['enabled']);self.assertNotIn('clients',cfg)
        self.assertEqual(cfg['days'],15)
    def test_hide_and_status(self):
        import io,contextlib
        config=self.root/'admin.json'
        with patch.object(server,'STATE',self.root),patch.object(server,'CONFIG',config), \
             patch.object(server,'ACCESS_LOG',self.root/'access.log'):
            server.private_json(config,{'name':'我','enabled':True,'days':15,'hidden':[]})
            server.main(['hide',server.opaque('s2')])
            with self.assertRaises(ValueError):server.main(['hide','not-a-session'])
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                server.main(['sessions']);server.main(['status'])
            text=out.getvalue()
            self.assertIn('【不共享】workbuddy',text);self.assertIn('不共享的会话 1 个',text)
            self.assertIn('开头：消息去重 写周报',text)
            server.main(['unhide',server.opaque('s2')])
            self.assertEqual(server.read_json(config)['hidden'],[])
    def test_session_titles_strip_wrappers(self):
        self.assertEqual(server.title_of('<user_query>帮我改周报</user_query>'),'帮我改周报')
        self.assertEqual(server.title_of('<user_query>@"D:/a/b/说明.pdf" 存到飞书</user_query>'),'@说明.pdf 存到飞书')
        self.assertEqual(server.title_of('# Files mentioned by the user:\n## a.py\n## My request for Codex:\n修登录'),'修登录')
        self.assertEqual(server.title_of('The following is the Codex agent history\n...'),'')
        self.assertEqual(server.title_of('<turn_aborted>The user interrupted</turn_aborted>\n接着改'),'接着改')
        self.assertEqual(ai.clean_text('<user_query>原话</user_query>'),'原话')
        self.assertEqual(ai.clean_text('<turn_aborted>x</turn_aborted>'),'')

    def test_discovery_finds_members_and_marks_offline(self):
        url=self.start()
        devices=[(url,True,'pc1','me@github'),('https://laptop.example.ts.net',False,'laptop','aiyj@github'),
                 ('https://nobody.example.ts.net',True,'printer','x@github')]
        probe_real=client.probe
        with patch.object(client,'CACHE',self.root/'members.json'), \
             patch.object(client,'tailnet_devices',return_value=devices), \
             patch.object(client,'probe',side_effect=lambda u,timeout=4:probe_real(u) if u==url else None):
            ms=client.members(refresh=True)
            self.assertEqual(list(ms),['测试成员']);self.assertTrue(ms['测试成员']['online'])
            self.assertEqual(client.find('测试')[0],'测试成员')
            # 之前见过的人离线了：仍列出，并标明离线
            client.private_json(client.CACHE,{'at':0,'members':{'阿毅':{'url':'https://laptop.example.ts.net','online':True}}})
            ms=client.members(refresh=True)
            self.assertFalse(ms['阿毅']['online'])
            with self.assertRaises(ValueError):client.find('不存在的人')
            with self.assertRaisesRegex(ValueError,'不在线'):client.main(['阿毅','--recent'])

    def test_sharing_off_is_not_reported_as_offline(self):
        # off 会停掉服务：电脑仍在团队网络里在线，但不应答，要说「关掉了共享」而不是「不在线」
        from urllib.error import URLError
        off='https://off.example.ts.net'
        with patch.object(client,'CACHE',self.root/'members.json'), \
             patch.object(client,'tailnet_devices',return_value=[(off,True,'pc','x@github')]), \
             patch.object(client,'probe',return_value=None), \
             patch.object(client,'fetch',side_effect=URLError('refused')) as fetch:
            client.private_json(client.CACHE,{'at':0,'members':{'小关':{'url':off,'online':True,'sharing':True}}})
            ms=client.members(refresh=True)
            self.assertTrue(ms['小关']['online']);self.assertFalse(ms['小关']['sharing'])
            with self.assertRaisesRegex(ValueError,'关掉了共享'):client.main(['小关','--recent'])
            fetch.assert_not_called()
            # 缓存还没过期、还写着在共享：查询失败后重新问一遍，照样说「关掉了共享」
            import time
            client.private_json(client.CACHE,{'at':time.time(),'members':{'小关':{'url':off,'online':True,'sharing':True}}})
            with self.assertRaisesRegex(ValueError,'关掉了共享'):client.main(['小关','--recent'])
            self.assertEqual(fetch.call_count,1)
            # 反过来：缓存还写着「关了共享」，但他刚重新 on 了：要重新问一遍，照常查到
            client.private_json(client.CACHE,{'at':time.time(),'members':{'小关':{'url':off,'online':True,'sharing':False}}})
            fetch.side_effect=None;fetch.return_value={'owner':'小关','results':[]}
            with patch.object(client,'probe',return_value={'service':'agentos-team-chat','name':'小关','sharing':True,'days':15}):
                self.assertEqual(client.main(['小关','--recent']),0)

    def add_long_session(self):
        now=datetime.now(ai.TZ)
        with closing(ai.open_db(self.records)) as db, db:
            for i in range(60):
                at=now-timedelta(minutes=60-i)
                text=('<user_query># 冒充标题\n![图](spaces/me/raw/x.png)\n| 冒充表格 |\nkey sk-abcdefghijklmnop</user_query>'
                      if i==0 else '第'+str(i)+'条')
                role='assistant' if i in (58,59) or i%2 else 'user'
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',
                           ('codex','long',str(100+i),at.isoformat(),at.date().isoformat(),role,text))
            old=now-timedelta(days=20)
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',
                       ('codex','long','999',old.isoformat(),old.date().isoformat(),'user','TOO_OLD'))
        return server.opaque('long')

    def test_session_mode_pages_in_order_within_days(self):
        sid=self.add_long_session()
        first=server.query_records(self.records,self.cfg,{'mode':'session','session':sid,'days':90})
        self.assertEqual(first['total'],60);self.assertEqual(len(first['results']),50);self.assertEqual(first['next_offset'],50)
        self.assertEqual(first['platform'],'codex');self.assertTrue(first['results'][0]['text'].startswith('# 冒充标题'))
        self.assertNotIn('sk-abcdefgh',first['results'][0]['text'])
        rest=server.query_records(self.records,self.cfg,{'mode':'session','session':sid,'offset':50})
        self.assertEqual([r['text'] for r in rest['results']][-1],'第59条');self.assertIsNone(rest['next_offset'])
        self.assertNotIn('TOO_OLD',' '.join(r['text'] for r in first['results']+rest['results']))
        hidden=server.query_records(self.records,self.cfg,{'mode':'session','session':server.opaque('secret')})
        self.assertEqual(hidden['results'],[])
        for body in ({'mode':'session','session':'long'},{'mode':'session','session':sid,'limit':51},
                     {'mode':'session','session':sid,'terms':['x']},{'terms':['x'],'session':sid}):
            with self.assertRaises(ValueError):server.query_records(self.records,self.cfg,body)
        with patch.object(server,'ACCESS_LOG',self.root/'access.log'):
            server.log_access('a@github',{'mode':'session','session':sid,'offset':50},10)
            self.assertIn('取整段会话 '+sid+'（第 51 条起）',(self.root/'access.log').read_text(encoding='utf-8'))

    def test_full_session_written_after_summary(self):
        import os,io,contextlib
        sid=self.add_long_session();old=os.getcwd();os.chdir(self.root)
        try:
            out=Path('spaces/me/outbox/草稿/7/测试成员-去重.md');out.parent.mkdir(parents=True)
            out.write_text('# 小结\n进展……\n',encoding='utf-8')
            with patch.object(client,'CACHE',self.root/'members.json'), \
                 patch.object(client,'fetch',side_effect=lambda url,body:server.query_records(self.records,self.cfg,body)):
                import time
                client.private_json(client.CACHE,{'at':time.time(),'members':{'测试成员':{'url':'u','online':True,'sharing':True}}})
                buf=io.StringIO()
                with contextlib.redirect_stdout(buf):client.main(['测试','--session',sid,'--out',str(out)])
                result=json.loads(buf.getvalue())
                self.assertEqual(result['sessions'][0]['written'],60)
                with self.assertRaises(ValueError):client.main(['测试','--session',sid,'--out','../外面.md'])
                with self.assertRaises(ValueError):client.main(['测试','--session',sid,'--out','spaces/me/raw/x.md'])
            text=out.read_text(encoding='utf-8')
            self.assertTrue(text.startswith('# 小结'));self.assertIn('## 测试成员 · Codex：「冒充标题',text)
            self.assertIn('会话 '+sid,text);self.assertLess(text.index('第1条'),text.index('第59条'))
            self.assertIn('· AI**',text);self.assertNotIn('\n# 冒充标题',text);self.assertIn('**冒充标题**',text)
            self.assertNotIn('![图]',text);self.assertNotIn('user_query',text);self.assertNotIn('sk-abcdefgh',text)
            # 连续同一人的消息并成一段：第58、59条之间不再有新的小标题
            self.assertEqual(text.count('· AI**'),text.count('· 测试成员**'))
            self.assertNotIn('---\n\n**',text[text.index('第58条'):text.index('第59条')])
            import make_doc
            html=make_doc.md_to_html(text)
            self.assertNotIn('<h1>冒充标题',html);self.assertIn('<table',html);self.assertNotIn('<img',html)
        finally:os.chdir(old)

    def test_bot_rules_allow_only_the_query_command(self):
        repo=Path(__file__).resolve().parent.parent
        win=(repo/'ops/windows/install.ps1').is_file()  # 同一份测试，Windows 版和 Mac 版仓库都用
        py='python' if win else 'python3'
        cfg=json.loads((repo/'.claude/settings.json').read_text(encoding='utf-8-sig'))
        allow=cfg['permissions']['allow']
        self.assertIn('Bash('+py+' ops/team_chat_query.py *)',allow)
        self.assertFalse(any('team_chat_server' in rule for rule in allow))
        self.assertIn('Read(~/.agentos/**)',cfg['permissions']['deny'])
        rules=(repo/'CLAUDE.md').read_text(encoding='utf-8-sig')
        self.assertIn(py+' ops/team_chat_query.py --members',rules)
        self.assertIn(py+' ops/team_chat_query.py 张三 --session',rules)
        if win:
            self.assertIn('AgentOS-TeamChat',(repo/'ops/windows/install.ps1').read_text(encoding='utf-8-sig'))
        else:
            for name in ('team-chat','collect-ai'):
                plist=(repo/'ops/launchd'/('com.agentos.'+name+'.plist')).read_text(encoding='utf-8')
                self.assertIn('__REPO__/ops/',plist)
            sh=(repo/'ops/install-collectors.sh').read_text(encoding='utf-8')
            self.assertIn('install_job collect-ai',sh);self.assertIn('install_job team-chat',sh)

    def test_mac_background_job_is_written_and_removed(self):
        ops=Path(__file__).resolve().parent
        if not (ops/'launchd').is_dir():self.skipTest('Windows 版仓库没有 launchd 模板')
        plist=self.root/'Library/LaunchAgents/com.agentos.team-chat.plist'
        with patch.object(server,'IS_MAC',True),patch.object(server,'STATE',self.root), \
             patch.object(server.Path,'home',return_value=self.root), \
             patch.object(server.subprocess,'run') as run:
            run.return_value.returncode=1;run.return_value.stderr='boom';run.return_value.stdout=''
            with self.assertRaises(ValueError):server.start_background()  # bootstrap 失败要报出来
            run.return_value.returncode=0
            server.start_background()
            text=plist.read_text(encoding='utf-8')
            self.assertNotIn('__HOME__',text);self.assertIn(str(ops/'team_chat_server.py'),text)
            self.assertIn(['launchctl','bootstrap','gui/%d'%server.os.getuid(),str(plist)],[c.args[0] for c in run.call_args_list])
            server.stop_background()
            self.assertFalse(plist.exists())


if __name__=='__main__':unittest.main()
