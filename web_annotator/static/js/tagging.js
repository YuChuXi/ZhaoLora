/**
 * tagging.js —— 批量打标
 */
(function () {
  'use strict';

  var _dirs = [];
  var _pollTimer = null;
  var _toastTimer = null;

  function toast(msg) {
    var el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  async function api(url, opts) {
    var res = await fetch(url, opts);
    if (!res.ok) {
      var t = await res.text();
      throw new Error(t || res.statusText);
    }
    return res.json();
  }

  function selectedDirNames() {
    return _dirs.filter(function (d) { return d.checked; }).map(function (d) { return d.name; });
  }

  function renderDirs() {
    var list = document.getElementById('dir-list');
    list.innerHTML = '';
    if (!_dirs.length) {
      list.innerHTML = '<div style="padding:8px;color:var(--text-dim);font-size:12px;">暂无图片目录</div>';
      return;
    }
    _dirs.forEach(function (d, idx) {
      var row = document.createElement('label');
      row.className = 'dir-item';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!d.checked;
      cb.addEventListener('change', function () {
        _dirs[idx].checked = cb.checked;
      });
      var name = document.createElement('span');
      name.textContent = d.label || d.name;
      var meta = document.createElement('span');
      meta.className = 'meta';
      meta.textContent = d.image_count + ' 图';
      row.appendChild(cb);
      row.appendChild(name);
      row.appendChild(meta);
      list.appendChild(row);
    });
  }

  function setRunningUi(running) {
    document.getElementById('btn-start').disabled = running;
    document.getElementById('btn-cancel').disabled = !running;
    document.getElementById('threshold').disabled = running;
    document.getElementById('chk-disable-orig').disabled = running;
    document.getElementById('chk-force').disabled = running;
    document.getElementById('btn-all').disabled = running;
    document.getElementById('btn-none').disabled = running;
  }

  function applyStatus(s) {
    var total = s.total || 0;
    var done = s.done || 0;
    var pct = (typeof s.pct === 'number') ? s.pct : (total > 0 ? (done / total) * 100 : 0);
    var fill = document.getElementById('progress-fill');
    var loading = s.status === 'running' && s.phase === 'loading';
    fill.classList.toggle('indeterminate', loading);
    if (!loading) {
      fill.style.width = Math.min(100, pct) + '%';
    }
    document.getElementById('progress-pct').textContent = loading
      ? '…'
      : (Math.min(100, Math.round(pct)) + '%');
    document.getElementById('progress-meta-line').textContent = s.current
      ? s.current
      : (s.message || '等待开始');

    document.getElementById('m-done').textContent = String(done);
    document.getElementById('m-total').textContent = String(total);
    document.getElementById('m-ok').textContent = String(s.ok || 0);
    document.getElementById('m-skip').textContent = String(s.skipped || 0);
    document.getElementById('m-fail').textContent = String(s.failed || 0);
    document.getElementById('m-current').textContent = s.current
      ? ('当前: ' + s.current)
      : (s.message || '');

    var label = {
      idle: '就绪',
      running: '运行中',
      done: '已完成',
      error: '失败',
      cancelled: '已取消'
    }[s.status] || s.status;
    document.getElementById('stat-status').textContent = label + (s.message ? ' · ' + s.message : '');

    if (s.logs && s.logs.length) {
      document.getElementById('log-box').textContent = s.logs.join('\n');
      var box = document.getElementById('log-box');
      box.scrollTop = box.scrollHeight;
    }

    setRunningUi(s.status === 'running');
  }

  function stopPoll() {
    if (_pollTimer) {
      clearInterval(_pollTimer);
      _pollTimer = null;
    }
  }

  function startPoll() {
    stopPoll();
    _pollTimer = setInterval(async function () {
      var s = await api('/api/tagging/status');
      applyStatus(s);
      if (s.status !== 'running') {
        stopPoll();
        if (s.status === 'done') toast('打标完成');
        if (s.status === 'cancelled') toast('已取消');
        if (s.status === 'error') toast('打标失败');
      }
    }, 300);
  }

  async function loadDirs() {
    var data = await api('/api/tagging/dirs');
    _dirs = (data.dirs || []).map(function (d) {
      return {
        name: d.name,
        label: d.label,
        image_count: d.image_count,
        checked: false
      };
    });
    // 默认勾选 head（若存在）
    _dirs.forEach(function (d) {
      if (d.name === 'head') d.checked = true;
    });
    renderDirs();

    var info = document.getElementById('model-info');
    var warn = document.getElementById('model-warn');
    if (data.model_ready) {
      info.textContent = '模型已加载';
      warn.hidden = true;
    } else {
      info.textContent = '模型未就绪';
      warn.hidden = false;
      warn.textContent = data.model_path || '未找到 WD14 模型';
      document.getElementById('btn-start').disabled = true;
    }
    if (data.default_threshold != null) {
      document.getElementById('threshold').value = String(data.default_threshold);
    }
  }

  async function startJob() {
    var dirs = selectedDirNames();
    if (!dirs.length) {
      toast('请选择目录');
      return;
    }
    var threshold = parseFloat(document.getElementById('threshold').value);
    if (!(threshold >= 0 && threshold <= 1)) {
      toast('置信度须在 0～1');
      return;
    }
    var body = {
      dirs: dirs,
      threshold: threshold,
      disable_orig_tags: document.getElementById('chk-disable-orig').checked,
      force: document.getElementById('chk-force').checked
    };
    var data = await api('/api/tagging/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    applyStatus(data.status || { status: 'running' });
    toast('已开始');
    startPoll();
  }

  async function cancelJob() {
    await api('/api/tagging/cancel', { method: 'POST' });
    toast('正在取消…');
  }

  document.getElementById('btn-all').addEventListener('click', function () {
    _dirs.forEach(function (d) { d.checked = true; });
    renderDirs();
  });
  document.getElementById('btn-none').addEventListener('click', function () {
    _dirs.forEach(function (d) { d.checked = false; });
    renderDirs();
  });
  document.getElementById('btn-start').addEventListener('click', function () {
    startJob().catch(function (e) { toast(String(e.message || e)); });
  });
  document.getElementById('btn-cancel').addEventListener('click', function () {
    cancelJob().catch(function (e) { toast(String(e.message || e)); });
  });

  loadDirs()
    .then(function () { return api('/api/tagging/status'); })
    .then(function (s) {
      applyStatus(s);
      if (s.status === 'running') startPoll();
    })
    .catch(function (e) { toast(String(e.message || e)); });
})();
