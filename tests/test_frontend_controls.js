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
    assert.match(markup, /id="hostcb"/);
  }
  assert.match(fresh, /创建并发送/);
  assert.match(conv, /工作状态下也发送/);
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
    assert.deepEqual(sent,[[target,{cwd:'/project',text:'hello',model:'vendor-model'}]]);
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
  const card=b.run('interactionsHtml([{id:"r1",tool_name:"Bash",preview:{kind:"text",text:"ls -la"},' +
    'allow_always:true,reason:"runs a command"}])');
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
  const plan=b.run('interactionsHtml([{id:"p1",kind:"plan",' +
    'preview:{kind:"text",text:"# Plan"}}])');
  assert.match(plan,/批准计划/); assert.match(plan,/继续规划/);
  assert.match(plan,/keep planning/);
  const choice=b.run('interactionsHtml([{id:"c1",kind:"choice",questions:[' +
    '{question:"Q1",header:"URL 方案",multiSelect:true,' +
    'options:[{label:"A",description:"说明 A"},{label:"B"}]}]}])');
  assert.match(choice,/checkbox/); assert.match(choice,/Q1/);
  assert.match(choice,/URL 方案/); assert.match(choice,/说明 A/);
  assert.match(choice,/choice-opt/); assert.match(choice,/data-act="choice-submit"/);
  const free=b.run('collectChoiceAnswers({querySelectorAll:()=>[' +
    '{dataset:{q:"Q1"},querySelector:()=>({value:"custom"}),querySelectorAll:()=>[]}]})');
  assert.equal(JSON.stringify(free), JSON.stringify({Q1:'custom'}));
  const picked=b.run('collectChoiceAnswers({querySelectorAll:()=>[' +
    '{dataset:{q:"Q1"},querySelector:()=>null,' +
    'querySelectorAll:()=>[{dataset:{label:"A"}},{dataset:{label:"B"}}]}]})');
  assert.equal(JSON.stringify(picked), JSON.stringify({Q1:'A, B'}));
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
test('URL 路由可生成并解析项目/会话/新建视图', () => {
  const b = boot();
  const cwd = 'F:\\code\\proj one';
  b.context.v = {project: cwd, session: null, new: false};
  assert.equal(b.run('routePath(v)'), '/p/F%3A%5Ccode%5Cproj%20one');
  b.context.v = {project: cwd, session: '0f3c-uuid', new: false};
  assert.equal(b.run('routePath(v)'), '/p/F%3A%5Ccode%5Cproj%20one/s/0f3c-uuid');
  b.context.v = {project: cwd, session: null, new: true};
  assert.equal(b.run('routePath(v)'), '/p/F%3A%5Ccode%5Cproj%20one/new');
  b.context.v = {project: null, session: null, new: false};
  assert.equal(b.run('routePath(v)'), '/');

  b.context.path = '/p/F%3A%5Ccode%5Cproj%20one/s/abc';
  assert.deepEqual({...b.run('parseRoute(path)')}, {project: cwd, session: 'abc', new: false});
  b.context.path = '/p/F%3A%5Ccode%5Cproj%20one';
  assert.deepEqual({...b.run('parseRoute(path)')}, {project: cwd, session: null, new: false});
  b.context.path = '/p/F%3A%5Ccode%5Cproj%20one/new';
  assert.deepEqual({...b.run('parseRoute(path)')}, {project: cwd, session: null, new: true});

  for(const bad of ['/', '/p/', '/p/x/unknown', '/p/x/s/', '/s/abc', '/p/%E0%A4%A']){
    b.context.path = bad;
    assert.equal(b.run('parseRoute(path)'), null, bad);
  }
});
test('导航同步 URL，并在默认项目解析后替换首页地址', () => {
  const b = boot();
  b.context.__s = {id:'s1', project:'F:\\code\\p', cwd:'F:\\code\\p',
                   state:'idle', tokens:{}};
  b.run('sessions = [__s]');

  b.context.__next = {project:'F:\\code\\p', session:null, new:false};
  b.run('navigate(__next)');
  assert.deepEqual(b.historyCalls, [['push', '/p/F%3A%5Ccode%5Cp']]);
  assert.equal(b.location.pathname, '/p/F%3A%5Ccode%5Cp');

  b.context.__next = {project:'F:\\code\\p', session:'s1', new:false};
  b.run('navigate(__next)');
  assert.deepEqual(b.historyCalls[1], ['push', '/p/F%3A%5Ccode%5Cp/s/s1']);
  assert.equal(b.run('view.session'), 's1');

  b.context.__next = {project:'F:\\code\\p', session:null, new:false};
  b.run('navigate(__next)');
  assert.equal(b.historyCalls.length, 3);

  // 回到根路径：数据已加载，应自动选中项目并 replace 成项目地址
  b.location.pathname = '/';
  b.run('navigate({project:null, session:null, new:false})');
  assert.deepEqual(b.historyCalls[3], ['replace', '/p/F%3A%5Ccode%5Cp']);
});
