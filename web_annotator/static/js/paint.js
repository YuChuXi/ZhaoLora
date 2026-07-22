/**
 * paint.js -- Canvas Alpha 绘制. 无 try/catch, 全局错误处理.
 */
(function() {
  'use strict';

  var _images = [], _index = 0;
  var _origW = 0, _origH = 0;
  var _alpha = null, _rgbImg = null, _scale = 1;
  var _brushRadius = 80, _brushPct = 3, _defaultAlpha = 255;
  var _tool = 'brush', _isDrawing = false, _paintValue = 0;
  var _rectStart = null, _mouseScreen = null, _modified = false;
  var _preloadCache = {};
  var _renderScheduled = false;
  var _rgbScaledCanvas = null;
  var _compCanvas = null;

  // ---- 笔刷半径: sqrt(w*h) * 百分比 ----
  function _calcBrush() {
    if (!_origW || !_origH) return;
    _brushRadius = Math.max(2, Math.round(Math.sqrt(_origW * _origH) * _brushPct / 100));
  }

  var canvas = document.getElementById('paint-canvas');
  var ctx = canvas.getContext('2d');
  var wrap = document.getElementById('canvas-wrap');
  var _brushPreview = document.getElementById('brush-preview');

  // ---- 全局错误显示 ----
  function errshow(msg) {
    document.getElementById('info-text').textContent = '\u26a0 ' + msg;
  }
  window.addEventListener('error', function(e) { errshow(e.message); });
  window.addEventListener('unhandledrejection', function(e) { errshow(e.reason); });

  // ---- API ----
  async function api(path, opts) {
    opts = opts || {};
    var res = await fetch(path, { headers: opts.headers || {}, method: opts.method || 'GET', body: opts.body || undefined });
    if (!res.ok) {
      var msg = '';
      try { var j = await res.json(); msg = j.detail || JSON.stringify(j); } catch(_) { msg = await res.text(); }
      throw new Error(res.status + ': ' + msg);
    }
    return res;
  }

  // ---- 配置 ----
  async function loadConfig() {
    var cfg = await (await api('/api/paint/config')).json();
    _defaultAlpha = cfg.default_alpha || 255;
    document.getElementById('default-alpha').value = _defaultAlpha;
  }
  async function saveConfig() {
    _defaultAlpha = parseInt(document.getElementById('default-alpha').value) || 255;
    await api('/api/paint/config', { method: 'POST',
      body: JSON.stringify({ default_alpha: _defaultAlpha }),
      headers: { 'Content-Type': 'application/json' } });
  }

  // ---- 加载 ----
  async function loadImages() {
    var res = await (await api('/api/paint/images')).json();
    _images = res;
    if (_images.length > 0) loadIndex(0);
    else document.getElementById('info-text').textContent = '没有图片';
  }

  async function loadIndex(idx) {
    if (_images.length === 0) return;
    _index = Math.max(0, Math.min(_images.length - 1, idx));
    var img = _images[_index];

    var cached = _preloadCache[img.path];
    if (cached) {
      _rgbImg = cached.img; _origW = cached.w; _origH = cached.h;
      if (cached.alpha) {
        _alpha = new Uint8Array(cached.alpha);
      } else {
        _alpha = new Uint8Array(_origW * _origH);
        _alpha.fill(_defaultAlpha);
      }
      _modified = false;
      _calcBrush(); _updateBrushUI();
      invalidateScaledCache(); _alphaOrigCanvas = null;
      fitCanvas(); _scheduleRender(); updateNav();
      _preloadNeighbors(_index);
      return;
    }

    var blob = await (await api('/api/paint/image?path=' + encodeURIComponent(img.path))).blob();
    var url = URL.createObjectURL(blob);
    _rgbImg = new Image();
    _rgbImg.onload = function() {
      _origW = _rgbImg.naturalWidth; _origH = _rgbImg.naturalHeight;
      URL.revokeObjectURL(url);
      _alpha = new Uint8Array(_origW * _origH);
      _defaultAlpha = parseInt(document.getElementById('default-alpha').value) || 255;
      _alpha.fill(_defaultAlpha);
      _modified = false;
      _calcBrush(); _updateBrushUI();
      invalidateScaledCache(); _alphaOrigCanvas = null;
      fitCanvas(); _scheduleRender(); updateNav();
      loadExistingMask(img.path).then(function() { _alphaOrigCanvas = null; _scheduleRender(); });
      _preloadNeighbors(_index);
    };
    _rgbImg.onerror = function() { throw new Error('图片解码失败: ' + img.path); };
    _rgbImg.src = url;
  }

  async function loadExistingMask(path) {
    var resp = await fetch('/api/paint/mask?path=' + encodeURIComponent(path));
    if (!resp.ok) return;  // 404 = 无 mask, 正常
    var blob = await resp.blob();
    var url = URL.createObjectURL(blob);
    await new Promise(function(resolve) {
      var mi = new Image();
      mi.onload = function() {
        URL.revokeObjectURL(url);
        if (mi.naturalWidth !== _origW || mi.naturalHeight !== _origH) { resolve(); return; }
        var tc = document.createElement('canvas'); tc.width = _origW; tc.height = _origH;
        var tctx = tc.getContext('2d'); tctx.drawImage(mi, 0, 0);
        var d = tctx.getImageData(0, 0, _origW, _origH).data;
        for (var i = 0; i < _alpha.length; i++) _alpha[i] = d[i * 4];
        resolve();
      };
      mi.onerror = resolve;
      mi.src = url;
    });
  }

  // ---- 预加载 ----
  var _preloadSeq = 0;
  async function _preloadNeighbors(idx) {
    var seq = ++_preloadSeq;
    for (var di = 1; di <= 15; di++) {
      if (seq !== _preloadSeq) return;
      var ni = idx + di; if (ni < _images.length) await _preloadOne(_images[ni].path);
      ni = idx - di; if (ni >= 0) await _preloadOne(_images[ni].path);
    }
  }
  async function _preloadOne(path) {
    if (_preloadCache[path]) return;
    var blob = await fetch('/api/paint/image?path=' + encodeURIComponent(path)).then(function(r) { return r.blob(); });
    var url = URL.createObjectURL(blob);
    var img = new Image();
    img.onload = function() {
      var entry = { img: img, w: img.naturalWidth, h: img.naturalHeight };
      _preloadCache[path] = entry;
      URL.revokeObjectURL(url);
      // 同时预加载 mask
      fetch('/api/paint/mask?path=' + encodeURIComponent(path)).then(function(r) {
        if (!r.ok) return;
        return r.blob();
      }).then(function(b) {
        if (!b) return;
        var u = URL.createObjectURL(b);
        var mi = new Image();
        mi.onload = function() {
          if (mi.naturalWidth === entry.w && mi.naturalHeight === entry.h) {
            var tc = document.createElement('canvas'); tc.width = entry.w; tc.height = entry.h;
            var tctx = tc.getContext('2d'); tctx.drawImage(mi, 0, 0);
            var d = tctx.getImageData(0, 0, entry.w, entry.h).data;
            var a = new Uint8Array(entry.w * entry.h);
            for (var i = 0; i < a.length; i++) a[i] = d[i * 4];
            entry.alpha = a;
            entry.alphaCanvas = tc;
          }
          URL.revokeObjectURL(u);
        };
        mi.src = u;
      });
    };
    img.src = url;
  }
  function _updateBrushUI() {
    document.getElementById('brush-label').textContent = _brushPct.toFixed(1) + '% ~' + _brushRadius + 'px';
  }
  function fitCanvas() {
    var cw = wrap.clientWidth, ch = wrap.clientHeight;
    _scale = Math.min(cw / _origW, ch / _origH);
    var vw = Math.max(1, Math.floor(_origW * _scale)), vh = Math.max(1, Math.floor(_origH * _scale));
    canvas.width = vw; canvas.height = vh;
    canvas.style.position = 'absolute';
    canvas.style.left = ((cw - vw) / 2) + 'px';
    canvas.style.top = ((ch - vh) / 2) + 'px';
    canvas.style.margin = '0';
  }
  window.addEventListener('resize', function() { if (_rgbImg) { invalidateScaledCache(); fitCanvas(); _scheduleRender(); } });

  // ---- 渲染 ----
  var _checkerCanvas = null, _alphaOrigCanvas = null;

  function _scheduleRender() {
    if (!_renderScheduled) {
      _renderScheduled = true;
      requestAnimationFrame(function() {
        _renderScheduled = false;
        _doRender();
      });
    }
  }

  function invalidateScaledCache() {
    _rgbScaledCanvas = null;
    _checkerCanvas = null;
    _compCanvas = null;
  }

  function _doRender() {
    if (!_rgbImg || !_alpha) return;
    var vw = canvas.width, vh = canvas.height;

    // 棋盘格（缓存）
    if (!_checkerCanvas || _checkerCanvas.width !== vw || _checkerCanvas.height !== vh || _checkerCanvas._darken !== _defaultAlpha) {
      _checkerCanvas = document.createElement('canvas');
      _checkerCanvas.width = vw; _checkerCanvas.height = vh;
      _checkerCanvas._darken = _defaultAlpha;
      var cc = _checkerCanvas.getContext('2d');
      var dk = _defaultAlpha / 255;
      var c0 = Math.round(204 * dk), c1 = Math.round(153 * dk);
      for (var y = 0; y < vh; y += 12) {
        for (var x = 0; x < vw; x += 12) {
          var isOdd = (Math.floor(x/12) + Math.floor(y/12)) % 2 === 1;
          cc.fillStyle = 'rgb(' + (isOdd ? c1 : c0) + ',' + (isOdd ? c1 : c0) + ',' + (isOdd ? c1 : c0) + ')';
          cc.fillRect(x, y, 12, 12);
        }
      }
    }
    ctx.drawImage(_checkerCanvas, 0, 0);

    // RGB 缩放到显示大小（缓存，仅在图片或视口变化时重建）
    if (!_rgbScaledCanvas || _rgbScaledCanvas.width !== vw || _rgbScaledCanvas.height !== vh) {
      _rgbScaledCanvas = document.createElement('canvas');
      _rgbScaledCanvas.width = vw; _rgbScaledCanvas.height = vh;
      var rctx = _rgbScaledCanvas.getContext('2d');
      rctx.drawImage(_rgbImg, 0, 0, vw, vh);
    }

    // 构建 alpha 原始分辨率 canvas（仅首次，后续画笔直接画在上面）
    if (!_alphaOrigCanvas) {
      _alphaOrigCanvas = document.createElement('canvas');
      _alphaOrigCanvas.width = _origW; _alphaOrigCanvas.height = _origH;
      var aoctx = _alphaOrigCanvas.getContext('2d');
      var imgData = aoctx.createImageData(_origW, _origH);
      for (var i = 0; i < _alpha.length; i++) {
        var a = _alpha[i], j = i * 4;
        imgData.data[j] = a; imgData.data[j+1] = a; imgData.data[j+2] = a; imgData.data[j+3] = a;
      }
      aoctx.putImageData(imgData, 0, 0);
    }

    // 合成：RGB * AlphaMask -> 棋盘格上
    if (!_compCanvas || _compCanvas.width !== vw || _compCanvas.height !== vh) {
      _compCanvas = document.createElement('canvas');
      _compCanvas.width = vw; _compCanvas.height = vh;
    }
    var cctx = _compCanvas.getContext('2d');
    cctx.globalCompositeOperation = 'source-over';
    cctx.clearRect(0, 0, vw, vh);
    cctx.drawImage(_rgbScaledCanvas, 0, 0);
    cctx.globalCompositeOperation = 'destination-in';
    cctx.drawImage(_alphaOrigCanvas, 0, 0, vw, vh);
    ctx.drawImage(_compCanvas, 0, 0);
  }

  // ---- 笔刷预览 ----
  function _updateBrushPreview() {
    if (_tool !== 'brush' || !_mouseScreen) {
      _brushPreview.style.display = 'none'; return;
    }
    var wrapRect = wrap.getBoundingClientRect();
    var mx = _mouseScreen[0] - wrapRect.left, my = _mouseScreen[1] - wrapRect.top;
    var r = _brushRadius * _scale;
    if (r < 2) { _brushPreview.style.display = 'none'; return; }
    _brushPreview.style.display = 'block';
    _brushPreview.style.width = (r * 2) + 'px';
    _brushPreview.style.height = (r * 2) + 'px';
    _brushPreview.style.left = (mx - r) + 'px';
    _brushPreview.style.top = (my - r) + 'px';
  }

  function screenToOrig(sx, sy) {
    var rect = canvas.getBoundingClientRect();
    return [Math.max(0, Math.min(_origW-1, Math.floor((sx - rect.left) / _scale))),
            Math.max(0, Math.min(_origH-1, Math.floor((sy - rect.top) / _scale)))];
  }

  function stampDisc(cx, cy, r, val) {
    if (_alphaOrigCanvas) {
      var aoctx = _alphaOrigCanvas.getContext('2d');
      if (val === 0) {
        aoctx.globalCompositeOperation = 'destination-out';
        aoctx.fillStyle = '#fff';
        aoctx.beginPath();
        aoctx.arc(cx, cy, r, 0, Math.PI * 2);
        aoctx.fill();
        aoctx.globalCompositeOperation = 'source-over';
      } else {
        aoctx.globalAlpha = val / 255;
        aoctx.fillStyle = '#fff';
        aoctx.beginPath();
        aoctx.arc(cx, cy, r, 0, Math.PI * 2);
        aoctx.fill();
        aoctx.globalAlpha = 1;
      }
    }
  }

  function strokeLine(x0, y0, x1, y1, r, val) {
    var dist = Math.max(Math.abs(x1-x0), Math.abs(y1-y0), 1);
    for (var i = 0; i <= dist; i++) {
      var t = i/dist;
      stampDisc(Math.round(x0+(x1-x0)*t), Math.round(y0+(y1-y0)*t), r, val);
    }
  }

  function fillRect(x0, y0, x1, y1, val) {
    var xa = Math.max(0, Math.min(x0, x1)), xb = Math.min(_origW-1, Math.max(x0, x1));
    var ya = Math.max(0, Math.min(y0, y1)), yb = Math.min(_origH-1, Math.max(y0, y1));
    if (_alphaOrigCanvas) {
      var aoctx = _alphaOrigCanvas.getContext('2d');
      if (val === 0) {
        aoctx.clearRect(xa, ya, xb - xa + 1, yb - ya + 1);
      } else {
        aoctx.globalAlpha = val / 255;
        aoctx.fillStyle = '#fff';
        aoctx.fillRect(xa, ya, xb - xa + 1, yb - ya + 1);
        aoctx.globalAlpha = 1;
      }
    }
  }

  // ---- 鼠标 ----
  canvas.addEventListener('mousedown', function(e) {
    e.preventDefault(); if (!_alpha) return;
    _isDrawing = true;
    var pos = screenToOrig(e.clientX, e.clientY);
    _paintValue = (e.button === 0) ? (document.getElementById('chk-left-transparent').checked ? 0 : 255) : 255;
    if (_tool === 'brush') stampDisc(pos[0], pos[1], _brushRadius, _paintValue);
    _rectStart = pos; _scheduleRender();
  });
  canvas.addEventListener('mousemove', function(e) {
    _mouseScreen = [e.clientX, e.clientY];
    if (!_alpha) return;
    _updateBrushPreview();
    if (!_isDrawing) return;
    var pos = screenToOrig(e.clientX, e.clientY);
    if (_tool === 'brush' && _rectStart) { strokeLine(_rectStart[0], _rectStart[1], pos[0], pos[1], _brushRadius, _paintValue); _rectStart = pos; }
    _scheduleRender();
  });
  canvas.addEventListener('mouseup', function(e) {
    if (!_isDrawing || !_alpha) return;
    if (_tool === 'rect' && _rectStart) { var pos = screenToOrig(e.clientX, e.clientY); fillRect(_rectStart[0], _rectStart[1], pos[0], pos[1], _paintValue); }
    _isDrawing = false; _rectStart = null; _brushPreview.style.display = 'none'; _modified = true; _scheduleRender();
  });
  canvas.addEventListener('mouseleave', function() { _isDrawing = false; _rectStart = null; _mouseScreen = null; _brushPreview.style.display = 'none'; });
  canvas.addEventListener('contextmenu', function(e) { e.preventDefault(); });
  canvas.addEventListener('wheel', function(e) {
    e.preventDefault();
    var d = e.deltaY < 0 ? 1 : -1;
    _brushPct = Math.max(0.5, Math.min(20, _brushPct + d * Math.max(0.1, _brushPct * 0.15)));
    _calcBrush(); _updateBrushUI(); _updateBrushPreview();
  });

  // ---- 保存 ----
  async function doSave(force) {
    if (!_alpha || !_alphaOrigCanvas) return;
    if (!force && !_modified) return;
    var img = _images[_index];
    var fmt = document.getElementById('save-format').value || 'alpha_bw';
    // 从 canvas 回读 alpha 数组（保存时需要）
    var aoctx = _alphaOrigCanvas.getContext('2d');
    var imgData = aoctx.getImageData(0, 0, _origW, _origH);
    for (var i = 0; i < _alpha.length; i++) _alpha[i] = imgData.data[i * 4 + 3];
    return new Promise(function(resolve) {
      _alphaOrigCanvas.toBlob(function(blob) {
        if (!blob) { errshow('toBlob failed'); resolve(); return; }
        fetch('/api/paint/upload-mask?path=' + encodeURIComponent(img.path) + '&format=' + fmt, {
          method: 'POST', headers: { 'Content-Type': 'image/png' }, body: blob
        }).then(function(r) { return r.json(); }).then(function(res) {
          _modified = false;
          _images[_index].has_mask = true; updateNav();
          document.getElementById('info-text').textContent = '已保存: ' + res.saved;
          var cached = _preloadCache[img.path];
          if (cached) { cached.alpha = new Uint8Array(_alpha); cached.alphaCanvas = _alphaOrigCanvas; }
          resolve();
        }).catch(function() { errshow('上传失败'); resolve(); });
      }, 'image/png');
    });
  }

  async function goPrev() { if (_index > 0) { await doSave(false); loadIndex(_index - 1); } }
  async function goNext() { if (_index < _images.length - 1) { await doSave(false); loadIndex(_index + 1); } }
  function goSkip() { if (_index < _images.length - 1) loadIndex(_index + 1); }

  // ---- 工具栏 ----
  document.getElementById('btn-prev').addEventListener('click', goPrev);
  document.getElementById('btn-next').addEventListener('click', goNext);
  document.getElementById('btn-skip').addEventListener('click', goSkip);
  var toolSel = document.getElementById('tool-select');
  if (toolSel) toolSel.addEventListener('change', function() { _tool = this.value; });
  document.getElementById('brush-slider').addEventListener('input', function() { _brushPct = parseFloat(this.value); _calcBrush(); _updateBrushUI(); _updateBrushPreview(); });
  document.getElementById('btn-save').addEventListener('click', function() { doSave(true); });
  document.getElementById('btn-clear').addEventListener('click', function() { if (_alpha && _alphaOrigCanvas) { _alpha.fill(0); _alphaOrigCanvas.getContext('2d').clearRect(0, 0, _origW, _origH); _modified = true; _scheduleRender(); } });
  document.getElementById('btn-fill').addEventListener('click', function() { if (_alpha && _alphaOrigCanvas) { _alpha.fill(255); var fc = _alphaOrigCanvas.getContext('2d'); fc.fillStyle = '#fff'; fc.fillRect(0, 0, _origW, _origH); _modified = true; _scheduleRender(); } });
  document.getElementById('btn-default').addEventListener('click', function() {
    _defaultAlpha = parseInt(document.getElementById('default-alpha').value) || 255;
    if (_alpha && _alphaOrigCanvas) { _alpha.fill(_defaultAlpha); var dc = _alphaOrigCanvas.getContext('2d'); dc.clearRect(0, 0, _origW, _origH); dc.globalAlpha = _defaultAlpha / 255; dc.fillStyle = '#fff'; dc.fillRect(0, 0, _origW, _origH); dc.globalAlpha = 1; _modified = true; _scheduleRender(); }
    saveConfig();
  });
  document.getElementById('default-alpha').addEventListener('change', saveConfig);

  document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); goPrev(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); goNext(); }
    else if (e.key === 's' || e.key === 'S') { e.preventDefault(); doSave(true); }
    else if (e.key === 'x' || e.key === 'X') { goSkip(); }
  });

  function updateNav() {
    var img = _images[_index];
    var done = 0;
    for (var i = 0; i < _images.length; i++) { if (_images[i].has_mask) done++; }
    document.getElementById('paint-progress').textContent = (_index+1) + '/' + _images.length + ' (已标注' + done + ')';
    document.getElementById('paint-filename').textContent = img ? img.stem : '-';
    var status = img ? (img.has_mask ? '\u2705 已标注' : '\ud83d\udd8c\ufe0f 待绘制') : '';
    if (_origW) status += '  |  ' + _origW + 'x' + _origH;
    document.getElementById('info-text').textContent = status;
  }

  loadConfig().then(loadImages);
})();
