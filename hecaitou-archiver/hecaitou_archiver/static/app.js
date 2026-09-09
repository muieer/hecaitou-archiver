"use strict";
const $ = id => document.getElementById(id);
const fields = [["what","主题"],["why","原因"],["how","方法"],["which","对象与选择"],["who","人物"],["where","地点与场景"],["when","时间"]];
let state = null, busy = false, refreshing = false, connected = false, dirty = false, articleSignature = "", noticeTimer, revision = 0, actionError = "";
const formatTime = value => value ? new Date(value).toLocaleString("zh-CN",{month:"long",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:false}) : "—";
function notice(message){$("notice").textContent=message;$("notice").hidden=false;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>{$("notice").hidden=true;},4500);}
function problem(message){$("problem").textContent=message||"";$("problem").hidden=!message;}
function controls(){
  $("daily-time").disabled=busy||!connected;
  $("save-time").disabled=busy||!connected||!dirty;
  $("enabled").disabled=busy||!connected;
  $("run-now").disabled=busy||!connected||!!state?.running;
  $("run-label").textContent=state?.running?"正在执行…":"立即执行一次";
}
function renderArticle(article){
  const signature=JSON.stringify(article);
  if(signature===articleSignature)return;
  articleSignature=signature;
  $("reading-grid").hidden=!article;$("empty-state").hidden=!!article;
  if(!article){$("article-date").textContent="暂无存档";$("empty-title").textContent="还没有文章";$("empty-text").textContent="点击“立即执行一次”，或开启每日调度，等待当天的新文章。";return;}
  $("article-date").textContent=article.published_date||"发布日期未知";
  $("article-title").textContent=article.title;
  $("source-link").hidden=!article.source_url;
  if(article.source_url)$("source-link").href=article.source_url;
  // This HTML is rendered and allowlist-sanitized by the local backend.
  $("article-body").innerHTML=article.html||"";
  $("body-error").hidden=!article.body_error;$("body-error").textContent=article.body_error||"";
  $("model-label").textContent=article.model?`云端模型 · ${article.model}`:"";
  $("analysis-message").hidden=article.analysis_state==="ready";
  $("analysis-message").textContent=article.analysis_message||"";
  $("analysis-list").replaceChildren();
  if(article.analysis){for(const [key,label] of fields){
    const item=document.createElement("div");item.className="analysis-item";
    const term=document.createElement("dt"),en=document.createElement("span"),value=document.createElement("dd");
    term.textContent=label;en.textContent=key;term.append(en);value.textContent=article.analysis[key];
    if(article.analysis[key]==="无")value.className="missing";
    item.append(term,value);$("analysis-list").append(item);
  }}
}
function render(data){
  state=data;
  $("enabled").checked=data.config.enabled;
  $("schedule-label").textContent=data.config.enabled?"调度开启中":"调度关闭中";
  if(!dirty)$("daily-time").value=data.config.time;
  $("timezone").textContent=`系统时区 ${data.timezone} · UTC${data.utc_offset.slice(0,3)}:${data.utc_offset.slice(3)}`;
  $("next-run").textContent=data.next_run?formatTime(data.next_run):"调度未开启";
  $("schedule-note").textContent=data.config.enabled?"错过时刻不补跑":"开启后，每天执行一次";
  const run=data.current_run||data.last_run;
  $("run-badge").className="status-badge "+(run?.status||"");
  $("run-badge").textContent=run?({saved:"成功产生结果",skipped:"正常跳过",failed:"执行失败",running:"执行中"}[run.status]||run.status):"尚未执行";
  $("run-message").textContent=run?.message||"首次运行后，这里会显示结果。";
  $("run-time").textContent=run?`${run.trigger==="manual"?"手动":"自动"} · ${formatTime(run.started)}`:"";
  problem(data.scheduler_error||actionError||(data.warnings||[]).join("；"));
  if(Object.hasOwn?Object.hasOwn(data,"article"):Object.prototype.hasOwnProperty.call(data,"article"))renderArticle(data.article);
  controls();
}
async function request(path, body){
  const response=await fetch(path,body===undefined?{cache:"no-store"}:{method:"POST",headers:{"Content-Type":"application/json","X-Local-Client":"1"},body:JSON.stringify(body)});
  const data=await response.json();if(!response.ok)throw new Error(data.error||"请求失败");return data;
}
async function refresh(){
  if(busy||refreshing)return;refreshing=true;const version=revision;
  try{const data=await request("/api/state");if(version!==revision)return;connected=true;$("connection-dot").className="connection-dot online";$("connection-label").textContent="本地服务运行中";render(data);}
  catch(error){if(version!==revision)return;connected=false;$("connection-dot").className="connection-dot";$("connection-label").textContent="本地服务未连接";problem("无法连接本地服务，调度状态暂时无法确认。请检查服务是否仍在运行。");}
  finally{refreshing=false;controls();}
}
async function mutate(path,body,message){
  if(busy)return;busy=true;revision++;actionError="";controls();
  try{const data=await request(path,body);if(path==="/api/config"&&body.time)dirty=false;render(data);notice(message);}
  catch(error){actionError=error.message;problem(actionError);if(state)$("enabled").checked=state.config.enabled;}
  finally{busy=false;controls();await refresh();}
}
$("daily-time").addEventListener("input",()=>{dirty=true;controls();});
$("time-form").addEventListener("submit",event=>{event.preventDefault();if($("time-form").reportValidity())mutate("/api/config",{time:$("daily-time").value},"每日执行时间已保存");});
$("enabled").addEventListener("change",()=>{const enabled=$("enabled").checked;mutate("/api/config",{enabled},enabled?"每日调度已开启":"每日调度已关闭，当前执行中的任务会正常完成");});
$("run-now").addEventListener("click",()=>mutate("/api/run",{},"已开始执行，结果会自动更新"));
refresh();setInterval(refresh,3000);
