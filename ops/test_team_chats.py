from datetime import datetime
from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import collect_ai_chats as ai
import team_chat_server as server
import team_chat_query as client


class TeamChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.records=self.root/'ai_chats.db';self.config=self.root/'sharing.json'
        self.token='a-test-token-'+'1'*32
        self.grant={'token_sha256':hashlib.sha256(self.token.encode()).hexdigest(),
                    'days':7,'platforms':{'grokbot':['allowed']}}
        server.private_json(self.config,{'name':'测试成员','enabled':True,'clients':{'jiaxu':self.grant}})
        now=datetime.now(ai.TZ)
        with closing(ai.open_db(self.records)) as db, db:
            for src,sid,mid,text in [('grokbot','allowed','1','消息去重需要处理幂等键'),
                                     ('grokbot','private','2','消息去重 PRIVATE_SESSION'),
                                     ('workbuddy','allowed','3','消息去重 PRIVATE_PLATFORM')]:
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',
                           (src,sid,mid,now.isoformat(),now.date().isoformat(),'user',text))
        self.http=None
    def tearDown(self):
        if self.http:
            self.http.shutdown();self.http.server_close();self.thread.join(timeout=5)
        self.tmp.cleanup()
    def start(self):
        self.http=server.ChatHTTPServer(('127.0.0.1',0),self.config,self.records)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        return {'url':'http://127.0.0.1:'+str(self.http.server_port),'token':self.token}
    def test_scopes_applied_before_results_and_limit(self):
        result=server.query_records(self.records,self.grant,{'terms':['消息去重'],'limit':1},'成员')
        self.assertEqual(len(result['results']),1)
        self.assertNotIn('PRIVATE',result['results'][0]['text'])
        self.assertEqual(result['results'][0]['session'],server.opaque('allowed'))
        with self.assertRaises(PermissionError):
            server.query_records(self.records,self.grant,{'terms':['消息'],'platform':'workbuddy'},'成员')
    def test_recent_and_validation(self):
        result=server.query_records(self.records,self.grant,{'mode':'recent','days':30},'成员')
        self.assertEqual(len(result['results']),1)
        for body in ({'terms':['x'],'limit':1000},{'terms':['x'],'days':True},{'mode':'exec','terms':[]},
                     {'terms':['x'],'path':'../private'},{'terms':[]}):
            with self.assertRaises(ValueError):server.query_records(self.records,self.grant,body,'成员')
    def test_expired_dates_and_literal_wildcards(self):
        with closing(server.connect_records(self.records)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],3)
        result=server.query_records(self.records,self.grant,{'terms':['%']},'成员')
        self.assertEqual(result['results'],[])
        # A valid grant cannot leak older or future-dated imported records.
        db=ai.open_db(self.records)
        for day in ('2001-01-01','2099-01-01'):
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',('grokbot','allowed',day,day+'T00:00:00+08:00',day,'user','不可外泄'))
        db.commit();db.close()
        result=server.query_records(self.records,self.grant,{'mode':'recent'},'成员')
        self.assertEqual(len(result['results']),1)
    def test_http_auth_query_live_revoke(self):
        peer=self.start();result=client.fetch(peer,{'terms':['消息去重']})
        self.assertEqual(len(result['results']),1)
        with self.assertRaises(HTTPError) as ctx:client.fetch(dict(peer,token='bad'),{'mode':'recent'})
        self.assertEqual(ctx.exception.code,401)
        server.private_json(self.config,{'name':'成员','enabled':True,'clients':{}})
        with self.assertRaises(HTTPError) as ctx:client.fetch(peer,{'mode':'recent'})
        self.assertEqual(ctx.exception.code,401)
    def test_network_urls_and_no_proxy(self):
        self.assertEqual(server.validate_url('https://alice.example.ts.net/'),'https://alice.example.ts.net')
        for url in ('https://example.com','http://100.80.0.2:8765','https://alice.ts.net/secret',
                    'https://a:b@alice.ts.net','file:///private','https://alice.ts.net:444'):
            with self.assertRaises(ValueError):server.validate_url(url)
        self.assertIsNone(client.NoRedirect().redirect_request(None,None,302,None,None,'https://example.com'))
    def test_admin_init_grant_invite_and_revoke(self):
        config=self.root/'admin.json';peers=self.root/'peers.json'
        # Admin commands use a separate current-user STATE, no model access needed.
        with patch.object(server,'STATE',self.root),patch.object(server,'CONFIG',config),patch.object(server,'PEERS',peers):
            server.main(['init','--name','成员'])
            server.main(['grant','--client','jiaxu','--url','https://alice.demo.ts.net',
                         '--platform','grokbot','--session',server.opaque('allowed')])
            invite=self.root/'team-invitations/jiaxu.json'
            server.main(['add-peer',str(invite),'--alias','张三'])
            self.assertIn('张三',server.read_json(peers))
            cfg=server.read_json(config);self.assertTrue(cfg['enabled'])
            self.assertNotIn('token',cfg['clients']['jiaxu'])
            import io,contextlib
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                server.main(['sessions','--platform','grokbot']);server.main(['status'])
            text=out.getvalue()
            self.assertIn('开头：消息去重需要处理幂等键',text)
            self.assertNotIn('PRIVATE_PLATFORM',text)
            self.assertIn('jiaxu：grokbot（1 个会话）；最近 7 天',text)
            self.assertNotIn(cfg['clients']['jiaxu']['token_sha256'],text)
            server.main(['revoke','--client','jiaxu']);self.assertEqual(server.read_json(config)['clients'],{})
    def test_session_titles_strip_wrappers(self):
        self.assertEqual(server.title_of('<user_query>帮我改周报</user_query>'),'帮我改周报')
        self.assertEqual(server.title_of('<user_query>@"D:/a/b/说明.pdf" 存到飞书</user_query>'),'@说明.pdf 存到飞书')
        self.assertEqual(server.title_of('# Files mentioned by the user:\n## a.py\n## My request for Codex:\n修登录'),'修登录')
        self.assertEqual(server.title_of('The following is the Codex agent history\n...'),'')
        self.assertEqual(ai.clean_text('<user_query>原话</user_query>'),'原话')
        self.assertEqual(server.title_of('<turn_aborted>The user interrupted</turn_aborted>\n接着改'),'接着改')
        self.assertEqual(ai.clean_text('<turn_aborted>x</turn_aborted>'),'')
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
