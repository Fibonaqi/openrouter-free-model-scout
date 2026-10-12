#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenRouter 免费大模型工作流管线
================================
工作流步骤：
  1. 抓取 (Acquire)    : GET https://openrouter.ai/api/v1/models
  2. 过滤 (Filter)     : pricing.prompt==0 且 pricing.completion==0  => 免费模型
  3. 增强 (Enrich)     : 逐个抓取 /api/v1/models/{slug}/endpoints
                         取「提供方数量 / 平均在线率 / 平均延迟」作热门度代理
  4. 分类 (Classify)   : 四维 —— 推理级别 / 模型能力 / 免费活动时间 / 免费热门度
  5. 评分 (Score)      : 加权综合评分 (推理0.4 + 能力0.35 + 热门0.25)
  6. 推荐 (Recommend)  : 综合 Top3 + 各维度冠军，去重输出 1-3 个

用法:
  python openrouter_free_models.py            # 联网抓取并生成报告
  python openrouter_free_models.py --use-cache # 仅用本地 openrouter_models_raw.json (不刷新模型列表)
输出:
  free_models_classified.json  (结构化数据)
  openrouter_free_report.md    (Markdown 摘要)
  openrouter_free_report.html  (可视化报告)
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
RAW_CACHE = os.path.join(BASE, "openrouter_models_raw.json")
MODELS_URL = "https://openrouter.ai/api/v1/models"

UA = {"User-Agent": "openrouter-free-workflow/1.0"}


def http_get_json(url, timeout=30, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa
            last = e
            time.sleep(1.0)
    print(f"  [WARN] 抓取失败 {url}: {last}", file=sys.stderr)
    return None


def is_free(m):
    p = m.get("pricing") or {}
    return str(p.get("prompt", "x")) == "0" and str(p.get("completion", "x")) == "0"


# 轻量/蒸馏变体命名特征：flash/lite/nano/mini/small/xs/lightning/sante/tiny/distil...
LIGHT_TOKENS = ("flash", "lite", "nano", "mini", "small", "xs",
                "lightning", "sante", "tiny", "distil", "mobile", "edge")


def is_light(name):
    """判断模型是否为轻量/快/蒸馏定位变体（用于'推理最强'体量感知护栏）。"""
    n = (name or "").lower()
    return any(tok in n for tok in LIGHT_TOKENS)


# ----------------------------------------------------------------------------
# 4 维分类逻辑
# ----------------------------------------------------------------------------
def classify_reasoning(m):
    """推理级别：基础分来自 reasoning 字段 + 模型族启发; 最高级别=强推理。"""
    r = m.get("reasoning") or {}
    mandatory = r.get("mandatory")
    default_on = r.get("default_enabled")
    sp = m.get("supported_parameters") or []
    has_reasoning_param = "reasoning" in sp or "include_reasoning" in sp
    name = (m.get("name", "") + " " + m.get("id", "")).lower()

    # 模型族启发：显式推理模型
    explicit_family = any(k in name for k in [
        "r1", "reasoning", "think", "qwq", "deepseek-r1", "o1", "o3", "qwen3-thinking"
    ])

    if mandatory is True:
        level, score, note = "强制推理", 95, "推理为必选项"
    elif explicit_family or (default_on is True and has_reasoning_param):
        level, score, note = "默认开启推理", 78, "默认走推理链"
    elif has_reasoning_param and default_on is False:
        level, score, note = "可选推理", 55, "支持手动开启 reasoning"
    else:
        level, score, note = "非推理", 25, "标准生成，无推理链"

    # 家族加成（仅对未达强制级的做微调）
    if explicit_family and level != "强制推理":
        score = min(95, score + 10)
        note += " / 推理模型族"

    # —— 推理「实力」(质量感知) ——
    # 层级分只代表推理能力开放程度，不代表实际推理质量。
    # 有实测智能指数时做质量加权；无质量证据则封顶，避免小模型被误判为"推理最强"。
    ba = (m.get("benchmarks") or {}).get("artificial_analysis") or {}
    intel = ba.get("intelligence_index")
    coding = ba.get("coding_index")
    agentic = ba.get("agentic_index")
    parts = []
    if isinstance(intel, (int, float)):
        parts.append((intel, 1.0))
    if isinstance(coding, (int, float)):
        parts.append((coding, 0.8))
    if isinstance(agentic, (int, float)):
        parts.append((agentic, 0.8))
    if parts:
        num = sum(v * w for v, w in parts)
        den = sum(w for _, w in parts)
        quality = max(0.0, min(100.0, num / den))
        measured = True
        strength = round(0.45 * min(100, score) + 0.55 * quality, 1)
        note += f" / 实测质量{quality:.1f}"
    else:
        measured = False
        quality = None
        strength = min(score, 40)  # 未测封底，避免无数据模型被误判为"推理最强"
    return level, score, strength, note, measured


def classify_capability(m):
    """模型能力：上下文长度 + 多模态广度 + 基准分(intelligence/coding/agentic) + 工具支持。"""
    ctx = m.get("context_length") or 0
    arch = m.get("architecture") or {}
    in_mod = arch.get("input_modalities") or ["text"]
    out_mod = arch.get("output_modalities") or ["text"]
    ba = (m.get("benchmarks") or {}).get("artificial_analysis") or {}
    intel = ba.get("intelligence_index")
    coding = ba.get("coding_index")
    agentic = ba.get("agentic_index")
    sp = m.get("supported_parameters") or []
    has_tools = "tools" in sp
    has_struct = "structured_outputs" in sp

    # 上下文得分 (对数刻度, 8k=20 -> 1M=100)
    if ctx >= 1_000_000:
        ctx_s = 100
    elif ctx >= 256_000:
        ctx_s = 90
    elif ctx >= 128_000:
        ctx_s = 78
    elif ctx >= 64_000:
        ctx_s = 64
    elif ctx >= 32_000:
        ctx_s = 50
    else:
        ctx_s = 35

    # 多模态广度 (输入模态种类)
    extra_mod = [x for x in in_mod if x != "text"]
    if {"image", "audio", "video"}.issubset(set(extra_mod)):
        mod_s = 100
    elif {"image", "audio"}.issubset(set(extra_mod)) or {"image", "video"}.issubset(set(extra_mod)):
        mod_s = 85
    elif "image" in extra_mod and "audio" in out_mod:
        mod_s = 80  # 图文入、语音出
    elif "image" in extra_mod:
        mod_s = 65
    elif extra_mod:
        mod_s = 55
    else:
        mod_s = 40

    # 基准分 (若无则用上下文+模态近似)
    bench_parts = [x for x in [intel, coding, agentic] if isinstance(x, (int, float))]
    if bench_parts:
        bench_s = sum(bench_parts) / len(bench_parts)  # artificial_analysis 指数大致 0-100+
        bench_s = max(0, min(100, bench_s))
    else:
        bench_s = ctx_s * 0.5 + mod_s * 0.5

    tool_s = 0
    if has_tools:
        tool_s += 18
    if has_struct:
        tool_s += 8

    score = round(ctx_s * 0.30 + mod_s * 0.25 + bench_s * 0.35 + tool_s, 1)
    score = max(0, min(100, score))

    tags = []
    if ctx >= 128_000:
        tags.append("长上下文")
    if "image" in extra_mod:
        tags.append("视觉")
    if "audio" in extra_mod:
        tags.append("音频")
    if "video" in extra_mod:
        tags.append("视频")
    if has_tools:
        tags.append("工具调用")
    if intel is not None:
        tags.append(f"智能指数{intel}")
    if not tags:
        tags.append("纯文本")
    return score, tags, {
        "context_length": ctx,
        "input_modalities": in_mod,
        "output_modalities": out_mod,
        "intelligence_index": intel,
        "coding_index": coding,
        "agentic_index": agentic,
        "has_tools": has_tools,
        "has_structured_outputs": has_struct,
    }


def classify_free_window(m, now_ts):
    """免费活动时间：永久免费 vs 限时免费(到期日)；以及上架时长。"""
    exp = m.get("expiration_date")  # ISO date 或 null
    created = m.get("created") or 0
    age_days = (now_ts - created) / 86400 if created else 0

    if exp:
        exp_ts = datetime.fromisoformat(exp).replace(tzinfo=timezone.utc).timestamp()
        remain = (exp_ts - now_ts) / 86400
        if remain <= 0:
            window, wscore, note, stab = "已过期", 5, f"已于 {exp} 到期", 0.10
        elif remain <= 30:
            window, wscore, note, stab = "即将到期(<30天)", 35, f"限免至 {exp} (剩{remain:.0f}天)", 0.60
        elif remain <= 120:
            window, wscore, note, stab = "短期限免", 60, f"限免至 {exp} (剩{remain:.0f}天)", 0.85
        else:
            window, wscore, note, stab = "长期限免", 80, f"限免至 {exp} (剩{remain:.0f}天)", 0.95
    else:
        window, wscore, note, stab = "永久免费", 100, "无到期日，长期可免费用", 1.00

    return window, wscore, note, round(age_days, 1), stab


def enrich_endpoints(m):
    """抓取端点接口，返回 (提供方数, 平均在线率, 平均延迟, provider列表)。"""
    slug = m.get("canonical_slug") or m.get("id")
    url = f"{MODELS_URL}/{slug}/endpoints"
    d = http_get_json(url, timeout=25)
    if not d or "data" not in d:
        return 0, None, None, []
    eps = (d["data"] or {}).get("endpoints") or []
    if not eps:
        return 0, None, None, []
    providers = []
    uptimes, lats = [], []
    for e in eps:
        pn = e.get("provider_name")
        if pn:
            providers.append(pn)
        u = e.get("uptime_last_1d")
        if isinstance(u, (int, float)):
            uptimes.append(u)
        lt = e.get("latency_last_30m")
        if isinstance(lt, (int, float)):
            lats.append(lt)
    n = len(providers)
    avg_up = round(sum(uptimes) / len(uptimes), 3) if uptimes else None
    avg_lat = round(sum(lats) / len(lats), 1) if lats else None
    return n, avg_up, avg_lat, sorted(set(providers))


def classify_popularity(providers_n, avg_up, avg_lat):
    """免费热门度：以『提供方数量』为主代理，叠加在线率与延迟。"""
    # 提供方数: 1=40, 2=65, 3=82, >=4=95
    if providers_n >= 4:
        pn_s = 95
    elif providers_n == 3:
        pn_s = 82
    elif providers_n == 2:
        pn_s = 65
    elif providers_n == 1:
        pn_s = 40
    else:
        pn_s = 20

    up_s = (avg_up if avg_up is not None else 95)  # 无数据默认按高在线率
    if avg_lat is None:
        lat_s = 70
    elif avg_lat <= 800:
        lat_s = 95
    elif avg_lat <= 1500:
        lat_s = 80
    elif avg_lat <= 3000:
        lat_s = 65
    else:
        lat_s = 45

    score = round(pn_s * 0.6 + up_s * 0.25 + lat_s * 0.15, 1)
    note = f"提供方{providers_n}家"
    if avg_up is not None:
        note += f"/在线率{avg_up:.2f}%"
    if avg_lat is not None:
        note += f"/延迟{avg_lat}ms"
    return score, note


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--use-cache", action="store_true",
                    help="不刷新模型列表，仅用本地 openrouter_models_raw.json")
    args = ap.parse_args()

    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 1) 抓取模型列表
    if args.use_cache and os.path.exists(RAW_CACHE):
        print("[1/6] 使用本地缓存模型列表 ...")
        with open(RAW_CACHE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        print("[1/6] 抓取 OpenRouter 模型列表 ...")
        data = http_get_json(MODELS_URL, timeout=60)
        if data and "data" in data:
            with open(RAW_CACHE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        elif os.path.exists(RAW_CACHE):
            print("  抓取失败，回退到本地缓存。", file=sys.stderr)
            with open(RAW_CACHE, encoding="utf-8") as f:
                data = json.load(f)
        else:
            print("  无法获取模型数据，退出。", file=sys.stderr)
            sys.exit(1)

    models = data.get("data") or []
    free = [m for m in models if is_free(m)]
    print(f"      全量模型 {len(models)} 个，免费模型 {len(free)} 个")

    # 3) 并发增强端点（热门度代理）
    print("[3/6] 抓取各免费模型端点（提供方/在线率/延迟）...")
    enriched = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut = {ex.submit(enrich_endpoints, m): m.get("id") for m in free}
        for f in as_completed(fut):
            mid = fut[f]
            try:
                enriched[mid] = f.result()
            except Exception:  # noqa
                enriched[mid] = (0, None, None, [])
    print(f"      完成 {len(enriched)} 个端点抓取")

    # 4~5) 分类 + 评分
    print("[4/6] 四维分类 ...")
    rows = []
    for m in free:
        mid = m.get("id")
        r_level, r_score, r_strength, r_note, r_measured = classify_reasoning(m)
        c_score, c_tags, c_detail = classify_capability(m)
        w_window, w_score, w_note, age, stab = classify_free_window(m, now_ts)
        pn, avg_up, avg_lat, providers = enriched.get(mid, (0, None, None, []))
        p_score, p_note = classify_popularity(pn, avg_up, avg_lat)

        composite = round(r_strength * 0.40 + c_score * 0.35 + p_score * 0.25, 1)
        # 免费稳定性护栏：作为乘子而非加项，避免临期限免模型被优先推荐
        composite_final = round(composite * stab, 1)

        rows.append({
            "id": mid,
            "name": m.get("name"),
            "description": (m.get("description") or "")[:240],
            "reasoning": {"level": r_level, "score": r_score, "strength": r_strength, "measured": r_measured, "note": r_note},
            "capability": {"score": c_score, "tags": c_tags, "detail": c_detail},
            "free_window": {"window": w_window, "score": w_score, "note": w_note, "age_days": age, "stability_factor": stab},
            "popularity": {"score": p_score, "note": p_note,
                           "providers": pn, "uptime": avg_up, "latency": avg_lat,
                           "provider_list": providers},
            "composite": composite,
            "composite_final": composite_final,
        })

    rows.sort(key=lambda r: r["composite_final"], reverse=True)

    # 6) 推荐：综合 Top3 + 各维度冠军去重
    print("[5/6] 综合评分与推荐 ...")
    top3 = rows[:3]
    champions = {}
    # 推理最强：体量感知护栏 —— 仅在「实测」模型中评选；
    # 优先非轻量变体（flash/small/lite…），避免把快/蒸馏变体误封"推理最强"；
    # 轻量定位里的实测第一另行标注，但不抢占头衔。
    measured_rows = [r for r in rows if r["reasoning"]["measured"]]
    full_size = [r for r in measured_rows if not is_light(r["name"])]
    pool = full_size if full_size else measured_rows
    champions["推理最强"] = max(pool, key=lambda r: r["reasoning"]["strength"])
    if measured_rows:
        top_incl_light = max(measured_rows, key=lambda r: r["reasoning"]["strength"])
        if top_incl_light["id"] != champions["推理最强"]["id"]:
            champions["推理最强(轻量定位第一)"] = top_incl_light
    champions["能力最强"] = max(rows, key=lambda r: r["capability"]["score"])
    champions["最热门"] = max(rows, key=lambda r: r["popularity"]["score"])
    champions["免费最稳"] = max(rows, key=lambda r: r["free_window"]["score"])
    rec_ids = []
    for r in top3:
        if r["id"] not in rec_ids:
            rec_ids.append(r["id"])
    for c in champions.values():
        if c["id"] not in rec_ids:
            rec_ids.append(c["id"])
    # 最多给 3 个：优先综合 Top3，若冠军不在其中则替换最低分
    if len(rec_ids) > 3:
        keep = [r["id"] for r in top3]
        extra = [c["id"] for c in champions.values() if c["id"] not in keep]
        final_ids = keep + extra
        final_ids = final_ids[:3]
    else:
        final_ids = rec_ids
    recommendations = [r for r in rows if r["id"] in final_ids]

    out = {
        "generated_at": now_iso,
        "total_models": len(models),
        "free_count": len(free),
        "weights": {"reasoning": 0.40, "capability": 0.35, "popularity": 0.25},
        "champions": {k: v["id"] for k, v in champions.items()},
        "recommendations": recommendations,
        "all_free": rows,
    }
    with open(os.path.join(BASE, "free_models_classified.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("[6/6] 已写出 free_models_classified.json")

    # 控制台摘要
    print("\n==== 综合推荐 (1-3) ====")
    for i, r in enumerate(recommendations, 1):
        print(f"{i}. {r['name']} ({r['id']})  综合={r['composite']} "
              f"推理={r['reasoning']['strength']} 能力={r['capability']['score']} 热门={r['popularity']['score']}")
    print("\n==== 各维度冠军 ====")
    for k, v in champions.items():
        print(f"{k}: {v['name']} ({v['id']})")
    return out


if __name__ == "__main__":
    main()
