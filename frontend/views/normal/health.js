// 普通风格 · 健康检查:主视觉 = 检查项表盘 + 智能体解读 + 四个统计;其下便当盒 = 发现表、巡检节奏、维度格、子技能。

import { esc, stamp, cn } from '../../lib/fmt.js';
import { I, lk, glyph, status, tag, tipAttr, dig, cu, primary } from '../../lib/ui.js';
import { rise } from '../../lib/motion.js';
import { setModel } from '../../lib/store.js';
import { dial, rhythm } from '../../lib/viz.js';
import * as insight from '../../lib/insight.js';
import { title, metricName, keyFig, fPrompt, countsOf, naLegend, hDigPrompt, hRedoPrompt, hAbout } from '../../pages/health.js';
import { pageHead, tile, insightCard, heroStats, emptyHero, errorHero, runPicker, rangeHero, goneHero } from './common.js';
import { missedBanner, reportLabel } from '../../lib/missed.js';

function hero(m) {
  const cnt = {};
  m.checks.forEach((s) => { cnt[s.lv] = (cnt[s.lv] || 0) + 1; });
  const legend = [['3', '严重'], ['2', '告警'], ['1', '关注'], ['0', '正常'], ['na', naLegend(m.dims)]].filter(([k]) => cnt[k])
    .map(([k, n]) => `<span>${glyph(k === 'na' ? null : +k)}${n}<b>${cnt[k]}</b></span>`).join('');
  const fs = m.findings, subs = m.subs, okN = subs.filter((s) => s.ok).length, hi = fs.filter((f) => (f.severity | 0) >= 2), lo = fs.filter((f) => (f.severity | 0) === 1);
  const dTot = m.prev ? fs.length - countsOf(m.prev) : null;
  const stats = m.level === null
    ? [{ l: `${glyph(null)}采集失败的维度`, v: cu(String(m.dims.length)), u: `/ ${m.dims.length}`, s: '一个维度都没有采到' },
       { l: '子技能', v: cu(String(okN)), u: `/ ${subs.length} 成功`, s: okN ? '' : '全部失败' },
       { l: '越过阈值的发现', v: '—', s: '没采到数据，无法判定' },
       { l: '存档报告', v: cu(String(m.idx.length)), u: '份', s: `本次 ${esc(stamp(m.entry && m.entry.at))}` }]
    : [{ l: '越过阈值的发现', v: cu(String(fs.length)), u: '条', s: dTot === null ? '第一份报告' : dTot > 0 ? `<span class="d-up">${I.up} ${dTot}</span> 比上次多` : dTot < 0 ? `<span class="d-dn">${I.dn} ${-dTot}</span> 比上次少` : '与上次持平' },
       { l: `${glyph(2)}告警及以上`, v: cu(String(hi.length)), u: '条', s: hi.some((f) => (f.severity | 0) === 3) ? '含严重' : '没有严重' },
       { l: `${glyph(1)}需要关注`, v: cu(String(lo.length)), u: '条', s: '需要跟进' },
       { l: '子技能', v: cu(String(okN)), u: `/ ${subs.length} 成功`, s: subs.length ? (okN < subs.length ? `${esc(subs.filter((s) => !s.ok).map((s) => s.skill.replace('gaussdb-', '')).join('、'))} 失败` : '全部成功') : '本次未运行子技能' }];
  const ins = insight.health(m);
  return `<section class="hero"><div class="hero-grid">
      <div class="glass ringbox rise"${rise()}><div class="title">${I.grid}检查项<span class="t3">点一项看明细</span></div>${dial(m.checks, m.level, `${m.checks.length} 项检查`)}<div class="ring-legend">${legend}</div></div>
      ${insightCard({ ...ins, ctx: hAbout(m) })}
    </div>${heroStats(stats)}</section>`;
}

function findingsTable(m) {
  const fs = m.findings;
  if (!fs.length) return `<div class="empty-state" style="padding:36px">${glyph(m.level === null ? null : 0)}<p style="margin-top:10px">${m.level === null ? '所有维度都没采到数据，这份报告<b>不能说明库是健康的</b>。' : '没有越过阈值的发现。'}</p></div>`;
  const over = (f) => {
    const t = insight.thresholdOf(f), x = insight.times(f);
    if (!x) return `<div class="over"><div class="x">阈值 ${esc(f.threshold)}</div><div class="x t3">组合条件，见证据</div></div>`;
    const sc = Math.max(t.v, t.thr) * 1.1;
    return `<div class="over" data-lv="${lk(f.severity)}"><div class="bar"><i class="grow" style="width:${(t.v / sc) * 100}%"></i><span class="thr" style="left:${(t.thr / sc) * 100}%"></span></div><div class="x">阈值 ${esc(f.threshold)}，<b>${x.toFixed(1)} 倍</b></div></div>`;
  };
  return `<div class="scroll"><table class="tbl"><thead><tr><th>级别</th><th>维度</th><th>指标</th><th class="r">当前值</th><th>对比阈值</th><th class="r">操作</th></tr></thead><tbody>${fs.map((f) => {
    const d = m.dims.find((x) => x.dimension === f.dimension), open = d ? ` data-dim="${d.i}"` : ` data-xdim="${esc(f.dimension)}"`;
    return `<tr><td>${status(f.severity)}</td><td><a href="#"${open}>${esc(title(f.dimension))}</a></td><td class="title metric"${tipAttr(`<b>证据</b><div class="d">${esc(cn(f.evidence))}</div>`)}>${esc(cn(metricName(f)))}</td>
      <td class="r"><b style="font-weight:500">${esc(f.value)}</b></td><td>${over(f)}</td><td class="ops"><a href="#"${dig(fPrompt(f, m))}>${f.sql_id ? '看执行计划' : '深挖'}</a>${f.sql_id ? `<a href="#" data-sql="${esc(f.sql_id)}">SQL</a>` : ''}</td></tr>`;
  }).join('')}</tbody></table></div>`;
}

export function render(root, m, ctx) {
  const head = (st, act, desc) => pageHead('健康检查', st, act, desc);
  if (!m.ok) {
    root.innerHTML = head('', '', []) + (m.reason === 'error' ? errorHero(m.error) : m.reason === 'range' ? rangeHero(ctx.range) : m.reason === 'gone' ? goneHero(m, '请对当前库做一次健康检查。', '在会话里跑一次健康检查', m.alt ? `<button class="btn" data-pick="health|${esc(m.alt.file)}">${I.cal}看${m.alt.mode === 'span' ? '时段体检' : '巡检'}（${esc(reportLabel(m.alt))}）</button>` : '') : emptyHero(ctx.key ? '这个实例还没有健康检查报告' : '还没有健康检查报告', '在会话里做一次巡检，报告会自动出现在这里。', '请对当前库做一次健康检查。', '在会话里跑一次健康检查'));
    return;
  }
  setModel('health', m);
  const r = m.d;
  // 时段体检里没采的维度大多是「回看不了」(瞬时状态,当时没有采),写它自己的说法,不笼统写「未采集」
  const naCap = (d) => (m.span && d.headline ? cn(d.headline) : '未采集');
  const tiles = `<div class="dims">${m.dims.map((d) => { const k = keyFig(d), s = k || { v: '—', u: '', cap: d.available === false ? naCap(d) : cn(d.headline || '') };
    return `<button class="dim" data-lv="${lk(d.level)}" data-dim="${d.i}"${tipAttr(`<b>${esc(title(d.dimension))}</b><div class="d">${esc(cn(d.headline || ''))}</div>`)}><div class="h"><b>${esc(title(d.dimension))}</b>${status(d.level, d.na || undefined)}</div><div class="v num">${esc(s.v)}<small>${esc(s.u)}</small></div><div class="c">${esc(s.cap)}</div></button>`; }).join('')}</div>`;
  const kb = r.kb_refs && r.kb_refs.status;
  const subs = `<div class="list">${m.subs.length ? m.subs.map((s) => `<div class="it">${glyph(s.ok ? 0 : null)}<div class="m"><b>${esc(s.skill.replace('gaussdb-', ''))}</b>${s.ok ? '' : `<small>${esc(cn(s.error || '失败'))}</small>`}</div><span class="r">${s.ok ? '已纳入' : '失败'}</span></div>`).join('') : '<p class="t3">本次未运行子技能</p>'}</div>
    ${(r.uncovered_capabilities || []).length || kb ? `<div class="note">${(r.uncovered_capabilities || []).length ? `不在巡检里的能力：${esc(r.uncovered_capabilities.map((x) => x.replace('gaussdb-', '')).join('、'))}，需要时在会话里单独运行。` : ''}${kb ? `${(r.uncovered_capabilities || []).length ? '<br>' : ''}知识库参照：${kb.attached ? `已接入，命中 ${(r.kb_refs.items || []).length} 条` : '未接入'}。` : ''}</div>` : ''}`;
  const target = m.name;
  const nNa = m.dims.filter((d) => d.level === null).length;
  const naTag = m.level !== null && nNa ? ` <span class="tag gray">${nNa} 个维度${m.span ? '回看不了或没采到' : '没采到'}，结论不完整</span>` : '';
  const desc = m.span
    ? [['实例', esc(target)], ['执行人', esc(ctx.whoami || '—')], ['评估时段', esc(m.win || '—')], ['口径', '窗口两端相减；连接、锁、膨胀等只有此刻的值'], ['存档', `${m.all.length} 份`]]
    : [['实例', esc(target)], ['执行人', esc(ctx.whoami || '—')], ['巡检时间', esc(stamp(m.entry && m.entry.at))], ['存档', `${m.all.length} 份`]];
  root.innerHTML = head((m.span ? '<span class="tag blue">时段体检</span> ' : '') + tag(m.level) + naTag,
    runPicker('health', m.entry, m.span ? '时段体检' : '巡检') + `<button class="btn"${dig(hRedoPrompt(m), '健康检查')}>${I.redo}重新巡检</button>` + primary(hDigPrompt(m), '在会话里深挖', '健康检查'),
    desc)
    + missedBanner(m) + hero(m)
    + `<div class="bento">${tile({ span: 'b8', title: `需要处理的发现<span class="cnt">${m.findings.length}</span>`, desc: '脚本按阈值判定，模型不改级别；指标上停留可看证据', body: findingsTable(m), flush: !!m.findings.length })}
      ${tile({ span: 'b4', title: m.span ? '时段体检记录' : '巡检节奏', desc: `最近 ${Math.min(20, m.idx.length)} 次，点格子切换报告`, body: rhythm('health', m.idx, m.entry && m.entry.file) })}
      ${tile({ span: 'b9', title: '维度状态', desc: `共 ${m.dims.length} 个维度，点击查看明细`, body: tiles })}
      ${tile({ span: 'b3', title: '子技能执行', desc: '任一失败也照常出报告', body: subs })}</div>`;
}
