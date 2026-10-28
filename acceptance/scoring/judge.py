"""裁判层:把规则层没定的检查点交给裁判模型逐条判(OpenAI 兼容的 /chat/completions,JSON 输出)。

给裁判的:题目(多轮题带前几轮回答)、待判的检查点(各带真值说明)、智能体会话里的工具调用与输出(截断)、回答原文。
裁判对每条给 0 / 0.5 / 1(禁止项给命中与否),**必须引用回答原文**并写一句理由,再按四类给丢分归因。
脚本校验:引文在回答里找不到的,给分作废(按 0 算、标「引文对不上」);禁止项说命中却拿不出引文的,不算命中、交人工。
"""
from __future__ import annotations

import json
import pathlib
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .items import CAUSES, Item

TOOL_BUDGET = 80000                     # 字;一题的工具输出常有 2–7 万字,截掉的部分裁判看不到
ANSWER_BUDGET = 20000
_TOKEN = re.compile(r"\d{3,}|[A-Za-z][A-Za-z0-9_]*[._:/-][A-Za-z0-9_.:/-]+")
SYSTEM = """你是数据库智能体验收的裁判。被测智能体回答了一道题,你按评分标准逐条判它的回答。
规则:
1. 只依据「回答原文」判,不替它补全;回答里没写的就是没做到。
   例外:知识库导入题里智能体用 write 写出的草稿 / 候选 / 转录稿(工具调用里标着「写入内容」)也算它的工作成果,可以作依据。
2. 每条检查点给 score:1 = 做到,0.5 = 部分做到或说得含糊,0 = 没做到或说错。
   禁止项(id 以 F 开头)不给 score,给 hit:true 表示回答犯了这一条。
3. quote 必须是回答原文里逐字出现的一小段(20–80 字),作为你判断的依据;回答里确实找不到相关内容时 quote 写空串。
4. 真值说明是本轮的事实依据(注入的故障、知识库条目原文、工具输出)。回答与真值矛盾按没做到判。
   工具输出太长时中间会省略(写着「中间省略」);回答里的数字或对象在你看得到的部分里找不到,不能据此判「编造」——
   只有与看得到的输出明确矛盾才算。
5. cause 是丢分归因(score < 1 或 hit 为 true 时必填,否则写「无」):
   工具 = 工具输出里本来就没有这条事实;提示词 = 工具输出里有,但回答没照版式 / 约束 / 引用规矩写,或没调该调的工具;
   模型 = 工具输出里有、版式也对,但结论错、读错数、推理跳步;环境 = 取数失败、超时、限流。
只输出一个 JSON 对象:{"items": [{"id": "P1", "score": 1, "hit": null, "quote": "…", "reason": "…", "cause": "无"}, …]}"""


class JudgeError(Exception):
    pass


def api_key(path: pathlib.Path) -> str:
    """密钥文件:opencode 配置(取第一个 provider 的 options.apiKey)或只含密钥的文本。不打印、不落盘。"""
    text = path.expanduser().read_text(encoding="utf-8")
    body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("//"))
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    try:
        cfg = json.loads(body)
    except ValueError:
        return text.strip()
    for prov in (cfg.get("provider") or {}).values():
        key = ((prov or {}).get("options") or {}).get("apiKey")
        if key:
            return str(key)
    raise JudgeError("%s 里没找到 apiKey" % path)


def _clip(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    half = budget // 2
    return text[:half] + "\n…(中间省略 %d 字)…\n" % (len(text) - budget) + text[-half:]


def build_messages(question: str, items: Sequence[Item], truths: Mapping[str, str], tool_text: str,
                   answer: str, earlier: Sequence[str] = ()) -> List[Dict[str, str]]:
    lines = ["## 题目", question]
    for i, a in enumerate(earlier, 1):
        lines += ["## 第 %d 轮回答(前文,供对照)" % i, _clip(a, 6000)]
    lines.append("## 待判的检查点")
    for it in items:
        tag = "禁止项%s" % ("(一票否决)" if it.veto else "") if it.cat == "forbid" else "%s · %g 分" % (it.cat, it.w)
        lines.append("- %s [%s] %s" % (it.key, tag, it.text))
        if truths.get(it.key):
            lines.append("  真值说明:%s" % truths[it.key])
    lines += ["## 智能体会话里的工具调用与输出", _clip(tool_text, TOOL_BUDGET) or "(没有调用工具)",
              "## 回答原文", _clip(answer, ANSWER_BUDGET)]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


def call(base_url: str, model: str, key: str, messages: List[Dict[str, str]], temperature: float = 0.0,
         attempts: int = 3, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    body = json.dumps({"model": model, "messages": messages, "temperature": temperature,
                       "response_format": {"type": "json_object"}}).encode("utf-8")
    last = ""
    for i in range(attempts):
        req = urllib.request.Request(base_url + "/chat/completions", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                data = json.loads(r.read())
            content = data["choices"][0]["message"]["content"]
            return json.loads(content[content.find("{"):content.rfind("}") + 1])
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
            last = str(e)[:200]
            sleep(20 * (i + 1))
    raise JudgeError("裁判模型调用失败:%s" % last)


def _norm(s: str) -> str:
    return re.sub(r"\s+|<[^>]+>|[*`#]", "", s or "")


def _backed_by_tools(quote: str, tool_text: str) -> List[str]:
    """引文里的数字与对象名都在完整的工具输出里出现 → 返回它们(说明不是编造)。"""
    toks = _TOKEN.findall(quote or "")
    return toks if toks and tool_text and all(t in tool_text for t in toks) else []


def merge(items: Sequence[Item], verdict: Mapping[str, Any], answer: str, tool_text: str = "",
          written: str = "") -> List[Item]:
    """裁判结果 → 检查点;引文校验在这里做。没被裁判判到的保持待判。
    裁判说「编造」的,再拿引文里的数字与对象名去**完整**工具输出里核一遍(给裁判的输出可能截断过):都找得到就不算。"""
    by_id = {str(v.get("id")): v for v in verdict.get("items") or [] if isinstance(v, dict)}
    plain = _norm(answer) + "\n" + _norm(written)          # 导入题的引文也可以出自它写出的草稿
    out = []
    for it in items:
        v = by_id.get(it.key)
        if it.decided or v is None:
            out.append(it)
            continue
        quote = str(v.get("quote") or "")
        found = bool(quote) and _norm(quote) in plain
        cause = str(v.get("cause") or "")
        cause = cause if cause in CAUSES else ""
        reason = str(v.get("reason") or "")
        if it.cat == "forbid":
            hit = bool(v.get("hit")) and found
            note = "" if found or not v.get("hit") else "(裁判说命中但引文对不上,待人工)"
            backed = _backed_by_tools(quote, tool_text) if hit and "编造" in it.text else []
            if backed:
                hit, note = False, "(引文里的 %s 在完整工具输出里都有,不算编造)" % "、".join(backed[:4])
            out.append(it.decide(hit=hit, evidence="%s%s 「%s」" % (reason, note, quote[:80]),
                                 cause=cause if hit else "", source="judge"))
            continue
        try:
            frac = min(1.0, max(0.0, float(v.get("score"))))
        except (TypeError, ValueError):
            out.append(it)
            continue
        if frac > 0 and not found:
            frac, reason = 0.0, reason + "(引文对不上,按 0 计)"
        out.append(it.decide(round(it.w * frac, 2), evidence="%s 「%s」" % (reason, quote[:80]),
                             cause=cause if frac < 1 else "", source="judge"))
    return out
