'use strict';
let creditConfirmation = null;
const creditRequests = new Set();

function buildCreditControls(card, label) {
  const area=document.createElement('section');
  area.className='credit-controls';area.setAttribute('aria-label',`账户 ${label} 的重置券`);
  area.innerHTML=`<div class="credit-heading"><span>重置券 <small>RESET</small></span><strong data-credit="count">未查询</strong></div><p data-credit="detail">查询后查看可用数量和有效期。</p><div class="credit-actions"><button type="button" class="button" data-credit="query">查询券</button><button type="button" class="button" data-credit="consume" disabled>使用重置券</button></div><button type="button" class="button credit-resolve" data-credit="resolve" hidden>结束上次确认</button><p class="credit-updated" data-credit="updated">只在手动确认后用券</p>`;
  area.querySelector('[data-credit="query"]').addEventListener('click',()=>queryCredits(label));
  area.querySelector('[data-credit="consume"]').addEventListener('click',()=>confirmCredit(label));
  area.querySelector('[data-credit="resolve"]').addEventListener('click',()=>resolveCredit(label));
  card.querySelector('.account-settings').before(area);
  card.querySelector('.account-settings-body').append(area.querySelector('[data-credit="updated"]'));
}

function renderCreditControls(row) {
  const card=$(`account-${row.label}`),credit=row.reset_credits;
  const field=name=>card.querySelector(`[data-credit="${name}"]`);
  const busy=credit?.busy || creditRequests.has(row.label);
  const count=credit?.snapshot?.available_count;
  const stale=Boolean(credit?.stale||!credit?.fetched_at||now()-credit.fetched_at>300||credit?.snapshot?.expires_at.some(expiry=>expiry<=now()));
  field('count').textContent=typeof count==='number'?`${count} 张${stale?' · 待更新':''}`:'未查询';
  field('query').disabled=Boolean(busy)||!row.registered||offline;
  field('query').textContent=busy?(credit?.busy==='consume'?'正在重置…':'查询中…'):'查询券';
  field('consume').disabled=Boolean(busy)||!row.registered||offline||!credit||stale||(!(count>0)&&!credit.pending_request_id);
  field('consume').textContent=credit?.pending_request_id?'确认上次重置':'使用重置券';
  field('resolve').hidden=!credit?.pending_request_id;
  field('resolve').disabled=Boolean(busy)||!row.registered||offline;
  let detail='查询后查看可用数量和有效期。';
  if(typeof count==='number') {
    const dates=credit.snapshot.expires_at;
    detail=count===0?'当前没有可用重置券。':dates.length?`最近到期：${dateFormat.format(new Date(dates[0]*1000))}${dates.length<count?' · 部分有效期未知':''}`:'上游未提供券的有效期。';
  }
  if(credit?.error)detail=credit.error.message+(credit.snapshot?' 显示上次查询结果。':'');
  if(credit?.last_reset?.status==='succeeded')detail=(credit.last_reset.code==='already_redeemed'?'上次请求已完成，无需再次用券。':credit.last_reset.windows_reset>0?`上次重置已确认，恢复 ${credit.last_reset.windows_reset} 个额度窗口。`:'上次重置已确认，请查看最新额度。')+detail;
  if(credit?.last_reset?.code==='nothing_to_reset')detail='上次未用券：当前没有可重置的额度窗口。'+detail;
  if(credit?.last_reset?.code==='no_credit')detail='上次未用券：上游确认没有可用重置券。'+detail;
  if(credit?.last_reset?.resolution==='manually_closed')detail='上次结果仍未知，已结束本地确认并保留记录。'+detail;
  if(credit?.pending_request_id)detail='上次用券结果未确认。可查询后沿用原编号重试；若确认一直失败，可结束上次确认，再查询券后另行用券。';
  field('detail').textContent=detail;
  field('updated').textContent=credit?.fetched_at?`查券 ${clockFormat.format(new Date(credit.fetched_at*1000))} · ${routeName(credit.mode)}`:'只在手动确认后用券';
  field('count').title=field('updated').textContent;
  card.querySelector('.credit-controls').classList.toggle('credit-warning',Boolean(credit?.error||credit?.pending_request_id));
}

async function queryCredits(label) {
  creditRequests.add(label);if(state)render();
  try {
    const result=await api('/api/credits/query',{account:label});
    if(!result.accepted&&!result.busy)toast('刚刚查询过，请稍等几秒。');
  } catch(error){toast(error.message);}
  finally {creditRequests.delete(label);await poll();if(state)render();}
}

function confirmCredit(label) {
  const row=state?.accounts.find(item=>item.label===label),credit=row?.reset_credits;
  if(!credit||credit.busy||creditRequests.has(label)||credit.stale||now()-(credit.fetched_at||0)>300||(!(credit.snapshot?.available_count>0)&&!credit.pending_request_id)){toast('请先查询此账户的可用重置券。');return;}
  creditConfirmation={label,kind:credit.pending_request_id?'retry':'consume',revision:credit.revision,request_id:credit.pending_request_id||crypto.randomUUID()};
  renderCreditConfirmation();
  $('credit-confirm').showModal();$('credit-cancel').focus();
}

function resolveCredit(label) {
  const row=state?.accounts.find(item=>item.label===label),credit=row?.reset_credits;
  if(!credit?.pending_request_id||credit.busy||creditRequests.has(label)||!row.registered||offline)return;
  creditConfirmation={label,kind:'resolve',request_id:credit.pending_request_id};
  renderCreditConfirmation();
  $('credit-confirm').showModal();$('credit-cancel').focus();
}

function renderCreditConfirmation() {
  if(!creditConfirmation)return;
  const {label,kind}=creditConfirmation,row=state?.accounts.find(item=>item.label===label),credit=row?.reset_credits;
  if(!row||!credit)return;
  const accountName=accountIdentityText(row,showAccountNames).label;
  $('credit-confirm-title').textContent=`${kind==='resolve'?'结束上次确认':kind==='retry'?'确认上次重置':'使用重置券'} · 账户 ${label}`;
  $('credit-confirm-text').textContent=`${accountName} 最近查到 ${credit.snapshot?.available_count??'未知'} 张可用券。${kind==='resolve'?'上次用券结果仍未知，结束后将保留原记录，停止重试旧请求。':kind==='retry'?'本次会重新提交上次用券请求，沿用原编号。':'本次将使用 1 张重置 Codex 额度。'}`;
  $('credit-confirm-note').textContent=kind==='resolve'?'此操作只结束本地确认，不消耗券，也不代表上次重置成功。结束后需重新查询券，再决定是否另用一张。':kind==='retry'?'这会再次请求上游处理原编号。若确认一直失败，可取消后选择“结束上次确认”。':'确认后会消耗此账户的 1 张券，无法撤销。实际恢复的额度窗口以上游返回和重新查询为准。';
  $('credit-redeem').textContent=kind==='resolve'?'结束确认':kind==='retry'?'重试原请求':'确认使用 1 张';
}

function initializeCreditDialog() {
  $('credit-cancel').addEventListener('click',()=>{$('credit-confirm').close();creditConfirmation=null;});
  $('credit-confirm').addEventListener('cancel',()=>{creditConfirmation=null;});
  $('credit-redeem').addEventListener('click',async()=>{
    if(!creditConfirmation)return;
    const operation=creditConfirmation;creditConfirmation=null;$('credit-confirm').close();
    creditRequests.add(operation.label);if(state)render();
    try {
      const resolving=operation.kind==='resolve';
      const body={account:operation.label,confirmed:true,request_id:operation.request_id};
      if(!resolving)body.revision=operation.revision;
      const result=await api(resolving?'/api/credits/resolve':'/api/credits/consume',body);
      toast(result.accepted?(resolving?'已结束上次确认。请重新查询券后，再决定是否使用。':`账户 ${operation.label} 正在重置，请留意卡片结果。`):'此请求已处理或正在处理，请查看卡片。');
    }catch(error){toast(error.message+' 请先查看卡片结果。');}
    finally{creditRequests.delete(operation.label);await poll();if(state)render();}
  });
}
