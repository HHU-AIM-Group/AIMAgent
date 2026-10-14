"""Bounded live collection and persistent report assembly."""
import datetime as dt
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import sources
from codex_bridge import enrich, select_shortlist

DEFAULT_SETTINGS = {'model':'gpt-6.1-sol','codex_path':'','sources':{'huggingface':True,'arxiv':True,'openreview':True,'proceedings':True,'news':True},'openreview_venues':['ICLR','ICML','NeurIPS'],'extra_feeds':[]}


def default_window():
    today = dt.datetime.now(sources.SHANGHAI).date()
    return (today - dt.timedelta(days=today.weekday())).isoformat(), today.isoformat()


def validate_window(start, end):
    try:
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except (ValueError, TypeError):
        raise ValueError('日期必须使用 YYYY-MM-DD 格式')
    if str(first) != start or str(last) != end or first > last:
        raise ValueError('日期范围无效，开始日期应不晚于结束日期')
    if (last - first).days > 13:
        raise ValueError('单次最多查询14天，请缩小日期范围')
    if last > dt.datetime.now(sources.SHANGHAI).date():
        raise ValueError('结束日期不能晚于今天（Asia/Shanghai）')
    return start, end


def collect_report(store, profile, settings, start, end, use_codex=True, progress=None, data_dir=None):
    progress = progress or (lambda *args: None)
    validate_window(start, end)
    fetch = sources.Fetcher(store, deadline=time.monotonic() + 240)
    papers, news, conferences, statuses, warnings = [], [], [], [], []
    enabled = settings['sources']
    collectors = {
        'huggingface':('Hugging Face Daily Papers','https://huggingface.co/papers',lambda:sources.collect_hf(fetch,start,end)),
        'arxiv':('arXiv','https://export.arxiv.org/api/query',lambda:sources.collect_arxiv(fetch,start,end)),
        'openreview':('OpenReview','https://openreview.net/',lambda:sources.collect_openreview(fetch,start,end,settings['openreview_venues'])),
        'proceedings':('官方会议论文集','https://openaccess.thecvf.com/',lambda:sources.proceedings_watch(fetch,store,start,end,profile['directions'])),
        'news':('产业一手动态','https://openai.com/news/',lambda:sources.collect_news(fetch,start,end,settings.get('extra_feeds',[])))
    }
    active = [key for key in collectors if enabled.get(key)]
    progress(6,'collecting','正在并行采集公开论文、会议目录与官方动态；单次采集预算约4分钟。')
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(collectors[key][2]):key for key in active}
        finished = 0
        for future in as_completed(futures):
            key = futures[future]
            name, url, _ = collectors[key]
            try:
                result = future.result()
                if key == 'news':
                    found, source_statuses = result
                    news.extend(found)
                    statuses.extend(source_statuses)
                elif key == 'proceedings':
                    found, conf, source_warnings = result
                    papers.extend(found); conferences.extend(conf); warnings.extend(source_warnings)
                    statuses.append(dict(id=key,name=name,url=url,status='partial' if source_warnings or any(c.get('baseline') for c in conf) else 'ok',count=len(found),message='；'.join(source_warnings[:3]) or '官方目录首次观察追踪；首次运行仅建立基线。'))
                else:
                    found, source_warnings = result
                    papers.extend(found); warnings.extend(source_warnings)
                    statuses.append(dict(id=key,name=name,url=url,status=('partial' if found else 'error') if source_warnings else 'ok',count=len(found),message='；'.join(source_warnings[:4]) or '已按公开日期/更新日期筛选；候选有明确时间依据。'))
            except Exception as exc:
                statuses.append(dict(id=key,name=name,url=url,status='error',count=0,message=str(exc)[:200]))
                warnings.append(name + '：' + str(exc)[:200])
            finished += 1
            progress(8 + int(60 * finished / max(1,len(active))), 'collecting', '%s 已完成（%d/%d组）；正在整理保留来源证据。' % (name,finished,len(active)))
    for key in collectors:
        if key not in active:
            statuses.append(dict(id=key,name=collectors[key][0],url=collectors[key][1],status='disabled',count=0,message='用户已停用此来源'))
    papers = sources.deduplicate(papers)
    news = sources.deduplicate(news)
    for item in papers + news:
        sources.score_item(item,profile['directions'])
    candidate_count = len(papers)
    papers = sorted([item for item in papers if item['research_score'] > 0],key=lambda item:item['research_score'],reverse=True)
    # Diversity selection protects narrow directions even when output must be capped.
    if len(papers) > 250:
        papers = select_shortlist(papers,[],profile['directions'],paper_limit=250,news_limit=0,total_limit=250)
        warnings.append('匹配论文超过250篇，报告保留各方向优先候选；原始采集%d篇。' % candidate_count)
    if len(news) > 80:
        warnings.append('本周官方动态超过80条，仅保留重要性初筛前80条。')
    news = sorted(news,key=lambda item:item['importance_score'],reverse=True)[:80]
    warnings.extend('%s：%s' % (status['name'],status['message']) for status in statuses if status['status'] in ('error','partial') and '%s：%s' % (status['name'],status['message']) not in warnings)
    if not active:
        warnings.append('所有来源均已停用，没有执行采集。')
    warnings.append('自动摘要基于公开摘要/官方公告，不代表已阅读全文或复现实验；优先级分数不评判正确性或创新性。')
    mode = 'heuristic'
    summary = '本次从公开来源采集%d篇论文候选，按团队五个方向筛出%d篇，并保留%d条官方产业动态。当前为规则初筛；请结合原文核验。' % (candidate_count,len(papers),len(news))
    if not papers and not news:
        summary = '当前窗口未取得可展示候选。来源可能失败、无可核验日期或方向匹配不足；请检查覆盖状态，不能据此认定本周没有进展。'
    progress(72,'ranking','已完成去重、时间依据检查与五个方向匹配。')
    if use_codex and (papers or news):
        try:
            shortlist = select_shortlist(papers,news,profile['directions'])
            progress(76,'analyzing','正在通过本机 Codex 分析%d条重点候选；其余条目保留规则初筛。' % len(shortlist))
            if len(papers)+len(news)>len(shortlist):
                warnings.append('为控制本机 Codex 分析耗时，单次自动分析最多24条，通常优先18篇论文与6条新闻；未入选候选保留规则初筛。')
            summary = enrich(shortlist,profile,settings,data_dir or store.path.parent)
            mode = 'codex'
            warnings.append('AI 已分析 %d / 保留 %d 条；其余条目为规则初筛。' % (sum(1 for item in shortlist if item.get('ai_enriched')),len(papers)+len(news)))
        except Exception as exc:
            warnings.append('Codex 未完成：' + str(exc)[:300])
    elif not use_codex:
        warnings.append('本次未启用 Codex，所有分析均为规则初筛。')
    papers.sort(key=lambda item:item['score'],reverse=True)
    news.sort(key=lambda item:item['score'],reverse=True)
    report = dict(id=uuid.uuid4().hex[:16],start=start,end=end,created_at=sources.now_iso(),status='partial' if any(status['status'] in ('error','partial') for status in statuses) or not active or (use_codex and mode!='codex') else 'completed',summary=summary,analysis_mode=mode,papers=papers,news=news,conferences=conferences,sources=statuses,warnings=list(dict.fromkeys(warnings)),candidate_count=candidate_count,paper_count=len(papers),news_count=len(news),timezone='Asia/Shanghai',profile_as_of=profile.get('as_of'))
    report['ai_analyzed_count'] = sum(bool(item.get('ai_enriched')) for item in papers+news)
    report['retained_count'] = len(papers)+len(news)
    progress(97,'saving','正在将报告和来源覆盖记录保存到本机。')
    store.save_report(report)
    return report


def report_markdown(report):
    def safe(value):
        return str(value or '').replace('<','&lt;').replace('>','&gt;')
    lines = ['# AIM Radar 研究周报', '', '%s — %s · Asia/Shanghai' % (report['start'],report['end']), '', '分析模式：' + ('Codex + 规则初筛' if report['analysis_mode']=='codex' else '规则初筛'), '', safe(report['summary']), '']
    for heading, key in [('论文线索','papers'),('产业动态','news')]:
        lines += ['## '+heading,'']
        for item in report[key]:
            lines += ['### '+safe(item['title']), '', '- 原文：'+item['url'], '- 来源：'+safe(item['source']), '- 日期：%s（%s）' % (safe(item['published_at']),safe(item['date_basis'])), '- 状态：'+safe(item['status']), '- 方向：'+', '.join(item['directions']), '- 阅读优先级：'+str(item['score']), '', safe(item['summary']), '', safe(item['why_it_matters']), '', '建议行动：'+safe(item['suggested_action']), '']
            if len(item.get('sources',[])) > 1:
                lines += ['来源证据：'] + ['- %s · %s · %s' % (safe(source['name']),safe(source.get('date_basis')),source['url']) for source in item['sources']] + ['']
    lines += ['## 来源覆盖','']
    lines += ['- %s：%s · %s' % (safe(source['name']),safe(source['status']),safe(source['message'])) for source in report['sources']]
    lines += ['', '## 说明与局限', ''] + ['- '+safe(warning) for warning in report['warnings']]
    return '\n'.join(lines) + '\n'
