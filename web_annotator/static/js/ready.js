/**
 * ready.js —— 导出训练 TOML；侧栏蒙版状态与目录，顶栏仅操作与 subset 数。
 *
 * 引用：templates/ready.html。
 */
(function () {
  'use strict';

  var _enabled = {};
  var _resolutions = {};
  var _sideKey = null;

  /**
   * 刷新侧栏工作目录列表：每目录一个条目, 首行为启用勾选/目录/张数·蒙版, 次行为全宽分辨率输入。
   * 目录集合与蒙版统计均未变化时跳过重建, 保留分辨率输入的焦点与未提交编辑。引用：init、generate。
   * @param {object} data
   */
  function renderSide(data) {
    var list = document.getElementById('dir-list');
    var dirs = data.dirs || [];
    var key = JSON.stringify([
      dirs.map(function (d) { return [d.name, d.image_count]; }),
      data.mask_stats || []
    ]);
    if (key === _sideKey) return;
    _sideKey = key;
    list.innerHTML = '';
    if (!dirs.length) {
      list.innerHTML = '<div class="dir-empty">无含图目录</div>';
      return;
    }
    dirs.forEach(function (d) {
      if (_enabled[d.name] === undefined) _enabled[d.name] = true;
      if (_resolutions[d.name] === undefined) _resolutions[d.name] = '1536,1024,512';
    });
    var statsByDir = {};
    (data.mask_stats || []).forEach(function (m) { statsByDir[m.name] = m; });
    dirs.forEach(function (d) {
      var m = statsByDir[d.name];
      var item = document.createElement('div');
      item.className = 'dir-item';

      var row = document.createElement('div');
      row.className = 'dir-row';

      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!_enabled[d.name];
      cb.title = '是否导出该目录';
      cb.addEventListener('change', function () {
        _enabled[d.name] = cb.checked;
        generate(true);
      });
      row.appendChild(cb);

      var name = document.createElement('span');
      name.className = 'dir-name';
      name.textContent = d.name;
      name.title = d.name;
      row.appendChild(name);

      var meta = document.createElement('span');
      meta.className = 'dir-meta';
      meta.title = '图片 | 标注 | 蒙版 (标注与蒙版仅统计有对应图片的)';
      if (m) {
        var dot = document.createElement('span');
        dot.className = 'status ' + (m.ready ? 'ok' : 'none');
        dot.title = m.ready ? '就绪: 标注齐全, 蒙版齐全或全无' : '未就绪: 标注未齐全或蒙版部分存在';
        meta.appendChild(dot);
        meta.appendChild(document.createTextNode(
          (d.image_count != null ? d.image_count : 0) + ' | ' + (m.text_count || 0) + ' | ' + m.mask_complete
        ));
      } else {
        meta.textContent = (d.image_count != null ? d.image_count : 0) + ' | 0 | 0';
      }
      row.appendChild(meta);

      var resInput = document.createElement('input');
      resInput.type = 'text';
      resInput.className = 'res-input';
      resInput.value = _resolutions[d.name];
      resInput.setAttribute('data-name', d.name);
      resInput.title = '逗号分隔的多个分辨率, 每个分辨率生成一个 [[datasets]] 块; 留空则该子集生成单块无 resolution';
      resInput.addEventListener('input', function () {
        _resolutions[d.name] = resInput.value;
        autoGenerate();
      });

      item.appendChild(row);
      item.appendChild(resInput);
      list.appendChild(item);
    });
  }

  /**
   * 读取顶栏与侧栏参数构造请求体; 参数无效时提示并返回 null。引用：generate、doExport。
   * @param {boolean} silent
   * @returns {object|null} 含 dirs / target_images / subset_resolutions / use_reg 的请求体。
   */
  function collectBody(silent) {
    var target = parseInt(document.getElementById('target-images').value, 10);
    if (!(target >= 1)) {
      if (!silent) toast('目标图片数无效');
      return null;
    }
    var subsetRes = {};
    var inputs = document.querySelectorAll('#dir-list .res-input');
    for (var i = 0; i < inputs.length; i++) {
      var input = inputs[i];
      var name = input.getAttribute('data-name');
      var text = input.value.trim();
      var list = [];
      if (text) {
        var parts = text.split(/\s*,\s*/);
        for (var j = 0; j < parts.length; j++) {
          var n = parseInt(parts[j], 10);
          if (!(n > 0)) {
            if (!silent) toast('目录 ' + name + ' 无效分辨率: ' + parts[j]);
            return null;
          }
          list.push(n);
        }
      }
      subsetRes[name] = list;
    }
    return {
      dirs: Object.keys(_enabled).filter(function (n) { return _enabled[n]; }),
      target_images: target,
      subset_resolutions: subsetRes,
      use_reg: document.getElementById('use-reg').checked
    };
  }

  /**
   * 生成 TOML 预览并更新侧栏; 输入变化自动调用时静默(silent=true), 仅按钮手动生成时 toast.
   * @param {boolean} [silent]
   */
  async function generate(silent) {
    var body = collectBody(silent);
    if (!body) return;
    var data;
    try {
      data = await API.request(API.apiPath('/ready/toml'), {
        method: 'POST',
        body: JSON.stringify(body)
      });
    } catch (e) {
      document.getElementById('stat-summary').textContent = String(e.message || e);
      if (!silent) toast(String(e.message || e));
      return;
    }
    document.getElementById('toml-box').value = data.toml || '';
    var subsets = data.subsets || [];
    var repeats = data.subset_repeats || {};
    var totalImages = subsets.reduce(function (sum, s) {
      return sum + (s.image_count || 0);
    }, 0);
    var repeated = subsets.reduce(function (sum, s) {
      var rs = repeats[s.name] || [];
      return sum + (s.image_count || 0) * rs.reduce(function (a, b) { return a + b; }, 0);
    }, 0);
    var repStr;
    if (!subsets.length) {
      repStr = '0';
    } else {
      var first = (repeats[subsets[0].name] || []).join('+');
      var allSame = subsets.every(function (s) {
        return (repeats[s.name] || []).join('+') === first;
      });
      repStr = allSame ? first : subsets.map(function (s) {
        return s.name + '×' + (repeats[s.name] || []).join('+');
      }).join(', ');
    }
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
  document.getElementById('btn-copy').addEventListener('click', function () {
    copyText().catch(function (e) { toast(String(e.message || e)); });
  });

  async function doExport(overwrite) {
    var body = collectBody(false);
    if (!body) return;
    var url = API.apiPath('/ready/export');
    if (overwrite) url += '?overwrite=1';
    var data = await API.request(url, {
      method: 'POST',
      body: JSON.stringify(body)
    });
    toast('已导出: ' + data.files.join(', '));
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
