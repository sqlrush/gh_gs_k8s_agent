"""大盘的时间段筛选(frontend/lib/range.js,方案 B1):在 Node 里真跑一遍纯函数。

两个口径最容易错,都钉在这里:
① 存档时间戳是 UTC(YYYYMMDDTHHMMSSZ),时间段按浏览器本地时间理解 —— 这里固定 TZ=Asia/Shanghai,
   07:23:21Z 就是本地 15:23:21。
② WDR 按**快照窗口**筛(与时间段有重叠即算),不按存档时间:15:16–15:26 的窗口 16:59 才分析,
   选 15:20–15:21 要能筛出来,选 16:50–17:00 不能。
没有 node 时跳过(交付环境的单测不依赖 node)。
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_RANGE = _ROOT / "frontend" / "lib" / "range.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")

_SCRIPT = r"""
const R = await import(process.argv[1]);
const H = { at: '20261016T072321Z', file: 'h.json' };
const W = { at: '20261016T085918Z', file: 'w.json', window: { begin_ts: '2026-10-16 15:16:42', end_ts: '2026-10-16 15:26:46' } };
const old = { at: '20261016T085918Z', file: 'old.json' };          // 没有 window 的老 WDR 存档
const r = (from, to) => ({ from, to });
console.log(JSON.stringify({
  h_in: R.inRange('health', H, r('2026-10-16T15:16', '2026-10-16T15:26')),
  h_out: R.inRange('health', H, r('2026-10-16T15:24', '2026-10-16T15:30')),
  h_to_minute_inclusive: R.inRange('health', H, r('', '2026-10-16T15:23')),
  w_overlap: R.inRange('wdr', W, r('2026-10-16T15:20', '2026-10-16T15:21')),
  w_not_by_archive_time: R.inRange('wdr', W, r('2026-10-16T16:50', '2026-10-16T17:00')),
  w_old_falls_back_to_at: R.inRange('wdr', old, r('2026-10-16T16:50', '2026-10-16T17:00')),
  none_keeps_all: R.filterIndex('health', [H, old], null).length,
  filtered: R.filterIndex('wdr', [W, old], r('2026-10-16T15:00', '2026-10-16T15:30')).map((e) => e.file),
  valid_empty: R.valid(r('', '')), valid_reversed: R.valid(r('2026-10-16T16:00', '2026-10-16T15:00')),
  valid_garbage: R.valid(r('昨天', '')), valid_one_side: R.valid(r('2026-10-16T15:00', '')),
  label: R.label(r('2026-10-16T15:16', '2026-10-16T15:26')), label_from: R.label(r('2026-10-16T15:16', '')),
}));
"""


def test_range_semantics_in_node():
    env = {**os.environ, "TZ": "Asia/Shanghai"}
    out = subprocess.run(["node", "--input-type=module", "-e", _SCRIPT, "file://%s" % _RANGE],
                         capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["h_in"] is True and got["h_out"] is False
    assert got["h_to_minute_inclusive"] is True, "「到 15:23」要包含 15:23:21 存的那份"
    assert got["w_overlap"] is True and got["w_not_by_archive_time"] is False
    assert got["w_old_falls_back_to_at"] is True
    assert got["none_keeps_all"] == 2 and got["filtered"] == ["w.json"]
    assert (got["valid_empty"], got["valid_reversed"], got["valid_garbage"], got["valid_one_side"]) == (False, False, False, True)
    assert got["label"] == "10-16 15:16 – 10-16 15:26" and got["label_from"] == "10-16 15:16 起"
