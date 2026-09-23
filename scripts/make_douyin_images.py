# -*- coding: utf-8 -*-
"""
make_douyin_images.py —— 涨停复盘日报 → 抖音图文卡片组(全内容图片化)
用法: python make_douyin_images.py [YYYYMMDD]   (缺省取 data/ 下最新一份)
产出: 复盘工作台/reports/douyin/YYYYMMDD/01_cover.png ~ 07_risk.png (1080x1440 竖版)
      同目录保留同名 .html(调试/复改用)
渲染: 纯本地 HTML + Chrome headless 截图,无外部 CDN、无风控问题。
数据: 只读 data/YYYYMMDD.json,不重新抓数(日报是档案,本脚本只做视觉转换)。
"""
import json
import os
import re
import subprocess
import sys
import pathlib
from datetime import datetime

BASE = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
OUT_ROOT = BASE / "reports" / "douyin"

CHROME_CANDS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

# A 股红涨绿跌 + 情绪七词配色(与驾驶舱/工作台一致)
UP, DOWN = "#c0392b", "{DOWN}"
INK, SUB = "{INK}", "{SUB}"
PAPER, LINE = "#f6f4ef", "{LINE}"
SENT_COLORS = {
    "冰点": "#4a6fa5", "回暖": "#3f9d76", "升温": "#e0a63f", "加速": "#b8352c",
    "分歧": "#9a5cc4", "退潮": "#6b7280", "降温": "#7d99c0",
}
SENT_ORDER = ["冰点", "回暖", "升温", "加速", "分歧", "退潮", "降温"]
TOTAL = 7

# 内容合规改造(2026-09-23 限流反馈后):
# 卡3 的"明日关注:个股"改为"操作纪律"话术(去荐股指向);
# 页眉徽章按日期轮换,打破"每日同模板"画像。
STRATEGY = {
    "退潮": "退潮期纪律:控制仓位、少出手,等情绪止跌信号;高位股断板潮中不接飞刀",
    "降温": "降温期纪律:只看高位股承接,不追不抢;注意量能是否继续萎缩",
    "分歧": "分歧期纪律:方向未明,轻仓试错快进快出;聚焦主线,放弃杂毛",
    "升温": "升温期纪律:跟随主线,注意节奏;前排锁仓,后排谨慎",
    "加速": "加速期纪律:警惕情绪顶部,兑现为主;高位票不恋战",
    "冰点": "冰点期纪律:观察止跌信号,备好名单等回暖;冰点后的首根大阳线是信号",
    "回暖": "回暖期纪律:关注修复主线,逐步参与;首板层质量比高度更重要",
}
HEADER_BADGES = ["A股盘面日记", "今日盘面手记", "复盘手记", "盘面观察"]


def pick_badge(date):
    try:
        return HEADER_BADGES[int(date.replace("-", "")) % len(HEADER_BADGES)]
    except (ValueError, AttributeError):
        return HEADER_BADGES[0]


def mask_name(name):
    """股票名脱敏:取前两字符缩写(ST 前缀保留)。华瓷股份→华瓷,大亚圣象→大亚"""
    name = (name or "").strip()
    if not name:
        return name
    prefix = ""
    core = name
    for p in ("*ST", "ST"):
        if core.startswith(p):
            prefix = p
            core = core[len(p):]
            break
    core = core.strip()
    if len(core) <= 2:
        return prefix + core
    return prefix + core[:2]


def mask_names(s):
    """对顿号/逗号分隔的多名文本逐个脱敏"""
    if not s:
        return s
    parts = re.split(r"([、,，;;\s]+)", s)
    out = []
    for p in parts:
        if p and re.match(r"^[\u4e00-\u9fa5A-Za-z*]{2,8}$", p):
            out.append(mask_name(p))
        else:
            out.append(p)
    return "".join(out)


CODE_PAT = re.compile(r"([\u4e00-\u9fa5]{2,4})([（(]\d{6}[\.]?(?:SZ|SH|BJ)?[）)])")


def mask_free_text(s):
    """自由文本脱敏:把「股票名(代码)」模式的股票名替换为缩写。"""
    if not s:
        return s
    return CODE_PAT.sub(lambda m: mask_name(m.group(1)) + m.group(2), s)


def first_sentence(s, maxlen=84):
    """取第一句;超长截断。用于长 note 的卡片化。"""
    if not s:
        return ""
    s = re.split(r"[。;;]", s)[0].strip()
    if len(s) > maxlen:
        s = s[:maxlen].rstrip(",、,; ") + "…"
    return s


def clip(s, maxlen):
    if not s:
        return ""
    return s if len(s) <= maxlen else s[:maxlen].rstrip(",、,; ") + "…"


def seal_yi(wan):
    """封单万元 → 亿元字符串"""
    try:
        return f"{float(wan) / 10000:.2f}"
    except (TypeError, ValueError):
        return "-"


def pct_color(v):
    """涨跌值配色:正红负绿(A 股口径)"""
    try:
        return UP if float(v) >= 0 else DOWN
    except (TypeError, ValueError):
        return INK


def pct_str(v, sign=True):
    try:
        v = float(v)
        return f"{'+' if sign and v >= 0 else ''}{v:.2f}%"
    except (TypeError, ValueError):
        return "-"


def find_chrome():
    for c in CHROME_CANDS:
        if os.path.exists(c):
            return c
    raise SystemExit("未找到 Chrome/Edge,请确认安装路径后把路径加入 CHROME_CANDS")


def pick_date(argv):
    if len(argv) >= 2 and re.fullmatch(r"\d{8}", argv[1]):
        return argv[1]
    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        raise SystemExit("data/ 下没有任何 JSON")
    return files[-1].stem


# ---------------------------------------------------------------- 主题系统
THEMES = {
    "light": {  # 浅色纸感(偶数日)
        "name": "light", "paper": "#f6f4ef", "card": "#ffffff", "ink": "{INK}",
        "sub": "{SUB}", "line": "{LINE}", "up": "#c0392b", "down": "{DOWN}",
        "chipbg": "{CHIPBG}", "badge": "#c0392b", "badge_fg": "#ffffff",
        "accent": "{ACCENT}", "warnbg": "{WARNBG}", "warnline": "{WARNLINE}",
        "medal2": "#a94a2c", "medal3": "{SUB}", "lbbg": "{LBBG}",
        "hot": "#c0392b",
    },
    "dark": {  # 深色行情终端风(奇数日)
        "name": "dark", "paper": "#10141b", "card": "#171d26", "ink": "#e6eaf2",
        "sub": "#7f8ba0", "line": "#262f3d", "up": "#ff6b6b", "down": "#2fd08e",
        "chipbg": "#1f2734", "badge": "#e8b339", "badge_fg": "#161a22",
        "accent": "#e8b339", "warnbg": "#2a1f26", "warnline": "#4a3038",
        "medal2": "#b3543f", "medal3": "#5a6577", "lbbg": "#2a3040",
        "hot": "#ff6b6b",
    },
}

# 全局色值(apply_theme 重绑定,卡片函数直接引用)
UP = DOWN = INK = SUB = "#000"
PAPER = LINE = CARD = CHIPBG = "#fff"
BADGE_C = BADGE_FG = ACCENT = "#000"
WARNBG = WARNLINE = "#fff"
MEDAL2 = MEDAL3 = LBBG = HOT = "#fff"

BASE_CSS_T = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1080px;height:1440px;overflow:hidden}
body{font-family:'Microsoft YaHei','PingFang SC','Segoe UI',sans-serif;
     background:__PAPER__;color:__INK__;-webkit-font-smoothing:antialiased}
.page{width:1080px;height:1440px;display:flex;flex-direction:column;padding:52px 60px 40px}
.hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:34px}
.hd .badge{background:__BADGE__;color:__BADGE_FG__;font-size:30px;font-weight:700;
           padding:12px 26px;border-radius:14px;letter-spacing:2px}
.hd .date{font-size:30px;color:__SUB__;font-weight:600}
.title{font-size:52px;font-weight:800;letter-spacing:1px;margin-bottom:10px}
.title .bar{display:inline-block;width:14px;height:44px;border-radius:7px;
            background:__UP__;margin-right:18px;vertical-align:-4px}
.subtitle{font-size:28px;color:__SUB__;margin-bottom:30px;line-height:1.5}
.body{flex:1;display:flex;flex-direction:column;min-height:0}
.ft{display:flex;justify-content:space-between;align-items:center;
    border-top:2px solid __LINE__;padding-top:22px;margin-top:26px;
    font-size:24px;color:__SUB__}
.up{color:__UP__}.down{color:__DOWN__}
"""


def apply_theme(T):
    global UP, DOWN, INK, SUB, PAPER, LINE, CARD, CHIPBG
    global BADGE_C, BADGE_FG, ACCENT, WARNBG, WARNLINE
    global MEDAL2, MEDAL3, LBBG, HOT, BASE_CSS
    UP, DOWN, INK, SUB = T["up"], T["down"], T["ink"], T["sub"]
    PAPER, LINE, CARD, CHIPBG = T["paper"], T["line"], T["card"], T["chipbg"]
    BADGE_C, BADGE_FG, ACCENT = T["badge"], T["badge_fg"], T["accent"]
    WARNBG, WARNLINE = T["warnbg"], T["warnline"]
    MEDAL2, MEDAL3, LBBG, HOT = T["medal2"], T["medal3"], T["lbbg"], T["hot"]
    css = BASE_CSS_T
    for k, v in T.items():
        css = css.replace("__" + k.upper() + "__", v)
    BASE_CSS = css


def get_theme(date):
    d = str(date).replace("-", "")
    try:
        wd = datetime.strptime(d, "%Y%m%d").weekday()
    except ValueError:
        wd = 0
    return THEMES["dark"] if wd % 2 == 1 else THEMES["light"]


def shell(title_text, subtitle_html, body_html, date, weekday, idx):
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{BASE_CSS}</style></head><body><div class="page">
<div class="hd"><span class="badge">{pick_badge(date)}</span>
<span class="date">{date} {weekday}</span></div>
<div class="title"><span class="bar"></span>{title_text}</div>
<div class="subtitle">{subtitle_html}</div>
<div class="body">{body_html}</div>
<div class="ft"><span>第 {idx} / {TOTAL} 张</span>
<span>数据整理与复盘 · 不构成投资建议</span></div>
</div></body></html>"""


# ---------------------------------------------------------------- 卡1 封面
def card_cover(d):
    s = d.get("sentiment", "")
    c = SENT_COLORS.get(s, INK)
    pred = d.get("next_pred") or ""
    pc = SENT_COLORS.get(pred, INK)
    node = d.get("node_day") or ""
    concl = first_sentence(d.get("sentiment_note", ""), 100)
    boxes = [
        ("涨停家数", f"{d.get('limit_up','-')} 只"),
        ("连板家数", f"{d.get('lb_count','-')} 只"),
        ("最高板", f"{d.get('max_streak','-')}板 {mask_names(d.get('max_streak_name',''))}"),
        ("炸板率", f"{d.get('broken_rate','-')}%"),
    ]
    grid = "".join(
        f'<div class="k"><div class="kl">{k}</div><div class="kv">{v}</div></div>'
        for k, v in boxes
    )
    node_html = f'<span class="nodechip">节点日 · {first_sentence(d.get("node_note",""),26)}</span>' if node else ""
    body = f"""
<style>
.hero{{text-align:center;margin-top:8px}}
.hero .lab{{font-size:34px;color:{SUB};letter-spacing:6px}}
.hero .word{{font-size:190px;font-weight:900;color:{c};line-height:1.12;letter-spacing:10px}}
.chips{{display:flex;gap:22px;justify-content:center;margin-top:26px}}
.nodechip{{background:{c};color:#fff;font-size:30px;font-weight:700;
           padding:14px 30px;border-radius:40px}}
.predchip{{background:{CARD};border:3px solid {pc};color:{pc};font-size:30px;
           font-weight:700;padding:11px 30px;border-radius:40px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:44px}}
.k{{background:{CARD};border:1px solid {LINE};border-radius:20px;padding:26px 30px}}
.kl{{font-size:27px;color:{SUB}}}
.kv{{font-size:46px;font-weight:800;margin-top:6px}}
.concl{{margin-top:42px;background:{CARD};border-left:10px solid {c};border-radius:14px;
        padding:30px 34px;font-size:33px;line-height:1.65;font-weight:600}}
</style>
<div class="hero">
  <div class="lab">今日情绪判定</div>
  <div class="word">{s}</div>
  <div class="chips">{node_html}<span class="predchip">明日预判 · {pred}</span></div>
</div>
<div class="grid">{grid}</div>
<div class="concl">{concl}</div>
"""
    return shell("涨停复盘 · 全图版", f"{d.get('date','')} {d.get('weekday','')} 市场情绪与结构速览",
                 body, d.get("date", ""), d.get("weekday", ""), 1)


# ---------------------------------------------------------------- 卡2 指标总览
def card_metrics(d):
    idxs = d.get("indices") or []
    idx_html = "".join(
        f'<div class="ix"><span class="in">{i.get("name","")}</span>'
        f'<span class="ic">{i.get("close","")}</span>'
        f'<span class="ip" style="color:{pct_color(i.get("pct"))}">{pct_str(i.get("pct"))}</span></div>'
        for i in idxs
    )
    to_yi = d.get("turnover_yi")
    to_delta = d.get("turnover_delta_yi")
    delta_s = f"{'+' if isinstance(to_delta,(int,float)) and to_delta>=0 else ''}{to_delta}亿" if to_delta is not None else ""
    cells = [
        ("涨停家数", f"{d.get('limit_up','-')}", "只", UP),
        ("跌停家数", f"{d.get('limit_down','-')}", "只", DOWN),
        ("连板家数", f"{d.get('lb_count','-')}", "只", UP),
        ("最高板", f"{d.get('max_streak','-')}", f"板 · {mask_names(d.get('max_streak_name',''))}", UP),
        ("封板率", f"{d.get('seal_rate','-')}", "%", INK),
        ("炸板率", f"{d.get('broken_rate','-')}", "%", UP),
        ("晋级率", f"{d.get('promotion_rate','-')}", "%", UP),
        ("昨日涨停溢价", pct_str(d.get("premium_pct")), "", pct_color(d.get("premium_pct"))),
        ("大面家数", f"{d.get('big_face_count','-')}", "只", UP),
    ]
    grid = "".join(
        f'<div class="cell"><div class="cl">{k}</div>'
        f'<div class="cv" style="color:{col}">{v}<span class="cu">{u}</span></div></div>'
        for k, v, u, col in cells
    )
    updown = f"{d.get('up_count','-')} : {d.get('down_count','-')}"
    body = f"""
<style>
.idxbar{{display:flex;justify-content:space-between;background:{CARD};border:1px solid {LINE};
        border-radius:18px;padding:24px 26px;margin-bottom:26px}}
.ix{{text-align:center}}
.in{{display:block;font-size:25px;color:{SUB}}}
.ic{{display:block;font-size:34px;font-weight:700;margin-top:4px}}
.ip{{display:block;font-size:26px;font-weight:700;margin-top:2px}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}}
.cell{{background:{CARD};border:1px solid {LINE};border-radius:18px;padding:26px 28px}}
.cl{{font-size:27px;color:{SUB}}}
.cv{{font-size:52px;font-weight:800;margin-top:8px}}
.cu{{font-size:26px;font-weight:600;color:{SUB};margin-left:8px}}
.wide{{display:flex;gap:20px;margin-top:20px}}
.wcell{{flex:1;background:{CARD};border:1px solid {LINE};border-radius:18px;
        padding:24px 28px;display:flex;justify-content:space-between;align-items:center}}
.wl{{font-size:27px;color:{SUB}}}
.wv{{font-size:38px;font-weight:800}}
</style>
<div class="idxbar">{idx_html}</div>
<div class="grid">{grid}</div>
<div class="wide">
  <div class="wcell"><span class="wl">两市成交</span>
    <span class="wv">{to_yi if to_yi is not None else '-'} 亿 <span style="font-size:26px;color:{UP};font-weight:700">{delta_s}</span></span></div>
  <div class="wcell"><span class="wl">涨跌家数</span><span class="wv">{updown}</span></div>
</div>
"""
    return shell("核心指标总览", "收盘口径 · 涨跌红绿按 A 股习惯(红涨绿跌)",
                 body, d.get("date", ""), d.get("weekday", ""), 2)


# ---------------------------------------------------------------- 卡3 情绪周期
def card_emotion(d):
    cur = d.get("sentiment", "")
    pred = d.get("next_pred") or ""
    pc = SENT_COLORS.get(pred, INK)
    cycle = "".join(
        f'<div class="stop{" cur" if w == cur else ""}" '
        f'style="{"background:%s;border-color:%s;color:#fff" % (SENT_COLORS[w], SENT_COLORS[w]) if w == cur else "background:{CARD};color:%s;border-color:{LINE}" % SUB}">'
        f'{w}</div>' + ("" if w == "降温" else '<div class="arr">→</div>')
        for w in SENT_ORDER
    )
    promo = d.get("max_streak_promo", "")
    mx_masked = mask_names(d.get("max_streak_name", ""))
    rows = [
        ("① 空间高度", f"{mx_masked} {d.get('max_streak','-')}板{' · ' + promo if promo else ''}",
         UP if promo == "晋级" else SUB),
        ("② 涨停/连板家数", f"涨停 {d.get('limit_up','-')} 只 · 连板 {d.get('lb_count','-')} 只", INK),
        ("③ 炸板率", f"{d.get('broken_rate','-')}%" + (" · 破 20% 警戒线" if isinstance(d.get("broken_rate"), (int, float)) and d["broken_rate"] >= 20 else ""),
         UP if isinstance(d.get("broken_rate"), (int, float)) and d["broken_rate"] >= 20 else INK),
        ("④ 昨日涨停溢价", pct_str(d.get("premium_pct")) + " · 打板效应未转负" if isinstance(d.get("premium_pct"), (int, float)) and d["premium_pct"] > 0 else pct_str(d.get("premium_pct")),
         pct_color(d.get("premium_pct"))),
        ("⑤ 大面家数", f"{d.get('big_face_count','-')} 只" + (" · 达 ≥5 警戒" if isinstance(d.get("big_face_count"), (int, float)) and d["big_face_count"] >= 5 else ""),
         UP if isinstance(d.get("big_face_count"), (int, float)) and d["big_face_count"] >= 5 else INK),
    ]
    table = "".join(
        f'<div class="row"><span class="rk">{k}</span>'
        f'<span class="rv" style="color:{col}">{v}</span></div>'
        for k, v, col in rows
    )
    note = mask_free_text(first_sentence(d.get("node_note", ""), 90))
    # 内容合规:"明日关注:个股"→"操作纪律"话术(去荐股指向)
    strategy = STRATEGY.get(cur, "按情绪周期纪律执行")
    body = f"""
<style>
.cycle{{display:flex;align-items:center;justify-content:space-between;
        background:{CARD};border:1px solid {LINE};border-radius:18px;
        padding:18px 20px;margin-bottom:18px}}
.stop{{font-size:27px;font-weight:700;padding:10px 16px;border-radius:36px;
       border:2px solid {LINE};white-space:nowrap}}
.stop.cur{{box-shadow:0 6px 16px rgba(60,40,90,.25)}}
.arr{{color:{SUB};font-size:24px}}
.nrow{{background:{CARD};border-left:10px solid {SENT_COLORS.get(cur, INK)};border-radius:14px;
       padding:16px 26px;font-size:29px;line-height:1.55;font-weight:600;margin-bottom:18px}}
.rows{{display:flex;flex-direction:column;gap:10px;margin-bottom:18px}}
.row{{display:flex;justify-content:space-between;align-items:center;background:{CARD};
      border:1px solid {LINE};border-radius:14px;padding:12px 24px}}
.rk{{font-size:28px;color:{SUB};font-weight:600}}
.rv{{font-size:29px;font-weight:800;text-align:right}}
.predline{{display:flex;align-items:center;gap:22px;margin-bottom:16px}}
.predbig{{background:{pc};color:#fff;font-size:36px;font-weight:900;
          padding:10px 36px;border-radius:18px}}
.predlab{{font-size:28px;color:{SUB};font-weight:700}}
.focus{{background:{CARD};border:1px dashed {ACCENT};border-radius:16px;padding:16px 24px}}
.fh{{font-size:26px;font-weight:800;color:{ACCENT};margin-bottom:8px}}
.fs{{font-size:25px;line-height:1.5;color:{INK};font-weight:600}}
</style>
<div class="cycle">{cycle}</div>
<div class="nrow">{note}</div>
<div class="rows">{table}</div>
<div class="predline"><span class="predbig">{pred}</span>
  <span class="predlab">明日情绪预判</span></div>
<div class="focus"><div class="fh">操作纪律</div><div class="fs">{strategy}</div></div>
"""
    return shell("情绪周期 · 五指标判读", "七词周期:冰点→回暖→升温→加速→分歧→退潮→降温",
                 body, d.get("date", ""), d.get("weekday", ""), 3)


# ---------------------------------------------------------------- 卡4 连板梯队
def card_ladder(d):
    rows = []
    for item in d.get("ladder") or []:
        st = item.get("streak", "")
        label = re.sub(r"^\d+\s*连板", "", item.get("label", "")).strip("（）()")
        label = label.replace(",", "·")
        chips = "".join(f'<span class="chip">{mask_name(n)}</span>' for n in item.get("stocks", []))
        hot = f' style="background:{HOT}"' if st == max((i.get("streak", 0) for i in d.get("ladder", [])), default=0) else ""
        rows.append(f"""
<div class="lrow"><div class="lb"{hot}><b>{st}</b><i>板</i></div>
<div class="lmain"><div class="llab">{label}</div><div class="lchips">{chips}</div></div></div>""")
    broken = d.get("ladder_stats", {}).get("broken", "")
    extra = ""
    if "区间结构另计" in broken:
        seg = broken.split("区间结构另计", 1)[1].lstrip("：:（( ")
        seg = mask_free_text(clip(seg.split("。")[0], 84))
        if seg:
            extra = f'<div class="extra"><b>区间结构另计</b>{seg}</div>'
    stats = d.get("ladder_stats", {})
    statline = (f"首板 {stats.get('first_board','-')} 只 · 2板及以上 {stats.get('two_plus','-')} 只"
                f" · 连板晋级率 {d.get('promotion_rate','-')}%")
    warn = ""
    if isinstance(d.get("broken_rate"), (int, float)) and d["broken_rate"] >= 20:
        warn = f'<div class="warn">⚠ 炸板率 {d["broken_rate"]}% 破警戒线 · 高位断板负反馈已现(详见风险卡)</div>'
    body = f"""
<style>
.lrows{{display:flex;flex-direction:column;gap:10px}}
.lrow{{display:flex;gap:22px;align-items:flex-start;background:{CARD};
       border:1px solid {LINE};border-radius:18px;padding:12px 22px}}
.lb{{min-width:120px;text-align:center;background:{LBBG};color:#fff;
     border-radius:14px;padding:8px 0 6px}}
.lb b{{font-size:46px;font-weight:900}}
.lb i{{font-style:normal;font-size:25px;margin-left:4px}}
.llab{{font-size:24px;color:{SUB};margin-bottom:8px}}
.lchips{{display:flex;flex-wrap:wrap;gap:9px}}
.chip{{font-size:27px;font-weight:700;background:{CHIPBG};border:1px solid {LINE};
       border-radius:10px;padding:6px 14px;color:{INK}}}
.statline{{margin-top:14px;background:{CARD};border:1px solid {LINE};border-radius:14px;
           padding:12px 22px;font-size:27px;font-weight:700;color:{INK}}}
.extra{{margin-top:10px;background:{CARD};border:1px solid {LINE};border-radius:14px;
        padding:12px 22px;font-size:24px;line-height:1.5;color:{INK}}}
.extra b{{color:{ACCENT};margin-right:12px}}
.warn{{margin-top:10px;background:{WARNBG};border:1px solid {WARNLINE};border-radius:14px;
       padding:12px 22px;font-size:25px;font-weight:700;color:#c0392b}}
</style>
<div class="lrows">{''.join(rows)}</div>
<div class="statline">{statline}</div>
{extra}{warn}
"""
    return shell("连板梯队", "空间高度与厚度 · 名单为收盘涨停口径",
                 body, d.get("date", ""), d.get("weekday", ""), 4)


# ---------------------------------------------------------------- 卡5 主线分布
def card_lines(d):
    blocks = []
    palette = ["#c0392b", "#b8352c", "#a94a2c", "#9a5cc4", "#4a6fa5",
               "#3f9d76", "#e0a63f", "#7d99c0", "{ACCENT}"]
    groups = d.get("main_lines") or []
    shown = groups[:7] if len(groups) > 8 else groups
    rest = groups[len(shown):]
    for i, g in enumerate(shown):
        name = g.get("name", "")
        if len(name) > 16 and "其他" in name:
            name = "其他零散(多行业)"
        col = palette[i % len(palette)]
        note = mask_free_text(clip(first_sentence(g.get("note", ""), 200), 54))
        blocks.append(f"""
<div class="mg" style="border-left:10px solid {col}">
 <div class="mh"><span class="mn">{name}</span><span class="mc" style="background:{col}">{g.get('count','-')} 只</span></div>
 <div class="mt">{note}</div></div>""")
    rest_html = ""
    if rest:
        rest_sum = sum(g.get("count", 0) or 0 for g in rest)
        rest_names = ";".join(
            (g.get("name", "").split("（")[0].split("(")[0] + f" {g.get('count','-')}")
            for g in rest)
        rest_names = clip(rest_names, 86)
        rest_html = (f'<div class="rest"><b>另计 {len(rest)} 组 · {rest_sum} 只</b>'
                     f'{rest_names}</div>')
    body = f"""
<style>
.mgs{{display:flex;flex-direction:column;gap:10px}}
.mg{{background:{CARD};border-radius:14px;padding:10px 22px;border:1px solid {LINE}}}
.mh{{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}}
.mn{{font-size:28px;font-weight:800}}
.mc{{color:#fff;font-size:23px;font-weight:700;border-radius:18px;padding:2px 14px}}
.mt{{font-size:23px;color:{SUB};line-height:1.42}}
.rest{{margin-top:2px;background:{CARD};border:1px dashed {ACCENT};border-radius:14px;
       padding:10px 22px;font-size:23px;line-height:1.45;color:{SUB}}}
.rest b{{color:{ACCENT};margin-right:12px}}
</style>
<div class="mgs">{''.join(blocks)}{rest_html}</div>
"""
    return shell("主线与题材分布", "涨停个股按主线归组 · 括注代表连板标",
                 body, d.get("date", ""), d.get("weekday", ""), 5)


# ---------------------------------------------------------------- 卡6 封单TOP10
def card_seal(d):
    rows = []
    for i, t in enumerate(d.get("top_seal") or []):
        medal = ["#c0392b", "#b8352c", "#a94a2c"][i] if i < 3 else "{SUB}"
        rows.append(f"""
<div class="srow"><span class="rk" style="background:{medal}">{i + 1}</span>
<span class="sn">{mask_name(t.get('name',''))}<i>{t.get('code','')}</i></span>
<span class="sv">{seal_yi(t.get('seal'))} 亿</span></div>""")
    total = sum(x.get("seal", 0) or 0 for x in (d.get("top_seal") or [])) / 10000
    body = f"""
<style>
.srows{{display:flex;flex-direction:column;gap:10px}}
.srow{{display:flex;align-items:center;gap:22px;background:{CARD};
       border:1px solid {LINE};border-radius:14px;padding:11px 24px}}
.rk{{width:48px;height:48px;border-radius:50%;color:#fff;display:flex;
     align-items:center;justify-content:center;font-size:26px;font-weight:800;flex:none}}
.sn{{font-size:31px;font-weight:800;flex:1}}
.sn i{{font-style:normal;font-size:23px;color:{SUB};font-weight:500;margin-left:14px}}
.sv{{font-size:33px;font-weight:900;color:#c0392b}}
.total{{margin-top:14px;background:{CARD};border:1px solid {LINE};border-radius:14px;
        padding:12px 24px;display:flex;justify-content:space-between;align-items:center}}
.tl{{font-size:27px;color:{SUB};font-weight:600}}
.tv{{font-size:33px;font-weight:900}}
</style>
<div class="srows">{''.join(rows)}</div>
<div class="total"><span class="tl">TOP10 封单合计(收盘口径)</span>
<span class="tv" style="color:{UP}">{total:.2f} 亿</span></div>
"""
    return shell("收盘封单 TOP10", "封单金额 = 收盘买单封死涨停价的对应金额",
                 body, d.get("date", ""), d.get("weekday", ""), 6)


# ---------------------------------------------------------------- 卡7 风险与观察
def card_risk(d):
    risks = [mask_free_text(clip(first_sentence(x, 120), 62)) for x in (d.get("risk_signals") or [])[:5]]
    watches = [mask_free_text(clip(first_sentence(x, 120), 62)) for x in (d.get("watch_next") or [])[:4]]
    rhtml = "".join(f'<div class="ri"><span class="dot" style="background:{UP}"></span><span>{x}</span></div>' for x in risks)
    whtml = "".join(f'<div class="ri"><span class="dot" style="background:#4a6fa5"></span><span>{x}</span></div>' for x in watches)
    body = f"""
<style>
.sec{{margin-bottom:12px}}
.sh{{display:flex;align-items:center;gap:16px;margin-bottom:6px}}
.sh b{{font-size:32px;font-weight:900}}
.sh i{{font-style:normal;font-size:24px;color:{SUB}}}
.ri{{display:flex;gap:14px;background:{CARD};border:1px solid {LINE};border-radius:14px;
     padding:9px 18px;font-size:24px;line-height:1.4;margin-bottom:7px;color:{INK}}}
.dot{{width:11px;height:11px;border-radius:50%;flex:none;margin-top:11px}}
</style>
<div class="sec"><div class="sh"><b style="color:{UP}">⚠ 风险信号</b><i>当日已验证 · 取前五条</i></div>{rhtml}</div>
<div class="sec"><div class="sh"><b style="color:#4a6fa5">◎ 明日观察</b><i>四条主线</i></div>{whtml}</div>
"""
    return shell("风险信号与明日观察", "退潮/分歧期的仓位与节奏参照",
                 body, d.get("date", ""), d.get("weekday", ""), 7)


# ---------------------------------------------------------------- 主流程
CARDS = [
    ("01_cover", card_cover, "封面"),
    ("02_metrics", card_metrics, "指标总览"),
    ("03_emotion", card_emotion, "情绪周期"),
    ("04_ladder", card_ladder, "连板梯队"),
    ("05_lines", card_lines, "主线分布"),
    ("06_seal", card_seal, "封单TOP10"),
    ("07_risk", card_risk, "风险观察"),
]


def main():
    date = pick_date(sys.argv)
    jpath = DATA_DIR / f"{date}.json"
    d = json.loads(jpath.read_text(encoding="utf-8"))
    theme = get_theme(date)
    apply_theme(theme)
    outdir = OUT_ROOT / date
    outdir.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome()
    results = []
    for fname, fn, cname in CARDS:
        html = fn(d)
        hpath = outdir / f"{fname}.html"
        ppath = outdir / f"{fname}.png"
        hpath.write_text(html, encoding="utf-8")
        cmd = [
            chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1", "--force-color-profile=srgb",
            "--default-background-color=FFFFFFFF", "--virtual-time-budget=4000",
            "--window-size=1080,1440", f"--screenshot={ppath}",
            hpath.as_uri(),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        ok = ppath.exists() and ppath.stat().st_size > 10000
        results.append((fname, cname, ok, ppath))
        print(f"[{'OK ' if ok else 'FAIL'}] {fname}.png ({cname})"
              + ("" if ok else f"  rc={r.returncode} {r.stderr[:200]}"))
    print(f"\n主题: {theme['name']} | 输出目录: {outdir}")


if __name__ == "__main__":
    main()
