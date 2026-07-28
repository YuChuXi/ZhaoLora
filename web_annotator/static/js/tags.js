/**
 * tags.js —— 标签频率统计
 */
(function () {
  'use strict';

  var _tags = [];
  var _selected = {};
  var _toastTimer = null;

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
   * @param {string} url
   * @param {RequestInit} [opts]
   * @returns {Promise<object>}
   */
  async function api(url, opts) {
    var res = await fetch(url, opts);
    if (!res.ok) {
      var t = await res.text();
      throw new Error(t || res.statusText);
    }
    return res.json();
  }

  /**
   * @returns {Array<object>}
   */
  function selectedTags() {
    return _tags.filter(function (t) { return _selected[t.tag]; });
  }

  /**
   * 同步导出框与表头勾选态。
   */
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

  /**
   * 渲染统计表。
   */
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

  /**
   * 拉取当前目录标签频率。
   * @returns {Promise<void>}
   */
  async function refresh() {
    var dirs = DirPicker.getDirs();
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
    var data = await api(API.apiPath('/tags/stats'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    _tags = data.tags || [];
    var keep = {};
    _tags.forEach(function (t) {
      if (_selected[t.tag]) keep[t.tag] = true;
    });
    _selected = keep;
    document.getElementById('stat-summary').textContent =
      '样本 ' + data.sample_count + ' · 标签 ' + data.tag_count;
    renderTable();
  }

  /**
   * @param {string} text
   */
  function copyText(text) {
    if (!text) {
      toast('无可复制内容');
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

  /**
   * @param {string} text
   */
  function fallbackCopy(text) {
    var ta = document.getElementById('export-text');
    ta.value = text;
    ta.focus();
    ta.select();
    document.execCommand('copy');
    toast('已复制');
  }

  /**
   * @param {'en'|'zh'} field
   */
  function exportList(field) {
    var sel = selectedTags();
    if (!sel.length) {
      toast('请先勾选');
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

  /**
   * 翻译当前表格中缺失中文的标签。
   */
  async function translateMissing() {
    var missing = _tags.filter(function (t) { return !t.zh; }).map(function (t) { return t.tag; });
    if (!missing.length) {
      toast('没有需要翻译的标签');
      return;
    }

    var btn = document.getElementById('btn-translate-missing');
    btn.disabled = true;
    btn.textContent = '翻译中…';
    try {
      var res = await api(API.apiPath('/tags/translate-missing'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tags: missing }),
      });
      toast('已翻译 ' + res.translated + ' 个标签');
      await refresh();
    } catch (e) {
      toast(String(e.message || e));
    } finally {
      btn.disabled = false;
      btn.textContent = '翻译缺失';
    }
  }

  /**
   * 绑定页面事件。
   */
  function init() {
    document.getElementById('btn-refresh').addEventListener('click', function () {
      refresh().catch(function (e) { toast(String(e.message || e)); });
    });
    document.getElementById('btn-translate-missing').addEventListener('click', function () {
      translateMissing();
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

    DirPicker.onChange(function () {
      refresh().catch(function (e) { toast(String(e.message || e)); });
    });

    // ---- 批量添加对话框 ----
    document.getElementById('btn-batch-add').addEventListener('click', function () {
      var sel = selectedTags();
      document.getElementById('batch-add-tags').value = sel.map(function (t) { return t.tag; }).join(', ');
      document.getElementById('batch-add-result').textContent = '';
      document.getElementById('batch-add-modal').classList.add('open');
    });
    document.getElementById('btn-batch-add-close').addEventListener('click', function () {
      document.getElementById('batch-add-modal').classList.remove('open');
    });
    document.getElementById('btn-batch-add-preview').addEventListener('click', function () {
      runBatchEdit('add', true).catch(function (e) { toast(String(e.message || e)); });
    });
    document.getElementById('btn-batch-add-apply').addEventListener('click', function () {
      runBatchEdit('add', false).catch(function (e) { toast(String(e.message || e)); });
    });

    // ---- 批量删除对话框 ----
    document.getElementById('btn-batch-delete').addEventListener('click', function () {
      var sel = selectedTags();
      document.getElementById('batch-delete-tags').value = sel.map(function (t) { return t.tag; }).join(', ');
      document.getElementById('batch-delete-result').textContent = '';
      document.getElementById('batch-delete-modal').classList.add('open');
    });
    document.getElementById('btn-batch-delete-close').addEventListener('click', function () {
      document.getElementById('batch-delete-modal').classList.remove('open');
    });
    document.getElementById('btn-batch-delete-preview').addEventListener('click', function () {
      runBatchEdit('delete', true).catch(function (e) { toast(String(e.message || e)); });
    });
    document.getElementById('btn-batch-delete-apply').addEventListener('click', function () {
      runBatchEdit('delete', false).catch(function (e) { toast(String(e.message || e)); });
    });

    setTimeout(function () {
      refresh().catch(function (e) {
        document.getElementById('tag-body').innerHTML =
          '<tr><td colspan="5" class="empty-hint">' + String(e.message || e) + '</td></tr>';
      });
    }, 80);
  }

  /**
   * 提交批量编辑任务。
   * @param {'add'|'delete'} mode
   * @param {boolean} dryRun
   */
  async function runBatchEdit(mode, dryRun) {
    var dirs = DirPicker.getDirs();
    if (!dirs.length) { toast('请选择目录'); return; }
    var resultEl, tagsInput;
    if (mode === 'add') {
      resultEl = document.getElementById('batch-add-result');
      tagsInput = document.getElementById('batch-add-tags');
    } else {
      resultEl = document.getElementById('batch-delete-result');
      tagsInput = document.getElementById('batch-delete-tags');
    }
    var tags = parseTagList(tagsInput.value);
    if (!tags.length) { toast('请指定标签'); return; }
    if (!dryRun) {
      if (!confirm('将覆盖所选目录中的旁路 .txt，是否继续？')) return;
    }
    var body = {
      dirs: dirs,
      delete_tags: mode === 'delete' ? tags : [],
      add_tags: mode === 'add' ? tags : [],
      position: mode === 'add' ? document.getElementById('batch-add-position').value : 'after',
      insert_mode: mode === 'add' ? document.getElementById('batch-add-mode').value : 'bottom',
      conditional: mode === 'add' ? document.getElementById('batch-add-conditional').checked : false,
      keep_duplicates: mode === 'add' ? document.getElementById('batch-add-keep-dup').checked : false,
      inject_pony_score: mode === 'add' ? document.getElementById('batch-add-inject-pony').checked : false,
      dry_run: !!dryRun,
      sample_limit: 40,
    };
    resultEl.textContent = dryRun ? '预览中…' : '写入中…';
    document.getElementById(mode === 'add' ? 'btn-batch-add-preview' : 'btn-batch-delete-preview').disabled = true;
    document.getElementById(mode === 'add' ? 'btn-batch-add-apply' : 'btn-batch-delete-apply').disabled = true;
    try {
      var submitted = await API.request(API.apiPath('/tags/batch-edit'), {
        method: 'POST', body: JSON.stringify(body),
      });
      var data = await new Promise(function (resolve, reject) {
        API.watchJob(submitted.job_id, function (s) {
          if (s.status === 'queued') { resultEl.textContent = '等待执行…'; return; }
          if (s.status === 'running') { resultEl.textContent = s.message || (dryRun ? '预览中…' : '写入中…'); return; }
          if (s.status === 'done') { resolve(s.result || {}); return; }
          if (s.status === 'error' || s.status === 'cancelled') { reject(new Error(s.message || s.status)); }
        });
      });
      var lines = [];
      lines.push((dryRun ? '预览完成' : '写入完成') + ' · 已变更 ' + data.changed + ' · 未变更 ' + data.unchanged);
      (data.samples || []).forEach(function (s) {
        lines.push('--- ' + (s.name || s.path));
        lines.push('- ' + s.before);
        lines.push('+ ' + s.after);
      });
      resultEl.textContent = lines.join('\n');
      toast((dryRun ? '预览' : '写入') + '完成 · 已变更 ' + data.changed);
      if (!dryRun) { await refresh(); }
    } finally {
      document.getElementById(mode === 'add' ? 'btn-batch-add-preview' : 'btn-batch-delete-preview').disabled = false;
      document.getElementById(mode === 'add' ? 'btn-batch-add-apply' : 'btn-batch-delete-apply').disabled = false;
    }
  }

  /**
   * @param {string} raw
   * @returns {string[]}
   */
  function parseTagList(raw) {
    return String(raw || '').split(/[\n,]+/).map(function (s) { return s.trim(); }).filter(Boolean);
  }

  init();
})();
