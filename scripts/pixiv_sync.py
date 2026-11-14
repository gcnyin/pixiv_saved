#!/usr/bin/env python3
"""
pixiv_sync.py — 批量保存某个 pixiv 用户的作品原图（可断点续传）

用法:
    python scripts/pixiv_sync.py <UID> [--out DIR] [--workers N]
                                 [--proxy URL] [--cookie COOKIE] [--config FILE]

示例:
    python scripts/pixiv_sync.py 104319135
    python scripts/pixiv_sync.py 104319135 --out D:\\pixiv_saved

配置优先级: 命令行 > 环境变量 PIXIV_COOKIE / PIXIV_PROXY > config.json
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
CHUNK = 40

lock = threading.Lock()
state = None
state_lock = threading.Lock()
cfg = {}
paths = {}
_page_count = [0]


# ---------------------------------------------------------------- config
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
    proxy = args.proxy or os.environ.get('PIXIV_PROXY') or data.get('proxy')
    if not cookie:
        sys.exit('缺少 cookie：请用 --cookie 传入，或设置 PIXIV_COOKIE，或在 config.json 中配置。\n'
                 'cookie 至少应包含 PHPSESSID=...（登录状态下访问 pixiv 时从浏览器开发者工具复制）。')
    return {'cookie': cookie, 'proxy': proxy}


def build_opener():
    if cfg.get('proxy'):
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': cfg['proxy'], 'https': cfg['proxy']}))
    return urllib.request.build_opener()


# ---------------------------------------------------------------- helpers
def log(msg):
    line = time.strftime('%H:%M:%S ') + msg
    print(line, flush=True)
    with open(paths['log'], 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def save_state():
    with state_lock:
        tmp = paths['state'] + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, paths['state'])


def get_json(url, tries=6, base_wait=10):
    last = None
    for i in range(tries):
        op = build_opener()
        try:
            req = urllib.request.Request(
                url, headers={'Referer': REFERER, 'User-Agent': UA,
                              'Cookie': cfg['cookie'], 'Accept': 'application/json'})
            with op.open(req, timeout=45) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                wait = min(base_wait * (2 ** i), 60)
                log('429 rate limit -> wait %ds' % wait)
                time.sleep(wait)
                continue
            time.sleep(3)
        except Exception as e:
            last = e
            time.sleep(3)
    return {'__err': str(last)}


def safe(s, n=60):
    s = re.sub(r'[\x00-\x1f\\/:*?"<>|]', '_', s or 'untitled')
    s = re.sub(r'\s+', ' ', s).strip().strip('.')
    return s[:n] or 'untitled'


def parse_thumb(thumb, wid, page_count, illust_type):
    """从缩略图 URL 推导原图 URL（原图扩展名稍后探测）。"""
    thumb = thumb or ''
    m = re.search(r'/img/(\d{4}/\d{2}/\d{2}/\d{2}/\d{2}/\d{2})/([^/]+)$', thumb)
    if not m:
        return []
    dp, fn = m.group(1), m.group(2)
    stem = re.sub(r'\.(?:jpg|png|gif|webp)$', '', fn)
    prefix = 'https://i.pximg.net/img-original/img/%s/' % dp
    if '_p0_' in stem:
        base = stem.split('_p0_')[0]
        return [prefix + '%s_p%d' % (base, n) for n in range(max(1, page_count or 1))]
    if illust_type == 2 or '_square1200' in stem or '_master1200' in stem:
        return [prefix + '%s_ugoira0.jpg' % wid]
    return []


def fetch_bytes(url):
    op = build_opener()
    req = urllib.request.Request(url, headers={'Referer': REFERER, 'User-Agent': UA})
    with op.open(req, timeout=60) as r:
        return r.read()


def download_page(base, path_base, tries=3):
    """按 jpg/png/gif/webp 依次探测并下载；网络错误重试，404 换扩展名。"""
    last = None
    for ext in EXTS:
        url = base + '.' + ext
        for i in range(tries):
            try:
                data = fetch_bytes(url)
                if len(data) < 1024:
                    raise IOError('too small %d' % len(data))
                path = path_base + '.' + ext
                tmp = path + '.part'
                with open(tmp, 'wb') as f:
                    f.write(data)
                os.replace(tmp, path)
                return path, len(data), url
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (404, 403):
                    break  # 换扩展名
                if i == tries - 1:
                    break
                time.sleep(2.0 * (i + 1))
            except Exception as e:
                last = e
                if i == tries - 1:
                    break
                time.sleep(2.0 * (i + 1))
    raise IOError('all extensions failed: %r' % (last,))


# ---------------------------------------------------------------- state
def load_state(uid):
    global state
    if os.path.exists(paths['state']):
        with open(paths['state'], encoding='utf-8') as f:
            state = json.load(f)
        log('RESUME uid=%s ids=%d meta=%d pages=%d' % (
            uid, len(state.get('ids', [])), len(state.get('meta', {})), len(state.get('pages', {}))))
    else:
        st = {'uid': uid, 'ids': [], 'meta': {}, 'pages': {}, 'errors': []}
        with open(paths['state'], 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
        state = st
    state.setdefault('ids', [])
    state.setdefault('meta', {})
    state.setdefault('pages', {})
    state.setdefault('errors', [])
    return state


def fetch_ids(uid):
    if state['ids']:
        return
    for _ in range(30):
        j = get_json('%s/ajax/user/%s/profile/all?lang=zh' % (BASE, uid), tries=1)
        if '__err' not in j and not j.get('error'):
            ids = []
            b = j['body']
            for k in ('illusts', 'manga'):
                for wid in (b.get(k) or {}):
                    if wid.isdigit() and wid not in ids:
                        ids.append(wid)
            state['ids'] = ids
            save_state()
            log('IDS %d' % len(ids))
            return
        log('profile/all not ready: %s' % (j.get('__err') or j.get('message')))
        time.sleep(10)
    raise SystemExit('could not fetch ids')


def page_key(wid, n):
    return '%s_p%d' % (wid, n)


def already_ok(key):
    rec = state['pages'].get(key)
    if not rec or rec.get('status') not in ('ok', 'exists'):
        return False
    p = rec.get('file')
    return bool(p and os.path.exists(p) and os.path.getsize(p) >= MIN_BYTES)


# ---------------------------------------------------------------- download
def download_job(job):
    wid, title, n, base = job
    key = page_key(wid, n)
    if already_ok(key):
        return
    name_base = '%s_%s_p%d' % (wid, safe(title), n)
    path_base = os.path.join(paths['user'], name_base)
    status, size, url, name = 'fail', 0, '', name_base
    try:
        existing = None
        for ext in EXTS:
            p = path_base + '.' + ext
            if os.path.exists(p) and os.path.getsize(p) >= MIN_BYTES:
                existing = p
                break
        if existing:
            size = os.path.getsize(existing)
            status = 'exists'
            name = os.path.basename(existing)
            url = base + '.' + existing.rsplit('.', 1)[-1]
        else:
            path, size, url = download_page(base, path_base)
            name = os.path.basename(path)
            status = 'ok' if size >= MIN_BYTES else 'small'
    except Exception as e:
        log('FAIL %s p%d %s :: %s' % (wid, n, base, e))
        status = 'fail'
    with lock:
        state['pages'][key] = {
            'status': status,
            'file': os.path.join(paths['user'], name) if status in ('ok', 'exists', 'small') else None,
            'bytes': size, 'url': url,
        }
        _page_count[0] += 1
        if _page_count[0] % 10 == 0:
            save_state()
            done = sum(1 for r in state['pages'].values() if r.get('status') in ('ok', 'exists'))
            print('PROGRESS pages_done=%d this_run=%d' % (done, _page_count[0]), flush=True)


def download_chunk(uid, chunk_ids):
    jobs, skipped = [], []
    for wid in chunk_ids:
        m = state['meta'].get(wid)
        if not m:
            continue
        bases = m.get('bases') or parse_thumb(m.get('thumb'), wid, m.get('pageCount'), m.get('illustType'))
        if not bases:
            skipped.append(wid)
            continue
        m['bases'] = bases
        for n, b in enumerate(bases):
            jobs.append((wid, m.get('title') or '', n, b))
    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(download_job, jobs))
    if skipped:
        log('SKIP_NO_THUMB %s' % skipped[:5])
    save_state()


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


# ---------------------------------------------------------------- main
def main():
    global state, cfg
    load_state(args.uid)
    cfg = load_config(args.config)

    os.makedirs(paths['user'], exist_ok=True)
    fetch_ids(args.uid)
    ids = state['ids']
    meta = state['meta']

    missing_meta = [x for x in ids if x not in meta]
    log('TODO ids=%d missing_meta=%d' % (len(ids), len(missing_meta)))
    chunks = [missing_meta[i:i + CHUNK] for i in range(0, len(missing_meta), CHUNK)]
    for ci, chunk in enumerate(chunks, 1):
        qs = '&'.join('ids[]=' + x for x in chunk)
        j = get_json('%s/ajax/user/%s/illusts?%s&lang=zh' % (BASE, args.uid, qs))
        if '__err' in j or j.get('error'):
            log('CHUNK_META_ERR %d: %s' % (ci, j.get('__err') or j.get('message')))
            time.sleep(5)
            continue
        b = j['body']
        got = 0
        for wid in chunk:
            it = b.get(wid)
            if not it:
                continue
            meta[wid] = {
                'id': wid, 'title': it.get('title'), 'userName': it.get('userName'),
                'userId': it.get('userId'), 'xRestrict': it.get('xRestrict'),
                'illustType': it.get('illustType'), 'pageCount': it.get('pageCount'),
                'createDate': it.get('createDate'), 'tags': it.get('tags'),
                'width': it.get('width'), 'height': it.get('height'),
                'aiType': it.get('aiType'), 'thumb': it.get('url'),
            }
            got += 1
        save_state()
        log('CHUNK %d/%d meta=%d got=%d -> downloading' % (ci, len(chunks), len(meta), got))
        download_chunk(args.uid, chunk)
        time.sleep(3)

    # 少数作品批量接口拿不到时，逐个补
    still = [x for x in ids if x not in meta]
    if still:
        log('FINAL_META_MISSING %d (retry per-id)' % len(still))
        for wid in still:
            j = get_json('%s/ajax/illust/%s' % (BASE, wid))
            if '__err' not in j and not j.get('error'):
                it = j['body']
                meta[wid] = {
                    'id': wid, 'title': it.get('illustTitle'), 'userName': it.get('userName'),
                    'userId': it.get('userId'), 'xRestrict': it.get('xRestrict'),
                    'illustType': it.get('illustType'), 'pageCount': it.get('pageCount'),
                    'createDate': it.get('createDate'), 'tags': it.get('tags'),
                    'width': it.get('width'), 'height': it.get('height'),
                    'aiType': it.get('aiType'), 'thumb': (it.get('urls') or {}).get('thumb'),
                }
                o = (it.get('urls') or {}).get('original')
                if o:
                    mm = re.match(r'(.*_p)0(\.\w+)$', o)
                    bases = [o]
                    if mm and (it.get('pageCount') or 1) > 1:
                        for n in range(1, it.get('pageCount')):
                            bases.append('%s%d%s' % (mm.group(1), n, mm.group(2)))
                    meta[wid]['bases'] = bases
            save_state()
            time.sleep(2)
        download_chunk(args.uid, still)

    # 补漏：所有已知作品里状态不是 ok 的页
    need = []
    for wid in ids:
        m = state['meta'].get(wid)
        if not m:
            continue
        bases = m.get('bases') or parse_thumb(m.get('thumb'), wid, m.get('pageCount'), m.get('illustType'))
        if not bases:
            continue
        m['bases'] = bases
        if any(not already_ok(page_key(wid, n)) for n in range(len(bases))):
            need.append(wid)
    if need:
        log('FINAL_DOWNLOAD_SWEEP works=%d' % len(need))
        for i in range(0, len(need), CHUNK):
            download_chunk(args.uid, need[i:i + CHUNK])
            time.sleep(2)

    save_state()
    write_manifest()
    done = sum(1 for r in state['pages'].values() if r.get('status') in ('ok', 'exists'))
    fail = sum(1 for r in state['pages'].values() if r.get('status') == 'fail')
    log('FINISHED uid=%s meta=%d/%d pages=%d ok=%d fail=%d'
        % (args.uid, len(state['meta']), len(ids), len(state['pages']), done, fail))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='批量保存 pixiv 用户作品原图（可断点续传）')
    ap.add_argument('uid', help='pixiv 用户 ID，例如 104319135')
    ap.add_argument('--out', default='.', help='输出根目录（默认当前目录）')
    ap.add_argument('--workers', type=int, default=10, help='同时下载的图片数（默认 10）')
    ap.add_argument('--proxy', default=None, help='HTTP 代理，例如 http://127.0.0.1:7890')
    ap.add_argument('--cookie', default=None, help='pixiv 登录 Cookie（至少包含 PHPSESSID）')
    ap.add_argument('--config', default=None, help='config.json 路径')
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    paths = {
        'user': os.path.join(out, args.uid),
        'state': os.path.join(out, '_state_%s.json' % args.uid),
        'log': os.path.join(out, '_sync_%s.log' % args.uid),
        'manifest': os.path.join(out, args.uid, '_manifest.csv'),
    }
    main()
