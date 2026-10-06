const $ = id => document.getElementById(id);
const clientId = typeof crypto.randomUUID === 'function'
  ? crypto.randomUUID()
  : Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
const labels = {queued: '排队中', running: '运行中', complete: '已完成', failed: '失败', cancelled: '已取消', partial: '部分完成'};
const active = job => ['queued', 'running'].includes(job.status);
let settings, selected, selectedBatch, currentBatch, poller, uploadURL, previewKey;
let tasks = [], batches = [], mode = 'single';
let heartbeatTimer, reconnectTimer, liveConnection, leaving = false;
let submitting = false, refreshing = false, batchRenderKey = '';
let pendingSubmission;
function normalizeBase(value) {
  const prefix = String(value || '').replace(/^\/+|\/+$/g, '');
  return prefix ? `/${prefix}/` : '/';
}
let appBase = normalizeBase(window.MOTION_BASE_PATH || new URL('.', document.baseURI).pathname);
function appPath(path) {
  if (appBase !== '/' && path.startsWith(appBase)) return path;
  return appBase + path.replace(/^\/+/, '');
}

function submissionAttempt(fingerprint) {
  const storageKey = `motion-pending-submission:${appBase}`;
  if (pendingSubmission === undefined) {
    try { pendingSubmission = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); }
    catch { pendingSubmission = null; }
  }
  if (pendingSubmission?.fingerprint !== fingerprint || !/^[a-f0-9]{32}$/.test(pendingSubmission?.key || '')) {
    pendingSubmission = {
      fingerprint,
      key: Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('')
    };
  }
  // Preserve uncertain submissions through a page refresh as well as a network retry.
  try { sessionStorage.setItem(storageKey, JSON.stringify(pendingSubmission)); } catch {}
  return pendingSubmission;
}

function confirmSubmission(attempt) {
  if (pendingSubmission?.key !== attempt.key) return;
  pendingSubmission = null;
  try { sessionStorage.removeItem(`motion-pending-submission:${appBase}`); } catch {}
}

async function api(path, options) {
  if (window.MOTION_SHOWCASE_CONFIG) {
    if (path === '/api/status') return window.MOTION_SHOWCASE_CONFIG;
    if (['/api/jobs', '/api/batches'].includes(path) && !options) return [];
    if (path.startsWith('/api/client/')) return {ok: true};
    throw Error('这是界面展示，请按项目说明启动推理服务。');
  }
  const response = await fetch(appPath(path), options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({detail: response.statusText}));
    throw Error(typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail));
  }
  return response.json();
}

function followsBrowser() {
  return !window.MOTION_SHOWCASE_CONFIG && settings?.runtime?.cancel_on_close !== false;
}

async function startClient() {
  if (!followsBrowser()) return;
  leaving = false;
  clearTimeout(reconnectTimer);
  await api(`/api/client/${clientId}/open`, {method: 'POST'});
  if (leaving) return;
  clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(() => api(`/api/client/${clientId}/heartbeat`, {method: 'POST'}).catch(() => {}), 10000);
  if (liveConnection && [WebSocket.CONNECTING, WebSocket.OPEN].includes(liveConnection.readyState)) return;
  const connection = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${appPath(`/api/client/${clientId}/watch`)}`);
  liveConnection = connection;
  connection.onclose = () => {
    if (liveConnection !== connection) return;
    liveConnection = null;
    if (!leaving) reconnectTimer = setTimeout(() => startClient().catch(() => {}), 1000);
  };
}

function closeClient() {
  leaving = true;
  clearInterval(heartbeatTimer);
  clearTimeout(reconnectTimer);
  if (liveConnection) liveConnection.close();
  if (followsBrowser()) navigator.sendBeacon(appPath(`/api/client/${clientId}/close`), new Blob([]));
}
window.addEventListener('pagehide', closeClient);
window.addEventListener('beforeunload', closeClient);
window.addEventListener('pageshow', event => {
  if (event.persisted) startClient().catch(() => {});
});

function duration(seconds) {
  seconds = Math.floor(Math.max(0, seconds));
  return `${Math.floor(seconds / 60)}分 ${seconds % 60}秒`;
}
function setError(message) { $('form-message').textContent = message; }
function setBatchError(message) { $('batch-message').textContent = message; }
function promptLines() { return $('batch-prompts').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean); }
function batchTitle(batch) { return batch.name || `批次 ${batch.id.slice(0, 8)}`; }
function makeText(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function updateInputSummary() {
  const prompts = promptLines();
  const repeat = Number($('batch-repeat').value);
  const validRepeat = Number.isInteger(repeat) && repeat >= 1 && repeat <= 10;
  const total = validRepeat ? prompts.length * repeat : 0;
  $('batch-input-summary').textContent = validRepeat
    ? `${prompts.length} 条描述 × ${repeat} 份 = ${total} 项，每批最多 100 项。`
    : '每条份数须为 1–10 的整数，每批最多 100 项。';
  $('batch-input-summary').classList.toggle('input-warning', !validRepeat || total > 100);
  const seedMode = $('batch-seed-mode').value;
  $('batch-seed-help').textContent = {
    increment: '从下方种子开始，按任务顺序逐项加 1。',
    fixed: '所有任务使用下方种子；相同描述和参数的重复项可能得到相同结果。',
    random: '服务端为每一项选择随机种子，提交后可在任务表中查看。'
  }[seedMode];
  $('generate-label').textContent = settings?.showcase ? '在线推理未启用'
    : submitting ? '正在提交…'
    : mode === 'batch' ? `提交批量任务${total ? `（${total} 项）` : ''}` : '生成动作';
  $('generate').disabled = submitting || !settings?.backend?.ready || Boolean(settings?.showcase);
}

function setMode(next) {
  mode = next;
  const batch = mode === 'batch';
  $('single-input').hidden = batch;
  $('batch-input').hidden = !batch;
  for (const name of ['single', 'batch']) {
    $(`mode-${name}`).classList.toggle('active', mode === name);
    $(`mode-${name}`).setAttribute('aria-pressed', String(mode === name));
  }
  // Hidden controls must not block native validation of the single-task form.
  $('batch-repeat').disabled = !batch;
  $('batch-seed-mode').disabled = !batch;
  $('batch-panel').hidden = settings?.kind === 'gemx' || (!batch && !batches.length && !selectedBatch);
  updateInputSummary();
  setError('');
}

function showUpload(file) {
  if (uploadURL) URL.revokeObjectURL(uploadURL);
  $('video-name').textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB` : '请选择视频';
  if (file) { uploadURL = URL.createObjectURL(file); $('input-video').src = uploadURL; }
  $('input-video').hidden = !file;
}

function history() {
  const container = $('history');
  container.replaceChildren();
  const entries = [
    ...tasks.filter(job => !job.batch_id).map(job => ({...job, isBatch: false})),
    ...batches.map(batch => ({...batch, isBatch: true}))
  ].sort((a, b) => b.created_at - a.created_at);
  if (!entries.length) { container.append(makeText('p', '提交的动作会出现在这里。', 'muted')); return; }
  for (const entry of entries) {
    const isSelected = entry.isBatch ? selectedBatch === entry.id : selected === entry.id;
    const button = makeText('button', '', `history-item${isSelected ? ' selected' : ''}`);
    button.type = 'button';
    const title = entry.isBatch ? `${batchTitle(entry)} · ${entry.total} 项` : entry.config.prompt || `视频动作 · ${entry.id.slice(0, 6)}`;
    const time = new Date(entry.created_at * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'});
    button.append(makeText('strong', title), makeText('small', `${labels[entry.status] || entry.status} · ${time}`));
    button.onclick = () => (entry.isBatch ? selectBatch(entry.id) : select(entry.id)).catch(error => setError(error.message));
    container.append(button);
  }
}

function resetPreview() {
  previewKey = null;
  $('empty-preview').hidden = false;
  $('empty-preview').querySelector('h3').textContent = '从一个动作开始';
  $('empty-preview').querySelector('p').textContent = '生成完成后，在这里查看真实结果。';
  $('original-preview').hidden = true;
  $('original-preview').src = 'about:blank';
  $('output-video').hidden = true;
  $('output-video').pause();
  $('preview-note').textContent = '预览不改变模型动作';
}
function reset() {
  selected = null;
  resetPreview();
  $('task-state').textContent = '等待生成';
  $('job-progress').hidden = true;
  $('download-all').hidden = true;
  $('files').replaceChildren(makeText('p', '每次生成保存在独立目录，不覆盖之前的动作。', 'muted'));
  $('logs').textContent = '任务开始后显示模型的实时日志。';
  setError('');
  history();
  if (currentBatch) displayBatch(currentBatch);
}

async function select(id) {
  selected = id;
  resetPreview();
  const job = await api(`/api/jobs/${id}`);
  if (selected !== id) return;
  display(job);
  history();
  if (currentBatch) displayBatch(currentBatch);
}

function display(job) {
  if (job.id !== selected) return;
  const live = active(job);
  $('task-state').textContent = labels[job.status] || job.status;
  $('job-progress').hidden = false;
  $('job-label').textContent = `任务 ${job.id.slice(0, 8)} · ${labels[job.status] || job.status}`;
  $('elapsed').textContent = duration((job.finished_at || Date.now() / 1000) - (job.started_at || job.created_at));
  $('job-message').textContent = job.message;
  $('cancel').hidden = !live || Boolean(job.cancel_requested);
  $('progress-bar').parentElement.classList.toggle('done', !live);
  $('logs').textContent = (job.log || []).join('\n') || '等待进程输出…';
  if ($('logs-details').open) $('logs').scrollTop = $('logs').scrollHeight;
  $('download-all').hidden = job.status !== 'complete';
  $('download-all').href = appPath(`/api/jobs/${job.id}/download`);
  const files = $('files');
  files.replaceChildren();
  const artifacts = job.artifacts || [];
  if (!artifacts.length) files.append(makeText('p', live ? '任务执行中，完成后显示输出文件。' : '没有可下载的结果，请检查日志。', 'muted'));
  for (const item of artifacts) {
    const row = makeText('div', '', 'file-row');
    const size = item.size > 1048576 ? `${(item.size / 1048576).toFixed(1)} MB` : `${Math.ceil(item.size / 1024)} KB`;
    const link = makeText('a', '下载 ↓');
    link.href = appPath(item.url);
    link.download = item.name.split('/').pop();
    row.append(makeText('span', item.name.split('.').pop().toUpperCase(), 'file-icon'), makeText('span', item.name, 'file-name'), makeText('span', size, 'file-size'), link);
    files.append(row);
  }
  if (!live && previewKey !== job.id) loadPreview(job);
}

function loadPreview(job) {
  resetPreview();
  previewKey = job.id;
  const artifacts = job.artifacts || [];
  const html = artifacts.find(item => item.name.toLowerCase().endsWith('motion.html'))
    || artifacts.find(item => item.name.toLowerCase().endsWith('preview.html'))
    || artifacts.find(item => item.name.toLowerCase().endsWith('.html'));
  const clip = artifacts.find(item => item.name.endsWith('skeleton_3d.mp4')) || artifacts.find(item => item.name.endsWith('.mp4'));
  if (selected !== job.id) return;
  if (html) {
    $('empty-preview').hidden = true;
    $('original-preview').hidden = false;
    $('original-preview').src = appPath(html.url) + '?preview=true';
    $('preview-note').textContent = '原版 HTML 预览 · 资源加载可能需要联网';
  } else if (clip) {
    $('empty-preview').hidden = true;
    $('output-video').hidden = false;
    $('output-video').src = appPath(clip.url);
    $('preview-note').textContent = '原版预览视频 · 未转码';
  } else {
    $('empty-preview').querySelector('h3').textContent = '当前任务没有预览文件';
    $('empty-preview').querySelector('p').textContent = '可以下载原始数据，或查看运行日志。';
  }
}

function updateBatchPicker() {
  const picker = $('batch-picker');
  const expected = batches.map(batch => `${batch.id}:${batchTitle(batch)}:${batch.total}`).join('|');
  if (picker.dataset.items !== expected) {
    picker.replaceChildren();
    if (!batches.length) picker.append(new Option('尚无批量任务', ''));
    for (const batch of batches) picker.append(new Option(`${batchTitle(batch)} · ${batch.total} 项`, batch.id));
    picker.dataset.items = expected;
  }
  picker.value = selectedBatch || '';
  picker.disabled = !batches.length;
  $('batch-panel').hidden = settings?.kind === 'gemx' || (mode !== 'batch' && !batches.length && !selectedBatch);
}

async function selectBatch(id) {
  if (!id) return;
  selectedBatch = id;
  setBatchError('');
  $('batch-panel').hidden = false;
  $('batch-state').textContent = '加载中…';
  updateBatchPicker();
  const batch = await api(`/api/batches/${id}`);
  if (selectedBatch !== id) return;
  displayBatch(batch);
  if (batch.jobs?.length) await select(batch.jobs[0].id);
  history();
}

function displayBatch(batch) {
  if (batch.id !== selectedBatch) return;
  currentBatch = batch;
  const counts = {queued: 0, running: 0, complete: 0, failed: 0, cancelled: 0, ...batch.counts};
  const settled = counts.complete + counts.failed + counts.cancelled;
  $('batch-state').textContent = labels[batch.status] || batch.status;
  $('batch-progress-area').hidden = false;
  $('batch-progress-label').textContent = `${batchTitle(batch)} · 已结束 ${settled} / ${batch.total} 项`;
  $('batch-counts').textContent = `排队 ${counts.queued} · 运行 ${counts.running} · 成功 ${counts.complete} · 失败 ${counts.failed} · 取消 ${counts.cancelled}`;
  $('batch-progress').max = Math.max(1, batch.total);
  $('batch-progress').value = settled;
  $('cancel-batch').hidden = !(batch.jobs || []).some(job => active(job) && !job.cancel_requested);
  $('download-batch').hidden = counts.complete === 0;
  $('download-batch').href = appPath(`/api/batches/${batch.id}/download`);
  const jobs = batch.jobs || [];
  $('batch-empty').hidden = jobs.length > 0;
  $('batch-table-wrap').hidden = !jobs.length;
  const renderKey = JSON.stringify([batch.id, selected, jobs.map(job => [job.id, job.status, job.cancel_requested, job.config, job.batch_index])]);
  if (renderKey === batchRenderKey) return;
  batchRenderKey = renderKey;
  const rows = $('batch-rows');
  rows.replaceChildren();
  jobs.forEach((job, index) => {
    const row = makeText('tr', '', selected === job.id ? 'selected' : '');
    const prompt = makeText('td', job.config.prompt, 'batch-prompt-cell');
    const seed = makeText('td', String(job.config.seed), 'batch-seed-cell');
    const state = makeText('td', '');
    state.append(makeText('span', labels[job.status] || job.status, `task-status status-${job.status}`));
    if (job.cancel_requested && active(job)) state.append(makeText('small', '已请求取消；当前项仍会完成并保存结果。', 'cancel-note'));
    const actions = makeText('td', '', 'batch-actions');
    const view = makeText('button', selected === job.id ? '正在查看' : '查看', 'secondary');
    view.type = 'button';
    view.setAttribute('aria-label', `查看第 ${index + 1} 项预览和日志`);
    view.onclick = () => select(job.id).catch(error => setBatchError(error.message));
    actions.append(view);
    if (active(job) && !job.cancel_requested) {
      const cancel = makeText('button', '取消', 'secondary danger');
      cancel.type = 'button';
      cancel.setAttribute('aria-label', `取消第 ${index + 1} 项`);
      cancel.onclick = async () => {
        cancel.disabled = true;
        try { await cancelJob(job.id); }
        catch (error) { setBatchError(error.message); }
        finally { cancel.disabled = false; }
      };
      actions.append(cancel);
    }
    if (job.status === 'complete') {
      const download = makeText('a', '下载', 'secondary');
      download.href = appPath(`/api/jobs/${job.id}/download`);
      download.setAttribute('aria-label', `下载第 ${index + 1} 项结果`);
      actions.append(download);
    }
    row.append(makeText('td', String(index + 1)), prompt, seed, state, actions);
    rows.append(row);
  });
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const [nextTasks, nextBatches] = await Promise.all([
      api('/api/jobs'), settings?.kind === 'hymotion' ? api('/api/batches') : Promise.resolve([])
    ]);
    tasks = nextTasks;
    batches = nextBatches;
    if (!selectedBatch && batches.length) selectedBatch = batches[0].id;
    updateBatchPicker();
    history();
    const current = tasks.find(job => job.id === selected);
    if (current) display(current);
    if (selectedBatch) {
      const id = selectedBatch;
      const batch = await api(`/api/batches/${id}`);
      if (selectedBatch === id) displayBatch(batch);
    }
  } catch (error) { setError('刷新失败：' + error.message); }
  finally { refreshing = false; }
}

async function cancelJob(id) {
  setBatchError('');
  display(await api(`/api/jobs/${id}/cancel`, {method: 'POST'}));
  await refresh();
}

async function start(event) {
  event.preventDefault();
  setError('');
  if (!settings?.backend?.ready || settings.showcase || submitting) return;
  const hy = settings.kind === 'hymotion';
  const config = hy ? {
    seconds: Number($('seconds').value), seed: Number($('seed').value),
    steps: Number($('steps').value), threads: Number($('threads').value)
  } : {
    threads: Number($('threads').value), start_frame: Number($('start-frame').value),
    max_frames: Number($('max-frames').value), render_skeleton: $('render-skeleton').checked,
    use_image_features: $('image-features').checked
  };
  let request, fingerprint;
  if (hy && mode === 'batch') {
    const prompts = promptLines();
    const repeat = Number($('batch-repeat').value);
    if (!prompts.length) return setError('请填写动作描述，一行一个动作。');
    if (prompts.some(prompt => prompt.length > 3000)) return setError('每条描述最多 3000 字符，请缩短后重试。');
    if (!Number.isInteger(repeat) || repeat < 1 || repeat > 10) return setError('每条份数须为 1–10 的整数。');
    if (prompts.length * repeat > 100) return setError('每批最多 100 项，请减少描述数量或每条份数。');
    request = {path: '/api/batches', options: {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prompts, repeat, seed_mode: $('batch-seed-mode').value, config})}};
    fingerprint = request.path + ':' + request.options.body;
  } else {
    if (hy) config.prompt = $('prompt').value.trim();
    const form = new FormData();
    form.append('config', JSON.stringify(config));
    if (!hy) {
      const file = $('video').files[0];
      if (!file) return setError('请先选择视频');
      if (file.size > 256 * 1024 * 1024) return setError('视频超过 256 MB，请先裁剪。');
      form.append('video', file);
    }
    request = {path: '/api/jobs', options: {method: 'POST', body: form}};
    const file = !hy && $('video').files[0];
    fingerprint = JSON.stringify([request.path, config, file ? [file.name, file.size, file.lastModified, file.type] : null]);
  }
  const attempt = submissionAttempt(fingerprint);
  request.options.headers = {...request.options.headers, 'Idempotency-Key': attempt.key};
  submitting = true;
  updateInputSummary();
  try {
    const result = await api(request.path, request.options);
    // Only a confirmed response makes the next click an intentional new generation.
    confirmSubmission(attempt);
    if (request.path === '/api/batches') {
      selectedBatch = result.id;
      batches = [result, ...batches.filter(batch => batch.id !== result.id)];
      selected = result.jobs?.[0]?.id || null;
      resetPreview();
      updateBatchPicker();
      displayBatch(result);
      if (result.jobs?.[0]) display(result.jobs[0]);
    } else {
      selected = result.id;
      resetPreview();
      display(result);
    }
    await refresh();
  } catch (error) {
    const retryNote = pendingSubmission?.key === attempt.key ? ' 提交标识已保留；相同输入重试会获取原任务，不会重复创建。' : '';
    setError(error.message + retryNote);
  }
  finally { submitting = false; updateInputSummary(); }
}

function showEnvironment() {
  const runtime = settings.runtime || {mode: 'local', backend: 'local', cancel_on_close: true};
  const cloud = runtime.backend === 'fc';
  const label = cloud ? '阿里 FC · GPU' : runtime.mode === 'hosted' ? '服务器 CPU' : '本机 CPU';
  $('environment-badge').textContent = settings.showcase ? '界面演示' : `${label} · ${settings.backend.ready ? '已就绪' : '未就绪'}`;
  $('environment-badge').classList.toggle('ready', settings.backend.ready && !settings.showcase);
  let message;
  if (settings.showcase) message = `${settings.backend.message} 此页面不启动模型或调用付费云服务，可切换查看单个和批量界面。`;
  else {
    message = settings.backend.ready ? (cloud ? '已连接阿里 FC GPU 执行环境。' : runtime.mode === 'hosted' ? '已连接服务器模型环境。' : '已连接现有本机版本。') : `${settings.backend.message} `;
    message += runtime.cancel_on_close ? '关闭最后一个页面将取消任务并停止服务。' : '关闭页面后任务继续运行，可稍后回到本页查看结果。';
  }
  $('environment').textContent = message;
  $('environment').classList.toggle('ready', settings.backend.ready && !settings.showcase);
  $('thread-settings').hidden = cloud;
  $('advanced-settings').hidden = cloud && settings.kind === 'hymotion';
}

$('generate-form').addEventListener('submit', start);
$('new-task').onclick = reset;
$('refresh-history').onclick = refresh;
$('refresh-batches').onclick = refresh;
$('mode-single').onclick = () => setMode('single');
$('mode-batch').onclick = () => setMode('batch');
$('batch-prompts').addEventListener('input', updateInputSummary);
$('batch-repeat').addEventListener('input', updateInputSummary);
$('batch-seed-mode').addEventListener('change', updateInputSummary);
$('batch-picker').onchange = () => selectBatch($('batch-picker').value).catch(error => setBatchError(error.message));
$('cancel-batch').onclick = async () => {
  if (!selectedBatch) return;
  const id = selectedBatch;
  $('cancel-batch').disabled = true;
  setBatchError('');
  try {
    const batch = await api(`/api/batches/${id}/cancel`, {method: 'POST'});
    if (selectedBatch === id) displayBatch(batch);
    await refresh();
  } catch (error) { setBatchError(error.message); }
  finally { $('cancel-batch').disabled = false; }
};
document.querySelectorAll('[data-prompt]').forEach(button => { button.onclick = () => { $('prompt').value = button.dataset.prompt; }; });
$('video').onchange = () => showUpload($('video').files[0]);
$('dropzone').ondragover = event => { event.preventDefault(); $('dropzone').classList.add('drag'); };
$('dropzone').ondragleave = () => $('dropzone').classList.remove('drag');
$('dropzone').ondrop = event => {
  event.preventDefault();
  $('dropzone').classList.remove('drag');
  if (event.dataTransfer.files.length) { $('video').files = event.dataTransfer.files; showUpload($('video').files[0]); }
};
$('cancel').onclick = async () => {
  if (!selected) return;
  $('cancel').disabled = true;
  try { await cancelJob(selected); }
  catch (error) { setError(error.message); }
  finally { $('cancel').disabled = false; }
};
$('output-video').addEventListener('error', () => { $('preview-note').textContent = '浏览器不支持原版视频编码，请下载视频播放。'; });

(async () => {
  try {
    settings = await api('/api/status');
    appBase = normalizeBase(settings.runtime?.root_path ?? window.MOTION_BASE_PATH ?? appBase);
    await startClient();
    document.title = settings.title;
    $('brand-name').textContent = settings.title;
    $('headline').textContent = settings.subtitle;
    $('description').textContent = settings.description;
    $('source-link').href = settings.upstream;
    $('repo-link').href = 'https://github.com/Chencole/' + (settings.kind === 'hymotion' ? 'hy-motion-webui' : 'gemx-webui');
    const hy = settings.kind === 'hymotion';
    $('text-input').hidden = !hy;
    $('hy-params').hidden = !hy;
    $('video-input').hidden = hy;
    $('gem-params').hidden = hy;
    $('gem-options').hidden = hy;
    $('input-title').textContent = hy ? '描述动作' : '导入视频';
    $('new-task').textContent = hy ? '＋ 创建动作' : '＋ 新建视频任务';
    $('footer-credit').textContent = hy ? 'Powered by Tencent HY' : 'Model: NVIDIA GEM-X';
    showEnvironment();
    setMode('single');
    await refresh();
    poller = setInterval(refresh, 1500);
  } catch (error) {
    setError(error.message);
    $('generate').disabled = true;
    $('environment').textContent = '无法连接服务，请确认服务仍在运行。';
  }
})();
