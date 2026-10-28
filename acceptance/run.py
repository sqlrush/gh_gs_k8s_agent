#!/usr/bin/env python3
"""智能体版本验收:跑题、评分、出报告(在能连测试环境的机器上跑;方案见 docs/智能体验收方案.md)。

    python3 acceptance/run.py check                        # 跑之前的自检(配置、入口、faultlab、知识库备份)
    python3 acceptance/run.py run [--smoke] [--only B2,F1] [--label v1.4.2]
                                                            # 一轮:换验收知识库 → 造故障 → 问题 → 还原 → 评分 → 报告
    python3 acceptance/run.py score <轮次> [--baseline <轮次>] [--rejudge]   # 重新评分(改了评分标准、加了人工改判)
    python3 acceptance/run.py restore                      # 中途中断时把共用知识库还原
    python3 acceptance/run.py list                         # 历次结果

配置在 ~/.acceptance/run.yaml(样例 acceptance/run.example.yaml),结果在 out_dir/<轮次>/,不进仓库。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import yaml  # noqa: E402

import check_bank  # noqa: E402
from runner import ask, config, fixtures, kbswap, lab, opencode  # noqa: E402
from scoring import judge as jdg  # noqa: E402
from scoring import report, score  # noqa: E402

LAB_PLACEHOLDERS = ("{start}", "{end}", "{snap_", "{sql_id:", "{pid:")
KB_SCRIPT = "/opt/agent/config/opencode/skills/gaussdb-kb-import/scripts/kb.py"


def log(msg: str) -> None:
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- 题目选择

def load_questions(cfg: config.Config, smoke: bool, only: Sequence[str]) -> List[Dict[str, Any]]:
    d = check_bank.BANK_DIR / cfg.bank
    questions, problems = check_bank.check_bank(d, check_bank.build_context())
    if problems:
        raise SystemExit("题库 %s 校验不过,先修:\n%s" % (cfg.bank, "\n".join("%s:%s" % (p.where, p.msg)
                                                                  for p in problems[:10])))
    if only:
        questions = [q for q in questions if q["id"] in set(only)]
    elif smoke:
        questions = [q for q in questions if q.get("smoke")]
    return questions


def needs_lab(q: Mapping[str, Any]) -> bool:
    text = json.dumps(q, ensure_ascii=False)
    return bool((q.get("needs") or {}).get("faultlab")) or q.get("phase") == "live" or \
        any(p in text for p in LAB_PLACEHOLDERS)


# ---------------------------------------------------------------- 环境

def kubectl(cfg: config.Config, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", "--context", cfg.kubectl_context, "-n", cfg.namespace, *args],
                          capture_output=True, text=True, timeout=timeout)


def env_info(cfg: config.Config) -> Dict[str, Any]:
    model = kubectl(cfg, "get", "cm", "agent-config", "-o", "jsonpath={.data.MODEL_ID}").stdout.strip()
    pods = kubectl(cfg, "get", "pods", "-o",
                   "jsonpath={range .items[*]}{.metadata.name}={.spec.containers[0].image}{'\\n'}{end}").stdout
    images = sorted({l.split("=", 1)[1] for l in pods.splitlines() if "=" in l and "gaussdb-agent" in l})
    return {"model": model, "images": images}


def start_admin_sso(cfg: config.Config, run_dir: pathlib.Path) -> subprocess.Popen:
    a = cfg.admin
    fh = (run_dir / "admin-sso.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(a["sso_sim"]), str(a["port"]), str(a["gateway_port"]), str(a["user"]),
                             str(a["trust_file"]), str(a["roles"])], stdout=fh, stderr=subprocess.STDOUT)
    time.sleep(2)
    if proc.poll() is not None:
        raise SystemExit("管理员的模拟 SSO 起不来,看 %s" % (run_dir / "admin-sso.log"))
    return proc


def kb_import_pod(cfg: config.Config) -> str:
    sel = "role=kb-import,user=%s" % cfg.admin["user"]
    for _ in range(60):
        p = kubectl(cfg, "get", "pods", "-l", sel, "-o",
                    "jsonpath={range .items[*]}{.metadata.name} {.status.containerStatuses[0].ready}{'\\n'}{end}")
        for line in p.stdout.splitlines():
            name, _, ready = line.partition(" ")
            if ready == "true":
                return name
        time.sleep(5)
    raise SystemExit("等不到 %s 的导入容器就绪" % cfg.admin["user"])


def swap_kb(cfg: config.Config, run_id: str, admin: opencode.Portal) -> None:
    kbswap.build_local(cfg.kb_local, cfg.kb_nas)
    kbswap.backup(cfg.kb_nas, cfg.kb_backup, run_id)
    log("知识库已备份到 %s" % cfg.kb_backup)
    kbswap.install(cfg.kb_nas, cfg.kb_local, cfg.kb_backup)
    admin.call("GET", "/session/status")                    # 让网关把导入容器建起来
    pod = kb_import_pod(cfg)
    out = kbswap.reindex(lambda argv: kubectl(cfg, "exec", pod, "-c", "opencode", "--", *argv), cfg.kb_pod, KB_SCRIPT)
    last = out.strip().splitlines()[-1][:120] if out.strip() else ""
    log("验收知识库已装上并建好索引 %s" % last)


# ---------------------------------------------------------------- 一轮

class Round:
    def __init__(self, cfg: config.Config, questions: List[Dict[str, Any]], run_id: str, use_lab: bool):
        self.cfg, self.questions, self.run_id = cfg, questions, run_id
        self.dir = cfg.out_dir / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.use_lab = use_lab
        self.sampler: Optional[lab.Sampler] = None
        self.labrun: Optional[lab.LabRun] = None
        self.sql: Optional[Callable[[str], str]] = None
        self.procs: List[subprocess.Popen] = []
        self.swapped = False
        self.facts: Dict[str, Any] = {"static": lab.STATIC, "old": fixtures.old_moment(cfg.old_days), "env": {}}
        self.c4 = HERE / "fixtures" / "C4-proc.sql"
        self.c4_made = False

    def values(self) -> Dict[str, str]:
        v = dict(self.cfg.names, old=self.facts["old"])
        for k in ("start", "end", "snap_begin", "snap_end", "clip_batch"):
            if self.facts.get(k):
                v[k] = str(self.facts[k])
        if self.sampler:
            for code in sorted(lab.FAULT_CODES):
                sid = self.sampler.sql_id(code)
                if sid:
                    v["sql_id:%s" % code] = sid
            for app in list(self.sampler.pids):
                v["pid:%s" % app.split(":", 1)[-1]] = self.sampler.pid(app)
        return v

    def ctx(self, admin: Optional[opencode.Portal]) -> ask.Ctx:
        return ask.Ctx(self.run_id, self.dir, self.values(), opencode.Portal(self.cfg.user_portal), admin,
                       self.cfg.question_timeout_s, self.cfg.faultlab_dir, self.dir / "generated", self.cfg.kb_nas,
                       self.cfg.kb_pod, fingerprint=lambda: kbswap.fingerprint(self.cfg.kb_nas), log=log)

    def ask_many(self, qs: List[Dict[str, Any]], admin: Optional[opencode.Portal], workers: int) -> None:
        if not qs:
            return
        log("问 %d 道:%s" % (len(qs), " ".join(q["id"] for q in qs)))
        c = self.ctx(admin)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda q: ask.ask(q, c), qs))

    def prepare(self, admin: Optional[opencode.Portal]) -> None:
        if any((q.get("attach") or {}).get("generated") for q in self.questions):
            fixtures.make_generated(self.dir / "generated", sys.executable)
        if self.cfg.kb_swap:
            swap_kb(self.cfg, self.run_id, admin)
            self.swapped = True
        if self.cfg.faultlab_dir.joinpath("faultlab.py").is_file():
            self.sql = lab.omm_runner(self.cfg.faultlab_dir)
            probe = lab.Sampler(self.sql)
            probe.sample_env()                                  # 版本、连接上限、保留期(G4、I1、B6 的真值)
            self.facts["env"].update(probe.env)
        if any(q["id"] == "C4" for q in self.questions):
            fixtures.db_fixture(self.c4, self.sql)
            self.c4_made = True

    def start_lab(self) -> bool:
        self.labrun = lab.LabRun(self.cfg.faultlab_dir, self.cfg.faultlab_python, self.dir / "faultlab.log")
        self.labrun.start()
        log("faultlab 已启动,等故障窗口开始")
        if not self.labrun.wait_window():
            log("faultlab 没进入故障窗口,看 %s" % (self.dir / "faultlab.log"))
            return False
        self.sampler = lab.Sampler(self.sql)
        self.sampler.start()
        return True

    def finish_lab(self) -> None:
        rc = self.labrun.wait_done() if self.labrun else -1
        if self.sampler:
            self.sampler.stop()
        tl = lab.summarize(lab.read_timeline())
        (self.dir / "timeline.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                                           for r in lab.read_timeline()), encoding="utf-8")
        if tl["start"]:
            self.facts.update(start=fixtures.minute(tl["start"]), end=fixtures.minute(tl["end"] or tl["start"], True))
        if tl["snapshots"]:
            self.facts.update(snap_begin=tl["snapshots"][0], snap_end=tl["snapshots"][-1])
        self.facts.update(probes=tl["probes"], failed_codes=[f.split(":")[0] for f in tl["failed"]],
                          faultlab_rc=rc)
        log("faultlab 结束(退出 %s):窗口 %s – %s,快照 %s" % (rc, self.facts.get("start"), self.facts.get("end"),
                                                       tl["snapshots"]))

    def run_k(self, ks: List[Dict[str, Any]], admin: opencode.Portal) -> None:
        if not ks:
            return
        c = self.ctx(admin)
        for q in ks:
            if (q.get("attach") or {}).get("mode") == "clip":
                try:
                    wiki = fixtures.start_wiki(self.cfg.mock_wiki_port, self.dir / "mock-wiki.log")
                    self.procs.append(wiki)
                    out = fixtures.upload_clip(self.cfg.admin_portal,
                                               fixtures.clip_payload("http://127.0.0.1:%d" % self.cfg.mock_wiki_port))
                    self.facts["clip_batch"] = out["batch"]
                    c = self.ctx(admin)
                    c.clip_sessions[q["id"]] = out["session"]
                except (fixtures.FixtureError, OSError) as e:
                    log("K2 剪藏准备失败:%s" % e)
            ask.ask(q, c)

    def skip(self, qs: List[Dict[str, Any]], why: str) -> None:
        for q in qs:
            qdir = self.dir / "questions" / q["id"]
            qdir.mkdir(parents=True, exist_ok=True)
            report.dump(qdir / "meta.json", {"id": q["id"], "status": "env_error", "error": why})
        if qs:
            log("%s:%s" % (why, " ".join(q["id"] for q in qs)))

    def skipped(self, q: Mapping[str, Any]) -> bool:
        return (self.dir / "questions" / q["id"] / "meta.json").is_file()

    def save_facts(self) -> None:
        f = dict(self.facts)
        if self.sampler:
            snap = self.sampler.snapshot()
            f["env"] = dict(f.get("env") or {}, **snap["env"])
            f["sampler"] = snap
            f["sql_ids"] = {c: self.sampler.sql_id(c) for c in sorted(lab.FAULT_CODES) if self.sampler.sql_id(c)}
            f["pids"] = {"K1": self.sampler.pid("fl:K1/root")}
        f["expected"] = lab.expected(self.cfg.faultlab_dir) if self.cfg.faultlab_dir.is_dir() else {}
        f["fixtures"] = yaml.safe_load((HERE / "fixtures" / "truth.yaml").read_text(encoding="utf-8"))
        report.dump(self.dir / "lab.json", f)

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        if self.labrun:
            self.labrun.stop()
        if self.c4_made and self.sql:
            try:
                fixtures.db_cleanup(self.c4, self.sql)
            except Exception as e:                              # noqa: BLE001 —— 收尾继续往下走
                log("删 C4 夹具失败:%s" % e)
        if self.swapped:
            log("还原知识库:%s" % kbswap.restore(self.cfg.kb_nas, self.cfg.kb_backup))


def cmd_run(cfg: config.Config, args: argparse.Namespace) -> int:
    questions = load_questions(cfg, args.smoke, [x for x in (args.only or "").split(",") if x])
    run_id = time.strftime("%Y%m%d-%H%M") + ("-" + args.label if args.label else "")
    use_lab = not args.no_faultlab and any(needs_lab(q) for q in questions)
    rd = Round(cfg, questions, run_id, use_lab)
    info = dict(run_id=run_id, bank=cfg.bank, started=time.strftime("%F %T"), questions=[q["id"] for q in questions],
                judge=cfg.judge.model if cfg.judge else "", faultlab=use_lab, names=dict(cfg.names), **env_info(cfg))
    report.dump(rd.dir / "run.json", info)
    log("第 %s 轮:%d 道题,%s造故障,结果在 %s" % (run_id, len(questions), "" if use_lab else "不", rd.dir))
    admin = None
    try:
        if cfg.kb_swap or any(q.get("env") == "kb-import" for q in questions):
            rd.procs.append(start_admin_sso(cfg, rd.dir))
            admin = opencode.Portal(cfg.admin_portal, kb_import=True)
        rd.prepare(admin)
        live = [q for q in questions if q.get("phase") == "live" and q.get("env") == "runtime"]
        later = [q for q in questions if q not in live and q.get("env") == "runtime"]
        ks = [q for q in questions if q.get("env") == "kb-import"]
        lab_ok = rd.start_lab() if use_lab else False
        if use_lab and not lab_ok:
            rd.skip([q for q in live + later if needs_lab(q)], "faultlab 没进入故障窗口,依赖故障的题没法问")
        elif not use_lab:
            rd.skip([q for q in live + later if needs_lab(q)], "这一轮没造故障(--no-faultlab)")
        live, later = [q for q in live if not rd.skipped(q)], [q for q in later if not rd.skipped(q)]
        if lab_ok:
            time.sleep(cfg.live_delay_s)
            rd.sampler.sample_env()
            rd.ask_many(live, admin, cfg.concurrency)
            rd.run_k(ks, admin)
            rd.finish_lab()
        else:
            rd.run_k(ks, admin)
        rd.ask_many(later, admin, cfg.concurrency)
    finally:
        rd.save_facts()
        rd.cleanup()
    if args.no_score:
        return 0
    return cmd_score(cfg, argparse.Namespace(run=run_id, baseline=args.baseline, rejudge=""))


# ---------------------------------------------------------------- 评分与报告

def _details(qdir: pathlib.Path) -> Dict[str, Any]:
    from runner import trajectory
    msgs = json.loads((qdir / "messages.json").read_text(encoding="utf-8")) if (qdir / "messages.json").is_file() else []
    tr = trajectory.parse(msgs)
    rd = lambda n: (qdir / n).read_text(encoding="utf-8") if (qdir / n).is_file() else ""  # noqa: E731
    return {"prompt": rd("prompt.txt"), "answer": rd("answer.md"),
            "tools": ["[%s] %s" % (t.tool, t.command[:300]) for t in tr.tools]}


def _baseline(cfg: config.Config, ref: Optional[str]) -> Optional[Dict[str, Any]]:
    if not ref:
        return None
    p = pathlib.Path(ref)
    p = p if p.suffix == ".json" else cfg.out_dir / ref / "score.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def cmd_score(cfg: config.Config, args: argparse.Namespace) -> int:
    rdir = cfg.out_dir / args.run
    run = json.loads((rdir / "run.json").read_text(encoding="utf-8"))
    bank = {q["id"]: q for q in check_bank.check_bank(check_bank.BANK_DIR / run["bank"], check_bank.build_context())[0]}
    facts = json.loads((rdir / "lab.json").read_text(encoding="utf-8")) if (rdir / "lab.json").is_file() else {}
    if not cfg.kb_local.is_dir():
        kbswap.build_local(cfg.kb_local, cfg.kb_nas)
    key = jdg.api_key(cfg.judge.api_key_from) if cfg.judge else ""
    manual = json.loads((rdir / "manual.json").read_text(encoding="utf-8")) if (rdir / "manual.json").is_file() else {}
    ids = [i for i in run["questions"] if i in bank]
    log("评分 %d 道(裁判 %s)" % (len(ids), cfg.judge.model if cfg.judge else "未配置"))

    redo = set(ids) if args.rejudge == "all" else set((args.rejudge or "").split(",")) - {""}

    def one(qid: str) -> Dict[str, Any]:
        return score.score_question(bank[qid], rdir / "questions" / qid, facts, cfg.kb_local, cfg.judge, key,
                                    manual, rejudge=qid in redo)
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.judge.concurrency if cfg.judge else 4) as ex:
        results = list(ex.map(one, ids))
    base = _baseline(cfg, args.baseline)
    agg = score.aggregate(results, base)
    report.dump(rdir / "score.json", {"run": run, "questions": results, **agg})
    html = report.render(run, results, agg, base, {i: _details(rdir / "questions" / i) for i in ids})
    (rdir / "report.html").write_text(html, encoding="utf-8")
    red = {None: "这轮没有红线题", True: "全过", False: "未过"}[agg["gates"]["redline"]]
    log("总分 %s;红线%s;报告 %s" % (agg["total"], red, rdir / "report.html"))
    return 0


# ---------------------------------------------------------------- 其它命令

def cmd_check(cfg: config.Config, args: argparse.Namespace) -> int:
    bad = 0

    def item(ok: bool, what: str, detail: str = "") -> None:
        nonlocal bad
        bad += 0 if ok else 1
        print("%s %s%s" % ("✓" if ok else "✗", what, ("  " + detail) if detail else ""))
    load_questions(cfg, False, [])
    item(True, "题库 %s 校验通过" % cfg.bank)
    try:
        opencode.Portal(cfg.user_portal, attempts=2).call("GET", "/session/status")
        item(True, "诊断环境入口 %s" % cfg.user_portal)
    except opencode.PortalError as e:
        item(False, "诊断环境入口 %s" % cfg.user_portal, str(e)[:120])
    left = kbswap.pending_backup(cfg.kb_backup)
    item(not left, "没有待还原的知识库备份", "上一轮 %s 没还原:先 run.py restore" % (left or {}).get("run") if left else "")
    item((cfg.kb_nas / "docsets").is_dir(), "共用知识库 %s" % cfg.kb_nas)
    item(cfg.faultlab_dir.joinpath("faultlab.py").is_file(), "faultlab %s" % cfg.faultlab_dir)
    item(cfg.admin["trust_file"].is_file(), "网关信任凭据文件")
    p = kubectl(cfg, "get", "pods", "-o", "name")
    item(p.returncode == 0, "kubectl 能访问 %s/%s" % (cfg.kubectl_context, cfg.namespace))
    if cfg.judge:
        try:
            item(bool(jdg.api_key(cfg.judge.api_key_from)), "裁判模型 %s 的密钥文件" % cfg.judge.model)
        except (OSError, jdg.JudgeError) as e:
            item(False, "裁判模型的密钥文件", str(e)[:120])
    else:
        item(True, "裁判模型未配置(只出规则层分数)")
    return 1 if bad else 0


def cmd_restore(cfg: config.Config, args: argparse.Namespace) -> int:
    print(kbswap.restore(cfg.kb_nas, cfg.kb_backup))
    return 0


def cmd_list(cfg: config.Config, args: argparse.Namespace) -> int:
    for d in sorted(cfg.out_dir.glob("*/run.json")):
        s = d.parent / "score.json"
        total = json.loads(s.read_text(encoding="utf-8")).get("total") if s.is_file() else "未评分"
        run = json.loads(d.read_text(encoding="utf-8"))
        print("%-28s %3d 题  总分 %-6s %s" % (d.parent.name, len(run.get("questions") or []), total,
                                         "; ".join(run.get("images") or [])[:80]))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="智能体版本验收:跑题、评分、出报告")
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    r = sub.add_parser("run")
    r.add_argument("--smoke", action="store_true", help="只跑冒烟子集(15 题)")
    r.add_argument("--only", help="只跑这几题,逗号分隔")
    r.add_argument("--label", default="", help="轮次名后缀,如 v1.4.2")
    r.add_argument("--baseline", help="对比的基线轮次")
    r.add_argument("--no-faultlab", action="store_true", help="不造故障(依赖故障的题记环境问题)")
    r.add_argument("--no-score", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("run")
    s.add_argument("--baseline")
    s.add_argument("--rejudge", nargs="?", const="all", default="",
                   help="不用存档的裁判结论、重新判:不带值 = 全部;或逗号分隔的题号")
    sub.add_parser("restore")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    try:
        cfg = config.load(pathlib.Path(args.config).expanduser())
    except config.ConfigError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 2
    return {"check": cmd_check, "run": cmd_run, "score": cmd_score, "restore": cmd_restore,
            "list": cmd_list}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
