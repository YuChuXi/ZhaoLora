/**
 * ready.js —— 导出训练 TOML；侧栏蒙版状态与目录，顶栏仅操作与 subset 数。
 *
 * 引用：templates/ready.html。
 */
(function () {
  'use strict';

  var _enabled = {};

  /**
   * 刷新侧栏工作目录表格：启用勾选、目录、张数、蒙版状态。引用：init、generate。
   * @param {object} data
   */
  function renderSide(data) {
    var tbody = document.getElementById('dir-tbody');
    tbody.innerHTML = '';
    var dirs = data.dirs || [];
    if (!dirs.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty">无含图目录</td></tr>';
      return;
    }
    dirs.forEach(function (d) {
      if (_enabled[d.name] === undefined) _enabled[d.name] = true;
    });
    var statsByDir = {};
    (data.mask_stats || []).forEach(function (m) { statsByDir[m.name] = m; });
    dirs.forEach(function (d) {
      var m = statsByDir[d.name];
      var row = document.createElement('tr');

      var enableCell = document.createElement('td');
      enableCell.className = 'col-enable';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!_enabled[d.name];
      cb.title = '是否导出该目录';
      cb.addEventListener('change', function () {
        _enabled[d.name] = cb.checked;
        generate(true);
      });
      enableCell.appendChild(cb);

      var nameCell = document.createElement('td');
      nameCell.textContent = d.name;

      var countCell = document.createElement('td');
      countCell.textContent = (d.image_count != null ? d.image_count : 0) + ' 张';

      var maskCell = document.createElement('td');
      maskCell.className = 'mask';
      if (m) {
        var dot = document.createElement('span');
        dot.className = 'status ' + (m.mask_status === '完整' ? 'ok' : 'none');
        maskCell.appendChild(dot);
        maskCell.title = '蒙版完成 / 图片总数';
        maskCell.appendChild(document.createTextNode(
          m.mask_complete + '/' + (m.mask_complete + m.mask_none)
        ));
      } else {
        maskCell.textContent = '-';
      }

      row.appendChild(enableCell);
      row.appendChild(nameCell);
      row.appendChild(countCell);
      row.appendChild(maskCell);
      tbody.appendChild(row);
    });
  }

  /**
   * 生成 TOML 预览并更新侧栏; 输入变化自动调用时静默(silent=true), 仅按钮手动生成时 toast.
   * @param {boolean} [silent]
   */
  async function generate(silent) {
    var target = parseInt(document.getElementById('target-images').value, 10);
    if (!(target >= 1)) {
      if (!silent) toast('目标图片数无效');
      return;
    }
    var resText = document.getElementById('resolutions').value.trim();
    var resolutions = [];
    if (resText) {
      var parts = resText.split(/\s*,\s*/);
      for (var i = 0; i < parts.length; i++) {
        var n = parseInt(parts[i], 10);
        if (!(n > 0)) {
          if (!silent) toast('无效分辨率: ' + parts[i]);
          return;
        }
        resolutions.push(n);
      }
    }
    var dirs = Object.keys(_enabled).filter(function (n) { return _enabled[n]; });
    var data;
    try {
      data = await API.request(API.apiPath('/ready/toml'), {
        method: 'POST',
        body: JSON.stringify({ dirs: dirs, target_images: target, resolutions: resolutions })
      });
    } catch (e) {
      document.getElementById('stat-summary').textContent = String(e.message || e);
      if (!silent) toast(String(e.message || e));
      return;
    }
    document.getElementById('toml-box').value = data.toml || '';
    var subsets = data.subsets || [];
    var totalImages = subsets.reduce(function (sum, s) {
      return sum + (s.image_count || 0);
    }, 0);
    var repeatsList = data.repeats || [];
    var sumRepeats = repeatsList.reduce(function (a, b) { return a + b; }, 0);
    var repeated = totalImages * sumRepeats;
    var repStr = repeatsList.length > 1 ? '(' + repeatsList.join('+') + ')' : (repeatsList[0] || 0);
    document.getElementById('stat-summary').textContent =
      subsets.length + ' 个 subset · ' + totalImages + ' 张 × ' + repStr + ' = ' + repeated + ' 张';
    renderSide(data);
    if (!silent) toast('已生成');
  }

  var _autoTimer = null;

  /**
   * 输入变化触发自动生成(防抖 300ms, 静默).
   */
  function autoGenerate() {
    clearTimeout(_autoTimer);
    _autoTimer = setTimeout(function () { generate(true); }, 300);
  }

  async function copyText() {
    var tb = document.getElementById('toml-box');
    var text = tb.value;
    if (!text) {
      toast('无可复制内容');
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      toast('已复制');
      return;
    }
    // 非安全上下文(局域网 http 访问)下 clipboard API 不可用: 选中文本供手动复制
    tb.select();
    tb.setSelectionRange(0, tb.value.length);
    toast('剪贴板不可用，已选中文本，请按 Ctrl+C');
  }

  document.getElementById('btn-gen').addEventListener('click', function () {
    generate(false);
  });
  document.getElementById('target-images').addEventListener('input', autoGenerate);
  document.getElementById('resolutions').addEventListener('input', autoGenerate);
  document.getElementById('btn-copy').addEventListener('click', function () {
    copyText().catch(function (e) { toast(String(e.message || e)); });
  });

  async function doExport(overwrite) {
    var target = parseInt(document.getElementById('target-images').value, 10);
    var resText = document.getElementById('resolutions').value.trim();
    var resolutions = [];
    if (resText) {
      resText.split(/\s*,\s*/).forEach(function (n) {
        var v = parseInt(n, 10);
        if (v > 0) resolutions.push(v);
      });
    }
    var dirs = Object.keys(_enabled).filter(function (n) { return _enabled[n]; });
    var url = API.apiPath('/ready/export');
    if (overwrite) url += '?overwrite=1';
    var data = await API.request(url, {
      method: 'POST',
      body: JSON.stringify({ dirs: dirs, target_images: target, resolutions: resolutions })
    });
    toast('已导出: ' + data.files.join(', ') + ' → ' + data.out_dir);
  }

  /**
   * 按当前参数把数据集导出为整套训练配置(lora/dataset/prompt)到配置路径.
   * 目标目录已存在配置时(409)先提示确认覆盖.
   * 引用: tpl btn-export.
   */
  async function exportConfig() {
    try {
      await doExport(false);
    } catch (e) {
      var status = parseInt(String(e.message), 10);
      if (status === 409) {
        if (window.confirm('目标目录已存在配置, 是否覆盖写入?')) {
          await doExport(true);
          return;
        }
      }
      throw e;
    }
  }

  document.getElementById('btn-export').addEventListener('click', function () {
    exportConfig().catch(function (e) { toast(String((e && e.message) || e)); });
  });

  API.request(API.apiPath('/ready/dirs'))
    .then(function (data) {
      renderSide(data);
      return generate();
    })
    .catch(function (e) { toast(String(e.message || e)); });
})();
