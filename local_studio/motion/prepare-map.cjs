const fs=require('node:fs'); const path=require('node:path');
const source=path.join(__dirname,'node_modules','maplibre-gl','dist');const dest=path.join(__dirname,'public','maplibre');fs.mkdirSync(dest,{recursive:true});
for(const file of ['maplibre-gl-worker.mjs','maplibre-gl-shared.mjs'])fs.copyFileSync(path.join(source,file),path.join(dest,file));
