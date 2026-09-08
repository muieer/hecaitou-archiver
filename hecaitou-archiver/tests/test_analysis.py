"""Only the Ark migration; article inputs come from the latest existing local Markdown."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from hecaitou_archiver import analysis, cli, dashboard, _lm_http
from hecaitou_archiver.credentials import DEFAULT_KEY_FILE, read_api_key
from live_model_selftest import latest_article, PROJECT

ANSWER = {key: '无' for key in analysis.FIELDS}
REPLY = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(ANSWER)}}]}

class Log:
    def __init__(self): self.events = []
    def emit(self, event, **fields): self.events.append(dict(event=event, **fields))

class CloudAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = latest_article(PROJECT.parent / '文章存档')
        cls.markdown = cls.source.read_text(encoding='utf-8')
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Path(self.temp.name) / 'key.txt'
        self.fake = 'test-only-not-a-real-key'
        self.key.write_text(self.fake + '\n')
        self.log = Log()
    def request(self):
        return analysis.request_json(analysis.DEFAULT_API_URL, '/chat/completions', {}, 5, self.log, self.key)
    def test_key_file_and_invalid_values(self):
        self.key.write_text('\ufeff  ' + self.fake + '\n')
        self.assertEqual(read_api_key(self.key), self.fake)
        for value in ['', 'a\nb', 'a b', '\x00']:
            self.key.write_text(value)
            with self.assertRaises(ValueError): read_api_key(self.key)
        self.key.unlink()
        with self.assertRaises(ValueError): read_api_key(self.key)
    def test_default_key_ignored_and_untracked(self):
        self.assertTrue(DEFAULT_KEY_FILE.is_absolute())
        self.assertEqual(subprocess.run(['git','check-ignore','-q',str(DEFAULT_KEY_FILE)], cwd=PROJECT).returncode, 0)
        self.assertNotEqual(subprocess.run(['git','ls-files','--error-unmatch',str(DEFAULT_KEY_FILE)], cwd=PROJECT, capture_output=True).returncode, 0)
    def test_payload_cloud_model_full_text_no_image(self):
        body = analysis.build_request(self.markdown, {'id': analysis.DEFAULT_MODEL})
        self.assertEqual(body['model'], 'doubao-seed-2-1-turbo-260628')
        self.assertTrue(body['messages'][1]['content'].endswith(self.markdown))
        self.assertTrue(all(isinstance(m['content'], str) for m in body['messages']))
        self.assertEqual(body['response_format'], {'type':'json_object'})
    def test_key_path_not_value_passed_to_worker(self):
        done = subprocess.CompletedProcess([], 0, json.dumps(REPLY).encode(), b'')
        with patch.object(analysis.subprocess,'run',return_value=done) as run:
            value = analysis.generate(self.markdown, {'id':analysis.DEFAULT_MODEL}, analysis.DEFAULT_API_URL, 5, self.log, self.key)
        self.assertEqual(value, ANSWER)
        self.assertEqual(run.call_count, 1)
        config = json.loads(run.call_args.kwargs['input'])
        self.assertEqual(config['url'], 'https://ark.cn-beijing.volces.com/api/v3/chat/completions')
        self.assertEqual(config['key_file'], str(self.key.resolve()))
        self.assertNotIn(self.fake, repr(run.call_args) + repr(self.log.events))
    def test_missing_key_no_network(self):
        self.key.unlink()
        with patch.object(analysis.subprocess,'run') as run, self.assertRaises(cli.ArchiveError) as error:
            self.request()
        self.assertEqual(error.exception.code, 'ark_key_error')
        run.assert_not_called()
    def test_auth_failure_no_retry_no_secret_leak(self):
        done = subprocess.CompletedProcess([], 1, b'', json.dumps({'http_status':401,'message':'Invalid '+self.fake}).encode())
        with patch.object(analysis.subprocess,'run',return_value=done) as run, self.assertRaises(cli.ArchiveError) as error:
            self.request()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(error.exception.code, 'lm_api_error')
        self.assertNotIn(self.fake, str(error.exception) + repr(self.log.events))
    def test_rate_limit_retries_once(self):
        bad = subprocess.CompletedProcess([], 1, b'', b'{"http_status":429,"message":"rate limited"}')
        good = subprocess.CompletedProcess([], 0, json.dumps(REPLY).encode(), b'')
        with patch.object(analysis.subprocess,'run',side_effect=[bad,good]) as run:
            self.assertEqual(self.request(), REPLY)
        self.assertEqual(run.call_count, 2)
    def test_timeout_only_two_attempts(self):
        with patch.object(analysis.subprocess,'run',side_effect=subprocess.TimeoutExpired('worker',5)) as run, self.assertRaises(cli.ArchiveError) as error:
            self.request()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(error.exception.code, 'lm_timeout')
    def test_worker_bearer_header(self):
        config = {'url':analysis.DEFAULT_API_URL+'/chat/completions','body':{},'key_file':str(self.key),'timeout':5}
        response = io.BytesIO(json.dumps(REPLY).encode());response.headers = {}
        out = io.TextIOWrapper(io.BytesIO(),encoding='utf-8');err=io.StringIO()
        with patch.object(_lm_http.urllib.request,'build_opener') as build, patch.object(sys,'stdin',io.StringIO(json.dumps(config))), patch.object(sys,'stdout',out), redirect_stderr(err):
            build.return_value.open.return_value = response
            result = _lm_http.main()
        self.assertEqual(result, 0)
        self.assertEqual(build.return_value.open.call_args.args[0].get_header('Authorization'), 'Bearer '+self.fake)
        self.assertNotIn(self.fake, out.buffer.getvalue().decode()+err.getvalue())
        out.close()
    def test_worker_redacts_error_and_disallows_redirects(self):
        config={'url':analysis.DEFAULT_API_URL+'/chat/completions','body':{},'key_file':str(self.key),'timeout':5}
        err=io.StringIO()
        with patch.object(_lm_http.urllib.request,'build_opener') as build, patch.object(sys,'stdin',io.StringIO(json.dumps(config))), redirect_stderr(err):
            build.return_value.open.side_effect=HTTPError(config['url'],401,'Unauthorized',{},io.BytesIO(self.fake.encode()))
            self.assertEqual(_lm_http.main(),1)
        self.assertNotIn(self.fake,err.getvalue())
        self.assertIsNone(_lm_http.NoRedirect().redirect_request(None,None,302,'',{},'https://other.example'))
    def test_cloud_record_and_existing_result_reuse(self):
        source=Path(self.temp.name)/'正文.md';source.write_text(self.markdown)
        result={'analysis':{'status':'not_started'}}
        with patch.object(analysis,'generate',return_value=ANSWER) as generate:
            analysis.analyze_archive(source,self.key,5,result,self.log)
        self.assertEqual(generate.call_args.args[1]['id'],analysis.DEFAULT_MODEL)
        self.assertEqual(json.loads(source.with_name('.analysis.json').read_text())['provider'],'volcengine_ark')
        with patch.object(analysis,'generate') as generate:
            analysis.analyze_archive(source,self.key,5,result,self.log)
        generate.assert_not_called()
        self.assertEqual(result['analysis']['status'],'skipped_existing')
    def test_cli_and_dashboard_key_argument(self):
        def execute(root,timeout,lock_timeout,started,result,log,key_file,analysis_timeout):
            self.assertEqual(key_file,self.key)
            result.update(status='skipped_not_today',archive_status='skipped_not_today')
        with patch.object(cli,'execute',side_effect=execute),redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--output-dir',str(Path(self.temp.name)/'out'),'--ark-key-file',str(self.key),'--lm-url','http://127.0.0.1:2051']),0)
        done=subprocess.CompletedProcess([],0,'{"status":"skipped_not_today"}','')
        with patch.object(dashboard.subprocess,'run',return_value=done) as run:
            dashboard.run_pipeline(Path(self.temp.name),self.key,5)
        command=run.call_args.args[0]
        self.assertIn('--ark-key-file',command)
        self.assertIn(str(self.key),command)
        self.assertNotIn('--lm-url',command)

if __name__ == '__main__': unittest.main(verbosity=2)
