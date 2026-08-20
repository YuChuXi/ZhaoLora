/**
 * tagging.js —— WD14 批量打标（标签页「自动生成」对话框，SSE 进度）
 */
var Tagging = (function () {
  'use strict';

  var _watch = null;
  var _jobId = null;
  var _modelReady = false;

  function modal() {
    return document.getElementById('tagging-modal');
  }

  function setRunningUi(running) {
    document.getElementById('btn-start').disabled = running || !_modelReady;
    document.getElementById('btn-cancel').disabled = !running;
    document.getElementById('threshold').disabled = running;
    document.getElementById('chk-disable-orig').disabled = running;
    document.getElementById('chk-force').disabled = running;
    if (typeof DirPicker !== 'undefined') DirPicker.setDisabled(running);
  }

  function applyStatus(s) {
    var total = s.total || 0;
    var done = s.done || 0;
    var pct = (typeof s.pct === 'number') ? s.pct : (total > 0 ? (done / total) * 100 : 0);
    var running = s.status === 'running' || s.status === 'queued';
    var bar = document.getElementById('job-progress');
    if (bar) bar.hidden = !running;

    var fill = document.getElementById('progress-fill');
    var loading = running && s.phase === 'loading';
    var waiting = s.status === 'queued';
    fill.classList.toggle('indeterminate', loading || waiting);
    if (!loading && !waiting) {
      fill.style.width = Math.min(100, pct) + '%';
    }
    document.getElementById('progress-pct').textContent = (loading || waiting)
      ? '…'
      : (Math.min(100, Math.round(pct)) + '%');

    document.getElementById('m-done').textContent = String(done);
    document.getElementById('m-total').textContent = String(total);
    document.getElementById('m-skip').textContent = String(s.skipped || 0);
    document.getElementById('m-fail').textContent = String(s.failed || 0);

    var label = {
      idle: '就绪',
      queued: '等待执行',
      running: '运行中',
      done: '已完成',
      error: '失败',
      cancelled: '已取消'
    }[s.status] || s.status;
    var statusEl = document.getElementById('stat-status');
    statusEl.textContent = (s.message && s.message !== label) ? (label + ' · ' + s.message) : label;

    if (s.logs && s.logs.length) {
      var box = document.getElementById('log-box');
      box.textContent = s.logs.join('\n');
      box.scrollTop = box.scrollHeight;
    }

    setRunningUi(running);

    var openBtn = document.getElementById('btn-auto-tag');
    if (openBtn) {
      openBtn.textContent = running ? '自动生成…' : '自动生成';
    }
  }

  function stopWatch() {
    if (_watch) {
      _watch.close();
      _watch = null;
    }
  }

  function startWatch(jobId) {
    stopWatch();
    _jobId = jobId;
    _watch = API.watchJob(jobId, function (s) {
      applyStatus(s);
      if (s.status === 'done') toast('打标完成');
      if (s.status === 'cancelled') toast('已取消');
      if (s.status === 'error') toast('打标失败');
    });
  }

  async function initModel() {
    var data = await API.request(API.apiPath('/tagging/dirs'));
    var info = document.getElementById('model-info');
    var warn = document.getElementById('model-warn');
    _modelReady = !!data.model_ready;
    if (_modelReady) {
      info.textContent = '模型就绪';
      warn.hidden = true;
    } else {
      info.textContent = '模型未就绪';
      warn.hidden = false;
      warn.textContent = data.model_path || '未找到模型';
      document.getElementById('btn-start').disabled = true;
    }
    if (data.default_threshold != null) {
      document.getElementById('threshold').value = String(data.default_threshold);
    }
  }

  async function startJob() {
    var dirs = DirPicker.getDirs();
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
      force: document.getElementById('chk-force').checked,
      artist_tag: document.getElementById('artist-tag').value.trim()
    };
    var data = await API.request(API.apiPath('/tagging/start'), {
      method: 'POST',
      body: JSON.stringify(body)
    });
    applyStatus({ status: 'queued', message: '等待执行' });
    toast('已提交');
    startWatch(data.job_id);
  }

  async function cancelJob() {
    if (!_jobId) return;
    await API.cancelJob(_jobId);
    toast('正在取消…');
  }

  /**
   * 页面刷新后恢复未结束的打标任务.
   * 引用: ``init``.
   */
  async function resumeActive() {
    var snap = await API.findActiveJob('tagging', API.dataset());
    if (!snap) return;
    applyStatus(snap);
    startWatch(snap.job_id);
    toast('已恢复打标进度');
  }

  function open() {
    var m = modal();
    if (!m) return;
    m.classList.add('open');
    m.setAttribute('aria-hidden', 'false');
  }

  function close() {
    var m = modal();
    if (!m) return;
    m.classList.remove('open');
    m.setAttribute('aria-hidden', 'true');
  }

  function init() {
    if (!document.getElementById('btn-start')) return;

    var openBtn = document.getElementById('btn-auto-tag');
    if (openBtn) openBtn.addEventListener('click', open);

    var closeBtn = document.getElementById('btn-tagging-close');
    if (closeBtn) closeBtn.addEventListener('click', close);

    var m = modal();
    if (m) {
      m.addEventListener('click', function (e) {
        if (e.target === m) close();
      });
    }

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var box = modal();
      if (box && box.classList.contains('open')) close();
    });

    document.getElementById('btn-start').addEventListener('click', function () {
      startJob().catch(function (e) { toast(String(e.message || e)); });
    });
    document.getElementById('btn-cancel').addEventListener('click', function () {
      cancelJob().catch(function (e) { toast(String(e.message || e)); });
    });

    initModel().catch(function (e) { toast(String(e.message || e)); });
    resumeActive();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return { open: open, close: close };
})();
