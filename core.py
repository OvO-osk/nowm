"""分享链接 → 无水印原视频 / 原图地址，并下载到本地。
支持：抖音、小红书、快手、B站、微博、TikTok、X、YouTube、Instagram。
requests；YouTube/Instagram 另需 yt-dlp + ffmpeg。

思路（都是拿平台自己给播放器用的原片地址，不做任何“去水印”图像处理）：
- 抖音：iesdouyin 分享页的 _ROUTER_DATA → play_addr，playwm 换 play。
- 小红书：笔记页 __INITIAL_STATE__ → 视频流 masterUrl / 图片原图。
- 快手：手机分享页 INIT_STATE → photoUrl / mainMvUrls。
- B站：公开 API view + playurl（html5 平台给 mp4 单文件）。B站本来就没有烧进画面的水印。
- 微博：m.weibo.cn/statuses/show → page_info 里各清晰度 mp4 / 图片 large。
- TikTok：视频页 __UNIVERSAL_DATA_FOR_REHYDRATION__ → playAddr（同一会话的 cookie 才能下）。
"""
import json
import os
import re
import time

import requests

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
DESK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

URL_RE = re.compile(r"https?://[^\s一-鿿，。！？、]+")

# 一个会话贯穿解析和下载（TikTok/微博要靠同一份 cookie）
S = requests.Session()


class ParseError(Exception):
    pass


def find_url(text):
    m = URL_RE.search(text or "")
    if not m:
        raise ParseError("没找到链接，请把分享文字整段粘贴进来")
    return m.group(0).rstrip(".,;)")


def get(url, ua=DESK_UA, timeout=15, **kw):
    h = {"User-Agent": ua}
    h.update(kw.pop("headers", {}))
    return S.get(url, headers=h, timeout=timeout, allow_redirects=True, **kw)


def browser_get(url, mobile=False, wait_for=None, timeout=30):
    """反爬页面（JS 挑战 / 访客验证）用系统 Chrome 真打开一次，拿渲染后的 HTML，
    并把浏览器 cookie 同步进 S，后面下载才不会 403。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ParseError("这个链接需要浏览器验证，当前环境没装浏览器，请稍后重试或换个链接")
    with sync_playwright() as p:
        args = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
        try:  # 电脑上用系统 Chrome；云端（Docker）用 playwright 自带的 Chromium
            b = p.chromium.launch(channel="chrome", headless=True, args=args)
        except Exception:
            b = p.chromium.launch(headless=True, args=args)
        ctx = b.new_context(user_agent=MOBILE_UA if mobile else DESK_UA,
                            viewport={"width": 390, "height": 844} if mobile else {"width": 1366, "height": 900},
                            locale="zh-CN")
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        if wait_for:
            try:
                page.wait_for_function(wait_for, timeout=timeout * 1000)
            except Exception:
                pass
        else:
            page.wait_for_timeout(3000)
        html, final = page.content(), page.url
        for c in ctx.cookies():
            S.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        b.close()
    return final, html


def _json_after(html, marker):
    """取 `marker = {...}` 或 `<script id=marker>{...}</script>` 里的 JSON。"""
    i = html.find(marker)
    if i < 0:
        return None
    i = html.find("{", i)
    end = html.find("</script>", i)
    raw = html[i:end].strip().rstrip(";")
    raw = re.sub(r"\bundefined\b", "null", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 后面还跟着别的语句：用解码器只读第一个对象
        try:
            return json.JSONDecoder().raw_decode(raw)[0]
        except json.JSONDecodeError:
            return None


def _walk(obj, key):
    """在嵌套 JSON 里找第一个名为 key 的非空值。"""
    if isinstance(obj, dict):
        if obj.get(key):
            return obj[key]
        for v in obj.values():
            r = _walk(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _walk(v, key)
            if r:
                return r
    return None


def safe_name(s, n=40):
    s = re.sub(r'[\\/:*?"<>|\r\n\t#@]+', " ", s or "").strip()
    return s[:n].strip() or "untitled"


def result(platform, rid, title, author, kind, urls, cover="", referer=""):
    if not urls:
        raise ParseError("没取到下载地址")
    return {"platform": platform, "id": str(rid), "title": title or str(rid), "author": author or "",
            "type": kind, "urls": urls, "cover": cover or "", "referer": referer}


# ---------------- 抖音 ----------------
def parse_douyin(url):
    final = get(url, MOBILE_UA).url
    m = (re.search(r"/(?:video|note|slides)/(\d+)", final)
         or re.search(r"modal_id=(\d+)", final)
         or re.search(r"(\d{15,})", final))
    if not m:
        raise ParseError("抖音链接里没找到作品 id：" + final)
    aid = m.group(1)
    items = None
    for _ in range(3):  # 第一次没有 ttwid cookie 时页面不带作品数据，拿到 cookie 再取就有
        html = get(f"https://www.iesdouyin.com/share/video/{aid}/", MOBILE_UA,
                   headers={"Referer": "https://www.douyin.com/"}).text
        data = _json_after(html, "window._ROUTER_DATA")
        items = _walk(data, "item_list") if data else None
        if items:
            break
        time.sleep(0.6)
    if not items:
        raise ParseError("抖音页面解析失败（作品可能已删除/私密）")
    it = items[0]
    author = (it.get("author") or {}).get("nickname", "")
    ref = "https://www.douyin.com/"
    if it.get("images"):
        urls = []
        for im in it["images"]:
            lst = im.get("url_list") or []
            pick = next((u for u in lst if ".jpeg" in u or ".jpg" in u), lst[0] if lst else None)
            if pick:
                urls.append(pick)
        return result("douyin", aid, it.get("desc"), author, "images", urls, urls[0] if urls else "", ref)
    vurl = it["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
    vurl = re.sub(r"ratio=\w+", "ratio=1080p", vurl)
    cover = ((it["video"].get("cover") or {}).get("url_list") or [""])[0]
    return result("douyin", aid, it.get("desc"), author, "video", [vurl], cover, ref)


# ---------------- 小红书 ----------------
def parse_xhs(url):
    r = get(url)
    final, html = r.url, r.text
    m = re.search(r"/(?:explore|discovery/item|item)/([0-9a-f]{24})", final)
    nid = m.group(1) if m else None
    state = _json_after(html, "window.__INITIAL_STATE__")
    if (not state or not _walk(state, "noteDetailMap")) and nid:
        q = final.split("?", 1)[1] if "?" in final else ""
        html = get(f"https://www.xiaohongshu.com/explore/{nid}?{q}").text
        state = _json_after(html, "window.__INITIAL_STATE__")
    dmap = _walk(state, "noteDetailMap") if state else None
    if not dmap:  # 被风控时用浏览器再开一次
        final, html = browser_get(final, wait_for="!!window.__INITIAL_STATE__")
        state = _json_after(html, "window.__INITIAL_STATE__")
        dmap = _walk(state, "noteDetailMap") if state else None
    if not dmap:
        raise ParseError("小红书页面解析失败（请复制完整分享链接，链接要带 xsec_token）")
    note = (dmap.get(nid) or {}).get("note") if nid else None
    if not note:
        note = next((v["note"] for v in dmap.values() if isinstance(v, dict) and v.get("note")), None)
    if not note:
        raise ParseError("小红书笔记读不到（可能需要登录、已删除或链接过期）")
    nid = note.get("noteId") or nid
    title = note.get("title") or note.get("desc")
    author = (note.get("user") or {}).get("nickname", "")
    imgs = note.get("imageList") or []
    cover = imgs[0].get("urlDefault", "") if imgs else ""
    ref = "https://www.xiaohongshu.com/"
    if note.get("type") == "video" and note.get("video"):
        stream = ((note["video"].get("media") or {}).get("stream")) or {}
        cands = []
        for codec in ("h264", "h265", "av1", "h266"):
            for s in stream.get(codec) or []:
                u = s.get("masterUrl") or (s.get("backupUrls") or [None])[0]
                if u:
                    cands.append((codec == "h264", s.get("height", 0), s.get("size", 0), u))
        key = _walk(note["video"], "originVideoKey")
        if not cands and key:
            cands.append((True, 0, 0, "https://sns-video-bd.xhscdn.com/" + key))
        cands.sort(reverse=True)  # h264 优先（兼容性最好），再按分辨率
        return result("xhs", nid, title, author, "video", [c[3] for c in cands[:1]], cover, ref)
    urls = []
    for im in imgs:
        u = im.get("urlDefault") or im.get("url") or ""
        tok = re.search(r"/([^/!?]+)!", u)  # 去掉 !nd_dft 之类的处理后缀 → 原图
        if tok:
            u = "https://sns-img-bd.xhscdn.com/" + tok.group(1)
        if u:
            urls.append(u)
    return result("xhs", nid, title, author, "images", urls, cover, ref)


# ---------------- 快手 ----------------
def parse_kuaishou(url):
    r = get(url, MOBILE_UA, timeout=20)
    final, html = r.url, r.text
    m = re.search(r"(?:short-video|photo|fw/photo|long-video)/([0-9a-zA-Z]+)", final) or \
        re.search(r"photoId=([0-9a-zA-Z]+)", final)
    pid = m.group(1) if m else ""
    state = _json_after(html, "window.INIT_STATE") or _json_after(html, "window.__APOLLO_STATE__")
    if not state and pid:
        try:
            html = get(f"https://m.gifshow.com/fw/photo/{pid}", MOBILE_UA, timeout=20).text
            state = _json_after(html, "window.INIT_STATE")
        except requests.RequestException:
            pass
    if not _walk(state, "photoUrl") and not _walk(state, "mainMvUrls") and not _walk(state, "atlas"):
        final, html = browser_get(url, mobile=True, wait_for="!!window.INIT_STATE")
        state = _json_after(html, "window.INIT_STATE") or state
    if not state:
        raise ParseError("快手页面解析失败（快手风控较严，稍后重试或换一条链接）")
    photo = _walk(state, "photo") or {}
    author = photo.get("userName") or (_walk(state, "author") or {}).get("name", "")
    title = photo.get("caption") or _walk(state, "caption")
    pid = photo.get("photoId") or pid
    ref = "https://www.kuaishou.com/"
    atlas = _walk(photo, "atlas") or {}
    if atlas.get("list"):
        cdn = (atlas.get("cdnList") or atlas.get("cdn") or [{}])[0]
        host = cdn.get("cdn") if isinstance(cdn, dict) else cdn
        urls = [f"https://{host}{p}" for p in atlas["list"]]
        return result("kuaishou", pid, title, author, "images", urls, urls[0], ref)
    mv = photo.get("mainMvUrls") or _walk(state, "mainMvUrls") or []
    vurl = (mv[0].get("url") if mv else None) or photo.get("photoUrl") or _walk(state, "photoUrl")
    cover = ((photo.get("coverUrls") or [{}])[0]).get("url", "")
    return result("kuaishou", pid, title, author, "video", [vurl] if vurl else [], cover, ref)


# ---------------- B站 ----------------
def parse_bilibili(url):
    final = get(url).url if "b23.tv" in url or "bili2233" in url else url
    bv = re.search(r"(BV[0-9A-Za-z]{10})", final)
    av = re.search(r"/av(\d+)", final)
    if not bv and not av:
        raise ParseError("B站链接里没找到 BV 号：" + final)
    p = re.search(r"[?&]p=(\d+)", final)
    page = int(p.group(1)) if p else 1
    q = f"bvid={bv.group(1)}" if bv else f"aid={av.group(1)}"
    h = {"Referer": "https://www.bilibili.com/"}
    if "buvid3" not in S.cookies:
        get("https://www.bilibili.com/", headers=h)  # 没有 buvid3 cookie 会被 412 风控
    v = get(f"https://api.bilibili.com/x/web-interface/view?{q}", headers=h).json()
    if v.get("code") != 0:
        raise ParseError("B站：" + v.get("message", "视频不存在"))
    d = v["data"]
    pages = d.get("pages") or [{"cid": d["cid"], "part": ""}]
    pg = pages[min(page, len(pages)) - 1]
    title = d["title"] + (f" P{page} {pg.get('part', '')}" if len(pages) > 1 else "")
    pu = get(f"https://api.bilibili.com/x/player/playurl?{q}&cid={pg['cid']}&qn=80&fnval=1"
             f"&platform=html5&high_quality=1", headers=h).json()
    durl = (pu.get("data") or {}).get("durl") or []
    if not durl:
        raise ParseError("B站：取不到播放地址（可能是大会员/番剧/付费内容）")
    return result("bilibili", d["bvid"] + (f"_p{page}" if len(pages) > 1 else ""), title,
                  d["owner"]["name"], "video", [durl[0]["url"]], d.get("pic", ""), "https://www.bilibili.com/")


# ---------------- 微博 ----------------
WB_ORDER = ["mp4_1080p_mp4", "mp4_720p_mp4", "hevc_mp4_hd", "mp4_hd_mp4", "mp4_hd_url", "stream_url_hd",
            "mp4_ld_mp4", "mp4_sd_url", "stream_url"]


def parse_weibo(url):
    final = get(url, MOBILE_UA).url
    fid = re.search(r"(\d{4}:\d{10,})", final)
    if fid and "status" not in final and "detail" not in final:
        return _weibo_tv(fid.group(1))
    m = (re.search(r"m\.weibo\.cn/(?:detail|status)/(\w+)", final)
         or re.search(r"weibo\.c(?:om|n)/\d+/(\w+)", final)
         or re.search(r"[?&](?:id|mid)=(\w+)", final))
    if not m:
        raise ParseError("微博链接里没找到微博 id：" + final)
    mid = m.group(1)
    d = {}
    for attempt in range(2):
        r = get(f"https://m.weibo.cn/statuses/show?id={mid}", MOBILE_UA,
                headers={"Referer": "https://m.weibo.cn/", "X-Requested-With": "XMLHttpRequest"})
        try:
            d = r.json().get("data") or {}
        except ValueError:
            d = {}
        if d or attempt:
            break
        browser_get(f"https://m.weibo.cn/detail/{mid}", mobile=True)  # 过访客验证拿 cookie
    if not d:
        raise ParseError("微博读不到（可能已删除、仅粉丝可见，或需要登录）")
    st = d.get("retweeted_status") or d
    title = re.sub(r"<[^>]+>", "", st.get("text") or "")[:60]
    author = (st.get("user") or {}).get("screen_name", "")
    ref = "https://m.weibo.cn/"
    pi = st.get("page_info") or {}
    if pi.get("type") == "video" or pi.get("urls") or pi.get("media_info"):
        pool = {**(pi.get("media_info") or {}), **(pi.get("urls") or {})}
        vurl = next((pool[k] for k in WB_ORDER if pool.get(k)), None)
        cover = (pi.get("page_pic") or {}).get("url", "")
        return result("weibo", st.get("bid") or mid, title, author, "video", [vurl] if vurl else [], cover, ref)
    pics = st.get("pics") or []
    urls = [(p.get("large") or {}).get("url") or p.get("url") for p in pics]
    urls = [u for u in urls if u]
    if not urls:
        raise ParseError("这条微博里没有视频或图片")
    return result("weibo", st.get("bid") or mid, title, author, "images", urls, urls[0], ref)


def _weibo_tv(fid):
    get("https://weibo.com/", DESK_UA)  # 拿访客 cookie
    r = S.post("https://weibo.com/tv/api/component", params={"page": f"/tv/show/{fid}"},
               data={"data": json.dumps({"Component_Play_Playinfo": {"oid": fid}})},
               headers={"User-Agent": DESK_UA, "Referer": f"https://weibo.com/tv/show/{fid}"}, timeout=15)
    try:
        info = r.json()["data"]["Component_Play_Playinfo"]
    except (ValueError, KeyError, TypeError):
        raise ParseError("微博视频页需要登录，请改用这条视频所在微博的链接")
    urls = info.get("urls") or {}
    vurl = next(iter(urls.values()), None) if urls else info.get("stream_url")
    if vurl and vurl.startswith("//"):
        vurl = "https:" + vurl
    return result("weibo", fid.replace(":", "_"), info.get("title"), info.get("author"), "video",
                  [vurl] if vurl else [], info.get("cover_image", ""), "https://weibo.com/")


# ---------------- TikTok ----------------
def parse_tiktok(url):
    r = get(url, DESK_UA, timeout=20)
    final, html = r.url, r.text
    m = re.search(r"/(?:video|photo)/(\d+)", final)
    if not m:
        raise ParseError("TikTok 链接里没找到视频 id：" + final)
    vid = m.group(1)
    if f"/video/{vid}" not in final and f"/photo/{vid}" not in final:
        html = get(f"https://www.tiktok.com/@_/video/{vid}", DESK_UA, timeout=20).text
    data = _json_after(html, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
    item = _walk(data, "itemStruct") if data else None
    if not item:  # 被 WAF 的 JS 挑战拦了 → 用浏览器过一遍
        final, html = browser_get(final,
                                  wait_for="!!document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')")
        data = _json_after(html, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
        item = _walk(data, "itemStruct") if data else None
    if not item:
        raise ParseError("TikTok 页面解析失败（需要能访问 TikTok 的网络，或作品已删除/地区限制）")
    author = (item.get("author") or {}).get("uniqueId", "")
    ref = "https://www.tiktok.com/"
    ip = item.get("imagePost") or {}
    if ip.get("images"):
        urls = [im["imageURL"]["urlList"][0] for im in ip["images"] if im.get("imageURL")]
        return result("tiktok", vid, item.get("desc"), author, "images", urls, urls[0] if urls else "", ref)
    v = item.get("video") or {}
    # playAddr 是播放器用的无水印流；downloadAddr 带水印，不用
    br = sorted(v.get("bitrateInfo") or [], key=lambda b: b.get("Bitrate", 0), reverse=True)
    vurl = None
    if br:
        vurl = ((br[0].get("PlayAddr") or {}).get("UrlList") or [None])[0]
    vurl = vurl or v.get("playAddr")
    return result("tiktok", vid, item.get("desc"), author, "video", [vurl] if vurl else [], v.get("cover", ""), ref)


# ---------------- X / YouTube / Instagram ----------------
# 这三家本身不加水印。X 用 fxtwitter 公开 API 直拿原 mp4/原图；
# YouTube 和 Instagram 的视频是音视频分离的 DASH，交给 yt-dlp 下载并用 ffmpeg 合并。
YTDLP = "ytdlp:"  # urls 里以此开头的条目由 yt-dlp 下载


def _proxy():
    import urllib.request
    p = urllib.request.getproxies()
    return p.get("https") or p.get("http")


def _ytdlp_info(url):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "proxy": _proxy(), "ignore_no_formats_error": True,
            "noplaylist": True, "socket_timeout": 20}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            return y.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
        if "login" in msg.lower() or "empty media" in msg or "cookies" in msg:
            raise ParseError("这条内容需要登录才能看（私密账号 / 限制级），暂时下不了")
        raise ParseError("解析失败：" + msg[:160])


def _first_line(s, n=60):
    s = (s or "").strip().split("\n")[0]
    return re.sub(r"https?://\S+", "", s).strip()[:n]


def parse_x(url):
    if "t.co/" in url:
        url = get(url).url
    m = re.search(r"/status(?:es)?/(\d+)", url)
    if not m:
        raise ParseError("没认出推文 ID，请复制推文的分享链接（x.com/用户名/status/数字）")
    tid = m.group(1)
    r = get(f"https://api.fxtwitter.com/status/{tid}")
    if r.status_code == 404:
        raise ParseError("推文不存在、已删除或是私密账号")
    t = (r.json() or {}).get("tweet") or {}
    media = (t.get("media") or {}).get("all") or []
    urls, kinds = [], set()
    for mm in media:
        if mm.get("type") == "photo":
            u = re.sub(r"[?&]name=\w+", "", mm["url"])
            urls.append(u + ("&" if "?" in u else "?") + "name=orig")
            kinds.add("image")
        else:  # video / gif：取码率最高的 mp4
            fs = [f for f in mm.get("formats") or mm.get("variants") or []
                  if f.get("container") == "mp4" or f.get("content_type") == "video/mp4"]
            best = max(fs, key=lambda f: f.get("bitrate") or 0)["url"] if fs else mm.get("url")
            urls.append(best)
            kinds.add("video")
    if not urls:
        raise ParseError("这条推文里没有视频或图片")
    author = (t.get("author") or {}).get("screen_name", "")
    return result("x", tid, _first_line(t.get("text")), author,
                  "video" if kinds == {"video"} else "image" if kinds == {"image"} else "mixed", urls,
                  referer="https://x.com/")


def parse_youtube(url):
    i = _ytdlp_info(url)
    if not i.get("formats"):
        raise ParseError("没拿到视频格式（可能是直播、会员或地区限制视频）")
    return result("youtube", i["id"], i.get("title"), i.get("uploader") or i.get("channel"), "video",
                  [YTDLP + i.get("webpage_url", url)], cover=i.get("thumbnail", ""))


def parse_instagram(url):
    i = _ytdlp_info(url)
    entries = i.get("entries") or [i]
    urls, kinds = [], set()
    for e in entries:
        if e.get("formats"):
            urls.append(YTDLP + (e.get("webpage_url") or url) + "#" + str(e.get("id", "")))
            kinds.add("video")
        elif e.get("thumbnail"):  # 图片帖：yt-dlp 把原图放在 thumbnail（最大一张）
            urls.append(e["thumbnail"])
            kinds.add("image")
    author = i.get("channel") or i.get("uploader_id") or i.get("uploader") or ""
    title = _first_line(i.get("description")) or _first_line(entries[0].get("description"))
    rid = re.search(r"/(?:p|reel|reels|tv)/([\w-]+)", url)
    return result("instagram", rid.group(1) if rid else i.get("id"), title, author,
                  "video" if kinds == {"video"} else "image" if kinds == {"image"} else "mixed", urls,
                  referer="https://www.instagram.com/")


def _ytdlp_download(page_url, out_base, progress, i, n):
    """yt-dlp 下载 + 合并；把它的分段进度换算成本条目的 0~1。返回最终文件路径。"""
    import yt_dlp
    page_url, _, entry_id = page_url.partition("#")
    state = {"part": 0, "parts": 1, "last": None, "final": None}

    def hook(d):
        if d.get("filename") != state["last"]:
            if state["last"] is not None:
                state["part"] += 1
            state["last"] = d.get("filename")
        if d["status"] in ("downloading", "finished") and progress:
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            sub = (done / total) if total else 0
            parts = max(state["parts"], state["part"] + 1)
            frac = min((state["part"] + sub) / parts, 0.99)  # 留 1% 给合并
            label = ("视频" if state["part"] == 0 else "音频") if parts > 1 else ""
            progress(i, n, done, total, frac=frac, note=label)

    def pp_hook(d):
        if d["status"] == "started" and d.get("postprocessor") == "Merger" and progress:
            progress(i, n, 0, 0, frac=0.995, note="合并音视频")
        if d["status"] == "finished":
            state["final"] = d.get("info_dict", {}).get("filepath") or state["final"]

    opts = {"quiet": True, "no_warnings": True, "proxy": _proxy(), "noprogress": True,
            "outtmpl": out_base + ".%(ext)s", "merge_output_format": "mp4", "noplaylist": True,
            "format": "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba/bv*+ba/b",
            "progress_hooks": [hook], "postprocessor_hooks": [pp_hook], "socket_timeout": 30}
    with yt_dlp.YoutubeDL(opts) as y:
        try:
            # 先选好格式（知道是 1 段还是 视频+音频 2 段，进度条才不会倒退），再下载
            info = y.extract_info(page_url, download=False)
            entries = info.get("entries") or [info]
            entry = next((e for e in entries if e and str(e.get("id")) == entry_id), None) if entry_id else None
            entry = entry or next((e for e in entries if e and e.get("formats")), entries[0])
            state["parts"] = len(entry.get("requested_formats") or [1])
            y.process_info(entry)
        except yt_dlp.utils.DownloadError as e:
            raise ParseError("下载失败：" + re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")[:160])
    path = state["final"]
    for rd in entry.get("requested_downloads") or []:
        path = rd.get("filepath") or path
    if not path or not os.path.exists(path):
        raise ParseError("下载完成但没找到文件")
    return path


PLATFORMS = [
    (("douyin.com", "iesdouyin.com"), parse_douyin),
    (("xhslink.com", "xiaohongshu.com"), parse_xhs),
    (("kuaishou.com", "gifshow.com", "chenzhongtech.com", "kwai"), parse_kuaishou),
    (("bilibili.com", "b23.tv", "bili2233.cn"), parse_bilibili),
    (("weibo.com", "weibo.cn"), parse_weibo),
    (("tiktok.com",), parse_tiktok),
    (("youtube.com", "youtu.be"), parse_youtube),
    (("instagram.com", "instagr.am"), parse_instagram),
    (("x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com"), parse_x),
]


def parse(text):
    url = find_url(text)
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if host == "t.co":
        return parse_x(url)
    for keys, fn in PLATFORMS:
        if any(k in host for k in keys):
            return fn(url)
    raise ParseError("暂不支持这个平台（支持：抖音、小红书、快手、B站、微博、TikTok、X、YouTube、Instagram）")


def download(info, out_dir, progress=None):
    os.makedirs(out_dir, exist_ok=True)
    ua = MOBILE_UA if info["platform"] in ("douyin", "kuaishou", "weibo") else DESK_UA
    title = "" if info["title"] == info["id"] else safe_name(info["title"]) + "_"
    base = f'{info["platform"]}_{title}{info["id"]}'
    saved = []
    n = len(info["urls"])
    for i, u in enumerate(info["urls"]):
        suffix = f"_{i + 1:02d}" if n > 1 else ""
        if u.startswith(YTDLP):
            path = _ytdlp_download(u[len(YTDLP):], os.path.join(out_dir, base + suffix), progress, i, n)
            saved.append(path)
            continue
        r = S.get(u, headers={"User-Agent": ua, "Referer": info.get("referer", "")},
                  stream=True, timeout=60, allow_redirects=True)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "")
        if "video" in ct or (info["type"] == "video" and "image" not in ct):
            ext = ".mp4"
        else:
            ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
        name = base + suffix + ext
        path = os.path.join(out_dir, name)
        if os.path.exists(path):
            path = path[:-len(ext)] + time.strftime("_%H%M%S") + ext
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        if progress:
            progress(i, len(info["urls"]), 0, total)
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(i, len(info["urls"]), done, total)
        if os.path.getsize(path) < 2048:
            os.remove(path)
            raise ParseError("下载到的文件是空的（地址失效或被平台拦截），请重新复制链接再试")
        saved.append(path)
    return saved
