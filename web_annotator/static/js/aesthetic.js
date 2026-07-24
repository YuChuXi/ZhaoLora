/**
 * aesthetic.js —— 美学评分（Server-Sent Events 进度）。
 *
 * 顶栏仅操作；底栏六段状态；进度条仅 queued/running 显示。
 * 引用：templates/aesthetic.html。
 */
(function () {
  'use strict';

  var _watch = null;
  var _jobId = null;
  var _toastTimer = null;
  var _lastReportMtime = null;

  /**
   * @param {string} msg
   */
  function toast(msg) {
    var el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  /**
   * @returns {string[]}
   */
  function currentDirs() {
    return DirPicker.getDirs();
  }

  /**
   * @param {boolean} running
   */
  function setRunningUi(running) {
    document.getElementById('btn-start').disabled = running;
    document.getElementById('btn-cancel').disabled = !running;
    document.getElementById('device').disabled = running;
    DirPicker.setDisabled(running);
  }

  /**
   * @param {string|null} url
   * @param {number|null} mtime
   */
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

  /**
   * 应用任务状态到底栏与进度条。
   * 段1 任务态 · 段2 完成数 · 段5 进度 · 段6 失败与说明。
   * 引用：startWatch、startJob。
   * @param {object} s
   */
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

    var label = {
      idle: '就绪',
      queued: '等待执行',
      running: '运行中',
      done: '已完成',
      error: '失败',
      cancelled: '已取消'
    }[s.status] || s.status;

    document.getElementById('seg-pos').textContent = label;
    document.getElementById('seg-done').textContent = '完成 ' + done;
    document.getElementById('seg-metric').textContent = '进度 ' + done + '/' + total;

    var extras = ['失败 ' + (s.failed || 0)];
    var note = s.message || '';
    if (note && note !== label) extras.push(note);
    if (s.current) extras.push(s.current);
    document.getElementById('seg-extra').textContent = extras.join(' · ');

    if ((s.status === 'done' || s.status === 'idle') && s.report_ready) {
      showReport(s.report_url || API.apiPath('/aesthetic/report'), s.report_mtime);
    }

    setRunningUi(running);
  }

  function stopWatch() {
    if (_watch) {
      _watch.close();
      _watch = null;
    }
  }

  /**
   * @param {string} jobId
   */
  function startWatch(jobId) {
    stopWatch();
    _jobId = jobId;
    _watch = API.watchJob(jobId, function (s) {
      applyStatus(s);
      if (s.status === 'done') {
        _lastReportMtime = null;
        showReport(s.report_url || API.apiPath('/aesthetic/report'), s.report_mtime || Date.now());
        toast('评分完成');
      }
      if (s.status === 'cancelled') toast('已取消');
      if (s.status === 'error') toast('评分失败');
    });
  }

  async function initDevice() {
    var data = await API.request(API.apiPath('/aesthetic/dirs'));
    var sel = document.getElementById('device');
    sel.value = 'cpu';
    var cudaOpt = sel.querySelector('option[value="cuda"]');
    if (cudaOpt) cudaOpt.disabled = !data.cuda;
    if (data.report_ready) {
      showReport(data.report_url || API.apiPath('/aesthetic/report'), Date.now());
    }
    applyStatus({ status: 'idle', done: 0, total: 0, failed: 0 });
  }

  async function startJob() {
    var dirs = currentDirs();
    if (!dirs.length) {
      toast('请选择目录');
      return;
    }
    var body = {
      dirs: dirs,
      device: document.getElementById('device').value,
      write_report: true
    };
    var data = await API.request(API.apiPath('/aesthetic/start'), {
      method: 'POST',
      body: JSON.stringify(body)
    });
    applyStatus({ status: 'queued', message: '等待执行', done: 0, total: 0, failed: 0 });
    toast('已提交');
    startWatch(data.job_id);
  }

  async function cancelJob() {
    if (!_jobId) return;
    await API.cancelJob(_jobId);
    toast('正在取消…');
  }

  document.getElementById('btn-start').addEventListener('click', function () {
    startJob().catch(function (e) { toast(String(e.message || e)); });
  });
  document.getElementById('btn-cancel').addEventListener('click', function () {
    cancelJob().catch(function (e) { toast(String(e.message || e)); });
  });

  initDevice().catch(function (e) { toast(String(e.message || e)); });
})();
