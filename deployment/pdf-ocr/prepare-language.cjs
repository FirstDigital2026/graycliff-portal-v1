const {createWorker}=require('tesseract.js');
const path=require('node:path');
(async()=>{const w=await createWorker('eng',1,{cachePath:path.join(__dirname,'language')});await w.terminate();})().catch(()=>process.exitCode=1);
