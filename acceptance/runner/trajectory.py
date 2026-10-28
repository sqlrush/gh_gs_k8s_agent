"""把一个会话的全部消息拆成评分要的东西:每轮的回答正文、工具调用(命令与输出)、提问、token、耗时、模型报错。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ToolCall:
    tool: str             # bash / read / question / webfetch …
    command: str          # bash 的命令行;read 的文件路径;其它工具的输入摘要
    output: str
    status: str


@dataclass(frozen=True)
class Trajectory:
    answers: List[str]                       # 每一轮(每条用户消息之后)的最终回答正文
    tools: List[ToolCall]
    asked: List[str]                         # 模型用提问工具问用户的话(含选项)
    tokens: Dict[str, int] = field(default_factory=dict)
    model_s: float = 0.0
    tool_s: float = 0.0
    errors: List[str] = field(default_factory=list)

    @property
    def answer(self) -> str:
        """最后一轮的回答;模型最后是在提问,就把提问也算进回答(I2、导入题看的就是它会不会问)。"""
        text = self.answers[-1] if self.answers else ""
        if self.asked:
            text = (text + "\n\n" if text else "") + "\n".join("【向用户提问】" + a for a in self.asked)
        return text.strip()

    def written(self) -> str:
        """write 工具写出的内容(导入题的草稿、候选、转录稿)。"""
        return "\n\n".join(t.output for t in self.tools if t.tool == "write")

    def tool_text(self) -> str:
        return "\n\n".join("$ %s\n%s" % (t.command, t.output) for t in self.tools if t.output)


def _input_summary(tool: str, inp: Dict[str, Any]) -> str:
    if "command" in inp:
        return str(inp["command"])
    if "filePath" in inp:
        return str(inp["filePath"])
    if tool == "question":
        return "; ".join(_question_text(q) for q in inp.get("questions") or [])
    return " ".join("%s=%s" % (k, str(v)[:200]) for k, v in sorted(inp.items()))


def _question_text(q: Dict[str, Any]) -> str:
    opts = " / ".join(str(o.get("label") or "") for o in q.get("options") or [] if isinstance(o, dict))
    return "%s%s" % (q.get("question") or q.get("header") or "", ("(选项:%s)" % opts) if opts else "")


def _span(t: Optional[Dict[str, Any]], a: str, b: str) -> float:
    if t and t.get(a) and t.get(b):
        return max(0.0, (t[b] - t[a]) / 1000.0)
    return 0.0


def parse(messages: List[Dict[str, Any]], pending: Optional[List[Dict[str, Any]]] = None) -> Trajectory:
    answers: List[str] = []
    tools: List[ToolCall] = []
    asked: List[str] = []
    errors: List[str] = []
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
    model_s = tool_s = 0.0
    current: List[str] = []
    started = False
    for m in messages:
        info = m.get("info") or {}
        if info.get("role") == "user":
            if started:
                answers.append("\n".join(current).strip())
            current, started = [], True
            continue
        if info.get("role") != "assistant":
            continue
        if info.get("error"):
            err = info["error"]
            errors.append(str((err.get("data") or {}).get("message") or err.get("name") or err)[:300]
                          if isinstance(err, dict) else str(err)[:300])
        t = info.get("tokens") or {}
        for k in ("input", "output", "reasoning"):
            tokens[k] += int(t.get(k) or 0)
        tokens["cache_read"] += int((t.get("cache") or {}).get("read") or 0)
        model_s += _span(info.get("time"), "created", "completed")
        texts = []
        for p in m.get("parts") or []:
            if p.get("type") == "text" and (p.get("text") or "").strip():
                texts.append(p["text"].strip())
            elif p.get("type") == "tool":
                st = p.get("state") or {}
                inp = st.get("input") or {}
                tool_s += _span(st.get("time"), "start", "end")
                out = str(st.get("output") or "")
                if p.get("tool") == "write" and inp.get("content"):     # 写出的草稿 / 候选是导入题的工作成果
                    out = "(写入内容)\n%s\n%s" % (str(inp["content"])[:20000], out)
                tools.append(ToolCall(str(p.get("tool") or ""), _input_summary(str(p.get("tool")), inp),
                                      out, str(st.get("status") or "")))
        if texts:
            current = texts                    # 一轮里最后一条有正文的助手消息才是回答
    if started:
        answers.append("\n".join(current).strip())
    for q in pending or []:
        asked.extend(_question_text(x) for x in q.get("questions") or [])
    return Trajectory(answers, tools, asked, tokens, round(model_s - tool_s, 1), round(tool_s, 1), errors)
