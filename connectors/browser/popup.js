'use strict';
const $ = id => document.getElementById(id);
let connection = null, shared = null, busy = false, disconnecting = false;
const status = text => { $('status').textContent = text; };

function serverAddress(value) {
  const url = new URL(value.trim());
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/') throw Error('Use the server address without a path.');
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost','127.0.0.1'].includes(url.hostname))) throw Error('An HTTPS address is required.');
  return url.origin;
}

async function request(method, path, data, config = connection) {
  const headers = {'Content-Type':'application/json'};
  if (config.token) headers.Authorization = `Bearer ${config.token}`;
  const response = await fetch(config.server + path, {method, headers, body:data === undefined ? undefined : JSON.stringify(data), redirect:'error', credentials:'omit', signal:AbortSignal.timeout(15000)});
  if (!response.ok) { const error = Error(`Request failed (${response.status}).`); error.code = response.status; throw error; }
  const text = await response.text();
  if (text.length > 262144) throw Error('Server response was too large.');
  return JSON.parse(text);
}

function render() {
  $('connect-form').hidden = !!connection;
  $('connected').hidden = !connection;
  if (connection) $('identity').textContent = `${connection.revocation_pending ? 'Disconnect pending from' : 'Connected to'} ${new URL(connection.server).hostname}`;
  $('disconnect').textContent = connection?.revocation_pending ? 'Retry disconnect' : 'Disconnect';
  $('shared').textContent = shared ? `Shared: ${shared.title}` : 'No tab shared.';
}

$('connect-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const server = serverAddress($('server').value);
    const allowed = await chrome.permissions.request({origins:[server+'/*']});
    if (!allowed) throw Error('Server access was not granted.');
    const result = await request('POST','/api/nodes/pair',{code:$('code').value.trim(),name:$('name').value.trim(),kind:'browser',capabilities:['browser.read','browser.open']},{server});
    connection = {server, token:result.token, node_id:result.node_id};
    await chrome.storage.local.set({connection});
    $('code').value = '';
    render(); status('Connected. Share a tab to allow reading it.');
  } catch (error) { status(error.code ? 'Could not pair. Check the code and server address.' : error.message); }
  finally { button.disabled = false; }
});

$('share').addEventListener('click', async () => {
  try {
    const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
    if (!tab?.id || !tab.url || !/^https?:/.test(tab.url)) throw Error('Choose a normal website tab first.');
    // activeTab is granted by the user opening this extension from that tab.
    await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>document.title});
    shared = {id:tab.id,origin:new URL(tab.url).origin,title:tab.title || tab.url};
    await chrome.storage.local.set({shared});
    render(); status('Tab shared. Approve a read task in your dashboard.');
  } catch { status('This tab cannot be shared. Open the extension from a normal website tab.'); }
});
$('unshare').addEventListener('click', async () => { shared=null; await chrome.storage.local.remove('shared'); render(); status('Tab sharing stopped.'); });
async function disconnect() {
  if (!connection || disconnecting) return;
  disconnecting=true;
  // Replace the object so any in-flight claim stops before execution.
  connection={...connection,revocation_pending:true};
  const saved=connection;
  shared=null;
  try {
    await chrome.storage.local.set({connection:saved});
    await chrome.storage.local.remove('shared');
    render(); status('Disconnecting. Browser actions are stopped.');
    try { await request('POST','/api/node/disconnect',{},saved); }
    catch(error) { if(error.code!==401) throw error; }
    await chrome.storage.local.remove(['connection','shared']);
    if(connection===saved) connection=null;
    render(); status('Disconnected. Pair again to allow access.');
  } catch {
    render(); status('Browser actions are stopped, but server removal is not confirmed. Retry disconnect when connected.');
  } finally {disconnecting=false;}
}
$('disconnect').addEventListener('click',disconnect);

async function execute(task) {
  const args = task.arguments || {};
  if (task.capability === 'browser.open') {
    const url = new URL(args.url);
    if (!['https:','http:'].includes(url.protocol) || url.username || url.password) throw Error('Only ordinary HTTP or HTTPS links are allowed.');
    const tab = await chrome.tabs.create({url:url.href,active:false});
    return {opened:true,url:url.href,tab_id:tab.id,note:'Open the extension on this tab and share it before requesting a read.'};
  }
  if (task.capability !== 'browser.read') throw Error('Unsupported browser action.');
  if (!shared) throw Error('Share a tab in the browser connector first.');
  if (args.tab_id !== undefined && Number(args.tab_id) !== shared.id) throw Error('Only the shared tab may be read.');
  const tab = await chrome.tabs.get(shared.id);
  if (!tab.url || new URL(tab.url).origin !== shared.origin) throw Error('The tab changed sites. Share it again.');
  const results = await chrome.scripting.executeScript({target:{tabId:shared.id},func:()=>({title:document.title,url:location.href,text:(document.body?.innerText || '').slice(0,16000),truncated:(document.body?.innerText || '').length>16000})});
  return results[0].result;
}

async function tick() {
  if (!connection || connection.revocation_pending || busy) return;
  busy=true;
  const config=connection;
  try {
    await request('POST','/api/node/heartbeat',{},config);
    const store = await chrome.storage.local.get('journal');
    const journal = store.journal || {};
    for (const [id,entry] of Object.entries(journal)) {
      if (!entry.payload || entry.done || entry.node_id !== config.node_id) continue;
      try { await request('POST',`/api/node/tasks/${id}/result`,entry.payload,config); entry.done=true; }
      catch (error) { if ([404,409,410].includes(error.code)) entry.done=true; else throw error; }
    }
    await chrome.storage.local.set({journal});
    if (connection !== config) return;
    const {task} = await request('GET','/api/node/work',undefined,config);
    if (!task) return;
    if (journal[task.id]) { status('A previous task will not be repeated. Review its state in HiveMind.'); return; }
    if (Object.keys(journal).length >= 2500) throw Error('This draft has reached its task history limit. Install a fresh connector profile.');
    journal[task.id]={node_id:config.node_id,started:true};
    await chrome.storage.local.set({journal});
    const payload={lease_id:task.lease_id,status:'completed'};
    try {
      const heartbeat=await request('POST','/api/node/heartbeat',{active_task:{id:task.id,lease_id:task.lease_id}},config);
      if (heartbeat.cancelled_task_ids?.includes(task.id) || connection !== config) throw Error('Cancelled before execution.');
      payload.result=await execute(task);
    } catch(error) { payload.status='failed'; payload.error=error.message.slice(0,300); }
    journal[task.id].payload=payload;
    await chrome.storage.local.set({journal});
    try { await request('POST',`/api/node/tasks/${task.id}/result`,payload,config); journal[task.id].done=true; await chrome.storage.local.set({journal}); }
    catch(error) { if (![404,409,410].includes(error.code)) throw error; }
    if(connection===config) status(payload.status==='completed' ? 'Task completed. Waiting for the next approved task.' : payload.error);
  } catch(error) {
    if(connection!==config) return;
    if ([401,403].includes(error.code)) status('Connection revoked or expired. Disconnect and pair again.');
    else status(error.code ? 'Server unavailable. Reconnecting…' : error.message);
  } finally { busy=false; }
}

chrome.storage.local.get(['connection','shared']).then(saved=>{connection=saved.connection||null;shared=saved.shared||null;render();if(connection)status(connection.revocation_pending ? 'Browser actions are stopped. Retry disconnect to confirm server removal.' : 'Connected while this panel is open.');tick();});
setInterval(tick,2000);
