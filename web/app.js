'use strict';
const $ = id => document.getElementById(id);
const refreshIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7a8 8 0 1 0 1 7M20 2v5h-5"/></svg>';
let state = null;
let offset = 0;
let offline = false;
let toastTimer;
let polling = false;
let pollAgain = false;
let checkingActivity=false;
let subscriptionEditor=null;
let changingNetwork = false;
const changingPool = new Set();
const changingThreshold = new Set();
let changingBatch = false;
let sortMode='reset';
let showAccountNames=true;
try {
  const saved=localStorage.getItem('codex-quota-sort');
  if(['reset','five_hour','weekly','updated'].includes(saved))sortMode=saved;
} catch (_) {}
try{showAccountNames=localStorage.getItem('codex-quota-show-names')!=='0';}catch(_){}
const routeName = mode => mode === 'direct' ? 'WireGuard／系统网络' : mode === 'clash' ? 'Clash' : '原路线';
const now = () => Date.now() / 1000 + offset;
const dateFormat = new Intl.DateTimeFormat('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
const clockFormat = new Intl.DateTimeFormat('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const fullDateFormat = new Intl.DateTimeFormat('zh-CN', {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});

function accountIdentityText(row,visible=true) {
  const base=`账户 ${row.label}`,name=visible&&row.display_name?row.display_name:base;
  return {name,email:visible&&row.email&&row.email!==name?row.email:'',
    label:visible&&row.display_name?`${base} · ${row.display_name}`:base};
}

function quotaAvailability(row,connectionOffline=false) {
  const windows={};
  for(const [name,threshold] of [['five_hour',30],['weekly',20]]) {
    const item=row.snapshot?.[name],value=item?.remaining;
    const known=typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=100;
    const notApplicable=item?.applicable===false;
    windows[name]={value:known&&!notApplicable?value:null,known:known&&!notApplicable,
      tone:notApplicable?'not-applicable':!known?'unknown':value<threshold?'low':'good'};
  }
  const applicable=Object.values(windows).filter(item=>item.tone!=='not-applicable');
  if(connectionOffline||!row.registered||row.refreshing||row.error||row.stale||!row.snapshot||
      !applicable.length||applicable.some(item=>!item.known)) {
    return {kind:'pending',reason:'额度待确认',windows};
  }
  const reasons=[];
  if(windows.five_hour.tone==='low')reasons.push('五小时低于30%');
  if(windows.weekly.tone==='low')reasons.push('周额度低于20%');
  const exhausted=applicable.some(item=>item.value===0);
  return {kind:exhausted?'exhausted':reasons.length?'low':'ready',
    reason:reasons.length?reasons.join(' · '):'五小时≥30%且周额度≥20%（无此限额除外）',windows};
}

function refreshIdentityDialogs() {
  if(subscriptionEditor) {
    const row=state?.accounts.find(item=>item.label===subscriptionEditor.label);
    if(row){const display=accountIdentityText(row,showAccountNames);$('subscription-account').textContent=[display.name,display.email].filter(Boolean).join(' · ');}
  }
  if(creditConfirmation&&$('credit-confirm').open)renderCreditConfirmation();
}

function setNameVisibility(visible,persist=true) {
  showAccountNames=Boolean(visible);$('show-account-names').checked=showAccountNames;
  if(persist){try{localStorage.setItem('codex-quota-show-names',showAccountNames?'1':'0');}catch(_){}}
  if(state)render();refreshIdentityDialogs();
}

function sortAccounts(accounts, currentTime, mode='reset') {
  const key = row => {
    if(mode==='five_hour'||mode==='weekly') {
      const item=row.snapshot?.[mode],value=item?.remaining;
      if(item?.applicable!==false&&(typeof value!=='number'||!Number.isFinite(value)||value<0||value>100))return [2,0,0];
      const other=row.snapshot?.[mode==='five_hour'?'weekly':'five_hour']?.remaining;
      const tie=typeof other==='number'&&Number.isFinite(other)&&other>=0&&other<=100?-other:1;
      return [row.stale?1:0,item?.applicable===false?-101:-value,tie];
    }
    if(mode==='updated') {
      const value=row.fetched_at;
      return typeof value==='number'&&Number.isFinite(value)&&value>0?[0,-value,0]:[2,0,0];
    }
    const value = row.snapshot?.five_hour?.reset_at;
    if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return [2,0,0];
    if (value <= currentTime) return [1,value,0];
    return [0,value,0];
  };
  return [...accounts].sort((a, b) => {
    const ak = key(a), bk = key(b);
    return ak[0]-bk[0]||ak[1]-bk[1]||ak[2]-bk[2]||a.label.localeCompare(b.label);
  });
}

function orderCards() {
  if (!state) return;
  const grid = $('accounts'), focused = document.activeElement;
  const ordered = sortAccounts(state.accounts, now(), sortMode);
  for (let i=0; i<ordered.length; i++) {
    const card=$(`account-${ordered[i].label}`);
    if(!card)continue;
    if (grid.children[i] !== card) grid.insertBefore(card, grid.children[i] || null);
  }
  if (focused && grid.contains(focused) && document.activeElement !== focused) focused.focus({preventScroll:true});
}

function buildCards(rows=[]) {
  const labels=rows.filter(row=>row.registered&&/^[A-Z]+$/.test(row.label)).map(row=>row.label);
  const expected=new Set(labels);
  for(const card of $('accounts').querySelectorAll('.account-card'))if(!expected.has(card.dataset.label))card.remove();
  for(const choice of $('batch-choices').querySelectorAll('.batch-choice'))if(!expected.has(choice.querySelector('input').value))choice.remove();
  $('accounts-empty').hidden=labels.length>0;
  $('codex-total-count').textContent=`/ ${labels.length}`;
  if(subscriptionEditor&&!expected.has(subscriptionEditor.label)){$('subscription-editor').close();subscriptionEditor=null;}
  if(creditConfirmation&&!expected.has(creditConfirmation.label)){$('credit-confirm').close();creditConfirmation=null;}
  for (const label of labels) {
    if($(`account-${label}`))continue;
    const article = document.createElement('article');
    article.className = 'account-card';
    article.id = `account-${label}`;
    article.dataset.label=label;
    article.setAttribute('aria-label',`账户 ${label} 的额度`);
    article.innerHTML = `<div class="card-header"><div class="account-heading"><span class="letter">${label}</span><div class="account-identity"><div class="account-name">账户 ${label}</div><div class="account-email" hidden></div></div></div><div class="card-state"><span class="state-badge" data-field="status">等待查询</span><span class="usage-indicator" hidden>使用中 · 15秒</span></div></div>
      <div class="plan-info"><span data-field="plan-name">套餐</span><span data-field="plan-expiry">到期时间未提供</span></div>
      <div class="quota-windows">${['five_hour','weekly'].map(name => `<div class="quota-window" data-window="${name}"><div class="window-row"><span class="window-name">${name==='five_hour'?'五小时':'周额度'}<small>${name==='five_hour'?'5H':'7D'}</small></span><span class="percentage unknown" data-field="percent">—</span></div><progress class="quota-progress unknown" max="100" value="0" aria-label="${label} ${name==='five_hour'?'五小时':'周'}剩余额度"></progress><div class="reset"><span data-field="reset">恢复时间未知</span><span class="reset-countdown" data-field="countdown">—</span></div></div>`).join('')}</div>
      <div class="card-notice" data-field="notice"><strong>正在等候额度数据</strong><span>不会向模型发送消息。</span></div>
      <details class="account-settings"><summary><span>账户设置</span><span class="account-mode">读取中</span><span class="settings-thresholds"></span></summary><div class="account-settings-body"><div class="pool-control"><label for="auto-pool-${label}">加入 AUTO 号池</label><input type="checkbox" id="auto-pool-${label}" aria-label="账户 ${label} 加入 AUTO 号池" disabled></div><div class="threshold-control"><label for="threshold-${label}">五小时剩余不高于</label><div><input type="number" id="threshold-${label}" min="0" max="99" step="1" inputmode="numeric" aria-label="账户 ${label} 五小时切出阈值" disabled><span>% 时切出</span></div><label for="weekly-threshold-${label}">周额度剩余不高于</label><div><input type="number" id="weekly-threshold-${label}" min="0" max="99" step="1" inputmode="numeric" aria-label="账户 ${label} 周额度切出阈值" disabled><span>% 时切出</span></div><small>0关闭；仅AUTO，无备选也停止</small></div><p class="subscription-updated" data-field="subscription-updated"></p></div></details>
      <div class="card-bottom"><div class="last-update">上次成功<span data-field="updated">尚未取得</span></div><button type="button" class="refresh-one" aria-label="只刷新账户 ${label}">${refreshIcon}<span>刷新</span></button></div>`;
    const edit=document.createElement('button');edit.type='button';edit.className='button subscription-edit';
    edit.textContent='手动更新日期';edit.setAttribute('aria-label',`手动更新账户 ${label} 套餐日期`);
    edit.addEventListener('click',()=>openSubscriptionEditor(label));
    article.querySelector('.account-settings-body').append(edit);
    const remove=document.createElement('button');remove.type='button';remove.className='button account-remove';remove.textContent='移除账号';
    remove.setAttribute('aria-label',`移除 Codex ${label}`);
    remove.addEventListener('click',()=>window.dispatchEvent(new CustomEvent('account-remove-request',{detail:{platform:'codex',label}})));
    article.querySelector('.account-settings-body').append(remove);
    article.querySelector('.letter').classList.toggle('long-label',label.length>2);
    article.querySelector('.refresh-one').addEventListener('click',() => requestRefresh(label));
    buildCreditControls(article,label);
    article.querySelector('input[type="checkbox"]').addEventListener('change',event => setAutoPool(label,event.target.checked));
    article.querySelector(`#threshold-${label}`).addEventListener('change',event => setThreshold(label,event.target.value,'five_hour'));
    article.querySelector(`#weekly-threshold-${label}`).addEventListener('change',event => setThreshold(label,event.target.value,'weekly'));
    const choice=document.createElement('label');choice.className='batch-choice';choice.innerHTML=`<input type="checkbox" value="${label}" disabled><span>${label}</span>`;
    choice.querySelector('input').addEventListener('change',syncBatchAll);
    $('batch-choices').append(choice);
    $('accounts').append(article);
  }
}

function countdown(reset) {
  if (!reset) return '—';
  const seconds = reset - now();
  if (seconds <= 0) return '已到期，待刷新';
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟后`;
  const hours = Math.floor(minutes/60), mins = minutes%60;
  if (hours < 24) return `${hours} 小时 ${mins} 分后`;
  return `${Math.floor(hours/24)} 天 ${hours%24} 小时后`;
}

function subscriptionDateState(expires,currentTime) {
  const known=typeof expires==='number'&&Number.isFinite(expires)&&expires>0;
  const seconds=known?expires-currentTime:null;
  return {known,past:known&&seconds<=0,near:known&&seconds>0&&seconds<=7*86400,
    days:known&&seconds>0?Math.ceil(seconds/86400):null};
}

function manualSubscriptionDateState(value,currentTime) {
  const empty={known:false,past:false,near:false,days:null,today:false};
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value))return empty;
  const [year,month,day]=value.split('-').map(Number),date=new Date(year,month-1,day);
  if(date.getFullYear()!==year||date.getMonth()!==month-1||date.getDate()!==day)return empty;
  const current=new Date(currentTime*1000);
  const days=Math.round((Date.UTC(year,month-1,day)-Date.UTC(current.getFullYear(),current.getMonth(),current.getDate()))/86400000);
  return {known:true,past:days<0,near:days>=0&&days<=7,days,today:days===0};
}

function renderSubscription(row,card) {
  const subscription=row.subscription,plan=card.querySelector('.plan-info');
  const manual=subscription?.source==='manual';
  const planNames={plus:'Plus',pro:'Pro',free:'Free',go:'Go',team:'Team',business:'Business',enterprise:'Enterprise',edu:'Edu'};
  card.querySelector('[data-field="plan-name"]').textContent=`${planNames[subscription?.plan_type]||subscription?.plan_type||'套餐'} · ${manual?'手动记录':'登录记录'}`;
  const expires=subscription?.active_until,date=manual?manualSubscriptionDateState(subscription.manual_date,now()):subscriptionDateState(expires,now());
  const warning=date.past?' · 待核实':date.today?' · 今天':date.near?` · ${date.days}天内`:'';
  const displayDate=manual?subscription.manual_date.slice(5).replace('-','/'):date.known?dateFormat.format(new Date(expires*1000)):'';
  card.querySelector('[data-field="plan-expiry"]').textContent=date.known?`到期记录 ${displayDate}${warning}`:'到期时间未提供';
  const checked=subscription?.checked_at,checkedText=checked?fullDateFormat.format(new Date(checked*1000)):'未提供';
  const updated=subscription?.manual_updated_at,updatedText=updated?fullDateFormat.format(new Date(updated*1000)):'未提供';
  plan.title=manual?`你手动填写的套餐日期：${subscription.manual_date}；记录时间：${updatedText}。按浏览器本地自然日提醒，未自动查询官网。`:
    `登录时保存的到期记录：${date.known?fullDateFormat.format(new Date(expires*1000)):'未提供'}；原资料核验时间：${checkedText}。不是当前官网订阅查询；续费或延期后可能仍是旧日期，请自行到官网核实。`;
  plan.classList.toggle('record-past',date.past);plan.classList.toggle('record-near',date.near);
  card.querySelector('[data-field="subscription-updated"]').textContent=subscription?.manual_date_error||
    (manual?`手动更新：${updatedText}；保存后额度刷新不会覆盖。可恢复原登录记录。`:`登录资料核验：${checkedText}；续费或延期后可能仍是旧记录，可手动更新日期。`);
  card.querySelector('.subscription-edit').disabled=!row.registered||offline;
}

function openSubscriptionEditor(label) {
  const row=state?.accounts.find(item=>item.label===label);
  if(!row?.registered||!row.subscription_edit_token){toast('请先刷新账户资料再编辑日期。');return;}
  subscriptionEditor={label,edit_token:row.subscription_edit_token};
  $('subscription-title').textContent=`账户 ${label} · 更新套餐日期`;
  const display=accountIdentityText(row,showAccountNames);
  $('subscription-account').textContent=[display.name,display.email].filter(Boolean).join(' · ');
  const subscription=row.subscription;
  let value=subscription?.manual_date||'';
  if(!value&&typeof subscription?.active_until==='number') {
    const date=new Date(subscription.active_until*1000);
    value=`${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
  }
  $('subscription-date').value=value;$('subscription-error').textContent='';
  $('subscription-restore').disabled=subscription?.source!=='manual';
  $('subscription-save').disabled=false;$('subscription-cancel').disabled=false;
  $('subscription-editor').showModal();$('subscription-date').focus();
}

async function saveSubscriptionDate(restore=false) {
  if(!subscriptionEditor||subscriptionEditor.saving)return;
  const value=restore?null:$('subscription-date').value;
  if(!restore&&(!$('subscription-date').reportValidity()||!value))return;
  const edit=subscriptionEditor;
  edit.saving=true;
  for(const id of ['subscription-save','subscription-restore','subscription-cancel'])$(id).disabled=true;
  try {
    await api('/api/subscription-date',{account:edit.label,date:value,edit_token:edit.edit_token});
    subscriptionEditor=null;$('subscription-editor').close();await poll();
    toast(restore?`账户 ${edit.label} 已恢复登录记录。`:`账户 ${edit.label} 已保存手动日期 ${value}。`);
  }catch(error){$('subscription-error').textContent=error.message;}
  finally {
    edit.saving=false;
    $('subscription-save').disabled=false;$('subscription-cancel').disabled=false;
    const row=state?.accounts.find(item=>item.label===edit.label);
    $('subscription-restore').disabled=row?.subscription?.source!=='manual';
  }
}

function updateTimers() {
  if (!state) return;
  let staleChanged=false;
  orderCards();
  for (const row of state.accounts) {
    const card = $(`account-${row.label}`);
    renderSubscription(row,card);
    renderCreditControls(row);
    for (const name of ['five_hour','weekly']) {
      const reset = row.snapshot?.[name]?.reset_at;
      card.querySelector(`[data-window="${name}"] [data-field="countdown"]`).textContent = row.snapshot?.[name]?.applicable===false?'无恢复倒计时':countdown(reset);
      if(!row.stale&&row.snapshot&&((reset&&reset<=now())||now()-(row.fetched_at||0)>Math.max(60,row.refresh_interval||3600))){row.stale=true;staleChanged=true;}
    }
  }
  const active = state.accounts.filter(row => row.refreshing).length;
  if (active) $('next-update').textContent = `${active} 个账户查询中 · 结果将逐个显示`;
  else if (!state.interval) $('next-update').textContent = '仅手动更新 · 切号检测仍独立运行';
  else {
    const seconds = Math.max(0,Math.ceil((state.next_refresh_at || now())-now()));
    $('next-update').textContent = `${state.interval===15?'智能更新 · ':''}下次 ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')} 后`;
  }
  if(staleChanged)queueMicrotask(()=>{if(state)render();});
}

function render() {
  if (!state) return;
  buildCards(state.accounts);
  let loaded=0, errors=0;
  const availabilityCounts={ready:0,low:0,pending:0};
  for (const row of state.accounts) {
    const card=$(`account-${row.label}`), status=card.querySelector('[data-field="status"]');
    const availability=quotaAvailability(row,offline);
    card.dataset.quotaState=availability.kind;
    availabilityCounts[availability.kind==='exhausted'?'low':availability.kind]++;
    const name=card.querySelector('.account-name'),email=card.querySelector('.account-email');
    const display=accountIdentityText(row,showAccountNames);
    name.textContent=display.name;name.title=display.name;
    email.textContent=display.email;email.title=display.email;email.hidden=!display.email;
    card.setAttribute('aria-label',`${display.label} 的额度`);
    card.classList.toggle('in-use',Boolean(row.active));
    const usage=card.querySelector('.usage-indicator');usage.hidden=!row.active;
    usage.textContent=`使用中 · ${!row.refresh_interval?'手动':row.refresh_interval===15?'15秒':row.refresh_interval===3600?'1小时':row.refresh_interval/60+'分钟'}`;
    usage.title=`检测到 ${row.active_processes||1} 个本机 Codex CLI 窗口`;
    renderSubscription(row,card);
    renderCreditControls(row);
    card.querySelector('.account-mode').textContent=row.auto_enabled?'AUTO 已开':'仅手动';
    const enabledThresholds=[];
    if(row.auto_threshold>0)enabledThresholds.push(`5H ≤${row.auto_threshold}%`);
    if(row.auto_weekly_threshold>0)enabledThresholds.push(`周 ≤${row.auto_weekly_threshold}%`);
    card.querySelector('.settings-thresholds').textContent=enabledThresholds.join(' · ')||'阈值未开';
    const poolToggle=card.querySelector('input[type="checkbox"]');
    if (!changingPool.has(row.label)) poolToggle.checked=Boolean(row.auto_enabled);
    poolToggle.disabled=changingPool.has(row.label)||!row.registered||offline;
    for (const [prefix,field] of [['threshold','auto_threshold'],['weekly-threshold','auto_weekly_threshold']]) {
      const input=$(`${prefix}-${row.label}`);
      if (!changingThreshold.has(row.label) && document.activeElement!==input) input.value=String(row[field] ?? 0);
      input.disabled=changingThreshold.has(row.label)||changingBatch||!row.registered||offline;
    }
    const choice=$('batch-choices').querySelector(`input[value="${row.label}"]`);
    choice.disabled=!row.registered||changingBatch||offline;
    if (!row.registered) choice.checked=false;
    const exhausted=availability.kind==='exhausted';
    const unknown=Object.values(availability.windows).some(item=>item.tone==='unknown');
    if (row.snapshot) loaded++;
    if (row.error) errors++;
    card.classList.toggle('stale',Boolean(row.stale));
    let text='可用', tone='good';
    if (!row.registered) {text='未登记';tone='';}
    else if (offline) {text='待确认';tone='warn';}
    else if (row.refreshing) {text='查询中';tone='busy';}
    else if (row.error) {text=row.error.kind==='auth'?'登录失效':row.error.kind==='timeout'?'查询超时':'连接失败';tone='error';}
    else if (!row.snapshot) {text='等待查询';tone='';}
    else if (row.stale) {text='待更新';tone='warn';}
    else if (exhausted) {text='已耗尽';tone='error';}
    else if (unknown) {text='部分未知';tone='warn';}
    else if (availability.kind==='low') {text='额度偏低';tone='error';}
    else if (availability.kind==='pending') {text='待确认';tone='warn';}
    status.className=`state-badge ${tone}`;
    status.textContent=text;
    status.title=availability.reason;
    for (const name of ['five_hour','weekly']) {
      const item=row.snapshot?.[name];
      const window=card.querySelector(`[data-window="${name}"]`);
      const windowState=availability.windows[name],value=windowState.value;
      const notApplicable=windowState.tone==='not-applicable';
      const known=windowState.known,low=windowState.tone==='low';
      const percent=window.querySelector('[data-field="percent"]');
      const progress=window.querySelector('progress');
      percent.replaceChildren(document.createTextNode(notApplicable?'无此限额':known?String(Number(value.toFixed(1))):'—'));
      if (known&&!notApplicable) {const unit=document.createElement('small');unit.textContent='%';percent.append(unit);}
      percent.className=`percentage${notApplicable?' not-applicable':known?low?' critical':' sufficient':' unknown'}`;
      window.classList.toggle('not-applicable',notApplicable);
      progress.className=`quota-progress${known?'':' unknown'}${low?' critical':''}`;
      progress.value=known?value:0;
      progress.setAttribute('aria-valuetext',notApplicable?'未设置此限额':known?`剩余 ${value}%`:'剩余额度未知');
      progress.setAttribute('aria-hidden',notApplicable?'true':'false');
      window.querySelector('[data-field="reset"]').textContent=notApplicable?`未设置${name==='five_hour'?'五小时':'周'}限额`:item?.reset_at?`${dateFormat.format(new Date(item.reset_at*1000))} 恢复`:'恢复时间未知';
    }
    const notice=card.querySelector('[data-field="notice"]');
    const headline=notice.querySelector('strong'), detail=notice.querySelector('span');
    if (offline) {
      notice.className='card-notice cached';headline.textContent='服务未连接，额度待确认。';detail.textContent='暂时保留上次数据，恢复连接后重新确认。';
    } else if (row.error) {
      notice.className=`card-notice ${row.snapshot?'cached':'failed'}`;
      headline.textContent=row.error.message;
      detail.textContent=`${routeName(row.attempted_mode || state.network_mode)}查询失败。`+(row.snapshot?`保留${routeName(row.snapshot_mode)}上次成功数据，请留意更新时间。`:'尚无成功数据，未知不代表没有额度。');
    } else if (row.refreshing) {
      notice.className='card-notice';headline.textContent=row.snapshot?'正在更新，暂时显示上次数据。':'正在查询，结果会自动显示。';
      detail.textContent=`正在通过${routeName(row.attempted_mode || state.network_mode)}查询。`;
    } else if (!row.registered) {
      notice.className='card-notice';headline.textContent='此账户尚未登记或登录无效。';detail.textContent='完成本机授权后再刷新。';
    } else if (!row.snapshot) {
      notice.className='card-notice';headline.textContent='尚未取得额度数据。';detail.textContent='点击刷新即可查询，无需进入会话。';
    } else if (row.stale) {
      notice.className='card-notice cached';headline.textContent='旧数据，等待当前路线刷新确认。';detail.textContent=`上次成功路线：${routeName(row.snapshot_mode)}。`;
    } else if (exhausted) {
      notice.className='card-notice failed';headline.textContent='有额度窗口已耗尽。';detail.textContent='可留意恢复倒计时。';
    } else if (unknown) {
      notice.className='card-notice cached';headline.textContent='部分额度窗口未返回数据。';detail.textContent='已有数据仍可查看，未知不代表零。';
    } else if (availability.kind==='low') {
      notice.className='card-notice failed';headline.textContent=availability.reason;detail.textContent='低于显示标准，可留意恢复倒计时。';
    } else if (availability.kind==='pending') {
      notice.className='card-notice cached';headline.textContent='额度窗口待确认。';detail.textContent='刷新后再判断可用状态。';
    } else {
      notice.className='card-notice healthy';headline.textContent='额度数据已更新。';detail.textContent=row.manual_only?'此账户保留手动使用。':'此账户在 AUTO 账户池中。';
    }
    notice.hidden=notice.classList.contains('healthy');
    detail.hidden=!row.error;
    notice.title=`${headline.textContent} ${detail.textContent}`;
    card.querySelector('[data-field="updated"]').textContent=row.fetched_at?`${clockFormat.format(new Date(row.fetched_at*1000))} · ${routeName(row.snapshot_mode)}`:'尚未取得';
    const button=card.querySelector('.refresh-one');button.disabled=row.refreshing||!row.registered;
    button.querySelector('span').textContent=row.refreshing?'查询中':'刷新';
    button.querySelector('svg').classList.toggle('loading-icon',row.refreshing);
  }
  $('loaded-count').textContent=String(loaded);
  $('error-count').textContent=String(errors);
  for(const kind of ['ready','low','pending'])$(`quota-${kind}-count`).textContent=String(availabilityCounts[kind]);
  syncBatchAll();
  $('batch-apply').disabled=changingBatch||offline;
  const anyBusy=state.accounts.some(row=>row.refreshing);
  const registeredRows=state.accounts.filter(row=>row.registered);
  const allBusy=registeredRows.length>0&&registeredRows.every(row=>row.refreshing);
  $('refresh-all').disabled=allBusy;
  $('refresh-all').querySelector('span').textContent=allBusy?'正在刷新':'全部刷新';
  $('refresh-all').querySelector('svg').classList.toggle('loading-icon',anyBusy);
  if (document.activeElement!==$('auto-interval')) $('auto-interval').value=String(state.interval);
  if (!changingNetwork) $('network-mode').value=state.network_mode || 'clash';
  $('network-mode').disabled=changingNetwork;
  const route=routeName(state.network_mode);
  const onRoute=state.accounts.filter(row=>row.registered && row.snapshot_mode===state.network_mode && !row.error && !row.stale).length;
  $('network-status').textContent=anyBusy?`当前：${route} · 正在验证查询`:`当前：${route} · ${onRoute} 个账户已取得当前路线数据${errors?` · ${errors} 个查询失败`:''}`;
  const using=state.active_accounts||[];
  $('activity-summary').textContent=state.activity_error||
    (state.interval===15?`${using.length?'使用中：'+using.join('、')+' · 约15秒更新；':'当前无已识别窗口；'}其余账号每1小时更新。切号每3秒本机检测，手动刷新随时可用。`:
    `本机使用中：${using.length?using.join('、'):'无'}。切号检测独立运行；额度按所选更新模式处理。`);
  updateTimers();
}

function toast(message) {
  $('toast').textContent=message;$('toast').hidden=false;
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('toast').hidden=true;},4500);
}

async function api(path,body) {
  const response=await fetch(path,{cache:'no-store',headers:body?{'Content-Type':'application/json','X-Quota-Request':'local-dashboard'}:{},
    method:body?'POST':'GET',body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(8000)});
  const result=await response.json();
  if (!response.ok) throw new Error(result.error||'本次操作未完成。');
  return result;
}

async function poll() {
  if (polling) {pollAgain=true;return;}
  polling=true;
  try {
    const result=await api('/api/status');
    state={...result,accounts:(result.accounts||[]).filter(row=>row.registered&&/^[A-Z]+$/.test(row.label))};offset=result.server_time-Date.now()/1000;offline=false;
    $('service-alert').hidden=true;$('connection').classList.remove('offline');
    $('connection-text').textContent='本机服务已连接';
    render();
  } catch (_) {
    offline=true;$('service-alert').hidden=false;$('connection').classList.add('offline');
    $('connection-text').textContent='服务未连接';
    if(state)render();
  } finally {polling=false;if(pollAgain){pollAgain=false;queueMicrotask(poll);}}
}

async function checkActivity() {
  if(checkingActivity||document.hidden)return;
  checkingActivity=true;
  try {
    const signal=await api('/api/activity');
    if(typeof signal.server_time==='number')offset=signal.server_time-Date.now()/1000;
    if(offline||!state||signal.instance_id!==state.instance_id||signal.revision!==state.revision||signal.credit_revision!==state.credit_revision)await poll();
  }catch(_){
    offline=true;$('service-alert').hidden=false;$('connection').classList.add('offline');
    $('connection-text').textContent='服务未连接';
    if(state)render();
  }finally{checkingActivity=false;}
}

async function requestRefresh(label) {
  try {
    const result=await api('/api/refresh',{account:label});
    if (!result.accepted.length&&!result.busy.length) toast('刚刚查询过，请稍等几秒后重试。');
    await poll();
  } catch (error) {toast(error.message);}
}

async function setAutoPool(label,enabled) {
  changingPool.add(label);
  const toggle=$(`auto-pool-${label}`);toggle.disabled=true;
  try {
    const result=await api('/api/auto-pool',{account:label,enabled});
    const row=state?.accounts.find(item=>item.label===label);
    if(row){row.auto_enabled=result.enabled;row.manual_only=!result.enabled;}
    toast(`账户 ${label} 已${result.enabled?'加入':'移出'} AUTO 号池，新启动的 AUTO 生效。`);
  } catch(error) {toast(error.message);}
  finally {changingPool.delete(label);await poll();if(state)render();}
}

async function setThreshold(label,raw,windowName) {
  const value=Number(raw), field=windowName==='weekly'?'auto_weekly_threshold':'auto_threshold', input=$(`${windowName==='weekly'?'weekly-threshold':'threshold'}-${label}`);
  if (raw.trim()==='' || !Number.isInteger(value) || value<0 || value>99) {
    toast('请输入 0 到 99 之间的整数。');input.value=String(state?.accounts.find(row=>row.label===label)?.[field] ?? 0);return;
  }
  changingThreshold.add(label);input.disabled=true;
  try {
    const result=await api('/api/auto-threshold',{account:label,threshold:value,window:windowName});
    const row=state?.accounts.find(item=>item.label===label);if(row)row[field]=result.threshold;
    const kind=windowName==='weekly'?'周额度':'五小时';
    toast(value?`账户 ${label} 已设置${kind}剩余 ${value}% 时切出。`:`账户 ${label} 已关闭${kind}提前切出。`);
  } catch(error){toast(error.message);}
  finally {changingThreshold.delete(label);await poll();if(state)render();}
}

function syncBatchAll() {
  const choices=[...$('batch-choices').querySelectorAll('input:not(:disabled)')], selected=choices.filter(x=>x.checked).length, all=$('batch-all');
  all.disabled=changingBatch||!choices.length;all.checked=choices.length>0&&selected===choices.length;all.indeterminate=selected>0&&selected<choices.length;
}

$('batch-all').addEventListener('change',event=>{
  for(const choice of $('batch-choices').querySelectorAll('input:not(:disabled)')) choice.checked=event.target.checked;
  syncBatchAll();
});
$('batch-apply').addEventListener('click',async()=>{
  const selected=[...$('batch-choices').querySelectorAll('input:checked:not(:disabled)')].map(x=>x.value);
  const five=$('batch-five-hour').value.trim(), weekly=$('batch-weekly').value.trim();
  if(!selected.length){toast('请先选择要应用的账户。');return;}
  if(!five&&!weekly){toast('请至少填写一项阈值。');return;}
  const valid=value=>value===''||(/^\d+$/.test(value)&&Number(value)<=99);
  if(!valid(five)||!valid(weekly)){toast('请输入 0 到 99 之间的整数。');return;}
  const body={accounts:selected};if(five!=='')body.five_hour=Number(five);if(weekly!=='')body.weekly=Number(weekly);
  changingBatch=true;render();
  try {
    await api('/api/auto-threshold-batch',body);
    for(const row of state.accounts)if(selected.includes(row.label)){
      if(five!=='')row.auto_threshold=Number(five);
      if(weekly!=='')row.auto_weekly_threshold=Number(weekly);
    }
    toast(`已更新 ${selected.join('、')} 账户的切出阈值。`);
  } catch(error){toast(error.message);}
  finally{changingBatch=false;await poll();if(state)render();}
});

$('network-mode').addEventListener('change',async event=>{
  const mode=event.target.value;
  changingNetwork=true;event.target.disabled=true;
  try {
    await api('/api/settings',{network_mode:mode});
    toast(`已选择${routeName(mode)}，正在重新查询。`);
  } catch(error) {toast(error.message);}
  finally {changingNetwork=false;event.target.disabled=false;await poll();}
});
$('refresh-all').addEventListener('click',()=>requestRefresh('all'));
$('show-account-names').checked=showAccountNames;
$('show-account-names').addEventListener('change',event=>setNameVisibility(event.target.checked));
window.addEventListener('storage',event=>{if(event.key==='codex-quota-show-names')setNameVisibility(event.newValue!=='0',false);});
$('sort-mode').value=sortMode;
$('sort-mode').addEventListener('change',event=>{
  sortMode=event.target.value;
  try{localStorage.setItem('codex-quota-sort',sortMode);}catch(_){}
  updateSortNote();orderCards();
});
function updateSortNote() {
  const notes={reset:'按五小时恢复时间由近到远；到期待刷新、无需此窗口和时间未知的账户排在后面。',
    five_hour:'按五小时剩余额度由多到少，无此限额的优先；旧数据随后，未知最后。',
    weekly:'按周剩余额度由多到少，无此限额的优先；旧数据随后，未知最后。',
    updated:'按最近成功刷新时间由新到旧；尚无成功数据的账户排在最后。'};
  $('sort-note').textContent=notes[sortMode];
}
updateSortNote();
$('auto-interval').addEventListener('change',async event=>{
  try {await api('/api/settings',{interval:Number(event.target.value)});await poll();toast(Number(event.target.value)?'自动更新设置已保存。':'已关闭自动更新，可随时手动刷新。');}
  catch(error){toast(error.message);await poll();}
});
$('timezone').textContent=`恢复时间按浏览器本地时区显示：${Intl.DateTimeFormat().resolvedOptions().timeZone}。`;
buildCards();
window.addEventListener('accounts-changed',poll);
initializeCreditDialog();
$('subscription-cancel').addEventListener('click',()=>{$('subscription-editor').close();subscriptionEditor=null;});
$('subscription-editor').addEventListener('cancel',()=>{subscriptionEditor=null;});
$('subscription-save').addEventListener('click',()=>saveSubscriptionDate(false));
$('subscription-restore').addEventListener('click',()=>saveSubscriptionDate(true));
$('subscription-date').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();saveSubscriptionDate(false);}});
poll();
setInterval(updateTimers,1000);
setInterval(()=>{if(!document.hidden&&(offline||state?.accounts.some(row=>row.refreshing||row.reset_credits?.busy)))poll();},2000);
setInterval(checkActivity,3000);
setInterval(()=>{if(!document.hidden)poll();},3600000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
