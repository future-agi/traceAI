const {execFileSync}=require('node:child_process');
const {rmSync,writeFileSync}=require('node:fs');
rmSync('dist',{recursive:true,force:true});
for(const p of ['tsconfig.json','tsconfig.esm.json']) execFileSync(process.execPath,[require.resolve('typescript/bin/tsc'),'-p',p],{stdio:'inherit'});
writeFileSync('dist/esm/package.json',JSON.stringify({type:'module'}));
