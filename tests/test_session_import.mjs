import assert from 'node:assert/strict';
import {loadParsers} from './frontend-source.mjs';
const {getSessionAccountPayload,parseImportJson}=loadParsers();
const sessionToken=`${'a'.repeat(32)}..${'b'.repeat(16)}.${'c'.repeat(16)}.${'d'.repeat(16)}`;
const plain=value=>JSON.parse(JSON.stringify(value));
assert.deepEqual(plain(getSessionAccountPayload({accessToken:'fixture-access',sessionToken,user:{private:'omit'}})),{access_token:'fixture-access',sessionToken,source_type:'web'});
assert.deepEqual(plain(getSessionAccountPayload({accessToken:'fixture-access'})),{access_token:'fixture-access',source_type:'web'});
for(const value of [null,[],{}, {accessToken:42},{accessToken:'bad\nheader'},{accessToken:'x'.repeat(32769)},
  ...[42,{},'invalid',sessionToken+'\nheader',sessionToken+'x'.repeat(16384)].map(sessionToken=>({accessToken:'fixture-access',sessionToken}))]){
  assert.equal(getSessionAccountPayload(value),null);
}
assert.throws(()=>parseImportJson('{fixture-secret-session'),error=>!error.message.includes('fixture-secret-session'));
console.log('完整会话、旧格式、非法输入和错误脱敏通过可构建源码验证');
