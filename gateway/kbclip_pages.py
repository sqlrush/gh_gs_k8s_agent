"""网页剪藏的两张页面与书签代码(2026-10-27,网关直接返回的静态 HTML,不依赖前端)。

· 安装页:书签代码在浏览器里按 location.origin 现生成 —— 页面是经贵方 SSO 打开的,location.origin 就是用户访问本系统的地址,
  网关自己并不知道外面的域名(SSO / Ingress 可能改写 Host)。
· 剪藏页:书签用 window.open 打开它;它向打开者报「就绪」,书签再用 postMessage 把已登录页面的 DOM 与图片交过来。
  只收名单里的网站、且确实来自打开它的那个窗口的消息;预览只数字数,**不渲染**剪到的 HTML(DOMParser 解析出的文档是惰性的,
  不执行脚本、不加载图片)。也可以拖入「另存为 → 网页,单个文件」的 .mhtml。

脚本带 nonce,CSP 只放行它;页面不许被嵌套(frame-ancestors 'none')。
"""
from __future__ import annotations

import json
from typing import Dict, Sequence

CLIP_PATH = "/kb-import/clip"
INSTALL_PATH = CLIP_PATH + "/install"
UPLOAD_PATH = CLIP_PATH + "/upload"

_STYLE = """body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:46rem;margin:2.5rem auto;
padding:0 1.25rem;color:#24292f;line-height:1.7}h1{font-size:1.35rem}h2{font-size:1.05rem;margin-top:1.8rem}
.muted{color:#57606a}.state{padding:.6rem .9rem;border-radius:6px;background:#f6f8fa}.off{background:#fff8c5}
.err{color:#cf222e}.ok{color:#1a7f37}.bm{display:inline-block;padding:.35rem .9rem;border-radius:6px;background:#0969da;
color:#fff;text-decoration:none;font-weight:600;cursor:grab}button{font:inherit;padding:.35rem 1rem;border-radius:6px;
border:1px solid #d0d7de;background:#f6f8fa;cursor:pointer}button.primary{background:#1f883d;color:#fff;border-color:#1f883d}
button:disabled{opacity:.5;cursor:default}textarea{width:100%;height:5rem;font:12px ui-monospace,monospace;margin-top:.5rem}
.card{border:1px solid #d0d7de;border-radius:8px;padding:1rem 1.1rem;margin:1rem 0}.t{font-weight:600;font-size:1.1rem}
.u{word-break:break-all;color:#57606a;font-size:.9rem}#drop{border:2px dashed #d0d7de;border-radius:8px;padding:1.4rem;
text-align:center;margin-top:.6rem}#drop.on{border-color:#0969da;background:#ddf4ff}"""


def headers(nonce: str) -> Dict[str, str]:
    return {"Content-Security-Policy": "default-src 'none'; script-src 'nonce-%s'; style-src 'unsafe-inline'; "
                                       "connect-src 'self'; img-src data:; frame-ancestors 'none'; form-action 'none'; "
                                       "base-uri 'none'" % nonce,
            "X-Frame-Options": "DENY", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff"}


def _cfg(origins: Sequence[str], max_bytes: int) -> str:
    # 放进 <script type=application/json>:把 < 转义掉,名单里再怎么写也跳不出这个标签
    return json.dumps({"origins": list(origins), "maxBytes": max_bytes, "maxImages": 30, "oneImage": 5 * 1024 * 1024,
                       "clip": CLIP_PATH, "upload": UPLOAD_PATH}, ensure_ascii=False).replace("<", "\\u003c")


def _page(title: str, body: str, origins: Sequence[str], max_bytes: int, nonce: str, script: str) -> bytes:
    return ("""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>%s</title><style>%s</style></head><body>%s
<script type="application/json" id="cfg">%s</script><script nonce="%s">%s</script></body></html>"""
            % (title, _STYLE, body, _cfg(origins, max_bytes), nonce, script)).encode("utf-8")


# 书签本体。__P__ 由安装页换成本系统的源(JSON 字符串)。在用户点它的那个页面里运行:先同步开窗(弹窗要在点击当下开),
# 再取 DOM(点下去那一刻的样子)与同源图片;剪藏页报就绪后只发给本系统的源。
BOOKMARKLET = r"""(()=>{const P=__P__,M=__M__,ONE=__ONE__,ALL=__ALL__;
if(location.origin===P){alert("请在要导入的那个网页上点这个书签(现在打开的是导入系统自己的页面)");return}
const w=window.open(P+"/kb-import/clip","kbclip","width=780,height=760");
if(!w){alert("浏览器拦住了弹出窗口:请允许这个网站弹出窗口,再点一次「导入知识库」");return}
const H=document.documentElement.outerHTML,T=document.title,U=location.href,F=new Date().toISOString();
const seen=new Set(),src=[];for(const i of document.images){const s=i.currentSrc||i.src;if(s&&!seen.has(s)){seen.add(s);src.push(s)}}
const rd=b=>new Promise((ok,no)=>{const f=new FileReader();f.onload=()=>ok(f.result);f.onerror=no;f.readAsDataURL(b)});
const job=(async()=>{const ims=[],sk=[];let tot=H.length;for(const s of src){let u;try{u=new URL(s,U)}catch(e){continue}
if(u.protocol!=="data:"&&u.origin!==location.origin){sk.push({src:s,reason:"跨站图片,读不到"});continue}
if(ims.length>=M){sk.push({src:s,reason:"超过 "+M+" 张上限"});continue}
try{const r=await fetch(u.href,{credentials:"include"});if(!r.ok)throw new Error(r.status);const b=await r.blob();
if(b.size>ONE){sk.push({src:s,reason:"单张超过 5 MB"});continue}if(tot+b.size>ALL){sk.push({src:s,reason:"总大小超过上限"});continue}
tot+=b.size;ims.push({src:s,data:await rd(b)})}catch(e){sk.push({src:s,reason:"取不回("+e.message+")"})}}
return{type:"kb-clip",v:1,url:U,title:T,fetched_at:F,html:H,images:ims,skipped:sk}})();
const on=async e=>{if(e.source!==w||e.origin!==P||!e.data||e.data.type!=="kb-clip-ready")return;
removeEventListener("message",on);w.postMessage({type:"kb-clip-busy",v:1,images:src.length},P);w.postMessage(await job,P)};
addEventListener("message",on)})()"""

_INSTALL_BODY = """<h1>安装「导入知识库」书签</h1>
<p class="state" id="state"></p>
<ol>
<li>让书签栏显示出来:Chrome / Edge 按 <b>Ctrl+Shift+B</b>(Mac 上 <b>Cmd+Shift+B</b>)。360 / 奇安信浏览器先切到<b>极速模式</b>。</li>
<li>把这个按钮<b>拖到书签栏</b>上:<a class="bm" id="bm" href="#">导入知识库</a>
 <span class="muted">(拖,不要点)</span></li>
<li>拖不动?点 <button id="copy">复制书签代码</button>,在书签栏上右键 → 添加网页 / 添加书签,
名称写「导入知识库」,网址粘贴进去。<textarea id="code" readonly></textarea><span id="copied" class="ok"></span></li>
</ol>
<h2>怎么用</h2>
<p>打开要导入的内网网页(先确认已登录、页面加载完),点书签栏上的「导入知识库」,在弹出的窗口里核对标题和字数后点「导入」。
剪藏页会在您的导入环境里自动新开一个会话,模型按知识库的规矩整理,入库前请您确认。</p>
<p class="muted">点了没反应、或被页面的安全设置拦住:在那个网页上按 Ctrl+S,保存类型选「网页,单个文件(*.mhtml)」,
再打开 <a href="/kb-import/clip">剪藏页</a> 把文件拖进去。</p>"""

_INSTALL_JS = r"""const C=JSON.parse(document.getElementById("cfg").textContent),$=i=>document.getElementById(i);
const st=$("state");
if(C.origins.length){st.textContent="允许剪藏的网站:"+C.origins.join("、")+"。其他网站上点书签会被剪藏页拒收。"}
else{st.textContent="网页剪藏还没有开启:请平台在 agent-config 里配 KB_CLIP_ORIGINS(允许剪藏的内网网站),重启网关后再来。";st.className="state off"}
const code="javascript:"+encodeURIComponent(BM.replace("__P__",JSON.stringify(location.origin)).replace("__M__",C.maxImages)
 .replace("__ONE__",C.oneImage).replace("__ALL__",C.maxBytes));
$("bm").href=code;$("code").value=code;
$("bm").addEventListener("click",e=>{e.preventDefault();alert("请把这个按钮拖到书签栏上,不要在这里点。")});
$("copy").addEventListener("click",async()=>{try{await navigator.clipboard.writeText(code)}catch(e){$("code").select();document.execCommand("copy")}
$("copied").textContent=" 已复制";});"""

_CLIP_BODY = """<h1>导入知识库 · 网页剪藏</h1>
<p id="wait" class="muted">正在接收网页内容…</p>
<div class="card" id="card" hidden><div class="t" id="t"></div><div class="u" id="u"></div><div id="n"></div>
<p><button class="primary" id="go" disabled>导入</button></p></div>
<p id="msg"></p>
<details id="alt"><summary>书签用不了?拖入 .mhtml 文件</summary>
<div id="drop">在那个网页上按 Ctrl+S,保存类型选「网页,单个文件(*.mhtml)」,把保存的文件拖到这里<br>
或 <input type="file" id="file" accept=".mhtml,.mht,.html,.htm"></div></details>"""

_CLIP_JS = r"""const C=JSON.parse(document.getElementById("cfg").textContent),$=i=>document.getElementById(i);
let clip=null,tries=0;const MB=n=>(n/1048576).toFixed(1)+" MB";
const say=(t,c)=>{$("msg").textContent=t;$("msg").className=c||""};
if(!C.origins.length){$("wait").textContent="网页剪藏还没有开启:请平台在 agent-config 里配 KB_CLIP_ORIGINS。";$("alt").hidden=true}
const ping=setInterval(()=>{if(clip||!window.opener||++tries>40){clearInterval(ping);if(!clip)nothing();return}
for(const o of C.origins){try{window.opener.postMessage({type:"kb-clip-ready",v:1},o)}catch(e){}}},500);
function nothing(){if(!C.origins.length)return;$("wait").textContent=window.opener
 ?"没有收到网页内容:来源网页不在允许剪藏的名单里,或页面的安全设置切断了与本窗口的联系。可以改用下面的 .mhtml。"
 :"请在要导入的网页上点书签「导入知识库」打开本页;或者把 .mhtml 文件拖到下面。";$("alt").open=true}
addEventListener("message",e=>{if(clip||e.source!==window.opener||!C.origins.includes(e.origin))return;const d=e.data;
if(d&&d.type==="kb-clip-busy"){clearInterval(ping);$("wait").textContent="已连上网页,正在取页面里的图片(共 "+(+d.images||0)+" 张)…";return}
if(!d||d.type!=="kb-clip"||typeof d.html!=="string")return;let u;try{u=new URL(d.url)}catch(x){return}if(u.origin!==e.origin)return;
clip={kind:"page",url:d.url,title:String(d.title||""),fetched_at:String(d.fetched_at||""),html:d.html,
 images:Array.isArray(d.images)?d.images:[],skipped:Array.isArray(d.skipped)?d.skipped:[]};
const doc=new DOMParser().parseFromString(clip.html,"text/html");doc.querySelectorAll("script,style,noscript,template").forEach(n=>n.remove());
const chars=(doc.body?doc.body.textContent:"").replace(/\s+/g,"").length;
const size=clip.html.length+clip.images.reduce((n,i)=>n+String(i.data||"").length*3/4,0);
show(clip.title||"(无标题)",clip.url,"正文约 "+chars+" 字 · 图片 "+clip.images.length+" 张"
 +(clip.skipped.length?"(另有 "+clip.skipped.length+" 张没取回,需要时请截图上传)":"")+" · "+MB(size),size)});
function show(t,u,n,size){$("wait").hidden=true;$("card").hidden=false;$("t").textContent=t;$("u").textContent=u;$("n").textContent=n;
if(size>C.maxBytes){say("超过单次剪藏 "+MB(C.maxBytes)+" 的上限:请把页面拆开分别剪藏","err");return}$("go").disabled=false;say("")}
$("go").addEventListener("click",async()=>{$("go").disabled=true;say("正在导入…(导入环境没在运行时要先启动它,最多等两分钟)");let r,b;
try{r=await fetch(C.upload,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-KB-Clip":"1"},
 body:JSON.stringify(clip)});b=await r.json().catch(()=>({}))}catch(e){say("上传失败:网络中断,请再点一次「导入」","err");$("go").disabled=false;return}
if(!r.ok){say(b.error||("上传失败("+r.status+")"),"err");$("go").disabled=false;return}
if(b.open_url){say("已存进知识库收件目录(批次 "+b.batch+"),已在导入环境里新开会话发出「导入剪藏」。正在打开这个会话…","ok");
 setTimeout(()=>{try{window.resizeTo(1280,880)}catch(e){}location.href=b.open_url},1500)}
else{say("已存进知识库收件目录(批次 "+b.batch+")。没能自动发起导入:请在导入环境里说「导入剪藏 clips/"+b.batch+"」","ok")}});
const drop=$("drop");["dragenter","dragover"].forEach(k=>drop.addEventListener(k,e=>{e.preventDefault();drop.classList.add("on")}));
drop.addEventListener("dragleave",()=>drop.classList.remove("on"));
drop.addEventListener("drop",e=>{e.preventDefault();drop.classList.remove("on");pick(e.dataTransfer.files[0])});
$("file").addEventListener("change",e=>pick(e.target.files[0]));
async function pick(f){if(!f)return;if(!/\.(mhtml|mht|html|htm)$/i.test(f.name)){say("只收 .mhtml / .mht / .html / .htm","err");return}
if(f.size>C.maxBytes){say("文件超过 "+MB(C.maxBytes)+" 上限","err");return}clearInterval(ping);
const a=new Uint8Array(await f.arrayBuffer());let s="";for(let i=0;i<a.length;i+=32768)s+=String.fromCharCode.apply(null,a.subarray(i,i+32768));
clip={kind:"file",file_name:f.name,data:btoa(s)};show(f.name,"拖入的文件","大小 "+MB(f.size),f.size)}"""


def install_page(origins: Sequence[str], max_bytes: int, nonce: str) -> bytes:
    script = "const BM=%s;\n%s" % (json.dumps(BOOKMARKLET).replace("<", "\\u003c"), _INSTALL_JS)
    return _page("安装「导入知识库」书签", _INSTALL_BODY, origins, max_bytes, nonce, script)


def clip_page(origins: Sequence[str], max_bytes: int, nonce: str) -> bytes:
    return _page("导入知识库 · 网页剪藏", _CLIP_BODY, origins, max_bytes, nonce, _CLIP_JS)
