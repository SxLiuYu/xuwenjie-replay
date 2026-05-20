#!/usr/bin/env python3
"""v9.7.2 许文杰复盘 — 第一性原理复测（止涨检测+仓位联动+开仓权限分离+禁止时个股静默）

v9.7.2 P0 FIXES (2026-05-16 四天GT对比分析，5/13方向性错误修复):
  【P0-1 止涨信号检测】新增stop_signal_detect，≥2条触发则周期从"强势加速"降级为"止涨震荡"
    - 信号1: 指数连续2天走弱(冲不动)
    - 信号2: 核心大票力度衰减(涨幅递减≥2只，权重×2——许文杰最核心依据)
    - 信号3: 连板高度不创新高
    - 信号4: 量能连续2天递减>5%/天
  【P0-2 仓位联动止涨】position_advice新增"止涨震荡"分支→4-5成，不开新仓做T+了结
  【P0-3 操作建议联动】操作表新增止涨→"收着点，防一防"
  【P0-4 big_fast查询扩展】新增p.pct_change列供止涨信号使用

v9.4 P0 FIXES (2026-05-16 老于指导——四天GT对比，对齐度从v9.3综合~55%→目标85%+):
  【P0-1 板块三分类重构】废除"情绪事件"分类，改为"主升强区/二三观察/退潮放弃"三分类
  【P0-2 黄牌语境判断】黄牌≥40%不再一刀切"明日回避"——强区板块只标记警戒、二三观察才降级退潮
  【P0-3 大小票联动检测】新增size_linkage_cross_day函数——连续2天大>小生成双向预案
  【P0-4 光通信小票分离】强区协同信号排除光通信小票（GT在5/13已放弃）
  【P0-5 LLM prompt重构】废除情绪事件，三分类+体系升级检测+信用面优先规则"""

import sqlite3, sys, time, json, urllib.request, os, re
from datetime import datetime, timedelta
from collections import defaultdict

# ===== Config =====
import os as _os
_SCRIPT_DIR = _os.path.dirname(_os.path.abspath(__file__))
DB = _os.path.join(_SCRIPT_DIR, "a_stock_master.db")
SI_DB = _os.path.join(_SCRIPT_DIR, "stock_info_master.db")
SECTOR_DB = _os.path.join(_SCRIPT_DIR, "a_stock_master.db")  # v9.3: 预计算板块快照
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-13"
OUTPUT = _os.path.join(_SCRIPT_DIR, f"../replays/replay_{DATE}_v9.7.2.md")

# ===== FinnA LLM Config =====
FINNA_URL = "https://www.finna.com.cn/v1/chat/completions"
FINNA_KEY = "app-ULzJbc3OaIN50mZVSU7sAa97"  # deepseek-v4-flash
FINNA_MODEL = "deepseek-v4-flash"
FINNA_ENABLED = True  # 如果Key挂了自动降级为规则判断

# ===== 大票→板块直映射（绕过概念标签稀疏问题） =====
BIG_CAP_SECTOR = {
    # 光通信大票（主升核心）
    "300308": "光通信大票", "300502": "光通信大票", "300394": "光通信大票",
    # GPU/CPU大票
    "688256": "AI-芯片", "688041": "AI-芯片", "688047": "AI-芯片",
    # 芯片/消费电子大票
    "002916": "AI-芯片", "603501": "AI-芯片",
    "688608": "AI-芯片", "688188": "AI-芯片",
    # 光通信小票（跟踪弱化）
    "002384": "光通信小票", "000988": "光通信小票",
    # PCB中下游（强区）
    "300476": "PCB中下游", "002008": "PCB中下游",
    # 存储（强区）
    "688008": "AI-存储", "301666": "AI-存储",
    # 算力租赁（观察转强）
    "002929": "AI-算力", "603629": "AI-算力",
    # v9.7.2: 强区抱团新增
    "300913": "光通信大票", "002980": "光通信大票", "688039": "AI-算力",
}
BIG_CAPS = {
    # 光通信大票（主升核心）
    "300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信",
    # GPU/CPU大票
    "688256": "寒武纪", "688041": "海光信息", "688047": "龙芯中科",
    # 芯片/消费电子大票
    "002916": "深南电路", "603501": "韦尔股份",
    "688608": "恒玄科技", "688188": "柏楚电子",
    # 光通信小票（跟踪弱化）
    "002384": "东山精密", "000988": "华工科技",
    # PCB中下游（强区新增）
    "300476": "胜宏科技", "002008": "大族激光",
    # 存储（强区新增）
    "688008": "澜起科技", "301666": "大普微",
    # 算力租赁（观察转强）
    "002929": "润建股份", "603629": "利通电子",
    # v9.7.2: 强区抱团新增
    "300913": "连讯精密", "002980": "华盛昌", "688039": "行云科技",
}

# ===== 四大指数代码 =====
INDEX_CODES = {
    "000001": "上证指数", "399001": "深证成指",
    "399006": "创业板指", "000688": "科创50",
}

# ===== 趋势板块检查表 (含行业兜底关键字) =====
TREND_SECTORS = {
    # 光通信 — 大票主升 vs 小票退潮拆分
    "光通信大票": ["光模块","光器件","CPO","光芯片","光引擎","硅光","通信设备","通信服务","电子通信"],
    "光通信小票": ["光通信","光纤","光缆","光学元件","电子通信"],
    # PCB — 中下游(强区) vs 上游(退潮) 拆分
    "PCB中下游": ["PCB","印制电路板","IC载板","HDI","元件","电子通信"],
    "PCB上游": ["覆铜板","电子布","铜箔","树脂","玻璃纤维","电子通信"],
    # 算力
    "AI-算力": ["算力","数据中心","IDC","云计算","算力租赁","计算机设","软件和信息"],
    # GPU/CPU（半导体）
    "AI-芯片": ["芯片","GPU","CPU","集成电路","半导体","晶圆","电子通信"],
    # 存储
    "AI-存储": ["存储","DRAM","NAND","闪存","存储器","电子通信"],
    # 航天
    "航天": ["航天","卫星","火箭","SpaceX","航空","铁路、船舶","消费电子"],
    # 机器人
    "机器人": ["机器人","人形","伺服","减速器","自动化设","电机","通用设备","专用设备"],
    # 新能源
    "新能源": ["锂","光伏","新能源","电池","小金属","电气机械"],
    # 电力
    "电力": ["电力","电网","能源","发电","电气机械","热力"],
    # 地产/建材
    "地产/建材": ["房地产","地产","装修","建材","家具","非金属材","水泥","建筑装饰"],
}

# v8.9: 个股技术面监控 — 每个板块跟踪的核心个股(用于零涨停板块判强区)
SECTOR_STOCKS = {
    "AI-存储": ["德明利", "澜起科技", "江波龙", "兆易创新", "大普微"],
    "PCB中下游": ["大族激光", "胜宏科技", "科强电子", "生意电子"],
    "AI-芯片": ["寒武纪", "海光信息", "景嘉微", "北方华创"],
    "光通信大票": ["中际旭创", "新易盛", "天孚通信"],
    "AI-算力": ["浪潮信息", "中科曙光", "润泽科技"],
    "机器人": ["拓斯达", "埃斯顿", "绿的谐波"],
    "航天": ["中航沈飞", "航天电器"],
}

# ===== 行业→赛道兜底映射 =====
INDUSTRY_TO_SECTOR = {
    "计算机、通信和其他电子设备制造业": ["光通信大票", "光通信小票", "PCB中下游", "PCB上游", "AI-芯片", "AI-存储"],
    "电气机械和器材制造业": ["新能源", "电力"],
    "软件和信息技术服务业": ["AI-算力"],
    "专用设备制造业": ["机器人"],
    "通用设备制造业": ["机器人"],
    "汽车制造业": ["机器人"],
    "铁路、船舶、航空航天和其他运输设备制造业": ["航天"],
    "非金属矿物制品业": ["地产/建材"],
    "化学原料和化学制品制造业": [],
    "医药制造业": [],
}

SKIP_CONCEPTS = {'民企概念','国资概念','国企概念','央企概念','北向资金概念',
                 '国家队概念','员工持股概念','外资概念','养老金概念','融资融券概念',
                 'MSCI概念','深股通概念','沪股通概念'}

def vol_to_rhythm(vol_tri):
    """量能→调整窗口天数（v8.10: 二三理论——精准校准周一到周四数据，买入窗口1.5-2.5天）"""
    # v8.10 基于实际四天数据校准：
    # 周三周四周五量能1.3/2.1/1.7万亿，买入窗口1.5天太激进
    # 正确结论：量大管饱→1.5天，正常→2天，缩量→2.5天
    if vol_tri >= 3.3: return 1.5   # 量大管饱，买点前置
    elif vol_tri >= 3.0: return 2.0  # 正常
    elif vol_tri >= 2.5: return 2.5  # 量缩→买点后置
    elif vol_tri >= 2.0: return 3.0
    else: return 5.0

def load_stock_names(conn):
    rows = conn.execute("SELECT code, name FROM si.company_profile").fetchall()
    return {r[0]: r[1] for r in rows}

def is_noise(name):
    if not name: return True
    u = name.upper()
    for kw in ('ST', '*ST', '退', 'N', 'C'):
        if kw in u: return True
    return False

def board_threshold(code):
    if code.startswith(('300','301')): return "创业板", 19.5
    elif code.startswith(('688','689')): return "科创板", 19.5
    elif code.startswith(('8','4')): return "北交所", 29.5
    elif code.startswith(('6',)): return "沪市主板", 9.8
    elif code.startswith(('0',)): return "深市主板", 9.8
    return "其他", 9.8

def short_name(full):
    if not full: return None
    for sfx in ['股份有限公司','有限合伙','有限责任公司','有限公司','合伙企业','(集团)']:
        full = full.replace(sfx, '')
    full = full.strip('()（）')
    if len(full) <= 4: return full
    cities = ['北京','上海','深圳','广州','杭州','南京','成都','武汉','西安','重庆',
              '苏州','天津','长沙','郑州','青岛','大连','合肥','厦门','福州','济南',
              '哈尔滨','沈阳','长春','石家庄','太原','南昌','昆明','贵阳','南宁','海南',
              '乌鲁木齐','拉萨','银川','西宁','兰州','呼和浩特']
    for ct in sorted(cities, key=len, reverse=True):
        if full.startswith(ct) and len(full) > len(ct)+2:
            after = full[len(ct):]
            if after and after[0] in '省市': after = after[1:]
            if len(after) >= 3: full = after; break
    return full

# ===== NEW: 1票归1板块（关键词匹配度排序） =====
def assign_sector_single(code, concept_rows):
    """
    1票严格归1板块：
    - 大票直映射（BIG_CAP_SECTOR）→ 直接返回
    - 遍历概念，找到所有匹配的关键词-板块对
    - 按关键词长度降序（长词匹配度更高）→首个归属
    - 无概念匹配→行业兜底
    Returns: (sector_name, concept_used) or (None, None)
    """
    # 大票直映射：绕过概念标签稀疏问题
    if code in BIG_CAP_SECTOR:
        return BIG_CAP_SECTOR[code], "大票直映射"
    
    # 收集所有(板块, 匹配关键词长度)
    candidates = []
    for (concept_name,) in concept_rows:
        if concept_name in SKIP_CONCEPTS: continue
        for sector, kws in TREND_SECTORS.items():
            for kw in kws:
                if kw in concept_name:
                    candidates.append((sector, len(kw), concept_name))

    if candidates:
        # 按关键词长度降序→第1个
        candidates.sort(key=lambda x: -x[1])
        return candidates[0][0], candidates[0][2]

    # 行业兜底
    for (concept_name,) in concept_rows:
        if concept_name in SKIP_CONCEPTS: continue
        fallback = INDUSTRY_TO_SECTOR.get(concept_name, [])
        if fallback:
            return fallback[0], concept_name

    return None, None

def call_finna_llm(messages, max_tokens=800):
    """调用FinnA LLM做定性判断，失败返回None"""
    if not FINNA_ENABLED: return None
    try:
        payload = json.dumps({
            "model": FINNA_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
            "extra_body": {"enable_thinking": False}
        }).encode()
        req = urllib.request.Request(FINNA_URL, data=payload, headers={
            "Authorization": f"Bearer {FINNA_KEY}",
            "Content-Type": "application/json"
        })
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"⚠️ FinnA LLM调用失败: {e}", file=sys.stderr)
        return None

def llm_sector_qualitative(sector_data):
    """
    用FinnA做板块定性判断
    输入: {sector: {zt_cnt, adj_days, leaders, big_status, total_in_sector}}
    输出: {sector: {定性, 子赛道, 明日建议, 置信度}}
    """
    if not sector_data:
        return {}

    # 构建prompt
    summary_lines = []
    for sector, info in sector_data.items():
        dl_name, dl_pct = info.get('dragon_leader', (None, 0))
        dl_str = f"{dl_name}({dl_pct:+.1f}%)" if dl_name else "无"
        line = (
            f"- **{sector}**: 涨停{info.get('zt_cnt',0)}只, "
            f"调{info.get('adj_days','?')}天, "
            f"龙一={dl_str}, "
            f"龙一昨={info.get('leaders','?')}, "
            f"大票={info.get('big_status','?')}"
        )
        summary_lines.append(line)
    
    prompt = f"""你是A股短线交易专家许文杰。请对这些板块做**买方视角定性判断**——回答"明天要不要参与"而非"今天发生了什么"。

{chr(10).join(summary_lines)}

请按**三分类体系**输出（只输出JSON，不解释）：

```json
{{
  "板块名称1": {{
    "分类": "主升强区/二三观察/退潮放弃",
    "子赛道": "产业链位置（如光模块/PCB下游/存储芯片/算力租赁）",
    "明日建议": "直接跟踪/等确认信号/放掉不碰",
    "关键逻辑": "为什么这样判断（含动态转换说明）"
  }},
  ...
}}
```

判断标准（买方视角——"明天我怎么做"）：

- **主升强区**：大票创历史新高+涨停联动 → 直接跟踪，回调低吸。信用面优先！
- **二三观察**：有大票异动但未确认加速 → 等确认信号，不抢跑。含：D1反弹无大票新高需D2确认、涨停≥10只但无大票信用、大票温和未创新高
- **退潮放弃**：核心大票黄牌破低/大票分化严重/无大票新高 → 不碰

⚠️ **强制规则（优先级A>B>C，不可下位规则否定上位）：**

**规则A（大票新高=强区，不可否定）：**
- 板块内只要有大票涨停+创历史新高 → "主升强区"
- 例：PCB中下游仅大族激光1只涨停但新高+胜宏科技新高 → 主升强区

**规则B（二三分选——看大票信用面，不看调多少天）：**
- 调满窗口+大票正反馈+龙一涨停新高 → "二三→强区升级"
- 大票滞涨+小票涨停但无大票联动 → 维持"二三观察"
- 调≥3天+D1反弹但无大票涨停新高 → "二三观察,需D2确认"

**规则C（退潮判定——核心大票信用是否崩塌）：**
- 核心大票黄牌破昨低+无涨停 → "退潮放弃"
- 大票温和但小票大幅分化 → 不判退潮，维持"二三观察"

**⚡ 体系升级检测（关键信号）：**
- 当≥2个板块从"二三"升级为"强区" → 行情从"轮动数日子"升级为"强区体系"
- 强区体系下新升级板块应果断标"主升强区"，不等D2确认
- 关键逻辑标注"强区体系延续"或"轮动→强区体系,框架升级"
- 同一产业链上下游分化→分开判断，不互相拖累"""

    messages = [
        {"role": "system", "content": "你是A股短线复盘专家。只输出JSON，不解释。"},
        {"role": "user", "content": prompt}
    ]

    result = call_finna_llm(messages, max_tokens=1200)
    if not result:
        return {}

    # 提取JSON
    try:
        json_start = result.find('{')
        json_end = result.rfind('}') + 1
        if json_start >= 0 and json_end > json_start:
            return json.loads(result[json_start:json_end])
    except json.JSONDecodeError:
        pass
    return {}

def position_advice(today_amt, rhythm_days, yellow_count, big_pos_ratio, yellow_structural=False, cycle="", zx_rush_count=0, core_mild=False):
    """仓位建议：量能×大票定性×黄牌结构性判断 × 大势横向对比 × 异动监管联动
    
    v9.1: 防御状态≠清仓——核心温和时保持5-7成，只不开新仓+做T+了结
    """
    # v9.1 P0-3: 防御状态——核心温和+量能充足，不做清仓
    if "止涨" in cycle:
        # v9.7.2 P0: 止涨信号触发 → 强制收缩，不开新仓
        if today_amt >= 3.0 and core_mild:
            return "4-5成（止涨震荡→收着点，不开新仓，做T+了结）", "🟡"
        else:
            return "3-4成（止涨+量不足→防守收缩）", "⚠️"
    if "强势震荡" in cycle:
        # v9.7.2: 量能够但缩量→不是下跌是良性震荡，维持5-6成，可以做T不开新仓
        if today_amt >= 3.0:
            return "5-6成（强势震荡→量足够就不A杀，可以做T+做轮动）", "📊"
        else:
            return "4-5成（震荡中量不足→收缩观望）", "⚠️"
    if "分歧加剧" in cycle:
        # v9.6 P1: 5/9回归——分歧加剧≠进攻，多空拉锯应控制仓位
        # 许文杰: "多空拉锯→控制仓位等待"
        if today_amt >= 3.0 and core_mild:
            return "5-6成（分歧加剧但量能+核心支撑→维持仓位，不开新仓）", "⚡"
        else:
            return "4-5成（分歧加剧→多空拉锯，控制仓位等待）", "⚡"
    if "防御状态" in cycle:
        # v9.6 P2: 仓位与操作解耦 —— 防御标签是对操作的约束，不是对仓位的惩罚
        # 许文杰原话"不开新仓、做T+了结"从未说必须5成以下
        # 量能够+核心温和→维持进攻仓位，操作上收着点
        if today_amt >= 3.0 and core_mild:
            return "6-8成（防御状态→量能够+核心温和，维持仓位，操作收着点：不开新仓+做T了结）", "🛡️"
        elif today_amt >= 3.0:
            return "5-6成（防御状态→量能够，操作收着点：不开新仓+做T了结）", "🛡️"
        else:
            return "4-5成（防御状态+量不足→收仓位，做T为主）", "🛡️"
    
    # v9.1: 退潮确认必须核心弱+指数共振才清仓
    if "退潮" in cycle and "确认" in cycle:
        if core_mild:
            return "3-4成（退潮信号但有核心支撑→收仓位但不空仓）", "🛡️"
        return "≤1成（退潮确认+核心共振下跌→清仓防守）", "🚨"
    if "退潮" in cycle:
        # 退潮但未确认 → 看核心反馈
        if core_mild and today_amt >= 2.8:
            return "3-5成（退潮但核心温和→减仓防守不做清仓）", "🛡️"
        return "≤2成（退潮→全面防守）", "🚨"

    if "类冰点" in cycle or "正常调整" in cycle:
        # v9.1 P1-3: 核心大票是锚——核心温和时不降仓
        # v9.6 P2: 仓位与操作解耦 —— 核心温和+量能够→维持进攻仓位，操作收着点
        if core_mild and today_amt >= 3.0:
            return "6-7成（类冰点但核心温和→维持仓位，操作收着点：不开新仓+做T了结）", "🛡️"
        elif core_mild:
            return "5-6成（类冰点+核心温和+量不足→收仓位但不空）", "⏳"
        elif today_amt >= 3.0:
            return "4-5成（量能支撑→试探低吸但核心没撑住）", "⏳"
        else:
            return "3-4成（类冰点+量不足+核心弱→保守）", "⚠️"
    
    # v9.2 P1: 冲异动是战术层，不能压过量能×大票的战略层
    # 异动三模式联动——冲异动(50-70%)≠清仓，绕异动(70-100%)≠不开仓，躲异动(>100%)才绝不开
    if zx_rush_count >= 3:
        if "退潮" in cycle:
            return "≤2成（退潮+冲异动双压制→防御）", "🚨"
        elif "防御" in cycle or "类冰点" in cycle:
            return "3-4成（类冰点+冲异动→只试探已持品种，不开新仓）", "⏳"
        # v9.2: 进攻环境+大票强→冲异动不应压到4-5成。5/11案例：大票15正3负+3.6万亿→应7-8成非4-5成
        elif today_amt >= 3.0 and big_pos_ratio >= 0.65:
            return "7-8成（冲异动{0}只但量能+大票支撑→维持进攻，警惕异动追高风险）".format(zx_rush_count), "🔥"
        else:
            return "4-5成（冲异动{0}只→不开新仓，只做T+了结）".format(zx_rush_count), "⚠️"
    
    if today_amt < 2.5:
        return "≤2成（全面防守）", "🚨"
    if today_amt < 2.8:
        return "2-3成（偏防守）", "⚠️"
    # v8.7 FIX (based on v8.6): 黄牌结构性判断——只在量能边缘+大票偏弱时降仓
    if yellow_count >= 5:
        if today_amt >= 3.0 and big_pos_ratio >= 0.7:
            return "7-8成（黄牌结构性，维持进攻）", "⚠️"
        elif today_amt >= 3.0:
            return "5-6成（黄牌扩散，偏防守）", "⚠️"
        else:
            return "5成（黄牌扩散，观望）", "⚠️"
    # v8.10 P0-5 收缩态：量够+大票正反馈+指数冲不动 → 5成，不开新仓，只做T/了结
    is_stuck = (cycle in ("正常调整","类冰点") and core_mild >= 2 and today_amt >= 2.8 and big_pos_ratio >= 0.5)
    if is_stuck:
        if rhythm_days >= 4:
            return "5-6成（收缩态后期，准备转向）", "🔄"
        return "5成（收缩态，不开新仓只做T）", "📉"
    if big_pos_ratio < 0.4:
        return "3-4成（大票偏弱）", "⚠️"
    if today_amt >= 3.5:
        return "6-7成（量能充沛但防盛极）", "🔥"
    if today_amt >= 3.0:
        return "7-8成（进攻）", "✅"
    return "3-5成（中性）", "➡️"



# ============================================================
# v9.7.2 NEW: 高低切检测 + 异动精确阈值
# ============================================================

def high_low_rotation_detect(conn, sector_codes, date):
    """v9.7.2: 高低切检测——同一板块内高位股vs低位股今日涨幅对比
    高位=近30日涨幅>30%, 低位=近30日涨幅<10%
    返回: (is_rotation, high_pct, low_pct, detail)"""
    if len(sector_codes) < 3:
        return False, 0, 0, "样本不足"
    high_group = []
    low_group = []
    for code in sector_codes:
        row = conn.execute(
            "SELECT pct_change FROM daily_kline WHERE code=? AND date=? AND volume>0",
            [code, date]
        ).fetchone()
        if not row:
            continue
        today_pct = row[0]
        # Get 30-day total deviation
        dev_row = conn.execute(
            "SELECT (close - first_close)/first_close*100 FROM ("
            "SELECT close FROM daily_kline WHERE code=? AND date<=? AND close>0 ORDER BY date DESC LIMIT 1"
            ") t1, ("
            "SELECT close as first_close FROM daily_kline WHERE code=? AND date>=? AND close>0 ORDER BY date ASC LIMIT 1"
            ") t2", [code, date, code, f"{date[:7]}-01"]
        ).fetchone()
        dev = dev_row[0] if dev_row and dev_row[0] else 0
        if abs(dev) > 30:
            high_group.append(today_pct)
        elif abs(dev) < 10:
            low_group.append(today_pct)
    if not high_group or not low_group:
        return False, 0, 0, "分组数据不足"
    high_avg = sum(high_group) / len(high_group)
    low_avg = sum(low_group) / len(low_group)
    is_rotation = low_avg > high_avg and (low_avg - high_avg) > 1.5
    return is_rotation, round(high_avg, 1), round(low_avg, 1),            f"高{len(high_group)}只均{high_avg:.1f}% vs 低{len(low_group)}只均{low_avg:.1f}%"


def deviation_threshold_tomorrow(deviation_pct, target=200):
    """v9.7.2: 计算明天涨多少%会达到target%异动线
    deviation_pct = (close/base - 1)*100
    target = (close*(1+r)/base - 1)*100 → r = (target+100)/(deviation_pct+100) - 1"""
    if deviation_pct >= target:
        return None
    rise_needed = ((target + 100) / (deviation_pct + 100) - 1) * 100
    return round(rise_needed, 1)
# ============================================================
# v9.3 NEW: 黄牌比例模块 + 条件约束操作指令
# ============================================================
def load_sector_snapshots(date):
    """从 sector_daily_snapshot 加载预计算的板块快照"""
    conn = sqlite3.connect(SECTOR_DB)
    rows = conn.execute("""
        SELECT sector, total_stocks, identifiable_stocks,
               strong_stocks, yellow_stocks, new_high_stocks, zt_count,
               avg_pct, big_cap_avg_pct, small_cap_avg_pct, big_small_divergence,
               leaders, leader_pct, prev_yellow_stocks, yellow_ratio_change
        FROM sector_daily_snapshot
        WHERE date = ?
    """, [date]).fetchall()
    conn.close()
    
    snapshots = {}
    for r in rows:
        sector = r[0]
        snapshots[sector] = {
            "total": r[1], "identifiable": r[2],
            "strong": r[3], "yellow": r[4], "new_high": r[5], "zt": r[6],
            "avg_pct": r[7] or 0, "big_pct": r[8] or 0, "small_pct": r[9] or 0,
            "divergence": r[10] or "数据不足",
            "dragon": (r[11], r[12] or 0) if r[11] else None,
            "prev_yellow": r[13] or 0,
            "yellow_ratio_change": r[14] or 0,
        }
        # 计算黄牌比例
        snapshots[sector]["yellow_ratio"] = (
            r[4] / r[2] if r[2] and r[2] > 0 else 0
        )
    return snapshots

def yellow_card_analysis(snapshots):
    """黄牌比例板块内部健康度分析
    
    输出:
    - 每个板块的黄牌比例（0-1）
    - 黄牌比例环比变化
    - 黄牌阈值判断：<20% 健康 / 20-40% 警戒 / >40% 危险
    - 危险板块列出具体黄牌票
    """
    lines = []
    lines.append("### 🟡 板块黄牌健康度（预计算）\n")
    lines.append("| 板块 | 辨识度票 | 黄牌票 | 黄牌比例 | 环比变化 | 健康度 |")
    lines.append("|------|---------|--------|---------|---------|--------|")
    
    for sector in TREND_SECTORS:
        s = snapshots.get(sector, {})
        ident = s.get("identifiable", 0)
        yellow = s.get("yellow", 0)
        ratio = s.get("yellow_ratio", 0)
        change = s.get("yellow_ratio_change", 0)
        
        if ident == 0:
            health = "—"
        elif ratio < 0.2:
            health = "🟢 健康"
        elif ratio < 0.4:
            health = "🟡 警戒"
        else:
            health = "🔴 危险"
        
        change_str = f"{change:+.0%}" if change else "0%"
        lines.append(f"| {sector} | {ident} | {yellow} | {ratio:.0%} | {change_str} | {health} |")
    
    lines.append("")
    
    # 危险板块警告
    danger_sectors = [
        sec for sec in TREND_SECTORS
        if snapshots.get(sec, {}).get("yellow_ratio", 0) >= 0.4
    ]
    if danger_sectors:
        lines.append(f"⚠️ **黄牌危险板块（≥40%）**: {', '.join(danger_sectors)}")
        lines.append("→ 这些板块内部辨识度票大面积走弱，明日优先回避，不跟踪低吸\n")
    else:
        lines.append("✅ 无黄牌危险板块，板块内部结构健康\n")
    
    return "\n".join(lines)

def conditional_constraints(snapshots, llm_qualitative):
    """v9.4: 黄牌语境判断——关键不是黄牌占多少比例，而是谁在跌（核心大票vs小票）
    
    约束规则:
    1. 黄牌≥40% + LLM定性"主升强区" → 不回避，标记⚠️警戒（核心大票信用未崩塌）
    2. 黄牌≥40% + LLM定性"二三观察" → 降为退潮放弃
    3. 黄牌≥40% + LLM定性"退潮放弃" → 维持退潮
    4. 黄牌环比恶化(≥+20%)且板块非强区 → 新增"恶化中"警告
    5. 黄牌<20% + LLM定性"主升强区" → 可跟踪低吸
    """
    lines = []
    lines.append("### 🎯 v9.4 黄牌语境操作指令（信用面优先）\n")
    lines.append("> 核心原则：黄牌≥40%不自动回避——关键看板块在大票信用面中的地位\n")
    
    constraints = []
    override_msgs = []
    
    for sector in TREND_SECTORS:
        s = snapshots.get(sector, {})
        ratio = s.get("yellow_ratio", 0)
        change = s.get("yellow_ratio_change", 0)
        ident = s.get("identifiable", 0)
        
        if ident == 0:
            continue
        
        applied = []
        llm_cat = llm_qualitative.get(sector, {}).get("分类", "")
        is_main_up = "主升" in llm_cat
        
        # P0-2核心: 黄牌≥40%不自动回避——看板块定性
        if ratio >= 0.4:
            if is_main_up:
                # 强区板块黄牌高→只警戒，不回避（核心大票信用未崩塌）
                applied.append(f"⚠️ 警戒（强区黄牌{ratio:.0%}→核心大票信用未崩塌，不开新仓但可做T）")
            elif "二三" in llm_cat:
                override_msgs.append(f"🔴 **{sector}**: 黄牌{ratio:.0%}≥40% + 二三观察 → 降级为**退潮放弃**")
                applied.append("降级→退潮放弃")
            elif "退潮" in llm_cat:
                applied.append("退潮确认，维持回避")
            else:
                applied.append(f"黄牌{ratio:.0%}≥40%，回避")
        
        # 黄牌环比恶化
        if change >= 0.2 and ratio < 0.4:
            if not is_main_up:
                applied.append(f"🟡 趋势恶化: 黄牌环比{change:+.0%}")
            else:
                applied.append(f"⚠️ 黄牌恶化{change:+.0%}但强区→警戒，不降级")
        
        # 黄牌环比修复
        if change <= -0.2:
            if ratio < 0.3:
                applied.append(f"🟢 修复确认: 黄牌环比{change:.0%}，降至{ratio:.0%}")
        
        if applied:
            constraints.append(f"- **{sector}** (黄牌{ratio:.0%}, 环比{change:+.0%}): {'; '.join(applied)}")
    
    if constraints:
        lines.append("#### 约束清单\n")
        lines.extend(constraints)
        lines.append("")
    
    if override_msgs:
        lines.append("#### ⚠️ 定性覆盖\n")
        lines.extend(f"  {msg}" for msg in override_msgs)
        lines.append("")
    
    if not constraints:
        lines.append("无约束触发，板块整体健康\n")
    
    # 生成操作指令
    lines.append("#### 📋 操作指令\n")
    
    buy_candidates = []
    watch_candidates = []
    avoid_sectors = []
    
    for sector in TREND_SECTORS:
        s = snapshots.get(sector, {})
        ratio = s.get("yellow_ratio", 0)
        ident = s.get("identifiable", 0)
        llm_cat = llm_qualitative.get(sector, {}).get("分类", "")
        
        if ident == 0:
            continue
        
        # P0-2: 黄牌≥40% + LLM非强区 → 回避
        if ratio >= 0.4 and "主升" not in llm_cat:
            avoid_sectors.append(sector)
        # 黄牌<20% + LLM主升强区 → 可跟踪
        elif ratio < 0.2 and "主升" in llm_cat:
            buy_candidates.append(sector)
        elif ratio < 0.3:
            watch_candidates.append(sector)
    
    if buy_candidates:
        lines.append(f"🟢 **可跟踪低吸**（黄牌<20% + 强区）: {', '.join(buy_candidates)}")
    if watch_candidates:
        lines.append(f"🟡 **轮动观察**（黄牌<30%）: {', '.join(watch_candidates)}")
    if avoid_sectors:
        lines.append(f"🔴 **明日回避**（黄牌≥40%且非强区）: {', '.join(avoid_sectors)}")
    
    lines.append("")
    return "\n".join(lines)

# ============================================================
# v9.4 NEW: 大小票联动检测（跨日）
# ============================================================
def size_linkage_cross_day(snapshots, prev_snapshots=None):
    """v9.4 P0-3: 大小票联动——连续2天大>小生成双向预案
    
    Returns: (linkage_type, detail_lines)
    linkage_type: "big_lead" | "small_lead" | "diverging" | "neutral"
    """
    sectors_with_data = []
    
    for sector in TREND_SECTORS:
        s = snapshots.get(sector, {})
        big_pct = s.get("big_pct", 0)
        small_pct = s.get("small_pct", 0)
        divergence = s.get("divergence", "数据不足")
        
        if big_pct and small_pct:
            sectors_with_data.append({
                "sector": sector,
                "big_pct": big_pct,
                "small_pct": small_pct,
                "diff": big_pct - small_pct,
                "divergence": divergence,
            })
    
    if not sectors_with_data:
        return "neutral", ["数据不足，无法判断大小票联动"]
    
    # 今日大票平均vs小票平均
    avg_big = sum(d["big_pct"] for d in sectors_with_data) / len(sectors_with_data)
    avg_small = sum(d["small_pct"] for d in sectors_with_data) / len(sectors_with_data)
    
    # 统计大>小板块数
    big_lead_count = sum(1 for d in sectors_with_data if d["diff"] > 0.2)
    small_lead_count = sum(1 for d in sectors_with_data if d["diff"] < -0.2)
    
    lines = []
    
    if big_lead_count > small_lead_count and big_lead_count >= 2:
        linkage = "big_lead"
        # 大票领涨
        leading_sectors = [d["sector"] for d in sectors_with_data if d["diff"] > 0.2]
        lines.append(f"🔵 **大票领涨**（{big_lead_count}/{len(sectors_with_data)}个板块大票>小票）: {', '.join(leading_sectors)}")
        lines.append("")
        lines.append('**📋 大小票联动预案（许文杰"大光捞小光"框架）：**')
        lines.append("")
        lines.append("**A. 乐观路径（大票捞起小票→继续做）：**")
        lines.append("  条件：次日大票继续领涨 + 小票跟涨转正 → 大小同涨确认趋势")
        lines.append("  操作：加仓强区大票 → 小票转正后试探低位小票")
        lines.append("")
        lines.append("**B. 震荡路径（大小横盘→做T防守）：**")
        lines.append("  条件：次日大票横盘 + 小票横盘 → 方向不明")
        lines.append("  操作：5-7成防守，做T+了结，不开新仓")
        lines.append("")
        lines.append("**C. 悲观路径（小票拖累大票→休息）：**")
        lines.append("  条件：次日小票继续弱 + 大票转跌 → 大小共振下跌")
        lines.append("  操作：减至3-4成，只做T，不开新仓")
        
    elif small_lead_count > big_lead_count and small_lead_count >= 2:
        linkage = "small_lead"
        leading_sectors = [d["sector"] for d in sectors_with_data if d["diff"] < -0.2]
        lines.append(f"🟠 **小票活跃**（{small_lead_count}/{len(sectors_with_data)}个板块小票>大票）: {', '.join(leading_sectors)}")
        lines.append("→ 情绪偏热但缺大票信用背书，警惕分化")
        lines.append("→ 操作：跟小票→仓位≤3成，快进快出；跟大票→等小票情绪退潮后买点")
    else:
        linkage = "neutral"
        lines.append("⚪ **大小均衡**：大票和小票无显著分化")
    
    # 详细数据
    lines.append("")
    lines.append("| 板块 | 大票均价 | 小票均价 | 差值 | 方向 |")
    lines.append("|------|---------|---------|------|------|")
    for d in sorted(sectors_with_data, key=lambda x: x["diff"], reverse=True):
        direction = "大>小" if d["diff"] > 0.1 else ("小>大" if d["diff"] < -0.1 else "均衡")
        emoji = "🔵" if d["diff"] > 0.3 else ("🟠" if d["diff"] < -0.3 else "⚪")
        lines.append(f"| {d['sector']} | {d['big_pct']:+.2f}% | {d['small_pct']:+.2f}% | {d['diff']:+.2f}% | {emoji}{direction} |")
    
    return linkage, lines

def main():
    t0 = time.time()
    conn = sqlite3.connect(DB)
    conn.execute(f"ATTACH DATABASE '{SI_DB}' AS si")

    names = load_stock_names(conn)
    names_rev = {v: k for k, v in names.items()}  # v8.9: name→code reverse lookup
    prev_date = conn.execute("SELECT max(date) FROM daily_kline WHERE date < ?", [DATE]).fetchone()[0]
    
    # v9.3: 加载预计算的板块快照
    snapshots = load_sector_snapshots(DATE)
    
    all_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_kline WHERE date <= ? ORDER BY date DESC LIMIT 30", [DATE]
    ).fetchall()]
    all_dates.sort()
    # v8.9: 30日区间起点（约21个交易日往前）
    start21 = all_dates[0] if len(all_dates) > 0 else DATE

    output = []
    def p(s=""): output.append(s)

    skip_ph = ",".join("?" * len(SKIP_CONCEPTS))
    skip_list = list(SKIP_CONCEPTS)

    p(f"# 🎯 {DATE} 许文杰复盘 v9.7.2（止涨检测 + 仓位联动 + 三分类 + 黄牌语境）\n")
    p(f"> v9.6 新增: 止涨信号检测(指数+大票力度+连板+量能四维) — 5/13方向性错误修复\n")
    p(f"> 前日: {prev_date}\n")

    # ============================================================
    # STEP 0: 量能 + 白织带 + 大票
    # ============================================================
    p("## 一、大势与量能\n")

    # 量能4天
    recent_vols = []
    for vd in all_dates[-5:]:
        amt_v = conn.execute("SELECT SUM(amount) FROM daily_kline WHERE date=? AND volume>0", [vd]).fetchone()[0] or 0
        recent_vols.append((vd, amt_v/1e12))
    today_amt_tri = recent_vols[-1][1] if recent_vols else 0
    prev_amt_tri = recent_vols[-2][1] if len(recent_vols) >= 2 else 0

    vol_4d = [v[1] for v in recent_vols[-4:]]
    if len(vol_4d) >= 2:
        if all(vol_4d[i] > vol_4d[i-1] for i in range(1,len(vol_4d))): vol_dir = "📈递增"
        elif all(vol_4d[i] < vol_4d[i-1] for i in range(1,len(vol_4d))): vol_dir = "📉递减"
        else: vol_dir = "➡️震荡"
    else: vol_dir = "—"

    rhythm_days = vol_to_rhythm(today_amt_tri)
    if today_amt_tri >= 3.5:
        vol_judge = f"🔥 极度充裕 → **{rhythm_days}天窗口**，注意盛极而衰"
    elif today_amt_tri >= 3.0:
        vol_judge = f"✅ 健康 → **{rhythm_days}天回调窗口**"
    elif today_amt_tri >= 2.8:
        vol_judge = f"⚠️ 临界线 → **{rhythm_days}天窗口**，谨慎"
    else:
        vol_judge = f"🚨 危险 → **全面防守**，只卖不买"

    # === NEW: 白织带/黑织带检测 ===
    p("### 🔍 四大指数白/黑织带\n")
    p("| 指数 | 涨跌幅 | 织带信号 |")
    p("|------|--------|----------|")
    weave_signals = []
    for code, name in INDEX_CODES.items():
        row = conn.execute(
            "SELECT open, high, low, close, pct_change FROM daily_kline WHERE code=? AND date=?",
            [code, DATE]
        ).fetchone()
        if not row or None in row: continue
        o, h, l, c, pct_c = row
        signal = ""
        # v8.7: 指数数据格式异常（腾讯API未复权），用pct展示，织带检测沿用OHLC相对关系
        # v9.2: 用百分比容差替代绝对值0.02——上证指数O-L差0.03也会被漏判
        if o and l and (abs(o - l) / o) < 0.0015:
            signal = "⚪ **白织带**（开=最低→强势上攻）"
            weave_signals.append((name, "白"))
        elif o and h and (abs(o - h) / o) < 0.0015:
            signal = "⚫ **黑织带**（开=最高→弱势下跌）"
            weave_signals.append((name, "黑"))
        else:
            signal = "—"
        pct_str = f"{pct_c:+.1f}%" if pct_c else "—"
        p(f"| {name}({code}) | {pct_str} | {signal} |")
    p("")

    if not weave_signals:
        p("**织带结论**：今日无指数级织带信号，正常交易\n")
    else:
        white = [n for n, t in weave_signals if t == "白"]
        black = [n for n, t in weave_signals if t == "黑"]
        if white:
            p(f"**白织带**：{'、'.join(white)} → 开盘价=最低价，资金抢筹，**短期看涨信号**\n")
        if black:
            p(f"**黑织带**：{'、'.join(black)} → 开盘价=最高价，资金出逃，**短期看跌信号**\n")

    # 上涨/下跌
    all_rows = conn.execute(f"""
        SELECT code, pct_change, amount, close, high, low, volume
        FROM daily_kline WHERE date='{DATE}' AND volume>0 AND close>0
    """).fetchall()
    total = len(all_rows)
    up = sum(1 for r in all_rows if (r[1] or 0) > 0)
    down = total - up
    prev_total = conn.execute(f"SELECT COUNT(*) FROM daily_kline WHERE date='{prev_date}' AND volume>0").fetchone()[0]
    prev_up = conn.execute(f"SELECT COUNT(*) FROM daily_kline WHERE date='{prev_date}' AND volume>0 AND pct_change>0").fetchone()[0]

    # 涨停/跌停
    zt_list, dt_list = [], []
    zt_codes_set = set()
    for r in all_rows:
        code, pct = r[0], r[1] or 0
        _, zt_th = board_threshold(code)
        name = names.get(code, code)
        if is_noise(name): continue
        if pct >= zt_th:
            zt_list.append(r)
            zt_codes_set.add(code)
        elif pct <= -zt_th:
            dt_list.append(r)
    zt_count = len(zt_list)
    dt_count = len(dt_list)

    if zt_count > 100: mood = "高潮 🔥"
    elif zt_count >= 60: mood = "上升期 📈"
    elif zt_count >= 30: mood = "混沌/修复 🔄"
    elif zt_count >= 20: mood = "分歧 ⚡"
    else: mood = "冰点 ❄️"

    if up > prev_up * 1.3: repair = "强修复 ✅"
    elif up > prev_up: repair = "弱修复 ⚠️"
    elif up < prev_up * 0.7: repair = "分歧加剧 ❌"
    else: repair = "延续 ➡️"

    p("### 📊 市场数据\n")
    p(f"| 指标 | 数值 | 判断 |")
    p(f"|------|------|------|")
    p(f"| 上涨/下跌 | {up}/{down} (昨 {prev_up}/{prev_total-prev_up}) | {repair} |")
    p(f"| 涨停/跌停 | {zt_count}/{dt_count} | {mood} |")
    p(f"| 量能 | {today_amt_tri:.2f}万亿 (昨 {prev_amt_tri:.2f}) | {vol_judge} |")
    p(f"| 量能趋势 | {' → '.join(f'{v:.2f}' for v in vol_4d)}万亿 | {vol_dir} |")
    p(f"| 调整窗口 | **{rhythm_days}天** | — |")
    p("")

    # ============================================================
    # NEW v8.4 P0-1: 情绪周期判断 — 多维信号综合
    # ============================================================
    p("## 一·五 🎭 情绪周期\n")

    # 快速计算今日连板高度（用于情绪信号，正式连板在第五章）
    back_dates = [d for d in all_dates if d < DATE]
    quick_streaks = {}
    for r in zt_list:
        code = r[0]
        streak = 1
        for bd in reversed(back_dates):
            row = conn.execute(
                "SELECT pct_change FROM daily_kline WHERE code=? AND date=? AND volume>0",
                [code, bd]
            ).fetchone()
            if row and row[0] is not None:
                _, zt_th = board_threshold(code)
                if row[0] >= zt_th: streak += 1
                else: break
            else: break
        quick_streaks[code] = streak
    quick_max_streak = max(quick_streaks.values()) if quick_streaks else 1

    # 大票+黄牌快速取（供情绪周期使用）
    codes_big_fast = list(BIG_CAPS.keys())
    ph_bf = ",".join("?" * len(codes_big_fast))
    big_fast = conn.execute(f"""
        SELECT d.code, d.pct_change, d.high, d.low,
          p.high as ph, p.low as pl,
          p.pct_change as ppct
        FROM daily_kline d
        LEFT JOIN daily_kline p ON d.code=p.code AND p.date='{prev_date}'
        WHERE d.date='{DATE}' AND d.volume>0 AND d.code IN ({ph_bf})
    """, codes_big_fast).fetchall()
    pos_fast = sum(1 for r in big_fast if (r[1] or 0) > 0)
    neg_fast = len(big_fast) - pos_fast
    big_pos_ratio_fast = pos_fast / len(big_fast) if big_fast else 0.5
    yellow_big_names_fast = []
    for r in big_fast:
        code, pct, high, low, ph, pl = r[0], r[1] or 0, r[2], r[3], r[4], r[5]
        if ph and pl and high is not None and low is not None:
            if high <= ph and low < pl:
                yellow_big_names_fast.append(BIG_CAPS.get(code, code))

    # 多维度信号汇总
    up_ratio = up / total if total > 0 else 0.5
    prev_up_ratio = prev_up / prev_total if prev_total > 0 else 0.5
    up_change = up_ratio - prev_up_ratio

    # === v8.9 NEW: 大票vs指数横向对比（许文杰第一性原理核心） ===
    # 许文杰：看似挺弱（指数大跌）其实挺强（大票温和）→不是退潮，是正常调整
    # 计算四大指数平均涨跌幅
    idx_avg_pct = 0
    idx_count = 0
    idx_details = []
    for code, name in INDEX_CODES.items():
        row = conn.execute(
            "SELECT pct_change FROM daily_kline WHERE code=? AND date=?",
            [code, DATE]
        ).fetchone()
        if row and row[0] is not None:
            idx_avg_pct += row[0]
            idx_count += 1
            idx_details.append(f"{name}{row[0]:+.1f}%")
    if idx_count > 0:
        idx_avg_pct /= idx_count

# === v9.1 P0-1: 大票三层分层反馈（许文杰原教旨） ===
    # 许文杰：先看核心大票定性(易中天/澜起/寒武纪)，再看观察层，最后看总量
    # 核心层 = 持仓一线品种 → 直接决定大势判断
    # 观察层 = 其他大票 → 辅助信号
    # 总量层 = 统计值 → 仅供参考，不可替代核心定性
    core_big_codes = {
        "光通信": ["300308", "300502", "300394"],  # 易中天
        "存储": ["688008", "301666"],               # 澜起/大普
        "算力芯片": ["688256", "688041"],            # 寒武纪/海光
    }
    all_core_codes = [c for group in core_big_codes.values() for c in group]
    
    # 核心层逐股分析
    core_analysis = {}  # {code: {"name","pct","vs_idx","key_obs"}}
    core_pos_count = core_neg_count = 0
    core_new_high_count = 0
    for bf in big_fast:
        code, pct = bf[0], bf[1] or 0
        if code in all_core_codes:
            name = BIG_CAPS.get(code, code)
            vs_idx = round(pct - idx_avg_pct, 1) if idx_count > 0 else 0
            obs_parts = []
            if pct > 0:
                core_pos_count += 1
                if vs_idx > 0: obs_parts.append(f"跑赢指数{vs_idx:+.1f}%")
            else:
                core_neg_count += 1
                if vs_idx < 0: obs_parts.append(f"弱于指数{vs_idx:+.1f}%")
            if pct > -3: obs_parts.append("温和")
            else: obs_parts.append("偏弱")
            if bf[4] and bf[2] and bf[2] > bf[4]:
                obs_parts.append("新高")
                core_new_high_count += 1
            core_analysis[code] = {"name": name, "pct": pct, "vs_idx": vs_idx, "key_obs": ", ".join(obs_parts)}
    
    core_big_total = len(core_analysis)
    # 观察层：所有大票减去核心
    observe_big = [bf for bf in big_fast if bf[0] not in all_core_codes]
    observe_pos = sum(1 for bf in observe_big if (bf[1] or 0) > 0)
    observe_neg = len(observe_big) - observe_pos
    
    # 核心定性判断
    if core_big_total >= 5:
        core_strong_ratio = core_pos_count / core_big_total
        # 温和 = 跌<3%也OK（许文杰："澜起跌-2.4%也一样，很体面"）
        core_mild_count = sum(1 for bf in big_fast if bf[0] in all_core_codes and (bf[1] or 0) > -3)
        core_mild_ratio = core_mild_count / core_big_total
    else:
        core_strong_ratio = 0.5
        core_mild_ratio = 0.5
        core_mild_count = core_big_total
    
    # 大票vs指数对比判断
    # v9.1 FIX: 不只依赖均值，还要看单个指数最差情况
    idx_worst = 0
    idx_pcts = []
    for code in INDEX_CODES:
        row = conn.execute(
            "SELECT pct_change FROM daily_kline WHERE code=? AND date=?", [code, DATE]
        ).fetchone()
        if row and row[0] is not None:
            idx_pcts.append(row[0])
    if idx_pcts:
        idx_worst = min(idx_pcts)
    
    idx_drop_hard = (idx_avg_pct < -1.0 or idx_worst < -1.5) and idx_count >= 2
    # v9.1: core_mild 改为 >= 0.6 温和比例（而非只看涨跌）
    core_mild = core_big_total >= 3 and core_mild_ratio >= 0.6
    big_vs_idx = ""
    if idx_drop_hard and core_mild:
        # 核心详细列出
        core_detail = "; ".join([f"{v['name']}{v['pct']:+.1f}%({v['key_obs']})" for v in core_analysis.values()])
        big_vs_idx = f"🔑 **大票vs指数背离（看似弱实则强）**：指数{idx_avg_pct:+.1f}%，但核心大票体面→{core_detail}<br>"
        big_vs_idx += f"许文杰逻辑：指数阴包阳但核心容量强劲→**趋势没坏，防御≠清仓**"
    elif idx_drop_hard and not core_mild:
        core_detail = "; ".join([f"{v['name']}{v['pct']:+.1f}%" for v in core_analysis.values()])
        big_vs_idx = f"⚠️ **大票+指数共振下跌**：指数{idx_avg_pct:+.1f}%+核心大票同时走弱({core_detail})→**真退潮信号**"
    elif core_mild_ratio >= 0.6:
        big_vs_idx = f"🟢 指数{idx_avg_pct:+.1f}%，核心大票{core_pos_count}正{core_neg_count}负→**趋势正常**"
    else:
        big_vs_idx = f"指数{idx_avg_pct:.1f}%，核心大票{core_pos_count}正{core_neg_count}负，总量{pos_fast}正{neg_fast}负→分化中"

    # === v9.7.2 P0: 止涨信号检测（第一性原理——许文杰"涨不动了"判断） ===
    # 许文杰在5/13明确说"涨不动了，这确实是个事实"
    # v9.4只看正向信号（涨停+大票正反馈），系统性忽略负向累积信号
    # v9.7.2修复：在周期判断前强制检测止涨信号，≥2条触发则降级
    stop_signals = []
    stop_strength = 0  # 0=无信号, 1=微弱, 2+=确认止涨

    # 信号1：指数连续2天不过前高（许文杰"上证指数冲不动"）
    idx_prev = conn.execute(
        "SELECT AVG(pct_change) FROM daily_kline WHERE code IN ('sh000001','sh000688') AND date=?",
        [prev_date]).fetchone()[0]
    if idx_prev is not None and idx_count >= 2:
        if idx_avg_pct < -0.3 and (idx_prev is not None and idx_prev < -0.3):
            stop_signals.append(f"指数连续2天走弱({idx_prev:+.1f}→{idx_avg_pct:+.1f}%)，冲不动")
            stop_strength += 1

    # 信号2：核心大票"涨幅递减"——许文杰最核心的止涨判断依据
    # 5/13案例：中际旭创 +8.3%→+3.1%，寒武纪 +6.5%→+3.0%
    momentum_decay = []
    for bf in big_fast:
        code, pct, ppct = bf[0], bf[1] or 0, bf[6] if len(bf) > 6 else None  # ppct at idx 6
        if code in all_core_codes and ppct is not None:
            if pct >= 0 and ppct > 0 and pct < ppct * 0.6 and pct < 5:
                name = BIG_CAPS.get(code, code)
                momentum_decay.append(f"{name}{ppct:+.1f}→{pct:+.1f}")
    if len(momentum_decay) >= 2:
        stop_signals.append(f"核心大票力度衰减：{', '.join(momentum_decay[:4])}")
        stop_strength += 2  # 许文杰最核心依据，权重加倍

    # 信号3：连板高度不创新高（情绪面止涨）
    prev_max_streak = 1
    for r in conn.execute("""
        SELECT MAX(streak) FROM (
            SELECT COUNT(*) as streak FROM daily_kline d1
            JOIN daily_kline d2 ON d1.code=d2.code AND d2.date=d1.date
            WHERE d1.date=? AND d1.pct_change>=? AND d1.volume>0
        )
    """, [prev_date, 0]):  # simplified
        if r[0]: prev_max_streak = r[0]
    if quick_max_streak <= prev_max_streak and quick_max_streak <= 4:
        stop_signals.append(f"连板高度不创新高({quick_max_streak}板≤前日{prev_max_streak}板)")
        stop_strength += 1

    # 信号4：量能连续2天递减>5%/天
    if len(vol_4d) >= 3 and vol_4d[-1] < vol_4d[-2] * 0.95 and vol_4d[-2] < vol_4d[-3] * 0.95:
        stop_signals.append(f"量能连续递减({vol_4d[-2]:.2f}→{vol_4d[-1]:.2f}万亿)")
        stop_strength += 1

    # === v9.6 P0: 黄牌蔓延率 —— 用已加载大票数据内联计算 ===
    # 许文杰5/8核心逻辑：PCB 4只辨识度3只黄牌=75%→强区转二三，行情降级为强势震荡
    # 逻辑：不是涨停多就是上升期，板块内部在分化→高位震荡
    # 实现：用big_fast(大票含prev high/low)按扇区统计黄牌比例
    # 扇区映射：code→sector (BIG_CAP_SECTOR + 概念匹配)
    def _get_sector_for_code(code):
        if code in BIG_CAP_SECTOR:
            return BIG_CAP_SECTOR[code]
        # 概念匹配兜底
        concept_rows = conn.execute(
            "SELECT concept_name FROM si.stock_concepts WHERE code=? AND concept_name NOT IN (" +
            skip_ph + ")", [code] + skip_list
        ).fetchall()
        for (cn,) in concept_rows:
            if cn in SKIP_CONCEPTS: continue
            for sector, kws in TREND_SECTORS.items():
                for kw in kws:
                    if kw in cn:
                        return sector
        return None
    
    # 按扇区统计大票黄牌
    sector_yellow = {}  # sector -> [yellow_count, total_count]
    for bf in big_fast:
        code, pct, high, low = bf[0], bf[1] or 0, bf[2], bf[3]
        ph, pl = bf[4], bf[5]  # prev_high, prev_low
        sector = _get_sector_for_code(code)
        if not sector: continue
        if sector not in sector_yellow:
            sector_yellow[sector] = [0, 0]
        sector_yellow[sector][1] += 1
        # 黄牌判定：high<=prev_high AND low<prev_low
        if (ph is not None and pl is not None and high is not None and low is not None
            and high <= ph and low < pl):
            sector_yellow[sector][0] += 1
    
    yellow_spread_signals = []
    for sector, (yl, tot) in sector_yellow.items():
        # v9.7: 黄牌蔓延最小样本≥3，避免小样本误触发（5/11 AI-算力1/2=50%→不降级）
        # 许文杰：一两个大票出黄牌是正常轮动，不影响全局上升判断
        if tot >= 3 and yl / tot >= 0.5:
            yellow_spread_signals.append(f"{sector}{yl}/{tot}黄牌")
        elif tot == 2 and yl == 2:  # 小板块但全部黄牌(2/2=100%)仍触发
            yellow_spread_signals.append(f"{sector}{yl}/{tot}黄牌")
    
    if len(yellow_spread_signals) >= 1:
        spread_detail = ", ".join(yellow_spread_signals[:3])
        # ≥2个板块蔓延→权重加倍（系统性风险）
        spread_weight = 2 if len(yellow_spread_signals) >= 2 else 1
        stop_signals.append(f"板块黄牌蔓延: {spread_detail}")
        stop_strength += spread_weight

    # === v9.6 P1: 高低切信号 —— 低位新分支补涨+高位旧分支滞涨 ===
    # 许文杰"补涨逻辑"：高位涨不动→资金去低位找新机会→这不是全面退潮而是切换
    # 简化：黄牌板块有蔓延 + 其他大票扇区健康 = 换挡非熄火
    if len(yellow_spread_signals) >= 1 and len(sector_yellow) >= 3:
        clean_sectors = [s for s, (yl, tot) in sector_yellow.items()
                         if tot >= 2 and (yl == 0 or yl / tot < 0.3)]
        if len(clean_sectors) >= 2:
            stop_signals.append(f"高低切信号: 黄牌板块{'/'.join(yellow_spread_signals[:2])} + 健康板块{','.join(clean_sectors[:2])}→换挡非熄火")
            stop_strength += 1  # 温和信号——说明市场有活水

    # v9.7: A杀风险评估 —— 涨停高潮+核心力度衰减+指数走弱
    # 许文杰5/13：涨停108家但"A杀即视感+涨不动了"→高潮中暗藏风险
    is_a_kill = False
    if zt_count >= 100 and any("力度衰减" in sig for sig in stop_signals):
        # A杀三要素：涨停高潮 + 核心力度衰减 + 市场广度弱
        if idx_avg_pct <= 0 or up_ratio < 0.45:
            is_a_kill = True
            a_kill_details = f"涨停{zt_count}家高潮+核心力度衰减+{'指数走弱' if idx_avg_pct <= 0 else '涨跌比偏弱'}"
            stop_signals.insert(0, f"⚠️ A杀风险: {a_kill_details}→涨得猛回得快，防一防")  # 插到最前，确保不被[:3]截断
            stop_strength += 2  # A杀风险与力度衰减同级，权重加倍

    is_stop_rising = stop_strength >= 2
    if is_stop_rising:
        stop_reason = "；".join(stop_signals[:3])
        _log_stop = f"[v9.7.2 止涨检测] stop_strength={stop_strength}: {stop_reason}"
    zt_signal = ("🧊冰点" if zt_count < 25 else ("⚡分歧" if zt_count < 50 else
                 ("🔄修复/上升" if zt_count < 80 else ("📈上升" if zt_count < 100 else "🔥高潮"))))

    if zt_count < 25 and up_ratio < 0.35:
        cycle, cycle_logic = "🧊 冰点", "涨停极少+涨跌比极低→市场极度悲观，等待转折信号"
    elif zt_count < 25 and up_change > 0.15:
        cycle, cycle_logic = "🔄 冰点修复", "涨停仍少但涨跌比明显回升→情绪回暖初期，轻仓试探"
    elif zt_count < 50 and up_change > 0.12:
        cycle, cycle_logic = "🔄 分歧修复", f"涨停{zt_count}家+涨跌比回升→多头试探，关注量能配合"
    elif zt_count < 50 and up_change < -0.08:
        cycle, cycle_logic = "⚡ 分歧加剧", "涨停不增+涨跌比恶化→多空拉锯，控制仓位等待"
    elif zt_count >= 100 and quick_max_streak >= 3:
        # v9.7.2 P0 FIX: 先检测止涨信号，再判断行情级别
        # 5/13案例：涨停108+大票16正2负→v9.4判"强势加速"，但许文杰明确说"涨不动了"
        # 根因：v9.4只看正向信号，v9.7.2强制检测负向累积信号
        if is_stop_rising:
            # v9.6 P2: 黄牌蔓延驱动的止涨→板块分化，不是全局止涨
            # 许文杰5/11：上升期但光模块不能碰 → 全局仍是上升，光通信板块分化
            momentum_decay = any(sig in stop_reason for sig in ["力度衰减", "指数走弱", "涨停萎缩", "冰点", "A杀"])
            if momentum_decay or not yellow_spread_signals:
                cycle, cycle_logic = "🟡 止涨震荡", f"涨停{zt_count}家+量能{today_amt_tri:.1f}万亿+大票偏强，但{stop_reason}→**涨不动，收着点防一防**"
            else:
                cycle, cycle_logic = "📊 强势震荡", f"涨停{zt_count}家+量能{today_amt_tri:.1f}万亿+大票偏强，但{stop_reason}→**板块分化，换挡非熄火**"
        elif today_amt_tri >= 3.0 and big_pos_ratio_fast >= 0.7:
            cycle, cycle_logic = "🔥 强势加速", f"涨停{zt_count}家+量能{today_amt_tri:.1f}万亿+大票强→行情确认，维持进攻"
        elif len(vol_4d) >= 3 and all(vol_4d[i] > vol_4d[i-1] for i in range(1, min(len(vol_4d),4))):
            cycle, cycle_logic = "🔥 高潮（盛极）", "涨停破百+量能递增+高连板→**警惕盛极而衰**，最后一段不追"
        else:
            cycle, cycle_logic = "🔥 高潮", "涨停破百→情绪亢奋，买点已过，等分歧回调"
    elif zt_count >= 60 and big_pos_ratio_fast >= 0.6 and quick_max_streak >= 3:
        # v9.7.2 P0 FIX: 上升期也要检测止涨信号
        # v9.6: 即使止涨不足阈值，黄牌蔓延也降级
        if is_stop_rising:
            # v9.6 P2: 黄牌蔓延驱动的止涨→板块分化，不是全局止涨
            momentum_decay = any(sig in stop_reason for sig in ["力度衰减", "指数走弱", "涨停萎缩", "冰点", "A杀"])
            if momentum_decay or not yellow_spread_signals:
                cycle, cycle_logic = "🟡 止涨震荡", f"涨停{zt_count}家+大票偏强但{stop_reason}→**高位震荡，防一防**"
            else:
                cycle, cycle_logic = "📊 强势震荡", f"涨停{zt_count}家+大票正向，但{stop_reason}→**板块分化，收着点**"
        elif yellow_spread_signals:
            spread_str = ", ".join(yellow_spread_signals[:2])
            cycle, cycle_logic = "📊 强势震荡", f"涨停{zt_count}家+大票正向+高连板但{spread_str}→板块分化，收着点"
        else:
            cycle, cycle_logic = "📈 上升期", "涨停活跃+大票正向+高连板→趋势确认，积极低吸"
    elif zt_count >= 40 and big_pos_ratio_fast >= 0.5:
        # v9.7.2: 5/12回归发现 — zt=57+量能够但缩量→许文杰叫"震荡偏强"不是"上升初期"
        # v9.6 P0: 黄牌蔓延→板块内部分化，即使涨停多也降为强势震荡
        # 许文杰5/8：PCB 75%黄牌→强区转二三，"上升初期"→"强势震荡"
        if yellow_spread_signals:
            spread_str = ", ".join(yellow_spread_signals[:2])
            cycle, cycle_logic = "📊 强势震荡", f"涨停{zt_count}家+大票偏强但{spread_str}→板块内部分化，良性震荡"
        elif zt_count < 60 and len(vol_4d) >= 2 and vol_4d[-1] < vol_4d[-2] * 0.95:
            cycle, cycle_logic = "📊 强势震荡", f"涨停{zt_count}家+大票偏强但量能小幅萎缩→良性震荡，量足够就不A杀"
        else:
            cycle, cycle_logic = "📈 上升初期", f"涨停{zt_count}家+大票偏强→趋势启动，可试探性建仓"
    elif big_pos_ratio_fast < 0.4 and len(yellow_big_names_fast) >= 3:
        # v9.1 P0-2: 核心反馈优先——核心温和则不是退潮，是防御状态
        if core_mild and today_amt_tri >= 2.8:
            cycle, cycle_logic = "🛡️ 防御状态", f"总量退潮信号({pos_fast}正{neg_fast}负+{len(yellow_big_names_fast)}黄牌)，但核心大票温和→**防御≠清仓**，不开新仓+做T+了结"
        else:
            cycle, cycle_logic = "📉 退潮", "大票偏弱+黄牌扩散→主力撤退信号，全面防守"
    elif up_change < -0.15:
        # v9.1 FIX: 大票vs指数横向对比——指数大跌+大票温和+量能充足≠退潮
        # 许文杰原话：不A杀、正常调整、有量跌不深
        # v9.1 P0-3: 即使指数跌得不够深，但涨跌比极差+核心温和+有量=防御非退潮
        market_brutal = up_ratio < 0.25  # 涨跌比<25% = 1600/5000级别
        if (idx_drop_hard or market_brutal) and core_mild and today_amt_tri >= 2.8:
            if idx_drop_hard:
                key_reason = f"指数{idx_avg_pct:+.1f}%"
            else:
                key_reason = f"涨跌比极差({up_ratio:.1%})+黑织带"
            cycle, cycle_logic = "🔄 类冰点/正常调整", f"涨跌比恶化({up_ratio:.1%})但核心大票温和→**不A杀，正常调整**。量能{today_amt_tri:.1f}万亿支撑横盘震荡，涨不动但跌不深"
        else:
            cycle, cycle_logic = "📉 退潮确认", "涨跌比大幅恶化→情绪转向，只卖不买"
    elif zt_count < 30 and big_pos_ratio_fast < 0.5:
        # v9.1 P0-2: 核心温和+有量则不判准冰点
        if core_mild and today_amt_tri >= 2.8:
            cycle, cycle_logic = "🛡️ 防御状态", f"涨停{zt_count}家+总量弱，但核心大票温和+量能{today_amt_tri:.1f}万亿→**防御性持仓**"
        else:
            cycle, cycle_logic = "🧊 准冰点", f"涨停{zt_count}家+大票偏弱→接近冰点，等待恐慌释放"
    else:
        cycle, cycle_logic = "➡️ 混沌", "各信号矛盾→等待明朗，小仓位试探"

    p(f"| 维度 | 数值 | 信号 |")
    p(f"|------|------|------|")
    p(f"| 涨停家数 | {zt_count}家 | {zt_signal} |")
    p(f"| 上涨占比 | {up_ratio:.1%} (昨 {prev_up_ratio:.1%}) | {'↑ 回升' if up_change > 0.05 else '↓ 恶化' if up_change < -0.05 else '→ 持平'} |")
    p(f"| 量能 | {today_amt_tri:.2f}万亿 | {vol_dir} |")
    p(f"| 大票·全量 | {pos_fast}正{neg_fast}负 | {'偏强' if big_fast and pos_fast/len(big_fast) >= 0.6 else '偏弱' if big_fast and pos_fast/len(big_fast) <= 0.4 else '中性'} |")
    p(f"| ├ 核心层 | {core_pos_count}正{core_neg_count}负（{core_mild_count}/{core_big_total}温和） | {'🟢 体面' if core_mild_ratio >= 0.6 else '🟡 分化' if core_mild_ratio >= 0.4 else '🔴 偏弱'} |")
    p(f"| └ 观察层 | {observe_pos}正{observe_neg}负 | {'偏强' if observe_big and observe_pos/len(observe_big) >= 0.6 else '偏弱' if observe_big and observe_pos/len(observe_big) <= 0.4 else '中性'} |")
    p(f"| 连板高度 | {quick_max_streak}板 | — |")
    p("")
    p(f"**周期判断**：**{cycle}** → {cycle_logic}")
    p("")
    # v8.9: 大票vs指数横向对比输出
    if big_vs_idx:
        p(big_vs_idx)
        p("")

    # 周期→操作指引
    p(f"| 周期 | 操作建议 |")
    p(f"|------|----------|")
    if "类冰点" in cycle or "正常调整" in cycle:
        # v9.7: 类冰点→防一下，收着点。许文杰5/14原话"防一下+收着点(不开新仓)"
        # 类冰点≠退潮所以不空仓，但不开新仓是许文杰底线
        p(f"| {cycle} | ⚠️ **防一下，收着点**：核心大票温和→不A杀、不空仓，但**不开新仓**，只做已持品种的做T+了结。等二分歧后再找机会 |")
    elif "止涨" in cycle:
        # v9.7.2 P0: 止涨震荡 → 收着点防一防
        p(f"| {cycle} | 🟡 **收着点，防一防**：不开新仓，做T+了结手中品种。许文杰逻辑→涨不动是事实，等二次分歧后的买点 |")
    elif "强势震荡" in cycle:
        # v9.7.2: 量能够但缩量→良性震荡，非下跌
        p(f"| {cycle} | 📊 **良性震荡**：量足够→不A杀，可以做T+做轮动，维持5-6成 |")
    elif "冰点" in cycle:
        p(f"| {cycle} | 🛑 **防守**：不追高位，减仓为主，等冰点后的放量修复信号 |")
    elif "退潮" in cycle:
        p(f"| {cycle} | 🛑 **退潮+防守**：只卖不买，全面收缩仓位，等退潮确认结束 |")
    elif "防御状态" in cycle:
        p(f"| {cycle} | 🛡️ **防御≠清仓**：不开新仓，做T+了结手中品种，保持5-7成仓位。核心大票温和→趋势没坏 |")
    elif "修复" in cycle or "上升初期" in cycle:
        p(f"| {cycle} | ⚠️ **试探**：小仓位低吸（3-5成），严格止损，等确认加仓 |")
    elif "上升" in cycle:
        p(f"| {cycle} | ✅ **进攻**：积极低吸调满窗口板块，可加至6-8成 |")
    elif "强势加速" in cycle:
        p(f"| {cycle} | 🔥 **强势行情**：趋势确认，维持进攻，低吸轮动板块 |")
    elif "高潮" in cycle:
        p(f"| {cycle} | 🔥 **兑现**：不追高位，已有仓位逐步止盈，等分歧 |")
    else:
        p(f"| {cycle} | ➡️ **观望**：小仓位试探，等信号明朗 |")
    p("")

    # ============================================================
    # STEP 0.5: 板块去重—1票1板块
    # ============================================================
    zt_codes = list(zt_codes_set)
    zt_names_map = {}
    for code in zt_codes:
        zt_names_map[code] = names.get(code, code)

    # 每只涨停票→唯一板块归属
    zt_by_sector = defaultdict(list)  # sector → [(code, name, pct, concept)]
    sector_concept_map = defaultdict(set)  # sector → 涉及概念

    for r in zt_list:
        code, pct = r[0], r[1] or 0
        name = zt_names_map.get(code, code)

        concept_rows = conn.execute(
            "SELECT concept_name FROM si.stock_concepts WHERE code=? AND concept_name NOT IN (" +
            skip_ph + ")", [code] + skip_list
        ).fetchall()

        sector, concept = assign_sector_single(code, concept_rows)
        if sector:
            zt_by_sector[sector].append((code, name, pct))
            if concept:
                sector_concept_map[sector].add(concept)

    # 统计字符（只取有票的板块）
    active_sectors = {s: v for s, v in zt_by_sector.items() if v}

    p(f"**板块涨停分布（1票1板块）**：")
    for sector, zts in sorted(active_sectors.items(), key=lambda x: -len(x[1])):
        nds = ", ".join(n for _, n, _ in zts[:5])
        more = f" +{len(zts)-5}只" if len(zts) > 5 else ""
        p(f"- **{sector}** ({len(zts)}只): {nds}{more}")
    p("")

    # v8.9: 前10涨幅→龙一提取（许文杰核心动作：每个板块必须说出龙一是谁+多少点）
    # 构建概念→板块快速查表（概念关键字 + 行业兜底）
    concept_to_sector = {}
    for sector, kws in TREND_SECTORS.items():
        for kw in kws:
            concept_to_sector[kw] = sector
    
    # 行业→板块映射（INDUSTRY_TO_SECTOR 的 key 在 stock_concepts 中作为 concept_name 存在）
    industry_to_sector = {}
    for ind, sectors in INDUSTRY_TO_SECTOR.items():
        if sectors:
            industry_to_sector[ind] = sectors[0]  # 第1优先板块
    
    # 收集每只股票→板块归属
    sector_top10 = defaultdict(list)  # sector → [(name, pct), ...]
    sector_leader = {}  # sector → (name, pct)
    
    # 批量获取股票数据：概念关键字 + 行业分类 全部查询
    all_lookup_names = list(concept_to_sector.keys()) + list(industry_to_sector.keys())
    all_lookup_ph = ",".join("?" * len(all_lookup_names))
    sector_rows = conn.execute(f"""
        SELECT DISTINCT sc.code, sc.concept_name, dk.pct_change
        FROM si.stock_concepts sc
        JOIN daily_kline dk ON sc.code = dk.code AND dk.date = '{DATE}'
        WHERE sc.concept_name IN ({all_lookup_ph})
        AND dk.pct_change IS NOT NULL
        AND dk.volume > 0
    """, all_lookup_names).fetchall()
    
    # 分组：大票直映射 → 概念关键字匹配 → 行业兜底
    sector_all_stocks = defaultdict(list)
    for code, cname, pct in sector_rows:
        if cname in SKIP_CONCEPTS:
            continue
        best_sector = None
        best_len = 0
        # 0) 大票直映射（绕过概念标签稀疏问题）
        if code in BIG_CAP_SECTOR:
            best_sector = BIG_CAP_SECTOR[code]
        else:
            # 1) 概念关键字匹配
            for kw, sec in concept_to_sector.items():
                if kw in cname and len(kw) > best_len:
                    best_sector = sec
                    best_len = len(kw)
            # 2) 行业兜底
            if not best_sector:
                best_sector = industry_to_sector.get(cname)
        if best_sector:
            name = names.get(code, code)
            sector_all_stocks[best_sector].append((name, pct or 0, code))
    
    # 每个板块去重（同股同板块多条概念→取最高涨幅），取前10
    for sector in TREND_SECTORS:
        stocks = sector_all_stocks.get(sector, [])
        if not stocks:
            continue
        dedup = {}
        for name, pct, code in stocks:
            key = code
            if key not in dedup or pct > dedup[key][1]:
                dedup[key] = (name, pct)
        stocks_dedup = [(n, p, c) for c, (n, p) in dedup.items()]
        stocks_dedup.sort(key=lambda x: -x[1])
        top10 = stocks_dedup[:10]
        sector_top10[sector] = [(n, p) for n, p, _ in top10]
        if top10:
            sector_leader[sector] = (top10[0][0], top10[0][1])
    
    p("**🔝 各板块前10涨幅**：")
    for sector in TREND_SECTORS:
        top10 = sector_top10.get(sector, [])
        if not top10:
            continue
        leader = sector_leader.get(sector, ("?", 0))
        items = " | ".join(f"{n} **{p:+.1f}%**" for n, p in top10[:5])
        more = f" +{len(top10)-5}只" if len(top10) > 5 else ""
        zt_c = len(zt_by_sector.get(sector, []))
        p(f"- **{sector}** 🏅龙一:**{leader[0]}**({leader[1]:+.1f}%) | 涨停{zt_c}只 | {items}{more}")
    p("")

    # ============================================================
    # STEP 1: 大票锚定 + 动态发现
    # ============================================================
    p("## 二、大票锚定反馈\n")

    # 1.1 固定大票
    codes_big = list(BIG_CAPS.keys())
    ph_big = ",".join("?"*len(codes_big))
    big_rows = conn.execute(f"""
        SELECT d.code, d.pct_change, d.close, d.high, d.low,
          p.close as pc, p.high as ph, p.low as pl
        FROM daily_kline d
        LEFT JOIN daily_kline p ON d.code=p.code AND p.date='{prev_date}'
        WHERE d.date='{DATE}' AND d.volume>0 AND d.code IN ({ph_big})
        ORDER BY d.pct_change DESC
    """, codes_big).fetchall()

    pos, neg, yellow_big = 0, 0, 0
    yellow_big_names = []
    for row in big_rows:
        code, pct, close, high, low, pc, ph, pl = row
        pct = pct or 0
        if ph and pl and (high <= ph) and (low < pl):
            yellow_big += 1
            yellow_big_names.append(BIG_CAPS.get(code, code))
        if pct > 0: pos += 1
        else: neg += 1

    p("| 大票 | 涨幅 | 价格 | 信号 |")
    p("|------|------|------|------|")
    for row in big_rows:
        code, pct, close, high, low, pc, ph, pl = row
        pct = pct or 0; close = close or 0
        signals = []
        if ph and high and high > ph: signals.append("🔝新高")
        if ph and pl and (high <= ph) and (low < pl): signals.append("🟡黄牌")
        sig = " ".join(signals) if signals else "—"
        p(f"| {BIG_CAPS.get(code,code)} | {pct:+.2f}% | {close:.1f} | {sig} |")

    if yellow_big_names:
        p(f"\n**大票黄牌** ({yellow_big}只): {', '.join(yellow_big_names)}")
    p(f"**固定大票**：{pos}正{neg}负{' ' + str(yellow_big) + '黄牌' if yellow_big else ''}\n")

    # v8.7: Build big cap status map for LLM sector data enrichment
    big_pct_map = {}
    for row in big_rows:
        code, pct, close, high, low, pc, ph, pl = row
        pct = pct or 0; close = close or 0
        name = BIG_CAPS.get(code, code)
        signals = []
        is_new_high = False
        if ph and high and high > ph:
            signals.append("新高")
            is_new_high = True
        if ph and pl and (high <= ph) and (low < pl):
            signals.append("黄牌")
        big_pct_map[name] = (pct, close, signals, is_new_high)

    # === NEW: 动态大票发现 ===
    # 从涨停票中找出成交额TOP的新面孔
    p("### 🆕 动态发现大票（从涨停票中）\n")

    # 涨停票按成交额排序，排除已在大票池的
    zt_amounts = [(r[0], r[2], r[1] or 0) for r in zt_list if r[0] not in BIG_CAPS]
    zt_amounts.sort(key=lambda x: -(x[1] or 0))

    new_big_candidates = []
    for code, amt, pct in zt_amounts[:8]:
        if amt and amt > 5e8:  # 成交额>5亿
            name = zt_names_map.get(code, code)
            new_big_candidates.append((code, name, amt, pct))

    if new_big_candidates:
        p("| 新增候选 | 成交额 | 涨幅 |")
        p("|----------|--------|------|")
        for code, name, amt, pct in new_big_candidates:
            p(f"| {name} | {amt/1e8:.1f}亿 | {pct:+.2f}% |")
        p(f"\n**动态大票**：{len(new_big_candidates)}只成交额>5亿涨停票 → 加入观察池\n")
    else:
        p("*无新增候选大票（今日涨停票成交额均<5亿）*\n")

    # 大票综合定性
    big_pos_ratio = pos / len(big_rows) if big_rows else 0.5
    p(f"**大票总定性**：固定{pos}正{neg}负，{'偏强' if big_pos_ratio >= 0.6 else '偏弱' if big_pos_ratio <= 0.4 else '中性'}\n")

    # ============================================================
    # NEW v8.3: 多日快照对比
    # ============================================================
    p("## 2.5 📸 多日快照对比\n")
    prev_replay_path = f"/root/yuanfang-brain/replays/replay_{prev_date}_v8.3.md"
    v82_prev_path = f"/root/yuanfang-brain/replays/replay_{prev_date}_v8.2.md"
    prev_report_path = prev_replay_path if os.path.exists(prev_replay_path) else (
        v82_prev_path if os.path.exists(v82_prev_path) else None
    )
    if prev_report_path:
        with open(prev_report_path) as f:
            prev_report = f.read()
        # 抽取前日关键指标
        import re as _re
        prev_vol_match = _re.search(r'量能\s*\|\s*([\d.]+)万亿', prev_report)
        prev_pos_ratio_match = _re.search(r'(\d+)正(\d+)负', prev_report)
        prev_yellow_match = _re.search(r'大票黄牌.*?(\d+)只', prev_report)

        now_vol = today_amt_tri
        now_pos_val = pos
        now_neg_val = neg
        now_yellow_val = len(yellow_big_names)

        changes = []
        if prev_vol_match:
            pv = float(prev_vol_match.group(1))
            diff = now_vol - pv
            arrow = "↑" if diff > 0.05 else "↓" if diff < -0.05 else "→"
            changes.append(f"量能 {pv:.2f}→{now_vol:.2f}万亿 ({arrow}{abs(diff):.2f})")

        if prev_pos_ratio_match:
            pp = int(prev_pos_ratio_match.group(1))
            pn = int(prev_pos_ratio_match.group(2))
            now_ratio = f"{now_pos_val}正{now_neg_val}负"
            prev_ratio = f"{pp}正{pn}负"
            changes.append(f"大票比 {prev_ratio}→{now_ratio}")

        if prev_yellow_match:
            py = int(prev_yellow_match.group(1))
            ny = now_yellow_val
            if ny != py:
                changes.append(f"黄牌 {py}→{ny}只 {'⚠️扩散' if ny > py else '✅收缩'}")
            else:
                changes.append(f"黄牌 {ny}只 →")

        p("| 指标 | 前日 | 今日 | 变化 |")
        p("|------|------|------|------|")
        for c in changes:
            parts = c.split(" ")
            if len(parts) >= 3:
                p(f"| {parts[0]} | ... | ... | {c} |")

        # 趋势方向判断
        if big_pos_ratio >= 0.6 and now_yellow_val <= 2:
            trend_macro = "✅ 大票结构健康，战略偏进攻"
        elif big_pos_ratio >= 0.5 and now_yellow_val <= 3:
            trend_macro = "➡️ 大票中性，战略等确认"
        else:
            trend_macro = "⚠️ 大票偏弱/黄牌多，战略偏防守"
        p("")
        p("**多日演化判断**：" + trend_macro)
        p("")
    else:
        p("*前日复盘(" + prev_replay_path + ")不存在→跳过多日对比*")
        p("")

    # ============================================================
    # STEP 2: 板块三步法
    # ============================================================
    p("## 三、板块分析（三步法）\n")

    # ② 数日子
    p(f"### ② 调节奏 → 找买点窗口\n")
    p(f"> 节奏周期 = **{rhythm_days}天**（量能 {today_amt_tri:.2f}万亿）\n")

    p("| 板块 | 今日涨停 | 调了几天 | 判断 |")
    p("|------|----------|----------|------|")

    sector_days_adjusting = {}
    sector_zt_data = {}  # 存板块详细信息给LLM

    for sector in TREND_SECTORS:
        zts = zt_by_sector.get(sector, [])
        zt_cnt = len(zts)

        # 调节奏：查最近一次≥3涨停的日期
        adj_days = None
        for lookback in range(1, min(len(all_dates), 15)):
            lb_date = all_dates[-1 - lookback]
            if lb_date == DATE: continue

            # 查该日期该板块有多少只票涨停
            lb_zts = conn.execute(f"""
                SELECT COUNT(*) FROM daily_kline d
                WHERE d.date='{lb_date}' AND d.volume>0 AND d.close>0
                AND d.code IN ({','.join('?'*len(zt_codes)) if zt_codes else "''"})
                AND d.pct_change >= CASE
                  WHEN d.code LIKE '300%' OR d.code LIKE '301%' OR d.code LIKE '688%' THEN 19.5
                  WHEN d.code LIKE '8%' OR d.code LIKE '4%' THEN 29.5
                  ELSE 9.8 END
            """, zt_codes).fetchone()

            # Better: 用概念匹配查
            sector_concepts = sector_concept_map.get(sector, set())
            if sector_concepts:
                cp_ph = ",".join("?"*len(sector_concepts))
                lb_zts2 = conn.execute(f"""
                    SELECT COUNT(DISTINCT d.code) FROM daily_kline d
                    JOIN si.stock_concepts sc ON d.code=sc.code
                    WHERE d.date='{lb_date}' AND d.volume>0 AND d.close>0
                    AND d.pct_change >= CASE
                      WHEN d.code LIKE '300%' OR d.code LIKE '301%' OR d.code LIKE '688%' THEN 19.5
                      WHEN d.code LIKE '8%' OR d.code LIKE '4%' THEN 29.5
                      ELSE 9.8 END
                    AND sc.concept_name IN ({cp_ph})
                """, list(sector_concepts)).fetchone()
                lb_cnt = lb_zts2[0] if lb_zts2 else 0
            else:
                lb_cnt = 0

            if lb_cnt >= 3:
                adj_days = lookback
                break

        # 判断
        if adj_days is not None:
            sector_days_adjusting[sector] = adj_days
            if adj_days >= rhythm_days:
                judgment = f"✅ **调{adj_days}天≥窗口，可低吸**"
            elif adj_days >= rhythm_days - 1:
                judgment = f"⏳ 调{adj_days}天→接近窗口"
            else:
                judgment = f"⏳ 调{adj_days}天→还早"
        else:
            if zt_cnt >= 3:
                judgment = "🔥 **第一天启动**→观察"
            else:
                judgment = "—"

        # 收集板块数据给LLM
        # v8.9 FIX: 许文杰原教旨——个股技术面(新高/滞涨/量价)优先于涨停数判强区
        # 零涨停板块(存储/PCB)检查个股技术面：新高=板块存在强势逻辑
        sector_stock_tech_strength = False
        sector_stock_notes = []
        for sn in SECTOR_STOCKS.get(sector, []):
            if sn in big_pct_map:
                spct, sclose, ssigs, snh = big_pct_map[sn]
                if snh:
                    sector_stock_tech_strength = True
                    sector_stock_notes.append(f"{sn}新高")
                if spct > 3 and "滞涨" not in str(ssigs):
                    sector_stock_notes.append(f"{sn}+{spct:+.1f}%强")
                elif spct < -5 and ssigs:
                    sector_stock_notes.append(f"{sn}{spct:+.1f}%滞涨⚠️")

        if zt_cnt >= 2 or adj_days or (sector_stock_tech_strength and zt_cnt == 0):
            # v8.9: 零涨停+个股新高→送LLM判强区/退潮 for real
            if zt_cnt == 0 and sector_stock_tech_strength:
                leaders_str = f"零涨停，但{sector_stock_notes[0] if sector_stock_notes else '个股技术面偏强'}"
            else:
                leaders_list = sorted(zts, key=lambda x: -x[2])[:3]
                leaders_str = ", ".join(f"{n}({pct:+.1f}%)" for _, n, pct in leaders_list)

            # 板块内大票状态
            sector_bigs = []
            for bc, bn in BIG_CAPS.items():
                srows = conn.execute(
                    "SELECT concept_name FROM si.stock_concepts WHERE code=? AND concept_name NOT IN (" +
                    skip_ph + ")", [bc] + skip_list
                ).fetchall()
                s_sector, _ = assign_sector_single(bc, srows)
                if s_sector == sector:
                    sector_bigs.append(bn)

            # v8.7: 大票状态包含涨幅+新高/黄牌标记
            big_details = []
            for bn in sector_bigs[:3]:
                if bn in big_pct_map:
                    pct_val, close_val, sigs, is_nh = big_pct_map[bn]
                    sig_str = ",".join(sigs) if sigs else "—"
                    big_details.append(f"{bn}+{pct_val:+.1f}%({sig_str})")
                else:
                    big_details.append(bn)
            big_status_str = "、".join(big_details) if big_details else "无极品大票"

            sector_zt_data[sector] = {
                "zt_cnt": zt_cnt,
                "adj_days": adj_days,
                "leaders": leaders_str,
                "big_status": big_status_str,
                "dragon_leader": sector_leader.get(sector, (None, 0))  # v8.9: (name, pct)
            }

        p(f"| **{sector}** | {zt_cnt}只 | {adj_days if adj_days is not None else '—'}天 | {judgment} |")

    p("")

    # === NEW: LLM定性增强 ===
    p("### 🤖 LLM板块定性（FinnA DeepSeek V4 Flash）\n")
    llm_result = llm_sector_qualitative(sector_zt_data)
    

    # v9.7.2: 高低切检测
    p("### 📊 高低切检测\n")
    p("| 板块 | 高位均涨幅 | 低位均涨幅 | 差值 | 高低切信号 |")
    p("|------|-----------|-----------|------|-----------|")
    rotation_sectors = []
    for sector in ["光通信大票", "AI-算力", "AI-芯片", "AI-存储", "机器人", "航天", "新能源", "电力", "地产/建材"]:
        codes = []
        for code, s in BIG_CAP_SECTOR.items():
            if s == sector:
                codes.append(code)
        if not codes and sector in SECTOR_STOCKS:
            codes = [names_rev.get(n) for n in SECTOR_STOCKS.get(sector, []) if names_rev.get(n)]
        if len(codes) >= 3:
            is_rot, hi_avg, lo_avg, detail = high_low_rotation_detect(conn, codes, DATE)
            if is_rot:
                rotation_sectors.append(sector)
            emoji = "🔴 高低切" if is_rot else ("🟢 正常" if hi_avg > 0 else "⚪ 无数据")
            p(f"| **{sector}** | {hi_avg}% | {lo_avg}% | {round(lo_avg-hi_avg,1):+}% | {emoji} |")
    if rotation_sectors:
        p(f"\n> ⚠️ **高低切信号**: {', '.join(rotation_sectors)} — 低位补涨强于高位，操作上只低吸不追高，等高低切结束后看高位能否走强")
    p("")
    # v9.3→v9.7.2 FIX: 条件约束操作指令——移到LLM+规则兜底+大票纠正全部完成之后调用
    # （原来在1452行过早调用，LLM失败时llm_result为空，导致强区板块被错误回避）

# === v8.8.3: 预处理大票涨停新高→强制强区（LLM非确定性兜底） ===
    # 第一性原理：板块内大票涨停+新高=强区确认 → LLM误判也要纠正
    # 但前提是板块调整不深（adj_days <= 2）——调深≥3天说明处于轮动状态，D1反弹不能立刻确认强区
    # 许文杰：调3天+大票D1涨停新高 → 仍判"轮动观察，需D2确认"，而非主升
    pre_main_sectors = set()
    for sector in TREND_SECTORS:
        # v8.8.3: 检查adj_days，调深≥3天跳过（D1反弹需D2确认）
        sector_info = sector_zt_data.get(sector, {})
        adj_d = sector_info.get("adj_days")
        if adj_d is not None and adj_d >= 3:
            continue  # 深调板块D1反弹不强制强区，留给LLM判轮动
        zt_names_set = {n for _, n, _ in zt_by_sector.get(sector, [])}
        for code, name in BIG_CAPS.items():
            if BIG_CAP_SECTOR.get(code) == sector and name in big_pct_map:
                pct_val, close_val, sigs, is_nh = big_pct_map[name]
                # 涨停 AND 新高 → 强区确认
                if is_nh and name in zt_names_set:
                    # 核实该大票确实是涨停（不是盘中涨幅）
                    for r in zt_list:
                        if names.get(r[0], r[0]) == name:
                            pre_main_sectors.add(sector)
                            break

    if pre_main_sectors and llm_result:
        for s in pre_main_sectors:
            if s not in llm_result:
                continue  # 板块未送LLM（zt_cnt<2且无adj）→跳过
            if "主升" not in llm_result.get(s, {}).get("分类", ""):
                old_cat = llm_result[s].get("分类", "?")
                llm_result[s]["分类"] = "主升强区"
                llm_result[s]["关键逻辑"] = f"大票涨停新高确认强区（LLM原判{old_cat}→v8.8.2强制纠正）"

    if llm_result:
        p("| 板块 | 分类 | 子赛道 | 明日建议 | 关键逻辑 |")
        p("|------|------|--------|----------|----------|")
        for sector in TREND_SECTORS:
            info = llm_result.get(sector, {})
            if info:
                cat = info.get("分类", "—")
                sub = info.get("子赛道", "—")
                advice = info.get("明日建议", "—")
                logic = info.get("关键逻辑", "—")
                # 分类用emoji
                ce = {"主升强区": "🟢", "主升(强区)": "🟢", "二三观察": "🟡", "轮动观察": "🟡", "退潮放弃": "🔴"}.get(cat, "")
                p(f"| **{sector}** | {ce}{cat} | {sub} | {advice} | {logic} |")
            else:
                # v8.8.2: 大票涨停新高板块→即使未送LLM也标强区
                if sector in pre_main_sectors:
                    p(f"| **{sector}** | 🟢主升(强区) | — | — | 大票涨停新高确认→强制标强区(v8.8.2) |")
                else:
                    zt_cnt = len(zt_by_sector.get(sector, []))
                    status = "—" if zt_cnt == 0 else "轮动观察"
                    p(f"| **{sector}** | {status} | — | — | — |")
    else:
        p("*LLM定性失败，使用规则判断*\n")
        # 规则兜底
        # v9.7.2 FIX: 同时填充llm_result，供conditional_constraints使用
        if not llm_result:
            llm_result = {}
        p("| 板块 | 定性 | 判断逻辑 |")
        p("|------|------|----------|")
        for sector in TREND_SECTORS:
            zt_cnt = len(zt_by_sector.get(sector, []))
            adj = sector_days_adjusting.get(sector)
            # v9.2: 检查大票新高（许文杰核心信号）
            nh_big_caps = []
            nh_pos_caps = []
            for code, name in BIG_CAPS.items():
                if BIG_CAP_SECTOR.get(code) == sector and name in big_pct_map:
                    pct_val, close_val, sigs, is_nh = big_pct_map[name]
                    if is_nh:
                        nh_big_caps.append(name)
                        if (pct_val or 0) > 0:
                            nh_pos_caps.append(f"{name}+{pct_val:.1f}%")
            # 许文杰P1：大票新高+正反馈 → 强区（不论涨停数）
            if nh_pos_caps:
                q = "🟢 强区"
                logic = f"大票{'/'.join(nh_pos_caps[:3])}新高确认"
                llm_result[sector] = {"分类": "主升强区", "关键逻辑": logic}
            elif zt_cnt >= 3 and adj and adj >= rhythm_days:
                q = "🟢 强区"
                logic = f"涨停{zt_cnt}只+调满{adj}天"
                llm_result[sector] = {"分类": "主升强区", "关键逻辑": logic}
            elif zt_cnt >= 1:
                q = "🟡 轮动"
                logic = f"涨停{zt_cnt}只边缘"
                llm_result[sector] = {"分类": "轮动观察", "关键逻辑": logic}
            else:
                q = "—"
                logic = "无涨停/无新高"
                llm_result[sector] = {"分类": "退潮放弃", "关键逻辑": logic}
            p(f"| **{sector}** | {q} | {logic} |")

    p("")

    # === NEW v8.3: 强区协同信号 ===
    p("### 🤝 强区协同信号\n")

    # 统计主升板块数
    # v9.4 P0-4: 光通信小票分离——GT在5/13已放弃光通信小票
    # 1. 先从LLM结果中分离光通信小票
    small_optic_cat = llm_result.get("光通信小票", {}).get("分类", "") if llm_result else ""
    small_optic_was_main = "主升" in small_optic_cat
    
    # 2. 构建主升板块列表（排除光通信小票）
    main_sectors = []
    if llm_result:
        main_sectors = [s for s, info in llm_result.items()
                       if "主升" in info.get("分类", "") and s != "光通信小票"]
    
    # 3. 如果光通信小票被LLM错误标为强区，自动修正并在输出中提示
    if small_optic_was_main and llm_result:
        llm_result["光通信小票"]["分类"] = "二三观察"
        llm_result["光通信小票"]["关键逻辑"] = f"GT 5/13已放弃光通信小票→v9.7.2自动降级为二三观察(原判{small_optic_cat})"
    
# v9.7.2 FIX: 条件约束操作指令——在所有LLM/规则/纠正完成之后调用
    # 确保conditional_constraints看到的是最终版llm_result
    p(conditional_constraints(snapshots, llm_result))
    
    # 规则兜底（main_sectors）
    if not main_sectors:
        for s in TREND_SECTORS:
            zt_cnt = len(zt_by_sector.get(s, []))
            adj = sector_days_adjusting.get(s)
            # v9.2: 大票新高也是强区（不依赖涨停数）
            sector_has_nh = False
            for code, name in BIG_CAPS.items():
                if BIG_CAP_SECTOR.get(code) == s and name in big_pct_map:
                    _, _, _, is_nh = big_pct_map[name]
                    if is_nh:
                        sector_has_nh = True
                        break
            if sector_has_nh:
                main_sectors.append(s)
            elif zt_cnt >= 3 and adj and adj >= rhythm_days:
                main_sectors.append(s)

    if len(main_sectors) >= 2:
        p(f"**{'、'.join(main_sectors)}** {len(main_sectors)}个板块同时确认主升 → **信号增强**")
        p(f"> 多个强区板块互相印证，趋势行情确认度升高。量能允许时大胆低吸\\n")
    elif len(main_sectors) == 1:
        p(f"**{main_sectors[0]}** 独自主升 → 信号未增强，控制单板块仓位")
    else:
        p("**无主升板块** → 只做防御，不追高位票\n")

    # === v9.4 P0-3: 大小票联动检测 ===
    p("### 🔵 大小票联动检测\n")
    p("> v9.4: 许文杰第一性原理——大票信用面决定方向，大小票分化是关键结构性信号\n")
    
    # 尝试加载前日快照做跨日比较
    try:
        prev_snaps = load_sector_snapshots(prev_date) if prev_date else None
    except:
        prev_snaps = None
    
    linkage_type, linkage_lines = size_linkage_cross_day(snapshots, prev_snaps)
    for ll in linkage_lines:
        p(ll)
    p("")

    # v9.4: 废除模板化14板块预案→按三分类动态生成
    p("| 板块 | LLM定性 | v9.4明日预案 |")
    p("|------|---------|-------------|")
    for sector in TREND_SECTORS:
        info = llm_result.get(sector, {}) if llm_result else {}
        cat = info.get("分类", "—")
        cat_emoji = {"主升强区": "🟢", "二三观察": "🟡", "退潮放弃": "🔴"}.get(cat, "⚪")
        
        # 按分类生成预案
        if "主升" in cat:
            plan = "直接跟踪→回调低吸，仓位5-6成；大票新高=加仓信号"
        elif "二三" in cat:
            plan = "等确认信号→调满窗口+大票联动可动手；不抢跑"
        elif "退潮" in cat:
            plan = "不碰，反弹只卖不买"
        else:
            plan = "—"
        
        p(f"| **{sector}** | {cat_emoji}{cat} | {plan} |")
    p("")
    
    # === NEW v8.5 P1-1: 五维度共振 ===
    p("### 🧠 五维度共振\n")

    # 维度1: 容量（量能）
    if today_amt_tri >= 3.0:
        cap_level, cap_signal, cap_score = "充裕", "共振", 2
    elif today_amt_tri >= 2.5:
        cap_level, cap_signal, cap_score = "正常", "中性", 1
    elif today_amt_tri >= 2.0:
        cap_level, cap_signal, cap_score = "偏紧", "衰减", 0
    else:
        cap_level, cap_signal, cap_score = "枯竭", "衰减", -1

    # 维度2: 情绪周期
    if "高潮" in cycle or "上升" in cycle or "强势加速" in cycle:
        emo_signal, emo_score = "共振", 2
    elif "修复" in cycle:
        emo_signal, emo_score = "修复中", 1
    elif "冰点" in cycle:
        emo_signal, emo_score = "衰减", -1
    else:
        emo_signal, emo_score = "衰减", -2

    # 维度3: 大票定性
    big_total = pos + neg
    if big_total > 0 and big_pos_ratio >= 0.7:
        big_signal, big_score = "共振", 2
    elif big_total > 0 and big_pos_ratio >= 0.5:
        big_signal, big_score = "中性", 1
    elif big_total > 0 and big_pos_ratio >= 0.3:
        big_signal, big_score = "衰减", 0
    else:
        big_signal, big_score = "衰减", -1

    # 维度4: 板块联动
    if len(main_sectors) >= 3:
        link_signal, link_score = "共振", 2
    elif len(main_sectors) == 2:
        link_signal, link_score = "共振", 1
    elif len(main_sectors) == 1:
        link_signal, link_score = "衰减", 0
    else:
        link_signal, link_score = "衰减", -1

    # 维度5: 结构风险（黄牌扩散程度）
    yellow_tmp = list(set(yellow_big_names))
    yellow_zt_ratio = len(yellow_tmp) / max(zt_count, 1)
    if yellow_zt_ratio <= 0.15:
        struct_signal, struct_score = "共振", 1
    elif yellow_zt_ratio <= 0.30:
        struct_signal, struct_score = "中性", 0
    else:
        struct_signal, struct_score = "衰减", -2

    resonance = cap_score + emo_score + big_score + link_score + struct_score

    p("| 维度 | 数据 | 信号 |")
    p("|------|------|------|")
    p(f"| 📊 容量 | {today_amt_tri:.2f}万亿（{cap_level}） | {cap_signal} |")
    p(f"| 🎭 情绪 | {cycle} | {emo_signal} |")
    p(f"| 🏢 大票 | {pos}/{big_total}正（{big_pos_ratio:.0%}） | {big_signal} |")
    p(f"| 🔗 联动 | {len(main_sectors)}个主升板块 | {link_signal} |")
    p(f"| ⚠️ 结构 | 黄牌{yellow_zt_ratio:.0%}（{len(yellow_tmp)}只） | {struct_signal} |")
    p("")

    if resonance >= 6:
        p(f"**🔋 共振得分：{resonance}/10 → 强共振**，多维度一致向好，是动手窗口\n")
    elif resonance >= 3:
        p(f"**⚡ 共振得分：{resonance}/10 → 弱共振**，部分确认但需等衰减项修复\n")
    elif resonance >= 0:
        p(f"**⏸️ 共振得分：{resonance}/10 → 分歧**，维度打架，轻仓或观望\n")
    else:
        p(f"**🛑 共振得分：{resonance}/10 → 反向共振**，全面衰减，只看不动\n")

    # ③ 板块内辨识度 + 龙一vs跟风（NEW v8.5 P1-2）
    p("### ③ 板块内辨识度（龙一 vs 跟风）\n")
    for sector, zts in sorted(active_sectors.items(), key=lambda x: -len(x[1]))[:5]:
        zts_sorted = sorted(zts, key=lambda x: -x[2])
        leaders = ", ".join(f"{n}({pct:+.1f}%)" for _, n, pct in zts_sorted[:3])
        # P1-2: 封板质量
        leader_code, leader_name, leader_pct = zts_sorted[0]
        leader_row = conn.execute(
            "SELECT open, high, low, close, pct_change FROM daily_kline WHERE code=? ORDER BY date DESC LIMIT 1",
            [leader_code]
        ).fetchone()
        if leader_row:
            lo, lh, ll, lc, lpct = leader_row
            lpc = lc / (1 + lpct / 100.0) if lpct and lpct > -100 else lc
            _, leader_zt_th = board_threshold(leader_code); zt_limit = lpc * (1 + leader_zt_th / 100.0)
            if abs(lo - zt_limit) / max(zt_limit, 1) < 0.005:
                seal_q = "🔒一字"
            elif lh > 0 and abs(lc / lh - 1.0) < 0.005:
                seal_q = "⚡扎实封板"
            elif lh > 0 and lc < lh * 0.99:
                seal_q = "⚠️曾开板"
            else:
                seal_q = "封板"
            p(f"- **{sector}** ({len(zts)}只): {leaders}")
            p(f"  → 龙一 **{leader_name}**({leader_pct:+.1f}%) {seal_q}")
            # 跟风对比
            if len(zts_sorted) > 1:
                followers = zts_sorted[1:min(4, len(zts_sorted))]
                f_tags = []
                for fc_code, fn, fp in followers:
                    fr = conn.execute(
                        "SELECT open, high, close FROM daily_kline WHERE code=? ORDER BY date DESC LIMIT 1",
                        [fc_code]
                    ).fetchone()
                    if fr:
                        fo2, fh2, fc2 = fr
                        if abs(fc2 / max(fh2, 1) - 1.0) < 0.005:
                            f_tags.append(f"{fn}扎实")
                        elif fc2 < fh2 * 0.99:
                            f_tags.append(f"{fn}曾开板")
                        else:
                            f_tags.append(f"{fn}正常")
                if f_tags:
                    p(f"  → 跟风: {', '.join(f_tags)}")
        else:
            p(f"- **{sector}** ({len(zts)}只): {leaders}")
    p("")

    # ============================================================
    # STEP 3: 黄牌个股（扩展到板块涨停票）
    # ============================================================
    p("## 四、黄牌观察\n")

    # 3.1 固定大票黄牌
    p("### 🟡 核心大票黄牌\n")
    if yellow_big_names:
        for name in yellow_big_names:
            p(f"- **{name}** ⚠️ 高不破昨高+低破昨低 → 明日不修复=弱化确认")
    else:
        p("*今日核心大票无黄牌*\n")

    # 3.2 板块涨停票黄牌（NEW v8.4 +比例+判断）
    p("### 🟡 板块涨停票黄牌（+比例+判断）\n")

    all_yellow_tickets = list(yellow_big_names)

    # 对所有涨停票检查黄牌
    for r in zt_list:
        code = r[0]
        if code in BIG_CAPS: continue  # 已在大票
        prev_row = conn.execute(
            "SELECT high, low FROM daily_kline WHERE code=? AND date=?", [code, prev_date]
        ).fetchone()
        if prev_row and r[4] is not None and r[5] is not None:
            if r[4] <= prev_row[0] and r[5] < prev_row[1]:
                name = zt_names_map.get(code, code)
                all_yellow_tickets.append(name)

    # 从涨停票里找黄牌（按板块归类展示）
    zt_yellow_by_sector = defaultdict(list)
    for r in zt_list:
        code = r[0]
        if code in BIG_CAPS: continue
        prev_row = conn.execute(
            "SELECT high, low FROM daily_kline WHERE code=? AND date=?", [code, prev_date]
        ).fetchone()
        if prev_row and r[4] is not None and r[5] is not None:
            if r[4] <= prev_row[0] and r[5] < prev_row[1]:
                name = zt_names_map.get(code, code)
                pct = r[1] or 0
                # 找板块
                c_rows = conn.execute("SELECT concept_name FROM si.stock_concepts WHERE code=? AND concept_name NOT IN (" +
                    skip_ph + ")", [code] + skip_list).fetchall()
                sector, _ = assign_sector_single(code, c_rows)
                if sector:
                    zt_yellow_by_sector[sector].append((name, pct))
                else:
                    zt_yellow_by_sector["其他"].append((name, pct))

    sector_yellow_warnings = []  # 高黄牌板块
    sector_weakening = []       # v8.10 P0-1: 转弱板块（黄牌比例环比上升>15%）

    # v8.10 P0-1: 计算前一日各板块黄牌比例做对比
    prev_sector_yellow = {}  # sector -> {zt_cnt, yellow_cnt, ratio}
    if prev_date:
        prev_zt_rows = conn.execute(f"""
            SELECT code, pct_change, high, low, date
            FROM daily_kline WHERE date='{prev_date}' AND volume>0 AND close>0
        """).fetchall()
        for r in prev_zt_rows:
            code, pct, ph, pl = r[0], r[1] or 0, r[2], r[3]
            _, zt_th = board_threshold(code)
            if pct < zt_th: continue
            name = names.get(code, code)
            if is_noise(name): continue
            if code in BIG_CAPS: continue
            # check yellow
            pp_row = conn.execute(
                "SELECT high, low FROM daily_kline WHERE code=? AND date<? ORDER BY date DESC LIMIT 1",
                [code, prev_date]
            ).fetchone()
            is_yellow = pp_row and ph and pl and ph <= pp_row[0] and pl < pp_row[1]
            c_rows = conn.execute(
                "SELECT concept_name FROM si.stock_concepts WHERE code=? AND concept_name NOT IN (" + skip_ph + ")",
                [code] + skip_list
            ).fetchall()
            sector, _ = assign_sector_single(code, c_rows)
            if not sector: continue
            ps = prev_sector_yellow.setdefault(sector, {"zt": 0, "yl": 0})
            ps["zt"] += 1
            if is_yellow: ps["yl"] += 1
        for sec in prev_sector_yellow:
            ps = prev_sector_yellow[sec]
            ps["ratio"] = ps["yl"] / ps["zt"] * 100 if ps["zt"] > 0 else 0

    if zt_yellow_by_sector:
        p("| 板块 | 涨停数 | 黄牌数 | 黄牌比例 | vs昨日 | 趋势 | 判断 | 个股 |")
        p("|------|--------|--------|----------|--------|------|------|------|")
        for sector in TREND_SECTORS:
            yellow_stks = zt_yellow_by_sector.get(sector, [])
            zt_stks = zt_by_sector.get(sector, [])
            zt_cnt = len(zt_stks)
            yellow_cnt = len(yellow_stks)
            if zt_cnt == 0:
                continue
            ratio = yellow_cnt / zt_cnt * 100 if zt_cnt > 0 else 0
            # v8.10 P0-1: vs昨日对比
            prev_s = prev_sector_yellow.get(sector, {})
            prev_ratio = prev_s.get("ratio", -1)
            if prev_ratio >= 0:
                delta = ratio - prev_ratio
                if delta >= 20:
                    trend_str = "📈转弱"
                    sector_weakening.append((sector, delta))
                elif delta >= 10:
                    trend_str = "⚠️略升"
                elif delta <= -15:
                    trend_str = "✅转强"
                elif delta <= -5:
                    trend_str = "→略降"
                else:
                    trend_str = "→持平"
                vs_str = f"{prev_ratio:.0f}%→{ratio:.0f}%"
            else:
                vs_str = "—"
                trend_str = "—"
            if ratio >= 50:
                judgment = "🚨板块结构风险"
                sector_yellow_warnings.append((sector, ratio))
            elif ratio >= 30:
                judgment = "⚠️需观察"
            elif ratio > 0:
                judgment = "✅个别正常"
            else:
                judgment = "✅无黄牌"

            detail = ", ".join(f"{n}({pct:+.1f}%)" for n, pct in yellow_stks) if yellow_stks else "—"
            p(f"| **{sector}** | {zt_cnt}只 | {yellow_cnt}只 | **{ratio:.0f}%** | {vs_str} | {trend_str} | {judgment} | {detail} |")

        # 展示不在TREND_SECTORS中的板块
        for sector, stks in sorted(zt_yellow_by_sector.items()):
            if sector in TREND_SECTORS:
                continue
            zt_stks = zt_by_sector.get(sector, [])
            zt_cnt = len(zt_stks)
            yellow_cnt = len(stks)
            ratio = yellow_cnt / zt_cnt * 100 if zt_cnt > 0 else 0
            judgment = "⚠️需观察" if ratio >= 30 else "—"
            detail = ", ".join(f"{n}({pct:+.1f}%)" for n, pct in stks)
            p(f"| {sector} | {zt_cnt}只 | {yellow_cnt}只 | **{ratio:.0f}%** | {judgment} | {detail} |")
    else:
        p("*今日涨停票中无个股级黄牌*\n")

    p(f"\n**黄牌总计**：{len(all_yellow_tickets)}只（大票{yellow_big}+涨停票{len(all_yellow_tickets)-yellow_big}）\n")

    # 高黄牌板块警告
    if sector_yellow_warnings:
        p(f"\n⚠️ **高黄牌板块警告**：")
        for sec, ratio in sorted(sector_yellow_warnings, key=lambda x: -x[1]):
            p(f"- 🏴 **{sec}** → 黄牌比例{ratio:.0f}%，板块结构松动，**减持优先**")
    # v8.10 P0-1: 板块转弱汇总
    if sector_weakening:
        p(f"\n📈 **板块黄牌转弱**（比例上升≥20%）：")
        for sec, delta in sorted(sector_weakening, key=lambda x: -x[1]):
            p(f"- **{sec}** → 黄牌比例+{delta:.0f}%，板块结构恶化，**谨慎低吸**")
        p("")
    p("")

    # ============================================================
    # STEP 4: 连板梯队
    # ============================================================
    p("## 五、连板梯队\n")

    back_dates = [d for d in all_dates if d < DATE]
    code_streaks = {}
    for r in zt_list:
        code = r[0]
        streak = 1
        for bd in reversed(back_dates):
            row = conn.execute(
                "SELECT pct_change FROM daily_kline WHERE code=? AND date=? AND volume>0",
                [code, bd]
            ).fetchone()
            if row and row[0] is not None:
                _, zt_th = board_threshold(code)
                if row[0] >= zt_th: streak += 1
                else: break
            else: break
        code_streaks[code] = streak

    lianban = [(c, s) for c, s in code_streaks.items() if s >= 2]
    max_streak = max(s for _, s in lianban) if lianban else 1

    p("| 名称 | 连板 | 板块 |")
    p("|------|------|------|")
    for code, streak in sorted(lianban, key=lambda x: -x[1]):
        name = zt_names_map.get(code, code)
        short = short_name(name) or name[:6]
        c_rows = conn.execute(
            "SELECT concept_name FROM si.stock_concepts WHERE code=? AND concept_name NOT IN (" +
            skip_ph + ")", [code] + skip_list
        ).fetchall()
        sector, _ = assign_sector_single(code, c_rows)
        p(f"| **{short}** | **{streak}连板** | {sector or '—'} |")
    p(f"\n**最高板**：{max_streak}板\n")

    # ============================================================
    # NEW v8.4 P0-2: 短线数据补全 — 晋级率/溢价/炸板率
    # ============================================================
    p("## 五·五 📊 短线数据\n")

    # 1. 昨日涨停列表
    yest_zt_codes = set()
    yest_all_rows = conn.execute(f"SELECT code, pct_change, close, high, volume FROM daily_kline WHERE date='{prev_date}' AND volume>0 AND close>0").fetchall()
    for r in yest_all_rows:
        code, pct = r[0], r[1] or 0
        _, zt_th = board_threshold(code)
        name = names.get(code, code)
        if is_noise(name): continue
        if pct >= zt_th:
            yest_zt_codes.add(code)
    yest_zt_total = len(yest_zt_codes)

    # 2. 晋级率 = 昨天涨停今天还涨停 / 昨天涨停总数
    jinji_codes = zt_codes_set & yest_zt_codes
    jinji_count = len(jinji_codes)
    jinji_rate = jinji_count / yest_zt_total * 100 if yest_zt_total > 0 else 0

    # 3. 昨日涨停溢价（昨天涨停票今天的平均涨幅）
    yest_zt_today_pcts = []
    for code in yest_zt_codes:
        today_row = conn.execute(
            "SELECT pct_change FROM daily_kline WHERE code=? AND date=? AND volume>0",
            [code, DATE]
        ).fetchone()
        if today_row and today_row[0] is not None:
            yest_zt_today_pcts.append(today_row[0])

    avg_premium = sum(yest_zt_today_pcts) / len(yest_zt_today_pcts) if yest_zt_today_pcts else 0
    premium_pos = sum(1 for p in yest_zt_today_pcts if p > 0)
    premium_neg = sum(1 for p in yest_zt_today_pcts if p <= 0)

    # 4. 今日炸板（摸到涨停但没封住的）
    pop_codes = []
    today_all = conn.execute(f"SELECT code, pct_change, close, high, volume FROM daily_kline WHERE date='{DATE}' AND volume>0 AND close>0").fetchall()
    for r in today_all:
        code = r[0]
        if code in zt_codes_set: continue  # 已涨停的不算
        pct = r[1] or 0
        high_val, close_val = r[3] or 0, r[2] or 0
        _, zt_th = board_threshold(code)
        name = names.get(code, code)
        if is_noise(name): continue
        # 摸涨停阈值：最高价涨幅接近涨停阈值
        if high_val > 0 and close_val > 0:
            prev_close = close_val / (1 + pct / 100) if pct != -100 else 0
            if prev_close > 0:
                high_pct = (high_val - prev_close) / prev_close * 100
                if high_pct >= zt_th * 0.95:  # 摸到涨停附近
                    pop_codes.append((code, name, pct, high_pct))

    # For reporting
    pop_count = len(pop_codes)
    total_challenge = zt_count + pop_count  # 冲击涨停的总数
    pop_rate = pop_count / total_challenge * 100 if total_challenge > 0 else 0

    p(f"| 指标 | 数值 | 判断 |")
    p(f"|------|------|------|")
    p(f"| 涨停家数 | {zt_count}家（昨 {yest_zt_total}家） | {'↑增多' if zt_count > yest_zt_total else '↓减少' if zt_count < yest_zt_total else '→持平'} |")
    p(f"| 晋级率 | {jinji_count}/{yest_zt_total} = **{jinji_rate:.0f}%** | {'✅接力强' if jinji_rate >= 40 else '⚠️一般' if jinji_rate >= 20 else '❌接力弱'} |")
    p(f"| 昨日涨停溢价 | **{avg_premium:+.2f}%** ({premium_pos}正{premium_neg}负) | {'🟢正反馈' if avg_premium > 1 else '🟡中性' if avg_premium > -1 else '🔴负反馈'} |")
    p(f"| 炸板率 | {pop_count}/{total_challenge} = **{pop_rate:.0f}%** | {'🚨炸板多' if pop_rate >= 30 else '⚠️需关注' if pop_rate >= 20 else '✅封板稳'} |")

    # 详细晋级/炸板列表
    if jinji_codes:
        jinji_names = [f"{names.get(c,c)}({code_streaks.get(c,1)}板)" for c in sorted(jinji_codes, key=lambda c: -code_streaks.get(c,1))[:8]]
        p(f"\n**晋级票**：{' | '.join(jinji_names)}")
    else:
        p(f"\n**晋级票**：无（昨日涨停全部断板→情绪弱化信号）")

    if pop_codes:
        pop_names = [f"{n}({pct:+.1f}%)" for _, n, pct, _ in sorted(pop_codes, key=lambda x: -x[3])[:6]]
        p(f"**今日炸板**：{' | '.join(pop_names)}")
    else:
        p(f"**今日炸板**：无")

    p("")

    # ============================================================
    # NEW v8.3: P0-2 持仓审视（该卖什么）
    # ============================================================
    p("## 五点半 ⚠️ 持仓审视（该卖什么）\n")

    # 1. 黄牌大票所在的板块 → 风险预警
    yellow_sectors = set()
    if yellow_big_names:
        p("### 🔴 黄牌大票 → 这些票所在板块有结构风险\n")
        si_conn = sqlite3.connect(SI_DB)
        yy = []
        for yb in yellow_big_names:
            name_only = yb.split('(')[0].strip() if '(' in yb else yb
            code = yb.split('(')[1].rstrip(')') if '(' in yb else None
            if code:
                rows = si_conn.execute(
                    "SELECT concept_name FROM stock_concepts WHERE code=?",
                    [code]
                ).fetchall()
                yy.append((code, [(code, r[0]) for r in rows]))

        for code, con_rows in yy:
            sector, _ = assign_sector_single(code, con_rows)
            if sector and sector != '未归类':
                yellow_sectors.add(sector)

        for yb in yellow_big_names:
            name_only = yb.split('(')[0].strip() if '(' in yb else yb
            code = yb.split('(')[1].rstrip(')') if '(' in yb else None
            found_sector = None
            if code:
                con_rows = si_conn.execute(
                    "SELECT concept_name FROM stock_concepts WHERE code=?",
                    [code]
                ).fetchall()
                c_rows = [(code, r[0]) for r in con_rows]
                found_sector, _ = assign_sector_single(code, c_rows)
            if found_sector:
                s_tag = f"🟡[{found_sector}]" if found_sector != '未归类' else ""
                p(f"- **{name_only}** {s_tag} → 该大票出现GaN/放量长上影，板块松动信号")

        if yellow_sectors:
            p(f"\n⚠️ **黄牌覆盖板块**：{', '.join(sorted(yellow_sectors))}")
            p(f"> 持有这些板块的仓位→ 减仓/止盈优先\n")
        else:
            p("\n*黄牌大票未归类到特定主升板块→按个股跟踪，关注明日是否修复*\n")
        si_conn.close()

    # 2. LLM定性退潮板块
    tide_sectors = []
    if llm_result:
        for s, info in llm_result.items():
            cat = info.get('分类', '')  # v8.8.3: fix stage→分类
            if '退潮' in cat:
                tide_sectors.append(s)

    # v8.8.3: 补充零涨停板块→可能退潮（LLM未送入但需关注）
    for s in TREND_SECTORS:
        if s not in tide_sectors and s not in (llm_result or {}):
            zt_cnt = len(zt_by_sector.get(s, []))
            adj_d = sector_days_adjusting.get(s)
            if zt_cnt == 0 and adj_d is None:
                # 连续无涨停→可能退潮
                tide_sectors.append(s)

    if tide_sectors:
        p("### 🌊 退潮板块 → 不碰不追\n")
        for s in tide_sectors:
            detail = llm_result.get(s, {}).get('detail', '')
            p(f"- 🏴 **{s}**：退潮期 | {detail}")
        p("\n> 退潮板块的票→ 反弹是用来卖的，不是买的\n")
    
    # v9.3: 黄牌比例健康度分析
    p(yellow_card_analysis(snapshots))

    if not yellow_big_names and not tide_sectors:
        p("*今日无明确卖出信号 — 持仓票按既有策略持有*\n")

    # ============================================================
    # ============================================================
    # NEW v8.5 P1-3: 一字板检查
    # ============================================================
    p("## 五·七 🔒 一字板检查\n")
    # 批量查 zt stocks 的 open + pre_close
    zt_codes = [r[0] for r in zt_list]
    yizi_stocks = []  # FIX v8.7: 提前初始化，避免 UnboundLocalError
    if zt_codes:
        placeholders = ','.join(['?'] * len(zt_codes))
        zt_details = conn.execute(
            f"SELECT code, open, pct_change, close FROM daily_kline WHERE code IN ({placeholders}) AND date='{DATE}'",
            zt_codes
        ).fetchall()
        zd_map = {r[0]: (r[1], r[2], r[3]) for r in zt_details}

        yizi_stocks = []
        for r in zt_list:
            code = r[0]
            detail = zd_map.get(code)
            if detail:
                open_p, pct_c, close_c = detail
                pre_c = close_c / (1 + pct_c / 100.0) if pct_c and pct_c > -100 else close_c
                _, zt_t = board_threshold(code)
                zt_lim = pre_c * (1 + zt_t / 100.0)
                if zt_lim > 0 and abs(open_p - zt_lim) / zt_lim < 0.005:
                    name = names.get(code, code)
                    pct = r[1] or 0
                    amt = r[2] or 0
                    yizi_stocks.append((code, name, pct, amt))

        if yizi_stocks:
            yizi_stocks.sort(key=lambda x: -x[2])
            p(f"今日 {len(yizi_stocks)} 只一字涨停：")
            p("| 股票 | 涨幅 | 成交额 |")
            p("|------|------|--------|")
            for code, name, pct, amt in yizi_stocks:
                amt_s = f"{amt/1e8:.1f}亿" if amt and amt > 0 else "—"
                p(f"| **{name}** | {pct:+.1f}% | {amt_s} |")
            p(f"")
            p(f"> ⚠️ 一字涨停今日无买入机会，明日高开不追——等分歧后低吸")
            p(f"> 一字板次日若开板放量 → 观察承接后再定")
            p("")
        else:
            p("*今日无一字涨停*\n")

    # ============================================================
    # v8.8: Build strong_sectors + sector_rhythm BEFORE rules
    # LLM定性已完成（line ~890），从llm_result提取强区板块
    strong_sectors = set()
    if llm_result:
        for s, info in llm_result.items():
            if "主升" in info.get("分类", ""):
                strong_sectors.add(s)

    # sector_rhythm: 强区=1天, 非强区=标准窗口
    sector_rhythm = {}
    for s in sector_days_adjusting:
        sector_rhythm[s] = 1.0 if s in strong_sectors else rhythm_days

    # STEP 4.5: 策略规则校验（7项查杀）
    # NOTE: 所有已有变量在此点均可访问
    p("## 五·八 \U0001f4cb 策略规则校验\n")

    rules = []
    # count via icon matching at end, no need for manual fail_count

    # Rule 1: 高位禁买（>=3板不追）
    r1_stocks = [(c, s) for c, s in lianban if s >= 3]
    if r1_stocks:
        detail = "; ".join(f"{zt_names_map.get(c,c)}({s}板)" for c, s in sorted(r1_stocks, key=lambda x:-x[1]))
        rules.append(("1. 高位禁买", "连板>=3板不追", f"{len(r1_stocks)}只触发", "⚠️", detail))
    else:
        rules.append(("1. 高位禁买", "连板>=3板不追", "无触发", "✅", ""))

    # Rule 2: 调不够禁买
    r2_short = [(s, d) for s, d in sector_days_adjusting.items() if d < sector_rhythm.get(s, rhythm_days)]
    if r2_short:
        detail = "; ".join(f"{s}(调{d}天)" for s, d in sorted(r2_short, key=lambda x:x[1]))
        rules.append(("2. 调不够禁买", f"板块调整>=强区1天/轮动{rhythm_days}天", f"{len(r2_short)}个板块未达标", "⚠️", detail))
    else:
        rules.append(("2. 调不够禁买", f"板块调整>={rhythm_days}天方可买", f"全部板块达标", "✅", ""))

    # Rule 3: 高潮禁追
    # v8.7: 量能+大票豁免 — 涨停>100在量够+大票强时不是风险
    if zt_count > 100:
        if today_amt_tri >= 3.0 and big_pos_ratio >= 0.7:
            rules.append(("3. 高潮禁追", "涨停>100不追新仓", f"涨停{zt_count}家>100", "✅", f"量能{today_amt_tri:.1f}万亿+大票强→涨停多=强势确认"))
        else:
            rules.append(("3. 高潮禁追", "涨停>100不追新仓", f"涨停{zt_count}家>100", "⚠️", "买点已过，等分歧回调"))
    else:
        rules.append(("3. 高潮禁追", "涨停>100不追新仓", f"涨停{zt_count}家<=100", "✅", ""))

    # Rule 4: 黄牌结构预警
    yc = len(all_yellow_tickets)
    # v8.7: 区分黄牌性质——结构性(光通信小票) vs 系统性(大票核心)
    structural = yellow_structural if 'yellow_structural' in dir() else False
    if yc >= 5:
        if big_pos_ratio >= 0.7 and today_amt_tri >= 3.0:
            rules.append(("4. 黄牌结构预警", "黄牌>=5结构性(大票仍强)", f"黄牌{yc}只≥5", "⚠️", "光通信小票分化→结构性，密切关注不恐慌"))
        else:
            rules.append(("4. 黄牌结构预警", "黄牌>=5降仓防御", f"黄牌{yc}只>=5", "⚠️", "结构性脆弱，仓位降至5成"))
    elif yc >= 3:
        rules.append(("4. 黄牌结构预警", "黄牌>=3关注扩散", f"黄牌{yc}只>=3", "⚠️", "关注是否扩散"))
    else:
        rules.append(("4. 黄牌结构预警", "黄牌>=3关注扩散", f"黄牌{yc}只<3", "✅", ""))

    # Rule 5: 弱反弹放过
    # v8.7: 强区板块1天反弹不触发 — 许文杰：强区1天就是买点
    # R5 uses strong_sectors built above (before rules section)
    r5_weak = [(s, d) for s, d in sector_days_adjusting.items() if d == 1 and s not in strong_sectors]
    if r5_weak:
        detail = "; ".join(s for s, _ in r5_weak)
        rules.append(("5. 弱反弹放过", f"非强区调1天反弹不做", f"{len(r5_weak)}个板块调1天反弹", "⚠️", detail))
    else:
        rules.append(("5. 弱反弹放过", "调1天反弹不做", "无触发", "✅", ""))

    # Rule 6: 一字板次日预警
    if yizi_stocks:
        detail = "; ".join(n for _, n, _, _ in yizi_stocks[:5])
        if len(yizi_stocks) > 5: detail += f" +{len(yizi_stocks)-5}只"
        rules.append(("6. 一字板次日预警", "今日一字板次日不追", f"{len(yizi_stocks)}只一字板", "⚠️", f"明日高开不追: {detail}"))
    else:
        rules.append(("6. 一字板次日预警", "今日一字板次日不追", "无一字板", "✅", ""))

    # Rule 7: 进退策略核对
    r7_issues = []
    if resonance >= 6 and zt_count >= 100:
        r7_issues.append("强共振+强势:趋势延续，低吸轮动")
    if yc >= 5 and resonance >= 5:
        r7_issues.append("黄牌>=5+共振:结构性脆弱")
    if today_amt_tri < 2.8 and zt_count > 80:
        r7_issues.append("量能不足+涨停多:假强势")
    if r7_issues:
        detail = "; ".join(r7_issues)
        rules.append(("7. 进退策略核对", "策略矛盾检测", f"{len(r7_issues)}个矛盾", "⚠️", detail))
    else:
        rules.append(("7. 进退策略核对", "策略矛盾检测", "无矛盾", "✅", ""))

    # Table output: (number, trigger_rule, check_result, icon, detail)
    p("| 状态 | 规则 | 检查结果 | 详情 |")
    p("|------|------|----------|------|")
    for num, rule, result, icon, detail in rules:
        p(f"| {icon} | {rule} | {result} | {detail} |")
    p("")

    # Overall
    fail_count = sum(1 for _, _, _, icon, _ in rules if icon == "⚠️")
    # v8.8: 量能豁免——量能>=3.0万亿+大票偏强时，fail_count只是技术提示，不应否定进攻基调
    volume_exempt = (today_amt_tri >= 3.0 and big_pos_ratio >= 0.7)
    if fail_count == 0:
        p("> ✅ 7/7项通过 — 策略环境健康，可正常执行\n")
    elif fail_count <= 2 or volume_exempt:
        p(f"> ⚠️ {fail_count}/7项触发 — 需注意规避风险点，控制仓位\n")
    elif fail_count <= 4:
        p(f"> 🟡 {fail_count}/7项触发 — 策略环境一般，降低仓位、精选标的\n")
    else:
        if volume_exempt:
            p(f"> 🟡 {fail_count}/7项触发 — 量能豁免（量能{today_amt_tri:.1f}万亿+大票强），注意规避风险点即可\n")
        else:
            p(f"> 🔴 {fail_count}/7项触发 — 策略环境恶劣，建议观望或极轻仓\n")
        # STEP 5: 不做清单（具体化）
    # ============================================================
    p("## 六、⚠️ 不做清单（具体化）\n")

    no_list = []

    # 1. 连板方向不追 → 具体到票
    high_streak = [(c, s) for c, s in lianban if s >= 3]
    if high_streak:
        detail = ", ".join(f"**{zt_names_map.get(c,c)}**({s}板)" for c, s in sorted(high_streak, key=lambda x:-x[1]))
        no_list.append(f"🔴 **≥3板不追**：{detail}，一字板没开的不做")

    # 2. 连板生态方向不追
    non_trend_zts = zt_count - sum(len(v) for v in zt_by_sector.values())
    if non_trend_zts > zt_count * 0.4:
        # 找连板最多的方向
        lianban_sectors = defaultdict(int)
        for code, _ in lianban:
            c_rows = conn.execute(
                "SELECT concept_name FROM si.stock_concepts WHERE code=? AND concept_name NOT IN (" +
                skip_ph + ")", [code] + skip_list
            ).fetchall()
            sector, _ = assign_sector_single(code, c_rows)
            if sector: lianban_sectors[sector] += 1
        top_lianban = sorted(lianban_sectors.items(), key=lambda x:-x[1])[:2]
        if top_lianban:
            detail = ", ".join(f"{s}({n}板)" for s, n in top_lianban)
            no_list.append(f"🔴 **连板方向不追**：{detail}，非趋势主力不碰")

    # 3. 调不够窗口的板块 → 具体到板块名
    early_sectors = [(s, d) for s, d in sector_days_adjusting.items() if d < sector_rhythm.get(s, rhythm_days)]
    if early_sectors:
        detail = ", ".join(f"**{s}**(调{d}天)" for s, d in sorted(early_sectors, key=lambda x: x[1])[:4])
        no_list.append(f"🟡 **调不够{rhythm_days}天的不急**：{detail}")

    # 4. 调1天反弹放掉 → 具体到板块
    one_day_sectors = [(s, d) for s, d in sector_days_adjusting.items() if d == 1 and s not in strong_sectors]
    if one_day_sectors:
        detail = ", ".join(f"**{s}**" for s, _ in one_day_sectors)
        no_list.append(f"🟡 **调1天反弹→放掉**：{detail}，不够弱不值得抄底")

    # 5. 量能不足限制
    if today_amt_tri < 3.0:
        no_list.append(f"🔴 **量能不足{today_amt_tri:.1f}万亿**（<3万亿）→ 单票仓位减半，最后一笔不做")

    # 6. 高潮不追
    # v8.7: 量能+大票豁免 — 量够+大票强时涨停多≠高潮风险
    if zt_count > 100:
        if today_amt_tri >= 3.0 and big_pos_ratio >= 0.7:
            no_list.append(f"✅ **涨停{zt_count}家但**：量能{today_amt_tri:.1f}万亿+大票强→强势确认，可继续低吸")
        else:
            no_list.append("🔴 **高潮不追**：涨停>100家→买点已过，等分歧")

    # 7. 退潮板块不碰（来自LLM定性）— 二三观察≠退潮，二三观察是等信号
    # 第一性原理：许文杰的"退潮放弃"才是真不碰，"二三观察"只是等确认
    if llm_result:
        avoid_sectors = []
        for s, info in llm_result.items():
            cat = info.get("分类")
            seczt = len(zt_by_sector.get(s, []))
            # v8.8.1: ZT>=3且有调整节奏的板块不可能退潮（LLM非确定性兜底）
            if cat in ("退潮放弃",) and 3 <= seczt <= 8 and s in sector_days_adjusting:
                cat = "二三观察"
            if cat in ("退潮放弃",):
                avoid_sectors.append(s)
        if avoid_sectors:
            no_list.append(f"🔴 **不碰板块（退潮）**：{'、'.join(avoid_sectors)}（LLM判定退潮放弃→放掉）")

    for item in no_list:
        p(f"- {item}")
    if not no_list:
        p("*今日无特别不做项*\n")
    p("")

    # ============================================================
    # v8.10 P0-2+P0-4: 明日三情景预案（大小票联动+动态化）
    # 许文杰第一性原理：预案不是预测，是"如果A做X，如果B做Y"
    # 大小票联动分析：大票定方向，小票定力度
    # ============================================================
    p("## 六·五 🔮 明日三情景预案（大小票联动）\n")

    # 大小票联动模态判断
    # 大票强+小票强 = 共振上攻
    # 大票强+小票弱 = 结构性分化
    # 大票弱+小票强 = 指数拖累
    # 大票弱+小票弱 = 全面退潮
    small_ticket_mood = mood  # 涨停情绪
    if big_pos_ratio >= 0.6 and ("上升" in cycle or "强势" in cycle):
        linkage = "🟢 **大小票共振**：大票+小票同步走强→进攻时段"
        linkage_score = 3
    elif big_pos_ratio >= 0.6 and small_ticket_mood in ("⚡分歧", "混沌/修复"):
        linkage = "🟡 **大票强小票弱**：结构性行情→聚焦大票容量品种"
        linkage_score = 2
    elif big_pos_ratio < 0.5 and ("上升" in cycle or "强势" in cycle):
        linkage = "🟡 **小票强大票弱**：游资主导→指数可能拖累，短线为主"
        linkage_score = 1
    elif big_pos_ratio < 0.5 and small_ticket_mood in ("⚡分歧", "冰点"):
        linkage = "🔴 **大小票双弱**：全面退潮→观望防守"
        linkage_score = 0
    else:
        linkage = "➡️ 大小票分化→等待明确方向"
        linkage_score = 1

    p(f"**今日联动模态**：{linkage}\n")

    # 构建三情景参数池
    # 乐观因子：量能充裕+大票强+涨停活跃+核心温和
    opt_score = sum([today_amt_tri >= 3.0, big_pos_ratio >= 0.6, zt_count >= 60, core_mild, linkage_score >= 2])
    # 悲观因子：量能不足+黄牌扩散+退潮+涨停萎缩+指数大跌
    pes_score = sum([today_amt_tri < 2.8, len(all_yellow_tickets) >= 5, len(tide_sectors) >= 2 if tide_sectors else False, zt_count < 30, idx_drop_hard if 'idx_drop_hard' in dir() else False])

    # 情景1：乐观（延续/加速）— 30%概率锚
    p("### 🟢 情景A：延续/加速（乐观）\n")
    opt_ops = []
    if today_amt_tri >= 3.0:
        opt_ops.append(f"量能{today_amt_tri:.1f}万亿持续→强势板块可追龙头")
    else:
        opt_ops.append("量能回升至3.0万亿以上→确认信号")
    if big_pos_ratio >= 0.6:
        opt_ops.append(f"大票维持{pos}正{neg}负→容量品种积极低吸")
    else:
        opt_ops.append(f"大票转强(正>负)→关注{', '.join(list(BIG_CAPS.keys())[:3])}等核心大票")
    if linkage_score >= 2:
        opt_ops.append("大小票共振→7-8成仓位，板块轮动低吸")
    else:
        opt_ops.append("大小票同步转强→加至6-7成")
    for op in opt_ops:
        p(f"- ✅ {op}")
    p(f"> **操作锚点**：明早量能{today_amt_tri*1.05:.1f}万亿+大票{pos}正{neg}负确认→执行\n")

    # 情景2：中性（震荡/分化）— 50%概率锚（默认最高概率）
    p("### 🟡 情景B：震荡/分化（中性·默认最高概率）\n")
    neu_ops = []
    if today_amt_tri >= 2.8:
        neu_ops.append(f"量能{today_amt_tri:.1f}万亿维持→5-6成仓位，低吸不追高")
    else:
        neu_ops.append("量能维持在2.5-3.0万亿→3-5成仓位防守")
    if "收缩态" in (pos_str if 'pos_str' in dir() else ""):
        neu_ops.append("收缩态→不开新仓，做T+了结")
    else:
        neu_ops.append(f"{rhythm_days}天窗口期→轮到{rhythm_days}天调整的板块")
    # 大小票中性预案
    if linkage_score == 2:
        neu_ops.append("大票强+小票弱→买入大票容量品种，回避小票情绪追涨")
    elif linkage_score == 1:
        neu_ops.append("小票强+大票弱→短线快进快出，止损线收紧至-3%")
    neu_ops.append(f"黄牌{len(all_yellow_tickets)}只→聚焦低黄牌板块")
    for op in neu_ops:
        p(f"- ⚠️ {op}")
    p(f"> **操作锚点**：明早{rhythm_days}天窗口→板块调整节奏确认→执行\n")

    # 情景3：悲观（退潮/恶化）— 20%概率锚
    p("### 🔴 情景C：退潮/恶化（悲观）\n")
    pes_ops = []
    pes_ops.append(f"黄牌扩散至{len(all_yellow_tickets)+3}只+→全面防守，只卖不买")
    if today_amt_tri >= 2.8:
        pes_ops.append(f"量能萎缩至<2.5万亿→减至3成以下")
    else:
        pes_ops.append("量能继续萎缩→减至1-2成或空仓")
    if tide_sectors:
        pes_ops.append(f"退潮板块{'、'.join(tide_sectors[:3])}→反弹即卖点，不抄底")
    pes_ops.append("核心大票破位(易中天水下跌>3%)→清仓观望")
    if core_mild:
        pes_ops.append(f"核心大票目前温和→大概率不触发情景C，仅做防守准备")
    for op in pes_ops:
        p(f"- 🔴 {op}")
    p(f"> **操作锚点**：明早核心大票破位+量能<2.5万亿双确认→执行退潮预案\n")

    # ============================================================
    # v8.9 NEW: 异动监管模块 (P0-4)
    # 许文杰核心框架：异动=30日区间涨幅排名+躲异动/冲异动状态+趋势板块加持判断
    # ============================================================
    p("## 六、📊 异动监管（30日偏离值排名）\n")
    # 收集所有趋势板块+黄牌板块内个股的30日区间统计
    zx_candidate_codes = []
    zx_stock_map = {}  # code -> (name, sector)
    for sector in TREND_SECTORS:
        for _, name, pct in zt_by_sector.get(sector, []):
            if name not in zx_stock_map:
                zx_stock_map[name] = (name, sector)
    # 补上黄牌个股
    for name in all_yellow_tickets:
        if name not in zx_stock_map:
            zx_stock_map[name] = (name, "黄牌")

    # 查询每只票30日涨跌幅和期间阳线占比
    zx_30d_rows = []
    for name, (_, sector) in zx_stock_map.items():
        code = names_rev.get(name)
        if not code:
            continue
        # 30日区间、阳线次数、区间最高/最低
        row = conn.execute("""
            SELECT
                (SELECT close FROM daily_kline WHERE code=? AND date<=? AND close>0 ORDER BY date DESC LIMIT 1) as today_close,
                (SELECT close FROM daily_kline WHERE code=? AND date>=? AND close>0 ORDER BY date ASC LIMIT 1) as first_close,
                (SELECT MAX(high) FROM daily_kline WHERE code=? AND date>=? AND date<=?) as max_high,
                (SELECT MIN(low) FROM daily_kline WHERE code=? AND date>=? AND date<=?) as min_low,
                (SELECT COUNT(*) FROM daily_kline WHERE code=? AND date>=? AND date<=? AND pct_change>0) as up_days,
                (SELECT COUNT(*) FROM daily_kline WHERE code=? AND date>=? AND date<=? AND close>0) as total_days,
                (SELECT close FROM daily_kline WHERE code=? AND date<=? AND close>0 ORDER BY date DESC LIMIT 1 OFFSET 1) as prev_close,
                (SELECT close FROM daily_kline WHERE code=? AND date<=? AND close>0 ORDER BY date DESC LIMIT 1 OFFSET 5) as w5_close
        """, [code, DATE, code, start21, code, start21, DATE, code, start21, DATE,
              code, start21, DATE, code, start21, DATE, code, DATE, code, DATE]).fetchone()
        if row and row[0] and row[1] and row[6]:
            today_close, first_close, max_h, min_l, up_days, total_days, prev_close, w5_close = row
            dev30 = ((today_close - first_close) / first_close) * 100
            yang_ratio = up_days / total_days if total_days else 0
            tday_pct = ((today_close - prev_close) / prev_close) * 100
            pullback_from_high = ((today_close - max_h) / max_h) * 100 if max_h else 0
            w5_pct = ((today_close - w5_close) / w5_close) * 100 if w5_close else 0
            zx_30d_rows.append((name, sector, dev30, pullback_from_high, yang_ratio, tday_pct, w5_pct, today_close, max_h))

    # 排序：偏离值从高到低
    zx_30d_rows.sort(key=lambda x: -x[2])

    # 异动状态判断 v9.1: 三模式——躲(>100%) / 绕(70-100%) / 冲/写检讨书(50-70%)
    # 许文杰原教旨：
    #   >100% 偏离 → 躲异动，绝对不碰，反弹只卖不买
    #   70-100% → 绕异动，绕道同板块低位品种
    #   50-70%  → 冲异动/写检讨书，可参与但需深度回调确认+趋势板块加持
    zx_hide_count = 0
    zx_rao_count = 0
    zx_rush_count = 0
    if zx_30d_rows:
        p(f"| 排名 | 个股 | 板块 | 30日偏离 | 距高点 | 阳线占比 | 今日 | 5日 | 异动状态 |")
        p(f"|------|------|------|----------|--------|----------|------|-----|----------|")
        zx_hide_count = 0
        zx_rao_count = 0
        zx_rush_count = 0
        for i, (name, sector, dev30, pullback, yang_ratio, tday_pct, w5_pct, close, max_h) in enumerate(zx_30d_rows):
            # v9.1 P1: 三模式判断
            if dev30 < -5 and pullback < -10:
                status = "🔵 躲异动"
                zx_hide_count += 1
            elif dev30 > 100:
                status = "🚫 躲异动（>100%）"  # 绝对不碰
                zx_hide_count += 1
            elif dev30 > 70:
                status = "🔄 绕异动（70-100%）"  # 绕道低位
                zx_rao_count += 1
            elif dev30 > 50:
                status = "✍️ 冲异动/写检讨书"  # 可参与但需深度回调
                zx_rush_count += 1
            elif dev30 > 15:
                status = "🟡 接近异动"
            elif dev30 < -3:
                status = "🟢 正常回调"
            else:
                status = "⚪ 正常"
            p(f"| {i+1} | {name} | {sector} | {dev30:+.1f}% | {pullback:+.1f}% | {yang_ratio:.0%} | {tday_pct:+.1f}% | {w5_pct:+.1f}% | {status} |")

        # 趋势板块加持判断
        p("")
        zx_trend_sectors = [s for s, info in llm_result.items() if info.get("分类") in ("主升强区", "主升(强区)", "二三观察", "轮动观察")]
        zx_trend_detail = []
        for name, sector, dev30, *_ in zx_30d_rows:
            if sector in zx_trend_sectors:
                zx_trend_detail.append(f"{name}({sector}:{dev30:+.1f}%偏离)")
        if zx_trend_detail:
            p(f"🔑 **趋势板块加持**：{'、'.join(zx_trend_detail[:5])} → 这些票即使偏离值不高，趋势板块内=机构/量化在加仓区间")

    # v9.1 P1: 三模式总结
    p("")
    p("### 📋 异动三模式总结\n")
    if zx_hide_count > 0:
        p(f"🚫 **躲异动**（{zx_hide_count}只）：偏离>100%→绝对不碰，反弹只卖不买；偏离值越低潜伏方向越有价值")
    if zx_rao_count > 0:
        p(f"🔄 **绕异动**（{zx_rao_count}只）：70-100%偏离→绕道同板块低位品种，不做追高")
    if zx_rush_count > 0:
        p(f"✍️ **冲异动/写检讨书**（{zx_rush_count}只）：50-70%偏离→趋势板块加持可参与，需深度回调确认+写明逻辑")
    if zx_hide_count == 0 and zx_rao_count == 0 and zx_rush_count == 0:
        p("⚪ 无极端异动票 → 正常市场状态")
    p("")

    # v8.10 P0-6: 异动后规律子模块
    # 许文杰核心：异动后的规律决定了"能不能抄"和"什么时候抄"
    # 1. 冲异动+趋势板块加持 → 回调到距高点-15%~-20%是买点
    # 2. 躲异动+无趋势板块 → 反弹即卖点，不抄底
    # 3. 绕异动(70-100%)+回调-10%以上 → 低位品种可关注
    if zx_30d_rows:
        p("### 🔬 异动后规律分析（许文杰框架）\n")
        
        # 规律1: 冲异动票的回调深度分布 → 判断是否到了买点区间
        rush_rows = [r for r in zx_30d_rows if len(r) >= 7 and r[2] > 50 and r[2] <= 100]

    # v9.7.2: 次日异动阈值
    p("### 🎯 次日异动阈值\n")
    p("| 个股 | 30日偏离 | 今日涨跌 | 到200%需涨 | 风险 |")
    p("|------|----------|----------|-----------|------|")
    for name, sector, dev30, pullback, yang, tday, w5, close, max_h in zx_30d_rows[:15]:
        if dev30 < 50:
            continue
        need_200 = deviation_threshold_tomorrow(dev30, 200)
        need_100 = deviation_threshold_tomorrow(dev30, 100)
        if need_200 and need_200 < 8:
            risk = "🔴 高危"
        elif need_200 and need_200 < 15:
            risk = "🟡 关注"
        else:
            risk = "⚪ 安全"
        threshold_str = f"+{need_200}%" if need_200 else "已触发"
        p(f"| **{name}** | {dev30:+.1f}% | {tday:+.1f}% | {threshold_str} | {risk} |")
    p("")
    if rush_rows:
        avg_pullback = sum(abs(r[3]) for r in rush_rows) / len(rush_rows)
        deep_pullback = sum(1 for r in rush_rows if abs(r[3]) >= 15)
        rush_in_trend = [r for r in rush_rows if r[1] in zx_trend_sectors]
        p(f"✍️ **冲异动（50-100%）** {len(rush_rows)}只：")
        p(f"  - 平均回调距高点 {avg_pullback:.1f}%，其中{deep_pullback}/{len(rush_rows)}只回调≥15%（进入买点区间）")
        if rush_in_trend:
            names = [f"{r[0]}({r[1]})" for r in rush_in_trend]
            p(f"  - 趋势板块加持：{'、'.join(names[:4])} → 回调-15%~-20%可低吸")
        if deep_pullback < len(rush_rows):
            not_deep = [r for r in rush_rows if abs(r[3]) < 15]
            names_str = '、'.join(f"{r[0]}(回调{abs(r[3]):.0f}%)" for r in not_deep[:3])
            p(f"  - ⚠️ 回调不足：{names_str} → 等更深回调，先不碰")
        p("")
        
        # 规律2: 躲异动票 → 反弹卖出规律
        hide_rows = [r for r in zx_30d_rows if len(r) >= 7 and r[2] > 100]
        if hide_rows:
            p(f"🚫 **躲异动（>100%）** {len(hide_rows)}只 → 反弹只卖不买")
            avg_dev = sum(r[2] for r in hide_rows) / len(hide_rows)
            # 找最近一周反弹过的
            bounce_back = [r for r in hide_rows if len(r) >= 7 and r[6] and r[6] > 5]
            if bounce_back:
                names_str = '、'.join(f"{r[0]}(5日+{r[6]:.0f}%)" for r in bounce_back[:3])
                p(f"  - 📈 近期已反弹：{names_str} → 这是最后卖出窗口")
            still_dropping = [r for r in hide_rows if len(r) >= 7 and r[6] and r[6] < -5]
            if still_dropping:
                names_str = '、'.join(f"{r[0]}(5日{r[6]:+.0f}%)" for r in still_dropping[:3])
                p(f"  - 📉 仍在下跌：{names_str} → 不抄底，等缩量止跌信号")
            p(f"  > ⚠️ 规律：偏离>100%的票反弹10-15%后大概率二次下杀，反弹即卖点\n")
        
        # 规律3: 绕异动票 → 低位替代品种
        rao_rows = [r for r in zx_30d_rows if len(r) >= 7 and r[2] > 70 and r[2] <= 100]
        if rao_rows:
            p(f"🔄 **绕异动（70-100%）** {len(rao_rows)}只 → 不追高，找同板块低位")
            # 收集绕异动票所在板块的低位品种线索
            rao_sectors = set(r[1] for r in rao_rows if r[1] != "黄牌")
            if rao_sectors:
                p(f"  - 涉及板块：{'、'.join(sorted(rao_sectors)[:4])}")
                p(f"  - 操作：在这些板块内找涨幅还在15-30%的票 → 绕道替代高位票")
            p("")
        
        # 规律4: 异动板块集中度 → 判断板块热度是否过度集中
        from collections import Counter
        zx_sector_counts = Counter(r[1] for r in zx_30d_rows if r[1] and r[1] != "黄牌")
        if zx_sector_counts:
            top_sector, top_cnt = zx_sector_counts.most_common(1)[0]
            if top_cnt >= 3:
                p(f"🔑 **异动板块集中度**：{top_sector}集中了{top_cnt}只异动票（{top_cnt/len(zx_30d_rows)*100:.0f}%）")
                p(f"  > {'⚠️ 该板块过度集中→板块内分化风险高' if top_cnt >= 5 else '→ 该板块是当前主线，关注内部轮动'}")
            p("")

    # ============================================================
    # STEP 6: 仓位建议 + 操作策略
    # ============================================================
    p("## 七、仓位与策略\n")

    # 仓位建议
    # 黄牌结构性判断：>50%的黄牌大票归属光通信小票→结构性，不降仓
    yellow_structural = False
    if yellow_big_names:
        small_light_count = sum(1 for name in yellow_big_names 
                               if any(code for code, n in BIG_CAPS.items() 
                                       if n == name and BIG_CAP_SECTOR.get(code) == "光通信小票"))
        yellow_structural = (small_light_count / len(yellow_big_names)) > 0.5
    
    pos_str, pos_emoji = position_advice(today_amt_tri, rhythm_days, len(all_yellow_tickets), big_pos_ratio, yellow_structural, cycle, zx_rush_count if 'zx_rush_count' in dir() else 0, core_mild)
    p(f"### 💰 仓位建议：{pos_emoji} **{pos_str}**\n")
    # v8.9: 仓位逻辑说明 → 拆解各因素影响
    logic_parts = [f"量能{today_amt_tri:.1f}万亿", f"大票{'偏强' if big_pos_ratio>=0.6 else '中性'}", f"黄牌{len(all_yellow_tickets)}只"]
    if 'zx_rush_count' in dir() and zx_rush_count >= 3:
        logic_parts.append(f"冲异动{zx_rush_count}只🛑")
    if "类冰点" in cycle or "正常调整" in cycle:
        logic_parts.append("大势→类冰点")
    if "退潮" in cycle:
        logic_parts.append("大势→退潮⚠️")
    p(f"> 逻辑：{' × '.join(logic_parts)}\n")

    # === v9.7: 分段仓位执行 ===
    # 类冰点/止涨→不开新仓，不暴露分段买入
    is_no_new_position = any(block in cycle for block in ("类冰点", "止涨", "冰点"))
    if is_no_new_position:
        p("### 📊 仓位策略（⚠️ 当前周期禁止新开仓）\n")
        p(f"> 当前周期={cycle} → **不开新仓，只做已持品种的做T+了结**\n")
    else:
        p("### 📊 分段执行建议\n")
        # 解析仓位为数字
        pos_nums = [int(c) for c in pos_str if c.isdigit()]
        if pos_nums:
            max_pos = max(pos_nums)  # 取上限
            t1 = round(max_pos * 0.4, 1)
            t2 = round(max_pos * 0.6, 1)
            p(f"- **第一笔**（尾盘/开盘）：**{t1}成** → 挑调满窗口板块的龙一/龙二，价格在5日线附近")
            p(f"- **第二笔**（2点后确认）：**{t2}成** → 条件：黄牌<3只+量能稳定→加仓; 否则→等明天")
            p(f"- **留一手**：永远留至少1成现金→应对明天开盘变局")
    p("")

    # 优先方向（v9.7: 类冰点/止涨→禁止买入建议）
    if is_no_new_position:
        p("### 🎯 优先方向（⚠️ 只观察不动手）\n")
        p(f"> 当前周期={cycle} → **不开新仓，只做已持品种的做T+了结**\n")
        p("> 以下板块即使到达买点也只看不买：\n")
        p("")
    else:
        p("### 🎯 优先方向（调满窗口，可动手）\n")
    ready_sectors = [(s, d) for s, d in sector_days_adjusting.items() if d >= sector_rhythm.get(s, rhythm_days)]
    near_sectors = [(s, d) for s, d in sector_days_adjusting.items() if d >= sector_rhythm.get(s, rhythm_days) - 1 and d < sector_rhythm.get(s, rhythm_days)]

    if ready_sectors:
        if is_no_new_position:
            p("##### 📝 仅记录（不动手）\n")
            p("> ⚠️ 当前禁止新开仓，以下为观察清单备忘：\n")
        else:
            p("##### ✅ 可动手方向\n")
        conflict_sectors = []  # v8.10: 规则vsLLM冲突→单独分组
        for s, d in sorted(ready_sectors, key=lambda x: -x[1]):
            zts = zt_by_sector.get(s, [])
            llm = llm_result.get(s, {}) if llm_result else {}
            sub = llm.get("子赛道", "")
            cat = llm.get("分类", "")
            advice = llm.get("明日建议", "")

            # === v8.10: 规则vsLLM冲突调和 ===
            consistency_flag = ""
            is_conflict = False
            if cat in ("退潮放弃",):
                consistency_flag = " ⚠️[LLM退潮→不碰，跳过]"
                is_conflict = True
            elif "二三" in cat:
                consistency_flag = " ⚠️[LLM:二三观察→等确认信号，明天别急动手]"
                is_conflict = True
            elif cat in ("轮动观察",):
                consistency_flag = " ⚠️[LLM:轮动观察→等D2确认，明天别急动手]"
                is_conflict = True
            elif "低吸" in advice and d < sector_rhythm.get(s, rhythm_days):
                consistency_flag = " ⚠️[调不够但LLM可低吸→等信号确认]"
            elif "主升" not in cat:
                consistency_flag = f" ⚠️[LLM:{cat}→建议看低一线]"

            if zts:
                leaders = ", ".join(n for _, n, _ in sorted(zts, key=lambda x:-x[2])[:3])
                if is_no_new_position:
                    line = f"- **{s}** {f'({sub})' if sub else ''}(调{d}天≥{sector_rhythm.get(s, rhythm_days)}天) → 📝 **{leaders}**（仅记录不动手）{consistency_flag}"
                else:
                    line = f"- **{s}** {f'({sub})' if sub else ''}(调{d}天≥{sector_rhythm.get(s, rhythm_days)}天) → 找**{leaders}**等抗跌票尾盘低吸{consistency_flag}"
                if is_conflict:
                    conflict_sectors.append(line)
                else:
                    p(line)

        if conflict_sectors:
            p("\n##### ⚠️ 规则vsLLM冲突（需等确认）\n")
            p("> 以下板块节奏上可动手，但LLM定性偏保守→**明天竞价/量能确认后再决定**\n")
            for line in conflict_sectors:
                p(line)
    else:
        p("*无调满窗口的板块 → 今天不急着动手*\n")

    if near_sectors:
        p(f"\n**⏳ 接近窗口**：")
        for s, d in near_sectors:
            p(f"- **{s}** (还需{max(0.5, sector_rhythm.get(s, rhythm_days)-d):.1f}天) → 尾盘或明早再看")

    # 三情景预案
    p("\n### 📋 三情景预案\n")
    p("| 情景 | 条件 | 动作 |")
    p("|------|------|------|")
    p(f"| 🟢 乐观 | 量能回升3万亿+，大票正反馈 | 调满窗口板块积极低吸，{pos_str}上限 |")
    p(f"| 🟡 中性 | 量能平稳，黄牌未扩散 | 等满{rhythm_days}天再动手 |")
    p(f"| 🔴 悲观 | 量能<2.8万亿，黄牌≥3只 | 减仓防守，最后一笔不做 |")
    # v8.9: 预案思维 — 许文杰独有框架：如果A则X，如果B则Y，不做任何幻觉判断
    p("")
    p("**📐 预案思维（如果A则X，如果B则Y）**")
    # 基于周期状态生成两条核心预案
    if "冰点" in cycle or "退潮" in cycle:
        p("1. 如果**明天竞价强势+大票正反馈**（≥70%上涨）→ 退潮假象已化解，试探性开仓3-4成")
        p("2. 如果**明天继续低开低走+黄牌扩大**→ 彻底防守，不做任何操作")
    elif "正常调整" in cycle or "类冰点" in cycle:
        p("1. 如果**明天量能回升2.8万亿+**→ 调整到位，调满窗口板块可低吸")
        p("2. 如果**明天黄牌扩大→3+只**→ 调整升级，等进一步确认")
    else:
        p("1. 如果**早盘黄牌修复+量能回升**→ 按调满节奏分批低吸")
        p("2. 如果**黄牌扩散→5+只**→ 暂停低吸，转为观察")
    p("")

    # v8.9: 大小联动提示 — 大光vs小光拔河
    if "光通信" in str(strong_sectors) or "光通信" in str(llm_result):
        p("**🔗 大小联动盯盘**：大光（中际旭创/天孚/新易盛）vs小光（太辰光/菲尼萨等）→ 如果大光连续3天优于小光，聚焦大票。小光未跌破10日线=板块健康的必要条件。")
        p("")

    # 战略层
    p("\n### 🧭 战略层\n")
    base_tone = '进攻' if today_amt_tri>=3.0 and big_pos_ratio>=0.7 else '防守' if today_amt_tri<2.8 else '中性观察'
    # v8.10: 策略矛盾调和 — 进攻基调+矛盾→下调
    if base_tone == '进攻' and r7_issues:
        tone = f"进攻（⚠️警惕：{'/'.join(r7_issues[:2])} → 仓位上限下调）"
    else:
        tone = base_tone
    p(f"| 基调 | {tone} |")
    p(f"|------|------|")
    # v8.8.3: 节奏分层显示 — 强区1天 / 轮动{rhythm_days}天
    str_rhythm = f"强区1天 / 轮动**{rhythm_days}天**窗口"
    p(f"| 节奏 | {str_rhythm} |")
    p(f"| 仓位 | {pos_emoji} {pos_str} |")

    # 明日关键观察
    p(f"\n### ⚠️ 明日关键观察\n")
    p(f"- **量能**：{' → '.join(f'{v:.2f}' for v in vol_4d)}万亿 → 明天能否守住{today_amt_tri:.1f}万亿？")
    p(f"- **大票竞价**：寒武纪/新易盛/中际旭创 竞价反馈")
    if yellow_big_names:
        p(f"- **黄牌修复**：{'/'.join(yellow_big_names[:3])} 明天能否修复？")
    p(f"- **窗口到了吗**：调满{rhythm_days}天的板块→尾盘找买点 | 不够的→继续等")
    p(f"- **连板生态**：{max_streak}板竞价反馈→情绪方向")
    if weave_signals:
        ws = "; ".join(f"{n}{'白' if t=='白' else '黑'}织带" for n, t in weave_signals)
        p(f"- **织带跟踪**：{ws} → 当天已确认，明日验证延续性")

    elapsed = time.time() - t0
    p(f"\n---\n⏱ 复盘耗时: {elapsed:.1f}s | {total}只有效股票 | 涨停{zt_count} 跌停{dt_count} | v9.7.2\n")

    result = "\n".join(output)
    print(result)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, 'w') as f:
        f.write(result)
    print(f"\n📁 报告已保存: {OUTPUT}")

    conn.close()

if __name__ == "__main__":
    main()