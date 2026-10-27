#!/usr/bin/env bash
# 端到端(模型级):扫描件 PDF(没有文字层)与 PPT(.pptx)经导入容器进知识库 —— 扫描件每页渲染成图片由模型看图转录,
# pptx 抽出标题、要点、表格、备注与图片;两份都只做到「待确认清单」为止,然后校验落盘。真实模型。
# 材料在本机现生成(PIL 画一份两页的扫描件规范、python-pptx 做一份三页的经验分享),内容全是虚构的。
# 用法: scripts/k8s/e2e-kb-scan-pptx.sh [管理员工号]     前提同 e2e-web-clip.sh(模拟 SSO 在 $SSO,角色 kb-admin)
# 收尾照备份还原知识库,删掉本次的收件目录与中间目录。
set -u
U=${1:-u9500}; GW=${GW:-18089}; SSO=${SSO:-18096}
HERE=$(cd "$(dirname "$0")/../.." && pwd)
NAS=${NAS:-/Users/Shared/agent-nas-k8s}; KB=$NAS/kb
OUT=${OUT:-/tmp/e2e-kb-scan-pptx}; TRUST=${TRUST:-$HOME/.og5-daily/trust}; WAIT=${WAIT:-1800}
PORTAL=http://127.0.0.1:$SSO; UP=$KB/inbox/uploads/scan-pptx-e2e
pass=0; fail=0
ok(){ pass=$((pass+1)); echo "[PASS] $1"; }
bad(){ fail=$((fail+1)); echo "[FAIL] $1"; echo "   | $(printf '%s' "${out:-}" | head -8 | tr '\n' ' ' | cut -c1-600)"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }
has(){ printf '%s' "$out" | grep -q -- "$1"; }
rm -rf "$OUT"; mkdir -p "$OUT"

BK=$OUT/kb-backup; mkdir -p "$BK"
EXCL=(--exclude /docsets --exclude /inbox --exclude /init --exclude /index/embed-cache)
rsync -a "${EXCL[@]}" "$KB/" "$BK/" || { echo "备份知识库失败"; exit 1; }
for d in inbox init; do ls -1 "$KB/$d" 2>/dev/null | sort > "$OUT/before-$d.txt"; done
restore(){
  echo "== 收尾:还原知识库"
  rsync -a --delete "${EXCL[@]}" "$BK/" "$KB/"
  rm -rf "$UP"
  for d in inbox init; do
    comm -13 "$OUT/before-$d.txt" <(ls -1 "$KB/$d" 2>/dev/null | sort) | while read -r x; do
      [ -n "$x" ] && rm -rf "${KB:?}/$d/$x" && echo "   删掉 $d/$x"
    done
  done
  kill "$SSO_PID" 2>/dev/null
}
python3 "$HERE/k8s/tools/sso-sim.py" "$SSO" "$GW" "$U" "$TRUST" kb-admin > "$OUT/sso.log" 2>&1 & SSO_PID=$!
trap restore EXIT
sleep 2

# ---- 0 材料 ----
mkdir -p "$UP"
python3 - "$UP" <<'EOF' || { echo "生成材料失败(要 pillow 与 python-pptx)"; exit 1; }
import io, pathlib, sys
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches, Pt
up = pathlib.Path(sys.argv[1])
font = next((ImageFont.truetype(p, 34) for p in ("/System/Library/Fonts/STHeiti Medium.ttc", "/System/Library/Fonts/PingFang.ttc",
             "/System/Library/Fonts/Hiragino Sans GB.ttc") if pathlib.Path(p).exists()), None)
pages = [["数据库备份与恢复规范(V2.1)", "", "第一章 适用范围", "1.1 本规范适用于生产环境全部 GaussDB 实例。", "",
          "第二章 备份要求", "2.1 全量备份每周日 01:00 执行,保留 4 周。", "2.2 增量备份每日 01:00 执行,保留 14 天。",
          "2.3 归档日志实时备份,保留 30 天。"],
         ["第三章 恢复演练", "3.1 每季度至少做一次恢复演练,演练报告存档。", "3.2 恢复时间目标(RTO)不超过 2 小时。", "",
          "第四章 职责", "4.1 备份失败由值班 DBA 在 30 分钟内处置并报告。"]]
imgs = []
for lines in pages:
    im = Image.new("L", (1240, 1754), 250)
    d = ImageDraw.Draw(im)
    for i, t in enumerate(lines):
        d.text((110, 140 + i * 70), t, fill=20, font=font)
    imgs.append(im.convert("RGB"))
imgs[0].save(up / "备份恢复规范-扫描件.pdf", save_all=True, append_images=imgs[1:], resolution=150)
p = Presentation()
s = p.slides.add_slide(p.slide_layouts[0]); s.shapes.title.text = "慢 SQL 治理经验分享"; s.placeholders[1].text = "运维二组 2026 年三季度"
s = p.slides.add_slide(p.slide_layouts[1]); s.shapes.title.text = "高频问题"
tf = s.placeholders[1].text_frame; tf.text = "统计信息过期导致计划跳变"
para = tf.add_paragraph(); para.text = "大表灌数后没有 ANALYZE"; para.level = 1
para = tf.add_paragraph(); para.text = "隐式类型转换导致索引失效"
s.notes_slide.notes_text_frame.text = "讲到这里强调:灌数后立即 ANALYZE 目标表"
s = p.slides.add_slide(p.slide_layouts[5]); s.shapes.title.text = "处置效果"
rows = [("问题", "处置", "耗时下降"), ("计划跳变", "ANALYZE + 固定计划", "95%"), ("索引失效", "改写谓词类型", "80%")]
tb = s.shapes.add_table(3, 3, Inches(0.5), Inches(1.6), Inches(9), Inches(1.5)).table
for r, row in enumerate(rows):
    for c, v in enumerate(row):
        tb.cell(r, c).text = v
buf = io.BytesIO(); Image.new("RGB", (320, 180), (230, 236, 245)).save(buf, "PNG"); buf.seek(0)
s.shapes.add_picture(buf, Inches(0.5), Inches(3.4), Inches(3), Inches(1.7))
p.save(up / "慢SQL治理经验分享.pptx")
print("ok")
EOF
chmod -R a+rwX "$UP"
out=$(ls "$UP")
check "0 材料就位(扫描件 PDF 两页、pptx 三页)"                               'has "扫描件.pdf" && has ".pptx"'

# ---- 1 让模型导入,做到待确认清单为止 ----
api(){ curl -s -m "${3:-60}" -H "Cookie: gsa_target=kb-import" -H "Content-Type: application/json" "$PORTAL$1" ${2:+-d "$2"}; }
touch "$OUT/start"; sleep 1
SID=$(api /session '{"title":"e2e-scan-pptx"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
[ -n "$SID" ] || { echo "建不了会话"; exit 1; }
api "/session/$SID/prompt_async" "{\"parts\":[{\"type\":\"text\",\"text\":\"收件目录 /nas/kb/inbox/uploads/scan-pptx-e2e 里有一份扫描件 PDF(备份恢复规范)和一份 PPT(慢 SQL 治理经验分享),请导入知识库:扫描件按 SKILL.md 逐页看图转录进 source.md 再条款化;PPT 按材料类型写成对应的条目。两份都做到给出待确认清单为止,先不要写入知识库。这是自动验收,没有人回答提问,不要使用提问工具;看不清的字照规矩写「[看不清]」。\"}]}" >/dev/null
START=$(date +%s); sleep 20
# 模型用提问工具请管理员确认(选择列表)就是「待确认清单」那一步:记下问题、停下来核对,最后拒掉它(不入库)
while [ $(( $(date +%s) - START )) -lt "$WAIT" ]; do
  api /question | python3 -c 'import json,sys
q=[x for x in json.load(sys.stdin) if x.get("sessionID")==sys.argv[1]]
json.dump(q[0], open(sys.argv[2], "w"), ensure_ascii=False) if q else None
sys.exit(0 if q else 1)' "$SID" "$OUT/question.json" 2>/dev/null && break
  api /session/status | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if (d.get(sys.argv[1]) or {}).get("type") in ("busy","retry") else 1)' "$SID" 2>/dev/null || break
  sleep 15
done
DT=$(( $(date +%s) - START ))
REPLY=$(api "/session/$SID/message" | python3 -c 'import json,sys
ms=[m for m in json.load(sys.stdin) if m.get("info",{}).get("role")=="assistant"]
print("".join(p.get("text","") for p in (ms[-1]["parts"] if ms else []) if p.get("type")=="text"))')
printf '%s\n' "$REPLY" > "$OUT/reply.txt"
api "/session/$SID/message" | python3 -c 'import json,sys
for m in json.load(sys.stdin):
    for p in m.get("parts",[]):
        if p.get("type")=="tool": print(p.get("tool"), json.dumps(p.get("state",{}).get("input",{}),ensure_ascii=False)[:300])' > "$OUT/tools.txt"
echo "   ↳ 耗时 ${DT}s;$(printf '%s' "$REPLY" | tr '\n' ' ' | cut -c1-400)"

# ---- 2 落盘核对(走 kb.py ingest 落在 inbox/<slug>/,走 kb_init scan 落在 init/ 与 sources/,两条路都认)----
PAGES=$(find "$KB/inbox" "$KB/sources" -newer "$OUT/start" -name 'page-0*.jpg' 2>/dev/null | sort)
out=$PAGES
check "1 扫描件每页渲染成图片(page-01.jpg、page-02.jpg)"                       'has "page-01.jpg" && has "page-02.jpg"'
out=$(grep -E '^read .*page-0[12]\.jpg' "$OUT/tools.txt" | wc -l | tr -d ' ')
check "2 模型逐页用 read 看了图"                                                '[ "$out" -ge 2 ]'
SDIR=$(dirname "$(printf '%s\n' "$PAGES" | head -1)")
out=$(cat "$(dirname "$SDIR")/source.md" 2>/dev/null; find "$KB/init" -newer "$OUT/start" -path '*draft*' -type f -exec cat {} + 2>/dev/null)
check "3 转录稿里有两页的关键内容(保留 30 天 / RTO 2 小时)"                        'has "30 天" && (has "RTO" || has "2 小时")'
check "4 转录完删掉了开头的转录说明"                                             '! has "扫描件转录稿"'
out=$(grep -l "慢 SQL 治理经验分享" "$KB"/inbox/*/source.md 2>/dev/null; find "$KB/init" -newer "$OUT/start" -name '*.json' -exec grep -l "慢 SQL 治理经验分享" {} + 2>/dev/null)
PPT=$(printf '%s\n' "$out" | head -1); out=$(cat "$PPT" 2>&1)
check "5 pptx 抽成按页的 md(标题、分级要点、表格、备注)"                         'has "## 第 2 页" && has "大表灌数后没有 ANALYZE" && has "| 计划跳变 |" && has "备注:讲到这里强调"'
out=$(find "$KB/inbox" "$KB/sources" -newer "$OUT/start" -name 'slide03-*' 2>/dev/null)
check "6 幻灯片里的图片取出来了(slide03-*)"                                      '[ -n "$out" ]'
out="$REPLY $(cat "$OUT/question.json" 2>/dev/null)"
check "7 模型给出待确认清单"                                                    'has "确认"'
out=$(comm -13 <(cd "$BK" && find rules errata cases wiki runbooks -type f 2>/dev/null | sort) <(cd "$KB" && find rules errata cases wiki runbooks -type f 2>/dev/null | sort))
check "8 确认之前没有写入知识库"                                                 '[ -z "$out" ]'

[ -f "$OUT/question.json" ] && api "/question/$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$OUT/question.json")/reject" '{}' >/dev/null
api "/session/$SID/abort" '{}' >/dev/null
echo "== 汇总: PASS $pass, FAIL $fail(模型回复与工具调用在 $OUT)"
[ $fail -eq 0 ]
