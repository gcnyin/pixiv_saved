#!/usr/bin/env python3
"""
pixiv_retry.py — 补下 pixiv_sync.py 遗漏/失败的图片（可反复运行）

用法:
    python scripts/pixiv_retry.py <UID> [--out DIR] [--workers N]
                                  [--proxy URL] [--cookie COOKIE] [--config FILE]

示例:
    python scripts/pixiv_retry.py 126854763
"""
import argparse
import concurrent.futures
import csv
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

BASE = 'https://www.pixiv.net'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0'
REFERER = BASE + '/'
MIN_BYTES = 20 * 1024
EXTS = ['jpg', 'png', 'gif', 'webp']

lock = threading.Lock()
cfg = {}
paths = {}
state = None


def load_config(explicit):
    data = {}
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append('config.json')
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.json'))
    for p in candidates:
        if p and os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                data = json.load(f)
            break
    cookie = args.cookie or os.environ.get('PIXIV_COOKIE') or data.get('cookie')
    if getattr(args, 'no_proxy', False):
        proxy = None
    else:
        proxy = args.proxy or os.environ.get('PIXIV_PROXY') or data.get('proxy') or None
    if not cookie:
        sys.exit('缺少 cookie：请用 --cookie 传入，或设置 PIXIV_COOKIE，或在 config.json 中配置。')
    return {'cookie': cookie, 'proxy': proxy}


def build_opener():
    if cfg.get('proxy'):
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': cfg['proxy'], 'https': cfg['proxy']}))
    return urllib.request.build_opener()


def log(msg):
    line = time.strftime('%H:%M:%S ') + msg
    print(line, flush=True)
    with open(paths['log'], 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def save_state():
    with lock:
        tmp = paths['state'] + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, paths['state'])


def safe(s, n=60):
    s = re.sub(r'[\x00-\x1f\\/:*?"<>|]', '_', s or 'untitled')
    s = re.sub(r'\s+', ' ', s).strip().strip('.')
    return s[:n] or 'untitled'


def fetch(url):
    op = build_opener()
    req = urllib.request.Request(url, headers={'Referer': REFERER, 'User-Agent': UA})
    with op.open(req, timeout=45) as r:
        return r.read()


def existing_file(wid, n, title):
    for ext in EXTS:
        p = os.path.join(paths['user'], '%s_%s_p%d.%s' % (wid, safe(title), n, ext))
        if os.path.exists(p) and os.path.getsize(p) >= MIN_BYTES:
            return p
    return None


def try_page(wid, n, base, title):
    last = None
    for ext in EXTS:
        url = base + '.' + ext
        try:
            data = fetch(url)
            if len(data) < 1024:
                raise IOError('too small %d' % len(data))
            path = os.path.join(paths['user'], '%s_%s_p%d.%s' % (wid, safe(title), n, ext))
            tmp = path + '.part'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, path)
            return path, len(data), url
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 403):
                continue
            return None, 0, str(last)
        except Exception as e:
            last = e
            return None, 0, str(last)
    return None, 0, str(last)


def write_manifest():
    rows = []
    for wid in state['ids']:
        m = state['meta'].get(wid)
        if not m:
            continue
        for n in range(len(m.get('bases') or [])):
            r = state['pages'].get('%s_p%d' % (wid, n), {})
            tags = '/'.join(m.get('tags') or []) if isinstance(m.get('tags'), list) else m.get('tags')
            rows.append([wid, m.get('title'), m.get('createDate'), m.get('xRestrict'),
                         m.get('illustType'), n, len(m.get('bases') or []), tags,
                         r.get('bytes'), os.path.basename(r.get('file') or ''), r.get('status')])
    with open(paths['manifest'], 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['id', 'title', 'createDate', 'xRestrict', 'illustType',
                    'page', 'pageCount', 'tags', 'bytes', 'file', 'status'])
        w.writerows(rows)
    log('MANIFEST %d rows' % len(rows))


def main():
    global cfg, state
    cfg = load_config(args.config)
    if not os.path.exists(paths['state']):
        sys.exit('找不到 %s，请先运行 pixiv_sync.py' % paths['state'])
    with open(paths['state'], encoding='utf-8') as f:
        state = json.load(f)
    os.makedirs(paths['user'], exist_ok=True)

    pending = []
    for wid in state['ids']:
        m = state['meta'].get(wid) or {}
        bases = m.get('bases') or []
        title = m.get('title') or ''
        for n, base in enumerate(bases):
            if existing_file(wid, n, title):
                continue
            pending.append((wid, n, base, title))
    log('PENDING_MISSING %d' % len(pending))

    def work(item):
        wid, n, base, title = item
        path, size, info = try_page(wid, n, base, title)
        if path:
            with lock:
                state['pages']['%s_p%d' % (wid, n)] = {
                    'status': 'ok', 'file': path, 'bytes': size, 'url': info}
            return True
        return False

    round_no = 0
    while pending and round_no < 80:
        round_no += 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(work, pending))
        nxt = [p for p, ok in zip(pending, results) if not ok]
        okc = len(pending) - len(nxt)
        save_state()
        log('ROUND %d ok=%d left=%d' % (round_no, okc, len(nxt)))
        pending = nxt
        if pending:
            time.sleep(25)

    write_manifest()
    log('DONE uid=%s remaining=%d' % (args.uid, len(pending)))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='补下 pixiv 作品遗漏/失败的图片')
    ap.add_argument('uid', help='pixiv 用户 ID')
    ap.add_argument('--out', default='.', help='输出根目录（默认当前目录）')
    ap.add_argument('--workers', type=int, default=3, help='并发数（默认 3，避免图床限流）')
    ap.add_argument('--proxy', default=None, help='HTTP 代理（可选），例如 http://127.0.0.1:7890')
    ap.add_argument('--no-proxy', action='store_true', help='强制直连，忽略 config.json / 环境变量中的代理')
    ap.add_argument('--cookie', default=None, help='pixiv 登录 Cookie')
    ap.add_argument('--config', default=None, help='config.json 路径')
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    paths = {
        'user': os.path.join(out, args.uid),
        'state': os.path.join(out, '_state_%s.json' % args.uid),
        'log': os.path.join(out, '_retry_%s.log' % args.uid),
        'manifest': os.path.join(out, args.uid, '_manifest.csv'),
    }
    main()
