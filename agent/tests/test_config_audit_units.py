"""参数体检的判定（纯函数，不连库）。

**这组测试守的核心是「不编依据」。** 参数体检最容易变成一份看起来很专业、
实际全是凭空数字的「推荐值大全」—— 而这个工具只有一条只读连接，看不到 CPU 核数、
物理内存和磁盘类型。所以只判三类客观错误，下面有专门的测试钉住「不许出现推荐值」。

另外三条来自自测时真踩到的坑，每条都有对应测试：
  · `setting != reset_val` 判「改了没生效」—— og5 上 108 个参数都这样而且
    source 全是 default，规则前提就是错的，已删除；
  · `pg_settings.setting` 是**会话值**，而取数层会给自己的会话设
    `statement_timeout` —— 不区分就会把「全局没有语句超时」漏掉（工具观测到自己）；
  · `shared_buffers` 单位是 8kB，不归一就会把 4GB 读成 524288。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-config" / "scripts"

for _m in ("audit", "baseline", "render", "config"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import audit as A        # noqa: E402
import baseline as B     # noqa: E402
import render as R       # noqa: E402


def _row(name, setting, unit="", boot="", reset="", context="sighup",
         source="configuration file"):
    return {"name": name, "setting": setting, "unit": unit, "vartype": "string",
            "boot_val": boot, "reset_val": reset, "context": context,
            "source": source, "category": "", "short_desc": ""}


def _st(*rows):
    return B.to_settings(list(rows))


def _codes(st):
    return [f.code for f in A.audit(st)]


# ================================================================ 单位归一

@pytest.mark.parametrize("unit,raw,want", [
    ("8kB", "524288", 4 * 1024 ** 3),      # shared_buffers 实测值 = 4GB
    ("kB", "16384", 16 * 1024 ** 2),       # work_mem 实测值 = 16MB
    ("kB", "12582912", 12 * 1024 ** 3),    # max_process_memory 实测值 = 12GB
    ("ms", "30000", 30000),
    ("s", "900", 900_000),
    ("min", "5", 300_000),
    ("", "1000", 1000),
])
def test_values_are_normalized_by_their_unit(unit, raw, want):
    """**不归一就会把 4GB 读成 524288。** 这不是显示问题 ——
    内存超卖那条规则要拿它和 max_process_memory 比,单位错了结论就是错的。"""
    assert B.normalize(unit, raw) == want


@pytest.mark.parametrize("raw", ["", "  ", "on", "(disabled)", "ALL,on()", None])
def test_a_non_numeric_value_normalizes_to_none_not_zero(raw):
    """返回 0 会让「解析不出来」和「配置为 0（不限制）」混在一起 ——
    而那两件事的结论相反。"""
    assert B.normalize("ms", raw) is None


# ================================================================ 工具不许观测到自己

def test_a_value_our_own_session_set_is_not_judged_as_the_global_one():
    """**实测踩到的坑。** 取数层会给自己的会话设 `statement_timeout=30000`
    (保护超时),于是体检读到 30000,而全局配置其实是 0 ——
    「全局没有语句超时」这条就永远测不出来:工具观测到了自己。

    实测确认:SET 之后 `source` 变成 'session',而 `reset_val` 仍是全局值 0。
    这和 vacuum 的 oldest_xmin 脚本里 `pid <> pg_backend_pid()` 是同一类错。
    """
    st = _st(_row("statement_timeout", "30000", unit="ms", reset="0", source="session"))
    assert st["statement_timeout"].self_inflicted is True
    assert st["statement_timeout"].effective == "0"
    assert "CFG_NO_TIMEOUT" in _codes(st)


def test_a_globally_configured_timeout_is_not_flagged():
    st = _st(_row("statement_timeout", "30000", unit="ms", reset="30000",
                  source="configuration file"))
    assert "CFG_NO_TIMEOUT" not in _codes(st)


def test_reset_val_is_only_trusted_when_we_set_the_value_ourselves():
    """**og5 实测:`source='default'` 的参数里有 108 个 reset_val 没有有效值。**
    无条件信 reset_val 会引入一百多条噪音 —— 所以只在 source='session' 时用它。"""
    s = _st(_row("asp_sample_num", "100000", reset="0", source="default"))["asp_sample_num"]
    assert s.self_inflicted is False
    assert s.effective == "100000"          # 用会话值,不用那个没意义的 reset_val


def test_the_deleted_pending_apply_rule_stays_deleted():
    """这条规则的前提是错的(见 audit.py 顶部那段说明),**不许再出现**。

    第一版用 `setting != reset_val` 判「改了没生效」,报出 5 条全是假的;
    它还带了「最多 5 条」的上限,把 108 条噪音裁成 5 条、看着正好像真的。
    """
    st = _st(*[_row("p%d" % i, "1", reset="0", source="default") for i in range(20)])
    assert "CFG_PENDING_APPLY" not in _codes(st)
    assert A.audit(st) == []


# ================================================================ 危险配置

@pytest.mark.parametrize("name,bad", [
    ("fsync", "off"), ("full_page_writes", "off"),
    ("zero_damaged_pages", "on"), ("ignore_checksum_failure", "on"),
    ("autovacuum", "off"),
])
def test_data_losing_settings_are_critical(name, bad):
    """这几项与机器规格无关,开/关了就是错 —— 而且后果是**静默**丢数据。"""
    fs = A.audit(_st(_row(name, bad)))
    assert [f.code for f in fs] == ["CFG_UNSAFE"]
    assert fs[0].severity.name == "CRITICAL"


def test_logging_collector_off_is_a_warning_not_critical():
    """丢的是可观测性,不是数据 —— 严重度要分开。"""
    fs = A.audit(_st(_row("logging_collector", "off")))
    assert fs[0].severity.name == "WARN"


@pytest.mark.parametrize("name,good", [
    ("fsync", "on"), ("zero_damaged_pages", "off"), ("autovacuum", "on"),
    ("logging_collector", "on"),
])
def test_safe_settings_are_silent(name, good):
    assert A.audit(_st(_row(name, good))) == []


def test_a_missing_parameter_is_skipped_not_assumed():
    """这个实例上没有这个参数 → 不判。假设一个默认值去判,等于凭空断言。"""
    assert A.audit(_st(_row("unrelated", "x"))) == []


# ================================================================ 归档

def test_archive_on_with_no_target_is_critical():
    """og5 实测 archive_command 的值就是字符串 '(disabled)' —— 要当成「没配」。"""
    st = _st(_row("archive_mode", "on"), _row("archive_command", "(disabled)"),
             _row("archive_dest", ""))
    fs = [f for f in A.audit(st) if f.code == "CFG_ARCHIVE_NO_TARGET"]
    assert len(fs) == 1 and fs[0].severity.name == "CRITICAL"
    assert "填满磁盘" in fs[0].evidence


def test_archive_off_is_not_flagged():
    """归档没开不是错误 —— 很多测试/开发库本来就不需要归档。
    报它只会制造噪音,而噪音会让真的那条也被忽略。"""
    st = _st(_row("archive_mode", "off"), _row("archive_command", "(disabled)"),
             _row("archive_dest", ""))
    assert "CFG_ARCHIVE_NO_TARGET" not in _codes(st)


@pytest.mark.parametrize("cmd,dest", [("cp %p /arch/%f", ""), ("(disabled)", "/arch")])
def test_archive_with_either_target_configured_is_fine(cmd, dest):
    st = _st(_row("archive_mode", "on"), _row("archive_command", cmd),
             _row("archive_dest", dest))
    assert "CFG_ARCHIVE_NO_TARGET" not in _codes(st)


def test_the_archive_evidence_admits_what_it_cannot_check():
    """**这个内核没有 pg_stat_archiver**(实测),归档是否*正在*失败查不到。
    不写明这个边界,读的人会以为归档已经被完整体检过了。"""
    st = _st(_row("archive_mode", "on"), _row("archive_command", ""),
             _row("archive_dest", ""))
    ev = [f for f in A.audit(st) if f.code == "CFG_ARCHIVE_NO_TARGET"][0].evidence
    assert "pg_stat_archiver" in ev and "查不到" in ev


# ================================================================ 自相矛盾

def test_max_connections_above_the_thread_pool_is_a_contradiction():
    """og5 实测:max_connections=1000、池=800。这不是调优建议,是客观矛盾 ——
    多出来的 200 永远用不上,而按 1000 做容量规划的人会以为还有余量。"""
    st = _st(_row("enable_thread_pool", "on"),
             _row("thread_pool_attr", "800, 2, (nobind)"),
             _row("max_connections", "1000", context="postmaster"))
    fs = [f for f in A.audit(st) if f.code == "CFG_POOL_LT_CONNS"]
    assert len(fs) == 1
    assert "需重启实例" in fs[0].evidence


def test_no_contradiction_when_the_pool_is_off():
    st = _st(_row("enable_thread_pool", "off"),
             _row("thread_pool_attr", "800, 2"), _row("max_connections", "1000"))
    assert "CFG_POOL_LT_CONNS" not in _codes(st)


@pytest.mark.parametrize("attr", ["", "坏的", "(nobind)", "0, 2"])
def test_an_unparsable_pool_size_judges_nothing(attr):
    st = _st(_row("enable_thread_pool", "on"), _row("thread_pool_attr", attr),
             _row("max_connections", "1000"))
    assert "CFG_POOL_LT_CONNS" not in _codes(st)


# ================================================================ 内存超卖

def test_workmem_times_concurrency_over_the_instance_budget():
    """og5 实测:16MB × 800 = 12.5G > max_process_memory 12G。

    分母用 openGauss 自己的 max_process_memory(可读的事实),**不猜物理内存** ——
    猜一个看不到的数当分母,算出来的结论就是编的。
    """
    st = _st(_row("work_mem", "16384", unit="kB"),
             _row("max_process_memory", "12582912", unit="kB"),
             _row("enable_thread_pool", "on"),
             _row("thread_pool_attr", "800, 2, (nobind)"),
             _row("max_connections", "1000"))
    fs = [f for f in A.audit(st) if f.code == "CFG_WORKMEM_OVERCOMMIT"]
    assert len(fs) == 1
    assert "16.0M" in fs[0].value and "12.0G" in fs[0].value


def test_the_concurrency_limit_is_the_pool_when_the_pool_is_on():
    """开了池就该用池大小做乘数 —— 用 max_connections 会高估 25%,
    刚好可能把一个没越界的配置报成越界。"""
    st = _st(_row("work_mem", "16384", unit="kB"),
             _row("max_process_memory", "13631488", unit="kB"),   # 13G
             _row("enable_thread_pool", "on"),
             _row("thread_pool_attr", "800, 2"),
             _row("max_connections", "1000"))
    # 16M×800=12.5G < 13G → 不报;若错用 1000 则 15.6G > 13G 会误报
    assert "CFG_WORKMEM_OVERCOMMIT" not in _codes(st)


def test_no_overcommit_when_it_fits():
    st = _st(_row("work_mem", "4096", unit="kB"),
             _row("max_process_memory", "12582912", unit="kB"),
             _row("max_connections", "100"))
    assert "CFG_WORKMEM_OVERCOMMIT" not in _codes(st)


def test_a_missing_budget_means_no_judgement():
    """取不到 max_process_memory 就不判 —— 没有分母就没有结论。"""
    st = _st(_row("work_mem", "65536", unit="kB"), _row("max_connections", "1000"))
    assert "CFG_WORKMEM_OVERCOMMIT" not in _codes(st)


# ================================================================ 弱算法

@pytest.mark.parametrize("v,flagged", [("0", True), ("1", True), ("2", False), ("3", False)])
def test_weak_password_crypto(v, flagged):
    """og5 实测 password_encryption_type=1,建用户时内核自己就警告
    「contains MD5 ciphertext, which is not secure」。"""
    st = _st(_row("password_encryption_type", v))
    assert ("CFG_WEAK_PASSWORD_CRYPTO" in _codes(st)) is flagged


def test_the_crypto_fix_mentions_existing_passwords_do_not_upgrade():
    """改了参数不会把已有用户的口令重新加密 —— 不说清楚,改完以为就安全了。"""
    ev = [f for f in A.audit(_st(_row("password_encryption_type", "1")))][0].evidence
    assert "不会自动升级" in ev


# ================================================================ 规则互不短路

def test_rules_do_not_short_circuit_each_other():
    """一个实例可以同时犯好几条。用 elif 串起来会让排在后面的真实问题消失
    （vacuum/rules.py 已经踩过并写在注释里的坑）。"""
    st = _st(_row("fsync", "off"), _row("archive_mode", "on"),
             _row("archive_command", ""), _row("archive_dest", ""),
             _row("password_encryption_type", "1"),
             _row("statement_timeout", "0", unit="ms", source="default"))
    codes = set(_codes(st))
    assert {"CFG_UNSAFE", "CFG_ARCHIVE_NO_TARGET",
            "CFG_WEAK_PASSWORD_CRYPTO", "CFG_NO_TIMEOUT"} <= codes


# ================================================================ 呈现

def test_the_report_never_contains_a_made_up_recommended_value():
    """**本组最重要的一条。** 这个 skill 只有一条只读连接,看不到 CPU 核数、
    物理内存、磁盘类型 —— 任何「shared_buffers 建议设为 X」都是编的。
    """
    st = _st(_row("work_mem", "16384", unit="kB"),
             _row("max_process_memory", "12582912", unit="kB"),
             _row("max_connections", "1000"))
    findings = A.audit(st)
    assert findings, "这组配置本该有发现，否则这条测试什么都没验到"

    # 只检查**发现本身**的文字。抬头那段免责声明里会出现「不给「推荐值」」——
    # 那是否定句、正是要留着的（我在这条测试上连错三次：第一版直接
    # substring 查整篇，把否定句当成了违规）。
    body = "\n".join(f.metric + f.value + f.threshold + f.evidence for f in findings)
    for bad in ("建议设为", "推荐值", "建议调整为", "最佳实践值", "应设置为"):
        assert bad not in body, "发现里出现了凭空的推荐值措辞：%s" % bad
    # **区别在于参数有没有量纲。** 枚举/布尔型参数有客观正确答案，给具体值是对的
    # （`fsync=on`、`password_encryption_type=2`）；而 work_mem、shared_buffers
    # 这类有量纲的参数，"该设多少"取决于机器规格 —— 给数字就是编。
    # 所以只对后者断言「不许出现目标数值」。
    import re
    sized = "\n".join(f.metric + f.evidence for f in findings
                      if any(k in (f.metric + f.value) for k in
                             ("work_mem", "shared_buffers", "max_process_memory",
                              "max_connections", "thread_pool")))
    assert sized, "这组配置本该有有量纲参数的发现"
    assert not re.search(r"(调整|设置|改|降|提高)(到|为|成)\s*\d", sized), \
        "有量纲的参数不该给出目标数值：%s" % sized[:200]

    out = R.report(st, findings, [])
    assert "不给「推荐值」" in out, "免责声明必须在"


def test_no_findings_does_not_claim_the_parameters_are_tuned():
    """「没有发现」只说明没犯那三类客观错误,不说明参数适合这台机器。
    不写这句,用户会把它读成「参数已经调好了」。"""
    out = R.report(_st(_row("fsync", "on")), [], [])
    assert "不等于" in out and "性能参数" in out


def test_show_normalizes_the_value_so_it_cannot_be_misread():
    """`shared_buffers` 单位是 8kB、现值 524288 —— 不换算会被读成 512KB。
    本 skill 自己的文档里警告过这个误读,那就必须在输出里真的换算出来。"""
    st = _st(_row("shared_buffers", "524288", unit="8kB"))
    out = R.report(st, [], ["shared_buffers"])
    assert "4.0G" in out


@pytest.mark.parametrize("name", ["ssl_key_file", "my_password", "auth_token"])
def test_sensitive_parameters_are_redacted_even_when_asked_for(name):
    """--show 是用户点名要看的,但点名也不该把口令类取值打印进聊天记录和报告存档
    —— 两者都会被转发、被备份。"""
    st = _st(_row(name, "s3cr3t-value"))
    out = R.report(st, [], [name])
    assert "s3cr3t-value" not in out and "[已隐去]" in out


def test_a_parameter_that_does_not_exist_is_said_so():
    out = R.report(_st(_row("fsync", "on")), [], ["nosuchparam"])
    assert "没有这个参数" in out
