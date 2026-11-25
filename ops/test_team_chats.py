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
        devices=[(url,True,'tager','me@github'),('https://laptop.example.ts.net',False,'laptop','aiyj@github'),
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

    def test_bot_rules_allow_only_the_query_command(self):
        repo=Path(__file__).resolve().parent.parent
        cfg=json.loads((repo/'.claude/settings.json').read_text(encoding='utf-8-sig'))
        allow=cfg['permissions']['allow']
        self.assertIn('Bash(python ops/team_chat_query.py *)',allow)
        self.assertFalse(any('team_chat_server' in rule for rule in allow))
        self.assertIn('Read(~/.agentos/**)',cfg['permissions']['deny'])
        rules=(repo/'CLAUDE.md').read_text(encoding='utf-8-sig')
        self.assertIn('python ops/team_chat_query.py --members',rules)
        ps=(repo/'ops/windows/install.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('AgentOS-TeamChat',ps)


if __name__=='__main__':unittest.main()
