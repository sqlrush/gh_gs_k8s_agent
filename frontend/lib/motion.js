// 入场编排:进入页面或切实例时一次性的错开浮现与数字滚动。关动效(prefers-reduced-motion)时直接是终态;
// 地址里带 ?still 时也跳过(给无头浏览器截图验收用,它的 requestAnimationFrame 不一定走完)。

const still = () => new URLSearchParams(location.search).has('still') || window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function countUp(root) {
  root.querySelectorAll('[data-count]').forEach((el, i) => {
    const txt = el.textContent.trim(), m = /^(-?)([\d,]*\.?\d+)$/.exec(txt);
    if (!m) return;
    const target = parseFloat(m[2].replace(/,/g, '')), dec = (m[2].split('.')[1] || '').length, comma = m[2].includes(',');
    const fmt = (v) => { const s = v.toFixed(dec); if (!comma) return m[1] + s; const [a, b] = s.split('.'); return m[1] + Number(a).toLocaleString('en-US') + (b ? '.' + b : ''); };
    const t0 = performance.now(), dur = 900 + i * 35;
    const step = (t) => { const p = Math.min(1, (t - t0) / dur); el.textContent = p < 1 ? fmt(target * (1 - Math.pow(1 - p, 3))) : txt; if (p < 1) requestAnimationFrame(step); };
    el.textContent = fmt(0);
    requestAnimationFrame(step);
  });
}

/** root 刚画好时调用:加 .anim 让样式表里的入场动画生效,两秒后摘掉(之后的局部重画不重播)。 */
export function enter(root) {
  if (still()) return;
  root.classList.add('anim');
  countUp(root);
  clearTimeout(enter.t);
  enter.t = setTimeout(() => root.classList.remove('anim'), 2000);
}

/** 编排序号:每个块带 --i,样式表据此错开出现时间。每次整页重画前 reset。 */
let seq = 0;
export const resetRise = () => { seq = 0; };
export const rise = () => ` style="--i:${seq++}"`;
