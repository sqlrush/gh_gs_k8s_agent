// 知识库:取数与建模。全体共享一份知识库,不分实例。数据:/reports/kb/health.json(gaussdb-kb health --json)
// + /reports/kb/queries.jsonl(本人的检索日志)+ /reports/_kb/catalog.json(知识条目 / 案例 / 条款 / 关系图的目录)。

import { fetchKbHealth, fetchKbQueries, fetchKbCatalog } from '../lib/data.js';
import { allRules } from '../lib/kb-docs.js';

const pctOf = (text) => { const m = /(\d+)%/.exec(String(text || '')); return m ? +m[1] : null; };   // "DataVec(覆盖 93%)" → 93

export async function load() {
  const [h, q, c] = await Promise.all([fetchKbHealth(), fetchKbQueries(), fetchKbCatalog()]);
  return model(h, q, c);
}

export function model(h, q, c) {
  const cat = c.ok ? c.data : null, catOk = !!(cat && cat.attached);
  const d = h.ok ? h.data : {}, st = d.status || {}, cnt = st.counts || {}, ix = d.index_state || {};
  const cov = pctOf(st.vector);
  const pending = d.pending || [], warns = d.file_warnings || [], misses = d.misses || [];
  // 关系图边数:优先用知识库目录里实际读到的已确认边;没有目录时退回状态报告(配了图库看 index_state,没配时只写在 status.graph 那句话里)
  const gEdges = catOk ? (cat.edges || []).length : ix.graph && ix.graph !== 'none' ? (+ix.edges_confirmed || 0) : +((/(\d+)\s*条已确认边/.exec(st.graph || '') || [])[1] ?? NaN);
  const since = Date.now() - 7 * 86400e3;
  return {
    ok: h.ok, status: h.status, error: h.error, d, st, ix, cat, catOk, catErr: cat ? cat.reason || '' : c.status === 404 ? '当前镜像版本不提供知识库目录' : c.error,
    attached: st.attached !== false,
    counts: { case: +cnt['docs.case'] || 0, rule: +cnt['docs.rule'] || 0, raw: +cnt['docs.raw'] || 0 },
    cov, pending, warns, misses, gEdges,
    gKind: !st.graph ? '未配置' : /neo4j|图库/i.test(st.graph) && !/未配置/.test(st.graph) ? '图库' : '图文件',
    issues: pending.length + warns.length + (cov !== null && cov < 100 ? 1 : 0),
    queries: (q.ok ? q.data : []).filter((r) => new Date(r.at).getTime() >= since).sort((a, b) => (b.at > a.at ? 1 : -1)).slice(0, 20),
    rules: catOk ? allRules(cat) : [],
    types: catOk && cat.types ? cat.types : null,   // 八类知识各多少(本行知识;官方文档集另见 docsets);旧镜像的目录没有这一项
    docsets: catOk ? cat.docsets || [] : [],        // 官方文档集(标准知识库)每套一行:代号、条目数;逐条看在「目录」页签
    cases: catOk ? [...(cat.cases || [])].sort((a, b) => String(b.occurred_at).localeCompare(String(a.occurred_at))) : [],
  };
}
/** 缺口清单里 q: 开头的是检索词无命中,其余是诊断发现码。 */
export const missLabel = (code) => (String(code).startsWith('q:') ? `检索无命中 · 「${String(code).slice(2)}」` : String(code));

/** 普通风格的知识库页(含知识条目 / 案例 / 条款 / 关系图页签)。 */
export async function render(root, ctx) {
  const view = await import('../views/normal/kb.js');
  return view.render(root, await load(), ctx);
}
