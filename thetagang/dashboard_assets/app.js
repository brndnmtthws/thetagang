"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let overview = null, detail = null, selected = null, page = 1, tab = "overview", requestVersion = 0;
let zone = "Asia/Shanghai";
const number = (v, digits=0) => v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toLocaleString("zh-CN", {maximumFractionDigits:digits, minimumFractionDigits:digits});
const money = (v, currency="USD", digits=0) => v == null ? "—" : `${currency === "USD" ? "$" : esc(currency || "")} ${number(v,digits)}`;
const time = (v, short=false) => !v ? "—" : new Intl.DateTimeFormat("zh-CN", {timeZone:zone, month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit", ...(short ? {} : {year:"numeric", second:"2-digit"}),hourCycle:"h23"}).format(new Date(v));
const badge = (label, state="") => `<span class="badge ${esc(state)}">${esc(label)}</span>`;
const panel = (title, body, meta="") => `<div class="panel"><div class="panel-head"><h2>${esc(title)}</h2><small>${esc(meta)}</small></div>${body}</div>`;
const empty = (title, text) => `<div class="empty"><strong>${esc(title)}</strong>${esc(text)}</div>`;
const eventNames = {run_start:"策略开始执行",run_end:"本次运行结束",batch_risk_checked:"订单批次检查完成",order_enqueued:"订单已加入计划",strategy_decision:"策略判断已记录",run_skipped:"交易时段检查未通过",order_submission_unknown:"订单提交结果待核实",order_price_adjustment_skipped:"本次改价已跳过"};
const stageNames = {put_write:"卖出 Put",call_write:"卖出 Call",option_scan:"合约筛选",vix_hedge:"VIX 对冲",cash_management:"现金管理",option_manage:"展期与平仓"};
const metricNames = {target_shares:"目标股数",current_shares:"持有股数",target_value:"目标金额",target_weight:"目标权重",contracts_needed:"待补合约",quantity:"计划数量",max_contracts:"单次上限",market_price:"标的价格",close_price:"昨收价格",threshold:"触发阈值",daily_change:"日内变动",strike_limit:"行权价边界",strike:"行权价",delta:"Delta",dte:"剩余天数",limit_price:"限价",weight:"对冲比例",vixmo:"VIXMO",allocation_amount:"对冲预算",cash_balance:"现金余额",pending_balance:"待支付金额",effective_cash_balance:"规划后现金",stock_count:"持股数量"};

async function api(path) {
  const response = await fetch(path, {cache:"no-store"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "读取失败，请稍后重试。");
  return data;
}
function error(message) { $("error").textContent = message; $("error").hidden = false; }
function renderRuns() {
  $("run-count").textContent = overview.total;
  $("page-number").textContent = `${overview.page} / ${overview.pages}`;
  $("previous").disabled = overview.page <= 1;
  $("next").disabled = overview.page >= overview.pages;
  $("run-list").innerHTML = overview.runs.map(run => `<button class="run-item ${run.id === selected ? "selected" : ""}" data-run="${run.id}" ${run.id===selected?'aria-current="true"':''}><div class="run-item-top"><span>${esc(time(run.started_at,true))}</span><span class="run-number">#${run.id}</span></div><small>${esc(run.mode)} · ${run.intents_count} 笔计划</small><div class="run-item-status">${esc(run.status_label)}</div></button>`).join("");
}
async function loadRuns(keep=true) {
  const version = ++requestVersion;
  $("refresh").disabled = true; $("error").hidden = true;
  try {
    const result = await api(`/api/runs?page=${page}&mode=${$("mode").value}`);
    if (version !== requestVersion) return;
    overview = result; page = result.page;
    if (!result.runs.length) { $("content").hidden = true; $("loading").hidden = false; $("loading").textContent = "还没有运行记录。交易程序启用数据库后，记录会出现在这里。"; renderRuns(); return; }
    if (!keep || !result.runs.some(r=>r.id===selected)) {
      selected = result.runs.find(r=>r.orders_count || r.intents_count)?.id ?? result.runs[0].id;
    }
    renderRuns();
    await loadDetail(selected);
  } catch(e) { error(e.message); $("loading").hidden=true; }
  finally { $("refresh").disabled=false; }
}
async function loadDetail(id) {
  const version = ++requestVersion;
  selected = id; renderRuns(); $("loading").hidden=false; $("content").hidden=true;
  try {
    const result = await api(`/api/runs/${id}`);
    if(version!==requestVersion) return;
    detail=result; $("error").hidden=true; render();
    $("content").hidden=false;
  } catch(e) { if(version===requestVersion) error(e.message); }
  finally { if(version===requestVersion) $("loading").hidden=true; }
}
function notice(run) {
  if (run.status === "working") return "运行结束时仍有订单在券商处挂单。下面展示的是最后一次记录，当前状态需到 IBKR 核实。";
  if (run.status === "unrecorded") return "本次只有启动记录或缺少结束事件，无法判断最终结果，也不能据此判断程序仍在运行。";
  if (run.status === "skipped") return "本次未进入配置允许的交易时段，未执行策略。";
  if (run.status === "failed" || run.status === "partial_failure" || run.status === "unknown") return `本次未正常完成，请结合订单状态与终端日志核实。${run.error_type ? "异常类型："+run.error_type : "历史记录未保存具体异常原因。"}`;
  return run.dry_run ? "本次为预演，订单计划不会通过预演流程提交给券商。" : "这是一次可下单运行；记录未单独标识 IBKR 模拟账户或实盘账户，请以账户设置为准。";
}
function render() {
  const {run, account} = detail;
  $("selected-id").textContent=`运行 #${run.id}`;
  $("run-title").textContent=`运行 #${run.id} · ${time(run.started_at,true)}`;
  $("run-meta").textContent=`${run.mode}　 /　${run.duration==null ? "耗时未记录" : `用时 ${number(run.duration/60,1)} 分钟`}　 /　${run.orders_count} 条订单提交记录`;
  $("export").href=`/api/runs/${run.id}/export`;
  $("run-notice").innerHTML=badge(run.status_label,run.status)+`<span>${esc(notice(run))}</span>`;
  const get = key => account[key]?.value;
  const curr = key => account[key]?.currency || "";
  const margin=get("NetLiquidation")!=null && detail.settings.margin_usage!=null ? Math.floor(get("NetLiquidation")*detail.settings.margin_usage) : null;
  $("metrics").innerHTML=[
    ["账户净值",money(get("NetLiquidation"),curr("NetLiquidation")),"本次账户快照"],
    ["现金余额",money(get("TotalCashValue"),curr("TotalCashValue")),"不含尚未成交订单的预计收支",get("TotalCashValue")<0],
    ["维持保证金",money(get("MaintMarginReq"),curr("MaintMarginReq")),`剩余流动性 ${money(get("ExcessLiquidity"),curr("ExcessLiquidity"))}`],
    ["Wheel 配置资金基数",money(margin,curr("NetLiquidation")),detail.settings.margin_usage==null ? "历史配置未记录" : `净值 × ${number(detail.settings.margin_usage*100)}%`]
  ].map(([label,value,foot,negative])=>`<div class="metric"><div class="metric-label">${label}</div><div class="metric-value ${negative?'negative':''}">${value}</div><div class="metric-foot">${foot}</div></div>`).join("");
  $("order-count").textContent=detail.orders.length;
  $("view-overview").innerHTML=renderOverview();
  $("view-orders").innerHTML=renderOrders(true)+renderExecutions();
  $("view-positions").innerHTML=renderPositions();
  $("view-decisions").innerHTML=renderDecisions();
  $("snapshot-info").textContent=`账户快照 ${time(detail.snapshot_at)} · ${$("timezone").selectedOptions[0].text}`;
  $("refresh-info").textContent=`最近读取 ${time(detail.read_at,true)}`;
  switchTab(tab);
}
function renderOverview() {
  const orders=detail.orders;
  const filled=orders.filter(o=>o.status==="Filled").length;
  const working=orders.filter(o=>["Submitted","PreSubmitted","PendingSubmit","PendingCancel"].includes(o.status)).length;
  const summary=`<div class="panel-body"><div class="status-cards"><div class="status-card"><span>订单计划</span><strong>${orders.length}</strong></div><div class="status-card"><span>已记录全部成交</span><strong>${filled}</strong></div><div class="status-card"><span>最后记录为挂单</span><strong>${working}</strong></div></div>${orders.length?`<ul class="summary-list">${orders.map(o=>`<li><span class="symbol-icon">${esc(o.symbol.substring(0,2))}</span><div><strong>${esc(o.symbol)}</strong>${esc(orderPurpose(o))}<div class="muted">${o.action==="BUY"?"买入":"卖出"} ${number(o.quantity)} ${o.sec_type==="STK"?"股":"张"} · 限价 ${money(o.limit_price,o.currency,2)}</div></div></li>`).join("")}</ul>`:`<p class="caption">本次没有已留存的订单计划。</p>`}</div>`;
  const events=detail.events.filter(e=>e.type!=="strategy_decision");
  const timeline=events.length?`<div class="panel-body"><ol class="timeline">${events.map(e=>`<li>${esc(eventNames[e.type] || e.type)}${e.symbol ? ` · ${esc(e.symbol)}` : ""}<small>${esc(time(e.time))}</small></li>`).join("")}</ol></div>`:empty("未留存执行阶段","这次启动没有完整的过程记录。");
  return `<div class="grid"><div>${panel("本次订单摘要",summary,"以最后记录为准")}${renderOrders(false)}</div><div>${panel("配置权重与股票持仓",renderWeights(),"Wheel 资金基数")}${panel("运行时间线",timeline,`${events.length} 个事件`)}</div></div>`;
}
function orderPurpose(o) {
  if(o.symbol==="VIX" && o.right==="C") return `波动率对冲 Call · ${number(o.strike,2)} · ${formatExpiry(o.expiry)}`;
  if(o.sec_type==="STK") return "股票交易";
  return `${o.right==="P"?"Put":o.right==="C"?"Call":"期权"} · ${number(o.strike,2)} · ${formatExpiry(o.expiry)}`;
}
function formatExpiry(expiry) { return /^\d{8}$/.test(expiry || "") ? `${expiry.slice(0,4)}-${expiry.slice(4,6)}-${expiry.slice(6,8)}` : expiry || "到期日未记录"; }
function renderWeights() {
  const weights=detail.settings.weights;
  const nlv=detail.account.NetLiquidation?.value, margin=detail.settings.margin_usage;
  const base=nlv!=null && margin!=null ? Math.floor(nlv*margin) : null;
  if(!Object.keys(weights).length || !(base>0)) return empty("缺少配置权重","本次历史配置或资金基数未留存。");
  const rows=Object.entries(weights).map(([symbol,target])=>{
    const values=detail.positions.filter(p=>p.symbol===symbol && p.sec_type==="STK");
    const valid=detail.snapshot_at && values.every(p=>p.market_value!=null);
    const current=valid ? values.reduce((s,p)=>s+p.market_value,0)/base : null;
    const width=v=>Math.max(0,Math.min(100,(v||0)*100));
    return `<div class="weight-row"><strong>${esc(symbol)}</strong><svg viewBox="0 0 100 18" preserveAspectRatio="none" aria-label="${esc(symbol)} 配置 ${number(target*100,1)}%，股票 ${current==null?'未记录':number(current*100,1)+'%'}"><rect x="0" y="2" width="${width(target)}" height="5" rx="2" fill="#d9e3fc"/><rect x="0" y="11" width="${width(current)}" height="5" rx="2" fill="#2858e8"/></svg><span>${current==null?'—':number(current*100,1)+'%'}<br><small>${target==null?'—':number(target*100,1)+'%'}</small></span></div>`;
  }).join("");
  return `<div class="panel-body"><div class="weights">${rows}</div><div class="legend"><span><i></i>股票持仓</span><span><i class="target"></i>配置目标</span></div><p class="caption">占比按策略资金基数计算，仅比较股票市值；未合并期权敞口。</p></div>`;
}
function renderOrders(expanded) {
  const rows=detail.orders;
  if(!rows.length) return panel("订单记录",empty("本次没有订单记录","未留存订单不等于已确认没有券商交易，请结合运行结果判断。"));
  const table=`<div class="table-scroll"><table><thead><tr><th>标的 / 合约</th><th>方向</th><th class="number">数量</th><th class="number">限价</th><th>最后状态</th><th class="number">成交 / 计划</th></tr></thead><tbody>${rows.map(o=>`<tr><td class="symbol">${esc(o.symbol)}<small>${esc(orderPurpose(o))}</small></td><td>${o.action==="BUY"?"买入":"卖出"}</td><td class="number">${number(o.quantity)}</td><td class="number">${money(o.limit_price,o.currency,2)}</td><td>${badge((o.partial?"部分成交 · ":"")+o.status_label,o.status)}<small>${esc(time(o.observed_at,true))}</small></td><td class="number">${number(o.filled)} / ${number(o.quantity)}</td></tr>`).join("")}</tbody></table></div>`;
  const timelines=expanded?rows.map(o=>`<details><summary>${esc(o.symbol)} · ${esc(orderPurpose(o))} · 查看状态变化</summary><p class="code-ref">订单引用 ${esc(o.order_ref)}　/　订单号 ${esc(o.order_id)}</p>${o.planned_at || o.submitted_at?`<ol class="timeline">${o.planned_at?`<li>生成订单计划<small>${esc(time(o.planned_at))}</small></li>`:""}${o.submitted_at?`<li>已记录提交<small>${esc(time(o.submitted_at))}</small></li>`:""}</ol>`:""}${o.timeline.length?`<ol class="timeline">${o.timeline.map(s=>`<li>${badge(s.label,s.status)}　成交 ${number(s.filled)}，剩余 ${number(s.remaining)}<small>${esc(time(s.time))}</small></li>`).join("")}</ol>`:empty("尚无状态回报","记录中只有订单计划或提交信息。")}${o.executions.length?`<p class="caption">已关联成交：${o.executions.map(f=>`${esc(time(f.time))} · ${number(f.quantity)} @ ${number(f.price,2)}`).join("；")}</p>`:""}</details>`).join(""):"";
  return panel("订单与状态",table+timelines,"历史状态 · 手动刷新记录");
}
function renderExecutions() {
  const rows=detail.observed_executions;
  if(!rows.length) return panel("本轮收到的成交回报",empty("没有收到成交回报","程序结束后的成交可能尚未同步到本地，请到 IBKR 核实。"));
  return panel("本轮收到的成交回报",`<div class="panel-body"><p class="caption">这些回报可能属于此前运行的订单；收到回报的轮次不等于创建订单的轮次。</p></div><div class="table-scroll"><table><thead><tr><th>成交时间</th><th>标的</th><th>方向</th><th class="number">数量</th><th class="number">价格</th><th>订单号</th></tr></thead><tbody>${rows.map(e=>`<tr><td>${esc(time(e.execution_time))}</td><td>${esc(e.symbol)}</td><td>${e.side==="BOT"?"买入":e.side==="SLD"?"卖出":esc(e.side)}</td><td class="number">${number(e.shares)}</td><td class="number">${number(e.price,2)}</td><td>${esc(e.order_id)}</td></tr>`).join("")}</tbody></table></div>`);
}
function renderPositions() {
  const rows=detail.positions;
  if(!rows.length) return panel("持仓快照",empty("本次未留存持仓","程序可能在获取账户数据前退出。"));
  return panel("持仓快照",`<div class="table-scroll"><table><thead><tr><th>标的 / 合约</th><th>类别</th><th class="number">持仓数量</th><th class="number">市价</th><th class="number">市值</th><th class="number">未实现盈亏</th></tr></thead><tbody>${rows.map(p=>`<tr><td class="symbol">${esc(p.symbol)}<small>${p.sec_type==="OPT"?`${esc(formatExpiry(p.expiry))} · ${number(p.strike,2)} ${esc(p.right)}`:(p.symbol in detail.settings.weights?"配置标的":"账户其他持仓")}</small></td><td>${p.sec_type==="STK"?"股票 / ETF":p.sec_type==="OPT"?"期权":esc(p.sec_type)}</td><td class="number">${number(p.position,2)}</td><td class="number">${money(p.market_price,p.currency,2)}</td><td class="number">${money(p.market_value,p.currency)}</td><td class="number ${p.unrealized_pnl<0?'negative':p.unrealized_pnl>0?'positive':''}">${money(p.unrealized_pnl,p.currency)}</td></tr>`).join("")}</tbody></table></div>`,time(rows[0].created_at));
}
function renderDecisions() {
  const rows=detail.decisions;
  if(!rows.length) return panel("策略决策",empty("这次运行未留存详细决策原因","订单与配置快照仍可复盘。升级后的新运行会记录目标仓位、触发门槛和跳过原因；历史原因不会自动补造。"));
  return panel("策略决策",rows.map(d=>`<article class="decision"><strong>${esc(d.symbol || "账户")}</strong><div>${badge(d.outcome==="eligible"?"通过门槛":d.outcome==="planned"?"已规划":d.outcome==="error"?"数据异常":"跳过",d.outcome==="error"?"failed":"")} <span>${esc(stageNames[d.stage]||d.stage)}</span><p>${esc(d.reason)}</p><div class="decision-metrics">${Object.entries(d.metrics||{}).map(([key,value])=>`<span>${esc(metricNames[key]||key)}：${typeof value==="number"?number(value,Math.abs(value)<1?4:2):esc(value)}</span>`).join("")}</div></div><small>${esc(time(d.time,true))}</small></article>`).join(""),`${rows.length} 条判断`);
}
function switchTab(name) { tab=name; document.querySelectorAll("[data-tab]").forEach(b=>{b.classList.toggle("active",b.dataset.tab===name);if(b.dataset.tab===name)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current");}); for(const id of ["overview","orders","positions","decisions"]) $("view-"+id).hidden=id!==name; }
$("run-list").addEventListener("click",e=>{const button=e.target.closest("[data-run]");if(button)loadDetail(Number(button.dataset.run));});
document.querySelector(".tabs").addEventListener("click",e=>{const b=e.target.closest("[data-tab]");if(b)switchTab(b.dataset.tab);});
$("refresh").addEventListener("click",()=>loadRuns(true));
$("mode").addEventListener("change",()=>{page=1;loadRuns(false);});
$("previous").addEventListener("click",()=>{page--;loadRuns(false);});
$("next").addEventListener("click",()=>{page++;loadRuns(false);});
$("timezone").addEventListener("change",()=>{zone=$("timezone").value;if(overview)renderRuns();if(detail)render();});
loadRuns(false);
