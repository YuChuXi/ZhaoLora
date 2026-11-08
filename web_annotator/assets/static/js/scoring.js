/**
 * scoring.js —— 统一评分页（美学 + WD14 评分项），前端 JSON 渲染画廊。
 *
 * 评分项由数据集定义, 后端按 WD14 缓存概率计算; 本页负责标签切换、筛选排序、
 * 评分项增删与 WD14 缓存建立。
 *
 * 引用：templates/scoring.html。
 */
(function () {
  'use strict';

  var _watch = null;
  var _jobId = null;
  var _kind = 'aesthetic';
  var _items = [];
  var _counts = {};
  var _results = {};
  var _view = [];
  var _wd14 = { ready: false, count: 0, total: 0 };
  var _sortKey = 'score';
  var _sortDesc = true;

  var AES_COLOURS = [
    '#c0392b', '#e74c3c', '#e67e22', '#f39c12', '#d4ac0d',
    '#27ae60', '#1e8449', '#148f77', '#0e6655', '#0a4f42',
  ];

  // -----------------------------------------------------------------------
  // 工具栏
  // -----------------------------------------------------------------------

  function setRunningUi(running) {
    document.getElementById('btn-start').disabled = running;
    document.getElementById('btn-cache').disabled = running || !_wd14.ready;
    document.getElementById('btn-cancel').disabled = !running;
    DirPicker.setDisabled(running);
  }

  function currentDirs() {
    return DirPicker.getDirs();
  }

  function kindLabel(kind) {
    if (kind === 'aesthetic') return '美学';
    var item = _items.filter(function (x) { return x.key === kind; })[0];
    return item ? item.label : kind;
  }

  function isKnownKind(kind) {
    return kind === 'aesthetic' || _items.some(function (x) { return x.key === kind; });
  }

  // -----------------------------------------------------------------------
  // 进度
  // -----------------------------------------------------------------------

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
      ? '\u2026'
      : (Math.min(100, Math.round(pct)) + '%');

    var label = {
      idle: '\u5C31\u7EEA',
      queued: '\u7B49\u5F85\u6267\u884C',
      running: '\u8FD0\u884C\u4E2D',
      done: '\u5DF2\u5B8C\u6210',
      error: '\u5931\u8D25',
      cancelled: '\u5DF2\u53D6\u6D88'
    }[s.status] || s.status;

    document.getElementById('seg-pos').textContent = label;
    document.getElementById('seg-done').textContent = '\u5B8C\u6210 ' + done;
    document.getElementById('seg-metric').textContent = '\u8FDB\u5EA6 ' + done + '/' + total;

    var extras = ['\u5931\u8D25 ' + (s.failed || 0)];
    var note = s.message || '';
    if (note && note !== label) extras.push(note);
    if (s.current) extras.push(s.current);
    document.getElementById('seg-extra').textContent = extras.join(' \u00B7 ');

    setRunningUi(running);
  }

  function stopWatch() {
    if (_watch) { _watch.close(); _watch = null; }
  }

  function startWatch(jobId, onDone) {
    stopWatch();
    _jobId = jobId;
    _watch = API.watchJob(jobId, function (s) {
      applyStatus(s);
      if (s.status === 'done') {
        if (onDone) onDone(s);
        else loadResults(_kind);
      }
      if (s.status === 'cancelled') {
        toast('已取消');
        refreshDirs().catch(function () {});
      }
      if (s.status === 'error') toast('任务失败');
    });
  }

  // -----------------------------------------------------------------------
  // 标签页
  // -----------------------------------------------------------------------

  function renderTabs() {
    var kinds = [{ key: 'aesthetic', label: '\u7F8E\u5B66' }].concat(
      _items.map(function (item) { return { key: item.key, label: item.label }; })
    );
    var box = document.getElementById('result-tab-list');
    box.innerHTML = kinds.map(function (k) {
      var active = k.key === _kind ? ' active' : '';
      return '<button class="result-tab' + active + '" data-kind="' + escHtml(k.key) + '">' +
        escHtml(k.label) + ' <span class="tab-badge">' + (_counts[k.key] || 0) + '</span></button>';
    }).join('');
    box.querySelectorAll('.result-tab').forEach(function (tab) {
      tab.addEventListener('click', function () { switchKind(tab.dataset.kind); });
    });
  }

  function switchKind(kind) {
    if (kind === _kind) return;
    _kind = kind;
    document.getElementById('searchBox').value = '';
    document.getElementById('scoreFilter').value = 'all';
    persistScoring();
    renderTabs();
    if (_results[kind]) {
      renderGallery();
    } else {
      loadResults(kind);
    }
  }

  // -----------------------------------------------------------------------
  // 结果加载与渲染
  // -----------------------------------------------------------------------

  function loadResults(kind) {
    API.request(API.apiPath('/scoring/results?kind=' + encodeURIComponent(kind))).then(function (data) {
      _results[kind] = data.results || [];
      _counts[kind] = data.total || 0;
      renderTabs();
      if (kind === _kind) renderGallery();
    }).catch(function (e) {
      if (kind === _kind) {
        _results[kind] = [];
        renderGallery();
      }
      toast(String(e.message || e));
    });
  }

  /**
   * 计算评分项直方图的分档边界。
   * @param {Array<{score: number}>} results 当前评分项的全部结果
   * @returns {{lo: number, hi: number, step: number}}
   */
  function scoreBins(results) {
    var lo = Infinity;
    var hi = -Infinity;
    for (var i = 0; i < results.length; i++) {
      var v = results[i].score;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (hi - lo < 1e-9) hi = lo + 1e-6;
    return { lo: lo, hi: hi, step: (hi - lo) / 10 };
  }

  function scoreBin(value, bins) {
    return Math.min(9, Math.max(0, Math.floor((value - bins.lo) / bins.step)));
  }

  function renderGallery() {
    var results = _results[_kind] || [];
    var gallery = document.getElementById('gallery');
    var empty = document.getElementById('gallery-empty');
    var stats = document.getElementById('stats');
    var histSection = document.getElementById('hist-section');
    var searchBar = document.getElementById('search-bar');

    if (!results.length) {
      gallery.innerHTML = '';
      gallery.style.display = 'none';
      _view = [];
      if (empty) empty.style.display = 'flex';
      if (stats) stats.style.display = 'none';
      if (histSection) histSection.style.display = 'none';
      if (searchBar) searchBar.style.display = 'none';
      document.getElementById('result-count').textContent = '0';
      return;
    }

    if (empty) empty.style.display = 'none';
    if (histSection) histSection.style.display = '';
    if (searchBar) searchBar.style.display = 'flex';
    gallery.style.display = '';

    var sorted = results.slice();
    sorted.sort(function (a, b) {
      var va = _sortKey === 'path' ? a.path : a.score;
      var vb = _sortKey === 'path' ? b.path : b.score;
      if (va < vb) return _sortDesc ? 1 : -1;
      if (va > vb) return _sortDesc ? -1 : 1;
      return 0;
    });

    var bins = _kind === 'aesthetic' ? null : scoreBins(sorted);
    renderHist(sorted, bins);

    var query = (document.getElementById('searchBox').value || '').toLowerCase();
    var scoreFilter = document.getElementById('scoreFilter').value;

    var visible = [];
    var html = '';
    for (var i = 0; i < sorted.length; i++) {
      var r = sorted[i];
      if (query && r.path.toLowerCase().indexOf(query) === -1) continue;
      if (_kind === 'aesthetic') {
        if (scoreFilter !== 'all' && String(r.pony) !== scoreFilter) continue;
      } else if (scoreFilter !== 'all' && String(scoreBin(r.score, bins)) !== scoreFilter) {
        continue;
      }
      var idx = visible.length;
      visible.push(r);
      html += '<div class="card" onclick="ScoringView.openLightbox(' + idx + ')">' +
        '<div class="card-img"><img src="' + escHtml(r.thumb_url || r.image_url) +
        '" loading="lazy" onerror="this.src=\'' + escHtml(r.image_url) + '\'"></div>' +
        _scoreBadge(r) +
        '<div class="card-score-val">' + r.score.toFixed(4) + '</div>' +
        '<div class="card-path" title="' + escHtml(r.path) + '">' + _fileName(r.path) + '</div>' +
        '</div>';
    }

    _view = visible;
    gallery.innerHTML = html;
    document.getElementById('result-count').textContent = String(visible.length);

    if (stats) {
      stats.style.display = '';
      renderStats(visible);
    }
  }

  function _scoreBadge(r) {
    if (_kind !== 'aesthetic') return '';
    var pony = r.pony || 0;
    return '<span class="card-badge" style="background:' + AES_COLOURS[pony] + '">score_' + pony + '</span>';
  }

  function _histColour(i, total) {
    return 'hsl(' + (200 - Math.round(i * 200 / Math.max(1, total - 1))) + ',72%,52%)';
  }

  function renderStats(results) {
    var scores = results.map(function (r) { return r.score; });
    var n = scores.length;
    if (!n) {
      ['stat-mean', 'stat-median', 'stat-std', 'stat-min', 'stat-max'].forEach(function (id) {
        document.getElementById(id).textContent = '-';
      });
      document.getElementById('stat-count').textContent = '0';
      return;
    }
    var mean = scores.reduce(function (a, b) { return a + b; }, 0) / n;
    var sorted = scores.slice().sort(function (a, b) { return a - b; });
    var median = n % 2 ? sorted[(n - 1) / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
    var variance = scores.reduce(function (s, v) { return s + (v - mean) * (v - mean); }, 0) / n;
    var std = Math.sqrt(variance);

    document.getElementById('stat-mean').textContent = mean.toFixed(4);
    document.getElementById('stat-median').textContent = median.toFixed(4);
    document.getElementById('stat-std').textContent = std.toFixed(4);
    document.getElementById('stat-min').textContent = sorted[0].toFixed(4);
    document.getElementById('stat-max').textContent = sorted[n - 1].toFixed(4);
    document.getElementById('stat-count').textContent = String(n);
  }

  function renderHist(results, bins) {
    var histEl = document.getElementById('histogram');
    if (!histEl) return;

    var html = '';
    var counts = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    var sel = document.getElementById('scoreFilter');

    if (_kind === 'aesthetic') {
      results.forEach(function (r) { counts[r.pony]++; });
      var maxC = Math.max.apply(null, counts) || 1;
      for (var i = 0; i < 10; i++) {
        html += '<div class="hist-col">' +
          '<div class="hist-count">' + counts[i] + '</div>' +
          '<div class="hist-bar" style="height:' + Math.round(counts[i] / maxC * 120) + 'px;background:' + AES_COLOURS[i] + '"></div>' +
          '<div class="hist-label" style="color:' + AES_COLOURS[i] + '">score_' + i + '</div>' +
          '</div>';
      }
      histEl.innerHTML = html;

      sel.innerHTML = '<option value="all">\u5168\u90E8\u6863\u4F4D</option>';
      for (var j = 0; j < 10; j++) {
        sel.innerHTML += '<option value="' + j + '">score_' + j + ' (' + counts[j] + ')</option>';
      }
    } else {
      results.forEach(function (r) { counts[scoreBin(r.score, bins)]++; });
      var maxQ = Math.max.apply(null, counts) || 1;
      for (var k = 0; k < 10; k++) {
        var lo = bins.lo + bins.step * k;
        var hi = lo + bins.step;
        var colour = _histColour(k, 10);
        html += '<div class="hist-col">' +
          '<div class="hist-count">' + counts[k] + '</div>' +
          '<div class="hist-bar" style="height:' + Math.round(counts[k] / maxQ * 120) + 'px;background:' + colour + '"></div>' +
          '<div class="hist-label" style="color:' + colour + '">' + lo.toFixed(2) + '\u2013' + hi.toFixed(2) + '</div>' +
          '</div>';
      }
      histEl.innerHTML = html;

      sel.innerHTML = '<option value="all">\u5168\u90E8\u6863\u4F4D</option>';
      for (var m = 0; m < 10; m++) {
        var mlo = bins.lo + bins.step * m;
        var mhi = mlo + bins.step;
        sel.innerHTML += '<option value="' + m + '">' + mlo.toFixed(2) + '\u2013' + mhi.toFixed(2) +
          ' (' + counts[m] + ')</option>';
      }
    }

    var savedFilter = (AppSettings.get('scoring') || {}).filter;
    if (savedFilter) {
      sel.value = savedFilter;
      if (sel.selectedIndex < 0) sel.value = 'all';
    }
  }

  // -----------------------------------------------------------------------
  // 灯箱
  // -----------------------------------------------------------------------

  function openLightbox(idx) {
    var r = _view[idx];
    if (!r) return;
    var lb = document.getElementById('lightbox');
    document.getElementById('lightboxImg').src = r.image_url;
    document.getElementById('lightboxScore').textContent = r.score.toFixed(4);
    document.getElementById('lightboxTag').textContent = _kind === 'aesthetic'
      ? ('score_' + r.pony)
      : kindLabel(_kind);
    document.getElementById('lightboxPath').textContent = r.path;
    lb.classList.add('active');
    document.body.style.overflow = 'hidden';
    lb._idx = idx;
  }

  function closeLightbox(e) {
    if (e && e.target !== document.getElementById('lightbox') && e.target !== document.querySelector('.lightbox-close')) return;
    document.getElementById('lightbox').classList.remove('active');
    document.body.style.overflow = '';
  }

  function navLightbox(d) {
    var lb = document.getElementById('lightbox');
    if (!_view.length || lb._idx == null) return;
    openLightbox((lb._idx + d + _view.length) % _view.length);
  }

  // -----------------------------------------------------------------------
  // 评分项管理
  // -----------------------------------------------------------------------

  function renderItemList() {
    var box = document.getElementById('items-list');
    box.innerHTML = '';
    _items.forEach(function (item) {
      var row = document.createElement('div');
      row.className = 'item-row';

      var label = document.createElement('span');
      label.className = 'item-label';
      label.textContent = item.label;
      row.appendChild(label);

      var expr = document.createElement('span');
      expr.className = 'item-expr';
      expr.textContent = item.expr;
      expr.title = item.expr;
      row.appendChild(expr);

      if (item.builtin) {
        var mark = document.createElement('span');
        mark.style.cssText = 'color:var(--text-dim);font-size:11px;';
        mark.textContent = '\u5185\u7F6E';
        row.appendChild(mark);
      } else {
        var del = document.createElement('button');
        del.className = 'btn danger';
        del.type = 'button';
        del.textContent = '\u5220\u9664';
        del.addEventListener('click', function () {
          deleteItem(item.key).catch(function (e) { toast(String(e.message || e)); });
        });
        row.appendChild(del);
      }
      box.appendChild(row);
    });
  }

  async function refreshDirs() {
    var data = await API.request(API.apiPath('/scoring/dirs'));
    _items = data.items || [];
    _counts = { aesthetic: data.aesthetic_count || 0 };
    var counts = data.item_counts || {};
    _items.forEach(function (item) { _counts[item.key] = counts[item.key] || 0; });
    _wd14 = { ready: !!data.wd14_ready, count: data.wd14_count || 0, total: data.wd14_total || 0 };
    var cacheBtn = document.getElementById('btn-cache');
    cacheBtn.disabled = !_wd14.ready;
    cacheBtn.title = _wd14.ready
      ? ('WD14 缓存 ' + _wd14.count + '/' + _wd14.total + ' (全部目录); 点击为所选目录补齐')
      : (data.wd14_message || 'WD14 模型未就绪');
    renderTabs();
    renderItemList();
  }

  async function createItem() {
    var label = document.getElementById('item-label').value.trim();
    var expr = document.getElementById('item-expr').value.trim();
    if (!label || !expr) { toast('请填写名称与表达式'); return; }
    var data = await API.request(API.apiPath('/scoring/items'), {
      method: 'POST',
      body: JSON.stringify({ label: label, expr: expr }),
    });
    _items = data.items || [];
    document.getElementById('item-label').value = '';
    document.getElementById('item-expr').value = '';
    renderItemList();
    await refreshDirs();
    toast('已添加评分项');
  }

  async function deleteItem(key) {
    var data = await API.request(API.apiPath('/scoring/items/' + encodeURIComponent(key)), {
      method: 'DELETE',
    });
    _items = data.items || [];
    delete _results[key];
    delete _counts[key];
    renderItemList();
    if (_kind === key) switchKind('aesthetic');
    await refreshDirs();
    toast('已删除评分项');
  }

  // -----------------------------------------------------------------------
  // 页面初始化
  // -----------------------------------------------------------------------

  async function initPage() {
    await AppSettings.ready();
    var saved = AppSettings.get('scoring') || {};
    if (saved.sort) _sortKey = saved.sort;
    if (saved.desc === false) _sortDesc = false;
    document.getElementById('sortSelect').value = _sortKey;
    document.getElementById('sortDir').textContent = _sortDesc ? '\u2193' : '\u2191';

    await refreshDirs();
    if (saved.kind && isKnownKind(saved.kind)) _kind = saved.kind;
    renderTabs();
    loadResults(_kind);
    applyStatus({ status: 'idle', done: 0, total: 0, failed: 0 });
    resumeActive();
  }

  async function resumeActive() {
    var ds = API.dataset();
    var kinds = ['aesthetic', 'wd14', 'wd14_cache'];
    for (var i = 0; i < kinds.length; i++) {
      var active = await API.findActiveJob(kinds[i], ds);
      if (!active) continue;
      applyStatus({ status: active.status || 'running' });
      startWatch(active.job_id, function () {
        loadResults(_kind);
        refreshDirs().catch(function () {});
      });
      toast('\u5DF2\u6062\u590D\u4EFB\u52A1\u8FDB\u5EA6');
      return;
    }
  }

  async function startJob() {
    var dirs = currentDirs();
    if (!dirs.length) {
      toast('\u8BF7\u9009\u62E9\u76EE\u5F55');
      return;
    }
    var body = {
      kind: _kind === 'aesthetic' ? 'aesthetic' : 'wd14',
      dirs: dirs
    };
    var data = await API.request(API.apiPath('/scoring/start'), {
      method: 'POST',
      body: JSON.stringify(body)
    });
    applyStatus({ status: 'queued', message: '\u7B49\u5F85\u6267\u884C', done: 0, total: 0, failed: 0 });
    toast('\u5DF2\u63D0\u4EA4');
    startWatch(data.job_id, function (s) {
      loadResults(_kind);
      refreshDirs().catch(function () {});
      toast(s.message || '\u8BC4\u5206\u5B8C\u6210');
    });
  }

  async function startCache() {
    var dirs = currentDirs();
    if (!dirs.length) {
      toast('\u8BF7\u9009\u62E9\u76EE\u5F55');
      return;
    }
    var data = await API.request(API.apiPath('/scoring/wd14'), {
      method: 'POST',
      body: JSON.stringify({ dirs: dirs })
    });
    applyStatus({ status: 'queued', message: '\u7B49\u5F85\u6267\u884C', done: 0, total: 0, failed: 0 });
    toast('\u5DF2\u63D0\u4EA4 WD14 \u7F13\u5B58\u4EFB\u52A1');
    startWatch(data.job_id, function (s) {
      refreshDirs().catch(function () {});
      toast(s.message || 'WD14 \u7F13\u5B58\u5DF2\u5C31\u7EEA');
    });
  }

  async function cancelJob() {
    if (!_jobId) return;
    await API.cancelJob(_jobId);
    toast('\u6B63\u5728\u53D6\u6D88\u2026');
  }

  // -----------------------------------------------------------------------
  // 事件绑定
  // -----------------------------------------------------------------------

  document.getElementById('btn-start').addEventListener('click', function () {
    startJob().catch(function (e) { toast(String(e.message || e)); });
  });
  document.getElementById('btn-cache').addEventListener('click', function () {
    startCache().catch(function (e) { toast(String(e.message || e)); });
  });
  document.getElementById('btn-cancel').addEventListener('click', function () {
    cancelJob().catch(function (e) { toast(String(e.message || e)); });
  });

  /**
   * 将排序与筛选状态写入设置（「scoring」键）。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - scoring.js:switchKind
   *   - scoring.js:sortSelect change 监听
   *   - scoring.js:sortDir click 监听
   *   - scoring.js:scoreFilter change 监听
   */
  function persistScoring() {
    AppSettings.set('scoring', {
      kind: _kind,
      sort: _sortKey,
      desc: _sortDesc,
      filter: document.getElementById('scoreFilter').value,
    });
  }

  // 排序
  document.getElementById('sortSelect').addEventListener('change', function () {
    _sortKey = this.value;
    _sortDesc = true;
    document.getElementById('sortDir').textContent = '\u2193';
    persistScoring();
    renderGallery();
  });
  document.getElementById('sortDir').addEventListener('click', function () {
    _sortDesc = !_sortDesc;
    this.textContent = _sortDesc ? '\u2193' : '\u2191';
    persistScoring();
    renderGallery();
  });

  // 搜索/筛选
  document.getElementById('searchBox').addEventListener('input', function () {
    renderGallery();
  });
  document.getElementById('scoreFilter').addEventListener('change', function () {
    persistScoring();
    renderGallery();
  });

  // 评分项弹窗
  document.getElementById('btn-items').addEventListener('click', function () {
    renderItemList();
    UI.openModal('items-modal');
  });
  document.getElementById('btn-items-close').addEventListener('click', function () {
    UI.closeModal('items-modal');
  });
  UI.bindModal('items-modal');
  document.getElementById('btn-item-add').addEventListener('click', function () {
    createItem().catch(function (e) { toast(String(e.message || e)); });
  });
  document.getElementById('item-expr').addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    createItem().catch(function (err) { toast(String(err.message || err)); });
  });

  // 灯箱
  document.getElementById('lightbox').addEventListener('click', function (e) { closeLightbox(e); });
  document.querySelector('.lightbox-close').addEventListener('click', function () { closeLightbox(); });
  document.querySelector('.lightbox-prev').addEventListener('click', function (e) { e.stopPropagation(); navLightbox(-1); });
  document.querySelector('.lightbox-next').addEventListener('click', function (e) { e.stopPropagation(); navLightbox(1); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeLightbox();
    if (e.key === 'ArrowLeft' || e.key === 'a' || e.key === 'A') navLightbox(-1);
    if (e.key === 'ArrowRight' || e.key === 'd' || e.key === 'D') navLightbox(1);
  });

  // 暴露灯箱给 onclick
  window.ScoringView = { openLightbox: openLightbox };

  // -----------------------------------------------------------------------
  // 工具函数
  // -----------------------------------------------------------------------

  function _fileName(p) {
    var parts = p.split('/');
    return parts[parts.length - 1];
  }

  initPage().catch(function (e) { toast(String(e.message || e)); });
})();
