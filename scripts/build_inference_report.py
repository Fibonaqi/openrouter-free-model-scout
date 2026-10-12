#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读 inference_stability.json，生成自包含 HTML 报告（无外部依赖，离线可看）。

★ 两阶段分开出报告（各是各的）：
   阶段一：phase1_report.html —— 初筛快览（全部候选，一屏看完初步结论）
   阶段二：phase2_report.html —— 复筛可信结论（仅幸存者，含 95%CI / 峰谷 / 速度 / 推理分项）

用法：
  python build_inference_report.py --phase 1 [inference_stability.json] [phase1_report.html]
  python build_inference_report.py --phase 2 [inference_stability.json] [phase2_report.html]

表格规则：不含「原因」列；原因/处置合并进「备注」列；备注列宽 ~56%（覆盖原原因列空间），
靠加宽容器压行高，不砍解释内容。
"""
import json
import sys
import os
from datetime import datetime, timezone

C_SUCCESS = "#2e9e5b"
C_CAP = "#d9483b"
C_INTR = "#e08a1e"
C_LOGIC = "#2f6fb0"
GRADE_COLOR = {
    "A(值得推荐)": "#1f8a4c", "B(可用)": "#2f6fb0", "C(勉强)": "#e08a1e",
    "已被淘汰": "#b91c1c", "D(不推荐)": "#d9483b", "样本不足": "#8a8a8a", "不可测": "#6b7280",
}
GRADE_RANK = {"A(值得推荐)": 0, "B(可用)": 1, "C(勉强)": 2, "已被淘汰": 3,
              "D(不推荐)": 4, "样本不足": 5, "不可测": 6}

UNTEST_SOLUTION = {
    "region-blocked": (
        "区域锁",
        "提供方/平台对请求来源地做合规限制（实测换 eu 域仍 403，非护栏问题）。无法通过账户设置解除；"
        "唯一途径是改变请求出口地区，但违反 OpenRouter 条款并引入不可控故障面，不建议。"
        "处置：直接从候选排除，改用无地域限制模型。"),
    "agentic-only": (
        "限agentic",
        "该免费端点依官方条款仅允许在 agentic harness 中使用（实测加 tools 参数、改用 /responses 端点均仍 403）。"
        "处置：需接入 https://openrouter.ai/apps 列表中的 coding agent 或生产力应用才能调用；"
        "本脚本为直连 API，故不可测（非稳定性结论）。"),
    "guardrail-blocked": (
        "护栏拦截",
        "workspace 护栏（数据策略 / 提供方过滤）拦截。处置：前往 "
        "https://openrouter.ai/workspaces/default/guardrails 放开对应限制后即可测试。"),
    "no-endpoints": (
        "无端点",
        "当前路由下该模型无可用端点（可能已下线或该区域不可达）。处置：确认模型仍在目录内后重跑。"),
}


def untest_tag(reason):
    r = (reason or "")
    for key, (tag, note) in UNTEST_SOLUTION.items():
        if r.startswith(key):
            return tag, note
    return "拦截", "账号/平台侧拦截：" + r[:120] + "。处置：确认账户权限与护栏设置后重跑。"


# 不可测的具体原因「简写」——直接锚定问题，避免笼统写"不可测"。
UNTEST_SHORT = {
    "region-blocked": "区域锁（来源地合规限制）",
    "agentic-only": "只能 agentic 调用",
    "guardrail-blocked": "护栏拦截",
    "no-endpoints": "无可用端点",
}


def untest_short(reason):
    r = (reason or "")
    for key, short in UNTEST_SHORT.items():
        if r.startswith(key):
            return short
    return "账号/平台侧拦截"


def pct(v):
    return f"{v*100:.1f}%" if isinstance(v, (int, float)) else "—"


def ci_str(m, key):
    c = m.get(key)
    if isinstance(c, list) and len(c) == 2 and c[0] is not None:
        return f"[{c[0]*100:.0f}–{c[1]*100:.0f}%]"
    return ""


def grade_color(g):
    return GRADE_COLOR.get(g, "#8a8a8a")


def disp_grade(entry):
    """展示用评级：
    - 不可测 → 不可测（具体原因另在行内/备注标出）
    - 被闸门淘汰（初筛早停）→ 已被淘汰（问题锚定：前 N 次有效调用全爆满/零成功）
    - 通过闸门但有效样本 <10 → 样本不足（问题锚定：样本仅 N）
    """
    if entry.get("untestable"):
        return "不可测", GRADE_COLOR["不可测"]
    if entry.get("phase1_excluded"):
        return "已被淘汰", GRADE_COLOR["已被淘汰"]
    g = entry.get("grade", "-")
    return g, grade_color(g)


def long_note(entry, m, phase=1):
    """备注列：把「原因 + 处置 + 样本说明 + 剔除说明」全部并进一列。"""
    if entry.get("untestable"):
        tag, note = untest_tag(entry.get("untest_reason"))
        return f"<b>[{tag}]</b> " + note
    parts = []
    if entry.get("phase1_excluded"):
        ne = m.get("n_eff") or 0
        sr = m.get("success_rate"); cr = m.get("capacity_full_rate"); ir = m.get("interrupt_rate")
        detail = ""
        if sr is not None and cr is not None:
            detail = f"（实测 {ne} 次有效调用：成功率 {sr*100:.0f}% · 爆满 {cr*100:.0f}%"
            if ir is not None:
                detail += f" · 中断 {ir*100:.0f}%"
            detail += "）"
        parts.append("<b>[已被淘汰]</b> " + (entry.get("exclude_reason") or "") +
                     "：上游容量/稳定性不达标，前 5 次有效调用已全爆满/零成功→按闸门提前停批（省配额），"
                     "不再消耗复筛配额。结论已由闸门给出，非随机缺失" + detail + "。")
    ne = m.get("n_eff") or 0
    if ne < 10 and not entry.get("untestable") and not entry.get("phase1_excluded"):
        eff = m.get("calls_n") or ne
        parts.append(f"<b>[样本仅 {ne}]</b> 有效样本 <10，未达可信门槛；非早停淘汰，而是调用批次偏少/被网络或平台剔除所致。"
                     f"该批共 {eff} 次调用；若需补充样本请提高 --rounds 后重跑。")
    if entry.get("peak_risk"):
        parts.append("<b>[峰值风险]</b> 高峰窗爆满率显著高于低谷窗，峰值时段不建议依赖。")
    if phase == 2 and m.get("fail_upper95") is not None:
        parts.append(f"最坏情况断言：真实失败率 ≤ {m['fail_upper95']*100:.0f}%（95% 置信，n={ne}）。")
    net = m.get("network_error") or 0
    if net:
        parts.append(f"已剔除本机网络/代理故障 {net} 次（不计入模型指标）。")
    pl = m.get("platform_limited") or 0
    if pl:
        parts.append(f"已剔除平台自身限流 {pl} 次（不计入模型不稳定）。")
    if not parts:
        g = entry.get("grade", "")
        if g.startswith("A"):
            parts.append("两阶段合并达标。注意：A 级要求失败率≤2%，需 n≈150 且零失败才能证实；"
                         f"当前 n={ne} 属「未达证实门槛」，请结合 CI 判断。")
        else:
            parts.append("两阶段指标合并后判定；无剔除样本。")
    return " ".join(parts)


def logic_cell(m):
    d = m.get("logic_detail") or {}
    if not d:
        return pct(m.get("reasoning_correct_rate"))
    out = []
    for k in ("L1", "L2", "L3"):
        v = d.get(k)
        out.append(f"{k}:{'—' if v is None else str(int(round(v*100)))+'%'}")
    return " ".join(out)


def bar_row(metrics, width=430):
    succ = metrics.get("success_rate") or 0
    cap = metrics.get("capacity_full_rate") or 0
    intr = metrics.get("interrupt_rate") or 0
    logic = metrics.get("reasoning_correct_rate")
    logic = logic if logic is not None else 0
    nb, gap = 4, 6
    bw = width // nb - gap
    specs = [("成功", succ, C_SUCCESS), ("爆满", cap, C_CAP), ("中断", intr, C_INTR), ("推理", logic, C_LOGIC)]
    bars = []
    for i, (lab, val, col) in enumerate(specs):
        x = i * (bw + gap)
        w = max(2, int(val * bw))
        bars.append(
            '<g transform="translate(' + str(x) + ',0)">' +
            '<rect width="' + str(bw) + '" height="12" rx="2" fill="#eee"/>' +
            '<rect width="' + str(w) + '" height="12" rx="2" fill="' + col + '"/>' +
            '<text x="2" y="9.5" font-size="8.5" fill="#222">' + lab + '</text>' +
            '<text x="' + str(bw - 2) + '" y="9.5" font-size="8.5" fill="#222" text-anchor="end">' + str(int(val * 100)) + '%</text>' +
            '</g>')
    return '<svg width="' + str(width) + '" height="14" viewBox="0 0 ' + str(width) + ' 14">' + "".join(bars) + '</svg>'


def dashboard_svg(models_items, get_metrics):
    rows = []
    y = 0
    rowh = 22
    for mid, entry in models_items:
        metrics = get_metrics(entry)
        g, color = disp_grade(entry)
        if metrics is None or entry.get("untestable"):
            tag = untest_short(entry.get("untest_reason")) if entry.get("untestable") else "—"
            rows.append(
                '<g transform="translate(0,' + str(y) + ')">' +
                '<rect x="0" y="0" width="900" height="' + str(rowh - 3) + '" rx="3" fill="#f3f4f6"/>' +
                '<text x="6" y="14" font-size="11" fill="#374151">' + mid[:44] + '</text>' +
                '<text x="470" y="14" font-size="10.5" fill="#6b7280">⛔ 不可测 · ' + tag + '</text>' +
                '</g>')
            y += rowh
            continue
        nm = mid if len(mid) <= 42 else mid[:40] + "…"
        rows.append(
            '<g transform="translate(0,' + str(y) + ')">' +
            '<rect x="0" y="2" width="11" height="11" rx="2" fill="' + color + '"/>' +
            '<text x="16" y="12.5" font-size="11" fill="#111">' + nm + '</text>' +
            '<g transform="translate(400,3)">' + bar_row(metrics) + '</g>' +
            '<text x="900" y="12.5" font-size="10.5" fill="' + color + '" text-anchor="end">' + g + '</text>' +
            '</g>')
        y += rowh
    return ('<svg width="910" height="' + str(max(y, 16)) + '" viewBox="0 0 910 ' + str(max(y, 16)) +
            '" font-family="monospace">' + "".join(rows) + '</svg>')


def phase1_table(models_items, get_metrics):
    """阶段一：快览表。无「原因」列（原因并入备注）。"""
    head = ("<tr><th>模型</th><th>评级</th><th>成功率</th><th>爆满</th><th>中断</th>"
            "<th>TTFT_P95</th><th>推理</th><th>样本</th><th>备注（含原因与处置）</th></tr>")
    body = []
    for mid, entry in models_items:
        g, color = disp_grade(entry)
        m = get_metrics(entry)
        if entry.get("untestable"):
            short = untest_short(entry.get("untest_reason"))
            body.append(
                '<tr class="untest"><td class="mid">' + mid + '</td>' +
                '<td><span style="color:#6b7280;font-weight:700">不可测</span><br>'
                '<span class="rsn">' + short + '</span></td>' +
                '<td colspan="6" class="dash">—</td>' +
                '<td class="note">' + long_note(entry, {}, 1) + '</td></tr>')
            continue
        if entry.get("phase1_excluded"):
            body.append(
                '<tr class="excl"><td class="mid">' + mid + '</td>' +
                '<td style="color:' + color + ';font-weight:700">已被淘汰</td>' +
                '<td colspan="6" class="dash" style="color:' + C_CAP + ';font-weight:600">已被淘汰</td>' +
                '<td class="note">' + long_note(entry, m or {}, 1) + '</td></tr>')
            continue
        ttft = (str(int(m.get("ttft_p95_ms"))) + "ms") if m.get("ttft_p95_ms") is not None else "—"
        logic = pct(m.get("reasoning_correct_rate")) if m.get("reasoning_correct_rate") is not None else "—"
        body.append(
            "<tr><td class=\"mid\">" + mid + "</td>" +
            '<td style="color:' + color + ';font-weight:700">' + g + "</td>" +
            "<td>" + pct(m.get("success_rate")) + "</td>" +
            '<td style="color:' + C_CAP + '">' + pct(m.get("capacity_full_rate")) + "</td>" +
            '<td style="color:' + C_INTR + '">' + pct(m.get("interrupt_rate")) + "</td>" +
            "<td>" + ttft + "</td>" +
            "<td>" + logic + "</td>" +
            "<td>" + str(m.get("n_eff") or 0) + "</td>" +
            '<td class="note">' + long_note(entry, m, 1) + "</td></tr>")
    return '<table class="tbl">' + head + "".join(body) + "</table>"


def phase2_table(models_items):
    """阶段二：可信表（含 95%CI / 速度 / 推理分项 / 峰谷）。无「原因」列。"""
    head = ("<tr><th>模型</th><th>评级</th><th>成功率 (95%CI)</th><th>最坏失败率</th><th>爆满</th><th>中断</th>"
            "<th>TTFT P50/P95</th><th>输出速度</th><th>完整输出</th><th>推理 L1/L2/L3</th>"
            "<th>峰/谷爆满</th><th>样本</th><th>备注</th></tr>")
    body = []
    for mid, entry in models_items:
        g, color = disp_grade(entry)
        m = entry
        _p50 = m.get("ttft_p50_ms") if m.get("ttft_p50_ms") is not None else m.get("ttft_median_ms")
        p50 = f"{int(_p50)}" if _p50 is not None else "—"
        p95 = f"{int(m['ttft_p95_ms'])}" if m.get("ttft_p95_ms") is not None else "—"
        cps = f"{m['throughput_cps']:.0f} 字/s" if m.get("throughput_cps") else "—"
        pk = pct(m.get("peak_capacity_rate")) if m.get("peak_capacity_rate") is not None else "—"
        op = pct(m.get("offpeak_capacity_rate")) if m.get("offpeak_capacity_rate") is not None else "—"
        fu = f"≤{m['fail_upper95']*100:.0f}%" if m.get("fail_upper95") is not None else "—"
        body.append(
            "<tr><td class=\"mid\">" + mid + "</td>" +
            '<td style="color:' + color + ';font-weight:700">' + g + "</td>" +
            "<td>" + pct(m.get("success_rate")) + " <span class='ci'>" + ci_str(m, "success_ci95") + "</span></td>" +
            "<td>" + fu + "</td>" +
            '<td style="color:' + C_CAP + '">' + pct(m.get("capacity_full_rate")) + "</td>" +
            '<td style="color:' + C_INTR + '">' + pct(m.get("interrupt_rate")) + "</td>" +
            "<td>" + p50 + " / " + p95 + "ms</td>" +
            "<td>" + cps + "</td>" +
            "<td>" + pct(m.get("output_validity")) + "</td>" +
            "<td>" + logic_cell(m) + "</td>" +
            "<td>" + pk + " / " + op + "</td>" +
            "<td>" + str(m.get("n_eff") or 0) + "</td>" +
            '<td class="note">' + long_note(entry, m, 2) + "</td></tr>")
    return '<table class="tbl">' + head + "".join(body) + "</table>"


METRIC_TABLE = """<table class="tbl">
<tr><th>指标</th><th>通俗解释（它回答什么问题）</th><th>观测方法（怎么算）</th><th>取自哪些调用</th></tr>
<tr><td>成功率</td><td>发出的请求有多大比例真拿到可用回答？</td><td>200 且无中断/未拦截且 finish∈{stop,tool_calls,length} 且内容非空</td><td>全部有效调用</td></tr>
<tr><td>爆满率</td><td>有多大比例被上游容量不足直接拒绝？（你体感的"爆满"）</td><td>429 带 provider_code / 503 / unavailable·upstream·overload / 流中 finish_reason:"error"</td><td>全部有效调用</td></tr>
<tr><td>中断率</td><td>有多大比例是已开始出字却中途断掉？</td><td>连接重置 / 流截断 / 读超时 / 首 token 后 5xx / 无 finish_reason 就结束</td><td>全部有效调用</td></tr>
<tr><td>首字时延 TTFT</td><td>多久开始出第一个字？（越快越跟手）</td><td>发请求到收到首个内容片段，取 P50 / P95</td><td>仅成功调用</td></tr>
<tr><td>输出速度</td><td>出字有多快？</td><td>输出字符数 ÷ 总时长（字/秒，中位数）</td><td>仅成功调用</td></tr>
<tr><td>完整输出率</td><td>有多大比例完整说完而非被截断？</td><td>finish_reason=="stop" 的比例</td><td>全部有效调用</td></tr>
<tr><td>推理正确率</td><td>它会不会思考？还是只会流畅地胡说？</td><td>3 道推理题答案正误比例（取最先出现的答案词判定）</td><td>仅 L1/L2/L3</td></tr>
<tr><td>峰谷差异</td><td>是不是只在高峰期才崩？</td><td>两窗口爆满率之差 &gt; 10 个百分点即打标</td><td>跨阶段合并</td></tr>
</table>
<p class="sub"><b>推理题三道</b>：L1 三段论传递（正解 yes）｜L2 球棒与球陷阱题（正解 $0.05）｜L3 不可逆推四段论（正解 no）。
判定取<b>最先出现的答案词</b>，因模型通常"先给答案再解释"。<b>推理正确率 &lt;90% 直接判 D</b>——能用不等于好用。</p>"""

SAMPLE_TABLE = """<table class="tbl">
<tr><th>样本 n</th><th>零失败时可断言的失败率上界</th><th>观测 90% 成功率时的 95%CI</th><th>能做什么</th></tr>
<tr><td>10</td><td>≤30%</td><td>约 [60%, 98%]</td><td>只能粗筛，不足以支撑结论</td></tr>
<tr class="hi"><td>12（阶段一）</td><td>≤25%</td><td>约 [65%, 99%]</td><td>检出量级级故障，用于淘汰</td></tr>
<tr class="hi"><td>30（幸存者合并）</td><td>≤10%</td><td>约 [74%, 97%]</td><td>给出相对可信的结论与区间</td></tr>
<tr><td>60</td><td>≤5%</td><td>约 [80%, 95%]</td><td>接近确认「基本稳定」</td></tr>
<tr><td>150</td><td>≤2%</td><td>约 [85%, 94%]</td><td>才够证实 A 级（成本过高）</td></tr>
</table>
<p class="sub">依据：比率用 <b>Wilson 95% 置信区间</b>；零失败用 <b>3/n 法则</b>。
<b>诚实边界</b>：可行样本无法证实 A 级所需的失败率 ≤2%（需 n≈150 且零失败），故 A 级须标注「未达证实门槛」；
样本再往上区间收窄变慢且显著拉长测试时长，故采用「淘汰用 12、结论用 30」的两级设计。</p>"""

CSS = """
 body{font-family:-apple-system,'Segoe UI',Roboto,'Microsoft YaHei',sans-serif;margin:18px;color:#1f2937;background:#fff}
 h1{font-size:20px;margin:0 0 2px} h2{font-size:15px;margin-top:20px;border-left:4px solid #2f6fb0;padding-left:8px}
 h3{font-size:13px;margin:12px 0 4px;color:#374151}
 .sub{color:#6b7280;font-size:12px;margin:4px 0 8px;line-height:1.5}
 .cards{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
 .card{background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:6px 12px;min-width:96px}
 .card .n{font-size:19px;font-weight:700;line-height:1.1} .card .l{font-size:11px;color:#6b7280}
 .tbl{border-collapse:collapse;width:100%;font-size:12px;margin-top:6px;table-layout:fixed}
 .tbl th,.tbl td{border:1px solid #e5e7eb;padding:4px 6px;text-align:left;vertical-align:top;white-space:nowrap}
 .tbl th{background:#f1f5f9;font-weight:600}
 .tbl tr.untest{background:#fafafa} .tbl tr.hi{background:#f0fdf4}
 .tbl tr.excl{background:#fef2f2}
 .tbl td.mid{font-family:ui-monospace,Consolas,monospace;font-size:11px;white-space:normal;word-break:break-all;width:14%}
 .tbl td.note{width:56%;white-space:normal;font-size:11.5px;line-height:1.4;color:#4b5563}
 .tbl td.dash{color:#9ca3af}
 .tbl td .rsn{display:block;font-size:10.5px;color:#6b7280;margin-top:1px;font-weight:400}
 .ci{color:#6b7280;font-size:10.5px}
 .legend{font-size:11.5px;color:#555;margin:4px 0}
 .legend span{display:inline-block;margin-right:12px}
 .dot{display:inline-block;width:9px;height:9px;border-radius:2px;vertical-align:middle;margin-right:4px}
 .note{background:#fff7ed;border:1px solid #fed7aa;border-radius:6px;padding:8px 12px;font-size:12px;color:#9a3412;line-height:1.5}
 .blk{background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:8px 12px;font-size:12px}
 .guide{background:#f0f9ff;border:1px solid #bae6fd;border-radius:6px;padding:8px 12px;font-size:12px;line-height:1.55}
 .guide b{color:#075985} .guide code{background:#e0f2fe;padding:0 4px;border-radius:3px}
 .meth{font-size:11.5px;color:#4b5563;line-height:1.55}
 .pend{background:#f8fafc;border:1px dashed #cbd5e1;border-radius:6px;padding:10px 12px;font-size:12px;color:#475569;line-height:1.6}
"""


def build(data, phase):
    models = data.get("models", {})
    cycle = data.get("cycle_days", 3)
    bl = data.get("blacklist", {}) or {}
    items = list(models.items())
    items.sort(key=lambda kv: (GRADE_RANK.get(kv[1].get("grade", "不可测"), 5),
                               -(kv[1].get("success_rate") or 0), kv[0]))

    n_total = len(items)
    n_untest = sum(1 for _, e in items if e.get("untestable"))
    n_excl = sum(1 for _, e in items if e.get("phase1_excluded"))
    n_pass = sum(1 for _, e in items if e.get("grade") in ("A(值得推荐)", "B(可用)", "C(勉强)"))
    n_black = len(bl.get("blacklisted") or {})
    calls_total = sum(e.get("calls_n") or 0 for _, e in items)
    eff_total = sum(e.get("n_eff") or 0 for _, e in items)
    net_total = sum(e.get("network_error") or 0 for _, e in items)
    plat_total = sum(e.get("platform_limited") or 0 for _, e in items)
    eff_rate = (eff_total / calls_total * 100) if calls_total else 0
    phase2_done = bool(data.get("phase2_done"))

    cards = (f'<div class="cards">'
             f'<div class="card"><div class="n">{n_total}</div><div class="l">候选模型</div></div>'
             f'<div class="card"><div class="n" style="color:{C_SUCCESS}">{n_pass}</div><div class="l">'
             f'{"最终可用" if phase == 2 else "初筛通过"}</div></div>'
             f'<div class="card"><div class="n" style="color:{C_CAP}">{n_excl}</div><div class="l">初筛淘汰</div></div>'
             f'<div class="card"><div class="n" style="color:#6b7280">{n_untest}</div><div class="l">不可测</div></div>'
             f'<div class="card"><div class="n" style="color:#b91c1c">{n_black}</div><div class="l">已拉黑</div></div>'
             f'<div class="card"><div class="n">{eff_rate:.0f}%</div><div class="l">有效测试率</div></div></div>')

    legend = (f'<div class="legend">'
              f'<span><i class="dot" style="background:{C_SUCCESS}"></i>成功率</span>'
              f'<span><i class="dot" style="background:{C_CAP}"></i>爆满率</span>'
              f'<span><i class="dot" style="background:{C_INTR}"></i>中断率</span>'
              f'<span><i class="dot" style="background:{C_LOGIC}"></i>推理正确率</span>'
              f'<span style="color:#6b7280">｜总调用 {calls_total} ｜有效样本 {eff_total} ｜已剔除：网络故障 {net_total} · 平台限流 {plat_total}</span>'
              f'</div>')

    if bl.get("blacklisted"):
        rows = []
        for mid, since in bl["blacklisted"].items():
            fd = ((bl.get("models") or {}).get(mid) or {}).get("fail_dates", [])
            rows.append("<li><b>" + mid + "</b> — 拉黑于 " + str(since) + " ｜ 失败测试日：" + ", ".join(fd) + "</li>")
        blacklist_html = ('<div class="blk">⛔ 当前 ' + str(n_black) + ' 个模型被拉黑（连续 ' + str(cycle) +
                          ' 测试日失败）。即便仍是免费类型，也默认排除出测试名单。<ul>' + "".join(rows) + "</ul></div>")
    else:
        blacklist_html = ('<div class="blk">✅ 暂无拉黑模型（需连续 ' + str(cycle) +
                          ' 个测试日失败才拉黑；被账号/区域/guardrail/agentic 拦截的模型不计入）。</div>')

    guide = ('<div class="guide">'
             '<b>护栏拦截</b>（可由你解除）：workspace 护栏过滤提供方/数据策略 → 到 '
             '<code>https://openrouter.ai/workspaces/default/guardrails</code> 放开即可恢复测试。<br>'
             '<b>数据区域限制</b>（可由你解除）：护栏 <code>allowed_data_regions</code> 会在推理前以 403 拒绝；'
             '需 Business/Enterprise 计划，或改走 <code>https://eu.openrouter.ai</code> / <code>https://us.openrouter.ai</code>。<br>'
             '<b>区域锁</b>（不可解除）：提供方对来源地合规限制，与护栏无关，换域实测仍 403。只能排除或换模型。<br>'
             '<b>限 agentic</b>（不可解除）：官方条款限定免费端点仅可在 agentic harness 中使用，直连 API 恒 403。'
             '需接入 <code>https://openrouter.ai/apps</code> 中的应用；本脚本直连，故标不可测（不计稳定、不进黑名单）。'
             '</div>')

    meth = (f'<div class="meth">'
            f'• 两阶段分离：<b>阶段一</b>初筛全量（{12} 次/模型，单窗口）→ 触阈值即淘汰；'
            f'<b>阶段二</b>仅复筛通过者于另一窗口（18 次），合并 n=30 算峰谷。<b>两份报告各出一份，互不混合。</b><br>'
            f'• 评级：A=值得推荐(稳定且会推理) B=可用 C=勉强 D=不推荐；推理正确率&lt;90%直接D；'
            f'被闸门淘汰者记<b>「已被淘汰」</b>（前 5 次有效调用全爆满/零成功→提前停批），'
            f'通过闸门但 n_eff&lt;10 者记<b>「样本不足」</b>（附实测样本数锚定）。<br>'
            f'• 无效测试消除：本机网络/代理故障与平台自身限流均<b>剔除</b>并重试，不计入任何指标；被拦截模型首次调用即早停；'
            f'前 5 次「全爆满/零成功」早停 → 把配额留给有效模型。<br>'
            f'• 撞输出上限（finish=length）不计为失败，另由「完整输出率 / 截断率」单独追踪。<br>'
            f'• 黑名单：连续 {cycle} 个测试日终判D→拉黑，未来默认排除（除非用户主动要求）。<br>'
            f'• 限额：免费:free 买币&lt;10→20次/分·50次/天；≥10→1000/分·1000/天。顺序调用+间隔≥1.2s。'
            f'</div>')

    if phase == 1:
        title = "OpenRouter 免费模型 · 阶段一 初筛快览报告"
        tagline = ("本报告只覆盖 <b>阶段一（初筛）</b>。目的：几十秒内形成初步判断——谁可用、谁淘汰、谁不可测。<br>"
                   "排序：可用置顶 · 淘汰居中 · 不可测沉底。此处只给初步结论与是否进复筛；"
                   "相对可信的最终结论见 <code>phase2_report.html</code>。")
        quick_html = dashboard_svg(items, lambda e: e) + phase1_table(items, lambda e: e)
        sections = (f'<h2>① 初筛快览（全量候选）</h2><p class="sub">{tagline}</p>{quick_html}'
                    f'<h2>② 黑名单状态</h2>{blacklist_html}'
                    f'<h2>③ 需你设置（拦截处置指引）</h2>{guide}'
                    f'<h2>④ 测试指标与方法对照（明示）</h2>'
                    f'<h3>4.1 核心方法：同一批调用，多口径观测</h3>'
                    f'<p class="sub">所有指标来自<b>同一批真实调用</b>，<b>不为每个指标单独发请求</b>。</p>{METRIC_TABLE}'
                    f'<h3>4.2 样本量与结论可信度</h3>{SAMPLE_TABLE}'
                    f'<h2>⑤ 方法学摘要</h2>{meth}')
    else:
        title = "OpenRouter 免费模型 · 阶段二 复筛可信报告"
        if phase2_done:
            surv = [(m, e) for m, e in items if not e.get("untestable") and not e.get("phase1_excluded")]
            tbl = phase2_table(surv)
            finals = [e for _, e in surv if (e.get("grade") or "").startswith(("A", "B", "C"))]
            concl = (f'<div class="guide">最终可信结论：{len(finals)} 个模型达 A/B/C（可用及以上）。'
                     f'本表在另一时间窗对幸存者再发 18 次，与初筛合并为 n=30，给出 95% 置信区间、'
                     f'峰谷爆满对比与推理分项。</div>')
            sections = (f'<h2>① 复筛可信结论（仅初筛通过者）</h2>'
                        f'<p class="sub">仅初筛通过者：在另一窗口再发 18 次（与初筛间隔 ≥30 分钟），两阶段合并为 n=30。</p>'
                        f'{concl}{tbl}'
                        f'<h2>② 黑名单状态</h2>{blacklist_html}'
                        f'<h2>③ 测试指标与方法对照</h2>'
                        f'<h3>3.1 核心方法：同一批调用，多口径观测</h3>{METRIC_TABLE}'
                        f'<h3>3.2 样本量与结论可信度</h3>{SAMPLE_TABLE}'
                        f'<h2>④ 方法学摘要</h2>{meth}')
        else:
            sections = ('<h2>① 复筛可信结论（仅初筛通过者）</h2>'
                        '<div class="pend">⏳ <b>阶段二复筛尚未运行。</b><br>'
                        '请先看阶段一报告 <code>phase1_report.html</code>；待阶段二跑完（与初筛间隔 ≥30 分钟，'
                        '在另一窗口对幸存者再发 18 次），本报告将自动填充 95% 置信区间、峰谷对比与最终评级。<br>'
                        '手动触发：<code>python inference_stability_test.py --phase 2 --window peak --rounds2 18</code>'
                        '</div>'
                        f'<h2>② 黑名单状态</h2>{blacklist_html}'
                        f'<h2>③ 方法学摘要</h2>{meth}')

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title><style>{CSS}</style></head><body>
<h1>{title}</h1>
<div class="sub">生成时间 {data.get("generated_at", "")} ｜ 黑名单周期：连续 {cycle} 个测试日失败即拉黑 ｜ 仅免费模型</div>
{cards}
{legend}
{sections}
<div class="sub" style="margin-top:16px">生成器：build_inference_report.py --phase {phase} ｜ 阶段一与阶段二为独立报告，互不混合。</div>
</body></html>"""
    return html, dict(n_total=n_total, n_pass=n_pass, n_excl=n_excl, n_untest=n_untest,
                      n_black=n_black, eff_rate=eff_rate)


def main():
    argv = sys.argv[1:]
    phase = 1
    if "--phase" in argv:
        i = argv.index("--phase")
        phase = int(argv[i + 1])
        del argv[i:i + 2]
    src = argv[0] if len(argv) > 0 else "inference_stability.json"
    out = argv[1] if len(argv) > 1 else (f"phase{phase}_report.html")
    if not os.path.exists(src):
        print("[ERROR] 找不到 " + src, file=sys.stderr)
        sys.exit(2)
    data = json.load(open(src, encoding="utf-8"))
    html, s = build(data, phase)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已写出阶段{phase}报告 {out}（候选 {s['n_total']} · 通过 {s['n_pass']} · 淘汰 {s['n_excl']} · "
          f"不可测 {s['n_untest']} · 拉黑 {s['n_black']} · 有效测试率 {s['eff_rate']:.0f}%）")


if __name__ == "__main__":
    main()
