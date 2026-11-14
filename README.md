# pixiv 用户作品批量保存

按 pixiv 用户 ID 批量下载该用户「插画 / 漫画」的**原图**，支持多页作品、断点续传、失败补漏。

脚本只依赖 Python 标准库，不需要安装第三方包。

## 目录结构

```
pixiv_saved/
├── scripts/
│   ├── pixiv_sync.py        # 主脚本：拉取作品列表 + 下载原图
│   └── pixiv_retry.py       # 补漏：重下遗漏/失败的页
├── config.example.json      # 配置示例（复制为 config.json 后填写）
├── README.md
├── .gitignore
├── <UID>/                   # 每个用户一个文件夹（已被 .gitignore 忽略）
│   ├── <作品id>_<标题>_p0.jpg
│   ├── <作品id>_<标题>_p1.jpg
│   └── _manifest.csv        # 该用户的清单
└── _state_<UID>.json        # 断点状态（忽略）
```

## 环境要求

- Python 3.9+
- 能访问 `www.pixiv.net` 与 `i.pximg.net`
  - 如果本机直连被墙，请先开代理（脚本支持 `--proxy` / `config.json`）
- 一个登录状态的 pixiv 会话 Cookie（R-18 作品必须登录才能拿到原图地址）

## 配置

复制示例配置：

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
  "cookie": "PHPSESSID=你的PHPSESSID; cf_clearance=可选的CloudflareCookie",
  "proxy": "http://127.0.0.1:7890"
}
```

### 如何获取 Cookie

1. 用浏览器登录 pixiv，打开任意页面；
2. 按 `F12` → **网络 / Network**，刷新页面，点任意 `pixiv.net` 请求；
3. 在请求头里找到 `Cookie`，至少复制 `PHPSESSID=...`；
   - 如果图床有 Cloudflare 校验，把 `cf_clearance=...` 也一起复制。
4. 也可以从浏览器的 **应用 / Application → Cookies** 里复制 `PHPSESSID`。

> `config.json` 已被 `.gitignore` 忽略，不会进版本库。

## 使用

在项目根目录执行：

```bash
# 下载某个用户的全部作品
python scripts/pixiv_sync.py 104319135

# 指定输出目录 / 代理 / 并发数
python scripts/pixiv_sync.py 104319135 --out D:\pixiv_saved --proxy http://127.0.0.1:7890 --workers 8

# 也可以直接传 cookie，不写 config.json
python scripts/pixiv_sync.py 104319135 --cookie "PHPSESSID=..."
```

下载完成后输出在 `./<UID>/`，并生成 `_manifest.csv`。

### 补漏

如果因为图床限流、网络抖动导致部分页失败：

```bash
python scripts/pixiv_retry.py 126854763
```

`pixiv_retry.py` 会扫描 `_state_<UID>.json` 里所有作品的 `bases`，
把磁盘上不存在、或体积小于 20 KB 的页重新下载，默认 3 并发、轮询重试。

### 断点续传

直接重跑同一条命令即可：

```bash
python scripts/pixiv_sync.py 104319135
```

- 已存在且 ≥ 20 KB 的文件会跳过；
- 进度保存在 `_state_<UID>.json`；
- 中途 `Ctrl+C` 后再次运行会从断点继续。

## 输出命名

```
<作品id>_<标题>_p<页码>.<扩展名>
```

例：

```
152069465_Princess Peach 1_p0.jpg
150613590_【# 147P】Bred by the Beast_p23.jpg
```

- 同一标题的多个作品用作品 id 区分；
- 多页作品逐页保存；
- 原图可能是 jpg / png / gif，脚本会按扩展名探测。

## 常见问题

**429 Too Many Requests**
pixiv 的 `/ajax` 接口有频率限制。脚本会自动退避重试；如果持续 429，减少并发或隔一段时间再跑。

**TLS 握手失败 / `SSLEOFError`**
`i.pximg.net` 图床会对短时间大量请求做临时限制，表现为 TLS 握手被断开。
用 `pixiv_retry.py` 低并发轮询即可补完；通常几十秒后恢复。

**R-18 作品拿不到原图**
未登录时 `urls.original` 为空。请配置带登录态的 `PHPSESSID`。

**视频 / ugoira**
`illustType == 2` 的作品是动图，脚本保存的是官方预览静帧 `<id>_ugoira0.jpg`，体积会明显小于普通插画。

**部分作品标题是特殊字符**
文件名会做安全化处理（去掉 `\ / : * ? " < > |` 等），并截断到 60 字符。

## 免责声明

仅供个人备份与学习使用。请遵守 pixiv 服务条款及当地法律，不要用于二次分发或商业用途；下载前请确认你拥有相应授权。
