/**
 * tagging.js —— 批量打标
 */
(function () {
  'use strict';

  var _picker = null;
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
    return _picker ? _picker.selected() : DirPicker.loadSaved();
  }


  function setRunningUi(running) {
    document.getElementById('btn-start').disabled = running;
    document.getElementById('btn-cancel').disabled = !running;
    document.getElementById('threshold').disabled = running;
    document.getElementById('chk-disable-orig').disabled = running;
    document.getElementById('chk-force').disabled = running;
    document.getElementById('btn-all').disabled = running;
    document.getElementById('btn-none').disabled = running;
    if (_picker) _picker.setDisabled(running);
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
    _picker = DirPicker.mount(document.getElementById('dir-list'), {});
    var data = await _picker.load('/api/tagging/dirs');

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
    if (_picker) _picker.setAll(true);
  });
  document.getElementById('btn-none').addEventListener('click', function () {
    if (_picker) _picker.setAll(false);
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
