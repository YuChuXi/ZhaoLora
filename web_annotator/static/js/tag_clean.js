/**
 * tag_clean.js —— 标签清洗页（长任务 + SSE）
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
   * 提交清洗任务并等待结束事件。
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
    document.getElementById('btn-clean-preview').disabled = true;
    document.getElementById('btn-clean-apply').disabled = true;
    try {
      var submitted = await API.request(API.apiPath('/tags/clean'), {
        method: 'POST',
        body: JSON.stringify(body),
      });
      var data = await new Promise(function (resolve, reject) {
        API.watchJob(submitted.job_id, function (s) {
          if (s.status === 'queued') {
            document.getElementById('clean-summary').textContent = '等待执行…';
            return;
          }
          if (s.status === 'running') {
            document.getElementById('clean-summary').textContent = s.message || (dryRun ? '预览中…' : '写入中…');
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
      showCleanResult(data);
      toast((dryRun ? '预览完成' : '写入完成') + ' · 已变更 ' + data.changed);
    } finally {
      document.getElementById('btn-clean-preview').disabled = false;
      document.getElementById('btn-clean-apply').disabled = false;
    }
  }

  document.getElementById('btn-clean-preview').addEventListener('click', function () {
    runClean(true).catch(function (e) { toast(String(e.message || e)); });
  });
  document.getElementById('btn-clean-apply').addEventListener('click', function () {
    runClean(false).catch(function (e) { toast(String(e.message || e)); });
  });

  // ---- 批量编辑 ----

  /**
   * 提交批量编辑任务并等待结束事件。
   * @param {boolean} dryRun
   * @returns {Promise<void>}
   */
  async function runBatchEdit(dryRun) {
    var dirs = DirPicker.getDirs();
    if (!dirs.length) {
      toast('请选择目录');
      return;
    }
    var deleteTags = parseTagList(document.getElementById('batch-delete').value);
    var addTags = parseTagList(document.getElementById('batch-add').value);
    if (!deleteTags.length && !addTags.length) {
      toast('请指定要删除或添加的标签');
      return;
    }
    if (!dryRun) {
      var ok = confirm('将覆盖所选目录中的旁路 .txt，是否继续？');
      if (!ok) return;
    }
    var body = {
      dirs: dirs,
      delete_tags: deleteTags,
      add_tags: addTags,
      position: document.getElementById('batch-position').value,
      insert_mode: document.getElementById('batch-mode').value,
      conditional: document.getElementById('batch-conditional').checked,
      keep_duplicates: document.getElementById('batch-keep-dup').checked,
      inject_pony_score: document.getElementById('batch-inject-pony').checked,
      dry_run: !!dryRun,
      sample_limit: 60,
    };
    document.getElementById('clean-summary').textContent = dryRun ? '预览中…' : '写入中…';
    document.getElementById('btn-batch-preview').disabled = true;
    document.getElementById('btn-batch-apply').disabled = true;
    try {
      var submitted = await API.request(API.apiPath('/tags/batch-edit'), {
        method: 'POST',
        body: JSON.stringify(body),
      });
      var data = await new Promise(function (resolve, reject) {
        API.watchJob(submitted.job_id, function (s) {
          if (s.status === 'queued') {
            document.getElementById('clean-summary').textContent = '等待执行…';
            return;
          }
          if (s.status === 'running') {
            document.getElementById('clean-summary').textContent = s.message || (dryRun ? '预览中…' : '写入中…');
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
      showCleanResult(data);
      toast((dryRun ? '预览完成' : '写入完成') + ' · 已变更 ' + data.changed);
    } finally {
      document.getElementById('btn-batch-preview').disabled = false;
      document.getElementById('btn-batch-apply').disabled = false;
    }
  }

  document.getElementById('btn-batch-preview').addEventListener('click', function () {
    runBatchEdit(true).catch(function (e) { toast(String(e.message || e)); });
  });
  document.getElementById('btn-batch-apply').addEventListener('click', function () {
    runBatchEdit(false).catch(function (e) { toast(String(e.message || e)); });
  });
})();
