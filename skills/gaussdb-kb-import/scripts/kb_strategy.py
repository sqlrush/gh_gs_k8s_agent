"""kb.py 工单导入的转化策略:首次导入一类材料时问用户的 8 个问题,答案翻成给模型的抽取约束。

从 kb_cases.py 拆出(2026-11-02,文件逼近 800 行上限);kb_cases 仍以原名导出这些名字。
"""
from __future__ import annotations

import pathlib
from typing import Dict, List

import yaml


# 写图/写向量之前要定的转化策略——问的是"这批材料怎么变成向量和图",不是元数据缺省值。
# 答案写进 strategies/tickets.yaml(key: 答案原文),之后每次 propose 都会把它塞进工作单约束模型抽取。
STRATEGY_QUESTIONS = [
    {"key": "target", "q": "这批材料沉淀成什么?",
     "options": ["案例(现象→根因→处置,进向量 + 图)", "规范条款(进 rules/,只进向量)", "两者都有(报告里的规范引用也抽成条款)"],
     "default": "案例"},
    {"key": "chain", "q": "图里每单抽几条因果链?",
     "options": ["一单一条主链(现象→根因→处置)", "允许多条(次要因素也建 caused_by 边,置信度打折)"],
     "default": "一单一条主链"},
    {"key": "relations", "q": "除因果链外还抽哪些关系进图?(可多选)",
     "options": ["案例→涉及对象(表 / GUC / 等待事件 / 错误码)", "案例→引用条款", "对象→对象依赖(只在材料明确写了时)"],
     "default": "涉及对象 + 引用条款"},
    {"key": "signals", "q": "复发标志(各 skill 按发现匹配案例靠它)从哪取?",
     "options": ["材料里的告警代码 / 等待事件 / GUC 名(优先)", "现象描述里的关键短语", "两者都要"],
     "default": "两者都要"},
    {"key": "merge", "q": "现象 / 根因与库里已有节点同义时?",
     "options": ["合并到已有节点(canonical),让多个案例支持同一条链", "各自新建"],
     "default": "合并"},
    {"key": "vector", "q": "哪些小节进向量?",
     "options": ["现场 / 判断 / 处置 / 复发标志全部", "只现场 + 复发标志(入口更准,但处置文本不可语义召回)"],
     "default": "全部"},
    {"key": "confidence", "q": "置信度口径?",
     "options": ["结论强度 已确认=1.0 / 推测=0.6 / 待验证=0.3;用户在选择列表接受的边=1.0;模型自报封顶 0.9"],
     "default": "按此口径"},
    {"key": "defaults", "q": "缺省元数据(材料没写时)?",
     "options": ["engine=gaussdb · severity=S3 · 系统名原样 · 对象名保留 schema 前缀"],
     "default": "按此缺省"},
]


def load_strategy(kb: pathlib.Path) -> Dict[str, str]:
    path = kb / "strategies" / "tickets.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def strategy_rules(strategy: Dict[str, str]) -> List[str]:
    """把策略答案翻成给模型的抽取约束(字符串匹配,答案是原文)。"""
    rules: List[str] = []
    if "条款" in strategy.get("target", "") and "案例" not in strategy.get("target", ""):
        rules.append("这批材料只沉淀成规范条款:不写 case/edges,把每条可判定要求写成候选条款")
    if "多条" in strategy.get("chain", ""):
        rules.append("允许多条因果链:次要因素各建一条 caused_by 边,confidence 比主链低 0.2")
    else:
        rules.append("每单只抽一条主链:现象 —caused_by→ 根因 —handled_by→ 处置;次要因素写进 secondary_factors,不建边")
    rel = strategy.get("relations", "")
    if rel and "对象" not in rel:
        rules.append("不抽 涉及对象(involves)关系")
    if rel and "条款" not in rel:
        rules.append("不抽 引用条款(references)关系")
    if "依赖" in rel:
        rules.append("材料明确写了对象依赖(A 依赖 B / A 下游是 B)时抽 depends_on 边,没写不猜")
    sig = strategy.get("signals", "")
    if "告警" in sig and "短语" not in sig:
        rules.append("signals 只取材料里的告警代码 / 等待事件 / GUC 名,不写现象短语")
    elif "短语" in sig and "告警" not in sig:
        rules.append("signals 取现象描述里的关键短语")
    else:
        rules.append("signals 同时取告警代码 / 等待事件 / GUC 名 和 现象关键短语,告警代码写在前面")
    if "新建" in strategy.get("merge", ""):
        rules.append("现象 / 根因不与已有节点合并,各自新建(known_entities 只作参考)")
    else:
        rules.append("现象 / 根因与 known_entities 里的同义节点合并:直接用已有的名字")
    if "只现场" in strategy.get("vector", ""):
        rules.append("向量只索引 现场 与 复发标志 两节(judge/处置 仍写进案例文件)")
    return rules


def write_default_strategy(kb: pathlib.Path) -> pathlib.Path:
    """用户说「全按默认」时由脚本代写 strategies/tickets.yaml(key: 默认答案),模型不用手写 yaml。"""
    path = kb / "strategies" / "tickets.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {q["key"]: q["default"] for q in STRATEGY_QUESTIONS}
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def print_strategy_questions() -> None:
    print("首次导入工单:写图/写向量之前先和用户确认转化策略——策略未确认,不出工作单。逐题问,每题带选项与默认:")
    for i, q in enumerate(STRATEGY_QUESTIONS, 1):
        print(f"  {i}. [{q['key']}] {q['q']}")
        for opt in q["options"]:
            print(f"       - {opt}")
        print(f"       默认:{q['default']}")
