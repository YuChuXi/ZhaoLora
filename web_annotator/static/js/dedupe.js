/**
 * dedupe.js —— 近似去重
 */
(function () {
  'use strict';

  var _groups = [];
  var _selected = {};
  var _previewMembers = [];
  var _previewIdx = 0;
  var _previewOpen = false;
  var _algorithm = 'mse';

  async function api(url, opts) {
    var res = await fetch(url, Object.assign({
      headers: { 'Content-Type': 'application/json' },
    }, opts || {}));
    if (!res.ok) {
      var t = await res.text();
      throw new Error(t || res.statusText);
    }
    return res.json();
  }

  function getDirs() {
    return DirPicker.getDirs();
  }

  var _simValue = { fft: '0.95', orb: '0.05' };
  var _lastAlg = 'mse';

  function syncAlgorithmUI() {
    var alg = document.getElementById('dedupe-alg').value;
    var metric = alg === 'fft' || alg === 'orb';
    document.getElementById('mse-wrap').style.display = alg === 'mse' ? 'flex' : 'none';
    document.getElementById('sim-wrap').style.display = metric ? 'flex' : 'none';
    // 恢复该算法各自保存的阈值, 避免 fft/orb 相互覆盖混淆
    if (metric) document.getElementById('sim-threshold').value = _simValue[alg];
  }

  function updateProgress(s, main) {
    var running = s.status === 'running' || s.status === 'queued';
    var bar = document.getElementById('job-progress');
    if (bar) bar.hidden = !running;
    if (!running) {
      if (main && s.status === 'running') {
        main.innerHTML = '<div class="dedupe-empty">' + (s.message || '扫描中…') + '</div>';
      }
      return;
    }

    var fill = document.getElementById('progress-fill');
    var pctEl = document.getElementById('progress-pct');
    var waiting = s.status === 'queued';
    var indeterminate = waiting || (s.phase !== 'fft' && s.phase !== 'orb');
    fill.classList.toggle('indeterminate', indeterminate);
    if (!indeterminate) {
      var p = Math.min(100, (typeof s.pct === 'number')
        ? s.pct
        : (s.total > 0 ? (s.done / s.total) * 100 : 0));
      fill.style.width = p + '%';
      pctEl.textContent = Math.round(p) + '%';
    } else {
      pctEl.textContent = waiting ? '排队' : '…';
    }
    if (main && s.status === 'running') {
      main.innerHTML = '<div class="dedupe-empty">' + (s.message || '扫描中…') + '</div>';
    }
  }

  function simText(m) {
    if (typeof m.similarity !== 'number') return '';
    if (_algorithm === 'fft' || _algorithm === 'orb') return '相似 ' + Math.round(m.similarity * 100) + '%';
    return 'MSE ' + m.similarity.toFixed(5);
  }

  function showHistogram(data) {
    var panel = document.getElementById('hist-panel');
    var img = document.getElementById('hist-img');
    if (!data || !data.histogram) { panel.hidden = true; return; }
    img.src = data.histogram;
    var labels = { mse: 'MSE', fft: 'FFT 余弦', orb: 'ORB 匹配' };
    document.getElementById('hist-title').textContent =
      '（' + (labels[data.algorithm] || data.algorithm) + '）';
    panel.hidden = false;
  }

  function thumbUrl(h) { return API.thumbnailUrl(h); }
  function imageUrl(p) { return API.imageUrl(p); }

  function selectedCount() {
    var n = 0;
    for (var k in _selected) if (_selected[k]) n++;
    return n;
  }

  function updateStats() {
    document.getElementById('stat-groups').textContent = String(_groups.length);
    document.getElementById('stat-selected').textContent = String(selectedCount());
    document.getElementById('btn-apply').disabled = selectedCount() === 0;
  }

  function syncCardSelected(path) {
    var cards = document.querySelectorAll('.dup-card');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].dataset.path === path) {
        if (_selected[path]) cards[i].classList.add('selected');
        else cards[i].classList.remove('selected');
      }
    }
  }

  function togglePath(path) {
    if (_selected[path]) delete _selected[path];
    else _selected[path] = true;
    syncCardSelected(path);
    updateStats();
    if (_previewOpen) syncPreviewToggleLabel();
  }

  function syncPreviewToggleLabel() {
    var m = _previewMembers[_previewIdx];
    var btn = document.getElementById('dup-preview-toggle');
    if (!m) { btn.textContent = '选中'; return; }
    btn.textContent = _selected[m.path] ? '取消' : '选中';
  }

  function showPreview(members, index) {
    _previewMembers = members || [];
    _previewIdx = Math.max(0, Math.min(index || 0, _previewMembers.length - 1));
    _previewOpen = true;
    document.getElementById('dup-preview').classList.add('show');
    renderPreview();
  }

  function hidePreview() {
    _previewOpen = false;
    _previewMembers = [];
    _previewIdx = 0;
    document.getElementById('dup-preview').classList.remove('show');
    document.getElementById('dup-preview-img').removeAttribute('src');
  }

  function renderPreview() {
    var m = _previewMembers[_previewIdx];
    if (!m) return;
    var img = document.getElementById('dup-preview-img');
    // 先缩略图秒开，再换原图
    if (m.thumbnail_hash) img.src = thumbUrl(m.thumbnail_hash);
    var full = new Image();
    var path = m.path;
    full.onload = function () {
      if (_previewOpen && _previewMembers[_previewIdx] && _previewMembers[_previewIdx].path === path) {
        img.src = full.src;
      }
    };
    full.src = imageUrl(m.path);

    document.getElementById('dup-preview-index').textContent =
      (_previewIdx + 1) + '/' + _previewMembers.length;
    document.getElementById('dup-preview-res').textContent =
      (m.width && m.height) ? (m.width + '×' + m.height) : '-';
    document.getElementById('dup-preview-name').textContent = m.stem || m.path;
    document.getElementById('dup-preview-keep').textContent = m.is_keep ? '建议保留' : '';
    syncPreviewToggleLabel();

    var multi = _previewMembers.length > 1;
    document.getElementById('dup-preview-prev').style.visibility = multi ? 'visible' : 'hidden';
    document.getElementById('dup-preview-next').style.visibility = multi ? 'visible' : 'hidden';
  }

  function previewStep(delta) {
    if (!_previewOpen || _previewMembers.length < 2) return;
    _previewIdx = (_previewIdx + delta + _previewMembers.length) % _previewMembers.length;
    renderPreview();
  }

  function bindPreview(card, members, index) {
    card.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      showPreview(members, index);
    });
  }

  function render() {
    var main = document.getElementById('dedupe-main');
    main.innerHTML = '';
    if (!_groups.length) {
      main.innerHTML = '<div class="dedupe-empty">未发现相似组</div>';
      updateStats();
      return;
    }

    _groups.forEach(function (g, gi) {
      var wrap = document.createElement('div');
      wrap.className = 'dup-group';

      var head = document.createElement('div');
      head.className = 'dup-group-head';
      head.innerHTML = '<span>组 <b>#' + (gi + 1) + '</b></span>' +
        '<span>' + g.size + ' 张</span>' +
        '<span>建议保留 <b>' + (g.keep || '-') + '</b></span>';
      var btnKeepOnly = document.createElement('button');
      btnKeepOnly.type = 'button';
      btnKeepOnly.className = 'btn';
      btnKeepOnly.textContent = '仅留建议';
      btnKeepOnly.addEventListener('click', function () {
        (g.members || []).forEach(function (m) {
          if (m.is_keep) delete _selected[m.path];
          else _selected[m.path] = true;
        });
        render();
      });
      head.appendChild(btnKeepOnly);
      wrap.appendChild(head);

      var members = g.members || [];
      var row = document.createElement('div');
      row.className = 'dup-row';
      members.forEach(function (m, mi) {
        var card = document.createElement('div');
        card.className = 'dup-card' +
          (_selected[m.path] ? ' selected' : '') +
          (m.is_keep ? ' keep-hint' : '');
        card.dataset.path = m.path;
        card.title = m.path;

        var im = document.createElement('img');
        im.src = thumbUrl(m.thumbnail_hash);
        im.alt = m.stem || '';
        im.loading = 'lazy';

        if (m.is_keep) {
          var badge = document.createElement('div');
          badge.className = 'dup-badge';
          badge.textContent = '建议保留';
          card.appendChild(badge);
        }

        var mark = document.createElement('div');
        mark.className = 'dup-mark';
        mark.textContent = '\u2713';

        var meta = document.createElement('div');
        meta.className = 'meta';
        var res = (m.width && m.height) ? (m.width + '×' + m.height) : '';
        var sim = simText(m);
        meta.innerHTML = '<div>' + [res, sim].filter(Boolean).join(' · ') +
          '</div><div class="name">' + (m.stem || m.path) + '</div>';

        card.appendChild(im);
        card.appendChild(mark);
        card.appendChild(meta);
        card.addEventListener('click', function (e) {
          e.preventDefault();
          togglePath(m.path);
        });
        bindPreview(card, members, mi);
        row.appendChild(card);
      });
      wrap.appendChild(row);
      main.appendChild(wrap);
    });
    updateStats();
  }

  async function scan() {
    var dirs = getDirs();
    if (!dirs.length) { toast('请选择目录'); return; }
    var algorithm = document.getElementById('dedupe-alg').value;
    var mse = parseFloat(document.getElementById('mse-threshold').value);
    var sim = parseFloat(document.getElementById('sim-threshold').value);
    var ratio = parseFloat(document.getElementById('res-ratio').value);
    if (algorithm === 'mse' && !(mse > 0)) { toast('MSE 无效'); return; }
    if ((algorithm === 'fft' || algorithm === 'orb') && !(sim > 0 && sim <= 1)) { toast('相似度无效'); return; }
    if (!(ratio >= 1)) { toast('分辨率比无效'); return; }

    hidePreview();
    var main = document.getElementById('dedupe-main');
    main.innerHTML = '<div class="dedupe-empty">扫描中…</div>';
    document.getElementById('btn-scan').disabled = true;
    try {
      var submitted = await api(API.apiPath('/dedupe/scan'), {
        method: 'POST',
        body: JSON.stringify({
          dirs: dirs,
          algorithm: algorithm,
          mse_threshold: mse,
          sim_threshold: sim,
          similar_res_ratio: ratio
        }),
      });
      await watchScan(submitted.job_id);
    } catch (e) {
      main.innerHTML = '<div class="dedupe-empty">扫描失败</div>';
      toast(String(e.message || e));
    } finally {
      document.getElementById('btn-scan').disabled = false;
    }
  }

  /**
   * 订阅扫描任务并处理结果(提交与重连共用).
   * @param {string} jobId
   * 引用: ``scan`` / IIFE 末尾重连.
   */
  async function watchScan(jobId) {
    var main = document.getElementById('dedupe-main');
    var data = await new Promise(function (resolve, reject) {
      API.watchJob(jobId, function (s) {
        updateProgress(s, main);
        if (s.status === 'queued') return;
        if (s.status === 'running') {
          main.innerHTML = '<div class="dedupe-empty">' + (s.message || '扫描中…') + '</div>';
          return;
        }
        if (s.status === 'done') {
          resolve(s.result || {});
          return;
        }
        if (s.status === 'error' || s.status === 'cancelled') {
          reject(new Error(s.message || s.status));
        }
      });
    });
    _groups = data.groups || [];
    _algorithm = data.algorithm || 'mse';
    showHistogram(data);
    _selected = {};
    _groups.forEach(function (g) {
      (g.suggested_remove || []).forEach(function (p) { _selected[p] = true; });
    });
    render();
    toast('完成 ' + _groups.length + ' 组');
  }

  function suggestAll() {
    _selected = {};
    _groups.forEach(function (g) {
      (g.suggested_remove || []).forEach(function (p) { _selected[p] = true; });
    });
    render();
    if (_previewOpen) syncPreviewToggleLabel();
  }

  function clearSel() {
    _selected = {};
    render();
    if (_previewOpen) syncPreviewToggleLabel();
  }

  async function applyDelete() {
    var paths = Object.keys(_selected).filter(function (p) { return _selected[p]; });
    if (!paths.length) { toast('未选择图片'); return; }
    if (!confirm('删除 ' + paths.length + ' 张到回收站？')) return;
    hidePreview();
    try {
      var res = await api(API.apiPath('/dedupe/apply'), {
        method: 'POST',
        body: JSON.stringify({ paths: paths, dirs: getDirs() }),
      });
      toast('已删除 ' + res.moved + ' 张');
      var gone = {};
      paths.forEach(function (p) { gone[p] = true; });
      _groups = _groups.map(function (g) {
        var members = (g.members || []).filter(function (m) { return !gone[m.path]; });
        return Object.assign({}, g, {
          members: members,
          size: members.length,
          suggested_remove: (g.suggested_remove || []).filter(function (p) { return !gone[p]; }),
        });
      }).filter(function (g) { return g.size >= 2; });
      _selected = {};
      render();
    } catch (e) {
      toast('删除失败: ' + (e.message || e));
    }
  }

  document.getElementById('btn-scan').addEventListener('click', function () {
    scan().catch(function (e) { toast(String(e.message || e)); });
  });
  /**
   * MSE 阈值上下箭头: 尾数步进 (1→9)e-N → (1→9)e-(N-1).
   * 上箭头增大: 1e-6 → 2e-6 → … → 9e-6 → 1e-5 → …
   * 下箭头减小: 1e-5 → 9e-6 → 8e-6 → … → 1e-6 → 9e-7 → …
   */
  document.getElementById('mse-threshold').addEventListener('keydown', function (e) {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    e.preventDefault();
    var v = parseFloat(this.value);
    if (!(v > 0)) v = 1e-4;
    var exp = Math.floor(Math.log10(v));
    var mant = Math.round(v / Math.pow(10, exp));
    if (mant < 1) mant = 1;
    if (mant > 9) mant = 9;
    if (e.key === 'ArrowUp') {
      mant++;
      if (mant > 9) { mant = 1; exp++; }
    } else {
      mant--;
      if (mant < 1) { mant = 9; exp--; }
    }
    if (exp > 0) exp = 0;
    if (exp < -9) exp = -9;
    this.value = mant + 'e' + exp;
    this.dispatchEvent(new Event('input'));
  });
  document.getElementById('dedupe-alg').addEventListener('change', function () {
    // 切换前保存当前算法的阈值, 再切换到新算法对应的阈值
    if (_lastAlg === 'fft' || _lastAlg === 'orb') {
      _simValue[_lastAlg] = document.getElementById('sim-threshold').value;
    }
    syncAlgorithmUI();
    _lastAlg = document.getElementById('dedupe-alg').value;
  });
  document.getElementById('sim-threshold').addEventListener('input', function () {
    if (_lastAlg === 'fft' || _lastAlg === 'orb') _simValue[_lastAlg] = this.value;
  });
  document.getElementById('btn-suggest').addEventListener('click', suggestAll);
  document.getElementById('btn-deselect').addEventListener('click', clearSel);
  document.getElementById('btn-apply').addEventListener('click', function () {
    applyDelete().catch(function (e) { toast(String(e.message || e)); });
  });

  document.getElementById('dup-preview-prev').addEventListener('click', function (e) {
    e.stopPropagation();
    previewStep(-1);
  });
  document.getElementById('dup-preview-next').addEventListener('click', function (e) {
    e.stopPropagation();
    previewStep(1);
  });
  document.getElementById('dup-preview-close').addEventListener('click', hidePreview);
  document.getElementById('dup-preview-toggle').addEventListener('click', function () {
    var m = _previewMembers[_previewIdx];
    if (m) togglePath(m.path);
  });
  document.getElementById('dup-preview').addEventListener('click', function (e) {
    if (e.target.id === 'dup-preview') hidePreview();
  });

  document.addEventListener('keydown', function (e) {
    if (!_previewOpen) return;
    if (e.key === 'Escape') { e.preventDefault(); hidePreview(); }
    else if (e.key === 'ArrowLeft' || e.key === 'a' || e.key === 'A') { e.preventDefault(); previewStep(-1); }
    else if (e.key === 'ArrowRight' || e.key === 'd' || e.key === 'D') { e.preventDefault(); previewStep(1); }
    else if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault();
      var m = _previewMembers[_previewIdx];
      if (m) togglePath(m.path);
    }
  });

  DirPicker.onChange(function () {
    _groups = [];
    _selected = {};
    document.getElementById('job-progress').hidden = true;
    document.getElementById('hist-panel').hidden = true;
    document.getElementById('dedupe-main').innerHTML =
      '<div class="dedupe-empty">目录已切换</div>';
    updateStats();
  });
  updateStats();
  syncAlgorithmUI();

  // 页面加载时恢复未结束的扫描任务
  API.findActiveJob('dedupe_scan', API.dataset()).then(function (snap) {
    if (!snap) return;
    document.getElementById('btn-scan').disabled = true;
    watchScan(snap.job_id).catch(function (e) {
      document.getElementById('dedupe-main').innerHTML =
        '<div class="dedupe-empty">扫描失败</div>';
      toast(String(e.message || e));
    }).finally(function () {
      document.getElementById('btn-scan').disabled = false;
    });
  });
})();
