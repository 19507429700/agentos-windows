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
