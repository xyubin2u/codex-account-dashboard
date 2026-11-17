(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const nf=new Intl.NumberFormat('zh-CN');
  const timezone=Intl.DateTimeFormat().resolvedOptions().timeZone||'Asia/Tokyo';
  const df=new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  const descriptions={waiting:'等待下一次额度更新建立观察基线',unknown:'额度窗口信息不完整',no_limit:'无此限额',
    unknown_group:'部分调用的额度组尚无法核对',need_change:'等待额度下降',small_change:'额度下降不足 20 个百分点',few_requests:'已归属请求不足 5 次',short_span:'观察不足 1 分钟',
    settling:'等待端点数据对齐',index_behind:'等待本机日志同步完成',unattributed:'观察期间存在未归属请求',identity_conflict:'观察期间存在账号身份差异',
    boundary:'额度查询边界存在请求，等待稳定快照',unpriced:'部分请求尚未定价',credit_history_unavailable:'用券记录暂不可核对',stale:'观察已过期，等待额度更新'};
  let live=true,busy=false,pending=false,queued=false,queuedScan=false,platform='all',filterVersion=0,controller=null;
  const num=n=>typeof n==='number'&&Number.isFinite(n);
  const tokens=n=>num(n)?(n>=1e6?`${(n/1e6).toFixed(2)}M`:nf.format(n)):'—';
  const money=n=>num(n)?`$${n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}`:'未定价';
  const date=n=>num(n)?df.format(new Date(n*1000)):'等待观察';
  function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
  function renderPlatform(data = {}) {
    $('platform-scope').textContent = {gpt:'GPT · Codex 本机观察',agy:'AGY · 本机额度与用量观察',all:'GPT + AGY · 同名账号按平台分别测算'}[platform];
    $('capacity-basis').textContent = typeof data.note === 'string' ? data.note : typeof data.source === 'string' ? data.source : `你已确认测算期间仅在本机使用。统计包含 2026-11-09 当天；各额度窗口分别测算，不能相加。${platform==='gpt'?'金额按现有 API 参考价计算':platform==='agy'?'AGY 按对应 API 模型参考价（Google / Anthropic 官方价），缓存与长上下文按供应商规则，缺价保留未定价':'GPT 按 OpenAI 官方 / 手动价，AGY 按对应 API 模型参考价（Google / Anthropic 官方价），缓存与长上下文按供应商规则；金额仅汇总已定价部分'}，非订阅扣款或官方固定额度。`;
  }
  function render(data){
    renderPlatform(data);
    const total=data.since_total;
    $('capacity-total').textContent=tokens(total.total_tokens);
    $('capacity-total-note').textContent=`${nf.format(total.total_tokens)} Token · ${nf.format(total.requests)} 次调用`;
    $('capacity-input').textContent=tokens(total.input_tokens);
    $('capacity-cached').textContent=`其中缓存 ${tokens(total.cached_input_tokens)} · 不重复累加`;
    $('capacity-output').textContent=tokens(total.output_tokens);
    $('capacity-money').textContent=money(total.estimated_cost_usd);
    $('capacity-unpriced').textContent=total.unpriced_requests?`${total.unpriced_requests} 次未定价，金额仅含已定价部分`:'逐请求参考价 · 非订阅扣款';
    $('capacity-error').hidden=!data.collection_error;$('capacity-error').textContent=data.collection_error||'';
    $('capacity-status').textContent=`${live?'实时查看':'已暂停实时'} · 日志同步 ${date(data.scan?.updated_at)} · ${data.timezone}`;
    const fragment=document.createDocumentFragment();
    for(const account of data.accounts){
      const card=el('article','capacity-card');card.dataset.account=account.label;card.dataset.platform=account.platform||'gpt';
      const head=el('header');head.append(el('h2','',account.display_name || `${account.platform==='agy'?'AGY':'GPT'} 账户 ${account.label}`),el('p','',`11/09 起已归属 ${tokens(account.since_usage.total_tokens)} · ${money(account.since_usage.estimated_cost_usd)}`));card.append(head);
      const windows=el('div','capacity-windows');
      for(const [name,row] of Object.entries(account.windows||{})){
        if(!row || typeof row !== 'object') continue;
        const section=el('section','capacity-window');section.dataset.window=name;
        section.append(el('h3','',row.title || {five_hour:'五小时窗口',weekly:'周窗口'}[name] || name));
        if(['waiting','no_limit','unknown'].includes(row.status)){
          section.append(el('p','capacity-waiting',descriptions[row.status]));windows.append(section);continue;
        }
        section.append(el('p','capacity-drop',`${date(row.from_at)} → ${date(row.until_at)} · 下降 ${row.quota_drop??0} 个百分点`),
          el('div','capacity-value',tokens(row.observed.total_tokens)),el('p','capacity-caption',`${nf.format(row.observed.requests)} 次已观察调用`),
          el('p','capacity-amount',money(row.observed.estimated_cost_usd)));
        const estimate=el('div','capacity-estimate');estimate.append(el('p','capacity-caption','按当前使用组合推算满额'));
        if(row.estimate){estimate.append(el('strong','',`≈ ${tokens(row.estimate.total_tokens)}`),el(num(row.estimate.api_usd)?'strong':'p',num(row.estimate.api_usd)?'':'capacity-caption',num(row.estimate.api_usd)?`≈ ${money(row.estimate.api_usd)}`:'金额未定价'),el('p','capacity-caption','本机样本粗估，非官方额度'));}
        else estimate.append(el('p','capacity-waiting',(row.reasons||[]).map(k=>descriptions[k]||'样本待核对').slice(0,2).join('；')));
        section.append(estimate);
        if(row.history_range){const range=row.history_range;section.append(el('p','capacity-caption',`${row.history_samples<3?'初步':'近期'} ${row.history_samples} 段样本 · Token ${tokens(range.total_tokens.min)}–${tokens(range.total_tokens.max)} · API ${money(range.api_usd.min)}–${money(range.api_usd.max)}`));}
        const details=el('details','capacity-detail');details.append(el('summary','','查看分项与样本说明'));
        details.append(el('p','',`输入 ${nf.format(row.observed.input_tokens)}，其中缓存 ${nf.format(row.observed.cached_input_tokens)}；输出 ${nf.format(row.observed.output_tokens)}（含推理）。`));
        details.append(el('p','',`最近额度观察 ${date(row.until_at)} · 预计重置 ${date(row.reset_at)}`));
        const list=el('ul');for(const reason of row.reasons||[])list.append(el('li','',descriptions[reason]||'样本待核对'));details.append(list);
        section.append(details);windows.append(section);
      }
      card.append(windows);
      const source = account.note || account.source;
      if(typeof source === 'string' && source) card.append(el('p','capacity-source',source));
      fragment.append(card);
    }
    if(!data.accounts.length)fragment.append(el('p','capacity-empty','尚未登记账号。'));
    $('capacity-accounts').replaceChildren(fragment);
  }
  async function request(path,post=false,signal){const response=await fetch(path,{signal,cache:'no-store',credentials:'same-origin',...(post?{method:'POST',headers:{'Content-Type':'application/json','X-Quota-Request':'local-dashboard'},body:'{}'}:{})});if(!response.ok)throw new Error('本机测算暂不可用，请稍后重试。');return response.json();}
  async function update(scan=false){
    if(busy){queued=true;queuedScan=queuedScan||scan;return;}busy=true;
    const requestedPlatform=platform,requestedVersion=filterVersion;
    const currentController=new AbortController();controller=currentController;
    const timeout=setTimeout(()=>currentController.abort(),25000);
    try{
      if(scan&&!pending){const s=await request('/api/usage/scan',true,currentController.signal);pending=Boolean(s.running);}
      const data=await request('/api/capacity?'+new URLSearchParams({tz:timezone,platform:requestedPlatform}),false,currentController.signal);
      pending=Boolean(data.scan?.running);
      if(requestedVersion!==filterVersion){queued=true;return;}
      render(data);
    }catch(error){if(requestedVersion===filterVersion){$('capacity-error').hidden=false;$('capacity-error').textContent=error.message;}}
    finally{clearTimeout(timeout);controller=null;busy=false;if(queued){const scanNext=queuedScan;queued=false;queuedScan=false;update(scanNext);}}
  }
  document.querySelectorAll('button[data-platform]').forEach(button=>button.addEventListener('click',()=>{
    if(platform===button.dataset.platform)return;
    platform=button.dataset.platform;filterVersion+=1;
    if(controller)controller.abort();
    document.querySelectorAll('button[data-platform]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));
    renderPlatform();
    for(const id of ['capacity-total','capacity-input','capacity-output','capacity-money'])$(id).textContent='—';
    $('capacity-accounts').replaceChildren(el('p','capacity-empty','正在读取所选平台…'));
    $('capacity-status').textContent='正在读取所选平台…';
    $('capacity-error').hidden=true;
    update(false);
  }));
  $('capacity-live').addEventListener('click',()=>{live=!live;$('capacity-live').textContent=live?'暂停实时':'恢复实时';$('capacity-live').setAttribute('aria-pressed',String(live));update(live&&!document.hidden);});
  $('capacity-sync').addEventListener('click',()=>update(true));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)update(live);});
  setInterval(()=>{if(!document.hidden&&(live||pending))update(live&&!pending);},3000);
  update(!document.hidden);
})();
