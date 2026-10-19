/**
 * dir_picker.js —— 全站统一子目录多选（右上角，main 默认选中且置顶）
 */
var DirPicker = (function () {
  'use strict';

  var _host = null;
  var _btn = null;
  var _menu = null;
  var _dirs = [];
  var _selected = {};
  var _listeners = [];
  var _disabled = false;
  var _single = false;
  var _open = false;

  /**
   * 从数据集设置读取已选目录名列表。
   * @returns {string[]}
   */
  function loadSaved() {
    var saved = AppSettings.get('dirs');
    if (Array.isArray(saved)) {
      var arr = saved.filter(function (x) { return typeof x === 'string' && x; });
      if (arr.length) return arr;
    }
    return ['main'];
  }

  /**
   * 持久化已选目录到数据集设置。
   * @param {string[]} names
   */
  function save(names) {
    var list = names && names.length ? names.slice() : ['main'];
    AppSettings.set('dirs', list);
  }

  /**
   * 当前已选目录名（至少一个；否则回落为 main）。
   * @returns {string[]}
   */
  function getDirs() {
    var out = [];
    for (var i = 0; i < _dirs.length; i++) {
      var n = _dirs[i].name;
      if (_selected[n]) out.push(n);
    }
    if (!out.length) {
      var saved = loadSaved();
      return saved.length ? saved : ['main'];
    }
    return out;
  }

  /**
   * 取已选列表中的第一项（单选语义, 供 paint.js 单目录工作流使用）。
   * @returns {string}
   */
  function getDir() {
    return getDirs()[0] || 'main';
  }

  /**
   * main 置顶；列表中无 main 时补一项。
   * @param {Array<{name: string, label?: string, image_count?: number, txt_count?: number}>} dirs
   * @returns {Array<object>}
   */
  function pinMain(dirs) {
    var list = (dirs || []).slice();
    var main = null;
    var rest = [];
    list.forEach(function (d) {
      if (d.name === 'main') main = d;
      else if (d.name !== '.') rest.push(d);
    });
    if (!main) main = { name: 'main', label: 'main', image_count: 0 };
    return [main].concat(rest);
  }

  /**
   * @param {function(string[]): void} fn
   */
  function onChange(fn) {
    if (typeof fn === 'function') _listeners.push(fn);
  }

  /**
   * 通知监听方当前目录选择。
   */
  function emit() {
    var names = getDirs();
    save(names);
    updateButton();
    for (var i = 0; i < _listeners.length; i++) {
      _listeners[i](names.slice());
    }
  }

  /**
   * 更新触发按钮上的摘要文案。
   */
  function updateButton() {
    if (!_btn) return;
    var names = getDirs();
    if (names.length === 1) {
      _btn.textContent = names[0] + ' ▾';
    } else {
      _btn.textContent = names.length + ' 个目录 ▾';
    }
    _btn.title = names.join(', ');
    _btn.disabled = _disabled;
  }

  /**
   * 关闭下拉菜单。
   */
  function closeMenu() {
    _open = false;
    if (_menu) _menu.hidden = true;
    if (_btn) _btn.setAttribute('aria-expanded', 'false');
  }

  /**
   * 按触发按钮定位悬浮菜单（挂在 body，避免被导航栏 overflow 裁切）。
   */
  function positionMenu() {
    if (!_btn || !_menu) return;
    var r = _btn.getBoundingClientRect();
    _menu.style.top = Math.round(r.bottom + 6) + 'px';
    _menu.style.right = Math.round(window.innerWidth - r.right) + 'px';
    _menu.style.left = 'auto';
  }

  /**
   * 打开下拉菜单。
   */
  function openMenu() {
    if (_disabled) return;
    _open = true;
    positionMenu();
    if (_menu) _menu.hidden = false;
    if (_btn) _btn.setAttribute('aria-expanded', 'true');
  }

  /**
   * 切换下拉菜单。
   */
  function toggleMenu() {
    if (_open) closeMenu();
    else openMenu();
  }

  /**
   * 渲染复选列表。
   */
  function renderMenu() {
    if (!_menu) return;
    _menu.innerHTML = '';
    if (!_dirs.length) {
      _menu.innerHTML = '<div class="app-dir-empty">暂无目录</div>';
      return;
    }
    _dirs.forEach(function (d) {
      var row = document.createElement('label');
      row.className = 'app-dir-item' + (d.name === 'main' ? ' is-main' : '');
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!_selected[d.name];
      cb.disabled = _disabled;
      cb.addEventListener('change', function () {
        if (_single) {
          // 单选模式: 勾选即唯一, 取消后回落 main
          _selected = {};
          if (cb.checked) _selected[d.name] = true;
          if (!Object.keys(_selected).length) _selected.main = true;
          renderMenu();
          emit();
          return;
        }
        if (cb.checked) {
          _selected[d.name] = true;
        } else {
          delete _selected[d.name];
          if (!Object.keys(_selected).length) {
            _selected.main = true;
            cb.checked = d.name === 'main';
            if (d.name !== 'main') {
              var mainCb = _menu.querySelector('input[data-name="main"]');
              if (mainCb) mainCb.checked = true;
            }
          }
        }
        emit();
      });
      cb.dataset.name = d.name;

      var name = document.createElement('span');
      name.className = 'name';
      name.textContent = d.label || d.name;

      var meta = document.createElement('span');
      meta.className = 'meta';
      var n = d.image_count != null ? d.image_count : (d.txt_count || 0);
      meta.textContent = n ? String(n) : '';

      row.appendChild(cb);
      row.appendChild(name);
      row.appendChild(meta);
      _menu.appendChild(row);
    });
  }

  /**
   * 用目录列表填充选择器，并恢复已保存勾选。
   * @param {Array<object>} dirs
   */
  function fill(dirs) {
    _dirs = pinMain(dirs);
    var saved = loadSaved();
    _selected = {};
    if (_single) {
      // 单选模式仅恢复第一个已保存目录
      for (var i = 0; i < _dirs.length; i++) {
        if (saved.indexOf(_dirs[i].name) !== -1) {
          _selected[_dirs[i].name] = true;
          break;
        }
      }
    } else {
      var set = {};
      saved.forEach(function (n) { set[n] = true; });
      _dirs.forEach(function (d) {
        if (set[d.name]) _selected[d.name] = true;
      });
    }
    if (!Object.keys(_selected).length) _selected.main = true;
    renderMenu();
    updateButton();
    emit();
  }

  /**
   * 从接口拉取目录列表并填充。
   * @param {string} [apiUrl]
   * @returns {Promise<object>}
   */
  async function load(apiUrl) {
    var res = await fetch(apiUrl || (typeof API !== 'undefined' ? API.apiPath('/dirs') : '/api/dirs'));
    if (!res.ok) throw new Error(await res.text() || res.statusText);
    var data = await res.json();
    fill(data.dirs || []);
    return data;
  }

  /**
   * 绑定到导航栏右侧容器，构建按钮与下拉。
   * @param {HTMLElement} host
   */
  function bind(host) {
    _host = host;
    if (!_host) return;
    _host.classList.add('app-dir-picker');
    _host.innerHTML = '';

    _btn = document.createElement('button');
    _btn.type = 'button';
    _btn.className = 'app-dir-btn';
    _btn.setAttribute('aria-haspopup', 'listbox');
    _btn.setAttribute('aria-expanded', 'false');
    _btn.addEventListener('click', function (e) {
      e.stopPropagation();
      toggleMenu();
    });

    _menu = document.createElement('div');
    _menu.className = 'app-dir-menu';
    _menu.hidden = true;
    _menu.addEventListener('click', function (e) { e.stopPropagation(); });

    _host.appendChild(_btn);
    document.body.appendChild(_menu);
    updateButton();
  }

  /**
   * 禁用或启用选择器。
   * @param {boolean} v
   */
  function setDisabled(v) {
    _disabled = !!v;
    updateButton();
    if (_disabled) closeMenu();
    renderMenu();
  }

  /**
   * 切换单选模式(蒙版页一次只处理一个目录); 默认多选不受影响。
   * @param {boolean} v
   */
  function setSingle(v) {
    _single = !!v;
    if (_single) {
      // 折叠为单选: 仅保留第一个选中目录
      var names = getDirs();
      _selected = {};
      if (names.length) _selected[names[0]] = true;
      else _selected.main = true;
      renderMenu();
      updateButton();
      emit();
    }
  }

  document.addEventListener('click', function () {
    if (_open) closeMenu();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && _open) closeMenu();
  });

  window.addEventListener('resize', function () {
    if (_open) positionMenu();
  });

  window.addEventListener('scroll', function () {
    if (_open) positionMenu();
  }, true);

  return {
    loadSaved: loadSaved,
    save: save,
    getDir: getDir,
    getDirs: getDirs,
    pinMain: pinMain,
    onChange: onChange,
    fill: fill,
    load: load,
    bind: bind,
    setDisabled: setDisabled,
    setSingle: setSingle,
  };
})();
