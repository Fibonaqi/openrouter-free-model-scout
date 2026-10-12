#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段一 · 实时动态看板（phase1_live.html）

用途：拿到免费名单后立刻生成"框架"，然后每测完一次调用就重写一次本页面；
页面自带 <meta refresh>，用浏览器开着即可看到「某模型正在被动态测试」的过程与状态，
直到阶段一全部跑完。

两种用法：
  ① 测试脚本内部调用（边测边刷）：
       from build_live_report import build_live_html
       build_live_html(ids, names, state, out_path, rounds=12, window="offpeak")
     其中 state = {mid: {"calls": [...], "status": "waiting|running|done"}}
  ② 离线快照（从已落盘数据还原一次）：
       python build_live_report.py [free_models_classified.json] [inference_stability.json] [phase1_live.html]

页面完全自包含（无 CDN、无外部请求），可直接双击用浏览器打开。
"""
import json
import sys
import os
from datetime import datetime, timezone

C_OK = "#2e9e5b"
C_CAP = "#d9483b"
C_INTR = "#e08a1e"
C_DIM = "#8a8a8a"
C_RUN = "#2f6fb0"

# 与测试脚本保持一致的状态判定
UNTEST_LABEL = {
    "region-blocked": "区域锁", "agentic-only": "只能agentic调用",
    "guardrail-blocked": "护栏拦截", "no-endpoints": "无端点",
}


def _tag_of(c):
    if c.get("untestable"):
        return "untestable"
    if c.get("ok"):
        return "ok"
    if c.get("capacity"):
        return "capacity"
    if c.get("platform"):
        return "platform"
    if c.get("interrupted"):
        return "interrupt"
    return "fail"


def _counts(calls):
    n = len(calls)
    ok = sum(1 for c in calls if c.get("ok"))
    cap = sum(1 for c in calls if c.get("capacity"))
    intr = sum(1 for c in calls if c.get("interrupted") and not c.get("capacity"))
    plat = sum(1 for c in calls if c.get("platform") and not c.get("capacity"))
    net = sum(1 for c in calls if (c.get("err") or "").startswith("[network]"))
    unt = sum(1 for c in calls if c.get("untestable"))
    # 有效样本 = 总数 - 平台限流 - 网络故障 - 不可测
    eff = n - plat - net - unt
    ttfts = [c.get("ttft_ms") for c in calls if c.get("ok") and c.get("ttft_ms") is not None]
    last_ttft = ttfts[-1] if ttfts else None
    logic = [c for c in calls if c.get("reasoning_correct") is not None]
    logic_ok = sum(1 for c in logic if c.get("reasoning_correct"))
    return dict(n=n, ok=ok, cap=cap, intr=intr, plat=plat, net=net, unt=unt, eff=eff,
                last_ttft=last_ttft, logic_n=len(logic), logic_ok=logic_ok)


def _bar(val, total, color, width=96):
    total = max(total, 1)
    w = max(1, int(val / total * width)) if val else 0
    return ('<svg width="' + str(width) + '" height="10" viewBox="0 0 ' + str(width) + ' 10">'
            '<rect width="' + str(width) + '" height="10" rx="2" fill="#eceff3"/>'
            '<rect width="' + str(w) + '" height="10" rx="2" fill="' + color + '"/></svg>')


def build_live_html(ids, names, state, out, rounds=12, window="offpeak",
                    started=None, now=None, phase_label="阶段一 · 初筛",
                    finished=False, note=""):
    """把当前测试状态渲染成自包含的实时看板 HTML。"""
    now = now or datetime.now()
    started = started or now
    elapsed = int((now - started).total_seconds())

    rows = []
    n_done = n_pass = n_excl = n_unt = 0
    call_total = eff_total = net_total = io = 0
    for i, mid in enumerate(ids, 1):
        st = state.get(mid) or {}
        calls = st.get("calls") or []
        status = st.get("status") or ("done" if st.get("done") else "waiting")
        c = _counts(calls)
        call_total += c["n"]
        eff_total += max(0, c["eff"])
        net_total += c["net"]
        nm = names.get(mid, mid)
        short = nm if len(nm) <= 34 else nm[:32] + "…"

        # 状态标签
        if status == "done":
            n_done += 1
            if c["unt"] > 0 and c["eff"] == 0:
                n_unt += 1
                ureason = ""
                for cc in calls:
                    if cc.get("untestable") and cc.get("untest_reason"):
                        ureason = UNTEST_LABEL.get(cc["untest_reason"], "拦截")
                        break
                stat_html = '<span class="pill p-unt">不可测 · ' + (ureason or "拦截") + '</span>'
            elif c["ok"] == 0 or c["cap"] / max(c["eff"], 1) > 0.2:
                n_excl += 1
                stat_html = '<span class="pill p-excl">淘汰</span>'
            else:
                n_pass += 1
                stat_html = '<span class="pill p-pass">完成·待复筛</span>'
        elif status == "running":
            stat_html = '<span class="pill p-run"><i class="spin"></i>测试中</span>'
        else:
            stat_html = '<span class="pill p-wait">待测</span>'

        done_shot = c["n"]
        prog = min(100, int(done_shot / max(rounds, 1) * 100))
        ttft = f'{int(c["last_ttft"])}ms' if c["last_ttft"] is not None else "—"
        logic = f'{c["logic_ok"]}/{c["logic_n"]}' if c["logic_n"] else "—"
        row_cls = ' class="row-run"' if status == "running" else ""
        rows.append(
            '<tr' + row_cls + '><td class="idx">' + str(i) + '</td>'
            '<td class="mid">' + mid + '<div class="nm">' + nm + '</div></td>'
            '<td>' + stat_html + '</td>'
            '<td class="progcell">' + _bar(done_shot, rounds, C_RUN) +
            '<div class="pl">' + str(done_shot) + '/' + str(rounds) +
            (' · 已停' if status == "done" and done_shot < rounds else '') + '</div></td>'
            '<td class="num" style="color:' + C_OK + '">' + str(c["ok"]) + '</td>'
            '<td class="num" style="color:' + C_CAP + '">' + str(c["cap"]) + '</td>'
            '<td class="num" style="color:' + C_INTR + '">' + str(c["intr"]) + '</td>'
            '<td class="num">' + str(max(0, c["eff"])) + '</td>'
            '<td class="num">' + ttft + '</td>'
            '<td class="num">' + logic + '</td></tr>')

        for cc in calls:
            if _tag_of(cc) == "ok":
                io += 1
    pct_done = int(n_done / max(len(ids), 1) * 100)
    eta = ""
    if 0 < n_done < len(ids) and elapsed > 0:
        per = elapsed / max(n_done, 1)
        eta = f'预计还需 ~{int(per*(len(ids)-n_done)/60)} 分钟'

    mode = "已结束" if finished else "进行中"
    head_note = note or ("本页每 3 秒自动刷新；测试脚本每完成一次调用即重写本文件。"
                         "开着此页即可看到逐个模型的动态测试过程与状态。")

    html = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="META">
<title>阶段一 实时测试看板</title>
<style>
 body{font-family:-apple-system,'Segoe UI',Roboto,'Microsoft YaHei',sans-serif;margin:18px;color:#1f2937;background:#fff}
 h1{font-size:19px;margin:0 0 2px}
 .sub{color:#6b7280;font-size:12px;margin:4px 0 10px;line-height:1.5}
 .top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 .live{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#2f6fb0;font-weight:600}
 .dot{width:8px;height:8px;border-radius:50%;background:#2f6fb0;animation:bl 1s infinite}
 @keyframes bl{0%,100%{opacity:1}50%{opacity:.25}}
 .cards{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
 .card{background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:6px 12px;min-width:92px}
 .card .n{font-size:19px;font-weight:700;line-height:1.1}.card .l{font-size:11px;color:#6b7280}
 .pbar{height:12px;background:#eceff3;border-radius:6px;overflow:hidden;margin:6px 0 2px}
 .pbar>i{display:block;height:100%;background:linear-gradient(90deg,#2f6fb0,#4a9be0)}
 .tbl{border-collapse:collapse;width:100%;font-size:12px;margin-top:8px;table-layout:fixed}
 .tbl th,.tbl td{border:1px solid #e5e7eb;padding:4px 6px;text-align:left;vertical-align:middle;white-space:nowrap}
 .tbl th{background:#f1f5f9;font-weight:600;font-size:11.5px}
 .tbl td.idx{color:#9ca3af;width:26px;text-align:center}
 .tbl td.mid{font-family:ui-monospace,Consolas,monospace;font-size:11px;white-space:normal;word-break:break-all;width:26%}
 .tbl td.mid .nm{color:#6b7280;font-size:10.5px;font-family:inherit;margin-top:1px}
 .tbl td.num{text-align:right;font-variant-numeric:tabular-nums;width:52px}
 .tbl td.progcell{width:120px}
 .pl{font-size:10.5px;color:#6b7280;margin-top:1px}
 tr.row-run{background:#f0f7ff}
 .pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;border:1px solid transparent;white-space:nowrap}
 .p-wait{background:#f3f4f6;color:#6b7280;border-color:#e5e7eb}
 .p-run{background:#eaf3fb;color:#2f6fb0;border-color:#bcdcff;font-weight:600}
 .p-pass{background:#eafaf0;color:#1f8a4c;border-color:#bfe8cf;font-weight:600}
 .p-excl{background:#fdeceb;color:#c0392b;border-color:#f6c9c4;font-weight:600}
 .p-unt{background:#f3f4f6;color:#6b7280;border-color:#e5e7eb}
 .spin{display:inline-block;width:9px;height:9px;margin-right:4px;border:2px solid #bcdcff;border-top-color:#2f6fb0;border-radius:50%;animation:sp .7s linear infinite;vertical-align:-1px}
 @keyframes sp{to{transform:rotate(360deg)}}
 .legend{font-size:11.5px;color:#555;margin:6px 0}
 .legend span{display:inline-block;margin-right:12px}
 .bdot{display:inline-block;width:9px;height:9px;border-radius:2px;vertical-align:middle;margin-right:4px}
</style></head><body>
<div class="top"><h1>OpenRouter 免费模型 · LABEL 实时测试看板</h1>
 <span class="live"><i class="dot"></i>MODE</span></div>
<div class="sub">窗口 <b>WINDOW</b> ｜ 每模型 ROUNDS 次 ｜ 已运行 TIME ｜ ETA ｜ 生成于 GENAT<br>NOTE</div>
<div class="pbar"><i style="width:PCTDONE%"></i></div>
<div class="sub">总体进度 PCTDONE%（NDONE/NTOTAL 个模型已测完）</div>
<div class="cards">
 <div class="card"><div class="n">NTOTAL</div><div class="l">候选模型</div></div>
 <div class="card"><div class="n" style="color:C_RUN">NRUN</div><div class="l">已测完</div></div>
 <div class="card"><div class="n" style="color:C_OK">NPASS</div><div class="l">初步通过</div></div>
 <div class="card"><div class="n" style="color:C_CAP">NEXCL</div><div class="l">初步淘汰</div></div>
 <div class="card"><div class="n" style="color:#6b7280">NUNT</div><div class="l">不可测</div></div>
 <div class="card"><div class="n">CALLTOT</div><div class="l">累计调用</div></div>
</div>
<div class="legend">
 <span><i class="bdot" style="background:C_OK"></i>成功</span>
 <span><i class="bdot" style="background:C_CAP"></i>爆满</span>
 <span><i class="bdot" style="background:C_INTR"></i>中断</span>
 <span style="color:#6b7280">｜有效样本 EFFTOT ｜已剔除网络故障 NETTOT</span>
</div>
<table class="tbl">
<tr><th>#</th><th>模型</th><th>状态</th><th>进度</th><th>成功</th><th>爆满</th><th>中断</th><th>样本</th><th>最近TTFT</th><th>推理</th></tr>
ROWS
</table>
<div class="sub" style="margin-top:12px">说明：本页为「过程看板」，只反映当前累计状态，不作为最终结论；
最终结论以阶段一快览报告（phase1_report.html）与阶段二可信报告（phase2_report.html）为准。</div>
</body></html>"""
    html = (html.replace("META", "3" if not finished else "30")
            .replace("LABEL", phase_label).replace("MODE", mode)
            .replace("WINDOW", window).replace("ROUNDS", str(rounds))
            .replace("TIME", f"{elapsed//60} 分 {elapsed%60} 秒")
            .replace("ETA", eta).replace("GENAT", now.strftime("%Y-%m-%d %H:%M:%S"))
            .replace("NOTE", head_note).replace("PCTDONE", str(pct_done))
            .replace("NDONE", str(n_done)).replace("NTOTAL", str(len(ids)))
            .replace("NRUN", str(n_done)).replace("NPASS", str(n_pass))
            .replace("NEXCL", str(n_excl)).replace("NUNT", str(n_unt))
            .replace("CALLTOT", str(call_total)).replace("EFFTOT", str(eff_total))
            .replace("NETTOT", str(net_total))
            .replace("C_OK", C_OK).replace("C_CAP", C_CAP).replace("C_INTR", C_INTR)
            .replace("C_RUN", C_RUN)
            .replace("ROWS", "".join(rows)))
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


def _from_files(classified="free_models_classified.json",
                stability="inference_stability.json"):
    cands = []
    if os.path.exists(classified):
        d = json.load(open(classified, encoding="utf-8"))
        cands = d.get("all_free") or []
    ids = [r.get("id") for r in cands if r.get("id")]
    names = {r.get("id"): (r.get("name") or r.get("id")) for r in cands if r.get("id")}
    state = {}
    if os.path.exists(stability):
        m = json.load(open(stability, encoding="utf-8"))
        for mid, e in (m.get("models") or {}).items():
            calls = e.get("calls") or []
            state[mid] = {"calls": calls, "status": "done"}
            if mid not in ids:
                ids.append(mid)
                names[mid] = mid
    return ids, names, state


def main():
    args = sys.argv[1:]
    classified = args[0] if len(args) > 0 else "free_models_classified.json"
    stability = args[1] if len(args) > 1 else "inference_stability.json"
    out = args[2] if len(args) > 2 else "phase1_live.html"
    ids, names, state = _from_files(classified, stability)
    if not ids:
        print("[ERROR] 未找到候选名单（free_models_classified.json）", file=sys.stderr)
        sys.exit(2)
    build_live_html(ids, names, state, out, rounds=12, window="offpeak",
                    phase_label="阶段一 · 初筛", finished=True,
                    note="本页为离线快照（阶段一已跑完）。运行阶段一时脚本会边测边刷新本页。")
    print(f"已写出实时看板 {out}（{len(ids)} 个模型）")


if __name__ == "__main__":
    main()
