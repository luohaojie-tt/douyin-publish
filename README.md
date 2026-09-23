# douyin-publish — 抖音图文自动发布技能

把 A 股涨停复盘日报转换成**全内容图片卡组**（7 张 1080×1440 竖版卡），通过 Chrome DevTools Protocol 半自动发布到抖音图文（creator.douyin.com）。

> 姊妹项目：[taoguba-publish](https://github.com/luohaojie-tt/taoguba-publish)（同体系复盘日报发淘股吧：纯文本转换 + 表格图 + CDP 半自动发帖）

**适用对象**：装了 [WorkBuddy](https://www.workbuddy.cn) 的个人投资者（技能由 AI 对话驱动），也兼容任何能跑 Python + Chrome 的环境。

---

## 卡片组内容

每天收盘后，脚本把结构化复盘数据（`data/YYYYMMDD.json`）渲染成 7 张竖版卡片：

| # | 卡片 | 内容 |
|---|------|------|
| 01 | 封面 | 情绪周期大字判定 + 节点日/明日预判徽章 + 核心数 + 一句话定性 |
| 02 | 指标总览 | 指数条（红涨绿跌）+ 涨停/连板/炸板率/晋级率/溢价/大面 9 宫格 |
| 03 | 情绪周期 | 七词周期环（冰点→回暖→升温→加速→分歧→退潮→降温）+ 五指标判读 + 明日关注 |
| 04 | 连板梯队 | 全档连板名单 chips + 首板/晋级率统计 + 炸板率警戒条 |
| 05 | 主线分布 | 涨停个股按主线归组（超 8 组自动折叠为「另计」汇总行） |
| 06 | 封单 TOP10 | 收盘封单金额榜 + TOP10 合计 |
| 07 | 风险与观察 | 风险信号 5 条 + 明日观察 4 条（自动取首句压缩） |

标题/简介自动生成（≤19 字标题 + 指标行 + 话题 `#涨停复盘 #短线打板`），配乐由发布者在抖音音乐面板里人工挑选（站内曲库，版权无忧）。

## 工作原理

- **图片生成**：纯本地 HTML 模板 + Chrome headless 截图（`--headless=new --window-size=1080,1440`），无外部 CDN，无平台风控问题
- **发布**：CDP（`websocket-client` + `suppress_origin`）驱动一个**独立 profile 的真实 Chrome**（`--remote-debugging-port=9224`），对 creator.douyin.com：
  - 图片上传走 `DOM.setFileInputFiles`（绕开系统文件对话框，7 张一次到位）
  - React 受控输入用 native value setter + input 事件；contenteditable 用 execCommand
  - 页面诊断全程落 `_publish_diag.json`，抖音改版锚点失效时按诊断修 selector，不盲猜
- **人机分工**（实战摸索出的稳定边界）：

| 动作 | 谁做 |
|------|------|
| 生成卡片、上传 7 图、填标题/简介、点发布、核验 | 脚本 |
| 启动专用浏览器（首次扫码登录）、发布前过目、配乐挑选、短信二次验证 | 人工 |

## 安装

### WorkBuddy 用户

```bash
git clone https://github.com/luohaojie-tt/douyin-publish.git ~/.workbuddy/skills/douyin-publish
```

然后把 `scripts/` 下的三个文件复制到你的复盘工作目录（脚本用相对路径找 `data/` 与 `reports/`，目录布局见下）。

### 依赖

- Python 3.10+（标准库 + `websocket-client`：`pip install websocket-client`）
- Chrome / Edge（本机已装即可）

### 目录布局

```
工作目录/
├── data/YYYYMMDD.json          ← 结构化复盘数据（数据契约见下）
├── reports/douyin/YYYYMMDD/    ← 卡片输出
├── make_douyin_images.py
├── douyin_publish.py
└── start_douyin_browser.bat
```

## 使用

```bash
# 0) 双击 start_douyin_browser.bat 启动专用 Chrome（独立 profile，首次扫码登录）
# 1) 生成 7 张卡片
python make_douyin_images.py 20260922
# 2) 上传 + 填标题简介 + 预览截图（人工过目）
python douyin_publish.py 20260922
# 3) （可选）配乐：在窗口里点“选择音乐”→搜歌→点“使用”
# 4) 确认无误后发布
python douyin_publish.py 20260922 --post
# 首次会弹短信二次验证：输入手机验证码后流程自动走完
```

变体参数：`--refill`（图已在页面，只重填文本）、`--force`（配合页面“清空并重新上传”重传）、`--music 关键词` / `--music-pick 曲名`（音乐面板辅助，可靠性一般，推荐人工选歌）。

## 数据契约

读取 `data/YYYYMMDD.json`，使用的字段（缺失显示 `-`，不崩）：

```
date, weekday, sentiment, next_pred,
limit_up, limit_down, lb_count, broken_rate, seal_rate, promotion_rate,
max_streak, max_streak_name, max_streak_promo, premium_pct, big_face_count,
turnover_yi, turnover_delta_yi, up_count, down_count,
indices[{name,close,pct}], ladder[{streak,label,stocks}], ladder_stats{first_board,two_plus,broken},
main_lines[{name,count,note}], top_seal[{code,name,seal(万元)}],
sentiment_note, node_day, node_note, focus_stocks[], risk_signals[], watch_next[]
```

## 实测踩坑（省你一下午）

1. **抖音图文标题上限 20 字符**（不是 30），超了静默截断——生成时按 ≤19 字控制
2. **简介的程序注入换行会被编辑器规整成单段**：`insertParagraph` / `insertHTML`+`<br>` 都拦
3. **曲名括号是全角（）**：音乐面板的曲名如「清明上河图（同花顺进行曲）」，做字符串匹配必须用全角，半角永远 NOT_FOUND
4. **音乐面板打开按钮对 JS click 和 CDP 真实鼠标全部免疫**（自管事件组件），面板内搜索/选歌自动化也不稳（虚拟列表 + 结果集漂移）→ 定案人工选歌
5. **面板关闭时 React keep-alive 保留隐藏 DOM**：`offsetParent` 和 rect 尺寸都会骗人，真伪判据用 `elementFromPoint`
6. **发布可能弹短信二次验证**(连续两天实测都弹)（本人手机验证码），人工输入后流程自动走完，后续低频复验
7. 发布成功判据：URL 跳 `content/manage?enter_from=publish` + 作品列表出现「N张 | 标题」+ 发布时间
8. React 页面 `element.click()` 不保证触发 onClick——先 JS click，失败再用 `Input.dispatchMouseEvent` 真实鼠标；两者都无效 = 组件自管事件，转人工
9. 搜索框做兜底选择时**必须排除主页面业务输入框**（否则关键词会打进标题里）

10. **财经内容红线 + 同质化限流**:连续两天发同模板图文被判定"批量发布同质化低质内容"并限流。改造:标题/简介/卡片一律不出现个股名与荐股指向(以操作纪律话术替代)、标题与话题每日轮换、发布节奏隔 2~3 天;遇限流停发 3~5 天。金融内容在抖音是重点审查区

## 风险与免责

- 抖音对自动化操作有风控（滑块/短信验证/限流），本工具用真实浏览器 + 拟人节奏（逐字符输入、真实鼠标事件）把风险降到最低，但不为任何账号后果负责
- 音乐从抖音站内曲库选择（平台已授权），不要尝试上传本地音频
- 本项目仅供学习与个人复盘记录，不构成任何投资建议
