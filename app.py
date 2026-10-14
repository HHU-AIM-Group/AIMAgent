#!/usr/bin/env python3
"""AIM Radar: loopback-only research radar, Python 3.9+, no runtime packages."""
import argparse
import copy
import json
import mimetypes
import re
import secrets
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codex_bridge import codex_status
from pipeline import DEFAULT_SETTINGS, collect_report, default_window, report_markdown, validate_window
from sources import validate_public_url
from storage import Store

ROOT = Path(__file__).resolve().parent


def validate_settings(payload, current):
    if not isinstance(payload, dict) or set(payload) - set(DEFAULT_SETTINGS):
        raise ValueError('设置字段无效')
    result = copy.deepcopy(current)
    if 'model' in payload:
        model = payload['model']
        if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,99}',model):
            raise ValueError('模型名称格式无效')
        result['model'] = model
    if 'codex_path' in payload:
        value = payload['codex_path']
        if not isinstance(value,str) or len(value)>1000 or ('\x00' in value):
            raise ValueError('Codex 路径格式无效')
        if value and (not Path(value).expanduser().is_absolute() or Path(value).name.lower() not in ('codex','codex.exe')):
            raise ValueError('请填写 codex 可执行文件的绝对路径')
        result['codex_path'] = value
    if 'sources' in payload:
        value = payload['sources']
        if not isinstance(value,dict) or set(value)-set(DEFAULT_SETTINGS['sources']) or any(type(toggle) is not bool for toggle in value.values()):
            raise ValueError('来源开关格式无效')
        result['sources'].update(value)
    if 'openreview_venues' in payload:
        value = payload['openreview_venues']
        if not isinstance(value,list) or len(value)>3 or any(venue not in ('ICLR','ICML','NeurIPS') for venue in value):
            raise ValueError('OpenReview 会议组无效')
        result['openreview_venues'] = list(dict.fromkeys(value))
    if 'extra_feeds' in payload:
        value = payload['extra_feeds']
        if not isinstance(value,list) or len(value)>8 or any(not isinstance(url,str) or len(url)>2000 for url in value):
            raise ValueError('最多填写8个公开 HTTPS 订阅地址')
        result['extra_feeds'] = [validate_public_url(url) for url in value]
    return result


class RadarApp:
    def __init__(self, data_dir=None, profile_path=None):
        self.data_dir = Path(data_dir or ROOT/'data')
        self.store = Store(self.data_dir/'radar.sqlite3')
        self.profile = json.loads(Path(profile_path or ROOT/'config'/'profile.json').read_text(encoding='utf-8'))
        self.csrf_token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.jobs, self.active_job = {}, None
        saved = self.store.get('settings',{})
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.settings.update({key:value for key,value in saved.items() if key!='sources'})
        self.settings['sources'].update(saved.get('sources',{}))
    def bootstrap(self):
        reports = self.store.reports()
        briefs = [{key:report.get(key) for key in ('id','start','end','created_at','status','analysis_mode','paper_count','news_count')} for report in reports]
        with self.lock:
            active = copy.deepcopy(self.jobs.get(self.active_job))
        return dict(profile=self.profile,settings=self.settings,codex=codex_status(self.settings),reports=briefs,active_job=active,latest_report=reports[0] if reports else None,csrf_token=self.csrf_token,default_window=dict(zip(('start','end'),default_window())))
    def start_run(self, payload):
        if not isinstance(payload,dict) or set(payload)-{'start','end','use_codex'}:
            raise ValueError('运行参数无效')
        start,end = default_window()
        start,end = validate_window(payload.get('start',start),payload.get('end',end))
        use_codex = payload.get('use_codex',True)
        if type(use_codex) is not bool:
            raise ValueError('use_codex 必须为布尔值')
        with self.lock:
            if self.active_job:
                raise RuntimeError('已有采集任务正在运行')
            job_id = uuid.uuid4().hex[:16]
            job = dict(id=job_id,status='queued',progress=0,stage='queued',message='等待采集',report_id=None,error=None)
            self.jobs[job_id],self.active_job = job,job_id
            settings = copy.deepcopy(self.settings)
        threading.Thread(target=self._run,args=(job_id,start,end,use_codex,settings),daemon=True).start()
        return copy.deepcopy(job)
    def _run(self, job_id, start, end, use_codex, settings):
        def progress(percent,stage,message):
            with self.lock:
                self.jobs[job_id].update(status='running',progress=percent,stage=stage,message=message)
        try:
            report = collect_report(self.store,self.profile,settings,start,end,use_codex,progress,self.data_dir)
            with self.lock:
                self.jobs[job_id].update(status='completed',progress=100,stage='completed',message='报告已保存到本机。',report_id=report['id'])
        except Exception as exc:
            with self.lock:
                self.jobs[job_id].update(status='failed',stage='failed',message='本次运行未完成，历史报告仍可查看。',error=str(exc)[:400])
        finally:
            with self.lock:
                self.active_job = None


class RadarServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, address=('127.0.0.1',8765), app=None):
        if address[0] != '127.0.0.1':
            raise ValueError('AIM Radar only binds to 127.0.0.1')
        self.app = app or RadarApp()
        super().__init__(address,Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'AIMRadar/1.0'
    def log_message(self, format, *args):
        # No request body, headers, model output or credential information in logs.
        pass
    def _headers(self, status, content_type='application/json; charset=utf-8', length=None, attachment=None):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if length is not None:
            self.send_header('Content-Length',str(length))
        if attachment:
            self.send_header('Content-Disposition','attachment; filename="'+attachment+'"')
        self.end_headers()
    def _json(self, status, payload):
        body = json.dumps(payload,ensure_ascii=False).encode('utf-8')
        self._headers(status,length=len(body)); self.wfile.write(body)
    def _guard(self, mutation=False):
        host = self.headers.get('Host','')
        try:
            parsed = urllib.parse.urlsplit('http://'+host)
            valid = parsed.hostname in ('127.0.0.1','localhost') and parsed.port == self.server.server_port and not parsed.username and not parsed.password and not parsed.path
        except ValueError:
            valid = False
        if not valid:
            self._json(403,{'error':'仅允许本机 loopback Host 访问'}); return False
        if mutation:
            origin = self.headers.get('Origin')
            if origin:
                try:
                    parsed = urllib.parse.urlsplit(origin)
                    allowed = parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1') and parsed.port==self.server.server_port and not parsed.username and not parsed.password and not parsed.path
                except ValueError:
                    allowed = False
                if not allowed:
                    self._json(403,{'error':'请求来源不被允许'}); return False
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),self.server.app.csrf_token):
                self._json(403,{'error':'缺少有效本机会话令牌，请刷新页面'}); return False
            if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':
                self._json(415,{'error':'请求必须使用 application/json'}); return False
        return True
    def do_OPTIONS(self):
        self._json(405,{'error':'跨域访问未开放'})
    def do_GET(self):
        if not self._guard():
            return
        try:
            self._get()
        except (BrokenPipeError,ConnectionResetError):
            pass
        except Exception:
            self._json(500,{'error':'本机服务无法完成请求，请检查数据目录或重启服务'})
    def _get(self):
        app = self.server.app
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path=='/api/health':
            return self._json(200,{'ok':True})
        if path=='/api/bootstrap':
            return self._json(200,app.bootstrap())
        match = re.fullmatch(r'/api/jobs/([a-f0-9]{16})',path)
        if match:
            with app.lock:
                job = copy.deepcopy(app.jobs.get(match.group(1)))
            return self._json(200,{'job':job}) if job else self._json(404,{'error':'任务不存在，服务重启后请查看报告历史'})
        match = re.fullmatch(r'/api/reports/([a-f0-9]{16})(/export)?',path)
        if match:
            report = app.store.report(match.group(1))
            if not report:
                return self._json(404,{'error':'报告不存在'})
            if not match.group(2):
                return self._json(200,{'report':report})
            fmt = urllib.parse.parse_qs(parsed.query).get('format',['markdown'])[0]
            if fmt not in ('markdown','json'):
                return self._json(400,{'error':'导出格式必须为 markdown 或 json'})
            body = (json.dumps(report,ensure_ascii=False,indent=2) if fmt=='json' else report_markdown(report)).encode('utf-8')
            self._headers(200,'application/json; charset=utf-8' if fmt=='json' else 'text/markdown; charset=utf-8',len(body),'aim-radar-%s-%s.%s' % (report['start'],report['end'],'json' if fmt=='json' else 'md'))
            return self.wfile.write(body)
        if path.startswith('/api/'):
            return self._json(404,{'error':'接口不存在'})
        relative = 'index.html' if path=='/' else urllib.parse.unquote(path[len('/static/'):]) if path.startswith('/static/') else ''
        target = (ROOT/'static'/relative).resolve()
        if not relative or not str(target).startswith(str((ROOT/'static').resolve())+'/') or not target.is_file():
            return self._json(404,{'error':'页面不存在'})
        body = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
        self._headers(200,content_type+'; charset=utf-8',len(body)); self.wfile.write(body)
    def do_POST(self):
        if not self._guard(mutation=True):
            return
        try:
            size = int(self.headers.get('Content-Length','0'))
            if size < 1 or size > 65536:
                return self._json(413,{'error':'请求体必须为1–65536字节'})
            payload = json.loads(self.rfile.read(size).decode('utf-8'))
            if self.path=='/api/runs':
                try:
                    return self._json(202,{'job':self.server.app.start_run(payload)})
                except RuntimeError as exc:
                    return self._json(409,{'error':str(exc)})
            if self.path=='/api/settings':
                app = self.server.app
                with app.lock:
                    settings = validate_settings(payload,app.settings)
                    app.store.set('settings',settings)
                    app.settings = settings
                return self._json(200,{'settings':settings})
            if self.path=='/api/codex/test':
                if not isinstance(payload,dict):
                    raise ValueError('参数必须为 JSON 对象')
                return self._json(200,{'codex':codex_status(self.server.app.settings,refresh=True)})
            self._json(404,{'error':'接口不存在'})
        except (ValueError,UnicodeDecodeError) as exc:
            self._json(400,{'error':str(exc)[:250]})
        except (BrokenPipeError,ConnectionResetError):
            pass
        except Exception:
            self._json(500,{'error':'请求未完成，请检查本机服务或数据目录'})


def main():
    parser = argparse.ArgumentParser(description='AIM Radar local-only research radar')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data')
    args = parser.parse_args()
    server = RadarServer(('127.0.0.1',args.port),RadarApp(args.data_dir))
    print('AIM Radar is ready: http://127.0.0.1:%d' % server.server_port,flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nAIM Radar stopped.',flush=True)
    finally:
        server.server_close()

if __name__=='__main__':
    main()
