"""Public-source collectors. Everything fetched is untrusted text, never instructions.

Dates are explicit: HF feature-day is separate from paper publication; undated
proceedings indexes establish a baseline before later first-observed events.
"""
import datetime as dt
import email.utils
import hashlib
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser

UTC = dt.timezone.utc
SHANGHAI = dt.timezone(dt.timedelta(hours=8))
ARXIV_LOCK = threading.Lock()
ARXIV_LAST = 0.0
MAX_RESPONSE = 8 * 1024 * 1024


def now_iso():
    return dt.datetime.now(UTC).isoformat()


def clean_text(value, limit=20000):
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', ' ', str(value or '')))).strip()[:limit]


def parse_date(value):
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(value / (1000 if value > 10**11 else 1), UTC)
        except (ValueError, OverflowError, OSError):
            return None
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        human = str(value).strip().replace('Sept ', 'Sep ')
        for format in ('%b %d, %Y','%B %d, %Y','%d %b %Y','%Y年%m月%d日'):
            try:
                return dt.datetime.strptime(human,format).replace(tzinfo=UTC)
            except ValueError:
                pass
        try:
            parsed = email.utils.parsedate_to_datetime(str(value))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except (ValueError, TypeError, OverflowError):
            return None


def in_window(value, start, end):
    date = parse_date(value)
    return bool(date and start <= date.astimezone(SHANGHAI).date().isoformat() <= end)


def utc_bounds(start, end):
    first = dt.datetime.combine(dt.date.fromisoformat(start), dt.time.min, SHANGHAI).astimezone(UTC)
    last = dt.datetime.combine(dt.date.fromisoformat(end), dt.time.max, SHANGHAI).astimezone(UTC)
    return first.strftime('%Y%m%d%H%M'), last.strftime('%Y%m%d%H%M')


def validate_public_url(url, resolve=True):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Source URL must be a public HTTPS URL without credentials or custom port')
    host = parsed.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('Local/private source addresses are not allowed')
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve:
            return url
        try:
            addresses = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
        except socket.gaierror as exc:
            raise ValueError('Source hostname could not be resolved') from exc
    if not addresses or any(not addr.is_global for addr in addresses):
        raise ValueError('Local/private source addresses are not allowed')
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(self, store=None, timeout=14, deadline=None):
        self.store, self.timeout, self.deadline = store, timeout, deadline
        self.opener = urllib.request.build_opener(SafeRedirect())
    def get(self, url, max_age=3600, arxiv=False):
        if self.deadline and time.monotonic() > self.deadline:
            raise TimeoutError('Collection time budget reached; remaining sources not fetched')
        validate_public_url(url)
        cached = self.store.cache_get(url, max_age) if self.store else None
        if cached is not None:
            return cached
        global ARXIV_LAST
        if arxiv:
            with ARXIV_LOCK:
                delay = max(0, 3.1 - (time.monotonic() - ARXIV_LAST))
                if delay:
                    time.sleep(delay)
                ARXIV_LAST = time.monotonic()
        req = urllib.request.Request(url, headers={'User-Agent':'AIM-Radar/1.0 (local academic research reader)', 'Accept':'application/json,application/atom+xml,application/rss+xml,text/html;q=0.9,*/*;q=0.5'})
        for attempt in range(2):
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    body = response.read(MAX_RESPONSE + 1)
                    if len(body) > MAX_RESPONSE:
                        raise ValueError('Source response exceeded 8 MiB limit')
                if self.store:
                    self.store.cache_set(url, body)
                return body
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt:
                    raise
                # Rate-limited sources are retried once with a bounded delay.
                time.sleep(3 if arxiv else 1)
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt:
                    raise
                time.sleep(0.5)
    def json(self, url, **kwargs):
        body = self.get(url, **kwargs)
        try:
            return json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            if self.store:
                self.store.cache_delete(url)
            if b'Verifying your browser' in body or b'challenge' in body[:6000].lower():
                raise RuntimeError('公开 API 返回浏览器验证页面，当前自动采集被阻止；请打开官方来源人工查看。') from exc
            raise RuntimeError('公开 API 未返回有效 JSON；可能暂时不可用。') from exc


def normalize_arxiv_id(value):
    text = str(value or '').strip().split('?')[0].rstrip('/')
    text = re.sub(r'^https?://(?:www\.)?(?:arxiv\.org|export\.arxiv\.org)/(?:abs|pdf)/', '', text)
    text = re.sub(r'\.pdf$', '', text)
    return re.sub(r'v\d+$', '', text)


def normalize_title(title):
    return re.sub(r'[^\w]+', '', unescape(title).casefold())


def make_item(title, url, abstract='', source='', published_at=None, date_basis='original_publication', kind='paper', **kwargs):
    item = dict(id=hashlib.sha256((kind + normalize_title(title)).encode()).hexdigest()[:20], title=clean_text(title, 1000), url=url, abstract=clean_text(abstract), summary='', why_it_matters='', directions=[], score=0, source=source, sources=[dict(name=source, url=url, date_basis=date_basis, published_at=published_at)], published_at=published_at, discovered_at=now_iso(), date_basis=date_basis, kind=kind, venue='', status='公开论文' if kind == 'paper' else '官方公告', related_work=[], suggested_action='', authors=[], research_score=0, importance_score=0)
    item.update(kwargs)
    return item


def deduplicate(items):
    """Merge same arXiv work or normalized title, retaining all source provenance."""
    result, by_arxiv, by_title = [], {}, {}
    for item in items:
        arxiv_id = item.get('arxiv_id') or (normalize_arxiv_id(item['url']) if 'arxiv.org/' in item.get('url', '') else '')
        title = normalize_title(item['title'])
        existing = by_arxiv.get(arxiv_id) if arxiv_id else None
        existing = existing or by_title.get(title)
        if existing:
            for source in item.get('sources', []):
                if source not in existing['sources']:
                    existing['sources'].append(source)
            for field in ('abstract', 'authors', 'venue'):
                if not existing.get(field) and item.get(field):
                    existing[field] = item[field]
            # Prefer an original publication date when duplicate is also featured.
            if existing['date_basis'] == 'hf_featured_day' and item['date_basis'] != 'hf_featured_day':
                existing['date_basis'], existing['published_at'] = item['date_basis'], item['published_at']
            existing['directions'] = list(dict.fromkeys(existing.get('directions', []) + item.get('directions', [])))
        else:
            existing = item
            result.append(existing)
        by_title[title] = existing
        if arxiv_id:
            by_arxiv[arxiv_id] = existing
    return result


def parse_arxiv(body, start, end):
    root = ET.fromstring(body)
    ns = {'a':'http://www.w3.org/2005/Atom'}
    items = []
    for entry in root.findall('a:entry', ns):
        val = lambda name: entry.findtext('a:' + name, '', ns)
        url = val('id').replace('http://', 'https://')
        if not re.search(r'arxiv\.org/abs/', url):
            continue
        published, updated = val('published'), val('updated')
        basis = 'original_publication' if in_window(published, start, end) else 'updated_this_week'
        selected = published if basis == 'original_publication' else updated
        if not in_window(selected, start, end):
            continue
        item = make_item(val('title'), url, val('summary'), 'arXiv', selected, basis, authors=[clean_text(a.findtext('a:name', '', ns)) for a in entry.findall('a:author', ns)], arxiv_id=normalize_arxiv_id(url), original_published_at=published, updated_at=updated, status='预印本 · 未经同行评审')
        items.append(item)
    return items


ARXIV_QUERIES = [
    '(all:"remote sensing" OR all:"earth observation" OR all:geospatial) AND (all:multimodal OR all:"foundation model" OR all:"vision language")',
    '(all:UAV OR all:drone OR all:"aerial navigation") AND (all:multimodal OR all:"world model" OR all:"vision language")',
    '(all:"few-shot" OR all:"test-time adaptation") AND (all:"vision language" OR all:multimodal OR all:"foundation model")',
    '(all:hallucination OR all:faithfulness) AND (all:multimodal OR all:"vision language")',
    '(all:"noisy label" OR all:"noisy correspondence" OR all:"label noise") AND (all:multimodal OR all:"cross-modal" OR all:"remote sensing")'
]


def collect_arxiv(fetch, start, end):
    lo, hi = utc_bounds(start, end)
    items, warnings = [], []
    for index, query in enumerate(ARXIV_QUERIES):
        count = 0
        try:
            for offset in (0, 50):
                params = {'search_query':'(' + query + ') AND (cat:cs.CV OR cat:cs.AI OR cat:cs.LG OR cat:cs.CL OR cat:eess.IV) AND (submittedDate:[' + lo + ' TO ' + hi + '] OR lastUpdatedDate:[' + lo + ' TO ' + hi + '])', 'start':offset, 'max_results':50, 'sortBy':'lastUpdatedDate', 'sortOrder':'descending'}
                body = fetch.get('https://export.arxiv.org/api/query?' + urllib.parse.urlencode(params), max_age=86400, arxiv=True)
                parsed = parse_arxiv(body, start, end)
                items.extend(parsed)
                root = ET.fromstring(body)
                raw_count = len(root.findall('{http://www.w3.org/2005/Atom}entry'))
                count += raw_count
                if raw_count < 50:
                    break
            if count >= 100:
                warnings.append('arXiv 第%d组查询达到100篇上限，候选可能不完整。' % (index + 1))
        except Exception as exc:
            warnings.append('arXiv 第%d组查询失败：%s' % (index + 1, str(exc)[:180]))
    return items, warnings


def parse_hf(data, day):
    items = []
    if isinstance(data, dict):
        data = data.get('dailyPapers', data.get('papers', []))
    for entry in data if isinstance(data, list) else []:
        paper = entry.get('paper', entry)
        aid = normalize_arxiv_id(paper.get('id', ''))
        title = paper.get('title')
        if not aid or not title:
            continue
        items.append(make_item(title, 'https://arxiv.org/abs/' + aid, paper.get('summary', ''), 'Hugging Face Daily Papers', day, 'hf_featured_day', authors=[a.get('name', '') if isinstance(a, dict) else str(a) for a in paper.get('authors', [])], arxiv_id=aid, original_published_at=paper.get('publishedAt'), featured_day=day, featured_day_timezone='Hugging Face site calendar (unspecified)', status='社区推荐预印本 · 推荐日期不等于发表日期'))
    return items


def collect_hf(fetch, start, end):
    items, warnings = [], []
    day = dt.date.fromisoformat(start)
    while day <= dt.date.fromisoformat(end):
        try:
            for page in range(2):
                url = 'https://huggingface.co/api/daily_papers?' + urllib.parse.urlencode({'date':day.isoformat(), 'limit':100, 'p':page})
                data = fetch.json(url, max_age=21600)
                parsed = parse_hf(data, day.isoformat())
                items.extend(parsed)
                if len(parsed) < 100:
                    break
                if page == 1:
                    warnings.append('HF %s 达到200篇上限。' % day)
        except Exception as exc:
            warnings.append('HF %s：%s' % (day, str(exc)[:180]))
        day += dt.timedelta(days=1)
    return items, warnings


def unwrap(value, default=''):
    if isinstance(value, dict):
        return value.get('value', default)
    return value if value is not None else default


def parse_openreview(notes, start, end, venue, first_seen=None):
    items = []
    for note in notes:
        if note.get('replyto') or (note.get('forum') and note.get('forum') != note.get('id')):
            continue
        content = note.get('content', {})
        online = parse_date(note.get('odate'))
        accepted = parse_date(note.get('pdate'))
        updated = parse_date(note.get('mdate') or note.get('tmdate'))
        venue_label = str(unwrap(content.get('venue'), ''))
        venueid = str(unwrap(content.get('venueid'), ''))
        accepted_status = bool(venue_label and not any(word in (venue_label + ' ' + venueid).lower() for word in ('submitted', 'submission', 'withdrawn', 'rejected', 'under review')) and (venueid or any(word in venue_label.lower() for word in ('accepted', 'poster', 'oral', 'spotlight'))))
        if online and in_window(online.isoformat(), start, end):
            basis, selected = 'public_online_this_week', online
        elif accepted and accepted_status and in_window(accepted.isoformat(), start, end):
            basis, selected = 'accepted_this_week', accepted
        elif updated and in_window(updated.isoformat(), start, end):
            basis, selected = 'updated_this_week', updated
        elif first_seen and in_window(first_seen.get(note.get('id')), start, end):
            basis, selected = 'first_observed', parse_date(first_seen[note['id']])
        else:
            continue
        title = unwrap(content.get('title'))
        if not title or not note.get('id'):
            continue
        status = venue_label if accepted_status else '公开投稿 · 未核验录用/同行评审状态'
        item = make_item(title, 'https://openreview.net/forum?id=' + urllib.parse.quote(note.get('forum') or note['id']), unwrap(content.get('abstract')), 'OpenReview', selected.isoformat(), basis, venue=venue, status=status, authors=unwrap(content.get('authors'), []), online_at=online.isoformat() if online else None, acceptance_marked_at=accepted.isoformat() if accepted else None, original_published_at=online.isoformat() if online else None, submission_created_at=note.get('cdate'), updated_at=updated.isoformat() if updated else None)
        if not online:
            item['date_limitation'] = 'OpenReview 未提供 odate，无法核验首次公开日期；创建日期不作为公开日期。'
        items.append(item)
    return items


def collect_openreview(fetch, start, end, venues):
    items, warnings = [], []
    year = dt.date.fromisoformat(end).year
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def one(venue, current):
        found, notes_warnings = [], []
        group = '%s.cc/%d/Conference' % (venue, current)
        try:
            response = fetch.json('https://api2.openreview.net/groups?' + urllib.parse.urlencode({'id':group}), max_age=21600)
            groups = response.get('groups', [])
            if not groups:
                return [], ['%s %d：公开组尚未发现。' % (venue, current)]
            content = groups[0].get('content', {})
            invitation = unwrap(content.get('submission_id'))
            if not invitation:
                invitation = group + '/-/' + str(unwrap(content.get('submission_name'), 'Submission'))
            count, total = 0, 0
            seen_key = 'openreview_seen:' + group
            seen = fetch.store.get(seen_key, {}) if getattr(fetch, 'store', None) else {}
            for offset in range(0, 2000, 200):
                query = {'invitation':invitation, 'limit':200, 'offset':offset, 'sort':'odate:desc'}
                data = fetch.json('https://api2.openreview.net/notes?' + urllib.parse.urlencode(query), max_age=21600)
                notes = data.get('notes', [])
                total = data.get('count', 0)
                count += len(notes)
                for note in notes:
                    if note.get('id') and not note.get('odate'):
                        seen.setdefault(note['id'], now_iso())
                found.extend(parse_openreview(notes, start, end, '%s %d' % (venue, current), seen))
                if len(notes) < 200:
                    break
                # odate sorted pages older than window cannot contain new public dates,
                # but can still contain later updates. Keep pagination to bound honestly.
            if getattr(fetch, 'store', None):
                fetch.store.set(seen_key, seen)
            if count >= 2000 or total > count:
                notes_warnings.append('%s %d：按首次公开日期检查%d/%s篇，达到候选上限；旧投稿本周更新可能遗漏。' % (venue, current, count, total or '未知'))
        except Exception as exc:
            notes_warnings.append('%s %d：%s' % (venue, current, str(exc)[:160]))
        return found, notes_warnings
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in as_completed([pool.submit(one, venue, current) for venue in venues for current in (year, year + 1)]):
            found, notes_warnings = future.result()
            items.extend(found)
            warnings.extend(notes_warnings)
    return items, warnings


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.current, self.text = [], None, []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.current, self.text = attrs['href'], []
    def handle_data(self, data):
        if self.current is not None:
            self.text.append(data)
    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.links.append((self.current, clean_text(' '.join(self.text))))
            self.current = None


def html_links(body, base):
    parser = Links()
    parser.feed(body.decode('utf-8', 'replace') if isinstance(body, bytes) else body)
    return [(urllib.parse.urljoin(base, href), text) for href, text in parser.links]


PROCEEDINGS = [
    ('cvf', 'CVF Open Access', 'https://openaccess.thecvf.com/', r'https://openaccess\.thecvf\.com/(?:CVPR|ICCV|WACV)\d{4}/?(?:\?day=all)?$'),
    ('pmlr', 'Proceedings of Machine Learning Research', 'https://proceedings.mlr.press/', r'https://proceedings\.mlr\.press/v\d+/?$'),
    ('acl', 'ACL Anthology', 'https://aclanthology.org/', r'https://aclanthology\.org/(?:events/[a-z0-9-]+-\d{4}/|volumes/\d{4}\.[^/]+/)$')
]


def proceedings_watch(fetch, store, start, end, directions):
    conferences, items, warnings = [], [], []
    for source_id, name, url, pattern in PROCEEDINGS:
        try:
            links = html_links(fetch.get(url, max_age=21600), url)
            volumes = dict((urllib.parse.urlunsplit(urllib.parse.urlsplit(link)._replace(query='',fragment='')).rstrip('/') + '/', title or link) for link, title in links if re.search(pattern, link))
            old = store.get('snapshot:' + source_id)
            baseline = old is None
            new = [] if baseline else [link for link in volumes if link not in old.get('known', {})]
            known = dict(old.get('known', {})) if old else {}
            observed = now_iso()
            for link, title in volumes.items():
                known.setdefault(link, {'title':title, 'first_seen':observed})
            store.set('snapshot:' + source_id, {'known':known, 'checked_at':observed})
            # Keep repeat reports reproducible: previously detected volumes within selected window remain eligible.
            eligible = [link for link, info in known.items() if not baseline and in_window(info['first_seen'], start, end) and (link in new or info.get('after_baseline'))]
            for link in new:
                known[link]['after_baseline'] = True
            store.set('snapshot:' + source_id, {'known':known, 'checked_at':observed})
            for link in eligible[:3]:
                volume_items = harvest_volume(fetch, source_id, link, known[link]['title'], known[link]['first_seen'], directions)
                items.extend(volume_items)
            if len(eligible) > 3:
                warnings.append(name + ' 新发现卷超过3个，仅采集前3卷。')
            message = '首次建立目录基线；索引未给出可靠发布日期，无法据此确认本周发布。' if baseline else ('%d 个卷在本窗口首次被本机观察；不代表官方发布日期。' % len(eligible))
            conferences.append(dict(id=source_id, name=name, url=url, status='baseline' if baseline else 'ok', message=message, new_volumes=[dict(url=link, title=known[link]['title'], first_seen=known[link]['first_seen'], date_basis='first_observed') for link in eligible], baseline=baseline))
        except Exception as exc:
            warnings.append(name + '：' + str(exc)[:180])
            conferences.append(dict(id=source_id, name=name, url=url, status='error', message=str(exc)[:200], new_volumes=[], baseline=False))
    return items, conferences, warnings


def harvest_volume(fetch, source_id, url, venue, observed, directions):
    page_url = url + '?day=all' if source_id == 'cvf' else url
    links = html_links(fetch.get(page_url, max_age=86400), page_url)
    if source_id == 'acl' and '/events/' in url:
        sub = [link for link, _ in links if '/volumes/' in link]
        links = [pair for volume in list(dict.fromkeys(sub))[:2] for pair in html_links(fetch.get(volume, max_age=86400), volume)]
    candidates = []
    for link, title in links:
        is_paper = (source_id == 'cvf' and '/html/' in link) or (source_id == 'pmlr' and re.search(r'/v\d+/[^/]+\.html$', link)) or (source_id == 'acl' and re.search(r'/\d{4}\.[a-z0-9-]+\.\d+/?$', link))
        if is_paper and len(title) > 20:
            item = make_item(title, link, '', '会议论文集', observed, 'first_observed', venue=venue, status='官方论文集 · 本机首次观察日期')
            score_item(item, directions)
            if item['research_score'] >= 20:
                candidates.append(item)
    for item in deduplicate(candidates)[:12]:
        try:
            body = fetch.get(item['url'], max_age=86400).decode('utf-8', 'replace')
            abstract = re.search(r'(?:<div[^>]+id=["\']abstract["\'][^>]*>|<div[^>]+class=["\'][^"\']*abstract[^"\']*["\'][^>]*>|<p[^>]+class=["\']abstract["\'][^>]*>)(.*?)</(?:div|p)>', body, re.I | re.S)
            if abstract:
                item['abstract'] = clean_text(abstract.group(1))
        except Exception:
            pass
    return deduplicate(candidates)[:12]


NEWS_FEEDS = [
    ('openai','OpenAI','https://openai.com/news/rss.xml'),
    ('anthropic','Anthropic','https://www.anthropic.com/news'),
    ('deepmind','Google DeepMind','https://deepmind.google/blog/rss.xml'),
    ('hf_news','Hugging Face','https://huggingface.co/blog/feed.xml'),
    ('microsoft','Microsoft Research','https://www.microsoft.com/en-us/research/feed/'),
    ('nvidia','NVIDIA','https://blogs.nvidia.com/feed/'),
    ('meta','Meta AI','https://ai.meta.com/blog/'),
    ('qwen','Qwen','https://qwenlm.github.io/blog/'),
    ('deepseek','DeepSeek','https://api.github.com/repos/deepseek-ai/DeepSeek-V3/releases'),
    ('baai','北京智源 BAAI','https://www.baai.ac.cn/'),
    ('kimi','Moonshot / Kimi','https://www.kimi.ai/blog/')
]


def parse_feed(body, name, start, end, base=''):
    root = ET.fromstring(body)
    if root.tag.rsplit('}', 1)[-1].lower() not in ('rss', 'feed', 'rdf'):
        raise ET.ParseError('Not an RSS/Atom feed')
    items = []
    entries = root.findall('.//item')
    atom = '{http://www.w3.org/2005/Atom}'
    entries += root.findall(atom + 'entry')
    for entry in entries:
        def text(names):
            for tag in names:
                element = entry.find(tag)
                if element is not None:
                    return ''.join(element.itertext())
            return ''
        title = text(['title', atom + 'title'])
        date = text(['pubDate', atom + 'published', atom + 'updated', '{http://purl.org/dc/elements/1.1/}date'])
        if not in_window(date, start, end):
            continue
        link = text(['link'])
        if not link:
            for element in entry.findall(atom + 'link'):
                if element.get('rel', 'alternate') == 'alternate':
                    link = element.get('href', '')
                    break
        link = urllib.parse.urljoin(base, link)
        if not link.startswith('https://') or not title:
            continue
        abstract = text(['description', '{http://purl.org/rss/1.0/modules/content/}encoded', atom + 'summary', atom + 'content'])
        items.append(make_item(title, link, abstract, name, parse_date(date).isoformat(), 'official_publication', kind='news'))
    return items


def parse_news_html(body, name, url, start, end):
    """Read structured NewsArticle JSON-LD or dated article cards, never infer dates."""
    text = body.decode('utf-8', 'replace') if isinstance(body, bytes) else body
    items = []
    def scan(data):
        if isinstance(data, list):
            for value in data:
                scan(value)
        elif isinstance(data, dict):
            if '@graph' in data:
                scan(data['@graph'])
            date = data.get('datePublished')
            title = data.get('headline') or data.get('name')
            link = data.get('url') or data.get('mainEntityOfPage')
            if isinstance(link, dict):
                link = link.get('@id')
            if date and title and isinstance(link, str) and in_window(date, start, end):
                items.append(make_item(title, urllib.parse.urljoin(url, link), data.get('description', ''), name, parse_date(date).isoformat(), 'official_publication', kind='news'))
            for key in ('itemListElement', 'item', 'blogPost'):
                if key in data:
                    scan(data[key])
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.I | re.S):
        try:
            scan(json.loads(block))
        except (ValueError, TypeError):
            pass
    # Associate a date only with its own bounded card, never a page-wide date.
    cards = re.findall(r'<a\b[^>]*>.*?</a>', text, re.I | re.S)
    cards += [block for block in re.findall(r'<article\b[^>]*>.*?</article>', text, re.I | re.S) if len(html_links(block,url)) <= 3]
    for block in cards:
        if len(block) > 16000:
            continue
        raw_date = None
        machine = re.search(r'datetime=["\']([^"\']+)', block, re.I)
        times = re.findall(r'<time\b[^>]*>(.*?)</time>',block,re.I|re.S)
        if machine:
            raw_date = machine.group(1)
        elif times:
            raw_date = clean_text(times[0])
        else:
            explicit = re.search(r'\b(\d{4}-\d{2}-\d{2}|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+\d{4})\b',clean_text(block),re.I)
            if explicit:
                raw_date = explicit.group(1)
        if not raw_date or not in_window(raw_date,start,end):
            continue
        links = html_links(block,url)
        headings = re.findall(r'<h[1-4][^>]*>(.*?)</h[1-4]>',block,re.I|re.S)
        if links:
            link,title = links[0]
            title = clean_text(headings[0]) if headings else clean_text(title.replace(raw_date,''))
            if title and link.startswith('https://'):
                items.append(make_item(title,link,'',name,parse_date(raw_date).isoformat(),'official_publication',kind='news'))
    return deduplicate(items)


def collect_news(fetch, start, end, extra_feeds=None):
    items, statuses = [], []
    feeds = NEWS_FEEDS + [('custom_%d' % i, '自定义官方订阅', url) for i, url in enumerate(extra_feeds or [])]
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def one(entry):
        source_id, name, url = entry
        try:
            body = fetch.get(url, max_age=3600)
            if url.startswith('https://api.github.com/'):
                records = json.loads(body)
                parsed = [make_item(row['name'] or row['tag_name'], row['html_url'], row.get('body', ''), name, row.get('published_at'), 'official_publication', kind='news') for row in records if in_window(row.get('published_at'), start, end)]
                mode = '公开 GitHub release；覆盖有限'
            else:
                try:
                    parsed = parse_feed(body, name, start, end, url)
                    mode = '官方订阅；按明确发布日期筛选'
                except ET.ParseError:
                    parsed = parse_news_html(body, name, url, start, end)
                    mode = '仅解析结构化日期；页面可能有未识别动态' if parsed else '未发现可解析的本周日期；动态页面或覆盖不足，不能据此推断无新闻'
            return parsed, dict(id=source_id, name=name, url=url, status='ok' if ('订阅' in mode or parsed) else 'partial', count=len(parsed), message=mode)
        except Exception as exc:
            return [], dict(id=source_id, name=name, url=url, status='error', count=0, message=str(exc)[:220])
    with ThreadPoolExecutor(max_workers=5) as pool:
        for future in as_completed([pool.submit(one, entry) for entry in feeds]):
            parsed, status = future.result()
            items.extend(parsed)
            statuses.append(status)
    return deduplicate(items), statuses


def score_item(item, directions):
    text = (item.get('title', '') + ' ' + item.get('abstract', '')).lower()
    matches = []
    for direction in directions:
        hits = [keyword for keyword in direction['keywords'] if keyword.lower() in text]
        if hits:
            matches.append((direction['id'], min(95, 30 + 16 * len(hits)), hits))
    item['directions'] = [row[0] for row in sorted(matches, key=lambda row:row[1], reverse=True)]
    item['research_score'] = max([row[1] for row in matches] or [0])
    important = ['release', 'launch', 'introduc', 'open source', 'open-source', 'open weight', 'model', 'benchmark', 'gpu', 'inference', 'training', 'agent', '发布', '开源', '模型', '芯片', '推理']
    item['importance_score'] = min(95, 25 + sum(8 for word in important if word in text)) if item['kind'] == 'news' else 0
    item['score'] = max(item['research_score'], item['importance_score'])
    item['summary'] = ('基于官方公告：' if item['kind'] == 'news' else '基于公开摘要：') + (item.get('abstract', '')[:420] or '当前来源未提供摘要；请打开原文核验。')
    if matches:
        direction = next(d for d in directions if d['id'] == matches[0][0])
        item['why_it_matters'] = '关键词匹配「%s」：%s。建议对照团队工作 %s 核验实际方法和适用场景。' % (direction['name'], '、'.join(matches[0][2][:3]), '、'.join(direction['anchors'][:2]))
        item['related_work'] = direction['anchors'][:2]
        item['suggested_action'] = '阅读方法与实验设置；选取团队现有数据和基线复现核心增益，检查跨域、少样本或噪声条件下是否成立。'
    else:
        item['why_it_matters'] = '产业重要性规则初筛；目前未检出与五个研究方向的明确关键词关联。' if item['kind'] == 'news' else '方向关联较弱，等待人工核验。'
        item['suggested_action'] = '核验发布内容、开放权重/接口、许可和评测结果，再决定是否试用。'
    return item
