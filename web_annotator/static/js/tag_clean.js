/**
 * tag_clean.js —— 标签清洗页
 */
(function () {
  'use strict';

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
   * 解析逗号/换行分隔的 tag 输入。
   * @param {string} raw
   * @returns {string[]}
   */
  function parseTagList(raw) {
    return String(raw || '')
      .split(/[\n,]+/)
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
  }

  /**
   * 展示清洗结果摘要与样本对比。
   * @param {object} data
   */
  function showCleanResult(data) {
    var mode = data.dry_run ? '预览' : '已写入';
    var ponyBit = '';
    if (data.inject_pony_score) {
      ponyBit = ' · 美学档位 ' + (data.pony_injected || 0) +
        (data.pony_missing ? '（无评分 ' + data.pony_missing + '）' : '');
    }
    document.getElementById('clean-summary').textContent =
      mode + ' · 已变更 ' + data.changed + ' · 未变更 ' + data.unchanged +
      ' · 共 ' + data.file_count + ' 个 .txt' + ponyBit;
    var lines = [];
    (data.samples || []).forEach(function (s) {
      lines.push('## ' + (s.name || s.path));
      lines.push('- ' + s.before);
      lines.push('+ ' + s.after);
      lines.push('');
    });
    document.getElementById('clean-result').textContent = lines.join('\n');
  }

  /**
   * 调用清洗 API。
   * @param {boolean} dryRun
   * @returns {Promise<void>}
   */
  async function runClean(dryRun) {
    var dirs = DirPicker.getDirs();
    if (!dirs.length) {
      toast('请选择目录');
      return;
    }
    if (!dryRun) {
      var ok = confirm('将覆盖所选目录中的旁路 .txt，是否继续？');
      if (!ok) return;
    }
    var body = {
      dirs: dirs,
      drop: parseTagList(document.getElementById('clean-drop').value),
      drop_contains: parseTagList(document.getElementById('clean-drop-contains').value),
      promote: parseTagList(document.getElementById('clean-promote').value),
      prepend: parseTagList(document.getElementById('clean-prepend').value),
      promote_separator: document.getElementById('clean-separator').checked,
      inject_pony_score: document.getElementById('clean-inject-pony').checked,
      dry_run: !!dryRun,
      sample_limit: 60,
    };
    document.getElementById('clean-summary').textContent = dryRun ? '预览中…' : '写入中…';
    var data = await api('/api/tags/clean', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    showCleanResult(data);
    toast((dryRun ? '预览完成' : '写入完成') + ' · 已变更 ' + data.changed);
  }

  document.getElementById('btn-clean-preview').addEventListener('click', function () {
    runClean(true).catch(function (e) { toast(String(e.message || e)); });
  });
  document.getElementById('btn-clean-apply').addEventListener('click', function () {
    runClean(false).catch(function (e) { toast(String(e.message || e)); });
  });
})();
