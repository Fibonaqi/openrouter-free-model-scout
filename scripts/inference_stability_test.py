#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenRouter 免费模型「推理级稳定性」实测 —— 两阶段 · 择时 · 省配额 · 带黑名单
============================================================================
真实发起 POST /api/v1/chat/completions（流式 SSE），逐调用测量：
  成功率 / 爆满率(上游 provider 429·503) / 中断率(流截断·超时) / TTFT / 输出有效 / 推理正确率
对照 references/stability_test_standard.md 评级。

两阶段（账号有日次数限制，故不追求大样本）：
  阶段一 初筛 (--phase 1)：全部候选，每模型 5–10 次（默认 7，单窗口）。触发排除阈值即排除，不再复筛。
  阶段二 复筛 (--phase 2)：仅初筛通过者，在另一窗口再 5–10 次（与初筛间隔 ≥30min 形成峰谷）。
                     与初筛合并重算最终评级，并标记峰值爆满风险。

指标同批：单阶段每轮按 T1→T4→T2→T3 轮转，5–10 次即同时覆盖 轻量/逻辑/代码/长文。

黑名单（用户要求）：连续 CYCLE_DAYS(默认3) 个测试日都失败(终判 D) 的模型，列入黑名单；
  此后即便仍是免费类型，也默认排除出测试名单（除非 --include-blacklisted 或用户点名）。
  ⚠️ 账号/区域/guardrail 拦截(untestable) 与 真实失败 严格区分：被拦截的模型**不计入黑名单**
  （无法判定其稳定性，不应误拉黑）。

必需：OPENROUTER_API_KEY（免费模型推理本身 $0，但仍需 Key 与配额）。
OpenRouter 免费层限额（官方）：买币 <10 → 20 次/分、50 次/天；≥10 → 1000/分、1000/天。
  - 上游「爆满」= 429 带 provider_code / 503 overloaded / 流中 finish_reason:"error" → 计入 爆满率（真实信号）。
  - 平台自身限流 429（无 provider_code）= 本测试被打限 → 退避重试，不计入模型不稳定。
  - 账号/区域/guardrail 拦截 = 403 region / 404 guardrail / 403 agentic-only → untestable，不计入任何指标与黑名单。
用法：
  export OPENROUTER_API_KEY=sk-or-...
  python inference_stability_test.py --phase 1 --from-classified free_models_classified.json --rounds 7 --window offpeak
  python inference_stability_test.py --phase 2 --rounds 7 --window peak
"""
import argparse
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, date, timedelta

try:                                    # 阶段一实时看板（缺文件则静默降级，不影响测试）
    from build_live_report import build_live_html
except Exception:                       # pragma: no cover
    build_live_html = None

# 实时看板状态（阶段一边测边刷 phase1_live.html）
LIVE = {"ids": [], "names": {}, "state": {}, "out": "phase1_live.html",
        "rounds": 12, "window": "offpeak",
        "started": None, "enabled": False, "last_write": 0.0}


def live_init(ids, names, out, rounds, window, phase_label="阶段一 · 初筛"):
    LIVE.update(ids=list(ids), names=dict(names), out=out, rounds=rounds,
                window=window, state={m: {"calls": [], "status": "waiting"} for m in ids},
                started=datetime.now(), enabled=bool(build_live_html), phase_label=phase_label,
                last_write=0.0)
    live_write(force=True)


def live_running(mid):
    if not LIVE["enabled"]:
        return
    LIVE["state"].setdefault(mid, {"calls": [], "status": "waiting"})
    LIVE["state"][mid]["status"] = "running"


def live_done(mid, calls):
    if not LIVE["enabled"]:
        return
    LIVE["state"][mid] = {"calls": list(calls), "status": "done"}
    live_write(force=True)


def live_write(force=False, finished=False, note=""):
    if not LIVE["enabled"]:
        return
    now = time.time()
    if not force and (now - LIVE.get("last_write", 0)) < 2.0:   # 限流：最快每 2s 重写一次
        return
    LIVE["last_write"] = now
    try:
        build_live_html(LIVE["ids"], LIVE["names"], LIVE["state"], LIVE["out"],
                        rounds=LIVE["rounds"], window=LIVE["window"],
                        started=LIVE["started"], phase_label=LIVE.get("phase_label", "阶段一 · 初筛"),
                        finished=finished, note=note)
    except Exception as e:
        print(f"[WARN] 实时看板写入失败: {e}", file=sys.stderr)
        LIVE["enabled"] = False


API = "https://openrouter.ai/api/v1/chat/completions"
UA = {"User-Agent": "openrouter-infer-stability/2.1", "Content-Type": "application/json"}
TTFT_TIMEOUT = 30.0
TOTAL_TIMEOUT = 120.0
SLEEP_BETWEEN = 1.2          # 远低于 20/分，避免触发平台/DDoS 限流
DEFAULT_ROUNDS = 12          # 阶段一（初筛）每模型次数：6 档轮转各 2 次
PHASE2_ROUNDS = 18           # 阶段二（复筛）每模型次数：6 档各 3 次 → 幸存者合并样本 30
EARLY_ABORT_MIN = 5          # 前 N 次有效调用若「全爆满」或「零成功」→ 提前停批（省配额、消除无效测试）
NETWORK_RETRY = 4            # 本机网络/代理故障重试次数（故障不计入模型指标；代理 502 频发时多 retry 提升捕获率）

# 路径：黑名单随技能持久化（用户级，跨会话）
BLACKLIST_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "blacklist.json")
CYCLE_DAYS = 3               # 用户授权由我定周期：连续 3 个测试日都失败 → 拉黑

# 单阶段轮转：轻量 / 推理×3 / 代码 / 长文 混编
# 关键方法：**同一批调用，多个指标只是观测口径不同**——不按指标各跑一遍。
TIER_ROTATION = ["T1", "L1", "T2", "L2", "T3", "L3"]
TIERS = {
    # —— 可用性与容量档 ——（观测：成功率 / 爆满 / 中断 / 速度 / 完整输出）
    "T1": "Reply with ONE sentence defining 'entropy' in physics.",
    "T2": "Write a short Python function that returns the Fibonacci numbers up to n using iteration, with a one-line comment. Keep it under 200 words.",
    "T3": "Write a detailed 500-word essay explaining how Transformer attention works, including query/key/value, self-attention, and one concrete example. Keep it coherent.",
    # —— 推理能力档 ——（观测：答案正误。三题各有唯一正解）
    "L1": "All Bloops are Razzies. All Razzies are Lazzies. Are all Bloops definitely Lazzies? "
          "Answer with a single word 'yes' or 'no' first, then explain in at most one sentence.",
    "L2": "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
          "How much does the ball cost? Give the amount first (like $0.05), then one sentence of reasoning.",
    "L3": "All cats are animals. Some animals are black. Does it follow that some cats are black? "
          "Answer with a single word 'yes' or 'no' first, then explain in at most one sentence.",
}
LOGIC_TIERS = ("L1", "L2", "L3")
LOGIC_ANSWER = {"L1": "yes", "L2": "$0.05", "L3": "no"}


def _first_choice(text, options):
    """取回答中最先出现的选项词（模型通常先给答案再解释，故以首次出现为准）。"""
    head = (text or "").lower()[:120]
    best, pos = None, 10 ** 9
    for o in options:
        m = re.search(re.escape(o), head)
        if m and m.start() < pos:
            best, pos = o, m.start()
    return best


def _logic_ok(tier, text):
    """推理题判定：L1/L3 看首个 yes/no；L2 看是否给出 $0.05 且未答 0.10。"""
    if tier in ("L1", "L3"):
        return _first_choice(text, ("yes", "no")) == LOGIC_ANSWER[tier]
    if tier == "L2":
        head = (text or "")[:200]
        return bool(re.search(r"0\.05\b|5\s*(?:cent|美分)", head)) and not re.search(r"\b0\.10\b|\b10\s*cent", head)
    return False

def wilson(k, n, z=1.96):
    """Wilson 比例置信区间（小样本下比正态近似稳健）。返回 (lo, hi)。"""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


# 初筛排除闸门：触发任一项即排除（不进复筛，省配额）
EXCL_CAP = 0.20      # 爆满率 > 20%
EXCL_SUCC = 0.75     # 成功率 < 75%
EXCL_INTR = 0.30     # 中断率 > 30%
EXCL_LOGIC = 0.50    # 逻辑正确率（有样本时）< 50%

# 本机网络/代理故障签名：属测试基础设施故障，非模型问题 → 剔除并重试，不计入任何指标、不进黑名单
NETWORK_SIG = re.compile(
    r"tunnel connection failed|bad gateway|unexpected_eof|remote end closed|"
    r"remotedisconnected|connection reset|connection aborted|proxyerror|ssl|temporary failure", re.I)
NETWORK_EXC = ("urlerror", "proxyerror", "sslerror", "connectionreseterror", "remotedisconnected")


def _is_network_error(e):
    """True=本机/代理基础设施故障（剔除+重试）；False=模型侧信号（超时等按中断处理）。"""
    name = type(e).__name__.lower()
    if name in NETWORK_EXC:
        return True
    if name in ("timeouterror", "sockettimeout"):
        return False            # 超时按「模型慢」处理，属真实信号
    return bool(NETWORK_SIG.search(f"{type(e).__name__}: {e}"))


def classify_error(status, body_text):
    """返回 (kind, detail)。kind ∈ {capacity, platform, untestable, fail}
    - capacity: 上游爆满（真实信号，计入爆满率）
    - platform: 平台自身限流（退避重试，不计入模型不稳定）
    - untestable: 账号/区域/guardrail/agentic 拦截（无法测，不计入任何指标与黑名单）
    - fail: 其他失败
    """
    try:
        obj = json.loads(body_text)
        # 兼容两种形状：{"error": {...}}（HTTP 响应体）与 {"code":..,"message":..}（SSE 流内 error 对象）
        eobj = obj.get("error") or obj
        msg = (eobj.get("message") if isinstance(eobj, dict) else "") or ""
        meta = (eobj.get("metadata") if isinstance(eobj, dict) else None) or {}
    except Exception:
        obj, msg, meta = {}, body_text, {}
    ml = msg.lower()
    if status in (403, 404):
        if "not available in your region" in ml or "region" in ml:
            return "untestable", f"region-blocked: {msg[:160]}"
        if "agentic harness" in ml:
            return "untestable", f"agentic-only: {msg[:160]}"
        if "guardrail" in ml or "data policy" in ml or "0 endpoints" in ml:
            return "untestable", f"guardrail-blocked: {msg[:160]}"
        if "no endpoints found" in ml:
            return "untestable", f"no-endpoints: {msg[:160]}"
        return "untestable", f"HTTP{status}: {msg[:160]}"
    if status == 429:
        if meta.get("provider_code"):
            return "capacity", body_text[:200]
        if meta.get("error_type") == "rate_limit_exceeded":
            return "platform", body_text[:200]
        if "rate limit" in ml:
            return "platform", body_text[:200]
        return "capacity", body_text[:200]   # 429 默认按上游爆满
    if status >= 500:
        # 上游不可用/过载（503 Service Unavailable、Upstream error、ResourceExhausted）均属「爆满」信号
        if ("overload" in ml or "capacity" in ml or "unavailable" in ml or "upstream" in ml
                or "resource exhausted" in ml or "try again" in ml or meta.get("provider_code")):
            return "capacity", body_text[:200]
        return "fail", body_text[:200]
    return "fail", body_text[:200]


def one_call(model, prompt, api_key, tier):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True, "temperature": 0, "max_tokens": 2000,
    }).encode()
    req = urllib.request.Request(API, data=body, headers={
        **UA, "Authorization": f"Bearer {api_key}"})
    t0 = time.perf_counter()
    ttft = None
    content = []
    reasoning = []
    finish = None
    status = None
    err = None
    interrupted = False
    capacity = False
    platform = False
    timeout = False
    untestable = False
    untest_reason = None
    network = False
    try:
        with urllib.request.urlopen(req, timeout=TOTAL_TIMEOUT) as r:
            status = r.status
            if status != 200:
                chunk = r.read(2000).decode("utf-8", "replace")
                kind, detail = classify_error(status, chunk)
                if kind == "capacity":
                    capacity = True
                elif kind == "platform":
                    platform = True
                elif kind == "untestable":
                    untestable = True; untest_reason = detail
                err = f"HTTP {status}: {detail}"
                return _pack(status, ttft, t0, content, finish, err, interrupted, capacity, platform, timeout, untestable, untest_reason, reasoning)
            for raw in r:
                line = raw.decode("utf-8", "replace")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if "error" in obj:
                    # 流内错误对象携带真实 code（如 502/429），须传入才能正确分类
                    eobj = obj.get("error") or {}
                    ecode = eobj.get("code") if isinstance(eobj, dict) else None
                    try:
                        ecode = int(ecode)
                    except Exception:
                        ecode = 500
                    kind, detail = classify_error(ecode, json.dumps(eobj if eobj else obj))
                    err = detail
                    if kind == "capacity":
                        capacity = True
                    elif kind == "untestable":
                        untestable = True; untest_reason = detail
                    else:
                        interrupted = True
                    break
                choices = obj.get("choices") or []
                if not choices:
                    continue
                ch = choices[0]
                if ttft is None:
                    if (time.perf_counter() - t0) * 1000.0 > TTFT_TIMEOUT * 1000.0:
                        timeout = True
                    ttft = (time.perf_counter() - t0) * 1000.0
                d0 = ch.get("delta") or {}
                delta = d0.get("content") or ""
                if delta:
                    content.append(delta)
                # 推理模型把 token 消耗在 thinking 上（delta.reasoning / reasoning_content），
                # 若只取 content 会误判为「空输出」而计失败 → 作为兜底文本收集
                rd = d0.get("reasoning") or d0.get("reasoning_content") or ""
                if rd:
                    reasoning.append(rd)
                fr = ch.get("finish_reason")
                if fr:
                    finish = fr
                    if fr == "error":   # 流中限流
                        interrupted = True
                        capacity = True
            if finish is None and not interrupted and err is None:
                interrupted = True
    except urllib.error.HTTPError as e:
        detail = e.read(2000).decode("utf-8", "replace") if hasattr(e, "read") else str(e)
        status = e.code
        kind, d = classify_error(status, detail)
        if kind == "capacity":
            capacity = True
        elif kind == "platform":
            platform = True
        elif kind == "untestable":
            untestable = True; untest_reason = d
        err = f"HTTP {e.code}: {d}"
        return _pack(status or 0, ttft, t0, content, finish, err, interrupted, capacity, platform, timeout, untestable, untest_reason, reasoning)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if _is_network_error(e):
            network = True                  # 本机/代理故障：不计中断，上层重试并从指标中剔除
            err = f"[network] {err}"
        else:
            interrupted = True
        return _pack(status or 0, ttft, t0, content, finish, err, interrupted, capacity, platform, timeout, untestable, untest_reason, network, reasoning)
    return _pack(status or 200, ttft, t0, content, finish, err, interrupted, capacity, platform, timeout, untestable, untest_reason, network, reasoning)


def _pack(status, ttft, t0, content, finish, err, interrupted, capacity=False, platform=False, timeout=False, untestable=False, untest_reason=None, network=False, reasoning=None):
    total = (time.perf_counter() - t0) * 1000.0
    text = "".join(content)
    # 推理模型把 token 全消耗在 thinking 上 → content 为空、finish=length。
    # 若不兜底会被误判为「空输出失败」，属测试设计边界而非模型不可用 → 以 thinking 文本兜底。
    reasoning_text = "".join(reasoning or [])
    from_reasoning = False
    if not text.strip() and reasoning_text.strip():
        text = reasoning_text
        from_reasoning = True
    # length=撞到输出上限：模型可用、只是被截断，属测试设计边界 → 计为可用成功，另由 valid 追踪「完整输出率」
    ok = (status == 200 and err is None and not interrupted and not untestable
          and finish in ("stop", "tool_calls", "length") and len(text.strip()) > 0)
    return {
        "status": status, "ttft_ms": round(ttft, 1) if ttft is not None else None,
        "total_ms": round(total, 1), "finish": finish, "interrupted": interrupted,
        "capacity": capacity, "platform": platform, "timeout": timeout, "network": network,
        "untestable": untestable, "untest_reason": untest_reason,
        "len": len(text), "text": text, "truncated": finish == "length",
        "reasoning_len": len(reasoning_text), "answer_from_reasoning": from_reasoning,
        "valid": ok and finish == "stop", "ok": ok, "err": err,
    }


def grade(m):
    if m.get("untestable"):
        return "不可测"
    n = m["n_eff"]
    if n < 10:
        return "样本不足"
    sr = m["success_rate"]; cf = m["capacity_full_rate"]
    ir = m["interrupt_rate"]; tp = m["ttft_p95_ms"]; ov = m["output_validity"]
    rc = m.get("reasoning_correct_rate")
    if rc is not None and rc < 0.9:
        return "D(不推荐)"            # 推理题答不对，直接否
    if sr >= 0.98 and cf <= 0.02 and ir <= 0.03 and (tp is None or tp <= 15000) and ov >= 0.95:
        return "A(值得推荐)"
    if sr >= 0.90 and cf <= 0.08 and ir <= 0.08 and (tp is None or tp <= 30000):
        return "B(可用)"
    if sr >= 0.75 and cf <= 0.20:
        return "C(勉强)"
    return "D(不推荐)"


def gate_exclude(m):
    """初筛排除闸门：触任一项即排除（不进复筛）。返回 (是否排除, 原因)。"""
    if m.get("untestable"):
        return False, "不可测(账号/区域/guardrail 拦截，不判定)"
    if m["capacity_full_rate"] > EXCL_CAP:
        return True, f"爆满率{m['capacity_full_rate']*100:.0f}%>{EXCL_CAP*100:.0f}%"
    if m["success_rate"] < EXCL_SUCC:
        return True, f"成功率{m['success_rate']*100:.0f}%<{EXCL_SUCC*100:.0f}%"
    if m["interrupt_rate"] > EXCL_INTR:
        return True, f"中断率{m['interrupt_rate']*100:.0f}%>{EXCL_INTR*100:.0f}%"
    rc = m.get("reasoning_correct_rate")
    if rc is not None and rc < EXCL_LOGIC:
        return True, f"逻辑正确率{rc*100:.0f}%<{EXCL_LOGIC*100:.0f}%"
    return False, ""


def aggregate(recs):
    """合并全部调用（可跨阶段），产出指标 + 评级。untestable 调用不计入任何指标。"""
    valid = [x for x in recs if not (x["platform"] and not x["capacity"])
             and not x.get("untestable") and not x.get("network")]
    n = len(valid)
    succ = [x for x in valid if x["ok"]]
    cap = [x for x in valid if x["capacity"]]
    intr = [x for x in valid if x["interrupted"] and not x["capacity"]]
    to = [x for x in valid if x.get("timeout")]
    ttfts = [x["ttft_ms"] for x in succ if x["ttft_ms"] is not None]
    tots = [x["total_ms"] for x in succ]
    valids = [x for x in valid if x["valid"]]
    logic_calls = [x for x in valid if x.get("reasoning_correct") is not None]
    logic_ok = [x for x in logic_calls if x["reasoning_correct"]]
    peak = [x for x in valid if x["window"] == "peak"]
    off = [x for x in valid if x["window"] == "offpeak"]
    windows = sorted(set(x["window"] for x in valid))
    untest_n = len([x for x in recs if x.get("untestable")])

    def rate(lst): return round(len(lst) / n, 4) if n else 0.0
    def p95(a): return round(sorted(a)[max(0, int(len(a) * 0.95) - 1)], 1) if a else None

    cap_peak = rate([x for x in peak if x["capacity"]]) if peak else None
    cap_off = rate([x for x in off if x["capacity"]]) if off else None
    peak_risk = bool(peak and off and abs(cap_peak - cap_off) > 0.10)

    cps = [x["len"] / (x["total_ms"] / 1000.0) for x in succ if x.get("total_ms") and x.get("len")]
    s_lo, s_hi = wilson(len(succ), n)
    c_lo, c_hi = wilson(len(cap), n)
    logic_detail = {}
    for t in LOGIC_TIERS:
        sub = [x for x in logic_calls if x["tier"] == t]
        logic_detail[t] = (round(sum(1 for x in sub if x["reasoning_correct"]) / len(sub), 4) if sub else None)

    m = {
        "calls_n": len(recs), "n_eff": n,
        "platform_limited": len([x for x in recs if x["platform"] and not x["capacity"]]),
        "network_error": len([x for x in recs if x.get("network")]),
        "untestable": untest_n > 0 and n == 0, "untest_reason": next((x.get("untest_reason") for x in recs if x.get("untestable")), None),
        "windows": windows,
        "success_rate": rate(succ), "capacity_full_rate": rate(cap),
        "interrupt_rate": rate(intr), "timeout_rate": rate(to),
        "ttft_median_ms": round(statistics.median(ttfts), 1) if ttfts else None,
        "ttft_p50_ms": round(statistics.median(ttfts), 1) if ttfts else None,
        "ttft_p95_ms": p95(ttfts),
        "total_median_ms": round(statistics.median(tots), 1) if tots else None,
        "output_validity": rate(valids),                                   # 完整输出率（finish=stop）
        "truncated_rate": rate([x for x in valid if x.get("truncated")]),  # 撞输出上限被截断率（不计为失败）
        "reasoning_correct_rate": round(len(logic_ok) / len(logic_calls), 4) if logic_calls else None,
        "logic_n": len(logic_calls),
        "throughput_cps": round(statistics.median(cps), 1) if cps else None,   # 输出速度（字符/秒，中位数）
        "success_ci95": [round(s_lo, 4), round(s_hi, 4)],
        "capacity_ci95": [round(c_lo, 4), round(c_hi, 4)],
        "fail_upper95": round(1 - s_lo, 4),      # 95% 置信下「真实失败率」的上界（最坏情况断言）
        "logic_detail": logic_detail,
        "tier_success": {t: rate([x for x in valid if x["tier"] == t and x["ok"]]) for t in sorted(TIERS)},
        "peak_capacity_rate": cap_peak, "offpeak_capacity_rate": cap_off,
        "peak_risk": peak_risk,
    }
    m["grade"] = grade(m)
    return m


def run_batch(model, rounds, window, api_key):
    """单阶段批次：按 TIER_ROTATION 轮转发 rounds 次调用，同批覆盖全部指标。"""
    print(f"\n=== {model}  (阶段批次 {rounds} 次, 窗口={window}) ===")
    recs = []
    for i in range(rounds):
        tier = TIER_ROTATION[i % len(TIER_ROTATION)]
        prompt = TIERS[tier]
        r = one_call(model, prompt, api_key, tier)
        if r["platform"] and not r["capacity"] and not r["untestable"]:   # 平台自身限流：退避重试
            for _ in range(3):
                print(f"    [平台限流] 退避 6s 重试...")
                time.sleep(6)
                r = one_call(model, prompt, api_key, tier)
                if not (r["platform"] and not r["capacity"] and not r["untestable"]):
                    break
        for _ in range(NETWORK_RETRY):          # 本机网络/代理故障：重试（不计入模型指标）
            if not r.get("network"):
                break
            print("    [网络故障] 本机链路异常，1.5s 后重试（不计入模型指标）...")
            time.sleep(1.5)
            r = one_call(model, prompt, api_key, tier)
        if tier in LOGIC_TIERS:
            r["reasoning_correct"] = bool(r["ok"] and _logic_ok(tier, r.get("text") or ""))
        r["tier"] = tier; r["window"] = window
        r["ts"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
        recs.append(r)
        if LIVE["enabled"]:                     # 边测边刷实时看板
            LIVE["state"][model] = {"calls": recs, "status": "running"}
            live_write()
        if r["untestable"]:
            tag = "不可测"
        elif r["ok"]:
            tag = "OK "
        elif r["capacity"]:
            tag = "爆满"
        elif r["platform"]:
            tag = "平台限"
        elif r["interrupted"]:
            tag = "中断"
        else:
            tag = "失败"
        extra = f"  逻辑={'对' if r.get('reasoning_correct') else '错'}" if r.get('reasoning_correct') is not None else ""
        print(f"  轮{i+1:2d}/{rounds} [{tier}] {tag:5} ttft={r['ttft_ms']}ms tot={r['total_ms']}ms fin={r['finish']} len={r['len']}{extra}")
        if r["untestable"]:
            print(f"       ↳ {r['untest_reason']}")
            print("    [早停] 账号/区域/guardrail/agentic 拦截 → 停止本模型后续调用（省配额）")
            break
        done = i + 1
        if done >= EARLY_ABORT_MIN:             # 明显失败则提前停批，避免无效测试
            eff = [x for x in recs if not (x.get("platform") and not x["capacity"])
                   and not x.get("untestable") and not x.get("network")]
            if eff and (all(x["capacity"] for x in eff) or sum(1 for x in eff if x["ok"]) == 0):
                why = "全部爆满" if all(x["capacity"] for x in eff) else "零成功"
                print(f"    [早停] 前 {done} 次有效调用「{why}」→ 停止本模型后续调用")
                break
        time.sleep(SLEEP_BETWEEN)
    return recs


# ---------- 黑名单 ----------
def save_progress(args, bl, models_out):
    """增量落盘：每测完一个模型就写一次 JSON。

    目的：长批次（20 模型 × 12 次）若中途被中断/会话结束，此前已完成的模型数据不丢失。
    """
    try:
        doc = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "standard": "references/stability_test_standard.md",
            "cycle_days": bl.get("cycle_days", CYCLE_DAYS),
            "phase1_done": any("phase1_excluded" in e for e in models_out.values()),
            "phase2_done": args.phase == 2,
            "partial": True,
            "blacklist": bl,
            "models": models_out,
        }
        with open(args.merge, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] 增量落盘失败: {e}", file=sys.stderr)


def load_blacklist():
    if os.path.exists(BLACKLIST_PATH):
        try:
            return json.load(open(BLACKLIST_PATH, encoding="utf-8"))
        except Exception:
            pass
    return {"cycle_days": CYCLE_DAYS, "blacklisted": {}, "models": {}}


def save_blacklist(bl):
    os.makedirs(os.path.dirname(BLACKLIST_PATH), exist_ok=True)
    json.dump(bl, open(BLACKLIST_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def _consecutive_fail(fail_dates, today, X):
    if len(fail_dates) < X:
        return False
    ds = sorted(set(fail_dates))
    last = ds[-X:]
    base = date.fromisoformat(today)
    for off in (0, 1):           # 允许截至今天或昨天
        ok = True
        for i in range(X):
            exp = (base - timedelta(days=(X - 1 - i) + off)).isoformat()
            if last[i] != exp:
                ok = False
                break
        if ok:
            return True
    return False


def update_blacklist(bl, tested_results):
    """依据本轮真实测试结果更新黑名单计数。untestable 模型不参与（无法判定）。"""
    today = date.today().isoformat()
    bl.setdefault("cycle_days", CYCLE_DAYS)
    bl.setdefault("blacklisted", {})
    bl.setdefault("models", {})
    changed = False
    for mid, e in tested_results.items():
        if e.get("untestable"):
            continue
        g = e.get("grade", "")
        rec = bl["models"].setdefault(mid, {"fail_dates": [], "blacklisted": False, "blacklisted_since": ""})
        if g.startswith("D"):                       # 真实测试失败
            if today not in rec["fail_dates"]:
                rec["fail_dates"].append(today)
                changed = True
        elif g in ("A(值得推荐)", "B(可用)", "C(勉强)"):
            if rec["fail_dates"]:
                rec["fail_dates"] = []
                changed = True
        # 判定拉黑
        if not rec["blacklisted"] and _consecutive_fail(rec["fail_dates"], today, bl["cycle_days"]):
            rec["blacklisted"] = True
            rec["blacklisted_since"] = today
            bl["blacklisted"][mid] = today
            changed = True
    if changed:
        save_blacklist(bl)
    return bl


def load_merge(path):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8")).get("models", {})
        except Exception:
            return {}
    return {}


def print_model(mid, m, phase_label):
    rc = m["reasoning_correct_rate"]
    if m.get("untestable"):
        print(f"  [不可测          ] {mid}\n        ↳ {m.get('untest_reason')}")
        return
    print(f"  [{m['grade']:12}] {mid}")
    rc_str = f"{rc*100:.0f}%" if rc is not None else "-"
    print(f"      窗口={m['windows']}  成功率{m['success_rate']*100:.1f}%  爆满{m['capacity_full_rate']*100:.1f}%  "
          f"中断{m['interrupt_rate']*100:.1f}%  TTFT_P95={m['ttft_p95_ms']}ms  "
          f"输出有效{m['output_validity']*100:.1f}%  逻辑正确{rc_str}"
          + ("  峰值爆满风险!" if m['peak_risk'] else "")
          + (f"  (平台限流{m['platform_limited']}次,样本不足)" if m['n_eff'] < 10 else "")
          + (f"  [{phase_label}]" if phase_label else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, choices=[1, 2], default=1, help="阶段 1=初筛(全量) 2=复筛(仅初筛通过者)")
    ap.add_argument("--models", help="逗号分隔模型 id（--phase 2 可只复筛指定几个，进一步省配额）")
    ap.add_argument("--from-classified", help="读 free_models_classified.json 全量免费模型（仅阶段一用）")
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help=f"每阶段每模型次数（默认 {DEFAULT_ROUNDS}）")
    ap.add_argument("--rounds2", type=int, default=PHASE2_ROUNDS, help=f"阶段二每模型次数（幸存者追加，默认 {PHASE2_ROUNDS}）")
    ap.add_argument("--window", default="offpeak", help="peak / offpeak / single —— 本阶段窗口标签")
    ap.add_argument("--key", help="或直接传 OPENROUTER_API_KEY")
    ap.add_argument("--merge", default="inference_stability.json", help="跨阶段合并输出文件")
    ap.add_argument("--include-blacklisted", action="store_true", help="即便已拉黑也纳入测试（用户主动要求时）")
    args = ap.parse_args()

    api_key = args.key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("[ERROR] 缺少 OPENROUTER_API_KEY（免费推理需 Key）。设环境变量或 --key 后重试。", file=sys.stderr)
        sys.exit(2)
    window = args.window if args.window in ("peak", "offpeak") else "single"
    if args.rounds < 5 or args.rounds > 60:
        print(f"[WARN] --rounds={args.rounds} 超出建议区间 5–60（样本过低不可信、过高浪费配额）。", file=sys.stderr)

    bl = load_blacklist()
    prior = load_merge(args.merge)
    models_out = dict(prior)

    if args.phase == 1:
        names = {}
        if args.from_classified:
            data = json.load(open(args.from_classified, encoding="utf-8"))
            ids = [r["id"] for r in (data.get("all_free") or [])]
            names = {r.get("id"): (r.get("name") or r.get("id")) for r in (data.get("all_free") or [])}
        elif args.models:
            ids = [x.strip() for x in args.models.split(",") if x.strip()]
        else:
            print("[ERROR] 阶段一需 --models 或 --from-classified", file=sys.stderr)
            sys.exit(2)
        # 黑名单过滤
        if not args.include_blacklisted and bl.get("blacklisted"):
            before = len(ids)
            ids = [m for m in ids if m not in bl["blacklisted"]]
            print(f"[黑名单] 已排除 {before - len(ids)} 个拉黑模型：{sorted(set(bl['blacklisted']) & set(before and ids)) }")
        live_init(ids, names, "phase1_live.html", args.rounds, window)   # 名单到手即建实时看板框架
        for mid in ids:
            live_running(mid)
            recs = run_batch(mid, args.rounds, window, api_key)
            m = aggregate(recs)
            exc, reason = gate_exclude(m)
            entry = {"calls": recs, "phase1_excluded": exc, "exclude_reason": reason if exc else "",
                     "untestable": m.get("untestable", False), "untest_reason": m.get("untest_reason")}
            entry.update(m)
            models_out[mid] = entry
            live_done(mid, recs)
            update_blacklist(bl, {mid: entry})       # 逐模型累计失败日（幂等，同日不重复）
            save_progress(args, bl, models_out)      # 逐模型落盘，防中断丢数据
        live_write(force=True, finished=True,
                   note="阶段一已跑完。最终结论见 phase1_report.html。")
        print("\n==== 阶段一 初筛结果 ====")
        for mid, e in models_out.items():
            if args.from_classified and mid not in ids and mid in bl.get("blacklisted", {}):
                continue
            lbl = "排除(不进复筛)" if e.get("phase1_excluded") else "通过→待复筛"
            print_model(mid, e, lbl)
            if e.get("phase1_excluded"):
                print(f"       排除原因: {e['exclude_reason']}")
        update_blacklist(bl, {k: v for k, v in models_out.items() if k in ids})
    else:  # phase 2
        if args.models:
            ids = [x.strip() for x in args.models.split(",") if x.strip()]
        else:
            ids = [mid for mid, e in prior.items() if not e.get("phase1_excluded")]
        if not ids:
            print("[WARN] 无初筛通过模型可复筛（或 --models 为空）。", file=sys.stderr)
        for mid in ids:
            old = prior.get(mid, {})
            old_calls = old.get("calls", [])
            # 跳过已不可测的（无需再试）
            if old.get("untestable"):
                print(f"  [跳过] {mid} 初筛即不可测，复筛无意义")
                continue
            new_calls = run_batch(mid, args.rounds2, window, api_key)
            recs = old_calls + new_calls
            m = aggregate(recs)
            entry = dict(old)
            snap = aggregate(old_calls)                      # 首轮快照，供报告单独展示
            snap["phase1_excluded"] = old.get("phase1_excluded", False)
            snap["exclude_reason"] = old.get("exclude_reason", "")
            entry["phase1_metrics"] = snap
            entry["calls"] = recs
            entry["phase1_excluded"] = old.get("phase1_excluded", False)
            entry["exclude_reason"] = old.get("exclude_reason", "")
            entry["phase2_window"] = window
            entry["untestable"] = m.get("untestable", False)
            entry["untest_reason"] = m.get("untest_reason")
            entry.update(m)
            models_out[mid] = entry
            update_blacklist(bl, {mid: entry})
            save_progress(args, bl, models_out)
        print("\n==== 阶段二 复筛 + 最终合并评级 ====")
        for mid, e in models_out.items():
            if mid in ids or e.get("phase1_excluded"):
                print_model(mid, e, "终" if not e.get("phase1_excluded") else "初筛已排除")
        update_blacklist(bl, {k: v for k, v in models_out.items() if k in ids})

    merged = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "standard": "references/stability_test_standard.md",
        "cycle_days": bl.get("cycle_days", CYCLE_DAYS),
        "phase1_done": any("phase1_excluded" in e for e in models_out.values()),
        "phase2_done": args.phase == 2,
        "partial": False,
        "blacklist": bl,
        "models": models_out,
    }
    with open(args.merge, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"\n已写出 {args.merge}（阶段{args.phase}）")
    n_bl = len(bl.get("blacklisted", {}))
    if n_bl:
        print(f"[黑名单] 当前 {n_bl} 个模型被拉黑（连续 {bl.get('cycle_days')} 测试日失败）：{list(bl['blacklisted'].keys())}")


if __name__ == "__main__":
    main()
