"""Persistent opt-in paper/repository monitoring; first success establishes a baseline."""
import copy
import re
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

from sources import Fetcher, now_iso, normalize_arxiv_id, html_links, unwrap, validate_public_url


def repository_url(value):
    validate_public_url(value, resolve=False)
    parsed = urllib.parse.urlsplit(value)
    match = re.fullmatch(r'/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?', parsed.path)
    if parsed.hostname!='github.com' or not match or parsed.query or parsed.fragment:
        raise ValueError('代码仓库必须为 https://github.com/owner/repo')
    return 'https://github.com/'+match.group(1)+'/'+match.group(2)


def tracking_state(store):
    return dict(items=store.get('tracking:items',[]),changes=store.get('tracking:changes',[]),last_checked_at=store.get('tracking:last_checked_at'))


def work_identity(item):
    aid=item.get('arxiv_id') or (normalize_arxiv_id(item.get('url','')) if 'arxiv.org/' in item.get('url','') else '')
    return 'arxiv:'+normalize_arxiv_id(aid) if aid else item['id']


def track_item(store, item, tracked=True, repositories=None, confirmed=False):
    if type(tracked) is not bool or type(confirmed) is not bool: raise ValueError('追踪开关必须为布尔值')
    if repositories is not None and (not isinstance(repositories,list) or len(repositories)>5 or any(not isinstance(v,str) for v in repositories)): raise ValueError('最多追踪5个仓库')
    repos = list(dict.fromkeys(repository_url(url) for url in (repositories or [])))
    with store.lock:
        rows = store.get('tracking:items',[])
        if tracked and len(rows)>=50 and not any(work_identity(row['item'])==work_identity(item) for row in rows): raise ValueError('最多追踪50篇论文，请先移除部分目标')
        current = next((row for row in rows if row['id']==item['id'] or work_identity(row['item'])==work_identity(item)),None)
        if not tracked:
            store.set('tracking:items',[row for row in rows if row['id']!=(current['id'] if current else item['id'])]); return dict(id=item['id'],tracked=False)
        if current is None:
            current = dict(id=item['id'],item=copy.deepcopy(item),tracked=True,confirmed=confirmed,created_at=now_iso(),last_checked_at=None,snapshot=None,changes=[],errors=[],repositories=repos)
            rows.append(current)
        else:
            current['item']=copy.deepcopy(item)
            if repositories is not None:
                if repos!=current.get('repositories',[]) or confirmed!=current.get('confirmed',False): current['repository_baseline_reset']=True
                current['repositories']=repos
                current['confirmed']=confirmed
        store.set('tracking:items',rows)
    return current


def resource(status='unknown',url='',message='尚未取得可核验信息'):
    return dict(status=status,url=url,message=message)


def discover_links(fetch,item):
    urls = [item['url']]
    aid = item.get('arxiv_id') or (normalize_arxiv_id(item['url']) if 'arxiv.org/' in item['url'] else '')
    if aid: urls.append('https://arxiv.org/html/'+aid)
    links, errors, successful_html = [], [], False
    for url in urls[:2]:
        try:
            body = fetch.get(url,max_age=0)
            if not body.startswith(b'%PDF'):
                successful_html=True
                links.extend(html_links(body,url))
        except Exception as exc: errors.append(dict(source=url,message=str(exc)[:180]))
    return links,errors,successful_html


def github_snapshot(fetch,url):
    url = repository_url(url); path = urllib.parse.urlsplit(url).path
    api = 'https://api.github.com/repos'+path
    meta = fetch.json(api,max_age=0)
    branch = meta.get('default_branch')
    if not isinstance(branch,str) or not branch: raise ValueError('仓库默认分支不可用')
    commit = fetch.json(api+'/commits/'+urllib.parse.quote(branch,safe=''),max_age=0)
    releases = fetch.json(api+'/releases?per_page=1',max_age=0)
    entries = fetch.json(api+'/git/trees/'+urllib.parse.quote(commit['sha'],safe='')+'?recursive=1',max_age=0)
    files = [r.get('path','') for r in entries.get('tree',[]) if r.get('type')=='blob']
    implementation = any(re.search(r'\.(py|ipynb|cpp|cc|c|cu|rs|jl|m|java|ts|js)$',name,re.I) and not name.startswith(('.github/','docs/')) for name in files)
    release = releases[0] if isinstance(releases,list) and releases else {}
    result = dict(url=url,default_branch=branch,commit_sha=commit['sha'],release=release.get('tag_name'),release_url=release.get('html_url'),code=resource('available' if implementation else 'placeholder',url,'发现实现文件，未运行或核验完整性。' if implementation else '仓库暂无可识别实现文件，可能仅为占位。'),weights=resource(),data=resource())
    # Read README for explicitly linked artifacts; links establish discoverability only.
    try:
        import base64
        readme = fetch.json(api+'/readme',max_age=0)
        text = base64.b64decode(readme.get('content','')).decode('utf-8','replace')[:100000]
        for target in re.findall(r'https://[^\s<>\)\]"\']+',text):
            low = target.lower()
            parsed_target=urllib.parse.urlsplit(target)
            if parsed_target.hostname=='huggingface.co' and parsed_target.path.startswith('/datasets/'): result['data']=resource('available',target,'README 提供数据链接；未验证下载、许可或内容。')
            elif parsed_target.hostname=='huggingface.co' and not parsed_target.path.startswith(('/spaces/','/datasets/')): result['weights']=resource('available',target,'README 提供模型链接；未验证权重或许可。')
    except Exception:
        pass
    return result


def observe(fetch,row):
    item = row['item']; previous = row.get('snapshot') or {}
    snapshot = copy.deepcopy(previous)
    snapshot.setdefault('arxiv_version',None); snapshot.setdefault('openreview_status',None)
    snapshot.setdefault('repositories',[])
    for field in ('code','weights','data'): snapshot.setdefault(field,resource())
    errors = []
    aid = item.get('arxiv_id') or (normalize_arxiv_id(item['url']) if 'arxiv.org/' in item['url'] else '')
    if aid:
        try:
            body = fetch.get('https://export.arxiv.org/api/query?'+urllib.parse.urlencode({'id_list':aid}),max_age=0,arxiv=True)
            root = ET.fromstring(body); entry = root.find('{http://www.w3.org/2005/Atom}entry')
            identity = entry.findtext('{http://www.w3.org/2005/Atom}id','') if entry is not None else ''
            version = re.search(r'v(\d+)$',identity)
            if not version: raise ValueError('未取得明确 arXiv 版本')
            snapshot['arxiv_version']='v'+version.group(1)
        except Exception as exc: errors.append(dict(source='arXiv',message=str(exc)[:180]))
    parsed = urllib.parse.urlsplit(item['url'])
    if parsed.hostname=='openreview.net':
        forum = urllib.parse.parse_qs(parsed.query).get('id',[''])[0]
        try:
            notes = fetch.json('https://api2.openreview.net/notes?'+urllib.parse.urlencode({'id':forum}),max_age=0).get('notes',[])
            if not notes: raise ValueError('未取得公开投稿状态')
            content = notes[0].get('content',{})
            snapshot['openreview_status']=str(unwrap(content.get('venue'),'未知公开状态'))
        except Exception as exc: errors.append(dict(source='OpenReview',message=str(exc)[:180]))
    links,discovery_errors,discovery_success = discover_links(fetch,item); errors.extend(discovery_errors)
    repos = list(row.get('repositories',[]))
    candidates=[]
    for target,label in links:
        if urllib.parse.urlsplit(target).hostname=='github.com':
            try:
                target = repository_url(target)
                if target not in repos: candidates.append(dict(url=target,confirmed=False,evidence_source=item['url'],message='论文页面发现的候选链接，关联关系待确认。'))
            except ValueError: pass
        low = target.lower()
        parsed_target=urllib.parse.urlsplit(target)
        if parsed_target.hostname=='huggingface.co' and parsed_target.path.startswith('/datasets/'): snapshot['data']=resource('available',target,'论文页面提供数据链接；未核验内容。')
        elif parsed_target.hostname=='huggingface.co' and not parsed_target.path.startswith(('/spaces/','/datasets/')): snapshot['weights']=resource('available',target,'论文页面提供模型链接；未核验权重。')
    candidate_map={r['url']:r for r in previous.get('candidate_repositories',[]) if r['url'] not in repos}
    candidate_map.update({r['url']:r for r in candidates})
    snapshot['candidate_repositories']=list(candidate_map.values())[:10]
    snapshot['discovery_success']=discovery_success
    if discovery_success:
        snapshot['discovery_repository_urls']=sorted(set(previous.get('discovery_repository_urls',[]))|{candidate['url'] for candidate in candidates})
    else:
        snapshot.setdefault('discovery_repository_urls',[])
    snapshots = {repo['url']:repo for repo in snapshot.get('repositories',[]) if repo['url'] in repos}
    for url in repos[:5]:
        try:
            observed=github_snapshot(fetch,url)
            observed['confirmed']=bool(row.get('confirmed'))
            observed['evidence_source']='用户确认' if row.get('confirmed') else '用户提供的待确认目标'
            snapshots[url]=observed
        except Exception as exc: errors.append(dict(source=url,message=str(exc)[:180]))
    snapshot['repositories']=list(snapshots.values())
    if not repos and previous.get('repositories'):
        snapshot['code']=resource(); snapshot['weights']=resource(); snapshot['data']=resource()
    confirmed_snapshots={url:r for url,r in snapshots.items() if r.get('confirmed')}
    if confirmed_snapshots:
        snapshots_values = list(confirmed_snapshots.values())
        snapshot['code']=next((r['code'] for r in snapshots_values if r['code']['status']=='available'),snapshots_values[0]['code'])
        for field in ('weights','data'):
            artifact = next((r[field] for r in snapshots_values if r[field]['status']=='available'),None)
            if artifact: snapshot[field]=artifact
    elif not discovery_errors and not repos and not candidates:

        snapshot['code']=resource('not_found','','已检查论文页面，尚未发现代码仓库链接；不代表未开源。')
    elif not confirmed_snapshots and (repos or candidates):
        snapshot['code']=resource('unknown','','发现待确认仓库或仓库检查失败，不能据此认定开源状态。')
    return snapshot,errors


def check_tracked(store,fetch=None):
    fetch = fetch or Fetcher(store,timeout=10,deadline=time.monotonic()+180)
    with store.lock: rows = copy.deepcopy(store.get('tracking:items',[]))
    checked = now_iso(); all_changes=[]; updates={}
    for row in rows:
        old = row.get('snapshot')
        snapshot,errors = observe(fetch,row)
        reset_repositories=row.get('repository_baseline_reset',False)
        if old is not None and not reset_repositories:
            for field in ('code','weights','data'):
                before,after=old.get(field),snapshot.get(field)
                if isinstance(before,dict) and before.get('status')!='unknown' and isinstance(after,dict) and after.get('status')=='unknown': snapshot[field]=copy.deepcopy(before)
        changes=[]
        if old is not None:
            for field in ('arxiv_version','openreview_status','code','weights','data'):
                if reset_repositories and field in ('code','weights','data'): continue
                before,after = old.get(field),snapshot.get(field)
                # An unknown field's first successful observation is its baseline.
                known = before is not None and (not isinstance(before,dict) or before.get('status')!='unknown')
                meaningful_before=(before.get('status'),before.get('url')) if isinstance(before,dict) else before
                meaningful_after=(after.get('status'),after.get('url')) if isinstance(after,dict) else after
                if known and meaningful_before!=meaningful_after:
                    changes.append(dict(id=uuid.uuid4().hex[:16],item_id=row['id'],at=checked,field=field,before=before,after=after,message='检测到 '+field+' 变化'))
            if not reset_repositories and old.get('discovery_success') and snapshot.get('discovery_success'):
                before_candidates=set(old.get('discovery_repository_urls',[]))
                for url in sorted(set(snapshot.get('discovery_repository_urls',[]))-before_candidates):
                    changes.append(dict(id=uuid.uuid4().hex[:16],item_id=row['id'],at=checked,field='candidate_repository',before=None,after=url,message='新发现候选仓库链接，请确认是否属于本论文'))
            old_repos = {r['url']:r for r in old.get('repositories',[])}
            for repo in ([] if reset_repositories else snapshot.get('repositories',[])):
                previous = old_repos.get(repo['url'])
                if previous:
                    for field in ('commit_sha','release'):
                        if previous.get(field)!=repo.get(field): changes.append(dict(id=uuid.uuid4().hex[:16],item_id=row['id'],at=checked,field='repository.'+field,before=previous.get(field),after=repo.get(field),message=repo['url']+(' 仓库有新提交' if field=='commit_sha' else ' 仓库有新发布')))
                elif repo.get('confirmed') and old.get('code',{}).get('status')=='not_found':
                    changes.append(dict(id=uuid.uuid4().hex[:16],item_id=row['id'],at=checked,field='repository',before=None,after=repo['url'],message='新发现代码仓库'))
        row.update(repository_baseline_reset=False,snapshot=snapshot,last_checked_at=checked,errors=errors,changes=(changes+row.get('changes',[]))[:100]); updates[row['id']]=row; all_changes.extend(changes)
    with store.lock:
        # Preserve additions/removals made while network checks were running.
        current = store.get('tracking:items',[])
        for row in current:
            if row['id'] in updates:
                result=updates[row['id']]
                if row.get('repositories',[])!=result.get('repositories',[]) or row.get('confirmed')!=result.get('confirmed'): continue
                for key in ('snapshot','last_checked_at','errors','changes','repository_baseline_reset'): row[key]=result[key]
        store.set('tracking:items',current)
        store.set('tracking:changes',(all_changes+store.get('tracking:changes',[]))[:500])
        store.set('tracking:last_checked_at',checked)
    return tracking_state(store)
