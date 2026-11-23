// 文艺风格的小零件:版面跳转链接、栏目眉题、段落。paper.js 与 sections.js 共用。

import { esc } from '../../lib/fmt.js';

export const jump = (id, label) => `<a href="#" class="jump" data-jump="${id}">${esc(label)} →</a>`;
export const kicker = (t) => `<div class="kicker">${esc(t)}</div>`;
export const paras = (lines) => lines.filter(Boolean).map((l) => `<p>${l}</p>`).join('');
