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
      dataset:{}, style:{}, options:[], appendChild(option){this.options.push(option);}, replaceChildren(){this.options=[];},
      classList:{toggle(){}, contains(){return false;}}, listeners:{},
      addEventListener(type, listener){(this.listeners[type] ||= []).push(listener);},
      dispatchEvent(event){for(const listener of this.listeners[event.type] || []) listener(event);},
      click(){this.clickCount=(this.clickCount || 0)+1; if(this.onclick) this.onclick();}});
    return nodes.get(id);
  };
  const documentListeners = {};
  const historyCalls = [];
  const location = {pathname: "/"};
  const history = {
    pushState(state, title, url){ historyCalls.push(["push", url]); location.pathname = url; },
    replaceState(state, title, url){ historyCalls.push(["replace", url]); location.pathname = url; },
  };
  const context = vm.createContext({document:{
      getElementById:node,
      createElement:tag=>({tagName:tag.toUpperCase(), value:'', textContent:'', dataset:{}}),
      addEventListener(type, listener){(documentListeners[type] ||= []).push(listener);},
    }, window:{confirm:()=>true, location, history},
    localStorage:{getItem(){return null;}}, fetch, AbortController, setTimeout, clearTimeout,
    setInterval(){}, clearInterval(){}, console});
  vm.runInContext(source, context);
  return {node, run: s => vm.runInContext(s, context), context, documentListeners,
          historyCalls, location};
}
const response = data => ({ok:true, status:200, json:async()=>data});
test('现有会话 Enter 发送一次，Shift 与 IME Enter 不发送', async () => {
  const requests=[];
  const b=boot(async(url, opts={})=>{
    requests.push([url, opts.method]);
    if(url==='/sessions/sid/message') return {ok:true,status:202,json:async()=>({job_id:'job-1'})};
    if(url==='/jobs/job-1') return response({status:'done'});
    return response([]);
  });
  b.context.setTimeout = callback => { callback(); return 1; };
  b.run('modelDraft("sid", null); bindSendBox("sid", {})');
  const box=b.node('msgbox');
  box.value='hello';
  const enter={type:'keydown',key:'Enter',shiftKey:false,isComposing:false,keyCode:13,preventDefault(){this.defaultPrevented=true;}};
  box.dispatchEvent(enter);
  assert.equal(enter.defaultPrevented,true);
  assert.equal(b.node('sendbtn').clickCount,1);
  for(let i=0;i<30;i++) await Promise.resolve();
  assert.equal(requests.filter(([url])=>url==='/sessions/sid/message').length,1);

  for(const event of [
    {type:'keydown',key:'Enter',shiftKey:true,isComposing:false,keyCode:13},
    {type:'keydown',key:'Enter',shiftKey:false,isComposing:true,keyCode:13},
    {type:'keydown',key:'Enter',shiftKey:false,isComposing:false,keyCode:229},
  ]){
    event.preventDefault=()=>{event.defaultPrevented=true;};
    box.dispatchEvent(event);
    assert.notEqual(event.defaultPrevented,true);
  }
  assert.equal(b.node('sendbtn').clickCount,1);
  assert.equal(requests.filter(([url])=>url==='/sessions/sid/message').length,1);

  // 新建会话复用同一个 composer，Enter 也应触发发送按钮
  const fresh=boot();
  fresh.run('bindNewBox({cwd:"/project"})');
  const newEnter={type:'keydown',key:'Enter',shiftKey:false,isComposing:false,keyCode:13,preventDefault(){this.defaultPrevented=true;}};
  fresh.node('msgbox').dispatchEvent(newEnter);
  assert.equal(newEnter.defaultPrevented,true);
  assert.equal(fresh.node('sendbtn').clickCount,1);
});
test('默认审批为自动，工作状态下发送默认勾选', () => {
  assert.ok(html.indexOf('value="auto"') < html.indexOf('value="ask"'));
  assert.match(html, /id="forcecb" checked/);
});
test('刷新时保留已展开的工具与信息折叠状态', () => {
  const b = boot();
  b.run('detailsOpen["tool:t1"]=true');
  const tool = b.run('toolPartHtml({type:"tool",tool_use_id:"t1",name:"Bash",input:"ls"})');
  assert.match(tool, /data-dkey="tool:t1"/);
  assert.match(tool, /data-dkey="tool:t1" open/);
  b.run('detailsOpen["tool:t2"]=false');
  const closed = b.run('toolPartHtml({type:"tool",tool_use_id:"t2",name:"Bash",input:"ls"})');
  assert.doesNotMatch(closed, /data-dkey="tool:t2" open/);

  b.run('detailsOpen["info:sid:tools"]=true');
  const info = b.run('infoHtml({id:"sid",tokens:{},tool_uses:{Bash:1},subagents:[],tasks:[]})');
  assert.match(info, /data-dkey="info:sid:tools" open/);

  b.run('detailsOpen["info:sid:tasks"]=false');
  const tasksClosed = b.run('infoHtml({id:"sid",tokens:{},tool_uses:{},subagents:[],tasks:[{id:"1",subject:"x",status:"pending"}],tasks_total:1,tasks_completed:0})');
  assert.doesNotMatch(tasksClosed, /data-dkey="info:sid:tasks" open/);

  const tasksDefault = b.run('infoHtml({id:"other",tokens:{},tool_uses:{},subagents:[],tasks:[{id:"1",subject:"x",status:"pending"}],tasks_total:1,tasks_completed:0})');
  assert.match(tasksDefault, /data-dkey="info:other:tasks" open/);
});
test('toggle 事件记录 details 展开状态', () => {
  const b = boot();
  const handler = b.documentListeners.toggle[0];
  handler({target:{tagName:'DETAILS', dataset:{dkey:'tool:t1'}, open:true}});
  assert.equal(b.run('detailsOpen["tool:t1"]'), true);
  handler({target:{tagName:'DETAILS', dataset:{dkey:'tool:t1'}, open:false}});
  assert.equal(b.run('detailsOpen["tool:t1"]'), false);
});
test('新建会话与对话复用同一个 composer', () => {
  const b = boot();
  const conv = b.run('composerHtml("conv", {hosted:false})');
  const fresh = b.run('composerHtml("new", {})');
  for(const markup of [conv, fresh]){
    assert.match(markup, /id="msgbox"/);
    assert.match(markup, /id="sendbtn"/);
    assert.match(markup, /model-controls/);
    assert.match(markup, /id="approvalsel"/);
    assert.match(markup, /id="hostcb"/);
  }
  assert.match(fresh, /创建并发送/);
  assert.match(conv, /工作状态下也发送/);
});
test('新建会话托管时把审批模式提交给 /hosted', async () => {
  const requests=[];
  const b=boot(async (url, opts={})=>{
    if(url.startsWith('/cc-switch?')) return response({model_options:[]});
    if(url==='/hosted'){
      requests.push(JSON.parse(opts.body));
      return {ok:false,status:500,json:async()=>({error:'stop'})};
    }
    throw Error('unexpected fetch ' + url);
  });
  b.run('view={project:"/project",session:null,new:true}; renderNewSession({cwd:"/project"})');
  b.node('msgbox').value='hello';
  b.node('hostcb').checked=true;
  b.node('approvalsel').value='full';
  await b.node('sendbtn').onclick();
  assert.deepEqual(requests,[{cwd:'/project',text:'hello',model:null,approval:'full'}]);
});
test('运行/排队中仍可发送，只有提交中的防重复点击会禁用', () => {
  const b = boot();
  b.run('curPanel={type:"conv",key:"sid"}');
  for(const status of ['running', 'queued']){
    b.run(`sendJobs["sid"]={status:${JSON.stringify(status)}}`);
    b.run('updateSendSt("sid")');
    assert.equal(b.node('sendbtn').disabled, false, status + ' 不应禁用发送');
  }
  b.run('sendJobs["sid"]={status:"sending"}');
  b.run('updateSendSt("sid")');
  assert.equal(b.node('sendbtn').disabled, true, '提交中应防重复点击');
  // 新建会话在拿到 sid 前不能连续发送，否则会创建多个新会话
  b.run('curPanel={type:"new",key:"/project"}; sendJobs["new:/project"]={status:"running"}');
  b.run('updateSendSt("new:/project")');
  assert.equal(b.node('sendbtn').disabled, true);
});
test('项目会话列表保留 local 配置，对话详情不显示或绑定 local 控件', async () => {
  const list=boot();
  list.run('renderSessionList({cwd:"/project",sessions:[],out:0})');
  assert.match(list.node('main').innerHTML,/ccpanel/);
  assert.match(list.node('main').innerHTML,/项目 local 配置 · cc-switch/);
  assert.equal(list.node('ccpanel').ccPanel,'list:/project');

  const requests=[];
  const detail=boot(async url=>{
    requests.push(url);
    if(url==='/sessions/sid') return response({id:'sid',cwd:'/project',state:'idle',selected_model:null,
      tokens:{},tool_uses:{},user_msgs:0,assistant_msgs:0,subagents:[],tasks:[]});
    if(url.startsWith('/sessions/sid/messages')) return response({size:1,messages:[]});
    if(url==='/cc-switch?cwd=%2Fproject') return response({model_options:[{name:'团队 Sonnet',model:'vendor-sonnet'}]});
    throw Error('unexpected fetch ' + url);
  });
  detail.run('view={project:"/project",session:"sid",new:false}; renderConversation("sid",true)');
  for(let i=0;i<30;i++) await Promise.resolve();
  assert.doesNotMatch(detail.node('main').innerHTML,/ccpanel|项目 local 配置 · cc-switch/);
  assert.equal(detail.node('ccpanel').ccPanel,undefined);
  assert.match(detail.node('main').innerHTML,/model-controls/);
  assert.ok(requests.includes('/cc-switch?cwd=%2Fproject'));
});
test('新建会话不显示或绑定项目 local 控件，列表仍保留 local 配置', async () => {
  const requests=[];
  const b=boot(async url=>{
    requests.push(url);
    if(url==='/cc-switch?cwd=%2Fproject') return response({model_options:[]});
    throw Error('unexpected fetch ' + url);
  });
  b.run('view={project:"/project",session:null,new:true}; renderNewSession({cwd:"/project"})');
  for(let i=0;i<12;i++) await Promise.resolve();
  assert.doesNotMatch(b.node('main').innerHTML,/ccpanel|项目 local 配置 · cc-switch/);
  assert.equal(b.node('ccpanel').ccPanel,undefined);
  assert.ok(requests.includes('/cc-switch?cwd=%2Fproject'));

  b.run('renderSessionList({cwd:"/project",sessions:[],out:0})');
  assert.match(b.node('main').innerHTML,/ccpanel|项目 local 配置 · cc-switch/);
  assert.equal(b.node('ccpanel').ccPanel,'list:/project');
});

test('会话模型控件保留默认和自定义，但不硬编码常规模型', () => {
  const b = boot();
  const controls = b.run('modelControlsHtml()');
  assert.match(controls, /默认（清除 Web 覆盖）/);
  assert.match(controls, /自定义模型 ID/);
  assert.match(controls, /aria-label="模型选择"/);
  assert.doesNotMatch(controls, /Web 选择|Web 模型选择/);
  assert.doesNotMatch(controls, /<option value="(?:opus|sonnet|haiku)">/);
});

test('profile 名称安全显示，选择后只将模型 ID POST 到偏好接口', async () => {
  const requests=[];
  const b=boot(async(url, opts)=>{requests.push([url, JSON.parse(opts.body)]); return response({selected_model:'vendor-sonnet'});});
  b.run('curPanel={type:"conv",key:"sid"}; modelDraft("sid", null); updateModelOptions("sid", [{name:"团队 Sonnet",model:"vendor-sonnet"}]);');
  const select=b.node('modelsel');
  assert.deepEqual(select.options.map(option=>[option.textContent, option.value, option.dataset.kind, option.dataset.model]), [
    ['默认（清除 Web 覆盖）','default','default',undefined],
    ['团队 Sonnet','profile:0','profile','vendor-sonnet'],
    ['自定义 ID','custom','custom',undefined],
  ]);
  b.run('bindModel("sid", null)');
  select.value='profile:0';
  select.onchange();
  for(let i=0;i<8;i++) await Promise.resolve();
  assert.deepEqual(requests, [['/sessions/sid/model',{model:'vendor-sonnet'}]]);
  assert.equal(b.run('modelDraft("sid").value'),'vendor-sonnet');
});

test('四系列选项显示模型值，相同 ID 的不同系列仍可见并提交真实 ID', async () => {
  const requests=[];
  const b=boot(async(url, opts)=>{requests.push([url,JSON.parse(opts.body)]); return response({});});
  b.run(`curPanel={type:"conv",key:"sid"}; modelDraft("sid", null);
    updateModelOptions("sid", [
      {name:"团队 · Opus: shared-id",model:"shared-id"},
      {name:"团队 · Sonnet: shared-id",model:"shared-id"},
      {name:"团队 · Haiku: fast-id",model:"fast-id"},
      {name:"团队 · Fable: reasoning-id",model:"reasoning-id"}
    ]); bindModel("sid", null);`);
  const select=b.node('modelsel');
  assert.deepEqual(select.options.filter(o=>o.dataset.kind==='profile').map(o=>o.textContent), [
    '团队 · Opus: shared-id','团队 · Sonnet: shared-id','团队 · Haiku: fast-id','团队 · Fable: reasoning-id',
  ]);
  select.value='profile:3';
  select.onchange();
  for(let i=0;i<8;i++) await Promise.resolve();
  assert.deepEqual(requests, [['/sessions/sid/model',{model:'reasoning-id'}]]);
});

test('profile ID 与自定义选择使用独立 key，选择恢复及自定义提交不混淆', async () => {
  const requests=[];
  const b=boot(async(url, opts={})=>{
    requests.push([url, opts.body ? JSON.parse(opts.body) : null]);
    return response({selected_model: JSON.parse(opts.body || '{}').model});
  });
  b.run('curPanel={type:"conv",key:"sid"}; modelDraft("sid", null); updateModelOptions("sid", [{name:"特殊profile",model:"__custom_model__"}]); bindModel("sid", null)');
  const select=b.node('modelsel');
  const profile=select.options.find(option=>option.textContent==='特殊profile');
  const custom=select.options.find(option=>option.textContent==='自定义 ID');
  assert.ok(profile);
  assert.notEqual(profile.value, '__custom_model__');
  assert.notEqual(profile.value, custom.value);
  select.value=profile.value;
  select.onchange();
  for(let i=0;i<8;i++) await Promise.resolve();
  assert.deepEqual(requests.at(-1), ['/sessions/sid/model',{model:'__custom_model__'}]);
  b.run('bindModel("sid", "__custom_model__")');
  assert.equal(select.value, profile.value);
  assert.equal(select.options.find(option=>option.value===select.value)?.textContent, '特殊profile');

  select.value=custom.value;
  select.onchange();
  b.node('modelcustom').value='my-custom';
  b.node('modelcustom').oninput();
  b.node('modelcustom').onchange();
  for(let i=0;i<8;i++) await Promise.resolve();
  assert.deepEqual(requests.at(-1), ['/sessions/sid/model',{model:'my-custom'}]);
  assert.ok(requests.every(([,body])=>!body || body.model !== custom.value));
});

test('新建会话从项目 profile 选择后以模型 ID 提交到 /new', async () => {
  const requests=[];
  const b=boot(async(url, opts={})=>{
    if(url.startsWith('/cc-switch?')) return response({model_options:[{name:'团队 Sonnet',model:'vendor-sonnet'}]});
    if(url==='/new'){
      requests.push([url,JSON.parse(opts.body)]);
      return {ok:true,status:202,json:async()=>({job_id:'job-1'})};
    }
    if(url==='/jobs/job-1') return response({status:'done'});
    throw Error('unexpected fetch ' + url);
  });
  b.context.setTimeout = callback => { callback(); return 1; };
  b.run('view={project:"/project",session:null,new:true}; renderNewSession({cwd:"/project"})');
  for(let i=0;i<8;i++) await Promise.resolve();
  const select=b.node('modelsel');
  assert.ok(select.options.some(option=>option.textContent==='团队 Sonnet' && option.dataset.model==='vendor-sonnet'));
  select.value=select.options.find(option=>option.textContent==='团队 Sonnet').value;
  select.onchange();
  b.node('msgbox').value='hello';
  await b.node('sendbtn').onclick();
  assert.deepEqual(requests,[['/new',{cwd:'/project',text:'hello',model:'vendor-sonnet'}]]);
});

test('新建会话成功进入详情后仍以 profile 名称显示所选模型', async () => {
  const b=boot(async(url, opts={})=>{
    if(url.startsWith('/cc-switch?')) return response({model_options:[{name:'团队 Sonnet',model:'vendor-model'}]});
    if(url==='/new') return {ok:true,status:202,json:async()=>({job_id:'job-1'})};
    if(url==='/jobs/job-1') return response({status:'done',result_session_id:'sid'});
    if(url==='/sessions/sid') return response({id:'sid',cwd:'/project',state:'idle',selected_model:null,
      tokens:{},tool_uses:{},user_msgs:0,assistant_msgs:0,subagents:[],tasks:[]});
    if(url.startsWith('/sessions/sid/messages')) return response({size:1,messages:[]});
    throw Error('unexpected fetch ' + url);
  });
  b.context.setTimeout = callback => { callback(); return 1; };
  b.run('view={project:"/project",session:null,new:true}; renderNewSession({cwd:"/project"})');
  for(let i=0;i<8;i++) await Promise.resolve();
  const select=b.node('modelsel');
  select.value=select.options.find(option=>option.textContent==='团队 Sonnet').value;
  select.onchange();
  b.node('msgbox').value='hello';
  await b.node('sendbtn').onclick();
  for(let i=0;i<30;i++) await Promise.resolve();
  assert.equal(b.run('view.session'),'sid');
  assert.equal(select.options.find(option=>option.value===select.value)?.textContent,'团队 Sonnet');
  assert.equal(b.node('modelcustom').hidden,true);
});

test('新建会话进入详情后仍显示无 profile 匹配的自定义模型 ID', async () => {
  const b=boot(async(url, opts={})=>{
    if(url.startsWith('/cc-switch?')) return response({model_options:[{name:'团队 Sonnet',model:'vendor-model'}]});
    if(url==='/new') return {ok:true,status:202,json:async()=>({job_id:'job-1'})};
    if(url==='/jobs/job-1') return response({status:'done',result_session_id:'sid'});
    if(url==='/sessions/sid') return response({id:'sid',cwd:'/project',state:'idle',selected_model:null,
      tokens:{},tool_uses:{},user_msgs:0,assistant_msgs:0,subagents:[],tasks:[]});
    if(url.startsWith('/sessions/sid/messages')) return response({size:1,messages:[]});
    throw Error('unexpected fetch ' + url);
  });
  b.context.setTimeout = callback => { callback(); return 1; };
  b.run('view={project:"/project",session:null,new:true}; renderNewSession({cwd:"/project"})');
  for(let i=0;i<8;i++) await Promise.resolve();
  b.node('modelsel').value='custom';
  b.node('modelcustom').value='my-custom-model';
  b.node('modelcustom').oninput();
  b.node('msgbox').value='hello';
  await b.node('sendbtn').onclick();
  for(let i=0;i<30;i++) await Promise.resolve();
  assert.equal(b.run('view.session'),'sid');
  assert.equal(b.node('modelsel').value,'custom');
  assert.equal(b.node('modelcustom').value,'my-custom-model');
});

test('切入 sid 时 profile options 请求仍 pending，/new 与 /hosted 都能恢复所选 profile', async () => {
  for(const useHost of [false,true]){
    let oldOptionsResolve;
    const sent=[]; let ccRequests=0;
    const b=boot(async(url, opts={})=>{
      if(url.startsWith('/cc-switch?')){
        ccRequests++;
        if(ccRequests===1) return new Promise(resolve=>oldOptionsResolve=resolve);
        return response({model_options:[{name:'团队 Sonnet',model:'vendor-model'}]});
      }
      if(url==='/new' || (url==='/hosted' && opts.method==='POST')){
        sent.push([url,JSON.parse(opts.body)]);
        return {ok:true,status:202,json:async()=>({job_id:'job-1',hosted_id:'host-1'})};
      }
      if(url==='/jobs/job-1') return response({status:'done',result_session_id:'sid'});
      if(url==='/hosted') return response([{hosted_id:'host-1',session_id:'sid',status:'running'}]);
      if(url==='/sessions/sid') return response({id:'sid',cwd:'/project',state:'idle',selected_model:null,
        tokens:{},tool_uses:{},user_msgs:0,assistant_msgs:0,subagents:[],tasks:[]});
      if(url.startsWith('/sessions/sid/messages')) return response({size:1,messages:[]});
      throw Error('unexpected fetch ' + url);
    });
    b.context.setTimeout = callback => { callback(); return 1; };
    b.run('view={project:"/project",session:null,new:true}; renderNewSession({cwd:"/project"})');
    b.run('updateModelOptions("new:/project", [{name:"团队 Sonnet",model:"vendor-model"}]); bindModel("new:/project", null)');
    b.node('modelsel').value='profile:0';
    b.node('modelsel').onchange();
    b.node('msgbox').value='hello';
    if(useHost) b.node('hostcb').checked=true;
    await b.node('sendbtn').onclick();
    for(let i=0;i<30;i++) await Promise.resolve();
    assert.equal(b.run('view.session'),'sid');
    assert.equal(ccRequests,2);
    assert.equal(b.node('modelsel').value,'profile:0');
    assert.equal(b.node('modelsel').options.find(option=>option.value==='profile:0')?.textContent,'团队 Sonnet');
    assert.equal(b.node('modelcustom').hidden,true);
    const target=useHost?'/hosted':'/new';
    const expected=useHost
      ? {cwd:'/project',text:'hello',model:'vendor-model',approval:'auto'}
      : {cwd:'/project',text:'hello',model:'vendor-model'};
    assert.deepEqual(sent,[[target,expected]]);
    oldOptionsResolve(response({model_options:[{name:'团队 Sonnet',model:'vendor-model'}]}));
    for(let i=0;i<5;i++) await Promise.resolve();
    assert.equal(b.node('modelsel').value,'profile:0');
  }
});

test('同一会话旧 cwd 的慢响应不能覆盖新 cwd 的模型选项', async () => {
  const requests=[];
  const b=boot(url=>new Promise(resolve=>requests.push({url,resolve})));
  b.run('curPanel={type:"conv",key:"sid"}; modelDraft("sid", null);');
  const oldRequest=b.run('loadModelOptions("sid", "/old-project")');
  const newRequest=b.run('loadModelOptions("sid", "/new-project")');
  requests[1].resolve(response({model_options:[{name:'New profile',model:'new-model'}]}));
  await newRequest;
  requests[0].resolve(response({model_options:[{name:'Old profile',model:'old-model'}]}));
  await oldRequest;
  assert.deepEqual(requests.map(item=>item.url), [
    '/cc-switch?cwd=%2Fold-project', '/cc-switch?cwd=%2Fnew-project',
  ]);
  assert.deepEqual(b.node('modelsel').options.map(option=>[option.textContent,option.value,option.dataset.kind,option.dataset.model]), [
    ['默认（清除 Web 覆盖）','default','default',undefined],
    ['New profile','profile:0','profile','new-model'], ['自定义 ID','custom','custom',undefined],
  ]);
});

test('异步 profile options 将未改动的已保存模型恢复为 profile label', async () => {
  let finish;
  const b=boot(url=>new Promise(resolve=>finish=resolve));
  b.node('modelsel').options.push(
    {value:'default',dataset:{kind:'default'}}, {value:'custom',dataset:{kind:'custom'}},
  );
  b.run('curPanel={type:"conv",key:"sid"}; modelDraft("sid", "vendor-model"); bindModel("sid", "vendor-model")');
  assert.equal(b.node('modelsel').value,'custom');
  const pending=b.run('loadModelOptions("sid", "/project")');
  finish(response({model_options:[{name:'团队 Profile',model:'vendor-model'}]}));
  await pending;
  assert.equal(b.node('modelsel').value,'profile:0');
  assert.equal(b.node('modelsel').options.find(option=>option.value==='profile:0')?.textContent,'团队 Profile');
  assert.equal(b.node('modelcustom').hidden,true);
  assert.equal(b.run('modelDraft("sid").value'),'vendor-model');
});

test('等待 options 时用户输入的自定义模型草稿不被匹配 profile 覆盖', async () => {
  let finish;
  const b=boot(url=>new Promise(resolve=>finish=resolve));
  b.node('modelsel').options.push(
    {value:'default',dataset:{kind:'default'}}, {value:'custom',dataset:{kind:'custom'}},
  );
  b.run('curPanel={type:"conv",key:"sid"}; modelDraft("sid", "vendor-model"); bindModel("sid", "vendor-model")');
  b.node('modelcustom').value='user-custom-model';
  b.node('modelcustom').oninput();
  const pending=b.run('loadModelOptions("sid", "/project")');
  finish(response({model_options:[{name:'团队 Profile',model:'vendor-model'}]}));
  await pending;
  assert.equal(b.node('modelsel').value,'custom');
  assert.equal(b.node('modelcustom').value,'user-custom-model');
  assert.equal(b.run('modelDraft("sid").value'),'user-custom-model');
});

test('模型 option 处理兼容没有 find 方法的浏览器 options collection', () => {
  const b=boot(async()=>response({}));
  b.run('curPanel={type:"conv",key:"sid"}; modelDraft("sid", null); updateModelOptions("sid", [{name:"团队 Profile",model:"vendor-model"}]); bindModel("sid", null)');
  const select=b.node('modelsel');
  const items=select.options;
  const options={length:items.length};
  items.forEach((option,index)=>{options[index]=option;});
  select.options=options;
  select.value='profile:0';
  assert.doesNotThrow(()=>select.onchange());
  assert.equal(b.run('modelDraft("sid").value'),'vendor-model');
});

test('异步 profile 响应保留正在编辑的模型 ID 与消息草稿', async () => {
  let finish, requested;
  const b=boot(url=>{requested=url; return new Promise(resolve=>finish=resolve);});
  b.run('curPanel={type:"conv",key:"sid"}; modelDraft("sid", null);');
  b.run('document.getElementById("main").innerHTML=""');
  b.node('modelsel').options.push(
    {value:'default',dataset:{kind:'default'}}, {value:'custom',dataset:{kind:'custom'}},
  );
  b.node('modelsel').value='custom';
  b.node('modelcustom').value='draft-model';
  b.run('bindModel("sid", null)');
  b.node('modelsel').value='custom';
  b.node('modelcustom').value='draft-model';
  b.node('modelcustom').oninput();
  b.node('msgbox').value='message draft';
  const pending=b.run('loadModelOptions("sid", "/project")');
  finish(response({model_options:[{name:'团队 Sonnet',model:'vendor-sonnet'}]}));
  await pending;
  assert.equal(requested,'/cc-switch?cwd=%2Fproject');
  assert.equal(b.node('modelsel').value,'custom');
  assert.equal(b.node('modelcustom').value,'draft-model');
  assert.equal(b.run('modelDraft("sid").value'),'draft-model');
  assert.equal(b.node('msgbox').value,'message draft');
});

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
test('审批卡片与富工具结果渲染，中断按钮随托管状态启用', () => {
  const b=boot();
  const card=b.run('interactionHtml({id:"r1",tool_name:"Bash",preview:{kind:"text",text:"ls -la"},' +
    'allow_always:true,reason:"runs a command",state:"pending"})');
  assert.match(card,/需要审批/); assert.match(card,/始终允许/); assert.match(card,/ls -la/);
  const result=b.run('convHtml([{role:"assistant",ts:0,parts:[{type:"tool",name:"Edit",' +
    'input:"{}",result:{kind:"diff",text:"-old"+String.fromCharCode(10)+"+new"}}]}])');
  assert.match(result,/diff-del/); assert.match(result,/diff-add/);
  const standalone=b.run('convHtml([{role:"tool",ts:0,parts:[{type:"tool_result",' +
    'kind:"text",text:"tool output",is_error:false}]}])');
  assert.match(standalone,/tool output/);
  b.run('updateHostedControls({hosted:true,hosted_busy:false,hosted_queued:2,approval:"full"})');
  assert.equal(b.node('intbtn').disabled,true);
  assert.equal(b.node('approvalsel').value,'full');
  b.run('updateHostedControls({hosted:true,hosted_busy:true,approval:"ask"})');
  assert.equal(b.node('intbtn').disabled,false);
});
test('全局待审批计数显示与隐藏', () => {
  const b=boot();
  b.run('sessions=[{id:"a",pending_interactions:1},{id:"b",pending_interactions:2}]');
  b.run('renderPendingBadge()');
  assert.equal(b.node('pendingbadge').textContent,'待审批 3');
  assert.equal(b.node('pendingbadge').style.display,'inline-block');
  b.run('sessions=[{id:"a",pending_interactions:0}]');
  b.run('renderPendingBadge()');
  assert.equal(b.node('pendingbadge').style.display,'none');
});
test('全局托管计数显示、隐藏与点击跳转', () => {
  const b=boot();
  b.run('sessions=[{id:"a",hosted:true},{id:"b",hosted:false},{id:"c",hosted:true}]');
  b.run('renderHostedBadge()');
  assert.equal(b.node('hostedbadge').textContent,'托管 2');
  assert.equal(b.node('hostedbadge').style.display,'inline-block');
  b.run('sessions=[{id:"a",hosted:false}]');
  b.run('renderHostedBadge()');
  assert.equal(b.node('hostedbadge').style.display,'none');
  // 有托管时点击跳到第一个托管会话
  b.run('sessions=[{id:"a",project:"/p",hosted:false},{id:"b",project:"/p",hosted:true}]');
  b.node('hostedbadge').click();
  assert.deepEqual(b.run('view'), {project:"/p",session:"b",new:false});
});
test('项目侧栏按名称固定排序，首次进入仍选中最近活跃项目', () => {
  const b=boot();
  const pairs = [
    {id:'1',cwd:'/b/project-b',project:'/b/project-b',state:'idle',last_event_at:900},
    {id:'2',cwd:'/a/project-a',project:'/a/project-a',state:'working',last_event_at:100},
  ];
  b.run(`sessions=${JSON.stringify(pairs)}`);
  assert.equal(b.run('groupByProject().map(p=>p.cwd).join(",")'), '/a/project-a,/b/project-b');
  assert.equal(b.run('view={project:null,session:null,new:false}; renderMain(); view.project'), '/b/project-b');
  // 活跃时间变化不再影响侧栏顺序
  b.run('sessions[1].last_event_at=9999');
  assert.equal(b.run('groupByProject().map(p=>p.cwd).join(",")'), '/a/project-a,/b/project-b');
});
test('SSE 快照更新会话列表与流式文本状态', async () => {
  const b=boot();
  b.run(`view={project:"/p",session:null,new:false}; onGlobalSse({data: JSON.stringify(
    {type:"snapshot",seq:1,session:null,
     data:{sessions:[{id:"s1",project:"/p",cwd:"/p",state:"idle",
       last_event_at:1,last_input_at:1}],interactions:[]}})});`);
  assert.equal(b.run('sessions.length'),1);
  assert.equal(b.run('globalSeq'),1);
  b.run('view={project:"/p",session:"s1",new:false}; ' +
    'onGlobalSse({data: JSON.stringify({type:"turn_delta",seq:2,session:"s1",' +
    'data:{text:"你好"}})});');
  await new Promise(r => setTimeout(r, 20));
  assert.equal(b.run('liveText'),'你好');
  b.run('onGlobalSse({data: JSON.stringify({type:"turn_finished",seq:3,session:"s1",data:{}})});');
  assert.equal(b.run('liveText'),'');
});
test('对话分页状态渲染与加载更早按钮', () => {
  const b=boot();
  b.run('convState={id:"s1",msgs:[' +
    '{role:"user",ts:0,parts:[{type:"text",text:"old"}]},' +
    '{role:"assistant",ts:1,parts:[{type:"text",text:"new"}]}],' +
    'total:10,start:8,size:1,hasMore:true,loadingEarlier:false}; renderConvList();');
  const html=b.node('conv').innerHTML;
  assert.match(html,/old/); assert.match(html,/new/); assert.match(html,/loadearlier/);
  assert.equal(b.node('convcount').textContent,'对话 (2/10)');
  b.run('convState.hasMore=false; renderConvList();');
  assert.doesNotMatch(b.node('conv').innerHTML,/loadearlier/);
});
test('计划与选择卡片渲染及答案收集', () => {
  const b=boot();
  const plan=b.run('interactionHtml({id:"p1",kind:"plan",state:"pending",' +
    'preview:{kind:"text",text:"# Plan"}})');
  assert.match(plan,/批准计划/); assert.match(plan,/继续规划/);
  assert.match(plan,/keep planning/);
  const choice=b.run('interactionHtml({id:"c1",kind:"choice",state:"pending",questions:[' +
    '{question:"Q1",header:"URL 方案",multiSelect:true,' +
    'options:[{label:"A",description:"说明 A"},{label:"B",preview:"code B"}]}]})');
  assert.match(choice,/checkbox/); assert.match(choice,/Q1/);
  assert.match(choice,/URL 方案/); assert.match(choice,/说明 A/);
  assert.match(choice,/choice-opt/); assert.match(choice,/data-act="choice-submit"/);
  // 选项 preview 渲染为等宽块；提交按钮初始禁用；跳过带明确拒绝语义
  assert.match(choice,/opt-preview/); assert.match(choice,/code B/);
  assert.match(choice,/choice-submit" disabled/);
  assert.match(choice,/declined to answer; continue in the conversation/);
  const free=b.run('collectChoiceAnswers({querySelectorAll:()=>[' +
    '{dataset:{q:"Q1"},querySelector:()=>({value:"custom"}),querySelectorAll:()=>[]}]})');
  assert.equal(JSON.stringify(free), JSON.stringify({Q1:'custom'}));
  const picked=b.run('collectChoiceAnswers({querySelectorAll:()=>[' +
    '{dataset:{q:"Q1"},querySelector:()=>null,' +
    'querySelectorAll:()=>[{dataset:{label:"A"}},{dataset:{label:"B"}}]}]})');
  assert.equal(JSON.stringify(picked), JSON.stringify({Q1:'A, B'}));
});
test('交互卡片按卡片级 diff 保留作答中的 DOM，提交按钮随作答启停', () => {
  const b=boot();
  // 给 interactions 容器与 template 装最小 DOM shim
  const el=b.node('interactions');
  const children=[];
  Object.defineProperty(el,'firstChild',{get:()=>children[0]||null});
  el.querySelectorAll=()=>[...children];
  el.insertBefore=(card,anchor)=>{
    const i=anchor?children.indexOf(anchor):children.length;
    children.splice(i<0?children.length:i,0,card); return card;};
  let seq=0;
  b.context.document.createElement=()=>{
    const card={dataset:{sig:'sig-'+(++seq)},removed:false,
      remove(){this.removed=true;const i=children.indexOf(this);if(i>=0)children.splice(i,1);},
      querySelector(){return null;},querySelectorAll(){return [];}};
    Object.defineProperty(card,'nextSibling',{get(){return children[children.indexOf(this)+1]||null;}});
    const tpl={};
    // data-sig 是 diff 的键，从 interactionHtml 生成的 HTML 里解析回卡片
    Object.defineProperty(tpl,'innerHTML',{set(v){
      const m=v.match(/data-sig="([^"]*)"/); if(m) card.dataset.sig=m[1];}});
    Object.defineProperty(tpl,'content',{value:{firstElementChild:card}});
    return tpl;};
  const choiceA={id:'a',state:'pending',kind:'choice',questions:[]};
  const permB={id:'b',state:'pending',tool_name:'Bash',preview:{kind:'text',text:'ls'}};
  const permC={id:'c',state:'pending',tool_name:'Bash',preview:{kind:'text',text:'pwd'}};
  b.run(`renderInteractions([${JSON.stringify(choiceA)},${JSON.stringify(permB)}])`);
  assert.equal(children.length,2);
  const cardA=children[0];
  // 其他交互解决/新增：作答中的 choice 卡 DOM 原样保留，勾选和输入不丢失
  b.run(`renderInteractions([${JSON.stringify(choiceA)},${JSON.stringify(permC)}])`);
  assert.equal(children[0],cardA);
  assert.equal(children.length,2);
  // 自身状态变化（解决）才重建
  b.run(`renderInteractions([${JSON.stringify({...choiceA,state:'allowed'})}])`);
  assert.notEqual(children[0],cardA);
  b.run('renderInteractions([])');
  assert.equal(children.length,0);
  // refreshChoiceSubmit：每题都有答案（自由文本或勾选）才启用提交
  const disabled=b.run('(function(){const btn={disabled:true,title:"t"};' +
    'const mkq=(free,checked)=>({querySelector:s=>s===".choice-free"?{value:free}:' +
    '(s==="input:checked"?(checked[0]||null):null),' +
    'querySelectorAll:s=>checked});' +
    'const card={querySelector:s=>btn,querySelectorAll:s=>[mkq("hi",[]),mkq("",[])]};' +
    'refreshChoiceSubmit(card);return btn.disabled;})()');
  assert.equal(disabled,true);
  const enabled=b.run('(function(){const btn={disabled:true,title:"t"};' +
    'const mkq=(free,checked)=>({querySelector:s=>s===".choice-free"?{value:free}:' +
    '(s==="input:checked"?(checked[0]||null):null),' +
    'querySelectorAll:s=>checked});' +
    'const card={querySelector:s=>btn,querySelectorAll:s=>[mkq("hi",[]),mkq("",[{dataset:{label:"A"}}])]};' +
    'refreshChoiceSubmit(card);return btn.disabled;})()');
  assert.equal(enabled,false);
});
test('SSE 事件触发的列表刷新遵循用户设置的间隔', async () => {
  const requested = [];
  const b = boot(async url => { requested.push(url); return response([]); });
  const timers = [];
  b.context.setTimeout = (fn, ms) => { timers.push({fn, ms}); return timers.length; };
  b.context.clearTimeout = id => { if(id && timers[id - 1]) timers[id - 1].cancelled = true; };
  b.node('ival').value = '5000';
  await b.run('load()');                    // 页面首屏加载 /sessions
  assert.deepEqual(requested, ['/sessions']);
  timers.length = 0;
  b.run('scheduleListRefresh()');           // 紧接着到达一条 SSE 事件
  assert.equal(timers.length, 1);
  assert.ok(timers[0].ms >= 4900,
    `用户间隔 5s 时事件不应立刻刷新列表，实际排定 ${timers[0].ms}ms`);
});
test('API 重试横幅显示错误与重试次数，清除后隐藏', () => {
  const b=boot();
  const el=b.node('apierr');
  b.run('renderApiError({text:"503 model temporarily unavailable",ts:Date.now()/1000,' +
    'retry_attempt:3,max_retries:10,retry_in_ms:3000})');
  assert.match(el.textContent, /503 model temporarily unavailable/);
  assert.match(el.textContent, /3\/10/);
  assert.match(el.textContent, /3 秒后/);
  assert.notEqual(el.style.display, 'none');
  b.run('renderApiError(null)');
  assert.equal(el.textContent, '');
  assert.equal(el.style.display, 'none');
});
test('URL 路由使用可读路径，并能解析旧编码链接', () => {
  const b = boot();
  const cwd = 'F:\\code\\proj one';
  b.context.v = {project: cwd, session: null, new: false};
  assert.equal(b.run('routePath(v)'), '/p/F:/code/proj%20one');
  b.context.v = {project: cwd, session: '0f3c-uuid', new: false};
  assert.equal(b.run('routePath(v)'), '/p/F:/code/proj%20one/s/0f3c-uuid');
  b.context.v = {project: cwd, session: null, new: true};
  assert.equal(b.run('routePath(v)'), '/p/F:/code/proj%20one/new');
  b.context.v = {project: null, session: null, new: false};
  assert.equal(b.run('routePath(v)'), '/');
  // POSIX 绝对路径不应出现 /p//home/... 这样的双斜杠
  b.context.v = {project: '/home/me/proj', session: null, new: false};
  assert.equal(b.run('routePath(v)'), '/p/home/me/proj');

  b.context.path = '/p/F:/code/proj%20one/s/abc';
  assert.deepEqual({...b.run('parseRoute(path)')},
    {project: 'F:/code/proj one', session: 'abc', new: false,
     fullProject: 'F:/code/proj one/s/abc'});
  b.context.path = '/p/F:/code/proj%20one';
  assert.deepEqual({...b.run('parseRoute(path)')},
    {project: 'F:/code/proj one', session: null, new: false,
     fullProject: 'F:/code/proj one'});
  b.context.path = '/p/F:/code/proj%20one/new';
  assert.deepEqual({...b.run('parseRoute(path)')},
    {project: 'F:/code/proj one', session: null, new: true,
     fullProject: 'F:/code/proj one/new'});
  // 旧格式（整条路径一个编码段）仍可解析
  b.context.path = '/p/F%3A%5Ccode%5Cproj%20one/s/abc';
  assert.equal(b.run('parseRoute(path).project'), cwd);

  for(const bad of ['/', '/p/', '/s/abc', '/p/%E0%A4%A']){
    b.context.path = bad;
    assert.equal(b.run('parseRoute(path)'), null, bad);
  }

  // 目录本身叫 new 时优先按项目列表解析
  b.context.__projects = [{cwd: 'F:\\code\\new'}];
  b.context.__route = {project: 'F:/code', session: null, new: true,
                       fullProject: 'F:/code/new'};
  b.run('view = __route; resolveViewProject(__projects)');
  assert.equal(b.run('view.project'), 'F:\\code\\new');
  assert.equal(b.run('view.new'), false);
});
test('导航同步 URL，并在默认项目解析后替换首页地址', () => {
  const b = boot();
  b.context.__s = {id:'s1', project:'F:\\code\\p', cwd:'F:\\code\\p',
                   state:'idle', tokens:{}};
  b.run('sessions = [__s]');

  b.context.__next = {project:'F:\\code\\p', session:null, new:false};
  b.run('navigate(__next)');
  assert.deepEqual(b.historyCalls, [['push', '/p/F:/code/p']]);
  assert.equal(b.location.pathname, '/p/F:/code/p');

  b.context.__next = {project:'F:\\code\\p', session:'s1', new:false};
  b.run('navigate(__next)');
  assert.deepEqual(b.historyCalls[1], ['push', '/p/F:/code/p/s/s1']);
  assert.equal(b.run('view.session'), 's1');

  b.context.__next = {project:'F:\\code\\p', session:null, new:false};
  b.run('navigate(__next)');
  assert.equal(b.historyCalls.length, 3);

  // 回到根路径：数据已加载，应自动选中项目并 replace 成项目地址
  b.location.pathname = '/';
  b.run('navigate({project:null, session:null, new:false})');
  assert.deepEqual(b.historyCalls[3], ['replace', '/p/F:/code/p']);
});

test('子代理折叠区显示运行数与状态圆点', () => {
  const b = boot();
  const html = b.run(`infoHtml({id:"s1", tokens:{}, subagents:[
    {type:"Explore", description:"scan", state:"running"},
    {type:"claude", description:"write", state:"done"},
    {type:"claude", description:"bad", state:"error"},
  ], tasks:[]})`);
  assert.ok(html.includes('1 运行中'));
  assert.ok(html.includes('dot run'));
  assert.ok(html.includes('dot done'));
  assert.ok(html.includes('dot err'));
});

test('会话卡片 chip 显示运行中子代理数', () => {
  const b = boot();
  b.run(`renderSessionList({cwd:"/p", sessions:[
    {id:"s1", state:"idle", subagent_count:3, subagents_running:2},
    {id:"s2", state:"idle", subagent_count:1, subagents_running:0},
  ], out:0})`);
  const html = b.node("main").innerHTML;
  assert.ok(html.includes('3 子代理 · 2 运行中'));
  assert.ok(html.includes('1 子代理'));
  assert.ok(!html.includes('1 子代理 ·'));   // 无运行中时不追加
});

test('command part 渲染为命令气泡与折叠输出', () => {
  const b = boot();
  const html = b.run(`convHtml([{role:"system", ts:0, parts:[
    {type:"command", command:"/model", args:"deepseek-v4.1-flash"},
    {type:"command_output", text:"switched"},
  ]}])`);
  assert.ok(html.includes('class="cmd"'));
  assert.ok(html.includes('/model'));
  assert.ok(html.includes('deepseek-v4.1-flash'));
  assert.ok(html.includes('命令输出'));
  assert.ok(html.includes('switched'));
});

test('sendJobText 对斜杠命令显示命令文案', () => {
  const b = boot();
  b.run(`sendJobs["s1"] = {status:"sending", text:"/model deepseek"}`);
  assert.equal(b.run(`sendJobText("s1")`), "命令 /model 发送中…");
  b.run(`sendJobs["s1"] = {status:"queued", text:"/model deepseek"}`);
  assert.equal(b.run(`sendJobText("s1")`), "命令 /model 排队中…");
  b.run(`sendJobs["s1"] = {status:"running", text:"/model deepseek"}`);
  assert.equal(b.run(`sendJobText("s1")`), "正在执行命令 /model…");
  b.run(`sendJobs["s1"] = {status:"running", text:"hello"}`);
  assert.equal(b.run(`sendJobText("s1")`), "Claude 正在处理…");
});

test('202 后保留 sendJobs 原文，命令状态文案不中断', async () => {
  // bindSendBox 全流程：POST 返回 202 后重建 sendJobs，若丢 text 字段，
  // 斜杠命令的状态文案会回落成普通"排队中/Claude 正在处理"
  const texts = [];
  let polls = 0;
  const b = boot(async(url)=>{
    if(url==='/sessions/sid/message') return {ok:true,status:202,json:async()=>({job_id:'job-1'})};
    if(url==='/jobs/job-1') return response({status: ++polls === 1 ? 'queued' : 'done'});
    if(url==='/sessions') return response([]);
    throw Error('unexpected fetch ' + url);
  });
  b.context.texts = texts;
  b.context.setTimeout = callback => { callback(); return 1; };
  b.run('curPanel={type:"conv",key:"sid"}; modelDraft("sid", null); bindSendBox("sid", {})');
  // 收集每次 updateSendSt 时的状态文案
  b.run('const _oldUpd = updateSendSt; updateSendSt = id => { _oldUpd(id); texts.push(sendJobText(id)); };');
  b.node('msgbox').value='/model x';
  await b.node('sendbtn').onclick();
  assert.ok(texts.includes('命令 /model 排队中…') || texts.includes('正在执行命令 /model…'),
    '命令状态文案应出现，实际: ' + JSON.stringify(texts));
  // 反向断言：queued/running 阶段不应回落成裸的普通文案
  assert.ok(!texts.includes('排队中…'), '202 后不应丢失命令前缀，实际: ' + JSON.stringify(texts));
});

test('echoVisible 按文本对账', () => {
  const b = boot();
  assert.equal(b.run(`echoVisible([], {text:" hi "})`), true);
  assert.equal(b.run(`echoVisible([{role:"user", parts:[{type:"text", text:"hi"}]}], {text:"hi"})`), false);
  assert.equal(b.run(`echoVisible([{role:"assistant", parts:[{type:"text", text:"hi"}]}], {text:"hi"})`), true);
  assert.equal(b.run(`echoVisible(null, {text:"hi"})`), false);
  assert.equal(b.run(`echoVisible([], null)`), false);
  assert.equal(b.run(`echoVisible([{role:"system", parts:[{type:"command", command:"/model", args:"deepseek"}]}], {text:"/model deepseek"})`), false);
  assert.equal(b.run(`echoVisible([{role:"system", parts:[{type:"command", command:"/compact"}]}], {text:"/compact"})`), false);
});

test('发送 202 后 pendingEcho 记录并在完成后清除', async () => {
  // 202 后立即有回显；job 到达非 failed 终态时消息必已落盘，未对账的回显兜底清除
  const echoStates = [];
  const b = boot(async(url) => {
    if(url === "/sessions/sid/message") return {ok:true, status:202, json:async()=>({job_id:"j1"})};
    if(url === "/jobs/j1") return response({status:"done"});
    return response({});
  });
  b.context.echoStates = echoStates;
  b.context.setTimeout = callback => { callback(); return 1; };
  b.run('curPanel={type:"conv",key:"sid"}; modelDraft("sid", null); bindSendBox("sid", {});');
  // 每次状态更新时采样回显文本，覆盖 202 后、轮询中的窗口
  b.run('const _oldUpd2 = updateSendSt; updateSendSt = id => { _oldUpd2(id); echoStates.push(pendingEcho["sid"]?.text); };');
  b.node("msgbox").value = "hello";
  b.node("sendbtn").click();
  for(let i = 0; i < 30; i++) await Promise.resolve();
  assert.ok(echoStates.includes("hello"), "202 后应有回显，实际: " + JSON.stringify(echoStates));
  assert.equal(b.run(`pendingEcho["sid"]`), undefined, "job 完成后回显应兜底清除");
});

test('job 终态失败后回显标记发送失败', async () => {
  const b = boot(async(url) => {
    if(url === "/sessions/sid/message") return {ok:true, status:202, json:async()=>({job_id:"j1"})};
    if(url === "/jobs/j1") return response({status:"failed", error:"x"});
    return response({});
  });
  b.context.setTimeout = callback => { callback(); return 1; };
  b.run('bindSendBox("sid", {})');
  b.node("msgbox").value = "hello";
  b.node("sendbtn").click();
  for(let i = 0; i < 30; i++) await Promise.resolve();
  assert.equal(b.run(`pendingEcho["sid"]?.failed`), true);
});

test('旧任务失败不污染最新一条回显', async () => {
  // 托管会话运行中可继续发送：第二条发送覆盖 pendingEcho 后，
  // 第一条的轮询循环才收到 job failed——文本守卫应拦住，不把新回显标成失败
  let gateJ1, gateJ2, post = 0, j1polls = 0, j2polls = 0;
  const b = boot(async(url) => {
    if(url === '/sessions/sid/message'){
      post++;
      return {ok:true, status:202, json:async()=>({job_id: post === 1 ? 'j1' : 'j2'})};
    }
    if(url === '/jobs/j1'){
      j1polls++;
      if(j1polls === 1) return new Promise(r => gateJ1 = r);   // 挂起第一条的轮询
      return response({status:'failed', error:'x'});
    }
    if(url === '/jobs/j2'){
      j2polls++;
      if(j2polls === 1) return new Promise(r => gateJ2 = r);   // 第二条也停在轮询，回显保持在场
      return response({status:'done'});
    }
    return response({});
  });
  b.context.setTimeout = callback => { callback(); return 1; };
  b.run('bindSendBox("sid", {})');
  b.node('msgbox').value = 'first';
  b.node('sendbtn').click();                        // 第一条发送并停在轮询
  for(let i = 0; i < 30; i++) await Promise.resolve();
  assert.equal(b.run(`pendingEcho["sid"]?.text`), 'first');
  b.node('msgbox').value = 'second';
  const p2 = b.node('sendbtn').onclick();           // 第二条发送，轮询同样挂起
  for(let i = 0; i < 30; i++) await Promise.resolve();
  assert.equal(b.run(`pendingEcho["sid"]?.text`), 'second');
  gateJ1(response({status:'failed', error:'x'}));    // 旧任务此时才失败
  for(let i = 0; i < 50; i++) await Promise.resolve();
  assert.equal(b.run(`pendingEcho["sid"]?.text`), 'second',
    '旧任务的失败不应清除/影响最新回显');
  assert.equal(b.run(`pendingEcho["sid"]?.failed`), undefined,
    '旧任务的失败不应标记最新回显');
  gateJ2(response({status:'done'}));                 // 第二条 done：兜底清除自己的回显
  await p2;
  for(let i = 0; i < 30; i++) await Promise.resolve();
  assert.equal(b.run(`pendingEcho["sid"]`), undefined, 'done 终态应兜底清除回显');
});

test('renderConvList 对账：落盘回显退场，未落盘保留', () => {
  const b = boot();
  b.run(`convState = {id:"sid", total:1, hasMore:false, loadingEarlier:false,
    msgs:[{role:"user", ts:1, parts:[{type:"text", text:"hello"}]}]}`);
  b.run('pendingEcho["sid"] = {text:"hello"}');
  b.run('renderConvList()');
  assert.equal(b.run('pendingEcho["sid"]'), undefined);
  assert.ok(!b.node('conv').innerHTML.includes('pending'), '落盘的回显不应再渲染');
  b.run('pendingEcho["sid"] = {text:"not yet"}');
  b.run('renderConvList()');
  assert.ok(b.node('conv').innerHTML.includes('pending'));
  assert.ok(b.node('conv').innerHTML.includes('not yet'));
});
