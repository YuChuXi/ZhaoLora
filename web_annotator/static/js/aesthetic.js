/**
 * aesthetic.js —— 美学评分（内嵌报告）
 */
(function () {
  'use strict';

  var _picker = null;
  var _pollTimer = null;
  var _toastTimer = null;
  var _lastReportMtime = null;

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
    document.getElementById('device').disabled = running;
    document.getElementById('chk-rename').disabled = running;
    document.getElementById('btn-all').disabled = running;
    document.getElementById('btn-none').disabled = running;
    if (_picker) _picker.setDisabled(running);
  }

  function showReport(url, mtime) {
    var frame = document.getElementById('report-frame');
    var empty = document.getElementById('report-empty');
    if (!url) {
      frame.hidden = true;
      frame.removeAttribute('src');
      empty.hidden = false;
      return;
    }
    var src = url + (mtime != null ? ('?t=' + encodeURIComponent(String(mtime))) : ('?t=' + Date.now()));
    if (_lastReportMtime === mtime && frame.getAttribute('src')) {
      empty.hidden = true;
      frame.hidden = false;
      return;
    }
    _lastReportMtime = mtime;
    frame.src = src;
    frame.hidden = false;
    empty.hidden = true;
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
    document.getElementById('m-renamed').textContent = String(s.renamed || 0);
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

    if (s.status === 'done' && s.report_ready) {
      showReport(s.report_url || '/api/aesthetic/report', s.report_mtime);
    } else if (s.status === 'idle' && s.report_ready) {
      showReport(s.report_url || '/api/aesthetic/report', s.report_mtime);
    } else if (s.status === 'running') {
      // 运行中保留旧报告，不打断查看
    } else if (s.status === 'error' || s.status === 'cancelled') {
      // 保持现有报告
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
      var s = await api('/api/aesthetic/status');
      applyStatus(s);
      if (s.status !== 'running') {
        stopPoll();
        if (s.status === 'done') {
          _lastReportMtime = null;
          showReport(s.report_url || '/api/aesthetic/report', s.report_mtime || Date.now());
          toast('评分完成');
        }
        if (s.status === 'cancelled') toast('已取消');
        if (s.status === 'error') toast('评分失败');
      }
    }, 300);
  }

  async function loadDirs() {
    _picker = DirPicker.mount(document.getElementById('dir-list'), {});
    var data = await _picker.load('/api/aesthetic/dirs');

    var sel = document.getElementById('device');
    sel.value = 'cpu';
    var cudaOpt = sel.querySelector('option[value="cuda"]');
    if (cudaOpt) cudaOpt.disabled = !data.cuda;

    if (data.report_ready) {
      showReport(data.report_url || '/api/aesthetic/report', Date.now());
    }
  }

  async function startJob() {
    var dirs = selectedDirNames();
    if (!dirs.length) {
      toast('请选择目录');
      return;
    }
    var body = {
      dirs: dirs,
      device: document.getElementById('device').value,
      rename: document.getElementById('chk-rename').checked,
      write_report: true
    };
    var data = await api('/api/aesthetic/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    applyStatus(data.status || { status: 'running' });
    toast('已开始');
    startPoll();
  }

  async function cancelJob() {
    await api('/api/aesthetic/cancel', { method: 'POST' });
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
    .then(function () { return api('/api/aesthetic/status'); })
    .then(function (s) {
      applyStatus(s);
      if (s.status === 'running') startPoll();
    })
    .catch(function (e) { toast(String(e.message || e)); });
})();
