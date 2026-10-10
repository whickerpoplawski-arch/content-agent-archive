#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公众号(GZH)面经抓取器：绕过验证码抓取合集全部文章并提取面试问题。

背景: 微信文章链接分两类
  - /s/<key> 短链: 直接可访问
  - /s?__biz=..&mid=..&idx=..&sn=.. 参数链: 无 chksm 时会触发滑块验证码
  - 但带 chksm 参数的同款链接可免验证直接访问
本脚本用 "带 chksm 的 pre/next 链接" 走遍公众号合集, 再逐篇提取面试问题。

用法:
  python scripts/gzh_crawler.py <主页URL> -o 输出.md
  例: python scripts/gzh_crawler.py https://mp.weixin.qq.com/s/DQZTjemG8ZV1NhlEH5mF_w -o out.md
依赖: requests, beautifulsoup4 (已在本项目 requirements / 环境内)
"""
import argparse
import html as H
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
HEADERS = {'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9'}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# 广告/噪音行特征(用于清理正文)
AD_KW = ['面经哥', '点击查看', '飞书', '获取方式', '预览时标签', '文档部分截图',
         '清单包含', '当前版本更新']
HEAD_PREFIX = ('一面', '二面', '三面', '反问', '面试问题', '自我介绍', '第')
EOL = ('。', '？', '?', '!', '！', ':')


def fetch(url, timeout=60, retry=3):
    last = None
    for i in range(retry):
        try:
            r = SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            body = r.content
            t = body.decode('utf-8', 'replace')
            if 'verify.html' in t or 'wappoc' in t:
                # 触发验证码页, 等待后重试
                last = 'captcha'
                time.sleep(2 * (i + 1))
                continue
            return body
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError('fetch failed: %s (last=%s)' % (url[:100], last))


def clean_url(u):
    u = u.replace('\\x26', '&')  # JS 转义 \x26 -> &
    return H.unescape(u)


def parse_msg_link(html):
    m = re.search(r'var msg_link = "([^"]+)"', html)
    return H.unescape(m.group(1)) if m else None


def parse_pre_next(html):
    """从文章页 JS 数据(appmsgalbuminfo)中取 pre/next 链接(带 chksm)。"""
    def g(key):
        m = re.search(key + r'\s*:\s*"([^"]*)"', html)
        if not m:
            m = re.search(key + r"\s*:\s*'([^']*)'", html)
        return clean_url(m.group(1)) if m else ''
    return g('pre_article_link'), g('next_article_link')


def get_ext(msg_link):
    """调 mp/getappmsgext 拿该文所属合集的 pre/next(带 chksm)。"""
    p = dict(re.findall(r'([a-z_]+)=([^&]+)', msg_link))
    url = ('https://mp.weixin.qq.com/mp/getappmsgext?__biz={}&mid={}&idx={}&sn={}&chksm={}&f=json'
           .format(p['__biz'], p['mid'], p['idx'], p['sn'], p['chksm']))
    try:
        j = json.loads(fetch(url, 40, 2))
        ext = j.get('appmsg_album_extinfo', {})
        return (clean_url(ext.get('pre_article_link', '')),
                clean_url(ext.get('next_article_link', '')))
    except Exception:
        return '', ''


def content_text(html):
    s = html.find('id="js_content"')
    if s < 0:
        return ''
    e = html.find('rich_media_tool', s)
    seg = html[s:e if e > 0 else s + 200000]
    soup = BeautifulSoup(seg, 'html.parser')
    for tag in soup(['script', 'style']):
        tag.decompose()
    return soup.get_text('\n', strip=True)


def is_heading(l):
    if any(k in l for k in AD_KW):
        return False
    if l.startswith(HEAD_PREFIX):
        return True
    return len(l) <= 26 and not l.endswith(EOL)


def extract_questions(text):
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    first = None
    for i, l in enumerate(lines):
        if re.match(r'^\d+[、．.]', l):
            first = i
            break
    start = first if first is not None else 0
    while start > 0 and is_heading(lines[start - 1]):
        start -= 1
    end = len(lines)
    for i in range(start, len(lines)):
        if re.search(r'《AI|ask\.feishu|api\.weixin', lines[i]):
            end = i
            break
    out = []
    for l in lines[start:end]:
        if any(k in l for k in AD_KW):
            continue
        if '<' in l or 'js_temp' in l or 'js_tag' in l:
            continue
        out.append(l)
    return out


def md_questions(qs):
    buf = []
    for l in qs:
        m = re.match(r'^(\d+)[、．.]\s*(.*)$', l)
        if m:
            buf.append(f"{m.group(1)}、{m.group(2)}")
            continue
        m2 = re.match(r'^追问[:：]\s*(.*)$', l)
        if m2:
            buf.append(f"    追问: {m2.group(1)}")
            continue
        if is_heading(l):
            buf.append('**' + l + '**')
        else:
            buf.append(l)
    return buf


def h1(html):
    m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
    return re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ''


def collect_album(seed_url, outdir):
    """从主页 pre_article_link 出发, 经 getappmsgext 的 pre/next 多次扫描直到闭合。"""
    os.makedirs(outdir, exist_ok=True)
    visited = {}

    def add(link):
        mm = re.search(r'mid=(\d+)', link)
        ii = re.search(r'idx=(\d+)', link)
        if not mm:
            return
        key = mm.group(1) + '_' + ii.group(1)
        if key in visited:
            return
        try:
            html = fetch(link, retry=3)
        except Exception:
            return
        if b'js_content' not in html:
            return
        out = os.path.join(outdir, f'{key}.html')
        with open(out, 'wb') as f:
            f.write(html)
        visited[key] = {'file': out,
                        'ml': parse_msg_link(html.decode('utf-8', 'replace')) or link}

    add(seed_url)
    changed = True
    guard = 0
    while changed and guard < 200:
        changed = False
        guard += 1
        for key in list(visited.keys()):
            pre, nxt = get_ext(visited[key]['ml'])
            for link in (pre, nxt):
                if not link:
                    continue
                before = len(visited)
                add(link)
                if len(visited) > before:
                    changed = True
            time.sleep(0.15)
    return visited


def main():
    ap = argparse.ArgumentParser(description='GZH 面经抓取')
    ap.add_argument('main_url', help='主页文章 /s/<key> 链接')
    ap.add_argument('-o', '--output', default='AI_Agent面试问题合集.md')
    ap.add_argument('-d', '--dir', default='_gzh_html', help='HTML 缓存目录')
    args = ap.parse_args()

    html = fetch(args.main_url)
    ht = html.decode('utf-8', 'replace')

    m = re.search(r'pre_article_link\s*:\s*"([^"]*)"', ht) or \
        re.search(r"pre_article_link\s*:\s*'([^']*)'", ht)
    seed = clean_url(m.group(1)) if m else None
    if not seed:
        sys.exit('主页未找到 pre_article_link(可能非合集内文章)')

    visited = collect_album(seed, args.dir)
    print('collected:', len(visited))

    items = []
    for key, info in visited.items():
        h = open(info['file'], encoding='utf-8', errors='replace').read()
        items.append({'key': key, 'title': h1(h), 'url': info['ml'],
                      'qs': extract_questions(content_text(h))})
    items.sort(key=lambda x: x['key'])

    out = []
    out.append('# AI Agent 面试问题合集（面经哥）')
    out.append('')
    out.append('> 来源: ' + args.main_url)
    out.append(f'> 收录主页 + 合集全部 {len(items)} 篇')
    out.append('')
    out.append('## 主页：' + h1(ht))
    out.append('')
    out.append('> ' + args.main_url)
    out.append('')
    out.extend(md_questions(extract_questions(content_text(ht))))
    out.append('')
    out.append('---')
    out.append('')
    for i, it in enumerate(items, 1):
        out.append(f"### {i}. {it['title']}")
        out.append('')
        out.append('> ' + it['url'])
        out.append('')
        out.extend(md_questions(it['qs']))
        out.append('')
        out.append('---')
        out.append('')

    with open(args.output, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
    print('written:', args.output)


if __name__ == '__main__':
    main()