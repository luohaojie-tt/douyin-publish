# -*- coding: utf-8 -*-
"""
douyin_publish.py — 抖音图文自动发布(半自动:脚本上传+填文本+预览,人工确认后提交)

用法(在 venv python 下运行,输出重定向到文件后 Read):
  python douyin_publish.py 20260922            # 上传图片 + 填标题/简介 + 预览截图
  python douyin_publish.py 20260922 --post     # 人工确认后,真正点击『发布』

前提:
  1. 双击 start_douyin_browser.bat 启动的自动化 Chrome 已运行(CDP 端口 9224)
  2. 该窗口里已登录抖音创作者平台(登录态存独立 profile,仅首次需扫码)
  3. 图片已生成:reports/douyin/YYYYMMDD/0*.png(make_douyin_images.py 产出)

设计:
  - 上传走 CDP DOM.setFileInputFiles(对 file input 直设文件,不经系统对话框)
  - 页面 DOM 不确定处全部落诊断(reports/douyin/YYYYMMDD/_publish_diag.json),
    失败时先读诊断再修锚点,不要盲猜
  - React 受控输入用 native value setter + input 事件;contenteditable 用 execCommand
  - 遇滑块/验证码:脚本会截图并退出,人工在窗口里过掉后重跑
"""
import base64
import glob
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

import websocket

DEBUG_PORT = 9224
BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
TITLE_MAX = 20  # 抖音图文标题上限实测 20 字符(页面提示"标题内容仅支持20个字符"),留 1 字余量
DIAG_NAME = "_publish_diag.json"

# 内容合规改造(2026-09-23 限流反馈后):
# 1) 标题/简介不再出现个股名与"明日关注:XXX"——荐股红线
# 2) 标题池按情绪分风格、按日期确定性轮换,打破"每日同模板"画像
# 3) 话题标签按星期轮换,不再固定双标签
# 4) 简介改为自然语言 + 操作纪律话术(纪律不荐股)
TITLE_POOL = {
    "退潮": ["今天的市场,给追高的人上了课",
             "高度板没了,接下来怎么看",
             "盘面冷下来了,这些信号要记住"],
    "降温": ["盘面又冷了一点,明天会更难吗",
             "钱变谨慎了,今天的数据说明一切"],
    "分歧": ["有人赚钱有人亏,今天盘面很诚实",
             "方向没变,节奏变了"],
    "升温": ["盘面回暖,今天的数据值得看一眼"],
    "加速": ["情绪在加速,热闹背后要想清楚"],
    "冰点": ["盘面冷到冰点,反而是观察窗口"],
    "回暖": ["止跌回升,今天的盘面有点意思"],
}
STRATEGY = {
    "退潮": "退潮期纪律:控制仓位、少出手,等情绪止跌信号",
    "降温": "降温期纪律:只看高位股承接,不追不抢",
    "分歧": "分歧期纪律:方向未明,轻仓试错快进快出",
    "升温": "升温期纪律:跟随主线,注意节奏",
    "加速": "加速期纪律:警惕情绪顶部,兑现为主",
    "冰点": "冰点期纪律:观察止跌信号,备好名单等回暖",
    "回暖": "回暖期纪律:关注修复主线,逐步参与",
}
TOPICS_BY_WD = {
    0: "#A股日记 #股市观察", 1: "#盘面手记 #交易日记", 2: "#股市观察 #复盘日记",
    3: "#交易日记 #盘面观察", 4: "#复盘日记 #A股观察", 5: "#盘面手记 #股市观察",
    6: "#交易日记 #复盘日记",
}

sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------- CDP
def list_pages():
    r = urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=5)
    return json.loads(r.read())


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self._id = 0

    def call(self, method, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval_js(self, expr, await_promise=False):
        params = {"expression": expr, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        r = self.call("Runtime.evaluate", **params)
        res = r.get("result", {})
        if res.get("subtype") == "error":
            raise RuntimeError("JS error: " + str(res.get("description"))[:300])
        return res.get("value")

    def screenshot(self, path):
        r = self.call("Page.captureScreenshot", format="png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))

    def close(self):
        self.ws.close()


def find_creator_page(cdp_pages):
    pages = [t for t in cdp_pages if t.get("type") == "page"]
    for t in pages:
        if "creator.douyin.com" in (t.get("url") or ""):
            return t
    return pages[0] if pages else None


# ---------------------------------------------------------------- 文本生成
def first_sentence(s, maxlen):
    if not s:
        return ""
    s = s.replace(";;", "。")
    for sep in ["。", ";;", ";"]:
        idx = s.find(sep)
        if idx > 0:
            s = s[:idx]
            break
    return s if len(s) <= maxlen else s[:maxlen].rstrip(",、,; ") + "…"


def clip(s, n):
    return s if len(s) <= n else s[:n].rstrip(",、,; ") + "…"


def gen_title(d):
    """按情绪选风格池、按日期确定性轮换——每天结构不同,无个股、无模板词。"""
    date = d.get("date", "")
    s = d.get("sentiment", "")
    pool = TITLE_POOL.get(s) or [f"{s}的一天,盘面手记"]
    idx = int(date.replace("-", "")) % len(pool)
    t = pool[idx]
    return t if len(t) <= TITLE_MAX - 1 else clip(t, TITLE_MAX)


def nv(v):
    """None 安全显示"""
    return "-" if v is None else v


def pct_str(v, sign=True):
    """None/非法值安全百分比"""
    try:
        v = float(v)
        return f"{'+' if sign and v >= 0 else ''}{v:.2f}%"
    except (TypeError, ValueError):
        return "-"


def gen_desc(d):
    """自然语言简介:数据+情绪+纪律话术。无个股名、无荐股指向、话题按星期轮换。"""
    date = d.get("date", "")
    try:
        wd = datetime.strptime(date, "%Y-%m-%d").weekday()
    except ValueError:
        wd = 0
    md = f"{int(date[5:7])}/{int(date[8:10])}" if len(date) >= 10 else date
    s = d.get("sentiment", "")
    pred = d.get("next_pred", "")
    note = first_sentence(d.get("sentiment_note", ""), 46)
    strategy = STRATEGY.get(s, "按情绪周期纪律执行")
    topics = TOPICS_BY_WD.get(wd, "#股市观察 #交易日记")
    mx, ms = nv(d.get("max_streak_name")), nv(d.get("max_streak"))
    lines = [
        f"{md} 盘面手记 | 情绪:{s},明日预判:{pred}",
        f"今天涨停 {nv(d.get('limit_up'))} 家、炸板率 {nv(d.get('broken_rate'))}%,"
        f"最高板回到 {ms} 板({mx}),大面 {nv(d.get('big_face_count'))} 只。",
        note,
        strategy,
        "个人复盘记录,不构成投资建议。",
        topics,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 页面操作
def dump_form(cdp):
    """把表单元素/按钮 dump 下来做诊断与锚点修正。"""
    return cdp.eval_js(
        "(function(){ var out={fields:[],buttons:[]};"
        " document.querySelectorAll('input,textarea,div[contenteditable=\\'true\\']')"
        ".forEach(function(e){ out.fields.push({tag:e.tagName,"
        " type:e.type||'',ph:e.placeholder||e.getAttribute('placeholder')||'',"
        " cls:(''+(e.className||'')).slice(0,90),ed:e.isContentEditable===true,"
        " vis:!!(e.offsetParent||e.isContentEditable)});});"
        " document.querySelectorAll('button').forEach(function(e){"
        " var t=(e.innerText||'').trim(); if(t) out.buttons.push({t:t.slice(0,16),dis:e.disabled===true});});"
        " out.url=location.href; out.title=document.title;"
        " out.imgs=document.images.length;"
        " out.bodyText=(document.body.innerText||'').slice(0,600);"
        " return out; })()"
    )


def switch_to_imagetext(cdp):
    """确认在图文上传模式:优先 URL 直达,否则点『图文』tab。"""
    url = cdp.eval_js("location.href")
    if "creator.douyin.com" not in url:
        cdp.call("Page.enable")
        cdp.call("Page.navigate", url=UPLOAD_URL)
        time.sleep(5)
        url = cdp.eval_js("location.href")
    if "content/upload" not in url:
        cdp.call("Page.navigate", url=UPLOAD_URL)
        time.sleep(5)
    # 登录检测
    info = dump_form(cdp)
    body = info.get("bodyText", "") or ""
    if "login" in cdp.eval_js("location.href") or "扫码登录" in body or "二维码登录" in body:
        return {"login_required": True, "url": info.get("url")}
    r = cdp.eval_js(
        "(function(){ var best=null;"
        " document.querySelectorAll('div,span,li,button,a,label').forEach(function(e){"
        "  var t=(e.innerText||'').trim();"
        "  if((t==='图文'||t==='发布图文')&&e.children.length<=2){"
        "   if(!best||t.length<best._t) best=e; }});"
        " if(!best) return 'NO_TAB';"
        " best.click(); return 'CLICKED'; })()"
    )
    if r == "CLICKED":
        time.sleep(2)
    return {"login_required": False, "tab": r}


def find_file_inputs(cdp):
    root = cdp.call("DOM.getDocument")
    nid = root["root"]["nodeId"]
    r = cdp.call("DOM.querySelectorAll", nodeId=nid, selector="input[type=file]")
    infos = []
    for n2 in r.get("nodeIds", []):
        try:
            node = cdp.call("DOM.describeNode", nodeId=n2)["node"]
            attrs = node.get("attributes", [])
            ad = {attrs[i]: attrs[i + 1] for i in range(0, len(attrs) - 1, 2)}
            infos.append({"nodeId": n2, "accept": ad.get("accept", ""),
                          "multiple": "multiple" in attrs, "name": ad.get("name", "")})
        except Exception as ex:
            infos.append({"nodeId": n2, "err": str(ex)[:120]})
    return infos


def upload_images(cdp, paths):
    # 防重复上传:页面已提示"已添加N张图片"时跳过(可用 --force 清空重传)
    added = cdp.eval_js(
        "(function(){ var m=(document.body.innerText||'').match(/已添加(\\d+)张图片/);"
        " return m?parseInt(m[1]):-1; })()"
    )
    if isinstance(added, (int, float)) and added > 0:
        return {"ok": True, "skipped": True, "already": int(added)}
    imgs_before = cdp.eval_js("document.images.length") or 0
    fis = find_file_inputs(cdp)
    target = None
    for f in fis:
        if "image" in (f.get("accept") or ""):
            target = f
            break
    if target is None:
        for f in fis:
            if f.get("multiple"):
                target = f
                break
    if target is None and fis:
        target = fis[-1]
    if target is None:
        return {"ok": False, "reason": "NO_FILE_INPUT", "file_inputs": fis}
    cdp.call("DOM.setFileInputFiles", files=[os.path.abspath(p) for p in paths],
             nodeId=target["nodeId"])
    # 轮询图片节点增量(仅诊断,最终以预览截图为准)
    last, stable = -1, 0
    for _ in range(20):
        time.sleep(2)
        n = cdp.eval_js("document.images.length") or 0
        if n == last and n > imgs_before:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = n
    return {"ok": True, "file_inputs": fis, "used_node": target,
            "imgs_before": imgs_before, "imgs_after": last if last >= 0 else imgs_before}


def set_text(cdp, kind, value):
    """kind: 'title' | 'desc'。返回诊断字符串。"""
    v = json.dumps(value, ensure_ascii=False)
    if kind == "title":
        js = (
            "(function(){ var v=" + v + ";"
            " var sels=['input[placeholder*=\"标题\"]','input[maxlength]','.title-input input','#title'];"
            " for(var i=0;i<sels.length;i++){ var els=document.querySelectorAll(sels[i]);"
            "  for(var j=0;j<els.length;j++){ var e=els[j];"
            "   if(!e.offsetParent) continue;"
            "   var st=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
            "   st.call(e,v);"
            "   e.dispatchEvent(new Event('input',{bubbles:true}));"
            "   e.dispatchEvent(new Event('change',{bubbles:true}));"
            "   return 'OK:'+sels[i]; }}"
            # 标题也可能是 contenteditable
            " var ceds=document.querySelectorAll('div[contenteditable=\\'true\\']');"
            " for(var k=0;k<ceds.length;k++){ var c=ceds[k];"
            "  if(!c.offsetParent) continue;"
            "  c.focus(); document.execCommand('selectAll',false,null);"
            "  document.execCommand('insertText',false,v);"
            "  return 'OK:contenteditable#'+k; }"
            " return 'NO_MATCH'; })()"
        )
    else:
        # 定位简介框并存全局引用
        loc = cdp.eval_js(
            "(function(){ var pri=document.querySelectorAll("
            "  'textarea[placeholder*=\"简介\"],textarea[placeholder*=\"更多\"],"
            "div[contenteditable=\\'true\\']');"
            " var cand=null;"
            " for(var i=0;i<pri.length;i++){ var e=pri[i];"
            "  var ph=e.placeholder||e.getAttribute('placeholder')||'';"
            "  var bb=e.getBoundingClientRect();"
            "  if(bb.width<2||bb.height<2) continue;"
            "  if(e.tagName==='TEXTAREA'||/简介|更多|描述|输入/.test(ph)){ cand=e; break; }"
            "  if(!cand&&e.isContentEditable) cand=e; }"
            " if(!cand) return 'NO_MATCH';"
            " window.__descEl=cand; cand.focus(); return 'FOUND'; })()"
        )
        if loc != "FOUND":
            return loc
        # 强力清空:execCommand selectAll+delete 循环,Ctrl+A/Delete 真实按键兜底
        for _ in range(3):
            n = cdp.eval_js(
                "(function(){ var e=window.__descEl; e.focus();"
                " document.execCommand('selectAll',false,null);"
                " document.execCommand('delete',false,null);"
                " return (e.innerText||'').trim().length; })()"
            )
            if not n:
                break
            cdp.call("Input.dispatchKeyEvent", type="keyDown", modifiers=2,
                     key="a", code="KeyA", windowsVirtualKeyCode=65)
            cdp.call("Input.dispatchKeyEvent", type="keyUp", modifiers=2,
                     key="a", code="KeyA", windowsVirtualKeyCode=65)
            cdp.call("Input.dispatchKeyEvent", type="keyDown", key="Delete",
                     code="Delete", windowsVirtualKeyCode=46)
            cdp.call("Input.dispatchKeyEvent", type="keyUp", key="Delete",
                     code="Delete", windowsVirtualKeyCode=46)
            time.sleep(0.5)
        leftover = cdp.eval_js("(window.__descEl.innerText||'').trim().length")
        if leftover:
            return "CANT_CLEAR:" + str(leftover)
        # 逐行 insertHTML(行间 <br>)
        v = json.dumps(value, ensure_ascii=False)
        r = cdp.eval_js(
            "(function(){ var v=" + v + ";"
            " var e=window.__descEl; e.focus();"
            " var lines=v.split('\\n');"
            " var html=lines.map(function(l){"
            "  return l.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');"
            " }).join('<br>');"
            " document.execCommand('insertHTML',false,html);"
            " return 'OK:'+(e.innerText||'').length; })()"
        )
        return r
    return cdp.eval_js(js)


def click_publish(cdp):
    return cdp.eval_js(
        "(function(){ var btns=document.querySelectorAll('button');"
        " for(var i=0;i<btns.length;i++){ var t=(btns[i].innerText||'').replace(/\\s+/g,'');"
        "  if(t==='发布'||t==='發布'){"
        "   if(btns[i].disabled) return 'DISABLED';"
        "   btns[i].scrollIntoView({block:'center'});"
        "   btns[i].click(); return 'CLICKED'; }}"
        " return 'NOT_FOUND'; })()"
    )


# ---------------------------------------------------------------- 音乐
MUSIC_ROW_JS = (
    "(function(){ var rows=[], re=/\\d{1,2}:\\d{2}/;"
    " document.querySelectorAll('div,li,section').forEach(function(e){"
    "  var bb=e.getBoundingClientRect();"
    "  if(bb.width<2||bb.height<2) return;"
    "  var t=(e.innerText||'');"
    "  if(re.test(t)&&t.length<120&&e.children.length>=1&&e.children.length<12){"
    "   var dup=false; for(var i=0;i<rows.length;i++){"
    "    if(rows[i]._el&&(rows[i]._el.contains(e)||e.contains(rows[i]._el))){"
    "     if(t.length>rows[i]._el.innerText.length)"
    "      rows[i]={_el:e,t:t.replace(/\\n+/g,' | ').slice(0,80)};"
    "     dup=true; break; } }"
    "   if(!dup) rows.push({_el:e,t:t.replace(/\\n+/g,' | ').slice(0,80)}); } });"
    " var out=[]; for(var i=0;i<rows.length&&i<15;i++)"
    "  out.push({i:i,t:rows[i].t});"
    " window.__musicRows=rows;"
    " return out; })()"
)


def real_click_candidates(cdp):
    """对 window.__musicCands 逐个做 CDP 真实鼠标点击,直到面板弹出(页面文本量大增)。"""
    bl = cdp.eval_js("document.body.innerText.length") or 0
    vp = cdp.eval_js("({vw:window.innerWidth, vh:window.innerHeight})") or {}
    tries = [f"viewport:{vp.get('vw')}x{vp.get('vh')}"]
    for i in range(5):
        r = cdp.eval_js(
            "(function(){ var c=window.__musicCands||[]; var idx=" + str(i) + ";"
            " if(idx>=c.length) return null;"
            " var e=c[idx]; e.scrollIntoView({block:'center'});"
            " var b=e.getBoundingClientRect();"
            " return {x:Math.round(b.left+b.width/2), y:Math.round(b.top+b.height/2),"
            " w:Math.round(b.width), h:Math.round(b.height),"
            " t:(e.innerText||'').trim().slice(0,16)}; })()"
        )
        if not r:
            tries.append(f"#{i}:NO_MORE")
            break
        cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=r["x"], y=r["y"])
        time.sleep(0.3)
        for t in ("mousePressed", "mouseReleased"):
            cdp.call("Input.dispatchMouseEvent", type=t, x=r["x"], y=r["y"],
                     button="left", clickCount=1)
        time.sleep(2)
        np_ = cdp.eval_js("document.body.innerText.length") or 0
        tries.append(f"#{i}:{r['t']}@({r['x']},{r['y']}) {r['w']}x{r['h']} +{np_ - bl}")
        if np_ - bl > 400:
            return {"opened": True, "tries": tries, "base_len": bl}
    return {"opened": False, "tries": tries, "base_len": bl}


def has_music_panel(cdp):
    # 面板关闭时 React 保留隐藏 DOM(visibility/keep-alive),rect 仍有尺寸,
    # 唯一诚实判据:elementFromPoint 命中搜索框自身或其内部
    return cdp.eval_js(
        "(function(){ var ips=document.querySelectorAll('input');"
        " for(var i=0;i<ips.length;i++){"
        "  if((ips[i].placeholder||'')==='搜索音乐'){"
        "   var b=ips[i].getBoundingClientRect();"
        "   if(b.width>1&&b.height>1){"
        "    var el=document.elementFromPoint(b.left+b.width/2, b.top+b.height/2);"
        "    if(el&&(el===ips[i]||ips[i].contains(el)||el.contains(ips[i]))) return 'Y';"
        "   } } } return 'N'; })()"
    ) == "Y"


def open_music_panel(cdp):
    """打开音乐面板;兼容『选择音乐』与『修改音乐』两种按钮文本。"""
    if has_music_panel(cdp):
        return {"opened": True, "how": "already"}
    cdp.eval_js(
        "(function(){ var cands=[];"
        " document.querySelectorAll('div,span,button,section,p').forEach(function(e){"
        "  var t=(e.innerText||'').trim();"
        "  if((t.indexOf('选择音乐')>=0||t.indexOf('修改音乐')>=0)&&t.length<20)"
        "   cands.push(e); });"
        " window.__musicCands=cands; })()"
    )
    n = cdp.eval_js("(window.__musicCands||[]).length") or 0
    for i in range(min(n, 6)):
        cdp.eval_js(
            "(function(){ var c=window.__musicCands; c[" + str(i) + "].click();"
            " return 'ok'; })()"
        )
        time.sleep(1.5)
        if has_music_panel(cdp):
            return {"opened": True, "how": f"jsclick#{i}"}
    for i in range(min(n, 6)):
        r = cdp.eval_js(
            "(function(){ var c=window.__musicCands||[]; var idx=" + str(i) + ";"
            " if(idx>=c.length) return null;"
            " var e=c[idx]; e.scrollIntoView({block:'center'});"
            " var b=e.getBoundingClientRect();"
            " return {x:Math.round(b.left+b.width/2), y:Math.round(b.top+b.height/2)}; })()"
        )
        if not r:
            break
        cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=r["x"], y=r["y"])
        for t in ("mousePressed", "mouseReleased"):
            cdp.call("Input.dispatchMouseEvent", type=t, x=r["x"], y=r["y"],
                     button="left", clickCount=1)
        time.sleep(1.5)
        if has_music_panel(cdp):
            return {"opened": True, "how": f"real#{i}"}
    return {"opened": False, "cands": n}


def fill_music_search(cdp, keyword):
    """在音乐面板搜索框填关键词(先清空)。"""
    kw = json.dumps(keyword, ensure_ascii=False)
    r = cdp.eval_js(
        "(function(){ var v=" + kw + ";"
        " var ips=document.querySelectorAll('input,textarea');"
        " var cand=null;"
        " for(var i=0;i<ips.length;i++){ var e=ips[i];"
        "  var ph=e.placeholder||e.getAttribute('placeholder')||'';"
        "  var bb=e.getBoundingClientRect();"
        "  if(bb.width<2||bb.height<2||e.type==='checkbox') continue;"
        "  if(ph==='搜索音乐'||/搜索|song|music/i.test(ph)||e.type==='search'){"
        "   cand=e; break; } }"
        " if(!cand) return 'NO_SEARCH_INPUT';"
        " var proto=cand.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype"
        "                                   :window.HTMLInputElement.prototype;"
        " var st=Object.getOwnPropertyDescriptor(proto,'value').set;"
        " st.call(cand,''); cand.dispatchEvent(new Event('input',{bubbles:true}));"
        " st.call(cand,v);"
        " cand.dispatchEvent(new Event('input',{bubbles:true}));"
        " cand.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true,key:'Enter'}));"
        " cand.focus(); return 'OK'; })()"
    )
    time.sleep(3.5)
    return r


def fill_music_search_human(cdp, keyword):
    """逐字符拟人输入(修复搜索状态紊乱/空态)。"""
    kw = json.dumps(keyword, ensure_ascii=False)
    return cdp.eval_js(
        "(async function(){ var v=" + kw + ";"
        " var cand=null;"
        " document.querySelectorAll('input').forEach(function(x){"
        "  if((x.placeholder||'')==='搜索音乐') cand=x; });"
        " if(!cand) return 'NO_SEARCH_INPUT';"
        " var st=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
        " cand.focus();"
        " st.call(cand,''); cand.dispatchEvent(new Event('input',{bubbles:true}));"
        " await new Promise(function(r){ setTimeout(r,350); });"
        " for(var i=0;i<v.length;i++){"
        "  st.call(cand, v.slice(0,i+1));"
        "  cand.dispatchEvent(new Event('input',{bubbles:true}));"
        "  await new Promise(function(r){ setTimeout(r,200); }); }"
        " cand.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true,key:'Enter'}));"
        " return 'OK'; })()",
        await_promise=True,
    )


def music_search(cdp, keyword):
    """打开音乐面板并搜索(面板已开则直接搜)。返回 dict。"""
    out = {}
    op = open_music_panel(cdp)
    out["open"] = json.dumps(op, ensure_ascii=False)
    if not op["opened"]:
        out["inputs_on_page"] = cdp.eval_js(
            "(function(){ var a=[];"
            " document.querySelectorAll('input,textarea').forEach(function(e){"
            "  if(e.offsetParent) a.push(e.placeholder||e.getAttribute('placeholder')||e.type); });"
            " return a; })()"
        )
        return out
    out["search"] = fill_music_search(cdp, keyword)
    out["items"] = cdp.eval_js(MUSIC_ROW_JS)
    return out


def music_pick(cdp, ref):
    """按曲名片段(或序号)选中歌曲;面板自动打开,选中后不自动确认。"""
    op = open_music_panel(cdp)
    if not op["opened"]:
        return "PANEL_FAIL:" + json.dumps(op, ensure_ascii=False)
    # 搜索词:取括号前主名("清明上河图(同花顺进行曲)"->"清明上河图");纯序号搜"同花顺"
    if isinstance(ref, str) and not ref.isdigit():
        kw = ref.split("(")[0]
    else:
        kw = "同花顺"
    # 搜索框已是目标词则不重填(反复清空重填会打断虚拟列表渲染)
    cur = cdp.eval_js(
        "(function(){ var e=null;"
        " document.querySelectorAll('input').forEach(function(x){"
        "  if((x.placeholder||'')==='搜索音乐') e=x; });"
        " return e?(e.value||''):''; })()"
    )
    if cur != kw:
        sr = fill_music_search(cdp, kw)
        if sr != "OK":
            return "SEARCH_FAIL:" + str(sr)

    def wait_rows(sec_each, times):
        n = 0
        for _ in range(times):
            cdp.eval_js(MUSIC_ROW_JS)
            n = cdp.eval_js("(window.__musicRows||[]).length") or 0
            if n >= 5:
                break
            time.sleep(sec_each)
        return n

    n = wait_rows(1.5, 6)
    if n < 5:
        # 列表空态:逐字符重打关键词强制重建搜索
        sr = fill_music_search_human(cdp, kw)
        if sr != "OK":
            return "SEARCH_FAIL:" + str(sr)
        n = wait_rows(2, 8)
    if n < 5 and kw != "同花顺":
        # 仍空:换"同花顺"(该词结果稳定),稍后滚动列表找目标
        fill_music_search_human(cdp, "同花顺")
        n = wait_rows(2, 8)
    if n < 2:
        return "NO_ROWS:" + str(n)

    # 面板列表滚动锚点(搜索框下方区域)
    cdp.eval_js(
        "(function(){ var e=null;"
        " document.querySelectorAll('input').forEach(function(x){"
        "  if((x.placeholder||'')==='搜索音乐') e=x; });"
        " if(e){ var b=e.getBoundingClientRect();"
        "  window.__panelPt={x:Math.round(b.left+b.width/2),"
        "                    y:Math.min(Math.round(b.bottom+260), window.innerHeight-60)}; } })()"
    )
    refjs = json.dumps(ref if isinstance(ref, str) else int(ref), ensure_ascii=False)
    match_js = (
        "(function(){ var rows=window.__musicRows||[]; var ref=" + refjs + ";"
        " var best=null;"
        " for(var i=0;i<rows.length;i++){ var e=rows[i]._el; if(!e) continue;"
        "  var t=e.innerText||'';"
        "  if(typeof ref==='number'){ if(i===ref){ best=e; break; } }"
        "  else { var first=(t.split('\\n')[0]||'').trim();"
        "   if(first.indexOf(ref)===0){"
        "    if(!best||t.length<best.innerText.length) best=e; } } }"
        " if(!best) return 'NOT_FOUND';"
        " best.scrollIntoView({block:'center'});"
        " var b=best.getBoundingClientRect();"
        " window.__pickRect={x:Math.round(b.left+b.width/2),"
        "                    y:Math.round(b.top+b.height/2)};"
        " best.click();"
        " return 'JSCLICK:'+(best.innerText||'').replace(/\\n+/g,' | ').slice(0,50); })()"
    )
    r = cdp.eval_js(match_js)
    scrolls = 0
    while r == "NOT_FOUND" and isinstance(ref, str) and scrolls < 12:
        pt = cdp.eval_js("window.__panelPt||null")
        if not pt:
            break
        cdp.call("Input.dispatchMouseEvent", type="mouseWheel", x=pt["x"], y=pt["y"],
                 deltaX=0, deltaY=500)
        time.sleep(1.2)
        r = cdp.eval_js(match_js)
        scrolls += 1
    if r == "NOT_FOUND":
        return "NOT_FOUND"
    time.sleep(2)
    has_use = cdp.eval_js(
        "(function(){ var bs=document.querySelectorAll('button');"
        " for(var i=0;i<bs.length;i++){"
        "  if((bs[i].innerText||'').trim()==='使用'&&!bs[i].disabled) return 'Y'; }"
        " return 'N'; })()"
    )
    if has_use != "Y":
        rc = cdp.eval_js("window.__pickRect||null")
        if rc:
            cdp.call("Input.dispatchMouseEvent", type="mouseMoved",
                     x=rc["x"], y=rc["y"])
            for t in ("mousePressed", "mouseReleased"):
                cdp.call("Input.dispatchMouseEvent", type=t, x=rc["x"], y=rc["y"],
                         button="left", clickCount=1)
            time.sleep(2)
    return str(r)


def music_confirm(cdp):
    """确认选中的音乐(面板底部/角落的确认按钮)。"""
    return cdp.eval_js(
        "(function(){ var btns=document.querySelectorAll('button');"
        " for(var i=0;i<btns.length;i++){ var t=(btns[i].innerText||'').replace(/\\s+/g,'');"
        "  if(t==='确认'||t==='确定'||t==='完成'||t==='使用'){"
        "   if(btns[i].disabled) continue;"
        "   var b=btns[i].getBoundingClientRect();"
        "   if(b.width<2||b.height<2) continue;"
        "   btns[i].click(); return 'CLICKED:'+t; }}"
        " return 'NO_CONFIRM_BTN'; })()"
    )


# ---------------------------------------------------------------- 主流程
def main():
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print(__doc__)
        sys.exit(1)
    date = sys.argv[1]
    do_post = "--post" in sys.argv
    do_refill = "--refill" in sys.argv  # 图已在页面上,只重填标题/简介
    do_force = "--force" in sys.argv    # 清空重传(配合页面"清空并重新上传"按钮手工操作)

    outdir = OUT_DIR_TMPL = os.path.join(BASE, "reports", "douyin", date)
    img_paths = sorted(glob.glob(os.path.join(outdir, "0*.png")))
    if not img_paths:
        print("图片不存在,先跑 make_douyin_images.py:", outdir)
        sys.exit(1)

    jpath = os.path.join(BASE, "data", f"{date}.json")
    d = json.load(open(jpath, encoding="utf-8"))
    title = gen_title(d)
    desc = gen_desc(d)
    print("标题(%d 字):%s" % (len(title), title))
    print("简介:\n" + desc + "\n")

    try:
        pages = list_pages()
    except Exception:
        print("浏览器不在线:请先双击 start_douyin_browser.bat,并确认已登录抖音")
        sys.exit(1)
    t = find_creator_page(pages)
    if not t:
        print("未找到抖音标签页——请先运行 start_douyin_browser.bat 并登录")
        sys.exit(1)
    cdp = CDP(t["webSocketDebuggerUrl"])
    diag = {"date": date, "imgs": [os.path.basename(p) for p in img_paths]}

    music_kw = None
    if "--music" in sys.argv:
        music_kw = sys.argv[sys.argv.index("--music") + 1]
    music_pick_n = None
    if "--music-pick" in sys.argv:
        v = sys.argv[sys.argv.index("--music-pick") + 1]
        music_pick_n = int(v) if v.isdigit() else v  # 序号或曲名片段

    if music_kw or music_pick_n is not None:
        # 音乐模式:搜索候选 / 选中第 N 项
        if music_pick_n is not None:
            r = music_pick(cdp, music_pick_n)
            print("选中音乐:", r)
            if r.startswith("PANEL_FAIL"):
                print(">>> 音乐面板没打开(这个按钮对自动化免疫)。")
                print(">>> 请你在 Chrome 窗口手动点『修改音乐』,面板弹出后回我一声,")
                print("    我立即接管:搜索→选『同花顺进行曲』→确认→给你过目。")
            else:
                time.sleep(1)
                rc = music_confirm(cdp)
                print("确认按钮:", rc)
        else:
            mr = music_search(cdp, music_kw)
            diag["music"] = mr
            print("候选收集:", mr.get("open"), "| 面板出现:", mr.get("opened"))
            for t in mr.get("tries") or []:
                print("   ", t)
            print("搜索:", mr.get("search"))
            if not mr.get("opened"):
                print(">>> 音乐面板未弹出。页面 input 列表:",
                      json.dumps(mr.get("inputs_on_page"), ensure_ascii=False))
                print(">>> 请人工在窗口点一下『选择音乐』看它长什么样,把现象告诉我")
            items = mr.get("items") or []
            for it in items:
                print("  %2d. %s" % (it["i"], it["t"]))
            if items:
                print(">>> 挑好后运行: python douyin_publish.py %s --music-pick 序号" % date)
        shot = os.path.join(outdir, f"{date}-音乐候选.png")
        cdp.screenshot(shot)
        print("截图:", shot)
    elif do_post:
        url = cdp.eval_js("location.href")
        if "creator.douyin.com" not in url:
            print("当前页面不在 creator.douyin.com,请先跑上传步骤(不带 --post)")
            sys.exit(1)
        info = dump_form(cdp)
        diag["before_post"] = info
        r = click_publish(cdp)
        diag["click"] = r
        print("发布点击:", r)
        if r == "CLICKED":
            time.sleep(8)
            diag["after_url"] = cdp.eval_js("location.href")
            diag["after_title"] = cdp.eval_js("document.title")
            body = cdp.eval_js("(document.body.innerText||'').slice(0,400)")
            diag["after_body"] = body
            print("发布后 URL:", diag["after_url"])
            if "成功" in (body or "") or "发布成功" in (diag["after_title"] or ""):
                print(">>> 发布成功")
            else:
                print(">>> 请人工查看窗口确认是否成功(可能有二次弹窗/审核提示)")
        else:
            print(">>> 未点到发布按钮,读 _publish_diag.json 的 before_post.buttons 修锚点")
        shot = os.path.join(outdir, f"{date}-发布结果.png")
        cdp.screenshot(shot)
        print("结果截图:", shot)
    else:
        if do_refill:
            diag["switch"] = "REFILL_MODE"
            print("refill 模式:跳过上传,只重填标题/简介")
        else:
            sw = switch_to_imagetext(cdp)
            diag["switch"] = sw
            print("进入图文模式:", json.dumps(sw, ensure_ascii=False))
            if sw.get("login_required"):
                shot = os.path.join(outdir, f"{date}-待登录.png")
                cdp.screenshot(shot)
                print(">>> 抖音未登录。请在弹出的 Chrome 窗口里扫码登录,登录后重跑本命令。截图:", shot)
                open(os.path.join(outdir, DIAG_NAME), "w", encoding="utf-8").write(
                    json.dumps(diag, ensure_ascii=False, indent=2))
                sys.exit(2)
            up = upload_images(cdp, [] if do_force else img_paths)
            diag["upload"] = up
            print("上传:", json.dumps({k: v for k, v in up.items() if k != "file_inputs"},
                                      ensure_ascii=False))
            if not up.get("ok"):
                info = dump_form(cdp)
                diag["form_after_fail"] = info
                print(">>> 未找到可用的 file input。诊断已写文件,按 form_after_fail 修锚点。")
            elif up.get("skipped"):
                print(">>> 页面已有 %d 张图片,跳过重复上传(重传请点页面『清空并重新上传』后重跑)" % up["already"])
        r1 = set_text(cdp, "title", title)
        time.sleep(1)
        r2 = set_text(cdp, "desc", desc)
        diag["fill"] = {"title": r1, "desc": r2}
        print("填标题:", r1)
        print("填简介:", r2)
        info = dump_form(cdp)
        diag["form_after_fill"] = info
        shot = os.path.join(outdir, f"{date}-发布预览.png")
        cdp.screenshot(shot)
        print("预览截图:", shot)
        print(">>> 请在浏览器窗口里检查图片顺序/标题/简介。确认后运行:")
        print("    python douyin_publish.py", date, "--post")

    open(os.path.join(outdir, DIAG_NAME), "w", encoding="utf-8").write(
        json.dumps(diag, ensure_ascii=False, indent=2))
    cdp.close()


if __name__ == "__main__":
    main()
