import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import ts from '../frontend/node_modules/typescript/lib/typescript.js';
export const root=new URL('../',import.meta.url);
export function source(path){return readFileSync(new URL(path,root),'utf8');}
export function runTypescript(code,context={}){
  const compiled=ts.transpileModule(code,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
  return runInNewContext(compiled,context);
}
export function loadParsers(){const exports={};runTypescript(source('frontend/src/app/accounts/components/account-import-parsers.ts'),{exports});return exports;}
export {ts};
