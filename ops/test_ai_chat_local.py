import base64
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ai_chat_local as local
import collect_ai_chats as ai


class LocalSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = ai.open_db(self.root/'test.db')
        key = 'sand.client.slice.account.test-user.transcript.replicas.session-1'
        self.path = self.root/(base64.b32encode(key.encode()).decode().rstrip('=').lower()+'.blob')
        self.cfg = {'exclude_sessions': [], 'exclude_cwds': []}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def write(self, entries, version=1):
        self.path.write_text(json.dumps({'schemaVersion':version,'value':{'entries':entries}}), encoding='utf-8')

    def message(self, **kw):
        return {'kind':'message','id':'u1','role':'user','content':'排查飞书消息去重',
                'timestampMs':1795510800000,'isStreaming':False, **kw}

    def collect(self, source='grokbot', path=None):
        with self.db:
            return ai.collect_snapshot(self.db,source,path or self.path,'2026-11-01',self.cfg)

    def test_grok_real_shape_text_voice_and_exclusions(self):
        self.write([self.message(),
            {'kind':'send-message','id':'a1','timestampMs':1795510800001,
             'message':{'type':'text','content':'建议检查 message_id'}},
            self.message(id='stream',isStreaming=True),
            {'kind':'send-message','id':'secret','timestampMs':1795510800001,
             'message':{'type':'secret-request','secretRequest':{'text':'DONT_IMPORT'}}},
            {'kind':'event','id':'tool','text':'DONT_IMPORT'},
            {'kind':'voice-call','id':'call','conversation':{'turns':[
                {'id':'v1','speaker':'user','text':'语音讨论重试','atMs':1795510801000},
                {'id':'v2','speaker':'assistant','text':'还要验证','atMs':1795510802000},
                {'id':'v3','speaker':'user','text':'relative','atMs':1234}],
                'toolCalls':[{'text':'DONT_IMPORT'}]}}])
        self.assertEqual(self.collect(),(4,0))
        self.assertEqual(self.collect(),(0,0))
        text=' '.join(x[0] for x in self.db.execute('SELECT text FROM messages'))
        self.assertNotIn('DONT_IMPORT',text)
        self.assertNotIn('relative',text)
        self.assertEqual(self.db.execute("SELECT role FROM messages WHERE id='a1'").fetchone()[0],'assistant')
        with self.db:ai.render_dirty(self.db,self.root/'raw')
        self.assertIn('待核实',next((self.root/'raw').rglob('*.md')).read_text(encoding='utf-8'))

    def test_snapshot_rotation_preserves_archive_and_updates_id(self):
        self.write([self.message()]);self.collect()
        self.write([self.message(content='已经运行测试确认消息去重'),self.message(id='u2',content='第二轮')])
        self.assertEqual(self.collect(),(2,0))
        self.write([self.message(id='u3',content='旧消息被客户端缓存淘汰')]);self.collect()
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0],3)

    def test_unknown_and_partial_snapshot_not_marked_processed(self):
        self.write([],version=99)
        with self.assertRaises(ValueError):self.collect()
        self.assertEqual(self.db.execute('SELECT count(*) FROM files').fetchone()[0],0)
        self.path.write_text('{')
        with self.assertRaises(ValueError):self.collect()
        self.write([{'kind':'voice-call','conversation':{'turns':None}}])
        with self.assertRaises(ValueError):self.collect()
        self.assertEqual(self.db.execute('SELECT count(*) FROM files').fetchone()[0],0)
        self.write([self.message()]);self.assertEqual(self.collect(),(1,0))

    def test_only_transcript_blobs_are_opened(self):
        for key in ('sand.client.slice.account.test-user.mcp.credentials', 'secrets'):
            p=self.root/(base64.b32encode(key.encode()).decode().rstrip('=').lower()+'.blob')
            p.write_text('DO NOT OPEN')
        self.write([])
        self.assertEqual(local.find_snapshots('grokbot',self.root),[local.native_path(self.path)])

    def test_original_dates_filters_and_redaction(self):
        self.write([self.message(timestampMs=1793440800000), self.message(id='secret',content='API_KEY=sk-12345678901234567890')])
        self.collect()
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
        self.assertNotIn('1234567890',self.db.execute('SELECT text FROM messages').fetchone()[0])
        self.cfg['exclude_sessions']=[local.grok_key(self.path)]
        self.write([self.message(id='excluded')]);self.assertEqual(self.collect(),(0,0))

    def test_wider_history_window_reprocesses_unchanged_snapshot(self):
        self.write([self.message(id='old',timestampMs=1793440800000)])
        with self.db:self.assertEqual(ai.collect_snapshot(self.db,'grokbot',self.path,'2026-11-01',self.cfg),(0,0))
        with self.db:self.assertEqual(ai.collect_snapshot(self.db,'grokbot',self.path,'2026-10-01',self.cfg),(1,0))

    def test_jsonl_wider_history_window_reprocesses_skipped_records(self):
        p=self.root/'history.jsonl'
        p.write_text(json.dumps({'type':'message','id':'history','role':'user',
            'content':'历史消息回填','timestamp':'2026-10-31T10:00:00Z'})+'\n',encoding='utf-8')
        with self.db:self.assertEqual(ai.collect_file(self.db,'workbuddy',p,'2026-11-01',self.cfg),(0,0))
        with self.db:self.assertEqual(ai.collect_file(self.db,'workbuddy',p,'2026-10-01',self.cfg),(1,0))
        with self.db:self.assertEqual(ai.collect_file(self.db,'workbuddy',p,'2026-10-01',self.cfg),(0,0))

    def test_grok_cli_to_existing_search_and_daily_review(self):
        import search
        state=self.root/'state';state.mkdir()
        raw=self.root/'space/raw'
        now=datetime.now(ai.TZ)
        self.write([self.message(timestampMs=int(now.timestamp()*1000))])
        (state/'ai_chats.json').write_text(json.dumps({'roots':{'grokbot':[str(self.root)]}}),encoding='utf-8')
        with patch.object(ai,'STATE',state),patch.object(ai,'RAW',raw):
            self.assertEqual(ai.main([]),0)
            self.assertEqual(ai.main([]),0)
        index=search.open_index(self.root/'space')
        try:
            search.refresh(index,self.root/'space')
            rows,total=search.search(index,['飞书消息去重'],source='ai')
            self.assertEqual(total,1);self.assertIn('grokbot-',rows[0][0])
        finally:index.close()
        from test_collect_ai_chats import check_daily_review
        check_daily_review(self,raw,now.date().isoformat(),'grokbot-')

    def test_windows_mac_paths_and_long_name(self):
        mac=local.default_snapshot_roots(self.root,self.root/'state','darwin')
        self.assertEqual(mac['grokbot'],[str(self.root/'Library/Application Support/Grok Bot/sand-client-persistence')])
        with patch.dict(os.environ,{'APPDATA':str(self.root/'Roaming')}):
            win=local.default_snapshot_roots(self.root,self.root/'state','win32')
        self.assertEqual(win['grokbot'],[str(self.root/'Roaming/Grok Bot/sand-client-persistence')])
        if os.name=='nt':
            deep=local.native_path(self.root/('long-directory-'*12))
            deep.mkdir()
            file=deep/self.path.name
            file.write_text(json.dumps({'schemaVersion':1,'value':{'entries':[]}}))
            self.assertGreater(len(str(file)),260)
            self.assertEqual(local.read_snapshot(file)['schemaVersion'],1)
            file.unlink();deep.rmdir()  # tempfile cleanup itself does not enable long paths.



if __name__=='__main__':unittest.main()
