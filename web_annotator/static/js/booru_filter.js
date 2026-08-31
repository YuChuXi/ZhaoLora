/**
 * booru_filter.js —— 筛选页挂载 Gelbooru / Danbooru
 * 搜索 tag → 瀑布流缩略图；选中即下载到当前目录（g{id}.png / d{id}.png）
 */
var BooruFilter = (function () {
  'use strict';

  var STATE_KEY = 'zhao_booru_filter_v1';

  /** 读取输入控件值; 不存在时返回空串. 引用: _SOURCES.buildRequest. */
  function _val(id) {
    return (document.getElementById(id) || {}).value || '';
  }

  /** Gelbooru/Danbooru 共用: 从标签工具栏构建请求参数. 引用: _SOURCES. */
  function _booruRequest() {
    return { tags: _val('booru-tags').trim(), extra: _val('booru-extra').trim(), order: _val('booru-order') };
  }

  /**
   * 工具栏 schema: 每个字段声明控件形态, renderToolbar() 据此生成 DOM。
   * 类型: text / select / hidden / button / sep。
   * button 用 role 区分 search(搜索) / batch(批量) / extra(条件弹窗)。
   * 引用: renderToolbar / buildField / stateKeysFor。
   */
  var _BARS = {
    booru: {
      extra: 'booru',
      fields: [
        { type: 'text', id: 'booru-tags', placeholder: '标签，空格分隔' },
        { type: 'select', id: 'booru-order', label: '排序',
          options: [['', '默认'], ['score', '分数'], ['id', 'ID 新→旧'], ['id_asc', 'ID 旧→新']] },
        { type: 'button', role: 'extra', id: 'btn-booru-extra', label: '条件',
          title: '评级、分数、分辨率等', badge: true },
        { type: 'hidden', id: 'booru-extra' },
        { type: 'button', role: 'search', label: '搜索', cls: 'green' },
        { type: 'button', role: 'batch', label: '批量下载', title: '按当前搜索结果批量下载' },
        { type: 'button', role: 'meta', label: '元数据', title: '重生成 Caption / 刷新元数据' },
        { type: 'sep' },
      ],
    },
    bilibili: {
      extra: 'bilibili',
      fields: [
        { type: 'text', id: 'bilibili-uid', placeholder: 'B站 UID' },
        { type: 'button', role: 'extra', id: 'btn-bilibili-extra', label: '条件',
          title: '类型、关键词、时间等', badge: true },
        { type: 'hidden', id: 'bilibili-extra' },
        { type: 'button', role: 'search', label: '搜索', cls: 'green' },
        { type: 'button', role: 'batch', label: '批量下载', title: '按当前筛选批量下载' },
        { type: 'sep' },
      ],
    },
    x: {
      extra: null,
      fields: [
        { type: 'text', id: 'x-user', placeholder: 'X 用户名（不含 @）' },
        { type: 'button', role: 'search', label: '搜索', cls: 'green' },
        { type: 'button', role: 'batch', label: '批量下载', title: '按当前搜索结果批量下载' },
        { type: 'sep' },
      ],
    },
    pixiv: {
      extra: null,
      fields: [
        { type: 'text', id: 'pixiv-query', placeholder: '画师 ID 或 标签' },
        { type: 'select', id: 'pixiv-mode', label: '类型',
          options: [['user', '画师'], ['tag', '标签']] },
        { type: 'button', role: 'search', label: '搜索', cls: 'green' },
        { type: 'button', role: 'batch', label: '批量下载', title: '按当前搜索结果批量下载' },
        { type: 'sep' },
      ],
    },
  };

  /**
   * 条件面板注册表: 每个 bar 声明其"条件"弹窗的字段形态与序列化方式。
   * 字段类型: chips(评级单选) / range(成对上下限, date 为日期) / select /
   *           text / date。parse/build 负责 extra 文本与扁平字段对象互转,
   *           count/summarize 驱动"条件"按钮的标记与提示。
   * 引用: curExtra / renderExtraPanel / readExtraDlg / fillExtraDlg /
   *       clearExtraDlg / syncExtraBtn / syncCheatLink。
   */
  var _EXTRA_PANELS = {
    booru: {
      cheat: true,
      fields: [
        { type: 'chips', key: 'rating', label: '评级', multi: true,
          options: function () {
            // Gelbooru 仅 3 个评级(safe/questionable/explicit), Danbooru 4 个.
            if (mode() === 'gelbooru') {
              return [['s', '全年龄'], ['q', '可疑'], ['e', '露骨']];
            }
            return [['g', '全年龄'], ['s', '敏感'], ['q', '可疑'], ['e', '露骨']];
          } },
        { type: 'range', label: '分数', lo: 'scoreLo', hi: 'scoreHi' },
        { type: 'range', label: '宽度', lo: 'widthLo', hi: 'widthHi' },
        { type: 'range', label: '高度', lo: 'heightLo', hi: 'heightHi' },
        { type: 'range', label: 'ID', lo: 'idLo', hi: 'idHi' },
        { type: 'range', label: '日期', lo: 'dateLo', hi: 'dateHi', date: true, rowId: 'booru-extra-date-row' },
        { type: 'text', key: 'user', label: '用户', placeholder: '上传者' },
        { type: 'text', key: 'more', label: '附加', placeholder: '其它元标签，空格分隔' },
      ],
      parse: parseExtra,
      build: buildExtra,
      count: countExtra,
      summarize: summarizeExtra,
    },
    bilibili: {
      cheat: false,
      fields: [
        { type: 'chips', key: 'type', label: '类型', multi: true,
          options: [['image', '图片'], ['video', '视频'], ['forward', '转发']] },
        { type: 'text', key: 'keyword', label: '关键词', placeholder: '过滤正文关键词' },
        { type: 'range', label: '日期', lo: 'dateLo', hi: 'dateHi', date: true },
      ],
      parse: _biliParseExtra,
      build: _biliBuildExtra,
      count: _biliCountExtra,
      summarize: _biliSummarizeExtra,
    },
  };

  /** 解析 B 站条件串为扁平字段对象; 类型为数组. 引用: _EXTRA_PANELS.bilibili. */
  function _biliParseExtra(raw) {
    var out = { type: [], keyword: '', dateLo: '', dateHi: '' };
    String(raw || '').split(/\s+/).forEach(function (t) {
      var m = /^(type|keyword|after|before):(.+)$/i.exec(t);
      if (!m) return;
      var k = m[1].toLowerCase();
      if (k === 'after') out.dateLo = m[2];
      else if (k === 'before') out.dateHi = m[2];
      else if (k === 'type') out.type.push(m[2]);
      else out[k] = m[2];
    });
    return out;
  }

  /** 序列化 B 站扁平字段对象为条件串; 多类型各输出一个 type: token. 引用: _EXTRA_PANELS.bilibili. */
  function _biliBuildExtra(f) {
    var parts = [];
    (Array.isArray(f.type) ? f.type : (f.type ? [f.type] : [])).forEach(function (t) {
      if (t) parts.push('type:' + t);
    });
    if (f.keyword) parts.push('keyword:' + f.keyword);
    if (f.dateLo) parts.push('after:' + f.dateLo);
    if (f.dateHi) parts.push('before:' + f.dateHi);
    return parts.join(' ');
  }

  /** B 站条件计数(每类型计 1; 日期上下限分别计; 精确相等计 1). 引用: _EXTRA_PANELS.bilibili. */
  function _biliCountExtra(f) {
    var n = 0;
    var types = Array.isArray(f.type) ? f.type : (f.type ? [f.type] : []);
    n += types.length;
    if (f.keyword) n += 1;
    if (f.dateLo && f.dateHi && f.dateLo === f.dateHi) n += 1;
    else {
      if (f.dateLo) n += 1;
      if (f.dateHi) n += 1;
    }
    return n;
  }

  /** B 站条件可读摘要. 引用: _EXTRA_PANELS.bilibili. */
  function _biliSummarizeExtra(f) {
    var bits = [];
    var types = Array.isArray(f.type) ? f.type : (f.type ? [f.type] : []);
    if (types.length) bits.push('类型:' + types.join('/'));
    if (f.keyword) bits.push('关键词:' + f.keyword);
    if (f.dateLo && f.dateHi && f.dateLo === f.dateHi) bits.push('日期=' + f.dateLo);
    else {
      if (f.dateLo) bits.push('日期≥' + f.dateLo);
      if (f.dateHi) bits.push('日期≤' + f.dateHi);
    }
    return bits.join(' · ');
  }

  /**
   * 远程来源注册表: 每个来源声明工具栏 schema / 请求构建 / 媒体 URL /
   * 原站地址与预览渲染。主逻辑只需查本表并渲染 bar, 新增来源只需加一项
   * 配置与对应 _BARS 字段, 无需改动业务主干。
   * 引用: isRemote / cur / thumbUrl / sampleUrl / cardLabel / postPageUrl /
   *       showPreview / syncToolbar / saveUiState / applyForm / init。
   */
  var _SOURCES = {
    local: { remote: false },
    gelbooru: {
      remote: true,
      bar: 'booru',
      batchDialog: true,
      metaTools: true, // 对话框内显示「重生成 Caption / 刷新元数据」booru 专用按钮
      stemPrefix: 'g',
      nameLabel: 'Gelbooru',
      buildRequest: _booruRequest,
      thumb: function (post) {
        return API.apiPath('/online-sources/gelbooru-image')
          + '?kind=thumb&id=' + encodeURIComponent(post.id)
          + '&url=' + encodeURIComponent(post.preview_url || post.sample_url || '');
      },
      sample: function (post) {
        return API.apiPath('/online-sources/gelbooru-image')
          + '?kind=sample&id=' + encodeURIComponent(post.id)
          + '&url=' + encodeURIComponent(post.sample_url || post.file_url || post.preview_url || '');
      },
      pageUrl: function (post) {
        return 'https://gelbooru.com/index.php?page=post&s=view&id=' + post.id;
      },
    },
    danbooru: {
      remote: true,
      bar: 'booru',
      batchDialog: true,
      metaTools: true, // 对话框内显示「重生成 Caption / 刷新元数据」booru 专用按钮
      stemPrefix: 'd',
      nameLabel: 'Danbooru',
      buildRequest: _booruRequest,
      thumb: function (post) { return post.preview_url || post.sample_url || ''; },
      sample: function (post) { return post.sample_url || post.file_url || post.preview_url || ''; },
      pageUrl: function (post) { return 'https://danbooru.donmai.us/posts/' + post.id; },
    },
    bilibili: {
      remote: true,
      bar: 'bilibili',
      batchDialog: true,
      stemPrefix: 'bi',
      nameLabel: 'Bilibili',
      noJump: true, // 游标式分页: 仅能顺序翻页, 禁用跳页
      requireTags: true,
      hintText: '输入 B站 UID 搜索 · 左键写入目录 · 右键大图',
      buildRequest: function () {
        return { tags: _val('bilibili-uid').trim(), extra: _val('bilibili-extra').trim(), order: '' };
      },
      thumb: function (post) {
        return API.apiPath('/online-sources/bilibili-image') + '?url=' + encodeURIComponent(post.preview_url || post.sample_url || '');
      },
      sample: function (post) {
        return API.apiPath('/online-sources/bilibili-image') + '?url=' + encodeURIComponent(post.sample_url || post.file_url || post.preview_url || '');
      },
      pageUrl: function (post) {
        var i = post.id.lastIndexOf('_');
        return 'https://t.bilibili.com/' + (i > 0 ? post.id.slice(0, i) : post.id);
      },
      renderPreviewTags: function (el, post) {
        var para = document.createElement('div');
        para.className = 'bili-preview-text';
        para.textContent = post.tags || '(无正文)';
        el.appendChild(para);
      },
    },
    x: {
      remote: true,
      bar: 'x',
      batchDialog: true,
      stemPrefix: 'x',
      nameLabel: 'X',
      noJump: true, // 游标式分页: 仅能顺序翻页, 禁用跳页
      requireTags: true,
      hintText: '输入 X 用户名搜索 · 左键写入目录 · 右键大图',
      buildRequest: function () {
        return { tags: _val('x-user').trim(), extra: '', order: '' };
      },
      thumb: function (post) {
        return API.apiPath('/online-sources/x-image') + '?url=' + encodeURIComponent(post.preview_url || post.sample_url || '');
      },
      sample: function (post) {
        return API.apiPath('/online-sources/x-image') + '?url=' + encodeURIComponent(post.sample_url || post.file_url || post.preview_url || '');
      },
      pageUrl: function (post) {
        var parts = post.id.split('_');
        return 'https://x.com/' + parts[0] + '/status/' + parts[1];
      },
      renderPreviewTags: function (el, post) {
        var para = document.createElement('div');
        para.className = 'bili-preview-text';
        para.textContent = post.tags || '(无正文)';
        el.appendChild(para);
      },
    },
    pixiv: {
      remote: true,
      bar: 'pixiv',
      batchDialog: true,
      stemPrefix: 'p',
      nameLabel: 'Pixiv',
      requireTags: true,
      hintText: '输入画师 ID 或标签搜索 · 左键写入目录 · 右键大图',
      buildRequest: function () {
        return { tags: _val('pixiv-query').trim(), extra: 'mode:' + _val('pixiv-mode'), order: '' };
      },
      thumb: function (post) {
        return API.apiPath('/online-sources/pixiv-image') + '?url=' + encodeURIComponent(post.preview_url || post.sample_url || '');
      },
      sample: function (post) {
        return API.apiPath('/online-sources/pixiv-image') + '?url=' + encodeURIComponent(post.sample_url || post.file_url || post.preview_url || '');
      },
      pageUrl: function (post) {
        var i = post.id.indexOf('_');
        return 'https://www.pixiv.net/artworks/' + (i > 0 ? post.id.slice(0, i) : post.id);
      },
      renderPreviewTags: function (el, post) {
        var para = document.createElement('div');
        para.className = 'bili-preview-text';
        para.textContent = post.tags || '(无标签)';
        el.appendChild(para);
      },
    },
    video: {
      remote: true,
      bar: null,
      nameLabel: '视频',
      noJump: true,
      hintText: '本地 video 目录 · 点击进入抽帧',
      buildRequest: function () { return { tags: '', extra: '', order: '' }; },
      thumb: function (post) {
        return API.apiPath('/video/cover') + '?path=' + encodeURIComponent(post.path);
      },
      // 点击/右键均进入抽帧页
      onClick: function (post) {
        location.href = '/d/' + encodeURIComponent(API.dataset())
          + '/video_extract?path=' + encodeURIComponent(post.path);
      },
      label: function (post) {
        var dur = post.duration ? (Math.round(post.duration) + 's ') : '';
        return dur + post.name;
      },
    },
  };

  /** 当前来源定义. 引用: 各来源相关函数. */
  function cur() {
    return _SOURCES[mode()] || _SOURCES.local;
  }

  /** 当前来源是否禁用跳页(游标式分页, 仅能顺序翻页). 引用: startPageFromInput/applyForm/updatePageStatus. */
  function noJump() {
    return !!cur().noJump;
  }

  /** 当前来源的条件面板定义; 无则返回 null. 引用: 条件弹窗相关函数. */
  function curExtra() {
    var bar = cur().bar;
    return (bar && _EXTRA_PANELS[bar]) || null;
  }

  /**
   * 依据 schema 字段构建工具栏控件 DOM. 引用: renderToolbar.
   * @param {object} f 字段定义
   * @returns {HTMLElement|null}
   */
  function buildField(f) {
    if (f.type === 'sep') {
      var s = document.createElement('span');
      s.className = 'sep';
      return s;
    }
    if (f.type === 'hidden') {
      var h = document.createElement('input');
      h.type = 'hidden';
      h.id = f.id;
      h.value = f.value || '';
      return h;
    }
    if (f.type === 'text') {
      var i = document.createElement('input');
      i.type = 'text';
      i.className = 'toolbar-input';
      if (f.id) i.id = f.id;
      if (f.placeholder) i.placeholder = f.placeholder;
      i.spellcheck = false;
      return i;
    }
    if (f.type === 'select') {
      var lab = document.createElement('label');
      lab.style.cssText = 'font-size:12px;color:var(--text-dim);display:flex;align-items:center;gap:4px;';
      lab.textContent = f.label;
      var sel = document.createElement('select');
      sel.id = f.id;
      (f.options || []).forEach(function (o) {
        var op = document.createElement('option');
        op.value = o[0];
        op.textContent = o[1];
        sel.appendChild(op);
      });
      lab.appendChild(sel);
      return lab;
    }
    if (f.type === 'button') {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn' + (f.cls ? ' ' + f.cls : '');
      b.textContent = f.label;
      if (f.id) b.id = f.id;
      if (f.title) { b.title = f.title; b.dataset.baseTitle = f.title; }
      if (f.role) b.dataset.role = f.role;
      if (f.badge) b.innerHTML = f.label + '<span class="booru-extra-badge" aria-hidden="true"></span>';
      return b;
    }
    return null;
  }

  /**
   * 依当前来源渲染工具栏到 #toolbar-source. 引用: syncToolbar / enterRemote.
   */
  function renderToolbar() {
    var def = cur();
    var bar = def.bar ? _BARS[def.bar] : null;
    var wrap = document.getElementById('toolbar-source');
    if (!wrap) return;
    wrap.innerHTML = '';
    if (!bar) return;
    (bar.fields || []).forEach(function (f) {
      var el = buildField(f);
      if (el) wrap.appendChild(el);
    });
    syncExtraBtn();
    attachTagAutocomplete();
  }

  /** 标签类别编号 -> 可读名称. 引用: attachTagAutocomplete. */
  var _CAT_NAMES = { 0: '通用', 1: '画师', 3: '版权', 4: '角色', 5: '元' };

  /**
   * 为 booru-tags 输入框绑定标签自动补全下拉.
   *
   * 仅补全当前正在输入的最后一个空格分隔词; 选中后替换该词并保留其余部分。
   * 下拉固定定位到输入框下方, 支持键盘上下选择 / 回车或点击确认 / Esc 关闭。
   * 引用: renderToolbar.
   */
  function attachTagAutocomplete() {
    var input = document.getElementById('booru-tags');
    if (!input) return;
    var box = document.createElement('div');
    box.className = 'tag-suggest';
    box.style.display = 'none';
    (input.parentNode || document.body).appendChild(box);
    var timer = null;
    var items = [];
    var activeIndex = -1;
    var seq = 0;
    var suppress = false; // 选中补全后跳过紧随的 input 事件, 避免下拉立即重弹

    function close() {
      seq++; // 使在途补全响应过期, 避免输入清空/切换来源后仍重弹下拉
      box.style.display = 'none';
      box.innerHTML = '';
      items = [];
      activeIndex = -1;
    }

    /** 从输入框取当前正在补全的词(最后一个非空片段). 引用: onInput. */
    function currentWord() {
      var v = input.value;
      var m = v.match(/(\S+)\s*$/);
      return m ? m[1] : '';
    }

    /** 用选中的补全词替换最后一个片段. 引用: choose. */
    function replaceWord(word) {
      var v = input.value;
      // 用占位符定位最后一个词起点, 避免 tag 名内含正则特殊字符引起转义
      var start = v.replace(/(\S+)\s*$/, '\u0000').indexOf('\u0000');
      input.value = v.slice(0, start) + word;
    }

    function render(data) {
      box.innerHTML = '';
      items = data;
      activeIndex = items.length ? 0 : -1;
      if (!items.length) { box.style.display = 'none'; return; }
      var table = document.createElement('table');
      table.className = 'tag-suggest-table';
      items.forEach(function (t, i) {
        var tr = document.createElement('tr');
        tr.className = 'tag-suggest-row' + (i === activeIndex ? ' active' : '');
        var tdName = document.createElement('td');
        tdName.className = 'tag-suggest-name';
        tdName.textContent = t.name;
        tdName.title = t.name;
        var tdCat = document.createElement('td');
        tdCat.className = 'tag-suggest-cat';
        tdCat.textContent = _CAT_NAMES[t.category] || '标签';
        var tdCnt = document.createElement('td');
        tdCnt.className = 'tag-suggest-cnt';
        tdCnt.textContent = t.count > 0 ? ('' + t.count) : '';
        tr.appendChild(tdName);
        tr.appendChild(tdCat);
        tr.appendChild(tdCnt);
        tr.addEventListener('mousedown', function (e) {
          e.preventDefault();
          choose(i);
        });
        tr.addEventListener('mousemove', function () {
          if (activeIndex !== i) {
            activeIndex = i;
            highlight();
          }
        });
        table.appendChild(tr);
      });
      box.appendChild(table);
      var r = input.getBoundingClientRect();
      box.style.left = r.left + 'px';
      box.style.top = (r.bottom + 2) + 'px';
      box.style.minWidth = Math.max(r.width, 220) + 'px';
      box.style.display = 'block';
    }

    function highlight() {
      var table = box.querySelector('.tag-suggest-table');
      if (!table) return;
      Array.prototype.forEach.call(table.rows, function (tr, i) {
        tr.classList.toggle('active', i === activeIndex);
      });
      var act = table.rows[activeIndex];
      if (act) act.scrollIntoView({ block: 'nearest' });
    }

    function choose(i) {
      if (i < 0 || i >= items.length) return;
      suppress = true;
      replaceWord(items[i].name);
      close();
      setTimeout(function () { suppress = false; }, 0);
    }

    async function onInput() {
      if (suppress) return;
      var word = currentWord();
      if (!word) { close(); return; }
      var src = mode();
      if (src !== 'gelbooru' && src !== 'danbooru') { close(); return; }
      var mySeq = ++seq;
      clearTimeout(timer);
      timer = setTimeout(function () {
        API.request(API.apiPath('/online-sources/tag-suggest')
          + '?source=' + encodeURIComponent(src)
          + '&q=' + encodeURIComponent(word))
          .then(function (data) {
            if (mySeq !== seq) return; // 丢弃过期响应
            render(data && data.tags ? data.tags : []);
          })
          .catch(function () { close(); });
      }, 200);
    }

    input.addEventListener('input', onInput);
    input.addEventListener('keydown', function (e) {
      if (box.style.display === 'none') return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        e.stopPropagation();
        activeIndex = (activeIndex + 1) % items.length;
        highlight();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        e.stopPropagation();
        activeIndex = (activeIndex - 1 + items.length) % items.length;
        highlight();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        e.stopPropagation();
        choose(activeIndex);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        close();
      }
    });
    input.addEventListener('blur', function () {
      setTimeout(function () { close(); }, 120);
    });
    input.addEventListener('focus', onInput);
  }

  /**
   * 依条件面板字段构建一行控件. 引用: renderExtraPanel.
   * @param {object} field 字段定义
   * @returns {HTMLElement}
   */
  function buildExtraRow(field) {
    var row = document.createElement('div');
    row.className = 'booru-extra-row';
    if (field.rowId) row.id = field.rowId;
    var lab = document.createElement('span');
    lab.className = 'label';
    lab.textContent = field.label;
    if (field.type === 'chips') {
      row.dataset.exRow = field.key;
      if (field.multi) row.dataset.multi = '1';
      row.appendChild(lab);
      var opts = typeof field.options === 'function' ? field.options() : (field.options || []);
      opts.forEach(function (o) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'booru-rate-chip';
        b.dataset.rate = o[0];
        if (o[2]) b.title = o[2];
        b.textContent = o[1];
        row.appendChild(b);
      });
      return row;
    }
    if (field.type === 'range') {
      row.appendChild(lab);
      var lo = document.createElement('input');
      var hi = document.createElement('input');
      var mid = document.createElement('span');
      if (field.date) {
        lo.type = 'date';
        lo.dataset.ex = field.lo;
        hi.type = 'date';
        hi.dataset.ex = field.hi;
        mid.textContent = '≤ 日 ≤';
      } else {
        lo.type = 'number';
        lo.dataset.ex = field.lo;
        lo.placeholder = '下限';
        hi.type = 'number';
        hi.dataset.ex = field.hi;
        hi.placeholder = '上限';
        mid.textContent = '≤ ' + field.label + ' ≤';
      }
      row.appendChild(lo);
      row.appendChild(mid);
      row.appendChild(hi);
      return row;
    }
    row.appendChild(lab);
    var input = document.createElement('input');
    if (field.type === 'select') {
      input = document.createElement('select');
      (field.options || []).forEach(function (o) {
        var op = document.createElement('option');
        op.value = o[0];
        op.textContent = o[1];
        input.appendChild(op);
      });
    } else {
      input.type = field.type === 'date' ? 'date' : 'text';
      if (field.type === 'text') input.spellcheck = false;
      if (field.placeholder) input.placeholder = field.placeholder;
    }
    input.dataset.ex = field.key;
    row.appendChild(input);
    return row;
  }

  /**
   * 依当前来源条件面板渲染弹窗字段区到 #booru-extra-fields. 引用: setExtraDlg.
   */
  function renderExtraPanel() {
    var panel = curExtra();
    var box = document.getElementById('booru-extra-fields');
    if (!box) return;
    box.innerHTML = '';
    if (!panel) return;
    (panel.fields || []).forEach(function (field) {
      var row = buildExtraRow(field);
      if (field.type === 'range' && field.date && mode() === 'gelbooru') row.hidden = true;
      box.appendChild(row);
    });
  }

  /**
   * 收集来源 schema 中所有需持久化的输入控件 id. 引用: saveUiState / applyForm.
   * @param {object} def 来源定义
   * @returns {string[]}
   */
  function stateKeysFor(def) {
    var bar = def.bar ? _BARS[def.bar] : null;
    if (!bar) return [];
    var keys = [];
    (bar.fields || []).forEach(function (f) {
      if (f.id) keys.push(f.id);
      if (f.ids) keys = keys.concat(f.ids);
    });
    return keys;
  }

  var _posts = [];
  var _page = 1;
  var _pageLo = 1;
  var _pageHi = 1;
  var _session = ''; // 会话式来源 (bilibili/x) 的续拉 token
  var _source = 'gelbooru';
  var _loading = false;
  var _hasMore = false;
  var _hasPrev = false;
  var _dupSkip = 0;
  var _selected = {};
  var _status = {};
  var _aborters = {};
  var _pageIo = null;
  var _colLo = null;
  var _colHi = null;
  var _minY = 0;
  var _cw = 0;
  var _gap = 8;
  var _pad = 8;
  var _scrollSaveTimer = null;
  var _scrollRaf = null;
  var _EX_KEYS = ['score', 'width', 'height', 'id'];
  var _batchRunning = false;
  var _batchJobId = null;
  var _batchWatch = null;
  var _batchParamTimer = null;

  function destDir() {
    if (typeof DirPicker !== 'undefined' && DirPicker.getDirs) {
      var ds = DirPicker.getDirs();
      if (ds.length) return ds[0];
    }
    return 'main';
  }

  function mode() {
    var el = document.getElementById('filter-mode');
    return el ? el.value : 'local';
  }

  function isRemote() {
    return !!(cur() && cur().remote);
  }

  /** 媒体 URL 依来源查表. 引用: 卡片/预览. */
  function thumbUrl(post) {
    var d = _SOURCES[post.source];
    return d && d.thumb ? d.thumb(post) : (post.preview_url || post.sample_url || '');
  }

  function sampleUrl(post) {
    var d = _SOURCES[post.source];
    return d && d.sample ? d.sample(post) : (post.sample_url || post.file_url || post.preview_url || '');
  }

  function saveUiState() {
    var payload = {
      mode: mode(),
      startPage: startPageFromInput(),
      source: _source,
      page: _page,
    };
    for (var skey in _SOURCES) {
      stateKeysFor(_SOURCES[skey]).forEach(function (cid) {
        payload[cid] = (document.getElementById(cid) || {}).value || '';
      });
    }
    try {
      sessionStorage.setItem(STATE_KEY, JSON.stringify(payload));
    } catch (e) { /* ignore quota */ }
  }

  function loadUiState() {
    try {
      var raw = sessionStorage.getItem(STATE_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  function startPageFromInput() {
    var el = document.getElementById('booru-start-page');
    if (noJump()) {
      if (el) el.value = '1';
      return 1;
    }
    var n = el ? parseInt(el.value, 10) : 1;
    if (!n || n < 1) n = 1;
    if (n > 10000) n = 10000;
    if (el) el.value = String(n);
    return n;
  }

  function applyForm(st) {
    if (!st) return;
    for (var akey in _SOURCES) {
      stateKeysFor(_SOURCES[akey]).forEach(function (cid) {
        var ael = document.getElementById(cid);
        if (ael && st[cid] != null) ael.value = st[cid];
      });
    }
    var start = document.getElementById('booru-start-page');
    var pg = parseInt(st.page != null ? st.page : st.startPage, 10);
    if (!(pg > 0)) pg = 1;
    if (noJump()) pg = 1; // 游标式来源恢复时固定第一页
    if (start) start.value = String(pg);
    syncExtraBtn();
  }

  function resetLayout() {
    _colLo = null;
    _colHi = null;
    _minY = 0;
  }

  /**
   * 统计已生效的附加条件条数（每项上下限分别计；精确相等计 1）。
   * 引用：syncExtraBtn。
   * @param {object} f
   * @returns {number}
   */
  function countExtra(f) {
    var n = 0;
    var ratings = Array.isArray(f.rating) ? f.rating : (f.rating ? [f.rating] : []);
    n += ratings.length;
    for (var i = 0; i < _EX_KEYS.length; i++) {
      var k = _EX_KEYS[i];
      var lo = f[k + 'Lo'] || '';
      var hi = f[k + 'Hi'] || '';
      if (lo && hi && lo === hi) n += 1;
      else {
        if (lo) n += 1;
        if (hi) n += 1;
      }
    }
    var dLo = f.dateLo || '';
    var dHi = f.dateHi || '';
    if (dLo && dHi && dLo === dHi) n += 1;
    else {
      if (dLo) n += 1;
      if (dHi) n += 1;
    }
    if (f.user) n += 1;
    var more = f.more;
    if (Array.isArray(more)) n += more.length;
    else if (more) n += String(more).split(/\s+/).filter(Boolean).length;
    return n;
  }

  /**
   * 附加条件可读摘要。引用：syncExtraBtn 的 title。
   * @param {object} f
   * @returns {string}
   */
  function summarizeExtra(f) {
    var bits = [];
    var ratings = Array.isArray(f.rating) ? f.rating : (f.rating ? [f.rating] : []);
    if (ratings.length) bits.push('评级:' + ratings.join('/'));
    for (var i = 0; i < _EX_KEYS.length; i++) {
      var k = _EX_KEYS[i];
      var lo = f[k + 'Lo'] || '';
      var hi = f[k + 'Hi'] || '';
      if (lo && hi && lo === hi) bits.push(k + '=' + lo);
      else if (lo && hi) bits.push(k + ' ' + lo + '…' + hi);
      else if (lo) bits.push(k + '≥' + lo);
      else if (hi) bits.push(k + '≤' + hi);
    }
    var dLo = f.dateLo || '';
    var dHi = f.dateHi || '';
    if (dLo && dHi && dLo === dHi) bits.push('日期=' + dLo);
    else if (dLo && dHi) bits.push('日期 ' + dLo + '…' + dHi);
    else if (dLo) bits.push('日期≥' + dLo);
    else if (dHi) bits.push('日期≤' + dHi);
    if (f.user) bits.push('用户:' + f.user);
    var more = f.more;
    if (Array.isArray(more) && more.length) bits.push(more.join(' '));
    else if (more && String(more).trim()) bits.push(String(more).trim());
    return bits.join(' · ');
  }

  function syncExtraBtn() {
    var panel = curExtra();
    var bar = cur().bar;
    if (!panel || !bar) return;
    var btn = document.getElementById('btn-' + bar + '-extra');
    var el = document.getElementById(bar + '-extra');
    if (!btn || !el) return;
    var f = panel.parse(el.value.trim());
    var n = panel.count(f);
    var summary = panel.summarize(f);
    btn.classList.toggle('has-extra', n > 0);
    btn.title = n > 0 ? summary : (btn.dataset.baseTitle || '');
    var badge = btn.querySelector('.booru-extra-badge');
    if (badge) badge.textContent = n > 0 ? String(n) : '';
  }

  function parseExtra(raw) {
    var out = { rating: [], user: '', more: [], dateLo: '', dateHi: '' };
    for (var i = 0; i < _EX_KEYS.length; i++) {
      out[_EX_KEYS[i] + 'Lo'] = '';
      out[_EX_KEYS[i] + 'Hi'] = '';
    }
    var dropDate = mode() === 'gelbooru';
    var tokens = String(raw || '').split(/\s+/).filter(Boolean);
    for (var j = 0; j < tokens.length; j++) {
      var t = tokens[j], m;
      if ((m = /^rating:([gsqe](?:,[gsqe])*)$/i.exec(t))) {
        out.rating = m[1].split(',').map(function (x) { return x.toLowerCase(); });
      } else if ((m = /^-rating:([gsqe])$/i.exec(t))) {
        // Gelbooru 排除语法: 选剩余两个评级.
        var gel = ['s', 'q', 'e'];
        out.rating = gel.filter(function (x) { return x !== m[1].toLowerCase(); });
      } else if ((m = /^(score|width|height|id):(>=|>|<=|<)?(-?\d+)$/i.exec(t))) {
        var k = m[1].toLowerCase(), op = m[2] || '=', v = m[3];
        if (op === '<=' || op === '<') out[k + 'Hi'] = v;
        else if (op === '=') { out[k + 'Lo'] = v; out[k + 'Hi'] = v; }
        else out[k + 'Lo'] = v;
      } else if (/^date:/i.test(t)) {
        if (dropDate) continue;
        if ((m = /^date:(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/i.exec(t))) {
          out.dateLo = m[1];
          out.dateHi = m[2];
        } else if ((m = /^date:(>=|>|<=|<)?(\d{4}-\d{2}-\d{2})$/i.exec(t))) {
          var dop = m[1] || '=', dv = m[2];
          if (dop === '<=' || dop === '<') out.dateHi = dv;
          else if (dop === '=') { out.dateLo = dv; out.dateHi = dv; }
          else out.dateLo = dv;
        } else out.more.push(t);
      } else if ((m = /^user:(.+)$/i.exec(t))) out.user = m[1];
      else out.more.push(t);
    }
    return out;
  }

  function buildExtra(f) {
    var parts = [];
    var ratings = Array.isArray(f.rating) ? f.rating : (f.rating ? [f.rating] : []);
    if (ratings.length) {
      if (mode() === 'gelbooru') {
        // Gelbooru 仅 3 个评级(s/q/e): 选两个即排除剩余一个(官方支持 -rating: 排除).
        var gel = ['s', 'q', 'e'];
        if (ratings.length === 2) {
          var excluded = gel.filter(function (x) { return ratings.indexOf(x) < 0; });
          if (excluded.length === 1) parts.push('-rating:' + excluded[0]);
        } else if (ratings.length === 1) {
          parts.push('rating:' + ratings[0]);
        }
      } else {
        // Danbooru 支持 rating:g,s 逗号 OR.
        parts.push('rating:' + ratings.join(','));
      }
    }
    for (var i = 0; i < _EX_KEYS.length; i++) {
      var k = _EX_KEYS[i], lo = f[k + 'Lo'] || '', hi = f[k + 'Hi'] || '';
      if (lo && hi && lo === hi) parts.push(k + ':' + lo);
      else {
        if (lo) parts.push(k + ':>=' + lo);
        if (hi) parts.push(k + ':<=' + hi);
      }
    }
    if (mode() !== 'gelbooru') {
      var dLo = f.dateLo || '', dHi = f.dateHi || '';
      if (dLo && dHi && dLo === dHi) parts.push('date:' + dLo);
      else {
        if (dLo) parts.push('date:>=' + dLo);
        if (dHi) parts.push('date:<=' + dHi);
      }
    }
    if (f.user) parts.push('user:' + f.user);
    if (Array.isArray(f.more) && f.more.length) parts.push(f.more.join(' '));
    else if (f.more && String(f.more).trim()) parts.push(String(f.more).trim());
    return parts.join(' ').trim();
  }

  function readExtraDlg() {
    var panel = curExtra();
    var f = panel ? panel.parse('') : {};
    var dlg = document.getElementById('booru-extra-dlg');
    if (panel) {
      (panel.fields || []).forEach(function (field) {
        if (field.type !== 'chips') return;
        var row = dlg.querySelector('[data-ex-row="' + field.key + '"]');
        var on = row ? row.querySelectorAll('.booru-rate-chip.on') : [];
        var vals = [];
        for (var k = 0; k < on.length; k++) {
          var r = on[k].dataset.rate;
          if (r) vals.push(r);
        }
        f[field.key] = field.multi ? vals : (vals.length ? vals[0] : '');
      });
    }
    var fields = dlg ? dlg.querySelectorAll('[data-ex]') : [];
    for (var i = 0; i < fields.length; i++) {
      var el = fields[i], key = el.dataset.ex;
      f[key] = el.value.trim();
    }
    return f;
  }

  function fillExtraDlg() {
    var panel = curExtra();
    var bar = cur().bar;
    if (!panel || !bar) return;
    var src = document.getElementById(bar + '-extra');
    var f = panel.parse((src || {}).value || '');
    var dlg = document.getElementById('booru-extra-dlg');
    if (panel) {
      (panel.fields || []).forEach(function (field) {
        if (field.type !== 'chips') return;
        var row = dlg.querySelector('[data-ex-row="' + field.key + '"]');
        var chips = row ? row.querySelectorAll('.booru-rate-chip') : [];
        var val = f[field.key];
        var arr = Array.isArray(val) ? val : (val ? [val] : []);
        for (var c = 0; c < chips.length; c++) {
          chips[c].classList.toggle('on', arr.indexOf(chips[c].dataset.rate) >= 0);
        }
      });
    }
    var fields = dlg ? dlg.querySelectorAll('[data-ex]') : [];
    for (var j = 0; j < fields.length; j++) {
      var el = fields[j], key = el.dataset.ex, v = f[key];
      el.value = Array.isArray(v) ? v.join(' ') : (v == null ? '' : v);
    }
  }

  function setExtraDlg(show) {
    if (show) {
      renderExtraPanel();
      syncCheatLink();
      fillExtraDlg();
    }
    var dlg = document.getElementById('booru-extra-dlg');
    if (!dlg) return;
    dlg.classList.toggle('show', !!show);
    dlg.setAttribute('aria-hidden', show ? 'false' : 'true');
  }

  function clearExtraDlg() {
    var dlg = document.getElementById('booru-extra-dlg');
    if (!dlg) return;
    var chips = dlg.querySelectorAll('.booru-rate-chip');
    for (var i = 0; i < chips.length; i++) chips[i].classList.remove('on');
    var fields = dlg.querySelectorAll('[data-ex]');
    for (var j = 0; j < fields.length; j++) fields[j].value = '';
  }

  function applyExtraDlg() {
    var panel = curExtra();
    var bar = cur().bar;
    if (panel && bar) {
      var el = document.getElementById(bar + '-extra');
      if (el) el.value = panel.build(readExtraDlg());
    }
    syncExtraBtn();
    setExtraDlg(false);
    saveUiState();
  }

  function updatePageStatus() {
    var start = document.getElementById('booru-start-page');
    var prev = document.getElementById('btn-booru-page-prev');
    var next = document.getElementById('btn-booru-page-next');
    var remote = isRemote();
    if (start) start.disabled = !!noJump();
    if (start && remote && _page >= 1 && document.activeElement !== start) {
      start.value = String(_page);
    }
    if (prev) {
      prev.disabled = !remote || _loading || !_hasPrev;
      prev.title = _loading ? '加载中…' : (_hasPrev ? '上一页' : '没有上一页');
    }
    if (next) {
      next.disabled = !remote || _loading || !_hasMore;
      next.title = _loading ? '加载中…' : (_hasMore ? '下一页' : '没有下一页');
    }
  }

  /** 视口靠上卡片 → 当前页 */
  function syncPageFromScroll() {
    if (noJump()) return; // 游标式来源页码无跳页语义
    if (!isRemote() || !_posts.length) return;
    var start = document.getElementById('booru-start-page');
    if (start && document.activeElement === start) return;
    var cards = document.querySelectorAll('#masonry .card[data-page]');
    if (!cards.length) return;
    var anchor = window.innerHeight * 0.28;
    var best = null;
    var bestDist = Infinity;
    for (var i = 0; i < cards.length; i++) {
      var r = cards[i].getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) continue;
      var d = Math.abs((r.top + r.bottom) / 2 - anchor);
      if (d < bestDist) {
        bestDist = d;
        best = cards[i];
      }
    }
    if (!best) return;
    var p = parseInt(best.dataset.page, 10);
    if (!(p > 0) || p === _page) return;
    _page = p;
    if (start) start.value = String(p);
  }

  /**
   * 滚动到指定页的第一张卡片。引用：app.js 更改列数后恢复页位。
   * @param {number} page
   */
  function scrollToPage(page) {
    var p = parseInt(page, 10);
    if (!(p > 0)) return;
    if (noJump()) return; // 游标式来源不支持跳页
    _page = p;
    var startEl = document.getElementById('booru-start-page');
    if (startEl) startEl.value = String(p);
    var cards = document.querySelectorAll('#masonry .card[data-page="' + p + '"]');
    if (!cards.length) return;
    var y = cards[0].getBoundingClientRect().top + window.scrollY - 80;
    window.scrollTo(0, Math.max(0, y));
  }

  /**
   * @returns {number}
   */
  function currentPage() {
    var start = document.getElementById('booru-start-page');
    if (start) {
      var v = parseInt(start.value, 10);
      if (v > 0) return v;
    }
    return _page > 0 ? _page : 1;
  }

  function stopPageObserve() {
    if (_pageIo) {
      _pageIo.disconnect();
      _pageIo = null;
    }
  }

  function ensureTrigger(container, id, topPx) {
    var el = document.getElementById(id);
    if (!el || el.parentNode !== container) {
      if (el) el.remove();
      el = document.createElement('div');
      el.id = id;
      el.setAttribute('aria-hidden', 'true');
      el.style.cssText =
        'position:absolute;left:0;width:1px;height:1px;margin:0;padding:0;' +
        'pointer-events:none;visibility:hidden;';
      container.appendChild(el);
    }
    el.style.top = Math.max(0, topPx) + 'px';
    return el;
  }

  function syncLoadTrigger() {
    stopPageObserve();
    var container = document.getElementById('masonry');
    var downOld = document.getElementById('booru-load-trigger-down');
    var upOld = document.getElementById('booru-load-trigger-up');
    if (!container || !isRemote() || !_posts.length || !_colHi || !_colHi.length) {
      if (downOld) downOld.remove();
      if (upOld) upOld.remove();
      return;
    }

    var margin = '280px 0px';
    _pageIo = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (!entries[i].isIntersecting) continue;
        if (_loading || !_posts.length) return;
        var id = entries[i].target.id;
        if (id === 'booru-load-trigger-down' && _hasMore) loadMore(false);
        if (id === 'booru-load-trigger-up' && _hasPrev) loadMore(true);
      }
    }, { root: null, rootMargin: margin, threshold: 0 });

    if (_hasMore) {
      var short = Math.min.apply(null, _colHi) - _minY;
      var downEl = ensureTrigger(container, 'booru-load-trigger-down', short);
      _pageIo.observe(downEl);
      requestAnimationFrame(function () {
        if (!_hasMore || _loading || !downEl.isConnected) return;
        if (downEl.getBoundingClientRect().top < window.innerHeight + 280) loadMore(false);
      });
    } else if (downOld) {
      downOld.remove();
    }

    if (_hasPrev) {
      var upEl = ensureTrigger(container, 'booru-load-trigger-up', 0);
      _pageIo.observe(upEl);
      requestAnimationFrame(function () {
        if (!_hasPrev || _loading || !upEl.isConnected) return;
        var r = upEl.getBoundingClientRect();
        if (r.top < window.innerHeight + 280 && r.bottom > -280) loadMore(true);
      });
    } else if (upOld) {
      upOld.remove();
    }
  }

  function filterNewPosts(list) {
    var seen = {};
    for (var i = 0; i < _posts.length; i++) seen[_posts[i].key] = true;
    var fresh = [];
    var dup = 0;
    for (var j = 0; j < list.length; j++) {
      var p = list[j];
      if (seen[p.key]) {
        dup += 1;
        continue;
      }
      seen[p.key] = true;
      fresh.push(p);
    }
    return { fresh: fresh, dup: dup };
  }

  function markDownloaded(list) {
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      if (p.downloaded) {
        _selected[p.key] = true;
        _status[p.key] = 'done';
      }
    }
  }

  /**
   * 远程「已收录」= 工作目录磁盘张数。
   * 引用：syncToolbar、目录切换、下载/移除。
   * @returns {Promise<void>}
   */
  async function refreshDiskCount() {
    if (!isRemote()) return;
    var keptEl = document.getElementById('stat-kept');
    var data = await API.request(API.apiPath('/dirs'));
    var name = destDir();
    var hit = null;
    var dirs = data.dirs || [];
    for (var i = 0; i < dirs.length; i++) {
      if (dirs[i].name === name) {
        hit = dirs[i];
        break;
      }
    }
    if (keptEl) keptEl.textContent = String(hit && hit.image_count != null ? hit.image_count : 0);
  }

  /**
   * 远程「已加载」= 当前搜索结果条数。
   * 引用：render、addPosts。
   */
  function paintRemoteCounts() {
    if (!isRemote()) return;
    var loadedEl = document.getElementById('stat-loaded');
    if (loadedEl) loadedEl.textContent = String(_posts.length);
  }

  /**
   * 同步本地/远程工具栏。
   * 本地：已收录 · 已选择 · 回收站
   * 远程：已收录 · 已加载
   * 引用：mode 切换、init。
   */
  function syncToolbar() {
    var remote = isRemote();
    var curDef = cur();
    var localBar = document.getElementById('toolbar-local');
    var srcBar = document.getElementById('toolbar-source');
    var pageWrap = document.getElementById('remote-page-wrap');
    var loadedWrap = document.getElementById('stat-loaded-wrap');
    var selWrap = document.getElementById('stat-selected-wrap');
    var trashWrap = document.getElementById('stat-trashed-wrap');
    if (localBar) localBar.classList.toggle('is-off', remote);
    if (srcBar) {
      srcBar.classList.toggle('is-off', !remote);
      if (remote) renderToolbar();
    }
    if (pageWrap) pageWrap.classList.toggle('is-off', !remote);
    if (loadedWrap) loadedWrap.hidden = !remote;
    if (selWrap) selWrap.hidden = remote;
    if (trashWrap) trashWrap.hidden = remote;
    var hint = document.getElementById('filter-hint');
    if (hint) {
      hint.textContent = !remote
        ? '点击选中 · 右键相似 · Delete 删除'
        : (curDef.hintText || '左键写入目录 · 右键大图 · 上滑上一页 · 下滑下一页');
    }
    if (!remote) {
      stopPageObserve();
      _hasMore = false;
      _hasPrev = false;
      updatePageStatus();
      return;
    }
    refreshDiskCount().catch(function (e) { toast(String(e.message || e)); });
  }

  function cardLabel(post) {
    var srcDef = _SOURCES[post.source] || {};
    if (srcDef.label) return srcDef.label(post);
    var st = _status[post.key] || '';
    var stLabel = st === 'downloading' ? '保存中…'
      : st === 'done' ? '已保存'
      : st === 'error' ? '失败'
      : '未保存';
    var tag = post.is_video ? '视频' : stLabel;
    var rs = resText(post);
    var stem = post.stem || (((_SOURCES[post.source] || {}).stemPrefix || 'g') + post.id);
    return tag + ' · ' + (rs ? rs + ' ' : '') + stem;
  }

  /** 帖子是否有真实宽高. 引用: cardLabel / createCard / showPreview. */
  function hasSize(post) {
    return post.width > 0 && post.height > 0;
  }

  /** 分辨率文本; 无真实宽高时返回空串. 引用: cardLabel / showPreview. */
  function resText(post) {
    return hasSize(post) ? (post.width + '×' + post.height) : '';
  }

  function findCard(key) {
    var container = document.getElementById('masonry');
    if (!container) return null;
    var cards = container.querySelectorAll('.card');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].dataset.key === key) return cards[i];
    }
    return null;
  }

  function postByKey(key) {
    for (var i = 0; i < _posts.length; i++) {
      if (_posts[i].key === key) return _posts[i];
    }
    return null;
  }

  function updateCard(key) {
    var card = findCard(key);
    if (!card) return;
    card.classList.toggle('selected', !!_selected[key]);
    var name = card.querySelector('.info .name');
    var post = postByKey(key);
    if (name && post) name.textContent = cardLabel(post);
  }

  function resolveCols() {
    var colsSlider = document.getElementById('cols-slider');
    if (colsSlider) {
      var v = parseInt(colsSlider.value, 10);
      if (v > 0) return v;
    }
    var w = window.innerWidth;
    return w >= 1600 ? 6 : w >= 1280 ? 5 : w >= 960 ? 4 : w >= 640 ? 3 : 2;
  }

  function createCard(post, cw) {
    var card = document.createElement('div');
    card.className = 'card';
    if (_selected[post.key]) card.classList.add('selected');
    card.dataset.key = post.key;
    if (post.page) card.dataset.page = String(post.page);
    // 来源可自定义卡片点击(如视频进入抽帧页); 否则采用默认下载/预览.
    var srcDef = _SOURCES[post.source] || {};
    card.addEventListener('click', function (e) {
      if (srcDef.onClick) { srcDef.onClick(post); return; }
      onCardClick(post);
    });
    card.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (srcDef.onClick) { srcDef.onClick(post); return; }
      showPreview(post);
    });

    var imgEl = document.createElement('img');
    imgEl.alt = post.id;
    imgEl.loading = 'lazy';
    imgEl.src = thumbUrl(post);
    imgEl.style.width = '100%';
    imgEl.style.height = 'auto';
    // 宽高已知时按真实比例占位, 避免加载前塌缩与加载后拉伸
    if (hasSize(post)) {
      imgEl.style.aspectRatio = post.width + ' / ' + post.height;
    }
    imgEl.style.background = '#111';
    card.appendChild(imgEl);

    var info = document.createElement('div');
    info.className = 'info';
    if (post.is_video) info.classList.add('info-video');
    var name = document.createElement('span');
    name.className = 'name';
    name.textContent = cardLabel(post);
    info.appendChild(name);
    if (post.is_video) {
      var vbtn = document.createElement('button');
      vbtn.className = 'video-dl-btn';
      vbtn.textContent = '下载视频';
      vbtn.title = '用 BBDown 下载视频到 video 目录';
      vbtn.addEventListener('click', function (e) {
        e.stopPropagation();
        e.preventDefault();
        downloadOne(post).catch(function (err) {
          toast('视频下载失败: ' + (err.message || err));
        });
      });
      info.appendChild(vbtn);
    }
    card.appendChild(info);

    var overlay = document.createElement('div');
    overlay.className = 'overlay';
    overlay.textContent = '\u2713';
    card.appendChild(overlay);
    return card;
  }

  var _previewPost = null;

  function postPageUrl(post) {
    var d = _SOURCES[post.source];
    return d && d.pageUrl ? d.pageUrl(post) : '#';
  }

  function parseTags(raw) {
    if (!raw) return [];
    return String(raw).replace(/,/g, ' ').split(/\s+/).filter(Boolean);
  }

  function hidePreview() {
    _previewPost = null;
    var box = document.getElementById('booru-preview');
    var img = document.getElementById('booru-preview-img');
    if (box) box.classList.remove('show');
    if (img) {
      img.removeAttribute('src');
      img.alt = '';
    }
  }

  function showPreview(post) {
    _previewPost = post;
    var box = document.getElementById('booru-preview');
    var img = document.getElementById('booru-preview-img');
    var meta = document.getElementById('booru-preview-meta');
    var tagsEl = document.getElementById('booru-preview-tags');
    var open = document.getElementById('booru-preview-open');
    var dl = document.getElementById('booru-preview-dl');
    if (!box || !img || !meta || !tagsEl) return;

    var sample = sampleUrl(post);
    img.alt = post.stem || post.id;
    img.src = sample;

    var res = resText(post) || '-';
    var st = _status[post.key] === 'done' ? '已保存' : '未保存';
    var srcName = ((_SOURCES[post.source] || {}).nameLabel) || post.source;
    meta.innerHTML =
      '<div><b>' + srcName + '</b> · ' +
      (post.stem || post.id) + '</div>' +
      '<div>分辨率 ' + res + ' · 分数 ' + (post.score != null ? post.score : '-') +
      ' · rating ' + (post.rating || '-') + '</div>' +
      '<div>状态 <b>' + st + '</b></div>';

    if (open) open.href = postPageUrl(post);
    if (dl) dl.textContent = (_status[post.key] === 'done') ? '移除' : '下载';

    tagsEl.innerHTML = '';
    var rpt = (_SOURCES[post.source] || {}).renderPreviewTags;
    if (rpt) {
      rpt(tagsEl, post);
    } else {
      var tags = parseTags(post.tags);
      if (!tags.length) {
        tagsEl.textContent = '无标签';
      } else {
        for (var i = 0; i < tags.length; i++) {
          (function (tag) {
            var chip = document.createElement('button');
            chip.type = 'button';
            chip.className = 'booru-tag-chip';
            chip.textContent = tag;
            chip.title = '点击搜索此标签';
            chip.addEventListener('click', function () {
              var input = document.getElementById('booru-tags');
              if (input) input.value = tag;
              hidePreview();
              search(true, true).catch(function (e) { toast(String(e.message || e)); });
            });
            tagsEl.appendChild(chip);
          })(tags[i]);
        }
      }
    }

    box.classList.add('show');
  }

  function togglePreviewDownload() {
    if (!_previewPost) return;
    var post = _previewPost;
    hidePreview();
    onCardClick(post);
  }

  function pickCol(up) {
    var arr = up ? _colLo : _colHi;
    var c = 0;
    for (var i = 1; i < arr.length; i++) {
      if (up ? arr[i] > arr[c] : arr[i] < arr[c]) c = i;
    }
    return c;
  }

  function place(card, post, up) {
    var container = document.getElementById('masonry');
    var c = pickCol(up);
    card.style.position = 'absolute';
    card.style.left = (_pad + c * (_cw + _gap)) + 'px';
    card.style.width = _cw + 'px';
    container.appendChild(card);
    // 用真实渲染高度累加列高, 而非预估, 保证竖直空隙恒定 = _gap
    var rh = card.offsetHeight;
    var y = up ? (_colLo[c] - rh - _gap) : _colHi[c];
    card.style.top = (y - _minY) + 'px';
    card.dataset.worldY = String(y);
    if (up) _colLo[c] = y;
    else _colHi[c] = y + rh + _gap;
  }

  function syncOrigin() {
    var container = document.getElementById('masonry');
    if (!container || !_colLo) return;
    var lo = Math.min.apply(null, _colLo);
    var hi = Math.max.apply(null, _colHi);
    if (lo < _minY) {
      var delta = _minY - lo;
      _minY = lo;
      var cards = container.querySelectorAll('.card');
      for (var i = 0; i < cards.length; i++) {
        var w = parseFloat(cards[i].dataset.worldY);
        if (!isNaN(w)) cards[i].style.top = (w - _minY) + 'px';
      }
      if (delta) window.scrollBy(0, delta);
    }
    container.style.height = Math.max(0, hi - _minY) + 'px';
  }

  function addPosts(fresh, up) {
    var container = document.getElementById('masonry');
    if (!container || !fresh.length || !_colHi) {
      render();
      return;
    }
    if (up) {
      for (var i = fresh.length - 1; i >= 0; i--) {
        place(createCard(fresh[i], _cw), fresh[i], true);
      }
      syncOrigin();
    } else {
      for (var j = 0; j < fresh.length; j++) {
        place(createCard(fresh[j], _cw), fresh[j], false);
      }
      container.style.height = (Math.max.apply(null, _colHi) - _minY) + 'px';
    }
    paintRemoteCounts();
    syncLoadTrigger();
    syncPageFromScroll();
  }

  function render() {
    var container = document.getElementById('masonry');
    if (!container) return;

    if (!_posts.length) {
      container.innerHTML =
        '<div style="padding:60px;text-align:center;color:var(--text-dim);">无结果，输入 tag 后搜索</div>';
      container.style.height = '';
      resetLayout();
      paintRemoteCounts();
      syncLoadTrigger();
      return;
    }

    var cols = resolveCols();
    _gap = 8;
    _pad = 8;
    var avail = Math.max(0, container.clientWidth - _pad * 2);
    _cw = (avail - _gap * (cols - 1)) / cols;
    if (!(_cw > 0)) _cw = 1;

    container.innerHTML = '';
    _colLo = new Array(cols).fill(0);
    _colHi = new Array(cols).fill(0);
    _minY = 0;

    for (var j = 0; j < _posts.length; j++) {
      place(createCard(_posts[j], _cw), _posts[j], false);
    }
    container.style.height = (Math.max.apply(null, _colHi) - _minY) + 'px';
    paintRemoteCounts();
    syncLoadTrigger();
    syncPageFromScroll();
  }

  async function downloadOne(post) {
    var ctrl = new AbortController();
    _aborters[post.key] = ctrl;
    _status[post.key] = 'downloading';
    updateCard(post.key);
    var res;
    try {
      res = await fetch(API.apiPath('/online-sources/download'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source: post.source,
          id: String(post.id),
          dir: destDir(),
          overwrite: false,
          post: post,
        }),
        signal: ctrl.signal,
      });
    } catch (e) {
      if (e && e.name === 'AbortError') return; // 主动取消, 状态已由 cancelDownload 清理
      throw e;
    } finally {
      delete _aborters[post.key];
    }
    if (!res.ok) {
      var t = await res.text();
      var err = new Error(t || res.statusText);
      err.status = res.status;
      err.retryAfter = parseInt(res.headers.get('Retry-After') || '', 10) || 0;
      _status[post.key] = 'error';
      updateCard(post.key);
      throw err;
    }
    var data = await res.json();
    // 视频帖: 后端返回长任务, 需监听进度; 图片帖直接返回写入结果
    if (data.job_id) {
      toast('视频下载中…');
      API.watchJob(data.job_id, function (s) {
        if (s.status === 'done') {
          _status[post.key] = 'done';
          updateCard(post.key);
          toast('视频下载完成 · ' + (s.message || ''));
          refreshDiskCount();
        } else if (s.status === 'error') {
          _status[post.key] = 'error';
          updateCard(post.key);
          delete _selected[post.key];
          toast('视频下载失败: ' + (s.message || ''));
        }
      });
      return data;
    }
    _status[post.key] = 'done';
    if (data.stem) post.stem = data.stem;
    if (data.filename) post.filename = data.filename;
    updateCard(post.key);
    await refreshDiskCount();
    return data;
  }

  function setBatchDlg(show) {
    var dlg = document.getElementById('booru-batch-dlg');
    if (!dlg) return;
    dlg.classList.toggle('show', !!show);
    dlg.setAttribute('aria-hidden', show ? 'false' : 'true');
    // 标题随模式切换: 远程含下载为「批量下载」, 本地仅元数据操作
    var title = isRemote() ? '批量下载' : '元数据工具';
    ['booru-batch-title', 'booru-batch-bar-label'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.textContent = title;
    });
    // 「重生成 Caption / 刷新元数据」按文件 stem 前缀识别来源, 本地与 booru 来源均可用
    var metaTools = !!cur().metaTools;
    ['booru-batch-regenerate', 'booru-batch-refresh-meta'].forEach(function (id) {
      var b = document.getElementById(id);
      if (b) b.hidden = !metaTools;
    });
    // 「开始下载」依赖在线来源搜索, 本地模式不适用
    var startBtn = document.getElementById('booru-batch-start');
    if (startBtn) startBtn.hidden = !isRemote();
    if (!show && !_batchRunning) setBatchProgress(null);
    syncBatchCloseBtn();
    syncBatchStopBtn();
  }

  /** 空闲显示「关闭」，运行中显示「隐藏」。引用：setBatchDlg、applyBatchSnapshot。 */
  function syncBatchCloseBtn() {
    var btn = document.getElementById('booru-batch-close');
    if (!btn) return;
    btn.textContent = _batchRunning ? '隐藏' : '关闭';
  }

  function syncBatchStopBtn() {
    var btn = document.getElementById('booru-batch-stop');
    if (!btn) return;
    btn.hidden = !_batchRunning;
  }

  function syncBatchStartBtn() {
    var startBtn = document.getElementById('booru-batch-start');
    if (!startBtn) return;
    startBtn.disabled = !!_batchRunning;
    startBtn.textContent = _batchRunning ? '运行中' : '开始';
  }

  /**
   * 同步对话框内与顶栏两处批量进度。
   * @param {number|null} done 已完成数；null 表示隐藏顶栏并重置对话框进度
   * @param {number} [limit=0] 目标数量
   * @param {string} [note=''] 附加说明（如 429 退避、已完成）
   * 引用：applyBatchSnapshot。
   */
  function setBatchProgress(done, limit, note) {
    var dlgWrap = document.getElementById('booru-batch-progress');
    var dlgFill = document.getElementById('booru-batch-fill');
    var dlgPct = document.getElementById('booru-batch-pct');
    var dlgText = document.getElementById('booru-batch-progress-text');
    var bar = document.getElementById('booru-batch-bar');
    var barFill = document.getElementById('booru-batch-bar-fill');
    var barPct = document.getElementById('booru-batch-bar-pct');
    var barText = document.getElementById('booru-batch-bar-text');

    function paint(fill, pctEl, textEl, wrap, hideWrap) {
      if (done == null) {
        if (wrap && hideWrap) wrap.hidden = true;
        if (fill) {
          fill.style.width = '0%';
          fill.classList.remove('indeterminate');
        }
        if (pctEl) pctEl.textContent = '0%';
        if (textEl) textEl.textContent = '-';
        return;
      }
      if (wrap) wrap.hidden = false;
      var total = limit > 0 ? limit : 0;
      var pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
      if (fill) {
        fill.classList.toggle('indeterminate', total <= 0 && done === 0);
        fill.style.width = (total > 0 ? pct : 32) + '%';
      }
      if (pctEl) pctEl.textContent = total > 0 ? (pct + '%') : '…';
      var line = total > 0 ? ('已完成 ' + done + '/' + total) : '准备中…';
      if (note) line += ' · ' + note;
      if (textEl) textEl.textContent = line;
    }

    paint(dlgFill, dlgPct, dlgText, dlgWrap, true);
    paint(barFill, barPct, barText, bar, true);
  }

  /**
   * 应用服务端 JobSnapshot 到进度 UI。
   * @param {object} s
   * 引用：watchBatch、resumeBatch。
   */
  function applyBatchSnapshot(s) {
    var running = s.status === 'running' || s.status === 'queued';
    _batchRunning = running;
    syncBatchCloseBtn();
    syncBatchStopBtn();
    syncBatchStartBtn();
    var done = s.done || 0;
    var total = s.total || 0;
    var note = s.message || '';
    if (s.status === 'queued') {
      setBatchProgress(0, total, note || '等待执行');
      return;
    }
    if (running || s.status === 'done' || s.status === 'cancelled' || s.status === 'error') {
      setBatchProgress(done, total, note);
    }
    if (!running && s.status !== 'queued') {
      updatePageStatus();
    }
  }

  function stopBatchWatch() {
    if (_batchWatch) {
      _batchWatch.close();
      _batchWatch = null;
    }
  }

  /**
   * 订阅批量任务 SSE。
   * @param {string} jobId
   * 引用：runBatch、resumeBatch。
   */
  function watchBatch(jobId) {
    stopBatchWatch();
    _batchJobId = jobId;
    _batchRunning = true;
    syncBatchCloseBtn();
    syncBatchStopBtn();
    syncBatchStartBtn();
    bindBatchParamInputs(true);
    _batchWatch = API.watchJob(jobId, function (s) {
      applyBatchSnapshot(s);
      if (s.status === 'done') {
        toast('批量完成 ' + (s.done || 0) + (s.message ? ' · ' + s.message : ''));
        finishBatchUi();
      } else if (s.status === 'cancelled') {
        toast('批量已取消 · 完成 ' + (s.done || 0));
        finishBatchUi();
      } else if (s.status === 'error') {
        toast('批量失败' + (s.message ? ' · ' + s.message : ''));
        finishBatchUi();
      }
    });
  }

  function finishBatchUi() {
    stopBatchWatch();
    _batchRunning = false;
    _batchJobId = null;
    bindBatchParamInputs(false);
    syncBatchCloseBtn();
    syncBatchStopBtn();
    syncBatchStartBtn();
    setBatchProgress(null);
    updatePageStatus();
    // 元数据工具对话框若处于打开状态, 恢复其操作按钮
    var metaDlg = document.getElementById('booru-meta-dlg');
    if (metaDlg && metaDlg.classList.contains('show')) setMetaDlg(true);
  }

  async function stopBatch() {
    if (!_batchJobId) {
      finishBatchUi();
      return;
    }
    await API.cancelJob(_batchJobId);
    toast('正在停止…');
  }

  /**
   * 从对话框读取并校正批量参数。
   * @returns {{limit:number, conn:number, rateRps:number}}
   * 引用：runBatch、pushBatchParams。
   */
  function readBatchParams() {
    var limitEl = document.getElementById('booru-batch-limit');
    var connEl = document.getElementById('booru-batch-conn');
    var rateEl = document.getElementById('booru-batch-rate');
    var limit = limitEl ? parseInt(limitEl.value, 10) : 50;
    var conn = connEl ? parseInt(connEl.value, 10) : 2;
    var rateRps = rateEl ? parseFloat(rateEl.value) : 0;
    if (!(limit > 0)) limit = 50;
    if (limit > 5000) limit = 5000;
    if (!(conn > 0)) conn = 1;
    if (conn > 8) conn = 8;
    if (!(rateRps > 0)) rateRps = 0;
    if (rateRps > 60) rateRps = 60;
    if (limitEl) limitEl.value = String(limit);
    if (connEl) connEl.value = String(conn);
    if (rateEl) rateEl.value = String(rateRps);
    return { limit: limit, conn: conn, rateRps: rateRps };
  }

  function pushBatchParams() {
    if (!_batchRunning || !_batchJobId) return;
    var p = readBatchParams();
    API.request(API.apiPath('/online-sources/batch/params'), {
      method: 'POST',
      body: JSON.stringify({
        limit: p.limit,
        conn: p.conn,
        rate_rps: p.rateRps,
      }),
    }).catch(function () {});
  }

  function onBatchParamInput() {
    if (!_batchRunning) return;
    clearTimeout(_batchParamTimer);
    _batchParamTimer = setTimeout(pushBatchParams, 200);
  }

  function bindBatchParamInputs(on) {
    var paramIds = ['booru-batch-limit', 'booru-batch-conn', 'booru-batch-rate'];
    for (var i = 0; i < paramIds.length; i++) {
      var el = document.getElementById(paramIds[i]);
      if (!el) continue;
      el.removeEventListener('input', onBatchParamInput);
      el.removeEventListener('change', onBatchParamInput);
      if (on) {
        el.addEventListener('input', onBatchParamInput);
        el.addEventListener('change', onBatchParamInput);
      }
    }
  }

  /**
   * 提交服务端批量下载并监听进度。
   * 引用：工具栏「批量下载」。
   */
  async function runBatch() {
    if (_batchRunning) {
      toast('批量下载进行中');
      return;
    }
    if (!isRemote()) {
      toast('请先切换到图站模式');
      return;
    }
    var params = readBatchParams();
    var req = cur().buildRequest();
    var tags = req.tags, extra = req.extra || '', order = req.order || '';
    if (cur().requireTags && !tags) {
      toast('请先输入 ' + cur().nameLabel + ' 的查询条件');
      return;
    }
    var body = {
      source: mode(),
      tags: tags,
      extra: extra,
      order: order,
      dir: destDir(),
      page: _pageLo > 0 ? _pageLo : 1,
      limit: params.limit,
      conn: params.conn,
      rate_rps: params.rateRps,
      anima_format: !!document.getElementById('booru-batch-anima')?.checked,
      include_meta_tags: !!document.getElementById('booru-batch-meta-tags')?.checked,
      keep_underscores: !!document.getElementById('booru-batch-keep-underscores')?.checked,
    };
    setBatchProgress(0, params.limit, '提交中…');
    _batchRunning = true;
    syncBatchCloseBtn();
    syncBatchStopBtn();
    syncBatchStartBtn();
    var data = await API.request(API.apiPath('/online-sources/batch/start'), {
      method: 'POST',
      body: JSON.stringify(body),
    });
    toast('已提交批量下载');
    watchBatch(data.job_id);
  }

  /**
   * 从在线来源 meta 批量重生成当前目录所有旁路 .txt。
   * 引用：元数据工具对话框「重生成 Caption」按钮。
   */
  async function regenerateCaptions() {
    var body = {
      dir: destDir(),
      anima_format: !!document.getElementById('booru-meta-anima')?.checked,
      include_meta_tags: !!document.getElementById('booru-meta-meta-tags')?.checked,
      keep_underscores: !!document.getElementById('booru-meta-keep-underscores')?.checked,
    };
    var data = await API.request(API.apiPath('/online-sources/regenerate-captions'), {
      method: 'POST',
      body: JSON.stringify(body),
    });
    toast('已重生成 ' + (data.count || 0) + ' 个 caption');
  }

  /**
   * 根据文件 ID 重新拉取 booru 元数据（长任务，SSE 进度）。
   * 引用：元数据工具对话框「刷新元数据」按钮。
   */
  async function refreshMetadata() {
    var body = { dir: destDir() };
    setBatchProgress(0, 0, '提交中…');
    _batchRunning = true;
    setMetaDlg(true);
    var submitted = await API.request(API.apiPath('/online-sources/refresh-metadata'), {
      method: 'POST',
      body: JSON.stringify(body),
    });
    watchBatch(submitted.job_id);
  }

  /**
   * 刷新后查询并重连进行中的批量任务。
   * 引用：init。
   */
  async function resumeBatch() {
    var data = await API.findActiveJob('online_batch', API.dataset());
    if (!data) return;
    applyBatchSnapshot(data);
    watchBatch(data.job_id);
    toast('已恢复批量下载进度');
  }

  async function undownloadOne(post) {
    var res = await API.request(API.apiPath('/online-sources/undownload'), {
      method: 'POST',
      body: JSON.stringify({
        source: post.source,
        id: String(post.id),
        filename: post.filename,
        dir: destDir(),
      }),
    });
    delete _status[post.key];
    updateCard(post.key);
    await refreshDiskCount();
    return res;
  }

  /**
   * 取消进行中的下载: 中断前端请求并清理本地状态与可能已写入的文件.
   * 引用: onCardClick.
   * @param {object} post
   */
  function cancelDownload(post) {
    var ctrl = _aborters[post.key];
    delete _selected[post.key];
    delete _status[post.key];
    updateCard(post.key);
    if (ctrl) ctrl.abort();
    // 后端检测断开会取消下载协程; 若文件已原子写入, 一并清理目录
    undownloadOne(post).catch(function () {});
    toast('已取消');
  }

  function onCardClick(post) {
    if (_status[post.key] === 'downloading') {
      cancelDownload(post);
      return;
    }
    if (_selected[post.key]) {
      delete _selected[post.key];
      updateCard(post.key);
      undownloadOne(post).then(function (data) {
        toast(data.count ? ('已移除 ' + (post.stem || post.id)) : ('目录无文件 ' + (post.stem || post.id)));
      }).catch(function (e) {
          _selected[post.key] = true;
        updateCard(post.key);
        toast('移除失败: ' + (e.message || e));
      });
      return;
    }
    _selected[post.key] = true;
    updateCard(post.key);
    downloadOne(post).then(function (data) {
      if (data) toast(data.skipped ? ('已存在 ' + (data.stem || post.id)) : ('已保存 ' + (data.stem || post.id) + '.png'));
    }).catch(function (e) {
      delete _selected[post.key];
      updateCard(post.key);
      toast('保存失败: ' + (e.message || e));
    });
  }

  function syncCheatLink() {
    var panel = curExtra();
    var a = document.getElementById('booru-cheat');
    if (a) {
      a.hidden = !(panel && panel.cheat);
      if (panel && panel.cheat) {
        a.href = mode() === 'danbooru'
          ? 'https://danbooru.donmai.us/wiki_pages/help:cheatsheet'
          : 'https://gelbooru.com/index.php?page=help&topic=cheatsheet';
      }
    }
    var dateRow = document.getElementById('booru-extra-date-row');
    if (dateRow) dateRow.hidden = mode() === 'gelbooru';
    // 去掉已保存的 date:（Gelbooru 无效）
    if (mode() === 'gelbooru' && panel) {
      var bar = cur().bar;
      var el = document.getElementById(bar + '-extra');
      if (el && el.value) {
        var next = panel.build(panel.parse(el.value));
        if (next !== el.value) el.value = next;
      }
      syncExtraBtn();
    }
  }

  /** 加载本地 ``video`` 目录视频并渲染封面瀑布流. 引用: ``search``(视频来源). */
  async function loadVideos() {
    if (_loading) return;
    _source = mode();
    _loading = true;
    updatePageStatus();
    try {
      var list = await API.request(API.apiPath('/videos') + '?probe=1');
      _posts = (list || []).map(function (v) {
        return {
          source: 'video', path: v.path, key: v.path, stem: v.stem, name: v.name,
          width: v.width, height: v.height, duration: v.duration,
        };
      });
      render();
    } finally {
      _loading = false;
      updatePageStatus();
    }
  }

  async function search(dir, fromFirst) {
    if (mode() === 'video') return loadVideos();
    if (_loading) return;
    var isReset = dir === true || dir === 'reset';
    var isUp = dir === 'up';
    var req = cur().buildRequest();
    var tags = req.tags, extra = req.extra || '', order = req.order || '';
    if (cur().requireTags && !tags) {
      toast('请先输入 ' + cur().nameLabel + ' 的查询条件');
      return;
    }
    _source = mode();
    if (isReset) {
      var start = fromFirst ? 1 : startPageFromInput();
      _pageLo = start;
      _pageHi = start;
      _page = start;
      _session = '';
      _posts = [];
      _selected = {};
      _status = {};
      _hasMore = false;
      _hasPrev = start > 1;
      _dupSkip = 0;
      stopPageObserve();
      window.scrollTo(0, 0);
    }
    var fetchPage = isUp ? _pageLo : _pageHi;
    if (!noJump()) _page = fetchPage;
    saveUiState();
    _loading = true;
    updatePageStatus();
    var srcBar = document.getElementById('toolbar-source');
    var btn = srcBar ? srcBar.querySelector('button[data-role="search"]') : null;
    if (btn) btn.disabled = true;
    var autoNext = false;
    var autoPrev = false;
    try {
      var pbody = {
        source: _source,
        tags: tags,
        extra: extra,
        order: order,
        dir: destDir(),
      };
      if (noJump()) pbody.session = isReset ? '' : _session;
      else pbody.page = fetchPage;
      var data = await API.request(API.apiPath('/online-sources/search'), {
        method: 'POST',
        body: JSON.stringify(pbody),
      });
      var list = data.posts || [];
      if (noJump()) {
        _session = data.session || '';
        _hasMore = !!data.has_more;
        for (var pi = 0; pi < list.length; pi++) list[pi].page = _page;
      } else {
        for (var pi = 0; pi < list.length; pi++) list[pi].page = fetchPage;
        if (!isUp) _hasMore = !!data.has_more;
      }

      var fresh = list;
      var dup = 0;
      if (isReset) {
        _posts = list;
        markDownloaded(list);
        render();
        _dupSkip = 0;
        _hasPrev = _pageLo > 1;
        // 首屏空结果停止；勿因 has_more 误报空转翻页
        if (!list.length) _hasMore = false;
      } else if (isUp && !list.length) {
        _hasPrev = false;
        _pageLo += 1;
        _page = _pageLo;
      } else if (!isReset) {
        var filtered = filterNewPosts(list);
        fresh = filtered.fresh;
        dup = filtered.dup;
        markDownloaded(fresh);
        if (fresh.length) {
          _posts = isUp ? fresh.concat(_posts) : _posts.concat(fresh);
          addPosts(fresh, isUp);
          _dupSkip = 0;
          if (isUp) _hasPrev = _pageLo > 1;
        } else if (_dupSkip < 8 && (isUp ? _pageLo > 1 : _hasMore)) {
          _dupSkip += 1;
          if (isUp) autoPrev = true; else autoNext = true;
        } else if (isUp) {
          _hasPrev = false;
        } else {
          _hasMore = false;
        }
      }

      if (!autoNext && !autoPrev && (isReset || fresh.length || dup)) {
        var head = isReset
          ? (((_SOURCES[_source] || {}).nameLabel || '') + ' · +')
          : (isUp ? '↑+' : '+');
        toast(head + fresh.length + (dup ? ' · 跳过重复 ' + dup : ''));
      }
    } finally {
      _loading = false;
      if (btn) btn.disabled = false;
      updatePageStatus();
      if (!autoNext && !autoPrev) syncLoadTrigger();
    }
    if (autoNext) {
      _pageHi += 1;
      _page = _pageHi;
      return search('down');
    }
    if (autoPrev) {
      _pageLo -= 1;
      _page = _pageLo;
      return search('up');
    }
  }

  function loadMore(up) {
    if (_loading) return;
    if (up) {
      if (noJump()) return; // 游标式来源仅向下会话续拉
      if (!_hasPrev || _pageLo <= 1) return;
      _pageLo -= 1;
      _page = _pageLo;
    } else {
      if (!_hasMore) return;
      if (noJump()) _page += 1;
      else { _pageHi += 1; _page = _pageHi; }
    }
    search(up ? 'up' : 'down').catch(function (e) {
      if (up) { _pageLo += 1; _page = _pageLo; }
      else { _pageHi -= 1; _page = _pageHi; }
      toast(String(e.message || e));
      updatePageStatus();
      syncLoadTrigger();
    });
  }

  function enterRemote(opts) {
    opts = opts || {};
    syncToolbar();
    syncCheatLink();
    applyForm(loadUiState());
    _source = mode();
    _posts = [];
    _session = '';
    var start = startPageFromInput();
    _page = start;
    _pageLo = start;
    _pageHi = start;
    _selected = {};
    _status = {};
    _hasMore = false;
    _hasPrev = start > 1;
    resetLayout();
    stopPageObserve();
    render();
    updatePageStatus();
    if (opts.autoSearch !== false) {
      search(true).catch(function (e) { toast(String(e.message || e)); });
    }
  }

  function leaveRemote() {
    stopBatch();
    hidePreview();
    syncToolbar();
    stopPageObserve();
    saveUiState();
  }

  /** 渲染本地流水: 加载 Store 并按回收站开关显示可见项. 引用: init / mode change. */
  function renderLocal() {
    if (typeof Store === 'undefined') return;
    Store.load().then(function () {
      if (typeof Masonry !== 'undefined') {
        var showTrash = document.getElementById('chk-show-trash');
        Masonry.render(Store.getVisible(showTrash ? showTrash.checked : false));
      }
    });
  }

  function init() {
    var modeEl = document.getElementById('filter-mode');
    if (!modeEl) return;

    var saved = loadUiState();
    if (saved && _SOURCES[saved.mode]) {
      modeEl.value = saved.mode;
    }

    syncCheatLink();
    if (isRemote()) {
      enterRemote({ autoSearch: true });
    } else {
      syncToolbar();
      applyForm(saved);
      renderLocal();
    }

    modeEl.addEventListener('change', function () {
      syncCheatLink();
      if (isRemote()) {
        enterRemote({ autoSearch: true });
      } else {
        leaveRemote();
        syncToolbar();
        applyForm(loadUiState());
        renderLocal();
      }
      saveUiState();
    });

    // 工具栏交互采用事件委托: 渲染后无需重新绑定, 重渲染也保持生效
    var srcBar = document.getElementById('toolbar-source');
    if (srcBar) {
      srcBar.addEventListener('click', function (e) {
        var btn = e.target.closest('button[data-role]');
        if (!btn) return;
        var role = btn.dataset.role;
        if (role === 'search') {
          search(true, true).catch(function (err) { toast(String(err.message || err)); });
        } else if (role === 'batch') {
          if (cur().batchDialog) setBatchDlg(true);
          else runBatch().catch(function (err) { toast(String(err.message || err)); });
        } else if (role === 'extra') {
          setExtraDlg(true);
        }
      });
      srcBar.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && e.target && e.target.tagName === 'INPUT') {
          e.preventDefault();
          search(true, true).catch(function (err) { toast(String(err.message || err)); });
        }
      });
      srcBar.addEventListener('change', saveUiState);
    }
    var batchDlg = document.getElementById('booru-batch-dlg');
    if (batchDlg) {
      batchDlg.addEventListener('click', function (e) {
        if (e.target === batchDlg) setBatchDlg(false);
      });
    }
    var batchStart = document.getElementById('booru-batch-start');
    if (batchStart) {
      batchStart.addEventListener('click', function () {
        runBatch().catch(function (e) {
          finishBatchUi();
          setBatchProgress(null);
          toast(String(e.message || e));
        });
      });
    }
    var batchClose = document.getElementById('booru-batch-close');
    if (batchClose) {
      batchClose.addEventListener('click', function () {
        setBatchDlg(false);
      });
    }
    var batchStop = document.getElementById('booru-batch-stop');
    if (batchStop) {
      batchStop.addEventListener('click', function () {
        stopBatch().catch(function (e) { toast(String(e.message || e)); });
      });
    }
    var metaDlg = document.getElementById('booru-meta-dlg');
    if (metaDlg) {
      metaDlg.addEventListener('click', function (e) {
        if (e.target === metaDlg) setMetaDlg(false);
      });
    }
    var metaEntry = document.getElementById('btn-meta-dlg');
    if (metaEntry) {
      metaEntry.addEventListener('click', function () { setMetaDlg(true); });
    }
    var metaClose = document.getElementById('booru-meta-close');
    if (metaClose) {
      metaClose.addEventListener('click', function () { setMetaDlg(false); });
    }
    var metaRegen = document.getElementById('booru-meta-regenerate');
    if (metaRegen) {
      metaRegen.addEventListener('click', function () {
        regenerateCaptions().catch(function (e) { toast(String(e.message || e)); });
      });
    }
    var metaRefresh = document.getElementById('booru-meta-refresh');
    if (metaRefresh) {
      metaRefresh.addEventListener('click', function () {
        refreshMetadata().catch(function (e) { toast(String(e.message || e)); });
      });
    }
    var extraDlg = document.getElementById('booru-extra-dlg');
    if (extraDlg) {
      extraDlg.addEventListener('click', function (e) {
        if (e.target === extraDlg) { setExtraDlg(false); return; }
        var chip = e.target.closest('.booru-rate-chip');
        if (chip) {
          chip.classList.toggle('on');
          return;
        }
        var act = e.target.closest('[data-ex-act]');
        if (!act) return;
        if (act.dataset.exAct === 'clear') clearExtraDlg();
        else if (act.dataset.exAct === 'cancel') setExtraDlg(false);
        else if (act.dataset.exAct === 'apply') applyExtraDlg();
      });
    }
    syncExtraBtn();
    var startEl = document.getElementById('booru-start-page');
    if (startEl) {
      startEl.addEventListener('change', saveUiState);
      startEl.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          search(true).catch(function (err) { toast(String(err.message || err)); });
        }
      });
    }
    var prevBtn = document.getElementById('btn-booru-page-prev');
    if (prevBtn) prevBtn.addEventListener('click', function () { loadMore(true); });
    var nextBtn = document.getElementById('btn-booru-page-next');
    if (nextBtn) nextBtn.addEventListener('click', function () { loadMore(false); });

    var preview = document.getElementById('booru-preview');
    if (preview) {
      preview.addEventListener('click', function (e) {
        if (e.target === preview) hidePreview();
      });
    }
    var previewClose = document.getElementById('booru-preview-close');
    if (previewClose) previewClose.addEventListener('click', hidePreview);
    var previewOpen = document.getElementById('booru-preview-open');
    if (previewOpen) previewOpen.addEventListener('click', hidePreview);
    var previewDl = document.getElementById('booru-preview-dl');
    if (previewDl) previewDl.addEventListener('click', togglePreviewDownload);

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var batchDlgEl = document.getElementById('booru-batch-dlg');
      if (batchDlgEl && batchDlgEl.classList.contains('show')) {
        setBatchDlg(false);
        return;
      }
      var extraDlg = document.getElementById('booru-extra-dlg');
      if (extraDlg && extraDlg.classList.contains('show')) {
        setExtraDlg(false);
        return;
      }
      if (document.getElementById('booru-preview') &&
          document.getElementById('booru-preview').classList.contains('show')) {
        hidePreview();
      }
    });

    window.addEventListener('scroll', function () {
      if (!isRemote()) return;
      if (!_scrollRaf) {
        _scrollRaf = requestAnimationFrame(function () {
          _scrollRaf = null;
          syncPageFromScroll();
        });
      }
      clearTimeout(_scrollSaveTimer);
      _scrollSaveTimer = setTimeout(saveUiState, 200);
    }, { passive: true });

    resumeBatch().catch(function () {});
  }

  return {
    init: init,
    isRemote: isRemote,
    render: render,
    mode: mode,
    currentPage: currentPage,
    scrollToPage: scrollToPage,
    refreshDiskCount: refreshDiskCount,
  };
})();