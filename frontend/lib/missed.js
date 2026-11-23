// 退过一份时页面上的那句说明。取数时怎么退见 lib/data.js fetchFirst:索引里登记着、文件却不在(404),
// 就显示同一类里离它最近的一份 —— 但不能悄悄换,用户以为看的是最新的,其实是三天前的。两种风格共用。

import { esc, stamp } from './fmt.js';
import { glyph } from './ui.js';
import { windowLabel } from './range.js';

/** 一份报告(索引条目)的称呼:时段报告写评估窗口,其余写存档时间。 */
const label = (e) => (e && e.mode && e.window && e.window.begin_ts ? `时段 ${windowLabel(e.window)}` : stamp(e && e.at));
const WHY = '索引里还登记着这份报告，但文件已不在存档目录里（可能被清理过）。要看最新的结果，在会话里重新跑一次。';

/** m.missed = 取不到的条目(想要的那份在前),m.entry = 实际显示的那份,m.picked = 是不是指定了某一份。没退过返回 ''。 */
export function missedText(m) {
  const miss = (m && m.missed) || [];
  if (!miss.length || !m.entry) return '';
  const what = m.picked ? `所选的那份报告（${label(miss[0])}）读取失败`
    : miss.length > 1 ? `最新的 ${miss.length} 份报告读取失败（最近的一份是 ${label(miss[0])}）` : `最新一份报告（${label(miss[0])}）读取失败`;
  return `${what}，显示的是 ${label(m.entry)} 的那份。`;
}

/** 页头下面那一条提示;没退过返回 ''。 */
export function missedBanner(m) {
  const t = missedText(m);
  return t ? `<div class="alert missed" data-lv="2">${glyph(2)}<div><b>${esc(t)}</b><p>${esc(WHY)}</p></div></div>` : '';
}

/** 一份都取不到(reason 'gone')时的说明。 */
export function goneText(m) {
  const miss = (m && m.missed) || [];
  const which = miss.length > 1 ? `索引里登记的 ${miss.length} 份报告（最近的是 ${label(miss[0])}）` : `索引里登记的那份报告${miss[0] ? `（${label(miss[0])}）` : ''}`;
  return `${which}文件${miss.length > 1 ? '都' : ''}已不在存档目录里（可能被清理过），读取不到。在会话里重新跑一次，新的报告会出现在这里。`;
}

export { label as reportLabel };
