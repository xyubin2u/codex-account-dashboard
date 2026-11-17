'use strict';
(() => {
  const byId=id=>document.getElementById(id);
  if(!byId('agy-accounts'))return;
  let data=null,disconnected=false,polling=false,pollAgain=false,clockOffset=0,showNames=true;
  const pending=new Set();
  try{showNames=localStorage.getItem('codex-quota-show-names')!=='0';}catch(_){}
  const current=()=>Date.now()/1000+clockOffset;
  const dates=new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  const fullDates=new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  const groupNames={'Gemini Models':'Gemini 模型','Claude and GPT models':'Claude / GPT'};
  const node=(tag,cls,value)=>{const item=document.createElement(tag);if(cls)item.className=cls;if(value!==undefined)item.textContent=value;return item;};
  function windowName(bucket){
    if(bucket.window==='weekly'||bucket.id.endsWith('-weekly'))return '周额度';
    if(bucket.window==='5h'||bucket.id.endsWith('-5h'))return '五小时';
    return bucket.display_name||bucket.window||bucket.id;
  }
  function bucketState(row,bucket){
    if(bucket.disabled)return 'disabled';
    if(disconnected||row.stale||row.error||row.refreshing||!row.registered)return 'pending';
    if(bucket.reset_at&&bucket.reset_at<=current())return 'pending';
    if(typeof bucket.remaining!=='number'||!Number.isFinite(bucket.remaining))return 'pending';
    const threshold=windowName(bucket)==='五小时'?30:20;
    return bucket.remaining>=threshold?'ready':'low';
  }
  function percent(value){
    if(value>0&&value<0.1)return '<0.1%';
    if(value<100&&value>99.9)return '99.9%';
    return `${Number(value.toFixed(1))}%`;
  }
  function remaining(bucket){
    if(bucket.disabled)return '未启用';
    if(typeof bucket.remaining==='number'&&Number.isFinite(bucket.remaining))return percent(bucket.remaining);
    if(bucket.remaining_amount!==null&&bucket.remaining_amount!==undefined)return `${bucket.remaining_amount} 剩余`;
    return '未知';
  }
  function resetText(bucket){
    if(bucket.disabled)return '此窗口未启用';
    if(!bucket.reset_at)return '重置时间未知';
    const seconds=Math.ceil(bucket.reset_at-current());
    if(seconds<=0)return '已到重置时间 · 待刷新';
    const days=Math.floor(seconds/86400),hours=Math.floor(seconds%86400/3600),minutes=Math.floor(seconds%3600/60),secs=seconds%60;
    return `${days?days+'天 ':''}${hours}小时 ${minutes}分 ${secs}秒后重置`;
  }
  function render(){
    const container=byId('agy-accounts');
    if(!data){byId('agy-policy').textContent=disconnected?'AGY 服务暂未连接，稍后自动重试。':'正在读取 AGY 额度';return;}
    const rows=(data.accounts||[]).filter(row=>row.registered&&/^[A-Z]+$/.test(row.label));
    const expected=new Set(rows.map(row=>`agy-account-${row.label}`));
    for(const card of container.querySelectorAll('.agy-card'))if(!expected.has(card.id))card.remove();
    byId('agy-accounts-empty').hidden=rows.length>0;
    byId('agy-count').textContent=rows.filter(row=>row.registered).length;
    const route=data.network_mode==='direct'?'WireGuard／系统网络':'Clash';
    const policy=data.interval===0?'仅手动更新':data.interval===15?'智能更新 · 使用中15秒 / 其余1小时':`每 ${data.interval/60} 分钟更新`;
    byId('agy-policy').textContent=disconnected?'AGY 服务未连接 · 保留上次数据，恢复后自动同步':`${policy} · ${route}`;
    byId('agy-policy').classList.toggle('agy-offline',disconnected);
    byId('agy-refresh-all').disabled=disconnected||rows.some(row=>row.refreshing)||pending.size>0;
    for(const row of rows){
      let card=byId(`agy-account-${row.label}`);
      if(!card){card=node('article','agy-card');card.id=`agy-account-${row.label}`;container.append(card);}
      const activeFocus=card.contains(document.activeElement)?document.activeElement?.dataset?.agyAction:null;
      card.replaceChildren();
      const header=node('div','agy-card-header'),identity=node('div','agy-identity');
      identity.append(node('span',`agy-letter${row.label.length>2?' long-label':''}`,row.label));
      const details=node('div','agy-identity-copy');details.append(node('h3','agy-account-name',`AGY ${row.label}`));
      if(showNames&&row.email)details.append(node('p','agy-account-email',row.email));
      identity.append(details);header.append(identity);
      header.append(node('span','agy-plan',row.snapshot?.plan_type||'套餐待确认'));
      card.append(header);
      if(row.active)card.append(node('p','agy-active','● 使用中'));
      const groups=row.snapshot?.groups||[];
      if(!groups.length)card.append(node('p','agy-empty',row.registered?'额度待查询':'未读取到此账号的有效本地登录'));
      let readyGroups=0,lowGroups=0;
      for(const group of groups){
        const states=group.buckets.filter(bucket=>!bucket.disabled).map(bucket=>bucketState(row,bucket));
        const tone=states.includes('pending')?'pending':states.includes('low')?'low':states.length?'ready':'disabled';
        if(tone==='ready')readyGroups++;if(tone==='low')lowGroups++;
        const section=node('section','agy-group');section.dataset.state=tone;
        const heading=node('div','agy-group-heading');heading.append(node('h4','',groupNames[group.display_name]||group.display_name));
        heading.append(node('span','agy-group-state',{ready:'可用',low:'额度偏低',pending:'待确认',disabled:'未启用'}[tone]));section.append(heading);
        const windows=node('div','agy-windows');
        for(const bucket of group.buckets){
          const item=node('div','agy-window');item.dataset.state=bucketState(row,bucket);item.dataset.bucket=bucket.id;
          const line=node('div','agy-window-line');line.append(node('span','agy-window-name',windowName(bucket)),node('strong','agy-percent',remaining(bucket)));item.append(line);
          if(!bucket.disabled&&typeof bucket.remaining==='number'){
            const progress=node('progress','agy-progress');progress.max=100;progress.value=bucket.remaining;
            progress.setAttribute('aria-label',`${windowName(bucket)}剩余 ${remaining(bucket)}`);item.append(progress);
          }
          const reset=node('p','agy-reset',resetText(bucket));
          if(bucket.reset_at&&!bucket.disabled)reset.title=fullDates.format(bucket.reset_at*1000);
          item.append(reset);
          if(bucket.reset_at&&!bucket.disabled)item.append(node('p','agy-reset-date',dates.format(bucket.reset_at*1000)));
          windows.append(item);
        }
        section.append(windows);card.append(section);
      }
      card.dataset.quotaState=(disconnected||row.stale||row.error||row.refreshing||!groups.length)?'pending':readyGroups&&lowGroups?'partial':lowGroups?'low':readyGroups?'ready':'pending';
      if(row.error)card.append(node('p','agy-error',row.error));
      else if(row.stale&&row.snapshot)card.append(node('p','agy-error','上次数据已过时，请刷新确认。'));
      const footer=node('div','agy-card-footer');
      footer.append(node('span','agy-updated',row.refreshing?'正在更新…':row.fetched_at?`更新于 ${dates.format(row.fetched_at*1000)}`:'尚未查询'));
      const button=node('button','agy-refresh-one',row.refreshing?'查询中…':'刷新额度 ↻');button.type='button';button.dataset.agyAction='refresh';
      button.disabled=!row.registered||disconnected||row.refreshing||pending.has(row.label)||pending.has('all');
      button.addEventListener('click',()=>refresh(row.label));footer.append(button);card.append(footer);
      const remove=node('button','account-remove','移除');remove.type='button';remove.dataset.agyAction='remove';remove.setAttribute('aria-label',`移除 AGY ${row.label}`);
      remove.addEventListener('click',()=>window.dispatchEvent(new CustomEvent('account-remove-request',{detail:{platform:'agy',label:row.label}})));
      footer.append(remove);
      if(activeFocus)card.querySelector(`[data-agy-action="${activeFocus}"]`)?.focus({preventScroll:true});
    }
  }
  async function request(path,body){
    const result=await fetch(path,{cache:'no-store',method:body?'POST':'GET',signal:AbortSignal.timeout(7000),
      headers:body?{'Content-Type':'application/json','X-Quota-Request':'local-dashboard'}:{},body:body?JSON.stringify(body):undefined});
    const value=await result.json();if(!result.ok)throw new Error(value.error||'AGY 操作暂未完成。');return value;
  }
  async function poll(){
    if(polling){pollAgain=true;return;}polling=true;
    try{
      const next=await request('/api/agy/status');
      if(!Array.isArray(next.accounts))throw new Error();
      data=next;clockOffset=next.server_time-Date.now()/1000;disconnected=false;render();
    }catch(_){disconnected=true;render();}
    finally{polling=false;if(pollAgain){pollAgain=false;queueMicrotask(poll);}}
  }
  async function refresh(label){
    pending.add(label);render();
    try{
      const result=await request('/api/agy/refresh',{account:label});
      if(!result.accepted.length&&!result.busy.length&&typeof toast==='function')toast('AGY 刚刚查询过，请稍等几秒再试。');
      await poll();
    }catch(error){if(typeof toast==='function')toast(error.message);}
    finally{pending.delete(label);render();}
  }
  byId('agy-refresh-all').addEventListener('click',()=>refresh('all'));
  window.addEventListener('accounts-changed',poll);
  byId('show-account-names')?.addEventListener('change',event=>{showNames=event.target.checked;render();});
  window.addEventListener('storage',event=>{if(event.key==='codex-quota-show-names'){showNames=event.newValue!=='0';render();}});
  byId('refresh-all')?.addEventListener('click',()=>setTimeout(poll,400));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
  poll();setInterval(()=>{if(!document.hidden)poll();},3000);
  setInterval(()=>{if(data&&!document.hidden)render();},1000);
})();
