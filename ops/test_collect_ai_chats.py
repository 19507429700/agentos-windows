import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import collect_ai_chats as ai


def check_daily_review(test, raw, day, needle, articles=None):
    """复盘会读到当天的 AI 对话：Windows 版是 daily_review.py，Mac 版是 daily-review.sh（按日期文件夹找原文）。"""
    try:
        import daily_review
    except ImportError:
        sh=(Path(__file__).resolve().parent/'daily-review.sh').read_text(encoding='utf-8')
        test.assertIn('collect_ai_chats.py',sh);test.assertIn('待核实',sh)
        test.assertEqual(len([p for p in raw.glob('*/'+day+'/*.md')]),1)
        return
    with patch.object(daily_review,'RAW',raw):
        files,docs=daily_review.day_files(day)
    test.assertEqual(len(files),1)
    prompt=daily_review.build_prompt(day,files,docs,[],*([articles] if articles else []))
    test.assertIn(needle,prompt)
    if articles:test.assertIn(articles[0],prompt);test.assertIn('待核实',prompt)

class ParserTest(unittest.TestCase):
    def test_custom_roots_and_env(self):
        with patch.dict(ai.os.environ, {'AI_TEST_HOME':str(Path.home()/'alternative-codex')}):
            value='%AI_TEST_HOME%/sessions' if ai.os.name=='nt' else '$AI_TEST_HOME/sessions'
            paths=ai.configured_roots({'roots':{'codex':[value]}})
            self.assertEqual(paths['codex'][0],Path.home()/'alternative-codex/sessions')
        with self.assertRaises(ValueError):ai.configured_roots({'roots':{'codex':'not-a-list'}})
        with self.assertRaises(ValueError):ai.configured_roots({'roots':{'codex':['relative/path']}})

    def test_platforms_and_dates(self):
        w={'type':'message','role':'user','timestamp':1795446000000,'id':'w','content':[{'type':'input_text','text':'完成了接口调试'}]}
        self.assertEqual(ai.parse_event('workbuddy',w,'s')['role'],'user')
        c={'type':'assistant','uuid':'c','timestamp':'2026-11-23T17:00:00Z','message':{'role':'assistant','content':[{'type':'thinking','thinking':'private'},{'type':'tool_use','name':'exec'},{'type':'text','text':'建议先写测试'}]}}
        result=ai.parse_event('claude',c,'s')
        self.assertEqual(result['day'],'2026-11-24')
        self.assertEqual(result['text'],'建议先写测试')
        self.assertIsNone(ai.parse_event('claude',dict(c,isMeta=True),'s'))
        x={'type':'response_item','timestamp':'2026-11-23T10:00:00Z','ordinal':9,'payload':{'type':'message','role':'assistant','channel':'analysis','content':[{'type':'output_text','text':'private'}]}}
        self.assertIsNone(ai.parse_event('codex',x,'s'))
        x['payload']['channel']='final'
        self.assertEqual(ai.parse_event('codex',x,'s')['text'],'private')

    def test_secrets_context_and_tools(self):
        safe=ai.clean_text('<environment_context>cwd=private</environment_context>\nAPI_KEY="sk-12345678901234567890"\n开始做项目')
        self.assertNotIn('cwd=',safe)
        self.assertNotIn('1234567890',safe)
        self.assertIn('开始做项目',safe)
        self.assertIsNone(ai.parse_event('workbuddy',{'type':'function_call_result','output':'secret'},'s'))

    def test_user_clarification_is_preserved(self):
        text='<send_user_message_question_reply>'+json.dumps([{'answer':'我负责技术开发'}])+'</send_user_message_question_reply>'
        self.assertEqual(ai.clean_text(text),'用户补充：我负责技术开发')

    def test_foreign_home_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(ai,'STATE',root):
                (root/'ai_chats.json').write_text(json.dumps({'enabled':'yes'}))
                with self.assertRaises(ValueError):ai.load_config()
                (root/'ai_chats.json').write_text(json.dumps({'exclude_sessions':'not-list'}))
                with self.assertRaises(ValueError):ai.load_config()
        for paths in ai.DEFAULT_ROOTS.values():
            for path in paths:self.assertTrue(Path(path).is_absolute())

    def test_agentos_backend_is_not_independent_claude(self):
        event={'type':'user','uuid':'backend','timestamp':'2026-11-23T08:00:00Z','cwd':str(ai.REPO),'entrypoint':'sdk-cli','message':{'role':'user','content':'你好（系统提示：本任务号1）'}}
        self.assertIsNone(ai.parse_event('claude',event,'s'))
        event['cwd']=str(ai.REPO/'another-project')
        self.assertIsNotNone(ai.parse_event('claude',event,'s'))

class IncrementalTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=ai.open_db(self.root/'state.db');self.p=self.root/'chat.jsonl'
        self.cfg={'exclude_sessions':[],'exclude_cwds':[]}
    def tearDown(self):
        self.db.close();self.tmp.cleanup()
    def event(self,id='one',text='今天准备做接口'):
        return {'type':'message','role':'user','timestamp':'2026-11-23T08:00:00Z','id':id,'content':[{'type':'text','text':text}]}
    def append(self,event,newline=True):
        with self.p.open('a',encoding='utf-8') as f:f.write(json.dumps(event,ensure_ascii=False)+ ('\n' if newline else ''))
    def collect(self):
        with self.db:return ai.collect_file(self.db,'workbuddy',self.p,'2026-11-01',self.cfg)
    def test_partial_append_and_rewrite(self):
        self.append(self.event());self.assertEqual(self.collect(),(1,0));self.assertEqual(self.collect(),(0,0))
        self.append(self.event('two'),False);self.assertEqual(self.collect(),(0,0))
        with self.p.open('a') as f:f.write('\n')
        self.assertEqual(self.collect(),(1,0))
        self.append(self.event('one','接口确实完成'));self.assertEqual(self.collect(),(1,0))
        with self.db:ai.render_dirty(self.db,self.root/'raw')
        files=list((self.root/'raw/ai/2026-11-23').glob('*.md'));self.assertEqual(len(files),1)
        text=files[0].read_text(encoding='utf-8');self.assertIn('接口确实完成',text);self.assertNotIn('今天准备做接口',text.split('接口确实完成')[0])
        self.assertEqual(self.db.execute('select count(*) from messages').fetchone()[0],2)
        self.p.write_text(json.dumps(self.event('three','另一个文件版本'))+'\n',encoding='utf-8')
        self.assertEqual(self.collect(),(1,0))
    def test_historical_and_auto_review(self):
        ev=self.event();ev['timestamp']='2026-10-01T08:00:00Z';self.append(ev);self.assertEqual(self.collect(),(0,0))
        self.append(self.event('daily','按技能 daily-review 写 2026-11-23 的每日复盘'))
        self.append(dict(self.event('reply','已生成复盘'),role='assistant'))
        self.assertEqual(self.collect(),(0,0))
    def test_failed_render_keeps_dirty(self):
        self.append(self.event());self.collect();self.assertEqual(self.db.execute('select count(*) from dirty').fetchone()[0],1)
        with self.db:ai.render_dirty(self.db,self.root/'raw')
        self.assertEqual(self.db.execute('select count(*) from dirty').fetchone()[0],0)
    def test_synthetic_assistant_is_labelled(self):
        self.append(dict(self.event('reply','我已帮你完成上传'),role='assistant'));self.collect()
        with self.db:ai.render_dirty(self.db,self.root/'raw')
        text=next((self.root/'raw').rglob('*.md')).read_text(encoding='utf-8')
        self.assertIn('AI 回复（待核实）',text)

class AttachmentTest(unittest.TestCase):
    """截图和文件：复制进 raw/ai/images|files/<日期>/，原文写路径；其他文件只记名字。"""
    def setUp(self):
        import base64
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.raw=self.root/'raw';self.home=self.root/'home'
        self.png=b'\x89PNG\r\n\x1a\n'+b'x'*100
        blob='ab'+'c'*62
        (self.home/'.workbuddy/blobs/ab').mkdir(parents=True)
        (self.home/'.workbuddy/blobs/ab'/(blob+'.png')).write_bytes(self.png)
        self.docs=self.root/'资料';self.docs.mkdir()
        (self.docs/'期中卷.pdf').write_bytes(b'%PDF-1.4 fake')
        (self.docs/'大.zip').write_bytes(b'zip')
        self.blob=blob;self.b64=base64.b64encode(self.png).decode()
        self.patches=[patch.object(ai,'RAW',self.raw),patch.object(ai,'HOME',self.home)]
        for x in self.patches:x.start()
    def tearDown(self):
        for x in self.patches:x.stop()
        self.tmp.cleanup()
    def test_workbuddy_blob_and_files(self):
        pdf=str(self.docs/'期中卷.pdf');zipf=str(self.docs/'大.zip')
        event={'type':'message','id':'w1','sessionId':'s','role':'user','timestamp':'2026-11-19T07:06:39Z',
               'content':[{'type':'input_text','text':'<user_query>@image#1:Clipboard.png @"'+pdf+'" @"'+zipf+'" 看看</user_query>'},
                          {'type':'image_blob_ref','blob_id':self.blob,'mime':'image/png','blob_path':'C:\\别的电脑\\x.png'},
                          {'type':'input_text','text':'<image_local_path>C:\\Users\\x\\clip.png</image_local_path>'}]}
        msg=ai.parse_event('workbuddy',event,'f')
        text=msg['text']
        self.assertIn('[截图] 可看：spaces/me/raw/ai/images/2026-11-19/',text)
        self.assertEqual(text.count('[截图]'),1)  # blob 和 image_local_path 是同一张，只记一次
        self.assertIn('[文件 期中卷.pdf] 可看：spaces/me/raw/ai/files/2026-11-19/',text)
        self.assertIn('[文件 大.zip]（这种文件不复制，只记文件名）',text)
        self.assertNotIn('@image#1',text);self.assertNotIn('image_local_path',text)
        saved=sorted(p.name for p in (self.raw/'ai').rglob('*') if p.is_file())
        self.assertEqual(len(saved),2);self.assertTrue(any(n.endswith('-期中卷.pdf') for n in saved))
        self.assertEqual(next((self.raw/'ai/images').rglob('*.png')).read_bytes(),self.png)
        # 再解析一次：内容一样、文件不重复
        self.assertEqual(ai.parse_event('workbuddy',event,'f')['text'],text)
        self.assertEqual(len([p for p in (self.raw/'ai').rglob('*') if p.is_file()]),2)
    def test_claude_and_codex_inline_images(self):
        claude={'type':'user','uuid':'c1','sessionId':'s','timestamp':'2026-11-20T01:00:00Z','cwd':str(self.docs),
                'message':{'role':'user','content':[{'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':self.b64}},
                                                     {'type':'text','text':'看图，另外读一下 @期中卷.pdf 和 a@b.com'}]}}
        text=ai.parse_event('claude',claude,'f')['text']
        self.assertIn('[截图] 可看：spaces/me/raw/ai/images/2026-11-20/',text);self.assertIn('.jpg',text)
        self.assertIn('[文件 期中卷.pdf] 可看：',text);self.assertIn('a@b.com',text)
        codex={'type':'response_item','timestamp':'2026-11-21T01:00:00Z',
               'payload':{'type':'message','role':'user','content':[{'type':'input_image','image_url':'data:image/png;base64,'+self.b64},
                                                                    {'type':'input_text','text':'这张图'}]}}
        self.assertIn('[截图] 可看：spaces/me/raw/ai/images/2026-11-21/',ai.parse_event('codex',codex,'f')['text'])
        # 只有截图、没有文字的消息也要留下
        only={'type':'user','uuid':'c2','sessionId':'s','timestamp':'2026-11-20T02:00:00Z',
              'message':{'role':'user','content':[{'type':'image','source':{'type':'base64','media_type':'image/png','data':self.b64}}]}}
        self.assertTrue(ai.parse_event('claude',only,'f')['text'].startswith('[截图] 可看：'))
    def test_missing_large_and_old(self):
        event={'type':'message','id':'w2','sessionId':'s','role':'user','timestamp':'2026-11-19T07:06:39Z',
               'content':[{'type':'image_blob_ref','blob_id':'ff'+'0'*62,'blob_path':'C:\\x.png'},
                          {'type':'input_text','text':'@"D:/不存在/报告.docx"'}]}
        text=ai.parse_event('workbuddy',event,'f')['text']
        self.assertIn('[截图]（电脑上已找不到，没存）',text);self.assertIn('[文件 报告.docx]（电脑上已找不到，没存）',text)
        with patch.object(ai,'MAX_ATTACH',10):
            self.assertIn('超过 20MB',ai.file_marker(str(self.docs/'期中卷.pdf'),'2026-11-19',''))
        # 采集范围以前的旧消息不复制附件
        old=dict(event,content=[{'type':'image_blob_ref','blob_id':self.blob}],id='w3')
        self.assertIsNone(ai.parse_event('workbuddy',old,'f','2026-11-20'))  # 只有图、没有文字：旧消息不留
        self.assertFalse((self.raw/'ai/images').exists())
    def test_old_database_is_reread_once(self):
        db=ai.open_db(self.root/'a.db')
        with db:db.execute("INSERT INTO files VALUES('x',10,'p',1,1)");db.execute("INSERT INTO files VALUES('g',0,'snapshot-v1',1,1)")
        db.execute("DELETE FROM meta");db.commit();db.close()
        db=ai.open_db(self.root/'a.db')
        self.assertEqual([r[0] for r in db.execute('SELECT path FROM files')],['g'])
        with db:db.execute("INSERT INTO files VALUES('x',10,'p',1,1)")
        db.close();db=ai.open_db(self.root/'a.db')
        self.assertEqual(len(db.execute('SELECT path FROM files').fetchall()),2);db.close()


class IntegrationTest(unittest.TestCase):
    def test_custom_directory_collection_search_and_review(self):
        import search
        from datetime import datetime
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=root/'state';state.mkdir()
            sessions=root/'数据盘'/'WorkBuddy'/'projects';sessions.mkdir(parents=True)
            raw=root/'space'/'raw';now=datetime.now(ai.TZ)
            event={'type':'message','id':'fixture','sessionId':'fixture-session','role':'user',
                   'timestamp':now.isoformat(),'content':[{'type':'text','text':'数字信号处理复习计划'}]}
            (sessions/'session.jsonl').write_text(json.dumps(event,ensure_ascii=False)+'\n',encoding='utf-8')
            (state/'ai_chats.json').write_text(json.dumps({'roots':{'workbuddy':[str(sessions)]}}),encoding='utf-8')
            with patch.object(ai,'STATE',state),patch.object(ai,'RAW',raw):
                self.assertEqual(ai.main([]),0)
                self.assertEqual(ai.main([]),0)
            records=list((raw/'ai').rglob('*.md'));self.assertEqual(len(records),1)
            db=search.open_index(root/'space')
            try:
                search.refresh(db,root/'space')
                rows,total=search.search(db,['数字信号'],source='ai')
                self.assertEqual(total,1);self.assertIn('用户',rows[0][2])
            finally:db.close()
            check_daily_review(self,raw,now.date().isoformat(),'workbuddy-',['article-fixture.md'])

    @unittest.skipUnless(ai.os.name=='nt','Windows 用户目录模拟')
    def test_other_windows_user_default_paths(self):
        import importlib.util
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)/'其他用户 空格'
            with patch.dict(ai.os.environ,{'USERPROFILE':str(home),'CODEX_HOME':str(home/'数据/Codex')}):
                spec=importlib.util.spec_from_file_location('other_user_collector',ai.__file__)
                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            self.assertEqual(module.DEFAULT_ROOTS['workbuddy'],[str(home/'.workbuddy/projects')])
            self.assertEqual(module.DEFAULT_ROOTS['codex'][0],str(home/'数据/Codex/sessions'))

if __name__=='__main__':unittest.main()
