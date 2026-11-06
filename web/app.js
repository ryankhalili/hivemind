'use strict';

const $ = selector => document.querySelector(selector);
const state = { data: null, tab: 'chat', pending: false, refreshing: false, signatures: {}, timer: null, selectedTask: null, permissionsNode: null, connectionLost: false, authView: 'login', sessionEpoch: 0 };
const terminal = new Set(['completed', 'failed', 'cancelled', 'outcome_unknown']);
let accountTimer = null;
let accountReady = false;
const statuses = {planning:'Choosing the next action',awaiting_approval:'Your approval is needed',queued:'Waiting for device to accept',waiting_device:'Waiting for device',waiting_children:'Waiting for the current step',running:'Running',cancel_requested:'Stopping',completed:'Completed',failed:'Failed',cancelled:'Cancelled',outcome_unknown:'Outcome unconfirmed'};
const capabilityLabels = {'device.info':'Device information','files.list':'List shared files','files.read':'Read shared files','files.write':'Create shared files','browser.open':'Open browser tabs','browser.read':'Read shared tabs','command.run':'Run commands','harness.run':'Run a device agent','cloud.answer':'Cloud response'};
const dangerousCapabilities = new Set(['command.run','harness.run']);
const approvalLabels = {ask:'Ask every time',read:'Allow routine reads',folder:'Allow reads and new files'};
const welcomeTemplate = $('.chat-welcome').cloneNode(true);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function date(value) {
  if (!value) return '';
  const parsed = new Date(typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : value);
  return Number.isNaN(parsed.getTime()) ? '' : parsed.toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
}

function readable(value) {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

function notify(message) {
  $('#notice').textContent = message;
  $('#notice').hidden = !message;
}

function showLogin() {
  state.sessionEpoch += 1;
  state.refreshing = false;
  $('#login-screen').hidden = false;
  $('#app').hidden = true;
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  if (accountTimer) clearInterval(accountTimer);
  accountTimer = null;
  state.data = null;
  state.signatures = {};
  state.selectedTask = null;
  state.permissionsNode = null;
  state.pending = false;
  $('#send-button').disabled = false;
  $('#chat-pending').hidden = true;
  accountReady = false;
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
  $('#api-key').value = '';
  $('#current-password').value = '';
  $('#new-password').value = '';
  $('#pairing-code').textContent = '';
  $('#pairing-box').hidden = true;
  $('#invitation-link').value = '';
  $('#invitation-created').hidden = true;
  $('#prompt').value = '';
  $('#memory-content').value = '';
  $('#base-url').value = '';
  $('#model-name').value = '';
  $('#account-model').value = '';
  $('#account-flow').hidden = true;
  $('#account-code').textContent = '';
  $('#account-link').removeAttribute('href');
  $('#profile-name').value = '';
  for (const id of ['messages','tasks-list','devices-list','memory-list','memory-candidates','invitations-list','profiles-list','task-detail','permissions-list']) $(`#${id}`).replaceChildren();
  $('#profile-result').textContent = '';
  $('#password-result').textContent = '';
  $('#provider-result').textContent = '';
  notify('');
  showAuthView(state.authView);
}

function showAuthView(view) {
  state.authView = view === 'signup' ? 'signup' : 'login';
  $('#login-form').hidden = state.authView !== 'login';
  $('#signup-form').hidden = state.authView !== 'signup';
}

function routeLocation() {
  if(!location.hash.startsWith('#join=')) {setTab(location.hash.slice(1));return false;}
  $('#signup-error').textContent = '';
  $('#signup-invite').value = '';
  try {$('#signup-invite').value = decodeURIComponent(location.hash.slice(6)).slice(0,128);}
  catch {$('#signup-error').textContent = 'The invitation link could not be read. Enter its code manually.';}
  history.replaceState(null,'',location.pathname + location.search);
  showAuthView('signup');
  showLogin();
  return true;
}

async function api(path, method = 'GET', body) {
  const epoch = state.sessionEpoch;
  const options = {method, credentials:'same-origin', headers:{'Accept':'application/json'}};
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const controller = new AbortController();
  options.signal = controller.signal;
  const timeout = setTimeout(() => controller.abort(), path === '/api/chat' ? 130000 : 20000);
  let response, content;
  try {
    response = await fetch(path, options);
    try { content = await response.json(); }
    catch(error) { if(controller.signal.aborted) throw error; content = {}; }
  } catch {
    if(controller.signal.aborted) throw new Error('This request was not confirmed in time. Refresh to check its status before repeating it; a task may already exist or still be running.');
    throw new Error('Cannot reach HiveMind. This request was not confirmed. Check the connection and refresh before repeating it; a task may already exist or still be running.');
  } finally { clearTimeout(timeout); }
  if(epoch !== state.sessionEpoch) throw new Error('Your signed-in profile changed. Refresh before repeating this request.');
  if (response.status === 401) showLogin();
  if (!response.ok) {
    const detail = content.detail || content.error || (response.status >= 500 ? 'The server could not complete this request. Check its connection and try again.' : `Request failed (${response.status}). Refresh this page and try again.`);
    throw new Error(typeof detail === 'string' ? detail : 'Please check the values and try again.');
  }
  return content;
}

function actionButton(label, style, action) {
  const button = element('button', style, label);
  button.type = 'button';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await action(); } catch (error) { notify(error.message); }
    finally { button.disabled = false; }
  });
  return button;
}

function empty(parent, message) {
  parent.replaceChildren(element('div','empty',message));
}

function setTab(tab) {
  if (!['chat','tasks','devices','memory','settings'].includes(tab)) tab = 'chat';
  state.tab = tab;
  document.querySelectorAll('.tab').forEach(node => node.hidden = node.id !== `tab-${tab}`);
  document.querySelectorAll('[data-tab]').forEach(node => {
    node.classList.toggle('active', node.dataset.tab === tab);
    if (node.dataset.tab === tab) node.setAttribute('aria-current','page'); else node.removeAttribute('aria-current');
  });
  $('#page-title').textContent = tab[0].toUpperCase() + tab.slice(1);
  if (location.hash !== `#${tab}`) history.replaceState(null, '', `#${tab}`);
  if (tab === 'settings' && state.data?.user?.is_admin) refreshInvitations();
}

function renderMessages(messages) {
  messages = messages.filter(message => !['activity','system_test'].includes(message.kind));
  const signature = JSON.stringify(messages);
  if (signature === state.signatures.messages) return;
  state.signatures.messages = signature;
  const target = $('#messages');
  if (!messages.length) { target.replaceChildren(welcomeTemplate.cloneNode(true)); return; }
  const nearBottom = target.scrollHeight - target.scrollTop - target.clientHeight < 130;
  const fragment = document.createDocumentFragment();
  for (const message of messages) {
    const row = element('article', `message ${message.role === 'user' ? 'user' : 'assistant'}`);
    const header = element('div','message-header',message.role === 'user' ? 'You' : 'HiveMind');
    header.append(element('time','',date(message.created_at)));
    row.append(header, element('div','message-content',message.content || ''));
    if (message.task_id) row.append(actionButton('View task','message-task',() => { setTab('tasks'); return showTask(message.task_id); }));
    fragment.append(row);
  }
  target.replaceChildren(fragment);
  if (nearBottom || state.pending) target.scrollTop = target.scrollHeight;
}

function taskChildren(task) {
  return (state.data?.tasks || []).filter(child => child.parent_id === task.id).sort((a,b) => (a.step_no || 0) - (b.step_no || 0));
}

function taskProgress(task) {
  const value = task.progress || task.last_progress;
  if (typeof value === 'string') return {message:value};
  return value ? {message:value.message || value.detail || '',created_at:value.created_at} : {message:''};
}

function taskPresentation(task) {
  const children = taskChildren(task);
  const child = children.find(item => !terminal.has(item.status)) || children.at(-1);
  const current = task.is_workflow && !terminal.has(task.status) && task.status !== 'cancel_requested' && child && task.status === 'waiting_children' ? child : task;
  const progress = taskProgress(current);
  const node = state.data?.nodes.find(item => item.id === current.node_id);
  const device = node?.name || 'the assigned device';
  const step = current.parent_id && current.step_no ? `Step ${current.step_no}: ` : '';
  const view = {status:current.status,label:statuses[current.status] || current.status,description:'',current,progress,attention:false};
  if (task.status === 'waiting_children' && child && child.status === 'completed') {
    view.status = 'planning'; view.label = 'Thinking about the next step'; view.description = 'The last step finished. The cloud AI is deciding whether more work is needed.';
    return view;
  }
  if (current.status === 'planning') {
    view.label = 'Choosing the next action'; view.description = 'The cloud AI is deciding what to do next. No device action is running yet.';
  } else if (current.status === 'awaiting_approval') {
    view.label = 'Your approval is needed'; view.attention = true;
    view.description = `${step}review the proposed action. It cannot run until you approve it.`;
  } else if (['queued','waiting_device'].includes(current.status)) {
    const offline = current.status === 'waiting_device' || (node && !node.online);
    view.status = offline ? 'waiting_device' : current.status;
    view.label = offline ? 'Waiting for device to reconnect' : 'Waiting for device to accept';
    const approval = current.approval === 'grant' ? 'Allowed by your folder settings.' : 'Approval is saved.';
    view.description = offline ? `${step}open the HiveMind app on ${device} and reconnect. ${approval}` : `${step}${approval} Keep the HiveMind app open on ${device}; it will collect this action when it is free.`;
    if(node?.kind === 'browser') view.description += ' Keep the browser connector popup open.';
  } else if (current.status === 'running') {
    const local = progress.message.startsWith('Waiting for confirmation on the computer');
    if(local) {
      view.label = 'Confirm on the computer'; view.attention = true;
      view.description = `${step}open the HiveMind window on ${device} and review its confirmation dialog. The program has not started yet.`;
    } else if (current.capability === 'cloud.answer') {
      view.label = 'Preparing the answer'; view.description = 'The cloud AI is writing the result using the information collected so far.';
    } else {
      view.label = 'Running on the device'; view.description = `${step}${device} accepted the action. Its latest report appears below.`;
      if(dangerousCapabilities.has(current.capability) && !progress.message && node?.local_confirmation !== false) view.description = `${step}${device} accepted the action. Check the HiveMind window there in case local confirmation is needed.`;
    }
  } else if (current.status === 'waiting_children') {
    view.label = 'Checking the current step'; view.description = 'The server is reconciling the current step. Open Activity for its latest report.';
  } else if (current.status === 'cancel_requested') {
    view.description = 'Stop requested. Waiting for the device to acknowledge it. Earlier effects cannot be undone.';
  } else if (current.status === 'outcome_unknown') {
    view.description = 'The result is unconfirmed. Check what happened on the device before trying again.';
  } else if (current.status === 'failed') {
    view.description = 'Review the error below. After resolving it, Try again creates a new task.';
  } else if (current.status === 'cancelled') {
    view.description = 'This task will run no further steps. Changes made before it stopped may remain.';
  }
  return view;
}

function taskStageBlock(task) {
  const view = taskPresentation(task);
  const box = element('div',`task-stage${view.attention ? ' needs-attention' : ''}`);
  box.append(element('strong','',view.label));
  if(view.description) box.append(element('p','',view.description));
  if(view.progress.message && !terminal.has(task.status)) box.append(element('p','stage-progress',`Device report: ${view.progress.message}`));
  const updated = view.progress.created_at || view.current.updated_at;
  if(updated) box.append(element('span','stage-updated',`Last update ${date(updated)}`));
  return box;
}

function taskDetailSignature(task) {
  return JSON.stringify({task,children:taskChildren(task),devices:(state.data?.nodes || []).map(node => ({id:node.id,name:node.name,online:node.online}))});
}

function approvalBlock(task) {
  const node = state.data?.nodes.find(item => item.id === task.node_id);
  const box = element('div','action-review');
  box.append(element('h3','',task.step_no ? `Review step ${task.step_no}` : 'Review this action'));
  box.append(element('p','',task.prompt));
  box.append(element('pre','',readable({device:node?.name || task.node_id,capability:task.capability,arguments:task.arguments || {}})));
  if(dangerousCapabilities.has(task.capability)) box.append(element('p','approval-note',node?.local_confirmation === false ? 'This device accepts approval from HiveMind. Approving here allows this program to run without a second confirmation on the device.' : 'After approving here, confirm this program in the HiveMind window on the device.'));
  box.append(actionButton('Approve this exact action','primary',async () => {await api(`/api/tasks/${encodeURIComponent(task.id)}/approve`,'POST',{});await refresh();}));
  return box;
}

function renderTasks(tasks) {
  const active = tasks.filter(task => !task.parent_id && !terminal.has(task.status));
  $('#task-count').hidden = active.length === 0;
  $('#task-count').textContent = active.length;
  const signature = JSON.stringify({tasks,nodes:state.data.nodes.map(node => ({id:node.id,name:node.name,online:node.online}))});
  if (signature === state.signatures.tasks) return;
  state.signatures.tasks = signature;
  const target = $('#tasks-list');
  if (!tasks.length) return empty(target,'No tasks yet. Ask HiveMind to do something in Chat.');
  target.replaceChildren();
  const ids = new Set(tasks.map(task => task.id));
  const roots = tasks.filter(task => !task.parent_id || !ids.has(task.parent_id));
  const rendered = new Set();
  function renderBranch(task, depth = 0) {
    if (rendered.has(task.id)) return;
    rendered.add(task.id);
    const card = element('article',`item${depth ? ' child-task' : ''}`);
    if (depth) card.style.marginLeft = `${Math.min(depth,3)*18}px`;
    card.dataset.taskId = task.id;
    const top = element('div','item-top');
    const view = taskPresentation(task);
    top.append(element('h3','',task.prompt), element('span',`status ${view.status}`,view.label));
    card.append(top);
    const meta = element('div','item-meta');
    const node = state.data.nodes.find(node => node.id === task.node_id);
    meta.append(taskIdButton(task.id));
    if(task.parent_id) meta.append(element('span','',task.step_no ? `Step ${task.step_no}` : 'Subtask'));
    meta.append(element('span','',task.is_workflow ? 'Multi-step task' : capabilityLabels[task.capability] || task.capability || 'Choosing an action'),element('span','',task.is_workflow ? 'Steps below' : node?.name || (task.node_id ? 'Device unavailable' : 'Cloud')),element('time','',date(task.created_at)));
    if(task.approval === 'grant') meta.append(element('span','grant-label','Allowed by folder access'));
    else if(task.approval === 'explicit') meta.append(element('span','','Approved by you'));
    card.append(meta);
    if(!terminal.has(task.status) || ['failed','outcome_unknown'].includes(task.status)) card.append(taskStageBlock(task));
    if(task.status === 'awaiting_approval') card.append(approvalBlock(task));
    if (task.error) card.append(element('p','error',task.error));
    const actions = element('div','item-actions');
    if(task.is_workflow && view.current.status === 'awaiting_approval') actions.append(actionButton('Review next step','primary',() => showTask(task.id)));
    actions.append(actionButton('Details','secondary',() => showTask(task.id)));
    if (!terminal.has(task.status) && task.status !== 'cancel_requested') actions.append(actionButton(task.is_workflow ? 'Stop workflow' : 'Stop','secondary',async () => { await api(`/api/tasks/${encodeURIComponent(task.id)}/cancel`,'POST',{}); await refresh(); }));
    if (!task.parent_id && ['failed','cancelled','outcome_unknown'].includes(task.status)) actions.append(actionButton('Try again','secondary',() => retryTask(task)));
    card.append(actions);
    target.append(card);
    const children = tasks.filter(child => child.parent_id === task.id).sort((a,b) => (a.step_no || 0)-(b.step_no || 0) || (a.created_at > b.created_at ? 1 : -1));
    for (const child of children) renderBranch(child,depth+1);
  }
  for (const task of roots) renderBranch(task);
  for (const task of tasks) if(!rendered.has(task.id)) renderBranch(task);
}

function taskIdButton(id) {
  const button = actionButton(`#${String(id).slice(0,8)}`,'task-id',async () => {
    try { await navigator.clipboard.writeText(id); notify('Task ID copied.'); }
    catch { notify(`Task ID: ${id}`); }
  });
  button.title = `Copy task ID: ${id}`;
  button.setAttribute('aria-label',`Copy task ID ${id}`);
  return button;
}

async function retryTask(task) {
  if(task.status === 'outcome_unknown' && !await confirmation('Create another task?','The previous action may already have happened. Check the device first. Continuing creates a new task and may repeat that work.','Create new task')) return;
  const response = await api(`/api/tasks/${encodeURIComponent(task.id)}/retry`,'POST',task.status === 'outcome_unknown' ? {confirmed_outcome:true} : {});
  await refresh();
  notify('A new task was created. It will follow your current device permissions.');
  const id = response.id || response.task?.id;
  if(id) await showTask(id);
}

function renderActivity(tasks) {
  const parents = tasks.filter(task => !task.parent_id);
  const visible = [...parents.filter(task => !terminal.has(task.status)),...parents.filter(task => terminal.has(task.status))].slice(0,5);
  $('#chat-activity').hidden = visible.length === 0;
  const active = parents.filter(task => !terminal.has(task.status)).length;
  const attention = parents.filter(task => taskPresentation(task).attention);
  $('#activity-summary').textContent = attention.length ? `Task activity · ${attention.length} need your attention` : active ? `Task activity · ${active} in progress` : 'Recent task activity';
  const attentionKey = attention.map(task => taskPresentation(task).current.id + ':' + taskPresentation(task).label).join('|');
  if(attentionKey && attentionKey !== state.attentionKey) $('#chat-activity').open = true;
  state.attentionKey = attentionKey;
  const signature = JSON.stringify(visible.map(task => ({task,view:taskPresentation(task)})));
  if(signature === state.signatures.activity) return;
  state.signatures.activity = signature;
  const list = $('#activity-list'); list.replaceChildren();
  for(const task of visible) {
    const view = taskPresentation(task);
    const button = actionButton('','activity-row',() => showTask(task.id));
    const text = element('span','activity-text');
    text.append(element('span','activity-prompt',task.prompt));
    if(view.description && !terminal.has(task.status)) text.append(element('span','activity-description',view.description));
    button.append(text,element('span',`status ${view.status}`,view.label));
    list.append(button);
  }
}

async function showTask(id, open = true) {
  const task = state.data?.tasks.find(task => task.id === id);
  if (!task) return;
  state.selectedTask = id;
  state.detailTaskSignature = taskDetailSignature(task);
  const scrollTop = $('#task-dialog').scrollTop;
  const content = $('#task-detail');
  const view = taskPresentation(task);
  content.replaceChildren(element('p','',task.prompt),taskIdButton(task.id),taskStageBlock(task));
  if(task.approval === 'grant') content.append(element('p','approval-note','This action was allowed by the folder access you granted.'));
  else if(task.approval === 'explicit') content.append(element('p','muted','You approved this specific action.'));
  if(task.parent_id) content.append(actionButton(`Parent task #${task.parent_id.slice(0,8)}`,'message-task',() => showTask(task.parent_id)));
  if(view.current.status === 'awaiting_approval') content.append(approvalBlock(view.current));
  else if(!task.is_workflow) content.append(element('p','detail-label','Action'),element('pre','',readable({capability:task.capability,arguments:task.arguments,node_id:task.node_id})));
  if (task.result !== undefined && task.result !== null) content.append(element('p','detail-label','Result'),element('pre','task-result',readable(task.result)));
  if (task.error) content.append(element('p','error',task.error));
  const controls = element('div','item-actions');
  if(!terminal.has(task.status) && task.status !== 'cancel_requested') controls.append(actionButton(task.is_workflow ? 'Stop task' : 'Stop step','secondary',async () => {await api(`/api/tasks/${encodeURIComponent(task.id)}/cancel`,'POST',{});await refresh();}));
  if(!task.parent_id && ['failed','cancelled','outcome_unknown'].includes(task.status)) controls.append(actionButton('Try again','secondary',() => retryTask(task)));
  content.append(controls);
  const children = state.data.tasks.filter(child => child.parent_id === id).sort((a,b) => (a.step_no || 0)-(b.step_no || 0));
  if(children.length) {
    const steps = element('div','task-steps');
    content.append(element('p','detail-label','Steps'));
    for(const child of children) steps.append(actionButton(`Step ${child.step_no || '?'} · ${taskPresentation(child).label} · ${child.prompt}`,'step-link',() => showTask(child.id)));
    content.append(steps);
  }
  const timeline = element('ul','timeline');timeline.id = 'task-timeline';
  content.append(element('p','detail-label','Activity'),timeline);
  const dialog = $('#task-dialog');
  if (open && !dialog.open) dialog.showModal();
  try {
    const response = await api(`/api/tasks/${encodeURIComponent(id)}/events`);
    const events = Array.isArray(response) ? response : response.events || [];
    if(state.selectedTask !== id) return;
    renderTimeline(timeline,events);
    if(!open) dialog.scrollTop = scrollTop;
  } catch (error) { timeline.append(element('li','error',error.message)); }
}

function renderTimeline(timeline, events) {
  const signature = JSON.stringify(events);
  if(timeline.dataset.signature === signature) return;
  timeline.dataset.signature = signature;
  timeline.replaceChildren();
  if(!events.length) timeline.append(element('li','','No activity recorded yet.'));
  for(const event of events) {
    const item = element('li');
    const description = event.detail || event.message || statuses[event.kind] || event.kind || event.type || event.event || event.status || readable(event);
    item.append(element('time','',date(event.created_at || event.timestamp)),element('p','',description));
    timeline.append(item);
  }
}

async function refreshOpenTask() {
  if(!$('#task-dialog').open || !state.selectedTask || state.detailsRefreshing) return;
  state.detailsRefreshing = true;
  const id = state.selectedTask;
  try {
    const task = state.data.tasks.find(task => task.id === id);
    if(task && taskDetailSignature(task) !== state.detailTaskSignature) await showTask(id,false);
    else {
      const result = await api(`/api/tasks/${encodeURIComponent(id)}/events`);
      if(state.selectedTask === id && $('#task-timeline')) renderTimeline($('#task-timeline'),Array.isArray(result) ? result : result.events || []);
    }
  } catch { /* The next state refresh will reconnect. */ }
  finally {state.detailsRefreshing = false;}
}

function confirmation(title, text, confirmLabel = 'Remove') {
  const dialog = $('#confirm-dialog');
  $('#confirm-title').textContent = title;
  $('#confirm-text').textContent = text;
  $('#confirm-button').textContent = confirmLabel;
  dialog.returnValue = 'cancel';
  dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close',() => resolve(dialog.returnValue === 'confirm'),{once:true}));
}

function renderNodes(nodes) {
  const signature = JSON.stringify(nodes);
  if (signature === state.signatures.nodes) return;
  state.signatures.nodes = signature;
  const select = $('#chat-node');
  const selected = select.value;
  select.replaceChildren(new Option('Any connected device',''));
  const usable = nodes.filter(node => !node.revoked && !node.revoked_at);
  for (const node of usable) select.append(new Option(node.name,node.id));
  if (usable.some(node => node.id === selected)) select.value = selected;
  const target = $('#devices-list');
  if (!usable.length) return empty(target,'No devices connected yet. Create a pairing code to connect your first device.');
  target.replaceChildren();
  for (const node of usable) {
    const card = element('article','item');
    const top = element('div','item-top');
    top.append(element('h3','',node.name),element('span',`status ${node.online ? 'completed' : ''}`,node.online ? 'Online' : 'Offline'));
    const meta = element('div','item-meta');
    meta.append(element('span','',node.kind || 'Device'),element('span','',`Last seen ${date(node.last_seen || node.updated_at) || 'not yet'}`));
    card.append(top,meta,element('p','muted',(node.capabilities || []).map(cap => capabilityLabels[cap] || cap).join(' · ') || 'No actions enabled'));
    const scope = node.scope?.name;
    if(scope) card.append(element('p','device-scope',`Shared folder: ${scope}`));
    card.append(element('p','muted',`Approval: ${approvalLabels[node.approval_level] || approvalLabels.ask}`));
    if((node.offered_capabilities || node.capabilities || []).some(cap => dangerousCapabilities.has(cap))) card.append(element('p','muted',node.local_confirmation === false ? 'Commands and device agents can be approved here in HiveMind.' : 'Commands and device agents also ask for confirmation on this device.'));
    const harnesses = node.manifest?.harnesses || [];
    if(harnesses.length) {
      const details = element('details','harness-list');
      details.append(element('summary','',`Device agents (${harnesses.length})`));
      for(const harness of harnesses) {
        const item = element('div','harness-item');
        item.append(element('strong','',harness.name || harness.id),element('code','',harness.id));
        if(harness.description) item.append(element('p','muted',harness.description));
        details.append(item);
      }
      card.append(details);
    }
    const actions = element('div','item-actions');
    actions.append(actionButton('Permissions','secondary',() => showPermissions(node.id)));
    actions.append(actionButton('Disconnect','secondary',async () => {
      if (!await confirmation(`Disconnect ${node.name}?`,'This removes the device from this workspace and revokes its access. Pending work stops. Connect it again with a new pairing code if needed.','Disconnect device')) return;
      await api(`/api/nodes/${encodeURIComponent(node.id)}`,'DELETE'); await refresh();
    }));
    card.append(actions);target.append(card);
  }
}

function permissionSnapshot(node) {
  return JSON.stringify({offered:node.offered_capabilities || node.capabilities || [],manifest:node.manifest || {},scope:node.scope || {},local_confirmation:node.local_confirmation,capabilities:node.capabilities || [],approval_level:node.approval_level || 'ask'});
}

function showPermissions(id) {
  const node = state.data.nodes.find(node => node.id === id);
  if(!node) return;
  state.permissionsNode = {id,snapshot:permissionSnapshot(node)};
  $('#permissions-device').textContent = `Choose what ${node.name} can do and which actions can proceed without asking.`;
  const hasScope = Boolean(node.scope?.id && node.scope?.name);
  $('#permissions-scope').textContent = hasScope ? `These folder permissions apply to ${node.scope.name}, as selected in the device app.` : 'This connector has not shared a folder scope. It will ask for each action. Update the desktop connector to grant folder access.';
  document.querySelectorAll('input[name="approval-level"]').forEach(input => {
    input.checked = input.value === (node.approval_level || 'ask');
    input.disabled = input.value !== 'ask' && !hasScope;
  });
  $('#permissions-confirmation').textContent = node.local_confirmation === false ? 'This device is configured to accept command and device-agent approvals from HiveMind. You can change that choice in the device app.' : 'Commands and device agents also require confirmation on the device. To approve only from HiveMind, enable “Accept command and agent approvals from HiveMind” in that device’s app.';
  $('#permissions-error').textContent = '';
  const list = $('#permissions-list');list.replaceChildren();
  const offered = node.offered_capabilities || node.capabilities || [];
  for(const capability of offered) {
    const label = element('label',`permission-option${dangerousCapabilities.has(capability) ? ' powerful' : ''}`);
    const input = element('input');input.type = 'checkbox';input.value = capability;input.checked = (node.capabilities || []).includes(capability);
    const body = element('span');body.append(element('strong','',capabilityLabels[capability] || capability));
    const descriptions = {'command.run':'Execute commands on this device. Review the command, arguments, and working folder before each run.','harness.run':'Delegate a task to a configured device agent. It may run tools or change files within its configured permissions.','files.write':'Create files inside the folder shared by this device.','files.read':'Read file contents inside the shared folder.','browser.read':'Read tabs explicitly shared in the browser connector.','browser.open':'Open a new browser tab at the approved address.'};
    if(descriptions[capability]) body.append(element('small','',descriptions[capability]));
    label.append(input,body);list.append(label);
  }
  if(!offered.length) list.append(element('p','muted','This connector has not advertised any actions. Update or reconnect it to continue.'));
  const agents = node.manifest?.harnesses || [];
  if(agents.length) {
    list.append(element('p','detail-label','Configured device agents'));
    for(const agent of agents) list.append(element('p','muted',`${agent.name || agent.id}: ${agent.description || agent.id}`));
  }
  $('#permissions-dialog').showModal();
}

function renderMemories(memories) {
  const signature = JSON.stringify(memories);
  if (signature === state.signatures.memories) return;
  state.signatures.memories = signature;
  const target = $('#memory-list');
  if (!memories.length) return empty(target,'No saved memories. Add information you want HiveMind to remember.');
  target.replaceChildren();
  for (const memory of memories) {
    const row = element('article','item memory-item');
    const body = element('div');
    body.append(element('p','',memory.content),element('div','item-meta',date(memory.created_at)));
    row.append(body,actionButton('Delete','secondary',async () => {
      if (!await confirmation('Delete this memory?','Deleting this note also resets recalled conversation context to prevent it from returning. Your visible chat history stays.')) return;
      await api(`/api/memories/${encodeURIComponent(memory.id)}`,'DELETE'); await refresh();
    }));
    target.append(row);
  }
}

function renderMemoryCandidates(candidates) {
  const signature = JSON.stringify(candidates);
  if(signature === state.signatures.candidates) return;
  state.signatures.candidates = signature;
  const target = $('#memory-candidates');target.replaceChildren();
  $('#memory-candidates-section').hidden = !candidates.length;
  for(const candidate of candidates) {
    const card = element('article','item memory-candidate');
    card.append(element('p','',candidate.content));
    const evidence = candidate.evidence || [];
    if(evidence.length) {
      const details = element('details','harness-list');details.append(element('summary','','Why this was suggested'));
      for(const source of evidence) {
        const quote = element('blockquote','candidate-source',source.quote || '');
        if(source.message_id) quote.append(element('div','candidate-source',`Message #${source.message_id.slice(0,8)}`));
        details.append(quote);
      }
      card.append(details);
    }
    const actions = element('div','item-actions');
    actions.append(actionButton('Save memory','primary',async () => {await api(`/api/memory/candidates/${encodeURIComponent(candidate.id)}/accept`,'POST',{});await refresh();await refreshCandidates();}));
    actions.append(actionButton('Dismiss','secondary',async () => {await api(`/api/memory/candidates/${encodeURIComponent(candidate.id)}/dismiss`,'POST',{});await refresh();await refreshCandidates();}));
    card.append(actions);target.append(card);
  }
}

async function refreshCandidates() {
  if(state.candidatesRefreshing || !state.data) return;
  state.candidatesRefreshing = true;
  const epoch = state.sessionEpoch;
  try {const result = await api('/api/memory/candidates');if(epoch === state.sessionEpoch) renderMemoryCandidates(Array.isArray(result) ? result : result.candidates || []);}
  catch(error) {if(epoch === state.sessionEpoch && state.tab === 'memory') notify(error.message);}
  finally {state.candidatesRefreshing = false;}
}

function renderProvider(provider) {
  const name = provider.type || provider.provider;
  const fallback = name === 'compatible' ? 'API connection' : name === 'mock' ? 'Test model' : 'Connected model';
  const label = provider.display_name || (provider.model && !/hermes|existing.*selection/i.test(provider.model) ? provider.model : fallback);
  $('#provider-label').textContent = label;
  $('#provider-label').title = provider.error || label;
  $('#model-onboarding').hidden = name !== 'unconfigured';
  if (!state.signatures.provider) {
    const current = provider.type || provider.provider;
    const selection = ['compatible','hermes-account'].includes(current) ? current : state.data?.user?.is_admin === false ? 'compatible' : 'hermes';
    $(`input[name="provider"][value="${selection}"]`).checked = true;
    updateProviderFields();
    if (provider.base_url) $('#base-url').value = provider.base_url;
    if (provider.model) $('#model-name').value = provider.model;
    if (selection === 'hermes-account' && provider.model) $('#account-model').value = provider.model;
    state.signatures.provider = 'initialized';
  }
}

function renderProfile(user = {}) {
  const signature = JSON.stringify(user);
  if(signature === state.signatures.profile) return;
  state.signatures.profile = signature;
  const admin = user.is_admin === true;
  $('#profile-shortcut').textContent = user.name || user.username || 'Your profile';
  $('#profile-username').textContent = `${user.username || 'owner'}${admin ? ' · Server owner' : ''}`;
  $('#profile-name').value = user.name || user.username || '';
  $('#invitations-section').hidden = !admin;
  $('#host-provider-option').hidden = user.is_admin === false;
  $('#host-provider-option input').disabled = user.is_admin === false;
  const endpoints = Array.isArray(user.provider_endpoints) ? user.provider_endpoints : [];
  $('#provider-endpoints').replaceChildren(...endpoints.map(url => {const option = element('option');option.value = url;return option;}));
  $('#endpoint-help').textContent = user.is_admin === false ? endpoints.length ? `This server allows: ${endpoints.join(', ')}. Ask the host to add another provider if needed.` : 'The server host has not enabled API endpoints. Ask them to add your provider.' : 'Use an HTTPS endpoint, or a local model on this server through localhost.';
  if(!$('#base-url').value && endpoints.length) $('#base-url').value = endpoints[0];
  if(admin && state.tab === 'settings') refreshInvitations();
}

async function refreshInvitations() {
  if(!state.data?.user?.is_admin || state.invitationsRefreshing) return;
  state.invitationsRefreshing = true;
  const profileId = state.data.user.id;
  try {
    const [invitations,profiles] = await Promise.all([api('/api/invitations'),api('/api/profiles')]);
    if(state.data?.user?.id !== profileId) return;
    const profilesList = $('#profiles-list');profilesList.replaceChildren();
    for(const profile of profiles) {
      const row = element('article','item invitation-item');
      const body = element('div');
      body.append(element('strong','',profile.name),element('p','muted',`${profile.username} · ${profile.is_admin ? 'Server owner' : profile.disabled ? 'Disabled' : 'Active'}`));
      row.append(body);
      if(!profile.is_admin && !profile.disabled) row.append(actionButton('Disable','secondary',async () => {
        if(!await confirmation(`Disable ${profile.name}?`,'This signs out the profile, disconnects its devices, and stops its work. Stored history is retained. This interface cannot re-enable a disabled profile.','Disable profile')) return;
        await api(`/api/profiles/${encodeURIComponent(profile.id)}/disable`,'POST',{});
        await refreshInvitations();
      }));
      profilesList.append(row);
    }
    const target = $('#invitations-list');
    target.replaceChildren();
    $('#invitations-result').textContent = '';
    if(!invitations.length) return empty(target,'No invitations yet.');
    for(const invitation of invitations) {
      const expired = invitation.expires_at * 1000 <= Date.now();
      const active = !invitation.used && !invitation.revoked && !expired;
      const row = element('article','item invitation-item');
      const body = element('div');
      body.append(element('strong','',invitation.used ? 'Used' : invitation.revoked ? 'Revoked' : expired ? 'Expired' : 'Waiting to be used'),element('p','muted',`Created ${date(invitation.created_at)} · Expires ${date(invitation.expires_at)}`));
      row.append(body);
      if(active) row.append(actionButton('Revoke','secondary',async () => {
        await api(`/api/invitations/${encodeURIComponent(invitation.id)}`,'DELETE');
        if(state.lastInvitationId === invitation.id) {$('#invitation-link').value = '';$('#invitation-created').hidden = true;}
        await refreshInvitations();
      }));
      target.append(row);
    }
    $('#invitations-result').textContent = '';
  } catch(error) {$('#invitations-result').textContent = error.message;}
  finally {state.invitationsRefreshing = false;}
}

async function refresh() {
  if (state.refreshing || document.hidden || (state.authView === 'signup' && !$('#login-screen').hidden)) return;
  state.refreshing = true;
  const epoch = state.sessionEpoch;
  try {
    const data = await api('/api/state');
    if(epoch !== state.sessionEpoch) return;
    if(state.data?.user?.id && state.data.user.id !== data.user?.id) showLogin();
    state.data = {messages:[],tasks:[],nodes:[],memories:[],memory_candidates:[],provider:{},...data};
    $('#login-screen').hidden = true; $('#app').hidden = false;
    $('#connection-dot').className = 'dot online'; $('#connection-label').textContent = 'Server connected';
    if(state.connectionLost) {notify('');state.connectionLost = false;}
    renderProfile(state.data.user);renderMessages(state.data.messages);renderTasks(state.data.tasks);renderActivity(state.data.tasks);renderNodes(state.data.nodes);renderMemories(state.data.memories);renderProvider(state.data.provider);
    const summaryCount = data.memory_stats?.summaries;
    $('#memory-summary').textContent = 'Recent messages stay in context. Older conversations are summarized and searched when relevant.' + (Number.isInteger(summaryCount) ? ` ${summaryCount} saved conversation ${summaryCount === 1 ? 'summary' : 'summaries'}.` : '');
    if(Object.hasOwn(data,'memory_candidates')) renderMemoryCandidates(data.memory_candidates || []);
    else if(state.tab === 'memory') refreshCandidates();
    refreshOpenTask();
    if (!state.timer) state.timer = setInterval(refresh,2000);
  } catch (error) {
    if(epoch !== state.sessionEpoch) return;
    $('#connection-dot').className = 'dot offline'; $('#connection-label').textContent = 'Server connection lost';
    state.connectionLost = true;
    if (!$('#app').hidden) notify('Cannot reach the server. Your existing tasks may still be running. Reconnecting…');
    else if ($('#login-screen').hidden) { showLogin(); $('#login-error').textContent = error.message; }
  } finally { if(epoch === state.sessionEpoch) state.refreshing = false; }
}

$('#login-form').addEventListener('submit',async event => {
  event.preventDefault();const button = event.currentTarget.querySelector('button');button.disabled = true;$('#login-error').textContent = '';
  try { await api('/api/login','POST',{username:$('#username').value.trim() || 'owner',password:$('#password').value});$('#password').value = '';showLogin();await refresh(); }
  catch(error) { $('#login-error').textContent = error.message; }
  finally { button.disabled = false; }
});

$('#show-signup').addEventListener('click',() => {showAuthView('signup');$('#signup-invite').focus();});
$('#show-login').addEventListener('click',() => {showAuthView('login');$('#username').focus();});
$('#signup-form').addEventListener('submit',async event => {
  event.preventDefault();const button = event.currentTarget.querySelector('button');button.disabled = true;$('#signup-error').textContent = '';
  const body = {invite:$('#signup-invite').value.trim(),username:$('#signup-username').value.trim(),name:$('#signup-name').value.trim(),password:$('#signup-password').value};
  try {await api('/api/register','POST',body);$('#signup-form').reset();state.authView = 'login';showLogin();await refresh();setTab('settings');}
  catch(error) {$('#signup-error').textContent = error.message;}
  finally {button.disabled = false;}
});

$('#profile-shortcut').addEventListener('click',() => setTab('settings'));
$('#profile-form').addEventListener('submit',async event => {
  event.preventDefault();const button = event.currentTarget.querySelector('button');button.disabled = true;$('#profile-result').textContent = '';
  try {await api('/api/profile','PATCH',{name:$('#profile-name').value.trim()});$('#profile-result').textContent = 'Name saved.';await refresh();}
  catch(error) {$('#profile-result').textContent = error.message;}
  finally {button.disabled = false;}
});
$('#password-form').addEventListener('submit',async event => {
  event.preventDefault();const button = event.currentTarget.querySelector('button');button.disabled = true;$('#password-result').textContent = '';
  try {await api('/api/profile','PATCH',{current_password:$('#current-password').value,password:$('#new-password').value});event.target.reset();$('#password-result').textContent = 'Password changed. Your other browser sessions have been signed out.';}
  catch(error) {$('#password-result').textContent = error.message;}
  finally {button.disabled = false;}
});
$('#create-invite').addEventListener('click',async () => {
  const button = $('#create-invite');button.disabled = true;$('#invitations-result').textContent = '';
  try {
    const invitation = await api('/api/invitations','POST',{});
    state.lastInvitationId = invitation.id;
    $('#invitation-link').value = `${location.origin}/#join=${encodeURIComponent(invitation.code)}`;
    $('#invitation-expiry').textContent = `Expires ${date(invitation.expires_at)}. One profile can use this link.`;
    $('#invitation-created').hidden = false;
    await refreshInvitations();
  } catch(error) {$('#invitations-result').textContent = error.message;}
  finally {button.disabled = false;}
});
$('#copy-invitation').addEventListener('click',async () => {
  try {await navigator.clipboard.writeText($('#invitation-link').value);$('#copy-invitation').textContent = 'Copied';setTimeout(() => $('#copy-invitation').textContent = 'Copy link',1800);}
  catch {$('#invitation-link').select();$('#invitations-result').textContent = 'Copy the selected link manually.';}
});

document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click',() => setTab(button.dataset.tab)));
window.addEventListener('hashchange',routeLocation);
document.addEventListener('visibilitychange',() => { if (!document.hidden) refresh(); });
$('#messages').addEventListener('click',event => {const button = event.target.closest('[data-prompt]');if(button){$('#prompt').value = button.dataset.prompt;$('#prompt').focus();}});
$('#new-task-button').addEventListener('click',() => {setTab('chat');$('#prompt').focus();});
$('#prompt').addEventListener('keydown',event => {if(event.key === 'Enter' && !event.shiftKey && !event.isComposing){event.preventDefault();if(!state.pending) $('#chat-form').requestSubmit();}});

$('#chat-form').addEventListener('submit',async event => {
  event.preventDefault();if(state.pending) return;
  if((state.data?.provider?.provider || state.data?.provider?.type) === 'unconfigured') {setTab('settings');notify('Connect your own model account before sending a message. Your draft is saved in Chat.');return;}
  const message = $('#prompt').value.trim();if(!message) return;
  const epoch = state.sessionEpoch;
  const request = {message,mode:'auto'};if($('#chat-node').value) request.node_id = $('#chat-node').value;
  state.pending = true;$('#send-button').disabled = true;$('#chat-pending').hidden = false;notify('');$('#prompt').value = '';
  try {
    const response = await api('/api/chat','POST',request);
    if(response.task) $('#chat-activity').open = true;
    await refresh();
  } catch(error) {if(epoch === state.sessionEpoch) {notify(error.message);$('#prompt').value = message;}}
  finally {if(epoch === state.sessionEpoch) {state.pending = false;$('#send-button').disabled = false;$('#chat-pending').hidden = true;$('#prompt').focus();}}
});

$('#pair-button').addEventListener('click',async () => {
  const button = $('#pair-button');button.disabled = true;
  try {const pairing = await api('/api/pairing','POST',{});$('#pairing-code').textContent = pairing.code;$('#pairing-expiry').textContent = `Expires ${date(pairing.expires_at)}. This code works once.`;$('#server-address').textContent = location.origin;$('#pairing-box').hidden = false;}
  catch(error){notify(error.message);}finally{button.disabled = false;}
});
$('#copy-code').addEventListener('click',async () => {
  try{await navigator.clipboard.writeText($('#pairing-code').textContent);$('#copy-code').textContent = 'Copied';setTimeout(()=>$('#copy-code').textContent = 'Copy code',1800);}
  catch{notify('Select the pairing code and copy it manually.');}
});

$('#memory-form').addEventListener('submit',async event => {
  event.preventDefault();const content = $('#memory-content').value.trim();if(!content)return;
  const button = event.currentTarget.querySelector('button');button.disabled = true;
  try{await api('/api/memories','POST',{content});$('#memory-content').value = '';await refresh();}catch(error){notify(error.message);}finally{button.disabled = false;}
});

function updateProviderFields() {
  const type = $('input[name="provider"]:checked').value;
  $('#api-fields').hidden = type !== 'compatible';
  $('#account-fields').hidden = type !== 'hermes-account';
  $('#save-provider').textContent = type === 'hermes-account' ? 'Use this account' : 'Save connection';
  $('#save-provider').disabled = type === 'hermes-account' && !accountReady;
  if(type === 'hermes-account') pollAccount();
}

function renderAccount(flow) {
  accountReady = flow.status === 'connected' || !!flow.profile_available;
  const pending = ['starting','pending'].includes(flow.status);
  $('#account-flow').hidden = flow.status === 'idle' && !accountReady;
  $('#connect-account').disabled = pending;
  $('#cancel-account').hidden = !pending;
  $('#account-verification').hidden = true;
  const statusText = {idle:'No account connected yet.',starting:'Starting provider sign-in…',pending:'Waiting for you to finish signing in.',connected:'Account connected. Choose “Use this account” to activate it.',expired:'The sign-in code expired. Start a new connection.',cancelled:'Sign-in cancelled.',failed:'Sign-in failed. Please try again.',error:'Sign-in failed. Please try again.'};
  $('#account-status').textContent = flow.error || statusText[flow.status] || 'Checking account connection…';
  if(pending && flow.verification_url && flow.user_code) {
    try {
      const target = new URL(flow.verification_url);
      if(target.protocol === 'https:' && !target.username && !target.password) {
        $('#account-link').href = target.href;
        $('#account-code').textContent = flow.user_code;
        $('#account-verification').hidden = false;
      }
    } catch {}
  }
  if($('input[name="provider"]:checked').value === 'hermes-account') $('#save-provider').disabled = !accountReady;
  if(pending && !accountTimer) accountTimer = setInterval(pollAccount,2500);
  if(!pending && accountTimer) {clearInterval(accountTimer);accountTimer = null;}
}

async function pollAccount() {
  const epoch = state.sessionEpoch;
  try {const flow = await api('/api/providers/login/status');if(epoch === state.sessionEpoch) renderAccount(flow);}
  catch(error) {if(epoch !== state.sessionEpoch) return;if(accountTimer){clearInterval(accountTimer);accountTimer = null;}$('#account-flow').hidden = false;$('#account-status').textContent = error.message;}
}

document.querySelectorAll('input[name="provider"]').forEach(input => input.addEventListener('change',updateProviderFields));
$('#connect-account').addEventListener('click',async () => {
  $('#connect-account').disabled = true;
  const body = {};if($('#account-model').value.trim()) body.model = $('#account-model').value.trim();
  try {renderAccount(await api('/api/providers/login/start','POST',body));}
  catch(error) {$('#account-flow').hidden = false;$('#account-status').textContent = error.message;$('#connect-account').disabled = false;}
});
$('#cancel-account').addEventListener('click',async () => {try{renderAccount(await api('/api/providers/login/cancel','POST',{}));}catch(error){notify(error.message);}});
$('#provider-form').addEventListener('submit',async event => {
  event.preventDefault();const button = $('#save-provider');const type = $('input[name="provider"]:checked').value;
  const body = {type};
  if(type === 'hermes-account') {if(!accountReady)return;body.model = $('#account-model').value.trim();}
  if(type === 'compatible') {
    body.base_url = $('#base-url').value.trim();body.model = $('#model-name').value.trim();body.api_key = $('#api-key').value.trim();
    if(!body.base_url || !body.model) {$('#provider-result').textContent = 'Enter the base URL and model name.';return;}
  }
  button.disabled = true;$('#provider-result').textContent = '';
  try{await api('/api/providers','POST',body);$('#api-key').value = '';$('#provider-result').textContent = 'Connection saved. Send a message to try it.';await refresh();}
  catch(error){$('#provider-result').textContent = error.message;}finally{button.disabled = false;}
});
$('#logout-button').addEventListener('click',async () => {try{await api('/api/logout','POST',{});location.reload();}catch(error){notify(error.message);}});
$('#close-task-dialog').addEventListener('click',() => $('#task-dialog').close());
$('#close-permissions-dialog').addEventListener('click',() => $('#permissions-dialog').close());
$('#permissions-form').addEventListener('submit',async event => {
  event.preventDefault();
  const record = state.permissionsNode;
  const node = state.data.nodes.find(node => node.id === record?.id);
  if(!node || permissionSnapshot(node) !== record.snapshot) {$('#permissions-error').textContent = 'The device configuration changed. Close this window and review its permissions again.';return;}
  const capabilities = [...$('#permissions-list').querySelectorAll('input:checked')].map(input => input.value);
  const enabled = capabilities.filter(cap => dangerousCapabilities.has(cap) && !(node.capabilities || []).includes(cap));
  if(enabled.length && !await confirmation('Enable device execution?',`This enables ${enabled.map(cap => capabilityLabels[cap]).join(' and ')} on ${node.name}. Programs run with your device account and may access files outside the shared folder. Each task needs your approval in HiveMind.${node.local_confirmation === false ? ' This device accepts that approval without a second local confirmation.' : ' You will also confirm it on that device.'}`,'Enable selected actions')) return;
  const approval_level = $('input[name="approval-level"]:checked')?.value || 'ask';
  const button = event.currentTarget.querySelector('button');button.disabled = true;
  try{await api(`/api/nodes/${encodeURIComponent(node.id)}/permissions`,'POST',{capabilities,approval_level,scope_id:node.scope?.id || ''});$('#permissions-dialog').close();await refresh();}
  catch(error){$('#permissions-error').textContent = error.message;}finally{button.disabled = false;}
});
$('#task-dialog').addEventListener('click',event => {if(event.target === $('#task-dialog')){const box=event.target.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)event.target.close();}});

if(!routeLocation()) refresh();
if('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(()=>{});
