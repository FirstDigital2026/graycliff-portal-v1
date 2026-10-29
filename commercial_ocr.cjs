// Local-only OCR. Images and recognized text never leave this service.
const fs=require('node:fs/promises');
const path=require('node:path');
const {createWorker}=require(process.env.COMMERCIAL_OCR_MODULE||'./deployment/pdf-ocr/node_modules/tesseract.js');
(async()=>{
 const manifest=JSON.parse(await fs.readFile(process.argv[2],'utf8'));
 const worker=await createWorker('eng',1,{langPath:process.env.COMMERCIAL_OCR_LANG_PATH||path.join(__dirname,'deployment/pdf-ocr/language'),gzip:false,cacheMethod:'none'});
 const out=[];
 try{
  for(const page of manifest){
   await worker.setParameters({tessedit_pageseg_mode:'3'});
   const full=(await worker.recognize(page.path)).data.text;
   let body='',finance='',recap='',recapRows='';
   if(/subcontractor\s+invoice/i.test(full)){
    await worker.setParameters({tessedit_pageseg_mode:'6'});
    const top=Math.floor(page.height*(page.page===1?.35:.30));
    body=(await worker.recognize(page.path,{rectangle:{left:Math.floor(page.width*.067),top,width:Math.floor(page.width*.883),height:Math.min(Math.floor(page.height*.59),page.height-top)}})).data.text;
    finance=(await worker.recognize(page.lowPath,{rectangle:{left:Math.floor(page.lowWidth*.639),top:Math.floor(page.lowHeight*(page.page===1?.35:.305)),width:Math.floor(page.lowWidth*.303),height:Math.floor(page.lowHeight*.532)}})).data.text;
   }
   if(!body&&/Week\s+Ending/i.test(full)){
    await worker.setParameters({tessedit_pageseg_mode:'6'});
    const result=(await worker.recognize(page.path,{}, {tsv:true})).data;
    recap=result.text;
    const groups=new Map();
    for(const row of (result.tsv||'').split('\n').slice(1)){
     const fields=row.split('\t');if(fields.length<12||!fields[11].trim())continue;
     const key=fields.slice(1,5).join('-');if(!groups.has(key))groups.set(key,[]);groups.get(key).push(fields);
    }
    const rows=[];
    for(const words of groups.values()){
     if(!/^(?:BFOV|BMG?60)/i.test(words.map(w=>w[11]).join(' ')))continue;
     if(rows.length>=200)throw new Error('Recap row limit');
     const top=Math.max(0,Math.min(...words.map(w=>+w[7]))-8),bottom=Math.min(page.height,Math.max(...words.map(w=>+w[7]+ +w[9]))+8);
     await worker.setParameters({tessedit_pageseg_mode:'7'});
     rows.push((await worker.recognize(page.path,{rectangle:{left:Math.floor(page.width*.028),top,width:Math.floor(page.width*.942),height:bottom-top}})).data.text.trim());
    }
    recapRows=rows.join('\n');
   }
   out.push({page:page.page,text:full,invoiceBody:body,financeBody:finance,recapBody:recap,recapRows});
  }
 }finally{await worker.terminate();}
 process.stdout.write(JSON.stringify(out));
})().catch(()=>{process.stderr.write('Private PDF OCR failed');process.exitCode=1});
