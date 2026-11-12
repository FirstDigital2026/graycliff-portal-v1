const path=require('node:path');
const {createWorker}=require(process.env.COMMERCIAL_OCR_MODULE||'./deployment/pdf-ocr/node_modules/tesseract.js');
(async()=>{
 const worker=await createWorker('eng',1,{langPath:process.env.COMMERCIAL_OCR_LANG_PATH||path.join(__dirname,'deployment/pdf-ocr/language'),gzip:false,cacheMethod:'none'});
 try{
  const data=(await worker.recognize(process.argv[2],{}, {tsv:true})).data;
  const words=[];
  for(const line of (data.tsv||'').split('\n').slice(1)){
   const f=line.split('\t');if(f.length<12||f[0]!=='5'||!f[11].trim())continue;
   words.push([+f[6],+f[7],+f[6]+ +f[8],+f[7]+ +f[9],f[11]]);
  }
  if(data.text.trim()&&!words.length)throw new Error('Missing OCR coordinates');
  process.stdout.write(JSON.stringify({text:data.text,words}));
 }finally{await worker.terminate();}
})().catch(()=>{process.stderr.write('Private field document OCR failed');process.exitCode=1;});
