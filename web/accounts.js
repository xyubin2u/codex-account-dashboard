'use strict';
(() => {
  const byId=id=>document.getElementById(id);
  if(!byId('account-add-dialog'))return;
  let registry=null,selected=null,adding=false,removing=false,revision=0;
  const platformName=platform=>platform==='agy'?'AGY':'Codex';
  const validLabel=value=>typeof value==='string'&&/^[A-Z]+$/.test(value);
  async function request(path,body){
    const response=await fetch(path,{cache:'no-store',credentials:'same-origin',method:body?'POST':'GET',
      headers:body?{'Content-Type':'application/json','X-Quota-Request':'local-dashboard'}:{},
      body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(15000)});
    const value=await response.json();
    if(!response.ok)throw new Error(value.error||'本次操作未完成，请重试。');
    return value;
  }
  function renderCommand(){
    const platform=byId('account-add-platform').value,label=byId('account-add-label').value.trim();
    const next=registry?.next_labels?.[platform]||'';
    byId('account-add-label').placeholder=next?`自动编号 ${next}`:'自动编号';
    const command=registry?.login_commands?.[platform]||'';
    byId('account-login-command').value=command&&(!label||validLabel(label))?command.replace(/--label\s+[A-Z]+(?=\s|$)/,`--label ${label||next}`):'';
    byId('account-login-copy').disabled=!byId('account-login-command').value;
  }
  function renderCounts(){
    const rows=registry?.accounts||[],codex=rows.filter(row=>row.platform==='codex').length,agy=rows.filter(row=>row.platform==='agy').length;
    byId('account-total-count').textContent=String(codex+agy);
    byId('account-platform-counts').textContent=`Codex ${codex} · AGY ${agy}`;
  }
  async function loadRegistry(){
    const current=++revision;
    const next=await request('/api/accounts');
    if(!Array.isArray(next.accounts))throw new Error('账号列表暂不可用，请重试。');
    if(current===revision){registry=next;renderCounts();renderCommand();}
    return next;
  }
  function clearImport(){byId('account-import-file').value='';}
  async function openAdd(){
    byId('account-add-platform').value='codex';byId('account-add-label').value='';clearImport();
    byId('account-add-error').textContent='';byId('account-login-status').textContent='';
    renderCommand();byId('account-add-dialog').showModal();byId('account-add-platform').focus();
    try{await loadRegistry();}catch(error){byId('account-add-error').textContent=error.message;}
  }
  function announceChange(){window.dispatchEvent(new CustomEvent('accounts-changed'));}
  async function addAccount(){
    if(adding)return;
    byId('account-add-error').textContent='';
    const platform=byId('account-add-platform').value,label=byId('account-add-label').value.trim(),file=byId('account-import-file').files[0];
    if(label&&!validLabel(label)){byId('account-add-error').textContent='账号编号请填纯大写英文字母，例如 I 或 AA。';return;}
    if(!file){byId('account-add-error').textContent='请先选择官方登录 JSON 文件，或使用下方隔离登录命令。';return;}
    if(file.size>240*1024){byId('account-add-error').textContent='登录文件过大，请选择官方登录 JSON 文件（不超过 240 KiB）。';return;}
    adding=true;byId('account-add-submit').disabled=true;byId('account-add-cancel').disabled=true;
    let credential;
    try{
      try{credential=JSON.parse(await file.text());}catch(_){throw new Error('文件不是有效的 JSON，请重新选择官方登录文件。');}
      if(!credential||Array.isArray(credential)||typeof credential!=='object')throw new Error('登录 JSON 必须是对象，请重新选择官方登录文件。');
      await request('/api/accounts/add',{platform,label,credential});
      credential=null;clearImport();byId('account-add-dialog').close();
      announceChange();loadRegistry().catch(()=>{});
      if(typeof toast==='function')toast('账号已添加。额度待查询时可点击刷新。');
    }catch(error){byId('account-add-error').textContent=error.message;}
    finally{credential=null;adding=false;byId('account-add-submit').disabled=false;byId('account-add-cancel').disabled=false;}
  }
  function renderRemoveIdentity(){
    if(!selected)return;
    const label=selected.label,platform=selected.platform;
    const card=byId(`${platform==='agy'?'agy-account':'account'}-${label}`);
    const visible=byId('show-account-names')?.checked;
    const details=visible?[card?.querySelector(platform==='agy'?'.agy-account-email':'.account-name')?.textContent,
      platform==='codex'?card?.querySelector('.account-email')?.textContent:''].filter(Boolean):[];
    byId('account-remove-identity').textContent=[`${platformName(platform)} ${label}`,...details].join(' · ');
  }
  async function openRemove(detail){
    if(!detail||!['codex','agy'].includes(detail.platform)||!validLabel(detail.label))return;
    try{
      await loadRegistry();
      selected=registry.accounts.find(row=>row.platform===detail.platform&&row.label===detail.label);
      if(!selected)throw new Error('此账号已变更，请刷新页面后重试。');
      byId('account-remove-title').textContent=`移除 ${platformName(selected.platform)} ${selected.label}`;
      renderRemoveIdentity();byId('account-remove-error').textContent='';
      byId('account-remove-text').textContent=selected.external?'仅取消本工具对该账号的管理，保留原生默认登录。保留共享会话、历史统计和用券记录。账号使用中需先结束相关窗口。':'删除本工具登录，保留共享会话、历史统计和用券记录。账号使用中需先结束相关窗口。';
      byId('account-remove-dialog').showModal();byId('account-remove-cancel').focus();
    }catch(error){if(typeof toast==='function')toast(error.message);}
  }
  async function removeAccount(){
    if(!selected||removing)return;
    const current=selected;removing=true;byId('account-remove-error').textContent='';
    byId('account-remove-submit').disabled=true;byId('account-remove-cancel').disabled=true;
    try{
      await request('/api/accounts/remove',{platform:current.platform,label:current.label,remove_token:current.remove_token,confirmed:true});
      byId('account-remove-dialog').close();selected=null;announceChange();loadRegistry().catch(()=>{});
      if(typeof toast==='function')toast(`${platformName(current.platform)} ${current.label} 已移除，共享会话和历史已保留。`);
    }catch(error){byId('account-remove-error').textContent=error.message;}
    finally{removing=false;byId('account-remove-submit').disabled=false;byId('account-remove-cancel').disabled=false;}
  }
  byId('account-add').addEventListener('click',openAdd);
  byId('account-add-submit').addEventListener('click',addAccount);
  byId('account-add-cancel').addEventListener('click',()=>byId('account-add-dialog').close());
  byId('account-add-dialog').addEventListener('close',clearImport);
  byId('account-add-dialog').addEventListener('cancel',event=>{if(adding)event.preventDefault();});
  byId('account-add-platform').addEventListener('change',()=>{clearImport();byId('account-add-error').textContent='';renderCommand();});
  byId('account-add-label').addEventListener('input',renderCommand);
  byId('account-login-copy').addEventListener('click',async()=>{
    try{await navigator.clipboard.writeText(byId('account-login-command').value);byId('account-login-status').textContent='命令已复制，请在本机终端运行。';}
    catch(_){byId('account-login-command').focus();byId('account-login-command').select();byId('account-login-status').textContent='请复制已选中的命令，在本机终端运行。';}
  });
  byId('account-login-check').addEventListener('click',async()=>{
    const before=new Set((registry?.accounts||[]).map(row=>`${row.platform}:${row.label}`));
    try{await loadRegistry();announceChange();const added=registry.accounts.some(row=>!before.has(`${row.platform}:${row.label}`));byId('account-login-status').textContent=added?'已发现新账号，可关闭此窗口查看。':'尚未发现新的有效登录；请先在终端完成官方授权。';}
    catch(error){byId('account-login-status').textContent=error.message;}
  });
  byId('account-remove-submit').addEventListener('click',removeAccount);
  byId('account-remove-cancel').addEventListener('click',()=>byId('account-remove-dialog').close());
  byId('account-remove-dialog').addEventListener('close',()=>{selected=null;});
  byId('account-remove-dialog').addEventListener('cancel',event=>{if(removing)event.preventDefault();});
  byId('show-account-names')?.addEventListener('change',renderRemoveIdentity);
  window.addEventListener('storage',event=>{if(event.key==='codex-quota-show-names')queueMicrotask(renderRemoveIdentity);});
  window.addEventListener('account-remove-request',event=>openRemove(event.detail));
  window.addEventListener('accounts-changed',()=>loadRegistry().catch(()=>{}));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)loadRegistry().catch(()=>{});});
  loadRegistry().catch(()=>{byId('account-platform-counts').textContent='账号列表待连接';});
})();
