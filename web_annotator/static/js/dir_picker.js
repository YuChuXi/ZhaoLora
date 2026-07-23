/**
 * dir_picker.js —— 统一子目录多选（main 默认选中且置顶）
 */
var DirPicker = (function () {
  'use strict';

  var KEY = 'annotator_work_dirs';

  function loadSaved() {
    try {
      var a = JSON.parse(localStorage.getItem(KEY) || '[]');
      if (Array.isArray(a) && a.length) {
        return a.filter(function (x) { return typeof x === 'string' && x; });
      }
    } catch (e) { /* ignore */ }
    var legacy = localStorage.getItem('annotator_work_dir');
    if (legacy) return [legacy];
    return ['main'];
  }

  function save(names) {
    localStorage.setItem(KEY, JSON.stringify(names && names.length ? names : ['main']));
  }

  function pinMain(dirs) {
    var list = (dirs || []).slice();
    var main = null;
    var rest = [];
    list.forEach(function (d) {
      if (d.name === 'main') main = d;
      else rest.push(d);
    });
    if (!main) main = { name: 'main', label: 'main', image_count: 0 };
    return [main].concat(rest);
  }

  function mount(el, opts) {
    opts = opts || {};
    if (!el) throw new Error('DirPicker: 缺少容器');

    var state = {
      dirs: [],
      onChange: opts.onChange || null,
      disabled: false,
    };

    function selected() {
      return state.dirs.filter(function (d) { return d.checked; }).map(function (d) { return d.name; });
    }

    function emit() {
      var names = selected();
      save(names);
      if (state.onChange) state.onChange(names.slice());
    }

    function render() {
      el.innerHTML = '';
      el.classList.add('dir-picker');
      if (opts.mode === 'compact') el.classList.add('compact');
      else el.classList.remove('compact');

      if (!state.dirs.length) {
    el.innerHTML = '<div class="dir-picker-empty">无目录</div>';
        return;
      }

      state.dirs.forEach(function (d, idx) {
        var row = document.createElement('label');
        row.className = 'dir-item' + (d.name === 'main' ? ' is-main' : '');
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = !!d.checked;
        cb.disabled = state.disabled;
        cb.addEventListener('change', function () {
          state.dirs[idx].checked = cb.checked;
          emit();
        });
        var name = document.createElement('span');
        name.className = 'name';
        name.textContent = d.label || d.name;
        var meta = document.createElement('span');
        meta.className = 'meta';
        var countKey = opts.countKey || 'image_count';
        var suffix = opts.countSuffix || ' 图';
        meta.textContent = (d[countKey] != null ? d[countKey] : (d.image_count || 0)) + suffix;
        row.appendChild(cb);
        row.appendChild(name);
        row.appendChild(meta);
        el.appendChild(row);
      });
    }

    function applyChecked(saved) {
      var set = {};
      (saved || []).forEach(function (n) { set[n] = true; });
      if (!Object.keys(set).length) set.main = true;
      state.dirs.forEach(function (d) {
        d.checked = !!set[d.name];
      });
      if (!state.dirs.some(function (d) { return d.checked; }) && state.dirs.length) {
        state.dirs[0].checked = true;
      }
    }

    function setDirs(dirs, preferSaved) {
      state.dirs = pinMain(dirs).map(function (d) {
        return {
          name: d.name,
          label: d.label || d.name,
          image_count: d.image_count || 0,
          txt_count: d.txt_count || 0,
          checked: false,
        };
      });
      applyChecked(preferSaved === false ? ['main'] : loadSaved());
      render();
      save(selected());
    }

    async function load(apiUrl) {
      var res = await fetch(apiUrl || '/api/dirs');
      if (!res.ok) throw new Error(await res.text() || res.statusText);
      var data = await res.json();
      setDirs(data.dirs || [], true);
      return data;
    }

    function setAll(checked) {
      state.dirs.forEach(function (d) { d.checked = !!checked; });
      render();
      emit();
    }

    function setDisabled(v) {
      state.disabled = !!v;
      render();
    }

    return {
      load: load,
      setDirs: setDirs,
      selected: selected,
      setAll: setAll,
      setDisabled: setDisabled,
      render: render,
    };
  }

  return {
    KEY: KEY,
    loadSaved: loadSaved,
    save: save,
    pinMain: pinMain,
    mount: mount,
  };
})();
