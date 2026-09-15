import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app as host
import reference_api as api
import reference_media as media


def shot(key='one', start=0, end=2, group='g'):
    return dict(id=key, start=start, end=end, description='人物A转身', camera='中景推进',
                palette='暖色', target_action='阿岚转身看向门口', cast=['阿岚'], group=group,
                reference=['camera', 'rhythm'])


CAST = [dict(name='阿岚', appearance='短发，蓝色外套，年轻女性')]


class PlanningTests(unittest.TestCase):
    def test_nonfinite_and_boolean_times_rejected(self):
        for value in (float('nan'), float('inf'), True, 'bad', None):
            with self.subTest(value=value), self.assertRaises(media.ReferenceError):
                media.validate_shots([shot(start=value)], 0, 10)

    def test_bounds_duplicate_ids_and_unknown_cast(self):
        for items in ([shot(end=11)], [shot(), shot()], [dict(shot(), cast='name')]):
            with self.assertRaises(media.ReferenceError):
                media.validate_shots(items, 0, 10)
        with self.assertRaises(media.ReferenceError):
            media.build_plan([dict(shot(), cast=['不存在'])], CAST, '目标故事', '老街', host.h3_frames_from_duration)

    def test_short_shots_group_into_one_production_segment(self):
        plan = media.build_plan([shot(), shot('two',2,4)], CAST, '寻找朋友', '雨夜老街', host.h3_frames_from_duration)
        self.assertEqual(len(plan['script']['shots']), 1)
        self.assertEqual(len(plan['segment_bindings'][0]['source_ranges']), 2)
        self.assertEqual(plan['script']['shots'][0]['characters'], ['阿岚'])
        self.assertEqual(plan['segment_bindings'][0]['frames'], host.h3_frames_from_duration(4))

    def test_long_shot_split_and_duration_adjustments_visible(self):
        plan = media.build_plan([shot(end=31)], CAST, '目标剧情', '老街', host.h3_frames_from_duration)
        self.assertEqual([s['duration'] for s in plan['script']['shots']], [15,15,3])
        self.assertEqual(plan['segment_bindings'][2]['source_duration'], 1)
        self.assertTrue(plan['segment_bindings'][2]['note'])

    def test_reordered_source_ranges_keep_source_times(self):
        items=media.validate_shots([shot('b',10,12,'b'),shot('a',0,2,'a')],0,12)
        plan=media.build_plan(items,CAST,'剧情','场景',host.h3_frames_from_duration)
        self.assertEqual(plan['segment_bindings'][0]['source_ranges'][0]['start'],10)

    def test_reference_toggles_and_target_story(self):
        plan=media.build_plan([dict(shot(),reference=[])],CAST,'新的剧情','新的场景',host.h3_frames_from_duration)
        s=plan['script']['shots'][0]
        self.assertNotIn('人物A',s['action'])
        self.assertIn('新的剧情',s['action'])
        self.assertEqual(s['camera'],'根据目标剧情设计镜头')
        self.assertEqual(s['duration'],8)

    def test_own_actions_required_and_names_cannot_be_paths(self):
        for chars,items in [(CAST,[dict(shot(),target_action='')]),([dict(name='../evil',appearance='test')],[shot()])]:
            with self.assertRaises(media.ReferenceError):
                media.build_plan(items,chars,'剧情','场景',host.h3_frames_from_duration)

    def test_media_probe_rotation_vfr_and_no_audio(self):
        raw={'streams':[{'codec_type':'video','duration':'3.5','width':640,'height':360,
                         'avg_frame_rate':'20/1','r_frame_rate':'30/1','side_data_list':[{'rotation':90}]}]}
        with patch.object(media,'run_media',return_value=(json.dumps(raw),'')):
            result=media.probe_video('x','probe',lambda:None)
        self.assertEqual(result['rotation'],90)
        self.assertNotEqual(result['frame_rate'],result['avg_frame_rate'])
        self.assertFalse(result['has_audio'])

    def test_audio_only_and_excessive_duration_rejected(self):
        for streams in ([{'codec_type':'audio'}],[{'codec_type':'video','duration':61,'width':10,'height':10}]):
            with patch.object(media,'run_media',return_value=(json.dumps({'streams':streams}),'')):
                with self.assertRaises(media.ReferenceError):media.probe_video('x','probe',lambda:None)

    def test_provider_modes_never_silently_send_local_frames_remote(self):
        config=dict(api.DEFAULTS,reference_local_model='vision',reference_cloud_model='cloud',reference_cloud_url='https://example.com/v1')
        self.assertEqual(api.provider_config(config,'local')['key'],'')
        self.assertEqual(api.provider_config(config,'cloud')['url'],'https://example.com/v1')
        for url in ('http://192.168.1.1/v1','https://example.com/v1','http://user:pass@localhost/v1','http://localhost/v1?q=a'):
            with self.subTest(url=url),self.assertRaises(media.ReferenceError):
                api.provider_config(dict(config,reference_local_url=url),'local')
        with self.assertRaises(media.ReferenceError):api.provider_config(dict(config,reference_cloud_url='http://example.com/v1'),'cloud')


class ReferenceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.projects=self.root/'projects';self.projects.mkdir()
        self.fake_tool=self.root/'ffprobe.exe';self.fake_tool.write_bytes(b'fixture')
        self.config=copy.deepcopy(host.CONFIG)
        self.config.update(reference_local_model='fixture-vision',reference_ffprobe_path=str(self.fake_tool),
                           reference_cloud_url='https://example.com/v1',reference_cloud_model='fixture-cloud')
        self.patches=[patch.object(host,'PROJECTS_DIR',str(self.projects)),patch.object(host,'CONFIG',self.config),
                      patch.object(host,'CONFIG_PATH',str(self.root/'config.json')),
                      patch.object(host,'find_ffmpeg',return_value=str(self.fake_tool))]
        for p in self.patches:p.start()
        self.client=host.app.test_client()
        self.pid=self.client.post('/api/reference/projects',json={}).json['pid']
        self.base='/api/reference/'+self.pid

    def tearDown(self):
        deadline=time.time()+5
        while host.project_job_active(self.pid) and time.time()<deadline:time.sleep(.01)
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def wait(self):
        deadline=time.time()+5
        while host.project_job_active(self.pid) and time.time()<deadline:time.sleep(.01)
        self.assertFalse(host.project_job_active(self.pid),'worker still active')
        return self.client.get(self.base).json

    def seed(self):
        proj=host.load_project(self.pid)
        ref=proj['reference_creation']
        ref.update(source=dict(id='source1',fingerprint='abc',preview='source1/preview.mp4',metadata=dict(duration=10)),
                   selection=[0,10])
        root=self.projects/'.references'/self.pid/'source1';root.mkdir(parents=True)
        (root/'preview.mp4').write_bytes(b'fixture')
        host.save_project(proj)
        return ref

    def fake_frames(self,proxy,s,directory,ffmpeg,check):
        check();name=s['id']+'.jpg';(Path(directory)/name).write_bytes(b'fixture')
        return [dict(file=name,time=(s['start']+s['end'])/2)]

    def analysis_patches(self,handler=None):
        return [patch.object(api,'detect_shots',return_value=[dict(id='one',start=0,end=2),dict(id='two',start=2,end=4)]),
                patch.object(api,'extract_frames',side_effect=self.fake_frames),
                patch.object(api,'analyze_shot',side_effect=handler or (lambda *a:dict(description='转身',camera='中景',palette='暖色',review='请复核')))]

    def analyzed(self):
        self.seed()
        patches=self.analysis_patches()
        for p in patches:p.start()
        try:
            self.assertEqual(self.client.post(self.base+'/analyze',json=dict(start=0,end=4)).status_code,202)
            return self.wait()['reference']
        finally:
            for p in reversed(patches):p.stop()

    def save_plan(self):
        self.seed()
        return self.client.post(self.base+'/edit',json=dict(revision=0,shots=[shot(),shot('two',2,4,'other')],
                                      characters=CAST,story='阿岚寻找朋友',scene='雨夜老街'))

    def test_create_config_keys_and_static_page(self):
        with self.client.get('/reference') as response:
            self.assertEqual(response.status_code,200)
        self.client.post('/api/reference/config',json=dict(reference_cloud_api_key='fixture-secret'))
        self.assertNotIn('fixture-secret',self.client.get('/api/reference/config').text)
        self.assertNotIn('fixture-secret',self.client.get('/api/config').text)
        self.client.post('/api/reference/config',json=dict(reference_cloud_api_key=''))
        self.assertTrue(self.client.get('/api/reference/config').json['config']['cloud_key_set'])
        self.client.post('/api/reference/config',json=dict(clear_cloud_key=True))
        self.assertFalse(self.client.get('/api/reference/config').json['config']['cloud_key_set'])

    def test_reference_config_cannot_bypass_validation_on_legacy_route(self):
        self.assertEqual(self.client.post('/api/config', json=dict(reference_max_mb='invalid')).status_code, 400)
        self.assertEqual(self.client.post('/api/reference/config', json=dict(reference_max_mb='invalid')).status_code, 400)
        self.assertEqual(self.config['reference_max_mb'], 300)

    def test_draft_cannot_start_legacy_production_or_delete_active_job(self):
        self.assertEqual(self.client.get('/api/pipeline/run', query_string=dict(pid=self.pid)).status_code, 409)
        self.assertEqual(self.client.post('/api/preproduction/'+self.pid+'/outline/generate').status_code, 409)
        run = host.begin_production_job(self.pid, 'reference_import')
        try:
            self.assertEqual(self.client.post('/api/project/'+self.pid+'/delete').status_code, 409)
            self.assertIsNotNone(host.load_project(self.pid))
        finally:
            host.update_production_job(self.pid, run, status='done')
            host.end_production_job(self.pid, run)

    def test_unsafe_path_and_wrong_project_rejected(self):
        self.seed()
        self.assertEqual(self.client.get(self.base+'/media/../../outside.mp4').status_code,400)
        self.assertEqual(self.client.get('/api/reference/missing').status_code,400)
        with self.client.get(self.base+'/media/source1/preview.mp4') as response:
            self.assertEqual(response.status_code,200)

    def test_upload_empty_and_wrong_extension_release_lock(self):
        for name,data in [('x.txt',b'test'),('x.mp4',b'')]:
            response=self.client.post(self.base+'/upload',data={'file':(io.BytesIO(data),name)})
            self.assertEqual(response.status_code,400)
            self.assertFalse(host.project_job_active(self.pid))

    def test_bad_video_retains_pending_import_and_can_retry(self):
        with patch.object(api,'probe_video',side_effect=media.ReferenceError('损坏视频')):
            self.assertEqual(self.client.post(self.base+'/upload',data={'file':(io.BytesIO(b'bad'),'x.mov')}).status_code,202)
            result=self.wait()
        self.assertEqual(result['job']['status'],'failed')
        self.assertTrue(result['reference']['pending_source'])

    def test_cloud_requires_exact_current_destination_and_selection(self):
        self.seed()
        for consent in (None,dict(url='https://example.com/v1',model='fixture-cloud',source_id='old',start=0,end=4)):
            response=self.client.post(self.base+'/analyze',json=dict(provider='cloud',start=0,end=4,consent=consent))
            self.assertEqual(response.status_code,400)
        self.assertFalse(host.project_job_active(self.pid))

    def test_analysis_is_candidate_cache_reused_and_edits_not_overwritten(self):
        ref=self.analyzed()
        self.assertEqual(ref['shots'],[])
        aid=ref['latest_analysis']
        self.assertEqual(self.client.post(self.base+'/apply-analysis',json=dict(revision=0,analysis_id=aid)).status_code,200)
        with patch.object(api,'analyze_shot',side_effect=AssertionError('cached analysis must not call provider')):
            self.client.post(self.base+'/analyze',json=dict(start=0,end=4));new=self.wait()['reference']
        self.assertEqual(len(new['analyses']),1)
        self.assertEqual(new['shots'][0]['id'],'one')

    def test_model_revision_invalidates_cache_without_replacing_edits(self):
        previous = self.analyzed()
        self.config['reference_analysis_revision'] = 'updated-model'
        patches = self.analysis_patches()
        for p in patches: p.start()
        try:
            self.client.post(self.base+'/analyze', json=dict(start=0, end=4))
            current = self.wait()['reference']
        finally:
            for p in reversed(patches): p.stop()
        self.assertEqual(len(current['analyses']), 2)
        self.assertNotEqual(previous['latest_analysis'], current['latest_analysis'])
        self.assertEqual(current['shots'], [])

    def test_stop_retains_partial_and_retry_skips_completed_shots(self):
        self.seed();entered=threading.Event();release=threading.Event();calls=[]
        def handler(provider,frames,directory,check):
            calls.append(frames[0]['file'])
            if len(calls)==2:
                entered.set();release.wait(3);check()
            return dict(description='动作',camera='镜头',palette='色调',review='')
        patches=self.analysis_patches(handler)
        for p in patches:p.start()
        try:
            r=self.client.post(self.base+'/analyze',json=dict(start=0,end=4));run=r.json['run_id']
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.client.post(self.base+'/analyze',json=dict(start=0,end=4)).status_code,409)
            self.assertEqual(self.client.post(self.base+'/edit',json=dict(revision=0)).status_code,409)
            for _ in range(2):self.assertIn(self.client.post('/api/project/'+self.pid+'/stop',json=dict(run_id=run)).status_code,(200,202))
            release.set();stopped=self.wait()
            self.assertEqual(stopped['job']['status'],'stopped')
            self.assertEqual(stopped['reference']['analysis_partial']['completed'],1)
            self.client.post(self.base+'/analyze',json=dict(start=0,end=4));done=self.wait()
            self.assertEqual(done['job']['status'],'done')
            self.assertEqual(calls.count('one.jpg'),1)
        finally:
            release.set()
            for p in reversed(patches):p.stop()

    def test_stale_stop_and_edit_conflicts(self):
        self.seed()
        run=host.begin_production_job(self.pid,'reference_analysis')
        try:self.assertEqual(self.client.post('/api/project/'+self.pid+'/stop',json=dict(run_id='old')).status_code,409)
        finally:
            host.update_production_job(self.pid,run,status='done');host.end_production_job(self.pid,run)
        self.assertEqual(self.client.post(self.base+'/edit',json=dict(revision=20)).status_code,409)

    def test_restart_does_not_resubmit_analysis(self):
        self.seed();run=host.begin_production_job(self.pid,'reference_analysis');host.end_production_job(self.pid,run)
        response=self.client.get(self.base).json
        self.assertEqual(response['job']['status'],'failed')
        self.assertFalse(host.project_job_active(self.pid))

    def test_export_preserves_old_outputs_and_supports_first_segment(self):
        result=self.save_plan();self.assertEqual(result.status_code,200,result.text)
        r=self.client.post(self.base+'/export',json=dict(revision=1,test_first=True)).json
        child=r['reference']['last_export'];project=host.load_project(child)
        self.assertEqual(len(project['script']['shots']),1)
        self.assertEqual(project['preproduction']['step'],'script_review')
        self.assertEqual(project['script']['characters'],[dict(CAST[0],visual_locked=True)])
        project['final']='preserved.mp4';host.save_project(project)
        r=self.client.post(self.base+'/export',json=dict(revision=2,test_first=True)).json
        self.assertEqual(r['reference']['last_export'],child)
        self.assertEqual(host.load_project(child)['final'],'preserved.mp4')
        r=self.client.post(self.base+'/export',json=dict(revision=3,test_first=False)).json
        self.assertNotEqual(r['reference']['last_export'],child)
        self.assertEqual(len(host.load_project(r['reference']['last_export'])['script']['shots']),2)

    def test_reapplying_analysis_invalidates_production_plan(self):
        ref=self.analyzed();aid=ref['latest_analysis']
        self.client.post(self.base+'/edit',json=dict(revision=0,shots=[shot()],characters=CAST,story='故事',scene='场景'))
        r=self.client.post(self.base+'/apply-analysis',json=dict(revision=1,analysis_id=aid))
        self.assertNotIn('plan',r.json['reference'])
        self.assertEqual(self.client.post(self.base+'/export',json=dict(revision=2)).status_code,400)

    @unittest.skipUnless(os.environ.get('AIX_TEST_FFMPEG') and os.environ.get('AIX_TEST_FFPROBE'), 'set media tool paths')
    def test_real_media_import_analysis_edit_export_and_script_confirmation(self):
        ffmpeg, ffprobe = os.environ['AIX_TEST_FFMPEG'], os.environ['AIX_TEST_FFPROBE']
        path = self.root/'fixture.mp4'
        media.run_media([ffmpeg, '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                         'testsrc2=s=160x90:r=24:d=3', '-c:v', 'libx264', str(path)])
        self.config['reference_ffprobe_path'] = ffprobe
        with patch.object(host, 'find_ffmpeg', return_value=ffmpeg):
            with path.open('rb') as file:
                response = self.client.post(self.base+'/upload', data={'file': (file, 'fixture.mp4')})
            self.assertEqual(response.status_code, 202)
            imported = self.wait()['reference']
            self.assertEqual(imported['revision'], 1)
            with patch.object(api, 'analyze_shot', return_value=dict(description='人物A走动', camera='全景', palette='暖色', review='确认动作')):
                self.assertEqual(self.client.post(self.base+'/analyze', json=dict(start=0, end=3)).status_code, 202)
                analyzed = self.wait()['reference']
        response = self.client.post(self.base+'/apply-analysis', json=dict(revision=1, analysis_id=analyzed['latest_analysis']))
        items = response.json['reference']['shots']
        self.assertTrue(items[0]['frames'])
        for item in items:
            item.update(target_action='阿岚推开门走进老街', cast=['阿岚'])
        response = self.client.post(self.base+'/edit', json=dict(revision=2, shots=items, characters=CAST, story='阿岚寻找朋友', scene='雨夜老街'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json['reference']['shots'][0]['review'], '确认动作')
        exported = self.client.post(self.base+'/export', json=dict(revision=3)).json['reference']['last_export']
        response = self.client.post('/api/preproduction/'+exported+'/script/confirm', json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(host.load_project(exported)['preproduction']['script_confirmed'])


if __name__=='__main__':
    unittest.main()
