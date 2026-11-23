// 数字 / 时间 / 级别的格式化。纯函数,无 DOM。

export const LV = ['ok', 'notice', 'warn', 'crit'];
export const LVN = ['正常', '关注', '告警', '严重'];

/** severity(0–3)→ css 级别名;非法值当 ok。 */
export const lv = (s) => LV[Number(s)] || 'ok';
export const lvn = (s) => LVN[Number(s)] || '正常';

export function fmtInt(n) {
  n = Number(n);
  if (!Number.isFinite(n)) return '—';
  if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
  if (n >= 1e4) return (n / 1e4).toFixed(1) + ' 万';
  return n.toLocaleString('en-US');
}

export function fmtSec(s) {
  s = Number(s);
  if (!Number.isFinite(s)) return '—';
  if (s >= 3600) return (s / 3600).toFixed(1) + ' h';
  if (s >= 60) return (s / 60).toFixed(1) + ' min';
  return s.toFixed(s < 10 ? 1 : 0) + ' s';
}

export function fmtMs(ms) {
  ms = Number(ms);
  if (!Number.isFinite(ms)) return '—';
  return ms >= 1000 ? (ms / 1000).toFixed(1) + ' s' : ms.toFixed(1) + ' ms';
}

/** 存档时间戳 20260922T060312Z 或 ISO 串 → "09-22 14:03"(本地时区)。 */
export function stamp(s) {
  if (!s) return '—';
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(s);
  const d = m ? new Date(Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6])) : new Date(s);
  if (Number.isNaN(d.getTime())) return String(s);
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export const mmdd = (s) => stamp(s).slice(0, 5);

/** 模板里插值用:把用户数据里的 < > & 转掉,报告里的 SQL 原文经常带 < 。 */
export function esc(v) {
  return String(v ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/** SQL 摘要:压掉空白,截到 n 个字符。 */
export function brief(sql, n = 80) {
  const s = String(sql ?? '').replace(/\s+/g, ' ').trim();
  return s.length > n ? s.slice(0, n) + '…' : s;
}

/** 表格单元格:第 0 列是行名;其余是数字右对齐。长文本(多半是 SQL)截断 + 悬停看全文,不撑破卡片。 */
// 像数字的(可带单位)才右对齐;文字列左对齐 —— 否则「184.8M 扫描 0 次(该表索引扫描共 0…)」这种会被挤成一字一行
const NUMLIKE = /^[-+]?[\d.,]+\s*(%|ms|s|MB|GB|KB|MiB|GiB|块|次|\/s)?$/;
export function cellTd(c, i) {
  const s = String(c ?? "");
  if (s.length > 48) return `<td class="q" title="${esc(s)}">${esc(s)}</td>`;
  if (i && NUMLIKE.test(s.trim())) return `<td class="r tnum">${esc(s)}</td>`;
  return `<td${i && s.length > 12 ? ' class="txt"' : ""}>${esc(s)}</td>`;
}

// ---------------------------------------------------------------- 两种风格的版式要用到的几样
/** 字符串里的数字(可带千分位);取不到返回 null。 */
export const num = (v) => { const n = parseFloat(String(v ?? '').replace(/,/g, '')); return Number.isFinite(n) ? n : null; };
/** "8.4 s" → ["8.4", "s"]:数值与单位分开排。 */
export const split = (s) => { const m = /^(-?[\d.,]+)\s*(.*)$/.exec(String(s)); return m ? [m[1], m[2]] : [String(s), '']; };
/** 中文与西文、数字之间补一个空格。 */
export const pangu = (s) => String(s).replace(/([㐀-鿿])([A-Za-z0-9])/g, '$1 $2').replace(/([A-Za-z0-9%])([㐀-鿿])/g, '$1 $2');
/** 报告里的中文句子常混着半角标点:挨着汉字的 , : ; ( ) 换成全角,再补中英文间距。 */
const FW = { ',': '，', ':': '：', ';': '；' };
export const cn = (s) => pangu(String(s ?? '')
  .replace(/([㐀-鿿）])\s*([,:;])\s*/g, (m, a, p) => a + FW[p])
  .replace(/\s*([,:;])\s*(?=[㐀-鿿（])/g, (m, p) => FW[p])
  .replace(/\(([^()]*[㐀-鿿][^()]*)\)/g, '（$1）'));
export const joinCn = (xs) => (xs.length <= 1 ? xs.join('') : xs.slice(0, -1).join('、') + '和' + xs[xs.length - 1]);
const toDate = (s) => { const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(s || ''); return m ? new Date(Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6])) : new Date(s); };
/** "2026 年 9 月 22 日 星期二"(本地时区),报纸报头用。 */
export function fullDate(s) {
  const d = toDate(s);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日 星期${'日一二三四五六'[d.getDay()]}`;
}
/** 表格列格式:整列最大值过百万时用「万 / 亿」,否则千分位;非数字原样。 */
export const colFmt = (rows, i) => {
  const mx = Math.max(0, ...rows.map((r) => Math.abs(num(r[i]) || 0)));
  return (c) => { const n = num(c); if (!/^-?[\d.]+$/.test(String(c)) || n === null) return String(c); return mx >= 1e6 ? fmtInt(n) : Number.isInteger(n) ? n.toLocaleString('en-US') : n.toLocaleString('en-US', { maximumFractionDigits: 2 }); };
};
