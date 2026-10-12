#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 free_models_classified.json，生成 Markdown 摘要与可视化 HTML 报告。"""
import json
import os
import html

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "free_models_classified.json")
MD = os.path.join(BASE, "openrouter_free_report.md")
HTML = os.path.join(BASE, "openrouter_free_report.html")

data = json.load(open(SRC, encoding="utf-8"))
rows = data["all_free"]
recs = data["recommendations"]
champs = data["champions"]
w = data["weights"]


def esc(s):
    return html.escape(str(s)) if s is not None else ""


# ---- 维度分布统计 ----
def dist(key_fn):
    from collections import Counter
    c = Counter(key_fn(r) for r in rows)
    return c


reason_tiers = dist(lambda r: r["reasoning"]["level"])
window_tiers = dist(lambda r: r["free_window"]["window"])
mod_tiers = dist(lambda r: "多模态" if any(t in r["capability"]["tags"] for t in ["视觉", "音频", "视频"]) else "纯文本")

# =====================================================================
# Markdown 摘要
# =====================================================================
md = []
md.append(f"# OpenRouter 免费大模型筛选与推荐报告\n")
md.append(f"> 生成时间：{data['generated_at']}  ｜  全量模型：{data['total_models']} ｜ 免费模型：{data['free_count']}\n")
md.append("## 工作流（6 步）")
md.append("1. **抓取** `GET /api/v1/models` → 全量模型")
md.append("2. **过滤** `pricing.prompt==0 且 pricing.completion==0` → 免费模型")
md.append("3. **增强** 逐个 `GET /models/{slug}/endpoints` → 提供方数/在线率/延迟（热门度代理）")
md.append("4. **四维分类** 推理级别 / 模型能力 / 免费活动时间 / 免费热门度")
md.append(f"5. **综合评分** 推理×{int(w['reasoning']*100)}% + 能力×{int(w['capability']*100)}% + 热门×{int(w['popularity']*100)}%（再乘免费稳定性护栏）")
md.append("6. **推荐** 综合 Top3 + 维度冠军去重，输出 1–3 个\n")

md.append("## 最终推荐（综合 Top 3）\n")
for i, r in enumerate(recs, 1):
    md.append(f"### {i}. {r['name']}  —  `{r['id']}`")
    md.append(f"- **综合得分**：{r['composite_final']}（原始 {r['composite']}）")
    md.append(f"- 推理：{r['reasoning']['level']}（实力 {r['reasoning']['strength']}）｜ 能力：{r['capability']['score']}｜ 热门：{r['popularity']['score']}")
    md.append(f"- 免费窗口：**{r['free_window']['window']}** ｜ {r['free_window']['note']}")
    md.append(f"- 能力标签：{', '.join(r['capability']['tags'])}")
    md.append(f"- 热门度：{r['popularity']['note']}")
    md.append("")

md.append("## 各维度冠军\n")
md.append("| 维度 | 模型 | ID |")
md.append("|------|------|-----|")
name_by_id = {r["id"]: r["name"] for r in rows}
for k, vid in champs.items():
    md.append(f"| {k} | {name_by_id.get(vid,'?')} | `{vid}` |")
md.append("")

md.append("## 四维分类分布\n")
md.append(f"- **推理级别**：" + "，".join(f"{k} {v}" for k, v in reason_tiers.most_common()))
md.append(f"- **免费窗口**：" + "，".join(f"{k} {v}" for k, v in window_tiers.most_common()))
md.append(f"- **模态**：" + "，".join(f"{k} {v}" for k, v in mod_tiers.most_common()))
md.append("")

md.append("## 全部免费模型排名（按综合分）\n")
md.append("| 排名 | 模型 | 综合 | 推理级 | 推理实力 | 能力 | 免费窗口 | 热门 |")
md.append("|------|------|------|--------|----------|------|----------|------|")
for i, r in enumerate(rows, 1):
    md.append(f"| {i} | {esc(r['name'])} | {r['composite_final']} | {r['reasoning']['level']} | {r['reasoning']['strength']} | {r['capability']['score']} | {r['free_window']['window']} | {r['popularity']['score']} |")
md.append("")

md.append("## 方法与口径说明\n")
md.append("- **免费判定**：`pricing.prompt` 与 `pricing.completion` 同时为字符串 `\"0\"`。")
md.append("- **推理级别**：依据 API 的 `reasoning` 字段（强制/默认开启/可选/无）+ 模型族启发（R1、Qwen3-thinking 等）。")
md.append("- **推理实力（质量感知）**：层级分与 `artificial_analysis.intelligence_index` 质量加权；无实测指数则封顶，避免小模型误判。")
md.append("- **模型能力**：上下文长度（对数刻度）+ 多模态广度 + 基准指数 + 工具/结构化输出支持。")
md.append("- **免费活动时间**：`expiration_date` 区分永久免费 / 长期限免 / 短期限免 / 即将到期(<30天)；`created` 计上架时长。")
md.append("- **免费热门度（代理）**：公开 models API **不含**使用量字段，故以「提供方数量」为主代理（越多=托管越广=越可靠），叠加在线率与延迟。")
md.append("- **免费稳定性护栏**：稳定性作为乘子（永久 1.0 / 长限免 0.95 / 短限免 0.85 / 临期 0.6 / 过期 0.1），避免临期模型被优先推荐。")
md.append("- **数据时效**：模型列表与端点数据均为运行时刻实时抓取，重跑即更新。")

with open(MD, "w", encoding="utf-8") as f:
    f.write("\n".join(md))
print("MD 写出:", MD)

# =====================================================================
# HTML 可视化报告
# =====================================================================
def bar(v, color):
    v = max(0, min(100, v))
    return f'<div class="bar"><span style="width:{v}%;background:{color}"></span></div>'


# 工作流步骤卡
steps = [
    ("1 抓取", "GET /api/v1/models", "全量模型"),
    ("2 过滤", "pricing==0", "免费模型"),
    ("3 增强", "GET /endpoints", "提供方/在线率/延迟"),
    ("4 分类", "四维", "推理/能力/窗口/热门"),
    ("5 评分", "加权+护栏", "综合分"),
    ("6 推荐", "Top3", "1–3 个模型"),
]
steps_html = "".join(
    f'<div class="step"><div class="step-n">{s[0]}</div><div class="step-t">{esc(s[1])}</div><div class="step-d">{esc(s[2])}</div></div>'
    for s in steps
)

# 推荐卡
rec_cards = ""
for i, r in enumerate(recs, 1):
    tags = " ".join(f'<span class="tag">{esc(t)}</span>' for t in r["capability"]["tags"])
    rec_cards += f"""
    <div class="rec-card">
      <div class="rec-rank">#{i}</div>
      <div class="rec-body">
        <div class="rec-title">{esc(r['name'])}</div>
        <div class="rec-id">{esc(r['id'])}</div>
        <div class="scores">
          <div class="score-row"><label>综合 {r['composite_final']}</label>{bar(r['composite_final'],'#2563eb')}</div>
          <div class="score-row"><label>推理 {r['reasoning']['strength']}</label>{bar(r['reasoning']['strength'],'#7c3aed')}</div>
          <div class="score-row"><label>能力 {r['capability']['score']}</label>{bar(r['capability']['score'],'#059669')}</div>
          <div class="score-row"><label>热门 {r['popularity']['score']}</label>{bar(r['popularity']['score'],'#d97706')}</div>
        </div>
        <div class="rec-meta">
          <span class="pill {'pill-ok' if r['free_window']['window']=='永久免费' else 'pill-warn'}">{esc(r['free_window']['window'])}</span>
          <span class="pill pill-ghost">{esc(r['reasoning']['level'])}</span>
        </div>
        <div class="tags">{tags}</div>
        <div class="rec-pop">{esc(r['popularity']['note'])}</div>
      </div>
    </div>"""

# 冠军表
champ_rows = ""
for k, vid in champs.items():
    rr = next((x for x in rows if x["id"] == vid), None)
    champ_rows += f"<tr><td>{esc(k)}</td><td>{esc(rr['name']) if rr else '?'}</td><td><code>{esc(vid)}</code></td></tr>"

# 全量排名表
rank_rows = ""
for i, r in enumerate(rows, 1):
    rank_rows += f"""<tr>
      <td>{i}</td><td>{esc(r['name'])}</td>
      <td><b>{r['composite_final']}</b></td>
      <td>{esc(r['reasoning']['level'])}</td>
      <td>{r['reasoning']['strength']}</td>
      <td>{r['capability']['score']}</td>
      <td>{esc(r['free_window']['window'])}</td>
      <td>{r['popularity']['score']}</td>
    </tr>"""

# 分布条
def dist_bar(counter, order=None):
    items = counter.most_common() if order is None else [(k, counter.get(k, 0)) for k in order]
    total = sum(counter.values()) or 1
    out = ""
    palette = ["#2563eb", "#059669", "#d97706", "#7c3aed", "#dc2626"]
    for (k, v), col in zip(items, palette):
        pct = v / total * 100
        out += f'<div class="drow"><span class="dlabel">{esc(k)}</span><div class="dbar"><span style="width:{pct}%;background:{col}"></span></div><span class="dval">{v}</span></div>'
    return out

reason_dist_html = dist_bar(reason_tiers)
window_dist_html = dist_bar(window_tiers)

html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenRouter 免费大模型筛选与推荐</title>
<style>
  :root{{--bg:#f7f8fa;--card:#ffffff;--ink:#1f2933;--sub:#5b6775;--line:#e4e8ee;--blue:#2563eb;--green:#059669;--amber:#d97706;--purple:#7c3aed;}}
  *{{box-sizing:border-box}}
  body{{margin:0;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);line-height:1.6}}
  .wrap{{max-width:1080px;margin:0 auto;padding:28px 20px 60px}}
  header{{border-bottom:2px solid var(--line);padding-bottom:16px;margin-bottom:24px}}
  h1{{font-size:24px;margin:0 0 6px}}
  .sub{{color:var(--sub);font-size:14px}}
  h2{{font-size:18px;margin:34px 0 14px;border-left:4px solid var(--blue);padding-left:10px}}
  .steps{{display:flex;flex-wrap:wrap;gap:10px}}
  .step{{flex:1;min-width:140px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;position:relative}}
  .step-n{{font-weight:700;color:var(--blue);font-size:13px}}
  .step-t{{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:#eef2ff;color:#3730a3;border-radius:6px;padding:2px 6px;display:inline-block;margin:6px 0}}
  .step-d{{font-size:13px;color:var(--sub)}}
  .grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}
  @media(max-width:820px){{.grid3{{grid-template-columns:1fr}}}}
  .rec-card{{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
  .rec-rank{{font-size:30px;font-weight:800;color:var(--blue);min-width:42px;text-align:center}}
  .rec-title{{font-weight:700;font-size:16px}}
  .rec-id{{font-family:ui-monospace,monospace;font-size:12px;color:var(--sub);margin-bottom:8px}}
  .scores{{margin:6px 0}}
  .score-row{{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:12px}}
  .score-row label{{width:64px;color:var(--sub);flex:none}}
  .bar{{flex:1;height:9px;background:#eef1f5;border-radius:6px;overflow:hidden}}
  .bar span{{display:block;height:100%;border-radius:6px}}
  .pill{{display:inline-block;font-size:12px;padding:2px 9px;border-radius:999px;margin-right:6px}}
  .pill-ok{{background:#dcfce7;color:#166534}}
  .pill-warn{{background:#fef3c7;color:#92400e}}
  .pill-ghost{{background:#eef2f7;color:#475569}}
  .tag{{display:inline-block;font-size:11px;background:#eef2f7;color:#475569;border-radius:6px;padding:1px 7px;margin:3px 3px 0 0}}
  .rec-pop{{font-size:12px;color:var(--sub);margin-top:6px}}
  table{{width:100%;border-collapse:collapse;background:var(--card);font-size:13px}}
  th,td{{border:1px solid var(--line);padding:7px 9px;text-align:left}}
  th{{background:#f1f4f8;color:var(--sub);font-weight:600}}
  .drow{{display:flex;align-items:center;gap:10px;margin:7px 0}}
  .dlabel{{width:130px;font-size:13px;flex:none}}
  .dbar{{flex:1;height:12px;background:#eef1f5;border-radius:6px;overflow:hidden}}
  .dbar span{{display:block;height:100%}}
  .dval{{width:30px;text-align:right;font-size:13px;color:var(--sub)}}
  .note{{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:12px 16px;font-size:13px;color:#7c5e10}}
  .note code{{background:#fef3c7;padding:1px 5px;border-radius:4px}}
  footer{{margin-top:40px;color:var(--sub);font-size:12px;text-align:center;border-top:1px solid var(--line);padding-top:16px}}
</style></head>
<body><div class="wrap">
<header>
  <h1>OpenRouter 免费大模型 · 筛选与推荐</h1>
  <div class="sub">生成时间 {esc(data['generated_at'])} ｜ 全量模型 {data['total_models']} 个 ｜ 免费模型 <b>{data['free_count']}</b> 个 ｜ 综合权重 推理{w['reasoning']:.0%}/能力{w['capability']:.0%}/热门{w['popularity']:.0%}</div>
</header>

<h2>工作流（6 步）</h2>
<div class="steps">{steps_html}</div>

<h2>最终推荐（综合 Top 3）</h2>
<div class="grid3">{rec_cards}</div>

<h2>各维度冠军</h2>
<table><thead><tr><th>维度</th><th>模型</th><th>ID</th></tr></thead><tbody>{champ_rows}</tbody></table>

<h2>四维分类分布</h2>
<div class="grid3">
  <div><b>推理级别</b>{reason_dist_html}</div>
  <div><b>免费活动时间</b>{window_dist_html}</div>
  <div><b>说明</b>
    <p class="sub" style="font-size:13px">永久免费 {window_tiers.get('永久免费',0)} 个；限时免费 {window_tiers.get('短期限免',0)+window_tiers.get('长期限免',0)+window_tiers.get('即将到期(<30天)',0)} 个。推理级别中「默认开启推理」{reason_tiers.get('默认开启推理',0)} 个为免费主力。</p>
  </div>
</div>

<h2>全部免费模型排名</h2>
<table><thead><tr><th>#</th><th>模型</th><th>综合</th><th>推理级</th><th>推理实力</th><th>能力</th><th>免费窗口</th><th>热门</th></tr></thead><tbody>{rank_rows}</tbody></table>

<h2>方法与口径</h2>
<div class="note">
  <b>免费判定</b>：<code>pricing.prompt</code> 与 <code>pricing.completion</code> 同时为 <code>"0"</code>。<br>
  <b>推理级别</b>：依据 API <code>reasoning</code> 字段（强制/默认开启/可选/无）+ 模型族启发。<br>
  <b>推理实力</b>：层级分与 <code>artificial_analysis.intelligence_index</code> 质量加权；无实测指数则封顶，避免小模型误判。<br>
  <b>模型能力</b>：上下文长度（对数）+ 多模态广度 + 基准指数 + 工具/结构化输出。<br>
  <b>免费活动时间</b>：<code>expiration_date</code> 区分 永久/长限免/短限免/临期(&lt;30天)；<code>created</code> 计上架时长。<br>
  <b>免费热门度（代理）</b>：公开 API 不含使用量，以「提供方数量」为主代理（托管越广=越可靠），叠加在线率与延迟。<br>
  <b>免费稳定性护栏</b>：稳定性作乘子（永久1.0/长限免0.95/短限免0.85/临期0.6/过期0.1），避免临期模型被优先推荐。
</div>

<footer>数据由 OpenRouter 公开 API 实时抓取 · 重跑 openrouter_free_models.py 即更新 · 本报告仅供选型参考</footer>
</div></body></html>"""

with open(HTML, "w", encoding="utf-8") as f:
    f.write(html_doc)
print("HTML 写出:", HTML)
