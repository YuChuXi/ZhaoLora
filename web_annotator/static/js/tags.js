/**
 * tags.js —— 标签频率统计
 */
(function () {
  'use strict';

  var _dirs = [];
  var _tags = [];
  var _selected = {}; // tag -> true
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
      list.innerHTML = '<div style="padding:8px;color:var(--text-dim);font-size:12px;">暂无标注目录</div>';
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
      meta.textContent = d.txt_count + ' txt';
      row.appendChild(cb);
      row.appendChild(name);
      row.appendChild(meta);
      list.appendChild(row);
    });
  }

  function selectedTags() {
    return _tags.filter(function (t) { return _selected[t.tag]; });
  }

  function updateExport() {
    var sel = selectedTags();
    document.getElementById('sel-count').textContent = '已选 ' + sel.length;
    document.getElementById('export-text').value = sel.map(function (t) { return t.tag; }).join(', ');
    var head = document.getElementById('chk-head');
    if (_tags.length && sel.length === _tags.length) {
      head.checked = true;
      head.indeterminate = false;
    } else if (sel.length === 0) {
      head.checked = false;
      head.indeterminate = false;
    } else {
      head.checked = false;
      head.indeterminate = true;
    }
  }

  function renderTable() {
    var body = document.getElementById('tag-body');
    body.innerHTML = '';
    if (!_tags.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty-hint">无结果</td></tr>';
      updateExport();
      return;
    }
    _tags.forEach(function (row) {
      var tr = document.createElement('tr');
      if (_selected[row.tag]) tr.className = 'selected';

      var tdCheck = document.createElement('td');
      tdCheck.className = 'col-check';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!_selected[row.tag];
      cb.addEventListener('click', function (e) { e.stopPropagation(); });
      cb.addEventListener('change', function () {
        if (cb.checked) _selected[row.tag] = true;
        else delete _selected[row.tag];
        tr.className = cb.checked ? 'selected' : '';
        updateExport();
      });
      tdCheck.appendChild(cb);

      var tdPct = document.createElement('td');
      tdPct.className = 'col-pct';
      tdPct.textContent = row.pct.toFixed(2) + '%';

      var tdCount = document.createElement('td');
      tdCount.className = 'col-count';
      tdCount.textContent = String(row.count);

      var tdEn = document.createElement('td');
      tdEn.className = 'col-en';
      tdEn.textContent = row.tag;

      var tdZh = document.createElement('td');
      tdZh.className = 'col-zh' + (row.zh ? '' : ' zh-miss');
      tdZh.textContent = row.zh || '—';

      tr.appendChild(tdCheck);
      tr.appendChild(tdPct);
      tr.appendChild(tdCount);
      tr.appendChild(tdEn);
      tr.appendChild(tdZh);
      tr.addEventListener('click', function (e) {
        if (e.target === cb) return;
        cb.checked = !cb.checked;
        if (cb.checked) _selected[row.tag] = true;
        else delete _selected[row.tag];
        tr.className = cb.checked ? 'selected' : '';
        updateExport();
      });
      body.appendChild(tr);
    });
    updateExport();
  }

  async function loadDirs() {
    var data = await api('/api/tags/dirs');
    _dirs = (data.dirs || []).map(function (d) {
      return {
        name: d.name,
        label: d.label || d.name,
        txt_count: d.txt_count || 0,
        checked: true,
      };
    });
    renderDirs();
  }

  async function refresh() {
    var dirs = selectedDirNames();
    if (!dirs.length) {
      toast('请选择目录');
      return;
    }
    document.getElementById('tag-body').innerHTML =
      '<tr><td colspan="5" class="empty-hint">统计中…</td></tr>';
    var body = {
      dirs: dirs,
      lower: document.getElementById('opt-lower').checked,
      underscores: document.getElementById('opt-underscores').checked,
      per_sample: document.getElementById('opt-per-sample').checked,
      min_count: Math.max(1, parseInt(document.getElementById('opt-min').value, 10) || 1),
    };
    var data = await api('/api/tags/stats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    _tags = data.tags || [];
    // 保留仍存在于新结果中的选中项
    var keep = {};
    _tags.forEach(function (t) {
      if (_selected[t.tag]) keep[t.tag] = true;
    });
    _selected = keep;
    document.getElementById('stat-summary').textContent =
      '样本 ' + data.sample_count + ' · 标签 ' + data.tag_count +
      ' · 目录 ' + (data.dirs || []).join(', ');
    renderTable();
    toast('完成：' + data.tag_count + ' 个标签');
  }

  function copyText(text) {
    if (!text) {
      toast('没有可复制内容');
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast('已复制 ' + text.split(',').filter(Boolean).length + ' 项');
      }, function () {
        fallbackCopy(text);
      });
      return;
    }
    fallbackCopy(text);
  }

  function fallbackCopy(text) {
    var ta = document.getElementById('export-text');
    ta.value = text;
    ta.focus();
    ta.select();
    document.execCommand('copy');
    toast('已复制');
  }

  function exportList(field) {
    var sel = selectedTags();
    if (!sel.length) {
      toast('请先勾选标签');
      return;
    }
    var parts = sel.map(function (t) {
      if (field === 'zh') return t.zh || t.tag;
      return t.tag;
    });
    var text = parts.join(', ');
    document.getElementById('export-text').value = text;
    copyText(text);
  }

  async function init() {
    document.getElementById('btn-refresh').addEventListener('click', function () {
      refresh().catch(function (e) { toast(String(e.message || e)); });
    });
    document.getElementById('btn-dirs-all').addEventListener('click', function () {
      _dirs.forEach(function (d) { d.checked = true; });
      renderDirs();
    });
    document.getElementById('btn-dirs-none').addEventListener('click', function () {
      _dirs.forEach(function (d) { d.checked = false; });
      renderDirs();
    });
    document.getElementById('btn-select-all-tags').addEventListener('click', function () {
      _tags.forEach(function (t) { _selected[t.tag] = true; });
      renderTable();
    });
    document.getElementById('btn-clear-tags').addEventListener('click', function () {
      _selected = {};
      renderTable();
    });
    document.getElementById('chk-head').addEventListener('change', function () {
      var on = document.getElementById('chk-head').checked;
      _selected = {};
      if (on) _tags.forEach(function (t) { _selected[t.tag] = true; });
      renderTable();
    });
    document.getElementById('btn-copy').addEventListener('click', function () {
      exportList('en');
    });
    document.getElementById('btn-export-en').addEventListener('click', function () {
      exportList('en');
    });
    document.getElementById('btn-export-zh').addEventListener('click', function () {
      exportList('zh');
    });
    document.getElementById('btn-copy-box').addEventListener('click', function () {
      copyText(document.getElementById('export-text').value);
    });

    await loadDirs();
    await refresh();
  }

  init().catch(function (e) {
    document.getElementById('tag-body').innerHTML =
      '<tr><td colspan="5" class="empty-hint">' + String(e.message || e) + '</td></tr>';
  });
})();
