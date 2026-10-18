'use strict';
// 无依赖：直接运行 node --test tests/test_frontend_controls.js。
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace(/\{\{ refresh_interval \| round\(3\) \}\}/, '3')
  .replace(/\nload\(\);\nresetTimer\(\);/, '\n');
function boot(fetch = async () => { throw Error('unexpected fetch'); }) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'', checked:false, textContent:'', innerHTML:'', disabled:false,
      dataset:{}, classList:{toggle(){}, contains(){return false;}}, addEventListener(){}});
    return nodes.get(id);
  };
  const context = vm.createContext({document:{getElementById:node}, window:{confirm:()=>true},
    localStorage:{getItem(){return null;}}, fetch, AbortController, setTimeout, clearTimeout,
    setInterval(){}, clearInterval(){}, console});
  vm.runInContext(source, context);
  return {node, run: s => vm.runInContext(s, context), context};
}
const response = data => ({ok:true, status:200, json:async()=>data});
test('模型草稿按面板隔离，刷新不覆盖自定义编辑', () => {
  const b = boot();
  b.run('modelDraft("a", "opus"); modelDraft("b", "haiku"); modelDraft("new:/p", null);');
  b.run('modelDrafts.a.value = "custom-id";');
  assert.equal(b.run('modelDraft("a", "sonnet").value'), 'custom-id');
  assert.equal(b.run('modelDraft("b").value'), 'haiku');
  assert.equal(b.run('modelDraft("new:/p").value'), null);
});
test('模型保存响应不能修改其他面板 DOM，null 原样提交', async () => {
  let finish; const requests=[];
  const b=boot((url, opts)=>{requests.push([url, JSON.parse(opts.body)]); return new Promise(r=>finish=r);});
  b.run('curPanel={type:"conv", key:"a"}; modelDraft("a", "opus"); modelDrafts.a.value=null;');
  const pending=b.run('saveModel("a")');
  await Promise.resolve();
  b.run('curPanel={type:"conv", key:"b"}');
  b.node('modelst').textContent='B 状态';
  finish(response({selected_model:null})); await pending;
  assert.deepEqual(requests[0], ['/sessions/a/model', {model:null}]);
  assert.equal(b.node('modelst').textContent, 'B 状态');
});
test('连续模型保存串行发送，最后选择胜出', async () => {
  const pending=[]; const values=[];
  const b=boot((u,o)=>{values.push(JSON.parse(o.body).model); return new Promise(r=>pending.push(r));});
  b.run('modelDraft("a", null).value="opus"');
  const first=b.run('saveModel("a")');
  b.run('modelDrafts.a.value="haiku"; saveModel("a")');
  assert.deepEqual(values,['opus']);
  pending.shift()(response({selected_model:'opus'}));
  for(let i=0;i<8;i++) await Promise.resolve();
  assert.deepEqual(values,['opus','haiku']);
  pending.shift()(response({selected_model:'haiku'})); await first;
  assert.equal(b.run('modelDrafts.a.value'),'haiku');
});
test('cc-switch 应用确认包含整文件覆盖、共享及不重启边界，只发送契约字段', async () => {
  const requests=[]; let warning='';
  const b=boot(async(u,o)=>{requests.push([u,JSON.parse(o.body)]); return response({installed:true,available:true,
    profiles:['prod'],current_profile:'prod',target_cwd:'/wt',target_path:'/wt/.claude/settings.local.json',revision:'r2',backed_up:true});});
  b.context.window.confirm=text=>(warning=text,true);
  b.run('curPanel={type:"conv",key:"a"}; ccStates["/wt"]={data:{installed:true,available:true,profiles:["prod"],target_cwd:"/wt",target_path:"/wt/.claude/settings.local.json",revision:"r1"}, profile:"prod"};');
  await b.run('applyCc("/wt")');
  assert.match(warning,/整文件覆盖/); assert.match(warning,/备份/); assert.match(warning,/同目录/);
  assert.match(warning,/不.*重启/); assert.match(warning,/\/wt\/\.claude\/settings.local.json/);
  assert.deepEqual(requests[0],['/cc-switch/local',{cwd:'/wt',profile:'prod',confirm:true,revision:'r1'}]);
  assert.match(b.run('ccStates["/wt"].message'),/重新托管/);
});
test('不可用 cc-switch 不执行写入，目标 pending 不互相阻塞', async () => {
  const b=boot();
  b.run('ccStates["/a"]={data:{installed:false,available:false},profile:"x"}; ccStates["/b"]={pending:true};');
  await b.run('applyCc("/a")');
  assert.equal(b.run('ccStates["/b"].pending'), true);
});
