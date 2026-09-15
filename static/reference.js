'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let pid = new URLSearchParams(location.search).get('pid') || '', ref = null, job = {}, config = {}, shots = [], revision = 0, dirty = false, polling = false, uploading = false;
  const notice = (message, error=false) => { $('notice').textContent=message; $('notice').classList.toggle('error',error); };
  async function api(path, body, method='POST') {
    const opts = body===undefined ? {} : body instanceof FormData ? {method,body} : {method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
    const response = await fetch(path,opts);
    let data; try {data=await response.json();} catch {throw Error('服务器未返回有效结果，请检查连接后刷新状态');}
    if(!response.ok||!data.ok) throw Error(data.msg||`请求失败 ${response.status}`);
    return data;
  }
  const base = () => `/api/reference/${encodeURIComponent(pid)}`;
  const media = file => `${base()}/media/${file.split('/').map(encodeURIComponent).join('/')}`;
  const key = () => `aix-reference-draft-${pid}`;
  function markDirty() {
    dirty=true; $('dirty').textContent='有未保存修改（本浏览器草稿已保留）';
    try {localStorage.setItem(key(),JSON.stringify({revision,shots,story:$('story').value,characters:$('characters').value,scene:$('scene').value}));}
    catch {$('dirty').textContent='有未保存修改；浏览器草稿存储不可用，请及时保存编辑';}
    controls();
  }
  function controls() {
    const busy=['queued','running','stopping','retrying','merging'].includes(job.status)||uploading;
    for(const id of ['upload','analyze','apply-analysis','save-edit','new-project','retry-import']) $(id).disabled=busy;
    for(const id of ['export','test-export']) $(id).disabled=busy||dirty||!ref?.plan;
    $('stop').hidden=!busy||uploading; $('stop').disabled=job.status==='stopping';
    $('stop').textContent=job.status==='stopping'?'正在停止…':'停止处理';
    $('retry-import').hidden=!ref?.pending_source;
    $('status').textContent=uploading?'正在上传…':job.message||'尚无任务';
  }
  function renderShots() {
    $('shots').innerHTML=shots.map((s,i)=>`<article class="card" data-index="${i}"><div class="row"><h3>镜头 ${i+1}</h3><span class="tag">${esc(s.id)}</span><button data-op="seek">定位原片</button></div>
      <div class="frames">${(s.frames||[]).map(f=>`<figure><img src="${esc(media(f.file))}" alt="参考镜头 ${i+1} 在 ${esc(f.time)} 秒的抽帧"><figcaption>${esc(f.time)}s</figcaption></figure>`).join('')}</div>
      <div class="row"><label>源开始 秒<input class="time" type="number" step=".01" data-field="start" value="${esc(s.start)}"></label><label>源结束 秒<input class="time" type="number" step=".01" data-field="end" value="${esc(s.end)}"></label><label>生产分组<input data-field="group" value="${esc(s.group||s.id)}"></label></div>
      <p class="muted">原片动作：${esc(s.description)}<br>待复核：${esc(s.review||'请检查抽帧无法确定的连续动作')}</p>
      <label>本镜头的目标动作 / 剧情<textarea data-field="target_action" rows="2">${esc(s.target_action)}</textarea></label>
      <label>出场目标角色（逗号分隔，与上方姓名一致）<input data-field="cast" value="${esc((s.cast||[]).join('，'))}"></label>
      <div class="grid"><label>镜头设计<textarea data-field="camera" rows="2">${esc(s.camera)}</textarea></label><label>光线色调<textarea data-field="palette" rows="2">${esc(s.palette)}</textarea></label></div>
      <div class="checks row">${[['camera','镜头设计'],['rhythm','源节奏'],['palette','色调'],['action','原片动作结构']].map(([v,l])=>`<label><input type="checkbox" data-option="${v}" ${(s.reference||[]).includes(v)?'checked':''}>参考${l}</label>`).join('')}</div>
      <div class="row shot-tools"><button data-op="up" ${i===0?'disabled':''}>上移</button><button data-op="down" ${i===shots.length-1?'disabled':''}>下移</button><button data-op="split">拆分</button><button data-op="merge" ${i===shots.length-1?'disabled':''}>合并下一镜头</button><button data-op="delete">删除</button></div></article>`).join('')||'<p class="muted">完成分析并应用候选版本后，这里会显示镜头卡片。</p>';
  }
  function renderPlan() {
    const bindings=ref?.plan?.segment_bindings||[];
    $('plan').innerHTML=bindings.length?`<table><thead><tr><th>生产片段</th><th>源镜头时间</th><th>计划时长</th><th>对齐后时长</th><th>说明</th></tr></thead><tbody>${bindings.map(b=>`<tr><td>${esc(b.segment_id)}</td><td>${b.source_ranges.map(x=>`${esc(x.start.toFixed(2))}–${esc(x.end.toFixed(2))}s`).join('<br>')}</td><td>${esc(b.planned_duration)}s</td><td>${esc(b.output_duration.toFixed(3))}s / ${esc(b.frames)}帧</td><td>${esc(b.note)}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">填写目标故事、角色、场景及各镜头动作后，保存以查看计划。</p>';
    $('exports').innerHTML=(ref?.exports||[]).map(x=>`<p><a href="/#reference-project=${encodeURIComponent(x.pid)}">制作项目 ${esc(x.pid)}${x.test_first?'（首片测试）':''}</a></p>`).join('');
  }
  function loadEditor(restore=false) {
    shots=structuredClone(ref.shots||[]);revision=ref.revision;dirty=false;
    $('story').value=ref.story||'';$('scene').value=ref.scene||'';
    $('characters').value=(ref.characters||[]).map(c=>`${c.name} | ${c.appearance}`).join('\n');
    if(restore){try{const draft=JSON.parse(localStorage.getItem(key()));if(draft&&draft.revision===revision){shots=draft.shots;$('story').value=draft.story;$('scene').value=draft.scene;$('characters').value=draft.characters;dirty=true;}}catch{}}
    $('dirty').textContent=dirty?'已恢复本浏览器未保存草稿':'已载入保存版本';renderShots();renderPlan();controls();
  }
  async function refresh(initial=false) {
    if(!pid||polling)return;polling=true;
    try {
      const oldSource=ref?.source?.id; const d=await api(base());ref=d.reference;job=d.job;
      $('project-id').textContent=pid; $('workspace').hidden=!ref.source;
      if(ref.source){
        if(initial||oldSource!==ref.source.id){$('video').src=media(ref.source.preview);$('start').value=ref.selection[0];$('end').value=ref.selection[1];}
        const m=ref.source.metadata;$('video-meta').textContent=`${m.width}×${m.height} · ${m.duration.toFixed(2)} 秒 · ${m.has_audio?'源片含音轨，本期不转写':'无音轨'} · 时间为源视频起点偏移`;
      }
      const selected=$('versions').value;
      $('versions').innerHTML=(ref.analyses||[]).map(a=>`<option value="${esc(a.id)}">${esc(a.id)} · ${esc(a.provider)} · ${a.shots.length} 个镜头</option>`).join('');
      $('versions').value=(ref.analyses||[]).some(a=>a.id===selected)?selected:ref.latest_analysis||'';
      $('analysis-progress').textContent=ref.analysis_partial?`已完成并缓存 ${ref.analysis_partial.completed}/${ref.analysis_partial.total} 个镜头；停止或失败后可用相同参数重试。`:'';
      if(initial)loadEditor(true);else if(oldSource!==ref.source?.id&&!dirty)loadEditor();
      controls();
    } finally {polling=false;}
  }
  async function create() {const d=await api('/api/reference/projects',{});pid=d.pid;ref=null;job={};dirty=false;history.replaceState(null,'',`/reference?pid=${encodeURIComponent(pid)}`);await refresh(true);notice('参考草稿已创建，请上传视频。');}
  function bind(id, action) {$(id).addEventListener('click',async()=>{try{await action();}catch(e){notice(e.message,true);}});}
  bind('new-project',async()=>{if(dirty&&!confirm('有未保存编辑，仍要创建新草稿吗？'))return;await create();});
  bind('upload',async()=>{if(!$('file').files[0])throw Error('请先选择视频');if(ref?.source&&!confirm('替换参考视频会重置分析和编辑，已导出的制作项目保留。继续吗？'))return;const form=new FormData();form.append('file',$('file').files[0]);uploading=true;controls();try{await api(base()+'/upload',form);dirty=false;localStorage.removeItem(key());notice('上传完成，正在处理视频。');}finally{uploading=false;await refresh();}});
  bind('retry-import',async()=>{await api(base()+'/retry-import',{});await refresh();});
  bind('stop',async()=>{if(!confirm('停止当前处理？已经完成的分析、编辑及视频会保留。'))return;await api(`/api/project/${encodeURIComponent(pid)}/stop`,{run_id:job.run_id});await refresh();});
  bind('save-settings',async()=>{const body={};for(const k of Object.keys(config)){if($(k))body[k]=$(k).value;}body.reference_cloud_api_key=$('reference_cloud_api_key').value;body.clear_cloud_key=$('clear-key').checked;const d=await api('/api/reference/config',body);config=d.config;$('reference_cloud_api_key').value='';$('clear-key').checked=false;showConfig();notice('分析设置已保存。');});
  function showConfig(){for(const[k,v]of Object.entries(config)){if($(k))$(k).value=v??'';}$('media-check').textContent=config.media_ready?'FFmpeg / ffprobe 已就绪':config.media_error;$('key-state').textContent=config.cloud_key_set?'已保存云端 Key':'尚未保存云端 Key';}
  bind('analyze',async()=>{const mode=$('provider').value;const body={provider:mode,start:Number($('start').value),end:Number($('end').value)};const url=(config[`reference_${mode}_url`]||'').replace(/\/$/,'');const model=config[`reference_${mode}_model`];if(mode==='cloud'){if(!confirm(`将 ${body.start}–${body.end} 秒选区的抽帧图像发送到：\n${url}\n模型：${model}\n每镜头 3 张，最长边 512 像素，不含音频。是否继续？`))return;body.consent={url,model,source_id:ref.source.id,start:body.start,end:body.end};}await api(base()+'/analyze',body);notice('分析已开始；结果会作为候选版本保留。');await refresh();});
  bind('apply-analysis',async()=>{if(!$('versions').value)throw Error('没有可应用的完整分析');if(shots.length&&!confirm('应用分析会替换当前镜头卡片，目标故事、角色和场景保留。继续吗？'))return;const targets=Object.fromEntries(['story','characters','scene'].map(id=>[id,$(id).value]));const d=await api(base()+'/apply-analysis',{revision,analysis_id:$('versions').value});ref=d.reference;localStorage.removeItem(key());loadEditor();for(const[id,value]of Object.entries(targets))$(id).value=value;markDirty();notice('已应用分析，请填写自己的角色和逐镜头目标动作。');});
  bind('reload-editor',async()=>{if(dirty&&!confirm('放弃本浏览器未保存修改并重新载入吗？'))return;localStorage.removeItem(key());await refresh();loadEditor();});
  for(const id of ['story','scene','characters'])$(id).addEventListener('input',markDirty);
  $('shots').addEventListener('input',e=>{const card=e.target.closest('[data-index]');if(!card)return;const s=shots[Number(card.dataset.index)],f=e.target.dataset.field,o=e.target.dataset.option;if(f){s[f]=f==='cast'?e.target.value.split(/[,，]/).map(x=>x.trim()).filter(Boolean):['start','end'].includes(f)?Number(e.target.value):e.target.value;}if(o)s.reference=e.target.checked?[...new Set([...(s.reference||[]),o])]:(s.reference||[]).filter(x=>x!==o);markDirty();});
  $('shots').addEventListener('click',e=>{const op=e.target.dataset.op,card=e.target.closest('[data-index]');if(!op||!card)return;const i=Number(card.dataset.index),s=shots[i];if(op==='seek'){$('video').currentTime=s.start;$('video').scrollIntoView({behavior:'smooth'});return;}if(op==='up'&&i>0)[shots[i-1],shots[i]]=[s,shots[i-1]];if(op==='down'&&i<shots.length-1)[shots[i+1],shots[i]]=[s,shots[i+1]];if(op==='delete'){if(!confirm('从计划中移除此参考镜头？'))return;shots.splice(i,1);}if(op==='split'){const value=prompt('在源视频第几秒拆分？',((s.start+s.end)/2).toFixed(2));if(value===null)return;const t=Number(value);if(!(s.start<t&&t<s.end)){notice('拆分时间必须在镜头内部',true);return;}const second=structuredClone(s);second.id=crypto.randomUUID().replaceAll('-','').slice(0,12);second.start=t;s.end=t;shots.splice(i+1,0,second);}if(op==='merge'&&shots[i+1]){const n=shots[i+1];if(!confirm('合并后参考范围将覆盖两个镜头间的全部时间，继续吗？'))return;s.start=Math.min(s.start,n.start);s.end=Math.max(s.end,n.end);for(const f of ['description','target_action','camera','palette'])s[f]=[s[f],n[f]].filter(Boolean).join('；');s.cast=[...new Set([...s.cast,...n.cast])];s.frames=[...(s.frames||[]),...(n.frames||[])].slice(0,3);shots.splice(i+1,1);}renderShots();markDirty();});
  bind('save-edit',async()=>{const chars=$('characters').value.split('\n').filter(x=>x.trim()).map(line=>{const at=line.indexOf('|');if(at<0)throw Error('每个角色请用 姓名 | 外貌设定 格式');return{name:line.slice(0,at).trim(),appearance:line.slice(at+1).trim()};});const d=await api(base()+'/edit',{revision,shots,story:$('story').value,scene:$('scene').value,characters:chars});ref=d.reference;localStorage.removeItem(key());loadEditor();notice('编辑已保存，请审核下方时长与生产片段映射。');});
  async function exportPlan(test_first){if(!confirm(test_first?'将首个生产片段导入为独立测试项目？':'按当前已保存计划创建制作项目？已有成片不会被覆盖。'))return;const d=await api(base()+'/export',{revision,test_first});ref=d.reference;loadEditor();location.href=`/#reference-project=${encodeURIComponent(ref.last_export)}`;}
  bind('export',()=>exportPlan(false));bind('test-export',()=>exportPlan(true));
  async function init(){try{config=(await api('/api/reference/config')).config;showConfig();if(pid)await refresh(true);else await create();notice('先导入视频，再分析和编辑。'+(!config.media_ready?' 请先在设置中配置视频工具。':''));setInterval(()=>refresh().catch(e=>notice(e.message,true)),2000);}catch(e){notice(e.message,true);}}
  init();
})();
