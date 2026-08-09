/**
 * scoring.js —— 统一评分页（美学 + 质量），前端 JSON 渲染画廊。
 *
 * 引用：templates/scoring.html。
 */
(function () {
  'use strict';

  var _watch = null;
  var _jobId = null;
  var _currentType = 'aesthetic';
  var _results = { aesthetic: null, quality: null };
  var _sortKey = 'score';
  var _sortDesc = true;

  var AES_COLOURS = [
    '#c0392b', '#e74c3c', '#e67e22', '#f39c12', '#d4ac0d',
    '#27ae60', '#1e8449', '#148f77', '#0e6655', '#0a4f42',
  ];

  var QUAL_COLOURS = {
    'masterpiece': '#2ecc71',
    'best quality': '#27ae60',
    'good quality': '#1e8449',
    'normal quality': '#f39c12',
    'low quality': '#e67e22',
    'worst quality': '#e74c3c',
  };

  // -----------------------------------------------------------------------
  // 工具栏
  // -----------------------------------------------------------------------

  function setRunningUi(running) {
    document.getElementById('btn-start').disabled = running;
    document.getElementById('btn-cancel').disabled = !running;
    DirPicker.setDisabled(running);
  }

  function currentDirs() {
    return DirPicker.getDirs();
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

  function startWatch(jobId) {
    stopWatch();
    _jobId = jobId;
    _watch = API.watchJob(jobId, function (s) {
      applyStatus(s);
      if (s.status === 'done') {
        loadResults(_currentType);
        toast((_currentType === 'quality' ? '\u8D28\u91CF' : '\u7F8E\u5B66') + '\u8BC4\u5206\u5B8C\u6210');
      }
      if (s.status === 'cancelled') toast('\u5DF2\u53D6\u6D88');
      if (s.status === 'error') toast('\u8BC4\u5206\u5931\u8D25');
    });
  }

  // -----------------------------------------------------------------------
  // 结果加载与渲染
  // -----------------------------------------------------------------------

  function loadResults(type) {
    API.request(API.apiPath('/scoring/results?kind=' + type)).then(function (data) {
      _results[type] = data.results || [];
      if (type === _currentType) {
        renderGallery();
      }
      updateResultBadge(type, data.total || 0);
    }).catch(function (e) {
      if (type === _currentType) {
        document.getElementById('gallery').innerHTML =
          '<div class="gallery-empty">\u6682\u65E0\u8BC4\u5206\u6570\u636E</div>';
      }
    });
  }

  function updateResultBadge(type, count) {
    var tab = document.querySelector('.result-tab[data-type="' + type + '"]');
    if (tab) {
      var badge = tab.querySelector('.tab-badge');
      if (badge) badge.textContent = count;
    }
  }

  function _scoreBadge(r) {
    if (_currentType === 'aesthetic') {
      var pony = r.pony || 0;
      return '<span class="card-badge" style="background:' + AES_COLOURS[pony] + '">score_' + pony + '</span>';
    } else {
      var tag = r.quality_tag || '';
      var color = QUAL_COLOURS[tag] || '#888';
      return '<span class="card-badge" style="background:' + color + '">' + tag + '</span>';
    }
  }

  function _scoreValue(r) {
    return r.score.toFixed(4);
  }

  function renderGallery() {
    var results = _results[_currentType] || [];
    var gallery = document.getElementById('gallery');
    var empty = document.getElementById('gallery-empty');
    var stats = document.getElementById('stats');

    var histSection = document.getElementById('hist-section');
    var searchBar = document.getElementById('search-bar');

    if (!results.length) {
      gallery.innerHTML = '';
      gallery.style.display = 'none';
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

    // 排序
    var sorted = results.slice();
    sorted.sort(function (a, b) {
      var va = _sortKey === 'path' ? a.path : a.score;
      var vb = _sortKey === 'path' ? b.path : b.score;
      if (va < vb) return _sortDesc ? 1 : -1;
      if (va > vb) return _sortDesc ? -1 : 1;
      return 0;
    });

    // 搜索过滤
    var query = (document.getElementById('searchBox').value || '').toLowerCase();
    var scoreFilter = document.getElementById('scoreFilter').value;

    var html = '';
    for (var i = 0; i < sorted.length; i++) {
      var r = sorted[i];
      var path = r.path.toLowerCase();

      if (query && path.indexOf(query) === -1) continue;

      if (_currentType === 'aesthetic' && scoreFilter !== 'all') {
        if (String(r.pony) !== scoreFilter) continue;
      }
      if (_currentType === 'quality' && scoreFilter !== 'all') {
        if (r.quality_tag !== scoreFilter) continue;
      }

      html += '<div class="card" onclick="ScoringView.openLightbox(\'' + _currentType + '\',' + i + ')">' +
        '<div class="card-img"><img src="' + _escapeHtml(r.thumb_url || r.image_url) +
        '" loading="lazy" onerror="this.src=\'' + _escapeHtml(r.image_url) + '\'"></div>' +
        _scoreBadge(r) +
        '<div class="card-score-val">' + _scoreValue(r) + '</div>' +
        '<div class="card-path" title="' + _escapeAttr(r.path) + '">' + _fileName(r.path) + '</div>' +
        '</div>';
    }

    gallery.innerHTML = html;
    document.getElementById('result-count').textContent = String(sorted.length);

    // 统计
    if (stats) {
      stats.style.display = '';
      renderStats(sorted);
    }

    // 直方图
    renderHist(sorted);
  }

  function renderStats(results) {
    var scores = results.map(function (r) { return r.score; });
    var n = scores.length;
    if (!n) return;
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

  function renderHist(results) {
    var histEl = document.getElementById('histogram');
    if (!histEl) return;

    if (_currentType === 'aesthetic') {
      var counts = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
      results.forEach(function (r) { counts[r.pony]++; });
      var maxC = Math.max.apply(null, counts) || 1;
      var html = '';
      for (var i = 0; i < 10; i++) {
        html += '<div class="hist-col">' +
          '<div class="hist-count">' + counts[i] + '</div>' +
          '<div class="hist-bar" style="height:' + Math.round(counts[i] / maxC * 120) + 'px;background:' + AES_COLOURS[i] + '"></div>' +
          '<div class="hist-label" style="color:' + AES_COLOURS[i] + '">score_' + i + '</div>' +
          '</div>';
      }
      histEl.innerHTML = html;

      // 更新筛选下拉
      var sel = document.getElementById('scoreFilter');
      sel.innerHTML = '<option value="all">\u5168\u90E8\u6863\u4F4D</option>';
      for (var j = 0; j < 10; j++) {
        sel.innerHTML += '<option value="' + j + '">score_' + j + ' (' + counts[j] + ')</option>';
      }
    } else {
      var tags = ['worst quality', 'low quality', 'normal quality', 'good quality', 'best quality', 'masterpiece'];
      var tagCounts = tags.map(function (t) {
        return results.filter(function (r) { return r.quality_tag === t; }).length;
      });
      var maxQ = Math.max.apply(null, tagCounts) || 1;
      var qColors = ['#e74c3c', '#e67e22', '#f39c12', '#1e8449', '#27ae60', '#2ecc71'];
      var html2 = '';
      for (var k = 0; k < tags.length; k++) {
        html2 += '<div class="hist-col">' +
          '<div class="hist-count">' + tagCounts[k] + '</div>' +
          '<div class="hist-bar" style="height:' + Math.round(tagCounts[k] / maxQ * 120) + 'px;background:' + qColors[k] + '"></div>' +
          '<div class="hist-label" style="color:' + qColors[k] + '">' + tags[k] + '</div>' +
          '</div>';
      }
      histEl.innerHTML = html2;

      var sel2 = document.getElementById('scoreFilter');
      sel2.innerHTML = '<option value="all">\u5168\u90E8\u7B49\u7EA7</option>';
      for (var m = 0; m < tags.length; m++) {
        sel2.innerHTML += '<option value="' + tags[m] + '">' + tags[m] + ' (' + tagCounts[m] + ')</option>';
      }
    }
  }

  // -----------------------------------------------------------------------
  // 灯箱
  // -----------------------------------------------------------------------

  function openLightbox(type, idx) {
    var results = _results[type] || [];
    if (!results.length) return;
    var lb = document.getElementById('lightbox');
    document.getElementById('lightboxImg').src = results[idx].image_url;
    document.getElementById('lightboxScore').textContent = results[idx].score.toFixed(4);
    var tag = type === 'aesthetic'
      ? ('score_' + results[idx].pony)
      : (results[idx].quality_tag || '');
    document.getElementById('lightboxTag').textContent = tag;
    document.getElementById('lightboxPath').textContent = results[idx].path;
    lb.classList.add('active');
    document.body.style.overflow = 'hidden';
    lb._idx = idx;
    lb._type = type;
  }

  function closeLightbox(e) {
    if (e && e.target !== document.getElementById('lightbox') && e.target !== document.querySelector('.lightbox-close')) return;
    document.getElementById('lightbox').classList.remove('active');
    document.body.style.overflow = '';
  }

  function navLightbox(d) {
    var lb = document.getElementById('lightbox');
    var results = _results[lb._type] || [];
    if (!results.length || lb._idx == null) return;
    lb._idx = (lb._idx + d + results.length) % results.length;
    openLightbox(lb._type, lb._idx);
  }

  // -----------------------------------------------------------------------
  // 页面初始化
  // -----------------------------------------------------------------------

  async function initPage() {
    var data = await API.request(API.apiPath('/scoring/dirs'));

    if (data.aesthetic_ready) {
      loadResults('aesthetic');
      updateResultBadge('aesthetic', data.aesthetic_count);
    }
    if (data.quality_ready) {
      loadResults('quality');
      updateResultBadge('quality', data.quality_count);
    }
    applyStatus({ status: 'idle', done: 0, total: 0, failed: 0 });
    resumeActive();
  }

  async function resumeActive() {
    var active = await API.request(API.apiPath('/scoring/active'));
    if (active && active.job_id) {
      _currentType = active.kind;
      applyStatus({ status: active.status ? active.status.status : 'running' });
      startWatch(active.job_id);
      toast('\u5DF2\u6062\u590D' + (active.kind === 'quality' ? '\u8D28\u91CF' : '\u7F8E\u5B66') + '\u8BC4\u5206\u8FDB\u5EA6');
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
      kind: _currentType,
      dirs: dirs,
      write_report: true
    };
    var data = await API.request(API.apiPath('/scoring/start'), {
      method: 'POST',
      body: JSON.stringify(body)
    });
    applyStatus({ status: 'queued', message: '\u7B49\u5F85\u6267\u884C', done: 0, total: 0, failed: 0 });
    toast('\u5DF2\u63D0\u4EA4');
    startWatch(data.job_id);
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
  document.getElementById('btn-cancel').addEventListener('click', function () {
    cancelJob().catch(function (e) { toast(String(e.message || e)); });
  });

  // 类型切换标签
  document.querySelectorAll('.result-tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      _currentType = tab.dataset.type;
      document.querySelectorAll('.result-tab').forEach(function (t) {
        t.classList.toggle('active', t.dataset.type === _currentType);
      });
      document.getElementById('searchBox').value = '';
      document.getElementById('scoreFilter').value = 'all';
      if (_results[_currentType]) {
        renderGallery();
      } else {
        loadResults(_currentType);
      }
    });
  });

  // 排序
  document.getElementById('sortSelect').addEventListener('change', function () {
    _sortKey = this.value;
    _sortDesc = true;
    renderGallery();
  });
  document.getElementById('sortDir').addEventListener('click', function () {
    _sortDesc = !_sortDesc;
    this.textContent = _sortDesc ? '\u2193' : '\u2191';
    renderGallery();
  });

  // 搜索/筛选
  document.getElementById('searchBox').addEventListener('input', function () {
    renderGallery();
  });
  document.getElementById('scoreFilter').addEventListener('change', function () {
    renderGallery();
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

  function _escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function _escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  initPage().catch(function (e) { toast(String(e.message || e)); });
})();
