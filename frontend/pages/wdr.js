// WDR 分析:取数与建模。数据:/reports/wdr/<key>/latest.json(或指定的一份)+ index.json;
// 上一窗口 = index 里的前一份,有则 KPI 带对比,没有就写「没有可对比的窗口」,不造假。
// 七个维度名与列名取自 gaussdb-wdr 的 model.py / collectors.py(契约守卫逐个核对,见下方 DIMS 与 KPIS)。

import { fetchLatest, fetchIndex, fetchReport, fetchFirst, chainFrom } from '../lib/data.js';
import { fmtInt, num, split } from '../lib/fmt.js';
import { instName } from '../lib/ui.js';
import { loadInRange } from '../lib/range.js';
import { DEADLOCK_CODES, deadlockPrompt } from '../lib/prompts.js';

// 维度名 = gaussdb-wdr/scripts/model.py 的 DIM_* 常量。
// **第一版对着自编的样例写,维度名和行列形状都与真技能不符,真数据上 KPI 全是「未采集」。**
export const DIMS = {
  loadprofile: 'Load Profile', dbstat: 'Database Stat', topsql: 'Top SQL', waits: 'Wait Events / Classes',
  checkpoint: 'Checkpoint / BgWriter / Redo', cache: 'Cache / Memory', fileio: 'File IO',
};
export const WT = { 'Load Profile': '负载概况', 'Database Stat': '数据库统计', 'Top SQL': '窗口 Top SQL', 'Wait Events / Classes': '等待事件',
  'Checkpoint / BgWriter / Redo': '检查点与 Redo', 'Cache / Memory': '缓存与物理读对象', 'File IO': '文件 IO' };
export const dimOf = (d, k) => ((d && d.dims) || []).find((x) => x.dimension === DIMS[k]);
const ratio = (a, secs) => (a === null || !secs ? null : a / secs);

/** 单行维度里按列名(前缀)取值。 */
export function val(d, k, colName) {
  const dim = dimOf(d, k);
  if (!dim || !dim.rows || !dim.rows.length) return null;
  const i = (dim.headers || []).findIndex((h) => String(h).startsWith(colName));
  return i < 0 ? null : num(dim.rows[0][i]);
}
// Load Profile 与 Database Stat 各是一行、按列存值;列名是 collectors.py 里的 headers(前缀匹配,容忍单位后缀)。
// KPI 只放报告里真有来源的:没有来源的宁可不放,也不画一格「未采集」充数。
export const KPIS = [
  { l: 'DB Time', dim: 'loadprofile', col: 'DB time', u: 's', worse: 'up' },
  { l: '平均活跃会话', calc: (d, secs) => ratio(val(d, 'loadprofile', 'DB time'), secs), u: '', worse: 'up' },
  { l: '事务 TPS', calc: (d, secs) => ratio(val(d, 'loadprofile', 'commits'), secs), u: '/s', worse: null },
  { l: 'CPU 占 DB Time', dim: 'loadprofile', col: 'CPU占DBtime', u: '%', worse: null },
  { l: '缓存命中率', dim: 'dbstat', col: 'cache_hit', u: '%', worse: 'down' },
  { l: '物理读', dim: 'loadprofile', col: '物理读', u: '块', worse: 'up' },
  { l: '临时溢出', dim: 'dbstat', col: '临时溢出', u: 'MiB', worse: 'up' },
  { l: '回滚率', dim: 'dbstat', col: '回滚率', u: '%', worse: 'up' },
];
export const kpiVal = (k, d) => {
  if (!d) return null;
  const secs = (Number(d.window && d.window.duration_min) || 0) * 60;
  return k.calc ? k.calc(d, secs) : val(d, k.dim, k.col);
};
export const cpuShare = (d) => val(d, 'loadprofile', 'CPU占DBtime');
/** DB Time 秒数:过百取整加千分位,小值留小数(近乎空闲的窗口只有零点几秒,取整成 0 就看不出差别)。 */
export const fmtDbt = (v) => (v === null || v === undefined ? '—' : Math.abs(v) >= 100 ? fmtInt(Math.round(v)) : String(+v.toFixed(Math.abs(v) < 1 ? 2 : 1)));
const fmtK = (v) => (Math.abs(v) >= 1e4 ? fmtInt(v) : Math.abs(v) >= 100 ? fmtInt(Math.round(v)) : String(+v.toFixed(v < 1 ? 3 : 1)));
/** 数值 + 单位:MiB 上千就换成 GiB(19763 MiB 显示成「2.0 万 MiB」没人读得懂),万 / 亿进单位。 */
export function showK(v, u) {
  const [x, xu] = u === 'MiB' && Math.abs(v) >= 1024 ? [fmtK(v / 1024), 'GiB'] : [fmtK(v), u];
  const [n, u2] = split(x);
  return [n, [u2, xu].filter(Boolean).join(' ')];
}
/** 与上一窗口比:倍数 ≥1.5 写 ×N,百分比写差值 pt;上一窗口是 0 时不算百分比(除以 0 会得出「▲ 1976320%」)。
 *  cls:bad 明显变坏(×3 以上)/ worse 变坏 / better 变好 / flat 无好坏之分或持平。 */
export function change(k, cur, prev) {
  const r = prev ? cur / prev : null, diff = cur - prev;
  const worse = k.worse === 'up' ? diff > 0 : k.worse === 'down' ? diff < 0 : false;
  const cls = worse ? (r && (r >= 3 || r <= 1 / 3) ? 'bad' : 'worse') : k.worse && diff !== 0 ? 'better' : 'flat';
  const text = !prev ? (cur ? '新出现' : '持平') : r && Math.abs(r) >= 1.5 ? '×' + r.toFixed(1) : k.u === '%' ? Math.abs(diff).toFixed(1) + ' pt' : diff === 0 ? '持平' : Math.abs((diff / prev) * 100).toFixed(0) + '%';
  return { cls, text, dir: Math.sign(diff), r, diff };
}
// 深挖首句带上分析的是哪个窗口(快照号与时间):不带的话模型要自己去猜是哪一段(见 lib/prompts.js)
export const wWhen = (m) => (m && m.w && m.w.begin_ts ? `WDR 窗口 ${m.w.begin_ts} – ${m.w.end_ts}(快照 ${m.w.begin_id} → ${m.w.end_id})` : 'WDR 窗口');
/** 对比的那个窗口:「上一窗口(快照 2971 → 2978)」;没有上一窗口返回 ''。 */
export const wPrev = (m) => (m && m.p && m.p.window ? `上一窗口(快照 ${m.p.window.begin_id} → ${m.p.window.end_id})` : '');
export const wPrompt = (f, m) => (DEADLOCK_CODES.has(f.code) ? deadlockPrompt(`${wWhen(m)}里`, f)
  : `请针对${wWhen(m)}的发现 ${f.code}(${f.metric}=${f.value})做深入分析:${f.evidence}`);
export const wDimPrompt = (name, x, m) => `请深入分析${wWhen(m)}的「${WT[name] || name}」:${x.headline || ''}`;
/** 页头「在会话里深挖」。 */
export const wDigPrompt = (m) => `请分析${wWhen(m)}的 DB Time 构成,说明是算力型还是等待型负载。`;
/** 解读卡提问框与 ⌘J / ⌘K 自由提问的前缀。 */
export const wAbout = (m) => `关于实例 ${m.name} 的${wWhen(m)}:`;

/** 取一份报告(file 为空取最新)与它的上一窗口并建模。
 *  索引里登记着、文件却取不到 → 退到离它最近的一份(任一窗口都算同一类),模型带 missed;一份都取不到 → reason 'gone'。 */
export async function load(key, file, range) {
  if (!key) return { ok: false, reason: 'none' };
  let rep, idx, missed = [];
  const picked = !!file;
  if (range) {                     // 时间段按快照窗口筛(lib/range.js),上一窗口也只在时间段内找
    const got = await loadInRange('wdr', key, file, range);
    if (!got.ok) return got;
    ({ rep, idx, missed } = got);
    file = got.file;
  } else {
    const [r, index] = await Promise.all([file ? fetchReport('wdr', key, file) : fetchLatest('wdr', key), fetchIndex('wdr', key)]);
    idx = index.ok && Array.isArray(index.data) ? index.data : [];
    rep = r;
    // 指定的那份文件不在(被清理过)→ 往前退;latest.json 每次存档都重写,它在就用它
    const chain = !r.ok && r.status === 404 ? chainFrom(idx, file) : [];
    if (!r.ok && !chain.length) return { ok: false, reason: r.status === 404 ? 'none' : 'error', error: r.error };
    if (!r.ok) {
      const got = file ? await fetchFirst('wdr', key, chain.slice(1), chain.slice(0, 1)) : await fetchFirst('wdr', key, chain);
      if (!got.ok) return got;
      ({ rep, missed } = got);
      file = got.entry.file;
    }
  }
  const d = rep.data;
  const pos = file ? idx.findIndex((e) => e.file === file) : idx.length - 1;
  // 上一窗口 = 往前找第一份**分析的不是同一个快照窗口**的报告。同一个窗口分析了两次(重跑很常见)时,
  // 拿它跟自己比只会得出「与上一窗口相当」的假结论(2026-10-15 真数据上撞到)。最多往前看 5 份。
  const same = (a, b) => a && b && String(a.begin_id) === String(b.begin_id) && String(a.end_id) === String(b.end_id);
  let prevRep = null, sameSkipped = 0;
  for (let i = pos - 1; i >= 0 && i >= pos - 5; i--) {
    const r = await fetchReport('wdr', key, idx[i].file);
    if (!r.ok) continue;
    if (same(r.data.window, d.window)) { sameSkipped++; continue; }
    prevRep = r; break;
  }
  const native = d.native || {};
  // 只有原生报告在本实例的报告目录里才给下载链接:显式 --save-html 到 /tmp 的老报告,给链接就是 404
  const nativeName = native.saved_path && String(native.saved_path).includes(`/wdr/${key}/`) ? String(native.saved_path).split('/').pop() : '';
  return { ok: true, key, d, idx, pos, entry: idx[pos] || null, w: d.window || {}, p: prevRep && prevRep.ok ? prevRep.data : null, sameSkipped, missed, picked,
    sqlTexts: d.sql_texts || {},   // sql_id → 完整 SQL,给 SQL 抽屉用(采集时留的全文)
    findings: [...(d.findings || [])].sort((a, b) => (b.severity | 0) - (a.severity | 0)), native, nativeName,
    nativeUrl: nativeName ? `/reports/wdr/${encodeURIComponent(key)}/${encodeURIComponent(nativeName)}` : '' };
}

/** 普通风格的 WDR 页。 */
export async function render(root, ctx) {
  const view = await import('../views/normal/wdr.js');
  const m = await load(ctx.key, ctx.run && ctx.run.wdr, ctx.range);
  return view.render(root, m.ok ? { ...m, name: instName(ctx, m.d.conn) } : m, ctx);
}
