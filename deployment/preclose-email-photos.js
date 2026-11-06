const precloseDrafts=new Map();
function precloseState(key){if(!precloseDrafts.has(key))precloseDrafts.set(key,{photos:[],chosen:new Set(),loaded:false,without:false,review:'',batch:null,uploading:false});return precloseDrafts.get(key);}
function preclosePending(form){return [...(form.elements.precloseUploads?.files||[]),...(form.elements['outagePhotos[]']?.files||[])];}
function precloseFingerprint(form,state){return JSON.stringify({photos:[...state.chosen].sort(),without:state.without,note:form.elements.note?.value||'',noc:form.elements.nocTechnician?.value||'',pending:preclosePending(form).map(f=>[f.name,f.size,f.lastModified])});}
function precloseSummary(form,state){
 const selected=state.photos.filter(p=>state.chosen.has(p.key)),box=form.querySelector('[data-preclose-summary]');
 if(box)box.textContent=state.photos.length+' photos saved on this job · '+selected.length+' selected for the email';
 const button=form.querySelector('button[type="submit"]');if(button)button.textContent='Complete job & send completion email';
}
async function precloseLoad(form,key){
 const state=precloseState(key),message=form.querySelector('[data-preclose-message]'),gallery=form.querySelector('[data-preclose-gallery]');
 try{
  message.textContent='Loading saved job photos…';const response=await request('preclose_photos',{key});if(!form.isConnected)return;
  state.photos=response.photos||[];state.loaded=true;const allowed=new Set(state.photos.map(p=>p.key));state.chosen=new Set([...state.chosen].filter(p=>allowed.has(p)));state.review='';
  gallery.replaceChildren();
  for(const photo of state.photos){
   const card=document.createElement('div');card.className='fdgc-preclose-card';
   const label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.dataset.preclosePhoto=photo.key;check.checked=state.chosen.has(photo.key);
   const preview=document.createElement('span');preview.className='fdgc-preclose-thumb';preview.dataset.precloseThumb=photo.key;preview.textContent='Photo preview';
   const name=document.createElement('span');name.textContent=photo.name.replace(/^Field-upload-[a-f0-9]+-/,'');
   label.append(check,preview,name);const expand=document.createElement('button');expand.type='button';expand.className='btn';expand.dataset.precloseExpand=photo.key;expand.textContent='View photo';card.append(label,expand);gallery.append(card);
  }
  if(!state.photos.length)gallery.textContent='No field photos saved yet. Upload photos above, or explicitly send without photos.';
  const queue=[];let active=0;const drain=()=>{while(active<3&&queue.length&&form.isConnected){const slot=queue.shift();active++;request('preclose_photo_preview',{key,photo:slot.dataset.precloseThumb,thumbnail:'1'}).then(d=>{if(!slot.isConnected)return;if(d.kind!=='image'||!/^image\/(jpeg|png|webp)$/.test(d.mime))throw Error('Preview unavailable');const image=document.createElement('img');image.src='data:'+d.mime+';base64,'+d.content;image.alt='Saved job photo';image.loading='lazy';slot.replaceChildren(image);}).catch(()=>{if(slot.isConnected)slot.textContent='Use View photo to inspect';}).finally(()=>{active--;drain();});}};
  const slots=[...gallery.querySelectorAll('[data-preclose-thumb]')];
  if('IntersectionObserver' in window){const observer=new IntersectionObserver(entries=>{if(!form.isConnected){observer.disconnect();return;}for(const entry of entries)if(entry.isIntersecting){observer.unobserve(entry.target);queue.push(entry.target);}drain();},{root:gallery,rootMargin:'100px'});slots.forEach(slot=>observer.observe(slot));}else{queue.push(...slots);drain();}
  message.textContent='All saved photos stay on the job. Only checked photos will go in this completion email.';precloseSummary(form,state);
 }catch(error){state.loaded=false;message.textContent=error.message+' Use Refresh saved photos to retry.';}
}
function precloseMount(){
 const form=document.querySelector('#statusform'),box=form?.querySelector('#completion-photo-reminder'),key=detail?.job?.key||selected;if(!form||!box||box.dataset.precloseMounted)return;
 box.dataset.precloseMounted='1';box.style.cssText='padding:16px;background:#f1f7fb;border:1px solid #bdd3e3;border-radius:8px';
 box.innerHTML='<h3>Pre-close: save photos, then choose email photos</h3><p>Every uploaded photo stays with this job. Choose a few saved photos for the completion email.</p><h4>1. Save all job photos</h4><label>Add job photos<input type="file" name="precloseUploads" multiple accept="image/jpeg,image/png,image/webp,image/heic"></label><small>No limit on the number of job photos. Each file can be up to 15 MB.</small><div class="actions"><button type="button" class="btn" data-preclose-upload>Save photos to job</button><button type="button" class="btn" data-preclose-refresh>Refresh saved photos</button></div><p data-preclose-message role="status"></p><h4>2. Choose photos for the email</h4><p>Check up to 10 photos. Nothing is selected automatically.</p><div data-preclose-gallery class="fdgc-preclose-gallery"></div><label style="display:flex;gap:10px;align-items:center;margin:14px 0"><input type="checkbox" data-preclose-without style="width:auto"> Send completion email without photos</label><p data-preclose-summary aria-live="polite"></p><button type="button" class="btn" data-preclose-review>Review completion email photos</button><section data-preclose-review-panel hidden></section>';
 const state=precloseState(key);form.dataset.precloseKey=key;box.querySelector('[data-preclose-without]').checked=state.without;
 const outage=form.elements['outagePhotos[]'];if(outage&&form.elements.status.value==='Field complete')outage.closest('label').hidden=true;
 if(form.elements.status.value==='Field complete')precloseLoad(form,key);
}
const precloseOldDraw=drawDetail;drawDetail=function(...args){const result=precloseOldDraw(...args);precloseMount();return result;};
document.addEventListener('change',e=>{
 const form=e.target.closest('#statusform');if(!form)return;precloseMount();const key=form.dataset.precloseKey;if(!key)return;const state=precloseState(key);
 if(e.target.name==='status'){
  const closing=e.target.value==='Field complete',outage=form.elements['outagePhotos[]'];if(outage)outage.closest('label').hidden=closing;
  if(closing&&!state.loaded)precloseLoad(form,key);if(!closing){const button=form.querySelector('button[type="submit"]');if(button)button.textContent=detail?.job?.type==='Outage'?'Save & send outage update':'Save status';}else precloseSummary(form,state);
 }
 if(e.target.matches('[data-preclose-photo]')){
  if(e.target.checked&&state.chosen.size>=10){e.target.checked=false;toast('Choose up to 10 email photos. All other photos stay on the job.');return;}
  if(e.target.checked){state.chosen.add(e.target.dataset.preclosePhoto);state.without=false;form.querySelector('[data-preclose-without]').checked=false;}else state.chosen.delete(e.target.dataset.preclosePhoto);
 }
 if(e.target.matches('[data-preclose-without]')){state.without=e.target.checked;if(state.without){state.chosen.clear();form.querySelectorAll('[data-preclose-photo]').forEach(c=>c.checked=false);}}
 if(e.target.name==='precloseUploads')state.batch=null;
 state.review='';form.querySelector('[data-preclose-review-panel]').hidden=true;if(form.elements.status.value==='Field complete')precloseSummary(form,state);
});
document.addEventListener('input',e=>{const form=e.target.closest('#statusform');if(form?.dataset.precloseKey){precloseState(form.dataset.precloseKey).review='';form.querySelector('[data-preclose-review-panel]').hidden=true;}});
document.addEventListener('click',async e=>{
 const button=e.target.closest('[data-preclose-upload],[data-preclose-refresh],[data-preclose-review],[data-preclose-expand]');if(!button)return;
 const form=button.closest('#statusform'),key=form?.dataset.precloseKey;if(!key)return;const state=precloseState(key),message=form.querySelector('[data-preclose-message]');
 if(button.hasAttribute('data-preclose-refresh'))return precloseLoad(form,key);
 if(button.hasAttribute('data-preclose-expand')){
  button.disabled=true;try{const d=await request('preclose_photo_preview',{key,photo:button.dataset.precloseExpand,thumbnail:'0'});if(d.kind!=='image'||!/^image\/(jpeg|png|webp)$/.test(d.mime))throw Error('This photo cannot be previewed.');const dialog=document.createElement('dialog');dialog.style.cssText='max-width:95vw;max-height:90vh;padding:16px';const close=document.createElement('button');close.className='btn';close.textContent='Close photo';close.onclick=()=>dialog.close();const img=document.createElement('img');img.src='data:'+d.mime+';base64,'+d.content;img.alt='Saved job photo';img.style.cssText='display:block;max-width:85vw;max-height:75vh;object-fit:contain;margin-top:12px';dialog.append(close,img);dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);dialog.showModal();}catch(error){toast(error.message);}finally{button.disabled=false;}return;
 }
 if(button.hasAttribute('data-preclose-upload')){
  const input=form.elements.precloseUploads,files=preclosePending(form);if(!files.length){message.textContent='Choose the photos to save first.';return;}
  if(files.some(f=>f.size>15*1024*1024||!/[.](jpe?g|png|webp|heic)$/i.test(f.name))){message.textContent='Use JPEG, PNG, WebP or HEIC photos, up to 15 MB each.';return;}
  if(busy||state.uploading)return;state.uploading=true;busy=true;state.review='';button.disabled=true;input.disabled=true;if(form.elements['outagePhotos[]'])form.elements['outagePhotos[]'].disabled=true;
  try{
   if(!state.batch||state.batch.files.length!==files.length||files.some((file,i)=>file!==state.batch.files[i]))state.batch={files,entries:files.map(file=>({file,id:uid(),done:false}))};
   for(const [i,item]of state.batch.entries.entries()){if(item.done)continue;message.textContent='Saving photo '+(i+1)+' of '+files.length+': '+item.file.name;await request('upload',{key,file:item.file,submission:item.id,fieldSafe:'1'});item.done=true;}
   input.value='';if(form.elements['outagePhotos[]'])form.elements['outagePhotos[]'].value='';state.batch=null;await precloseLoad(form,key);message.textContent='All '+files.length+' photos saved to the job. Now choose the few photos for the email.';
  }catch(error){message.textContent=error.message+' Saved photos are kept. Retry Save photos to job to continue.';}finally{state.uploading=false;busy=false;button.disabled=false;input.disabled=false;if(form.elements['outagePhotos[]'])form.elements['outagePhotos[]'].disabled=false;}return;
 }
 if(!state.loaded){message.textContent='Refresh saved photos before reviewing the email.';return;}
 if(state.uploading||preclosePending(form).length){message.textContent='Save all selected uploads to the job before reviewing email photos.';return;}
 if(!state.chosen.size&&!state.without){message.textContent='Choose email photos, or explicitly select Send completion email without photos.';return;}
 const panel=form.querySelector('[data-preclose-review-panel]');panel.replaceChildren();const heading=document.createElement('h4');heading.textContent='Completion email photo review';panel.append(heading);
 const text=document.createElement('p');text.textContent=state.chosen.size?state.chosen.size+' selected photo(s) will be emailed. The remaining job photos will stay on the job.':'The completion email will have no photo attachments. All saved photos stay on the job.';panel.append(text);
 for(const photo of state.photos.filter(p=>state.chosen.has(p.key))){const row=document.createElement('p');row.textContent='✓ '+photo.name.replace(/^Field-upload-[a-f0-9]+-/,'');const thumb=[...form.querySelectorAll('[data-preclose-thumb]')].find(t=>t.dataset.precloseThumb===photo.key)?.querySelector('img');if(thumb){const image=thumb.cloneNode(true);image.style.cssText='display:block;width:160px;height:130px;object-fit:contain';row.prepend(image);}panel.append(row);}
 const note=document.createElement('p');note.textContent='After reviewing, use Complete job & send completion email below.';panel.append(note);panel.hidden=false;state.review=precloseFingerprint(form,state);panel.scrollIntoView({block:'nearest'});
});
document.addEventListener('submit',async e=>{
 if(e.target.id!=='statusform'||e.target.elements.status.value!=='Field complete')return;
 e.preventDefault();e.stopImmediatePropagation();const form=e.target;precloseMount();const key=form.dataset.precloseKey,state=precloseState(key),message=form.querySelector('[data-preclose-message]');
 if(busy||state.uploading)return;
 if(!state.loaded||!state.review||state.review!==precloseFingerprint(form,state)){message.textContent='Save your job photos, choose the email photos, then review the selection before completing.';return;}
 if(!form.reportValidity())return;busy=true;const submit=form.querySelector('button[type="submit"]');submit.disabled=true;
 try{
  await request('preclose_selection',{key,submission,photos:JSON.stringify([...state.chosen]),withoutPhotos:state.without?'1':'0'});
  const vals=Object.fromEntries(new FormData(form));delete vals.precloseUploads;delete vals['outagePhotos[]'];vals.key=key;vals.submission=submission;
  busy=false;const saved=await mutate('status',vals,true);if(saved){submission=uid();precloseDrafts.delete(key);}
 }catch(error){message.textContent=error.message;toast(error.message);}finally{busy=false;if(submit.isConnected)submit.disabled=false;}
},true);
const precloseStyle=document.createElement('style');precloseStyle.textContent='.fdgc-preclose-gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:12px;max-height:430px;overflow:auto;padding:8px}.fdgc-preclose-card{padding:10px;border:1px solid #bdcfdf;border-radius:8px;background:#fff;overflow-wrap:anywhere}.fdgc-preclose-card label{display:block;font-size:12px;cursor:pointer}.fdgc-preclose-card input{width:auto;margin:0 8px 8px 0}.fdgc-preclose-thumb{display:flex;align-items:center;justify-content:center;height:130px;background:#e8eff4;margin-bottom:8px}.fdgc-preclose-thumb img{width:100%;height:130px;object-fit:contain}.fdgc-preclose-card .btn{margin-top:8px;min-height:40px}.fdgc-preclose-card:has(input:checked){border:3px solid #217cad;padding:8px;background:#eaf7ff}[data-preclose-review-panel]{margin-top:16px;padding:14px;border:2px solid #217cad;border-radius:8px;background:#fff}';document.head.append(precloseStyle);
precloseMount();
