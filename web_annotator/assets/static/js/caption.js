/**
 * caption.js —— 描述页: 调用 Ollama 视觉模型批量生成自然语言描述, 写入各工作目录的 captions.json。
 *
 * 依赖: api.js / dir_picker.js / nav.js / masonry.js
 */
(function () {
  'use strict';

  // 描述模式的后端取值 → 界面文案(取值集合由 /caption/dirs 返回)
  var MODE_LABELS = { coarse: '粗略', detailed: '详细' };

  var _items = [];
  var _jobId = null;
  var _templates = [];

  /**
   * 浏览器本地存储键(按数据集区分)。
   * @param {string} kind 'rules' | 'triggers' | 'inject'
   * @returns {string}
   */
  function storeKey(kind) {
    return 'zhao_caption_' + kind + ':' + API.dataset();
  }

  /**
   * 读取本地保存的数组。
   * @param {string} kind
   * @returns {Array<object>}
   */
  function loadSaved(kind) {
    var raw = localStorage.getItem(storeKey(kind));
    if (!raw) return [];
    var arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  }

  /**
   * 写入本地存储。
   * @param {string} kind
   * @param {Array<object>} arr
   */
  function saveSaved(kind, arr) {
    localStorage.setItem(storeKey(kind), JSON.stringify(arr));
  }

  /**
   * 读取本地保存的 WD14 注入设置(通用/角色标签各自的下限)。
   * @returns {{general?: boolean, general_threshold?: string,
   *            character?: boolean, character_threshold?: string}|null}
   */
  function loadInject() {
    var raw = localStorage.getItem(storeKey('inject'));
    if (!raw) return null;
    var obj = JSON.parse(raw);
    return obj && typeof obj === 'object' ? obj : null;
  }

  /** 保存 WD14 注入设置。 */
  function persistInject() {
    localStorage.setItem(storeKey('inject'), JSON.stringify({
      general: document.getElementById('inject-general').checked,
      general_threshold: document.getElementById('general-threshold').value,
      character: document.getElementById('inject-character').checked,
      character_threshold: document.getElementById('character-threshold').value,
    }));
  }

  /**
   * 点击注入开关时同步对应阈值输入框的可用状态。
   * @param {string} checkboxId
   * @param {string} inputId
   */
  function syncThresholdEnabled(checkboxId, inputId) {
    document.getElementById(inputId).disabled = !document.getElementById(checkboxId).checked;
  }

  /**
   * 读取一个标签类别的注入阈值。
   * @param {string} checkboxId
   * @param {string} inputId
   * @returns {number|null|boolean} 数=该类别下限; null=未勾选不注入; false=取值非法
   */
  function readThreshold(checkboxId, inputId) {
    if (!document.getElementById(checkboxId).checked) return null;
    var value = parseFloat(document.getElementById(inputId).value);
    return value >= 0 && value <= 1 ? value : false;
  }

  // ==================== 模态 ====================

  /**
   * 打开模态。
   * @param {string} id
   */
  function openDlg(id) {
    var dlg = document.getElementById(id);
    dlg.classList.add('open');
    dlg.setAttribute('aria-hidden', 'false');
  }

  /**
   * 关闭模态。
   * @param {string} id
   */
  function closeDlg(id) {
    var dlg = document.getElementById(id);
    dlg.classList.remove('open');
    dlg.setAttribute('aria-hidden', 'true');
  }

  // ==================== 描述设置 ====================

  /**
   * 追加一行预设提示词。
   * @param {{text?: string, pattern?: string, always?: boolean}} [rule]
   */
  function addRuleRow(rule) {
    rule = rule || {};
    var row = document.createElement('div');
    row.className = 'rule-row';

    var pattern = document.createElement('input');
    pattern.type = 'text';
    pattern.className = 'rule-pattern';
    pattern.placeholder = '匹配标签, 如 *tail*';
    pattern.title = '逗号分隔的标签模式, * 为通配; 留空则对所有图片生效';
    pattern.value = rule.pattern || '';

    var always = document.createElement('label');
    always.className = 'rule-always';
    always.title = '手动指定: 对该数据集全部图片生效';
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!rule.always;
    always.appendChild(cb);
    always.appendChild(document.createTextNode('手动指定'));

    var text = document.createElement('input');
    text.type = 'text';
    text.className = 'rule-text';
    text.placeholder = '提示词内容, 如 尾巴的动作要详细描述';
    text.value = rule.text || '';

    var del = document.createElement('button');
    del.className = 'btn danger';
    del.type = 'button';
    del.textContent = '✕';
    del.title = '删除该条';
    del.addEventListener('click', function () {
      row.remove();
      persistRules();
    });

    row.appendChild(pattern);
    row.appendChild(always);
    row.appendChild(text);
    row.appendChild(del);
    document.getElementById('rule-list').appendChild(row);
  }

  /**
   * 收集当前全部预设提示词(内容为空的行丢弃)。
   * @returns {Array<{text: string, pattern: string, always: boolean}>}
   */
  function collectRules() {
    var rows = document.querySelectorAll('#rule-list .rule-row');
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var text = rows[i].querySelector('.rule-text').value.trim();
      if (!text) continue;
      out.push({
        text: text,
        pattern: rows[i].querySelector('.rule-pattern').value.trim(),
        always: rows[i].querySelector('.rule-always input').checked,
      });
    }
    return out;
  }

  // ==================== 提示词模板 ====================

  /**
   * 返回当前选中的模板。
   * @returns {object|null}
   */
  function currentTemplate() {
    var label = document.getElementById('rule-template').value;
    for (var i = 0; i < _templates.length; i++) {
      if (_templates[i].label === label) return _templates[i];
    }
    return null;
  }

  /** 按当前模板渲染待填字段输入框(字段由模板自身声明), 并显示模板源码。 */
  function renderTemplateFields() {
    var box = document.getElementById('rule-template-fields');
    box.innerHTML = '';
    var tpl = currentTemplate();
    document.getElementById('rule-template-desc').textContent = tpl ? tpl.text : '';
    if (!tpl) return;
    tpl.fields.forEach(function (name) {
      var row = document.createElement('div');
      row.className = 'rule-row';
      var label = document.createElement('span');
      label.className = 'rule-label';
      label.textContent = name;
      var input = document.createElement('input');
      input.type = 'text';
      input.className = 'rule-text';
      input.dataset.field = name;
      input.placeholder = '填写' + name;
      input.value = '';
      row.appendChild(label);
      row.appendChild(input);
      box.appendChild(row);
    });
  }

  /**
   * 用字段取值渲染模板源码(Jinja 变量语法 {{ 变量名 }})。
   * @param {string} source
   * @param {Object<string, string>} values
   * @returns {string}
   */
  function renderTemplate(source, values) {
    return source.replace(/\{\{\s*([^}]+?)\s*\}\}/g, function (match, name) {
      return Object.prototype.hasOwnProperty.call(values, name) ? values[name] : match;
    });
  }

  /**
   * 按当前模板与字段取值生成一条提示词条目(触发方式由用户在条目上设置)。
   * @returns {{text: string}|null}
   */
  function buildFromTemplate() {
    var tpl = currentTemplate();
    if (!tpl) {
      toast('请选择模板');
      return null;
    }
    var values = {};
    var inputs = document.querySelectorAll('#rule-template-fields input');
    for (var i = 0; i < inputs.length; i++) {
      var name = inputs[i].dataset.field;
      var value = inputs[i].value.trim();
      if (!value) {
        toast('请填写「' + name + '」');
        return null;
      }
      values[name] = value;
    }
    return { text: renderTemplate(tpl.text, values) };
  }

  /**
   * 渲染模板下拉框并显示首个模板的字段。
   * @param {Array<object>} templates /caption/dirs 返回的模板列表
   */
  function renderTemplates(templates) {
    _templates = templates || [];
    var sel = document.getElementById('rule-template');
    sel.innerHTML = '';
    _templates.forEach(function (tpl) {
      var option = document.createElement('option');
      option.value = tpl.label;
      option.textContent = tpl.label;
      sel.appendChild(option);
    });
    renderTemplateFields();
  }

  /** 收集并保存当前预设提示词。 */
  function persistRules() {
    saveSaved('rules', collectRules());
  }

  /** 保存三处设置并关闭描述设置对话框。 */
  function closeSettings() {
    persistRules();
    persistTriggers();
    persistInject();
    closeDlg('rules-dlg');
  }

  /**
   * 追加一行触发词。
   * @param {{word?: string, zh?: string}} [item]
   */
  function addTriggerRow(item) {
    item = item || {};
    var row = document.createElement('div');
    row.className = 'rule-row';

    var word = document.createElement('input');
    word.type = 'text';
    word.className = 'rule-pattern';
    word.placeholder = '触发词, 如 spiked halo behind head';
    word.value = item.word || '';

    var zh = document.createElement('input');
    zh.type = 'text';
    zh.className = 'rule-text';
    zh.placeholder = '中文说明, 如 角色脑后的金属带刺圆环';
    zh.value = item.zh || '';

    var del = document.createElement('button');
    del.className = 'btn danger';
    del.type = 'button';
    del.textContent = '✕';
    del.title = '删除该条';
    del.addEventListener('click', function () {
      row.remove();
      persistTriggers();
    });

    row.appendChild(word);
    row.appendChild(zh);
    row.appendChild(del);
    document.getElementById('trigger-list').appendChild(row);
  }

  /**
   * 收集当前触发词表(触发词为空的行丢弃)。
   * @returns {Object<string, string>} 触发词 → 中文说明
   */
  function collectTriggers() {
    var rows = document.querySelectorAll('#trigger-list .rule-row');
    var out = {};
    for (var i = 0; i < rows.length; i++) {
      var word = rows[i].querySelector('.rule-pattern').value.trim();
      if (!word) continue;
      out[word] = rows[i].querySelector('.rule-text').value.trim();
    }
    return out;
  }

  /** 收集并保存当前触发词表。 */
  function persistTriggers() {
    saveSaved('triggers', collectTriggers());
  }

  /** 按本地存储恢复描述设置。 */
  function loadSettings() {
    loadSaved('rules').forEach(addRuleRow);
    loadSaved('triggers').forEach(function (t) {
      addTriggerRow({ word: t.word, zh: t.zh });
    });
  }

  // ==================== 瀑布流 ====================

  /**
   * 卡片装饰: 已有描述的卡片标绿并在悬停提示中展示描述全文。
   * @param {HTMLElement} card
   * @param {object} img
   */
  function decorateCard(card, img) {
    card.classList.remove('tag-ok');
    if (img.captions && img.captions.length) {
      card.classList.add('tag-ok');
      card.title = img.captions.join('\n\n');
    } else {
      card.title = '(未描述)';
    }
  }

  /**
   * 点击卡片: 打开描述查看模态。
   * @param {object} img
   */
  function onCardClick(img) {
    document.getElementById('view-title').textContent = img.path;
    var body = document.getElementById('view-body');
    body.innerHTML = '';
    if (!img.captions || !img.captions.length) {
      var empty = document.createElement('div');
      empty.className = 'dlg-section-title';
      empty.textContent = '该图片尚无描述';
      body.appendChild(empty);
    } else {
      img.captions.forEach(function (text, i) {
        var box = document.createElement('div');
        box.className = 'view-caption';
        var label = document.createElement('span');
        label.className = 'view-label';
        label.textContent = '描述 ' + (i + 1);
        box.appendChild(label);
        box.appendChild(document.createTextNode(text));
        body.appendChild(box);
      });
    }
    openDlg('view-dlg');
  }

  /** 重新拉取所选目录的图片与描述现状。 */
  async function refresh() {
    var res = await API.request(API.apiPath('/caption/list'), {
      method: 'POST',
      body: JSON.stringify({ dirs: DirPicker.getDirs() }),
    });
    _items = res.items || [];
    Masonry.render(_items);
    var total = res.total || 0;
    var described = res.described || 0;
    document.getElementById('stat-total').textContent = total;
    document.getElementById('stat-described').textContent = described;
    document.getElementById('stat-missing').textContent = total - described;
  }

  // ==================== 标注任务 ====================

  /**
   * 当前选中的描述模式(按界面顺序)。
   * @returns {string[]}
   */
  function selectedModes() {
    var out = [];
    Array.prototype.forEach.call(document.querySelectorAll('#mode-seg button'), function (b) {
      if (b.classList.contains('active')) out.push(b.dataset.mode);
    });
    return out;
  }

  /**
   * 更新工具栏进度条。
   * @param {object} s 任务快照
   */
  function updateProgress(s) {
    var running = s.status === 'running' || s.status === 'queued';
    document.getElementById('progress').hidden = !running;
    var fill = document.getElementById('progress-fill');
    fill.classList.toggle('indeterminate', s.status === 'queued');
    if (s.status === 'running') fill.style.width = Math.min(100, s.pct || 0) + '%';
    document.getElementById('progress-pct').textContent = s.status === 'queued'
      ? '…'
      : (Math.min(100, Math.round(s.pct || 0)) + '%');
    if (s.message) document.getElementById('stat-job').textContent = s.message;
  }

  /** 重置任务相关界面控件。 */
  function resetJobUi() {
    document.getElementById('progress').hidden = true;
    document.getElementById('btn-cancel').hidden = true;
    document.getElementById('btn-start').disabled = false;
  }

  /**
   * 任务终态处理: 提示结果并刷新列表。
   * @param {object} s 任务快照
   */
  function finishJob(s) {
    _jobId = null;
    resetJobUi();
    if (s.status === 'done') {
      toast(s.message || '标注完成');
    } else if (s.status === 'cancelled') {
      toast(s.message || '已取消');
    } else {
      toast('标注失败: ' + (s.message || ''));
    }
    refresh().catch(function (e) { toast(String(e.message || e)); });
  }

  /**
   * 订阅任务进度, 终态时收尾。
   * @param {string} jobId
   */
  function watch(jobId) {
    _jobId = jobId;
    document.getElementById('btn-cancel').hidden = false;
    API.watchJob(jobId, function (s) {
      updateProgress(s);
      if (s.status === 'done' || s.status === 'error' || s.status === 'cancelled') {
        finishJob(s);
      }
    });
  }

  /** 提交描述标注长任务并订阅进度。 */
  async function startJob() {
    var modes = selectedModes();
    if (!modes.length) {
      toast('请至少选择一种描述模式');
      return;
    }
    var rules = collectRules();
    for (var i = 0; i < rules.length; i++) {
      if (!rules[i].always && !rules[i].pattern) {
        toast('第 ' + (i + 1) + ' 条提示词需填写「匹配标签」或勾选「手动指定」');
        return;
      }
    }
    var generalThreshold = readThreshold('inject-general', 'general-threshold');
    var characterThreshold = readThreshold('inject-character', 'character-threshold');
    if (generalThreshold === false || characterThreshold === false) {
      toast('注入标签的置信度阈值需在 0~1 之间');
      return;
    }
    var body = {
      dirs: DirPicker.getDirs(),
      modes: modes,
      general_threshold: generalThreshold,
      character_threshold: characterThreshold,
      rules: rules,
      triggers: collectTriggers(),
      force: document.getElementById('force').checked,
    };
    document.getElementById('btn-start').disabled = true;
    document.getElementById('progress').hidden = false;
    document.getElementById('progress-fill').style.width = '0%';
    try {
      var res = await API.request(API.apiPath('/caption/start'), {
        method: 'POST',
        body: JSON.stringify(body),
      });
      closeSettings();
      watch(res.job_id);
    } catch (e) {
      resetJobUi();
      toast('提交失败: ' + String(e.message || e));
    }
  }

  /** 请求取消当前标注任务。 */
  async function cancelJob() {
    if (!_jobId) return;
    var btn = document.getElementById('btn-cancel');
    btn.disabled = true;
    try {
      await API.cancelJob(_jobId);
    } finally {
      btn.disabled = false;
    }
  }

  /**
   * 按后端返回的模式、注入默认值与提示词模板渲染对话框选项。
   * @param {object} data /caption/dirs 响应
   */
  function renderOptions(data) {
    var seg = document.getElementById('mode-seg');
    seg.innerHTML = '';
    (data.modes || []).forEach(function (m) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'active';
      b.dataset.mode = m;
      b.textContent = MODE_LABELS[m] || m;
      b.addEventListener('click', function () { b.classList.toggle('active'); });
      seg.appendChild(b);
    });
    var saved = loadInject() || {};
    document.getElementById('inject-general').checked = !!saved.general;
    document.getElementById('general-threshold').value =
      saved.general_threshold || data.default_wd14_threshold;
    document.getElementById('inject-character').checked = !!saved.character;
    document.getElementById('character-threshold').value =
      saved.character_threshold || data.default_wd14_threshold;
    syncThresholdEnabled('inject-general', 'general-threshold');
    syncThresholdEnabled('inject-character', 'character-threshold');
    renderTemplates(data.rule_templates);
  }

  function init() {
    Masonry.init('masonry', {
      getData: function () { return _items; },
      onClick: onCardClick,
      decorate: decorateCard,
    });

    loadSettings();

    document.getElementById('rule-add').addEventListener('click', function () { addRuleRow(); });
    document.getElementById('inject-general').addEventListener('change', function () {
      syncThresholdEnabled('inject-general', 'general-threshold');
    });
    document.getElementById('inject-character').addEventListener('change', function () {
      syncThresholdEnabled('inject-character', 'character-threshold');
    });
    document.getElementById('rule-template').addEventListener('change', renderTemplateFields);
    document.getElementById('rule-template-add').addEventListener('click', function () {
      var rule = buildFromTemplate();
      if (!rule) return;
      addRuleRow(rule);
      persistRules();
    });
    document.getElementById('trigger-add').addEventListener('click', function () { addTriggerRow(); });
    document.getElementById('btn-open').addEventListener('click', function () { openDlg('rules-dlg'); });
    document.getElementById('rules-close').addEventListener('click', closeSettings);
    document.getElementById('view-close').addEventListener('click', function () { closeDlg('view-dlg'); });
    document.getElementById('rules-dlg').addEventListener('click', function (e) {
      if (e.target === this) closeSettings();
    });
    document.getElementById('view-dlg').addEventListener('click', function (e) {
      if (e.target === this) closeDlg('view-dlg');
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      closeSettings();
      closeDlg('view-dlg');
    });

    document.getElementById('btn-start').addEventListener('click', function () {
      startJob().catch(function (e) { toast(String(e.message || e)); });
    });
    document.getElementById('btn-cancel').addEventListener('click', function () {
      cancelJob().catch(function (e) { toast(String(e.message || e)); });
    });

    DirPicker.onChange(function () {
      refresh().catch(function (e) { toast(String(e.message || e)); });
    });

    // 页面刷新后重连未结束的标注任务
    API.findActiveJob('caption', API.dataset()).then(function (s) {
      if (s) watch(s.job_id);
    });

    API.request(API.apiPath('/caption/dirs'))
      .then(function (data) {
        renderOptions(data);
        renderServiceStatus(data);
        return refresh();
      })
      .catch(function (e) { toast(String(e.message || e)); });
  }

  /**
   * 渲染描述服务状态(就绪说明或失败原因)。
   * @param {object} data /caption/dirs 响应
   */
  function renderServiceStatus(data) {
    document.getElementById('stat-model').textContent =
      (data.ready ? '' : '未就绪: ') + (data.info || '');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
