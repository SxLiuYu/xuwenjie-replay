#!/usr/bin/env python3
"""
许文杰每日复盘 v10.0.0 - 基于 v46 Skill 体系
继承 v9.x 数据层配置（DB路径/BIG_CAPS/SECTOR_STOCKS等）
新增 v46 分析层（情绪周期/节点/风格/强区抱团/23定律/异动/许文杰预案）
"""
import sqlite3, sys, time, json, urllib.request, os, re, argparse
from datetime import datetime
from collections import defaultdict

# ===== 命令行参数 =====
parser = argparse.ArgumentParser(description='许文杰每日复盘 v10.0.0')
parser.add_argument('date', nargs='?', default=datetime.now().strftime('%Y-%m-%d'), help='复盘日期 (YYYY-MM-DD)')
parser.add_argument('--db', default=None, help='a_stock_master.db 路径（默认: 脚本同目录）')
parser.add_argument('--si-db', default=None, help='stock_info_master.db 路径（默认: 脚本同目录）')
parser.add_argument('--output', default=None, help='输出 Markdown 路径（默认: 脚本同目录）')
args = parser.parse_args()

# ===== 配置 =====
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATE = args.date
DB = args.db or os.path.join(_SCRIPT_DIR, "a_stock_master.db")
SI_DB = args.si_db or os.path.join(_SCRIPT_DIR, "stock_info_master.db")
OUTPUT = args.output or os.path.join(_SCRIPT_DIR, f"{DATE}-许文杰复盘-v10.0.0.md")

INDEX_CODES = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指", "000688": "科创50"}

BIG_CAPS = {
    "300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信",
    "688256": "寒武纪", "688041": "海光信息", "688047": "龙芯中科",
    "002916": "深南电路", "603501": "韦尔股份",
    "688608": "恒玄科技", "688188": "柏楚电子",
    "002384": "东山精密", "002463": "沪电股份",
}

SECTOR_STOCKS = {
    "AI-存储": ["德明利","澜起科技","江波龙","兆易创新"],
    "PCB中下游": ["大族激光","胜宏科技","科强电子"],
    "AI-芯片": ["寒武纪","海光信息","景嘉微","北方华创"],
    "光通信大票": ["中际旭创","新易盛","天孚通信"],
    "AI-算力": ["浪潮信息","中科曙光","润泽科技"],
    "机器人": ["拓斯达","埃斯顿","绿的谐波"],
}

SKIP_CONCEPTS = {'民企概念','国资概念','国企概念','央企概念','北向资金概念',
    '国家队概念','员工持股概念','外资概念','养老金概念','融资融券概念',
    'MSCI概念','深股通概念','沪股通概念'}

FINNA_URL = "https://www.finna.com.cn/v1/chat/completions"
FINNA_KEY = "app-ULzJbc3OaIN50mZVSU7sAa97"
FINNA_MODEL = "deepseek-v4-flash"

# ===== 数据库连接 =====
conn = sqlite3.connect(DB)
try:
    conn.execute(f"ATTACH DATABASE '{SI_DB}' AS si")
except:
    pass

prev_date = conn.execute("SELECT max(date) FROM daily_kline WHERE date < ?", [DATE]).fetchone()
prev_date = prev_date[0] if prev_date else DATE
all_dates = [r[0] for r in conn.execute(
    "SELECT DISTINCT date FROM daily_kline WHERE date <= ? ORDER BY date DESC LIMIT 30", [DATE]
).fetchall()]
all_dates.sort()

# 股票名称映射
names = {}
try:
    for r in conn.execute("SELECT code, name FROM si.company_profile"):
        names[r[0]] = r[1]
except:
    for r in conn.execute("SELECT code, name FROM si.company_profile LIMIT 1"):
        names[r[0]] = r[0]

output = []
def p(s=""): output.append(s)

# ============================================================
# v46 分析函数
# ============================================================

def load_sector_snapshots(date):
    """加载板块快照（复用v9逻辑）"""
    try:
        return {
            r[0]: {"up_count": r[1], "down_count": r[2], "pct_mean": r[3]}
            for r in conn.execute(
                "SELECT sector, up_count, down_count, pct_mean FROM sector_daily_snapshot WHERE date=?",
                [date]
            ).fetchall()
        }
    except:
        return {}

def _get_sector_for_code(code):
    """简单板块映射"""
    for sector, stocks in SECTOR_STOCKS.items():
        if code in names and names[code] in stocks:
            return sector
    return "其他"

def emotion_cycle_stage():
    """v46: 情绪周期阶段判断"""
    today = conn.execute(
        "SELECT code, pct_change FROM daily_kline WHERE date=? AND volume>0",
        [DATE]
    ).fetchall()
    prev = conn.execute(
        "SELECT code, pct_change FROM daily_kline WHERE date=? AND volume>0",
        [prev_date]
    ).fetchall()
    
    is_688_300 = lambda c: c.startswith('688') or c.startswith('300')
    limits = [r for r in today if r[1] and ((is_688_300(r[0]) and r[1] >= 19.5) or (not is_688_300(r[0]) and r[1] >= 9.5))]
    downs = [r for r in today if r[1] and r[1] <= -9.5]
    
    prev_limits = {r[0] for r in prev if r[1] and r[1] >= 9.5}
    streak = max([2 if r[0] in prev_limits else 1 for r in limits] + [0])
    
    prev_top = [r[0] for r in sorted(prev, key=lambda x: -(x[1] or 0))[:10] if r[1] and r[1] >= 5]
    feedback = sum(1 for pt in prev_top if any(t[0] == pt and t[1] and t[1] > 0 for t in today))
    
    up, down, fb = len(limits), len(downs), feedback/max(len(prev_top),1)
    
    if down >= 8 and up < 20:    s, d = "🧊 冰点", "恐慌释放尾声，准备转折"
    elif up >= 60 and down < 5 and fb > 0.7: s, d = "🔥 上升期", "情绪共振向上，重仓出击"
    elif up >= 80 and fb > 0.5:   s, d = "🚀 高潮", "盛极而衰预警，边涨边减"
    elif down >= 15:              s, d = "🌧️ 退潮期", "全面防守，不开新仓"
    elif up >= 30 and down < 10:  s, d = "⚡ 分歧", "汰弱留强，去后排留前排"
    else:                         s, d = "🔄 混沌期", "无方向震荡，轻仓试错"
    
    return {"stage": s, "desc": d, "up": up, "down": down, "streak": streak, "fb": f"{fb:.0%}", "detail": f"前10高标{feedback}只续涨"}

def node_detect(e):
    """v46: 节点确认"""
    s = e["stage"]
    prev_up = len(conn.execute("SELECT code FROM daily_kline WHERE date=? AND pct_change>=9.5", [prev_date]).fetchall())
    prev_down = len(conn.execute("SELECT code FROM daily_kline WHERE date=? AND pct_change<=-9.5", [prev_date]).fetchall())
    
    if "冰点" in s and prev_down >= 10:  return {"node": "冰点转折(潜在启动)", "advice": "确认：高标正反馈→启动→重仓"}
    elif "退潮" in s and prev_down < e["down"]: return {"node": "退潮确认", "advice": "高位崩塌→中位补跌→空仓等冰点"}
    elif "上升" in s: return {"node": "上升延续", "advice": "持有最强，汰弱留强"}
    elif "分歧" in s: return {"node": "分歧节点", "advice": "等分歧充分→找扛住的→分歧转一致=确认"}
    if "高潮" in s: return {"node": "高潮节点", "advice": "盛极而衰——明天如果竞价不及预期→边涨边减→不开新仓"}
    return {"node": "无明确节点", "advice": "观望为主"}

def style_detect():
    """v46: 风格判断"""
    today = conn.execute(
        "SELECT code, pct_change, amount FROM daily_kline WHERE date=? AND pct_change>=5 AND volume>0 ORDER BY amount DESC",
        [DATE]
    ).fetchall()
    
    boards = {"10cm": 0, "20cm": 0}
    for code, pct, amt in today:
        if code.startswith('688') or code.startswith('300'): boards["20cm"] += 1
        else: boards["10cm"] += 1
    
    big_up = sum(1 for c, p, a in today if c in BIG_CAPS and p >= 3)
    limits = sum(1 for c, p, a in today if p >= 9.5)
    
    if big_up >= 5 and limits < 30:       st, dt = "📊 趋势主导", "大票领涨，低吸回踩"
    elif limits >= 50 and big_up < 3:     st, dt = "🎯 情绪主导", "连板龙头优先，打板/半路"
    elif limits >= 30:                     st, dt = "🤝 抱团行情", "3-5只辨识度票来回做"
    else:                                  st, dt = "🔄 混合", "趋势+情绪并行"
    
    strongest = max(boards, key=boards.get)
    return {"style": st, "detail": dt, "boards": boards, "strongest": strongest, "big_up": big_up, "limits": limits}

def rhythm_23():
    """v46: 23定律"""
    output = []
    hist = defaultdict(list)
    for d in all_dates[-5:]:
        try:
            rows = conn.execute("""
                SELECT sc.concept_name, COUNT(*) as cnt
                FROM daily_kline dk JOIN si.stock_concepts sc ON dk.code = sc.stock_code
                WHERE dk.date=? AND dk.pct_change>=5 GROUP BY sc.concept_name ORDER BY cnt DESC LIMIT 8
            """, [d]).fetchall()
        except:
            continue
        for concept, cnt in rows:
            if concept in SKIP_CONCEPTS: continue
            hist[concept].append(cnt)
    
    for concept, cnts in hist.items():
        if len(cnts) < 2: continue
        last_hot = max([j for j, c in enumerate(cnts) if c >= 3] + [-1])
        if last_hot >= 0:
            ds = len(cnts) - 1 - last_hot
            if ds == 2: output.append(f"🔔 {concept}: 已调整2天 → 23定律修复窗口")
            elif ds == 1: output.append(f"⏳ {concept}: 调整第1天")
            elif ds == 0: output.append(f"⚠️ {concept}: 今日高潮 → 明日分歧预期")
    
    return output[:6] or [" 无明确符合23定律的板块"]

def id_ranking():
    """v46: 辨识度排序"""
    today = conn.execute(
        "SELECT dk.code, dk.pct_change, dk.amount, si.name FROM daily_kline dk "
        "LEFT JOIN si.company_profile si ON dk.code = si.code "
        "WHERE dk.date=? AND dk.pct_change>=5 AND dk.volume>0 ORDER BY dk.pct_change DESC",
        [DATE]
    ).fetchall()
    
    groups = defaultdict(list)
    for code, pct, amt, name in today:
        sec = _get_sector_for_code(code)
        groups[sec].append((name or code, pct, amt))
    
    rankings = []
    for sec, stocks in groups.items():
        if len(stocks) >= 2:
            stocks.sort(key=lambda x: -x[1])
            rankings.append({"sector": sec, "top": [f"{s[0]}({s[1]:+.1f}%)" for s in stocks[:3]]})
    
    rankings.sort(key=lambda x: len(x["top"]), reverse=True)
    return rankings[:8]

def deviation_track():
    """v46: 异动监管线"""
    alerts = []
    for span, threshold in [(10, 100), (30, 200)]:
        sd = all_dates[-span] if len(all_dates) >= span else all_dates[0]
        stocks = conn.execute("""
            SELECT code, ABS(MAX(close)-MIN(close))/MIN(close)*100 as r
            FROM daily_kline WHERE date BETWEEN ? AND ? GROUP BY code HAVING r >= ? ORDER BY r DESC
        """, [sd, DATE, threshold*0.7]).fetchall()
        for code, pct in stocks:
            if pct >= threshold*0.85:
                n = names.get(code, code)
                alerts.append(f"⚠️ {n}: {span}天涨{pct:.1f}%，距{threshold}%异动差{threshold-pct:.1f}%")
    return alerts[:8] or [" 无接近异动线的品种"]

def tomorrow_plan_xwj(e, style, node):
    """v46: 许文杰风格明日预案"""
    s = e["stage"]
    if "退潮" in s:
        return ["**全面防守**", "- 不开新仓不抄底", "- 锚定跌停前排看竞价核按钮", "- 等冰点修复信号"]
    elif "冰点" in s:
        return ["**转折前夜**", "- 竞价看高标正反馈+跌停减少", "- 竞价冰点+白织带→启动确认→重仓", "- 竞价弱→延后一天"]
    elif "上升" in s:
        return ["**顺势做多**", "- 持有最强汰弱留强", "- 首次分歧=加仓，二次分歧=警惕", "- 缩量加速→高潮预警"]
    elif "分歧" in s:
        return ["**等分歧充分**", "- A:先砸后修→低吸前排", "- B:先冲后杀→不追等午后", "- 观察谁扛住了→下波龙头"]
    elif "高潮" in s:
        return ["**盛极而衰预警**", "- 明天竞价关键：如果不及预期→边涨边减", "- 不开新仓，不加仓，不格局", "- 持有的前排看竞价再决定去留"]
    else:
        return ["**混沌期轻仓**", "- 2-3成仓只做最强低吸", "- 不追高不格局快进快出", "- 等方向明确再上仓位"]

# ============================================================
# main() - 复盘九步
# ============================================================

def main():
    t0 = time.time()
    
    # 量能数据
    recent_vols = []
    for vd in all_dates[-5:]:
        amt = conn.execute("SELECT SUM(amount) FROM daily_kline WHERE date=? AND volume>0", [vd]).fetchone()[0] or 0
        recent_vols.append((vd, amt/1e12))
    today_amt = recent_vols[-1][1] if recent_vols else 0
    prev_amt = recent_vols[-2][1] if len(recent_vols) >= 2 else 0
    
    # v46 分析
    e = emotion_cycle_stage()
    node = node_detect(e)
    style = style_detect()
    rhythm = rhythm_23()
    rankings = id_ranking()
    devs = deviation_track()
    
    # === 输出 ===
    p(f"# {DATE} 许文杰复盘 v10.0.0（v46 Skill 体系）")
    p("")
    p(f"> 情绪周期·节点·风格·辨识度·23定律 | 前一交易日: {prev_date}")
    p("")
    
    # 一
    p("## 一、大势与量能")
    p("")
    p("| 指数 | 涨跌幅 | 织带 |")
    p("|------|--------|------|")
    for code, name in INDEX_CODES.items():
        row = conn.execute("SELECT pct_change, open, high, low FROM daily_kline WHERE code=? AND date=?", [code, DATE]).fetchone()
        if row and row[0] is not None:
            pct, o, h, l = row
            sig = "白织带" if (o and l and abs(o-l)/max(o,0.01)<0.002) else ("黑织带" if (o and h and abs(o-h)/max(o,0.01)<0.002) else "—")
            p(f"| {name} | {pct:+.2f}% | {sig} |")
    vc = (today_amt-prev_amt)/prev_amt*100 if prev_amt else 0
    p(f"| 两市成交 | {today_amt:.2f}万亿({vc:+.1f}%) | — |")
    p("")
    
    # 二
    p("## 二、情绪周期与节点")
    p("")
    p(f"**{e['stage']}** — {e['desc']}")
    p("")
    p(f"| 涨停 | 跌停 | 连板高度 | 高标反馈 |")
    p(f"|------|------|---------|---------|")
    p(f"| {e['up']} | {e['down']} | {e['streak']}板 | {e['fb']}({e['detail']}) |")
    p("")
    p(f"**节点：{node['node']}**")
    p(f"> {node['advice']}")
    p("")
    
    # 三
    p("## 三、风格判断")
    p("")
    p(f"**{style['style']}** — {style['detail']}")
    p("")
    p(f"| 10cm | 20cm | 大票强势 | 涨停总数 |")
    p(f"|------|------|---------|---------|")
    p(f"| {style['boards']['10cm']} | {style['boards']['20cm']} | {style['big_up']}只 | {style['limits']}只 |")
    p("")
    
    # 四
    p("## 四、板块分析")
    p("")
    p("### 23 定律节奏")
    p("")
    for r in rhythm:
        p(r)
    p("")
    p("### 辨识度排序")
    p("")
    if rankings:
        p("| 板块 | Top 3 |")
        p("|------|-------|")
        for r in rankings[:6]:
            p(f"| {r['sector']} | {' > '.join(r['top'])} |")
    p("")
    
    # 五、强区抱团
    p("## 五、强区抱团")
    p("")
    try:
        top15 = conn.execute("""
            SELECT dk.code, si.name, dk.amount, dk.pct_change FROM daily_kline dk
            LEFT JOIN si.company_profile si ON dk.code = si.code
            WHERE dk.date=? AND dk.amount>0 ORDER BY dk.amount DESC LIMIT 15
        """, [DATE]).fetchall()
    except:
        top15 = conn.execute("""
            SELECT code, code as name, amount, pct_change FROM daily_kline
            WHERE date=? AND amount>0 ORDER BY amount DESC LIMIT 15
        """, [DATE]).fetchall()
    
    total_amt = sum(r[2] for r in top15) if top15 else 1
    top5_pct = sum(r[2] for r in top15[:5])/total_amt*100 if total_amt else 0
    
    if top5_pct > 15:
        p(f"⚠️ 资金高度集中：前5占全市场{top5_pct:.1f}% → 抱团确认")
        p("")
        
        p("| # | 股票 | 成交额 | 涨跌 |")
        p("|---|------|--------|------|")
        for i, (code, name, amt, pct) in enumerate(top15[:5], 1):
            n = (name or names.get(code) or code)
            p(f"| {i} | {n} | {amt/1e8:.1f}亿 | {pct:+.1f}% |")
    else:
        p(f"资金分布正常（前5占{top5_pct:.1f}%）→ 非抱团")
    p("")
    
    # 六
    p("## 六、异动监管线")
    p("")
    for d in devs:
        p(d)
    p("")
    
    # 七
    p("## 七、明日预案（许文杰风格）")
    p("")
    plan = tomorrow_plan_xwj(e, style, node)
    for line in plan:
        p(line)
    
    elapsed = time.time() - t0
    p("")
    p("---")
    p(f"> v10.0.0 | {elapsed:.1f}s | 基于许文杰短线体系 v46 Skill")
    p(f"> 分析层: 情绪周期→节点→风格→板块(23定律+辨识度)→强区→异动→许文杰预案")
    
    # 写入
    with open(OUTPUT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(output))
    
    print(f"✅ v10.0.0 复盘: {OUTPUT}")
    print(f"   情绪: {e['stage']} | 节点: {node['node']}")
    print(f"   风格: {style['style']} | 涨{e['up']}/跌{e['down']}")
    print(f"   耗时: {elapsed:.1f}s")

if __name__ == '__main__':
    main()
