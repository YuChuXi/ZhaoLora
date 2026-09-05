/**
 * paint.js —— 蒙版绘制
 */
(function() {
  'use strict';

  var _images = [];
  // 当前图片状态: 随图片切换的可变状态收敛于单一对象, 异步加载各自持有副本,
  // 完成后再交换到 _cur; 过期加载不会污染当前图(避免把上一张的蒙版保存到新图路径).
  var _cur = null; // { index, item, w, h, rgbImg, alpha, modified }
  var _strip = null; // 横排预览条控制器(Filmstrip)
  var _scale = 1;
  var _viewW = 0, _viewH = 0, _dpr = 1;
  var _brushRadius = 80, _brushPct = 3, _defaultAlpha = 255;
  var _isDrawing = false, _isTouch = false, _paintValue = 0;
  // _mouseOrig: 鼠标视口坐标经 screenToOrig 钳位后的原图坐标, 预览与实际绘制共用的单一事实源
  var _rectStart = null, _mouseOrig = null;
  // shift 直线模式: 落笔按下 shift 后, 从落笔点画直线到抬笔点, 终点即 _mouseOrig
  var _lineMode = false, _lineStart = null;
  var _preloadCache = {};
  var _renderScheduled = false;

  // ---- 蒙板模板 ----
  var _mergeMode = 'min';      // 模板与当前蒙板的合并方式: min(默认) / max
  var _templates = [];         // 模板槽位(≤10): {data:Uint8ClampedArray, w, h}
  var _captureMode = false;    // 框选当前图区域保存模板进行中
  var _captureStart = null;    // 框选起点(orig 坐标)
  var _floating = null;        // 未确认实例: {capture,slot,rx,ry,rw,rh,url,data,w,h,anchors} (capture=框选待确认, url/data 尚未就绪; anchors 为 0-1 分数坐标, ≤2 个)
  var _floatDrag = null;       // 模板拖动状态: {mode, anchor, sx, sy, x0, y0, w0, h0, h, v}

  // ---- WebGL 渲染层: RGB 纹理 + alpha 纹理 shader 合成, 无 2D 中间画布 ----
  var AlphaGL = (function () {
    var gl = null, canvas = null;
    var texRGB = null, texAlpha = null, texTpl = null, texMerge = null, fbo = null, fboMerge = null;
    var W = 0, H = 0;
    var progDraw = null, progShow = null, progCopy = null, progMerge = null, quadBuf = null;
    var DLOC = {}, SLOC = {}, CLOC = {}, MLOC = {};

    var VS = 'attribute vec2 a_pos;varying vec2 v_uv;' +
      'void main(){v_uv=a_pos*0.5+0.5;gl_Position=vec4(a_pos,0.0,1.0);}';
    var FS_DRAW = 'precision mediump float;' +
      'uniform vec2 u_res;uniform vec2 u_center;uniform float u_radius;' +
      'uniform float u_alpha;varying vec2 v_uv;' +
      'void main(){' +
      'vec2 px=vec2(v_uv.x,1.0-v_uv.y)*u_res;' +
      'if(distance(px,u_center)<=u_radius){gl_FragColor=vec4(1.0,1.0,1.0,u_alpha);}else{discard;}}';
    var FS_SHOW = 'precision mediump float;' +
      'uniform sampler2D u_rgb;uniform sampler2D u_alpha;uniform float u_red;' +
      'varying vec2 v_uv;' +
      'void main(){' +
      'vec4 rgb=texture2D(u_rgb,v_uv);' +
      'float a=texture2D(u_alpha,v_uv).a;' +
      // 红色模式(框选模板期间): 红色浓度随蒙板 alpha 反转——不透明处保持原图(透明红),
      // 透明处叠加半透明红, 画布像素全不透明, 避免 alpha=0 处透出棋盘格背景;
      // 正常模式: 蒙板 alpha 即画布 alpha.
      'if(u_red>0.5){gl_FragColor=vec4(mix(rgb.rgb,vec3(1.0,0.0,0.0),(1.0-a)*0.5),1.0);}' +
      'else{gl_FragColor=vec4(rgb.rgb,a);}}';
    // 蒙板合并(模板贴回当前蒙板): u_cur 为拷贝后的当前蒙板副本, u_tpl 为模板图案,
    // u_rect 为模板矩形(画布顶部坐标), 片段按 min/max 合成. 配合 scissor 只写交集矩形.
    var FS_COPY = 'precision mediump float;' +
      'uniform sampler2D u_src;varying vec2 v_uv;' +
      'void main(){gl_FragColor=texture2D(u_src,v_uv);}';
    var FS_MERGE = 'precision mediump float;' +
      'uniform sampler2D u_cur;uniform sampler2D u_tpl;' +
      'uniform vec2 u_res;uniform vec4 u_rect;uniform float u_merge;' +
      'varying vec2 v_uv;' +
      'void main(){' +
      'vec2 px=vec2(v_uv.x,1.0-v_uv.y)*u_res;' +
      'float cur=texture2D(u_cur,v_uv).a;' +
      'vec2 tuv=clamp(vec2((px.x-u_rect.x)/u_rect.z,(px.y-u_rect.y)/u_rect.w),0.0,1.0);' +
      'float t=texture2D(u_tpl,tuv).a;' +
      'float outA=(u_merge>0.5)?max(cur,t):min(cur,t);' +
      'gl_FragColor=vec4(1.0,1.0,1.0,outA);}';

    function _compile(type, src) {
      var s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
        throw new Error('Shader: ' + gl.getShaderInfoLog(s));
      return s;
    }
    function _link(vs, fs) {
      var p = gl.createProgram();
      gl.attachShader(p, vs); gl.attachShader(p, fs); gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS))
        throw new Error('Link: ' + gl.getProgramInfoLog(p));
      return p;
    }
    function _bindQuad(prog) {
      gl.useProgram(prog);
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
      var loc = gl.getAttribLocation(prog, 'a_pos');
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    }

    function _initTexParams(t) {
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    }

    /**
     * 初始化 WebGL context, 着色器, RGB/alpha 双纹理, framebuffer. 引用: paint.js 初始化.
     * @param {HTMLCanvasElement} c
     * @param {number} w
     * @param {number} h
     */
    function init(c, w, h) {
      canvas = c; canvas.width = w; canvas.height = h; W = w; H = h;
      gl = canvas.getContext('webgl', { premultipliedAlpha: false, alpha: true });
      if (!gl) throw new Error('WebGL 不可用');
      var vs = _compile(gl.VERTEX_SHADER, VS);
      progDraw = _link(vs, _compile(gl.FRAGMENT_SHADER, FS_DRAW));
      progShow = _link(vs, _compile(gl.FRAGMENT_SHADER, FS_SHOW));
      progCopy = _link(vs, _compile(gl.FRAGMENT_SHADER, FS_COPY));
      progMerge = _link(vs, _compile(gl.FRAGMENT_SHADER, FS_MERGE));
      quadBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      texRGB = gl.createTexture(); _initTexParams(texRGB);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, W, H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      texAlpha = gl.createTexture(); _initTexParams(texAlpha);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, W, H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      fbo = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texAlpha, 0);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      // 模板合并用的辅助纹理/帧缓存: texTpl 承载模板图案, texMerge 为合并前的当前蒙板副本
      texTpl = gl.createTexture(); _initTexParams(texTpl);
      texMerge = gl.createTexture(); _initTexParams(texMerge);
      gl.bindTexture(gl.TEXTURE_2D, texMerge);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, W, H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      fboMerge = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fboMerge);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texMerge, 0);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      DLOC.res = gl.getUniformLocation(progDraw, 'u_res');
      DLOC.center = gl.getUniformLocation(progDraw, 'u_center');
      DLOC.radius = gl.getUniformLocation(progDraw, 'u_radius');
      DLOC.alpha = gl.getUniformLocation(progDraw, 'u_alpha');
      SLOC.rgb = gl.getUniformLocation(progShow, 'u_rgb');
      SLOC.alpha = gl.getUniformLocation(progShow, 'u_alpha');
      SLOC.red = gl.getUniformLocation(progShow, 'u_red');
      CLOC.src = gl.getUniformLocation(progCopy, 'u_src');
      MLOC.cur = gl.getUniformLocation(progMerge, 'u_cur');
      MLOC.tpl = gl.getUniformLocation(progMerge, 'u_tpl');
      MLOC.res = gl.getUniformLocation(progMerge, 'u_res');
      MLOC.rect = gl.getUniformLocation(progMerge, 'u_rect');
      MLOC.merge = gl.getUniformLocation(progMerge, 'u_merge');
    }

    /**
     * 调整 alpha 纹理尺寸(图片尺寸变化时; RGB 纹理由 loadRGB 重新上传).
     * 引用: fitCanvas.
     * @param {number} w
     * @param {number} h
     */
    function resize(w, h) {
      if (W === w && H === h) return;
      W = w; H = h; canvas.width = w; canvas.height = h;
      gl.bindTexture(gl.TEXTURE_2D, texAlpha);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, W, H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texAlpha, 0);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      // texMerge 与 texAlpha 保持同尺寸, 作实时蒙板副本
      gl.bindTexture(gl.TEXTURE_2D, texMerge);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, W, H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    }

    function _toTex() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.viewport(0, 0, W, H);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ZERO);
      _bindQuad(progDraw);
      gl.uniform2f(DLOC.res, W, H);
    }

    function _scissor(cx, cy, r) {
      var sx = Math.max(0, Math.floor(cx - r - 1));
      var sy = Math.max(0, Math.floor(H - cy - r - 1));
      var sw = Math.min(W - sx, Math.ceil(2 * r + 2));
      var sh = Math.min(H - sy, Math.ceil(2 * r + 2));
      gl.enable(gl.SCISSOR_TEST);
      gl.scissor(sx, sy, sw, sh);
    }

    /** 画圆: 笔刷区域内 alpha 覆盖为 val. 引用: stampDisc. */
    function stampDisc(cx, cy, r, val) {
      _toTex();
      _scissor(cx, cy, r);
      gl.uniform2f(DLOC.center, cx, cy);
      gl.uniform1f(DLOC.radius, r);
      gl.uniform1f(DLOC.alpha, val / 255);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.disable(gl.SCISSOR_TEST);
    }

    /** 全清(alpha=0). 引用: btn-clear / _initAlphaCanvas. */
    function clear() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.viewport(0, 0, W, H);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }

    /** 全填充 alpha=val. 引用: btn-fill / btn-default / _initAlphaCanvas. */
    function fill(val) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.viewport(0, 0, W, H);
      gl.clearColor(1, 1, 1, val / 255);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }

    /** 上传 RGB 图片到 RGB 纹理. 引用: _applyCurrent. */
    function loadRGB(image) {
      gl.bindTexture(gl.TEXTURE_2D, texRGB);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    }

    /** 上传蒙版 Image/Canvas 到 alpha 纹理. 引用: _initAlphaCanvas. */
    function loadAlpha(image) {
      gl.bindTexture(gl.TEXTURE_2D, texAlpha);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    }

    /**
     * 读取画布局部矩形蒙板 alpha(Canvas 坐标, 顶部为 y0). 仅回读 tw*th 而不整幅 readPixels,
     * 供模板应用按交集局部合并, 避免全画布回读的带宽与逐像素打包开销.
     * 引用: _applyTemplate.
     * @param {number} x0 左 x(含).
     * @param {number} y0 上 y(画布顶部, 含).
     * @param {number} tw 宽.
     * @param {number} th 高.
     * @returns {Uint8Array} tw*th 行序 alpha(与 Canvas 同向, 自上而下).
     */
    function readAlphaRegion(x0, y0, tw, th) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      var raw = new Uint8Array(tw * th * 4);
      gl.readPixels(x0, H - (y0 + th), tw, th, gl.RGBA, gl.UNSIGNED_BYTE, raw);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      var alpha = new Uint8Array(tw * th);
      for (var r = 0; r < th; r++) {
        var src = (th - 1 - r) * tw * 4, dst = r * tw;
        for (var c = 0; c < tw; c++) alpha[dst + c] = raw[src + c * 4 + 3];
      }
      return alpha;
    }

    /**
     * 把局部蒙板 alpha(Canvas 行序)经 texSubImage2D 更新到 alpha 纹理的指定矩形,
     * 仅上传 tw*th 区域而不用整幅 texImage2D. 引用: _applyTemplate.
     * @param {number} x0 左 x(含).
     * @param {number} y0 上 y(画布顶部, 含).
     * @param {number} tw 宽.
     * @param {number} th 高.
     * @param {Uint8Array} local tw*th Canvas 行序 alpha(与 readAlphaRegion 同向).
     */
    /**
     * 在 GPU 上把模板图案按 min/max 合并到当前蒙板的交集矩形. 因同一纹理不能同时作为
     * 渲染目标与采样源, 先经 texMerge 拷贝当前蒙板一份, 再在 fbo 上以 scissor 约束到
     * 交集矩形由片元着色器合成写回, 无任何 CPU 逐像素循环. 引用: _applyTemplate.
     * @param {number} x0 交集左 x(含, 画布顶部坐标).
     * @param {number} y0 交集上 y(含).
     * @param {number} tw 交集宽.
     * @param {number} th 交集高.
     * @param {number} rx 模板矩形左(画布顶部坐标).
     * @param {number} ry 模板矩形上.
     * @param {number} rw 模板矩形宽.
     * @param {number} rh 模板矩形高.
     * @param {Uint8ClampedArray} tplData 模板 RGBA 数据(alpha 为图案).
     * @param {number} tplW 模板像素宽.
     * @param {number} tplH 模板像素高.
     * @param {boolean} isMax 是否 max 合并(min 否则).
     */
    function applyTemplate(x0, y0, tw, th, rx, ry, rw, rh, tplData, tplW, tplH, isMax) {
      // 1) 上传模板图案纹理(v=0 对应数据首行=模板顶部, 与画布同向, 故片段 y 不必翻转)
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.bindTexture(gl.TEXTURE_2D, texTpl);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, tplW, tplH, 0, gl.RGBA, gl.UNSIGNED_BYTE, tplData);
      // 2) 全屏拷贝当前蒙板 texAlpha → texMerge, 作为合并采样源(规避同纹理读写冲突)
      gl.bindFramebuffer(gl.FRAMEBUFFER, fboMerge);
      gl.viewport(0, 0, W, H);
      gl.disable(gl.BLEND);
      _bindQuad(progCopy);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, texAlpha);
      gl.uniform1i(CLOC.src, 0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      // 3) 在 fbo 上仅对交集矩形合成并写回 texAlpha
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.viewport(0, 0, W, H);
      gl.scissor(x0, H - (y0 + th), tw, th);
      gl.enable(gl.SCISSOR_TEST);
      _bindQuad(progMerge);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, texMerge);
      gl.uniform1i(MLOC.cur, 0);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, texTpl);
      gl.uniform1i(MLOC.tpl, 1);
      gl.uniform2f(MLOC.res, W, H);
      gl.uniform4f(MLOC.rect, rx, ry, rw, rh);
      gl.uniform1f(MLOC.merge, isMax ? 1.0 : 0.0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.disable(gl.SCISSOR_TEST);
    }

    /**
     * 读取 alpha 通道数据(y 翻转为 Canvas 坐标). 引用: doSave.
     * @returns {Uint8Array} W*H 的 alpha 值数组.
     */
    function readAlpha() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      var pixels = new Uint8Array(W * H * 4);
      gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      var alpha = new Uint8Array(W * H);
      for (var y = 0; y < H; y++)
        for (var x = 0; x < W; x++)
          alpha[y * W + x] = pixels[(H - 1 - y) * W * 4 + x * 4 + 3];
      return alpha;
    }

    /**
     * 合成 RGB 纹理与 alpha 纹理到 canvas. 红色模式下红色浓度随蒙板 alpha 反转(不透明处
     * 保持原图, 透明处叠加半透明红)且画布像素全不透明(框选模板期间用, 详见 FS_SHOW).
     * 引用: _doRender.
     * @param {boolean} redMode 是否红色模式.
     */
    function render(redMode) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, W, H);
      gl.disable(gl.BLEND);
      _bindQuad(progShow);
      gl.uniform1f(SLOC.red, redMode ? 1.0 : 0.0);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texRGB);
      gl.uniform1i(SLOC.rgb, 0);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, texAlpha);
      gl.uniform1i(SLOC.alpha, 1);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    return {
      init: init, resize: resize, loadRGB: loadRGB, loadAlpha: loadAlpha,
      readAlphaRegion: readAlphaRegion, applyTemplate: applyTemplate,
      stampDisc: stampDisc, clear: clear, fill: fill,
      readAlpha: readAlpha, render: render,
      get canvas() { return canvas; }, get w() { return W; }, get h() { return H; }
    };
  })();
  // 画布背景: checker 棋盘格 / white 纯白 / black 纯黑 / custom 自定义色(CSS 背景实现)
  var _bgStyle = 'checker', _bgColor = '#ffffff';
  var PRELOAD_RADIUS = 15;
  // 加载令牌: 递增序号, 过期请求/过期 onload 据此丢弃, 保证只有最新一次切图生效
  var _loadToken = 0;

  // ---- 笔刷半径: sqrt(w*h) * 百分比 ----
  function _calcBrush() {
    if (!_cur || !_cur.w || !_cur.h) return;
    _brushRadius = Math.max(2, Math.round(Math.sqrt(_cur.w * _cur.h) * _brushPct / 100));
  }

  var canvas = document.getElementById('paint-canvas');
  AlphaGL.init(canvas, 1, 1);
  var wrap = document.getElementById('canvas-wrap');
  var _brushPreview = document.getElementById('brush-preview');
  var _templateInst = document.getElementById('template-inst');
  var _captureRectEl = document.getElementById('capture-rect');
  var _templateDlg = document.getElementById('template-dlg');
  var _bgStyleEl = document.getElementById('bg-style');
  var _bgColorEl = document.getElementById('bg-color');
  if (_bgStyleEl) _bgStyle = _bgStyleEl.value;
  if (_bgColorEl) _bgColor = _bgColorEl.value;
  _applyBackdrop();

  // ---- 配置 ----
  async function loadConfig() {
    var cfg = await API.request(API.apiPath('/paint/config'));
    _defaultAlpha = cfg.default_alpha == null ? 255 : cfg.default_alpha;
    document.getElementById('default-alpha').value = _defaultAlpha;
  }
  async function saveConfig() {
    var value = parseInt(document.getElementById('default-alpha').value);
    _defaultAlpha = Number.isNaN(value) ? 255 : value;
    await API.request(API.apiPath('/paint/config'), { method: 'POST',
      body: JSON.stringify({ default_alpha: _defaultAlpha }) });
  }

  // ---- 加载 ----
  async function loadImages() {
    var res = await API.request(API.apiPath('/paint/images') + '?dir=' + encodeURIComponent(API.getDir()));
    _images = res;
    if (_strip) _strip.refresh();
    if (_images.length > 0) loadIndex(0);
    else {
      updateStatusBar();
      toast('没有图片');
    }
  }

  /**
   * 构建蒙版画布 cur.alpha: 有蒙版时继承 cur.mask 的 A 通道(蒙版),
   * 无蒙版时填默认 alpha. 蒙版唯一权威为 cur.alpha 的 A 通道.
   * Callers: loadIndex(缓存命中与加载完成).
   */
  function _initAlphaCanvas(cur) {
    if (AlphaGL.w !== cur.w || AlphaGL.h !== cur.h) AlphaGL.resize(cur.w, cur.h);
    cur.alpha = AlphaGL.canvas;
    if (cur.item.has_mask) {
      AlphaGL.loadAlpha(cur.mask);
    } else {
      var value = parseInt(document.getElementById('default-alpha').value);
      _defaultAlpha = Number.isNaN(value) ? 255 : value;
      AlphaGL.clear();
      if (_defaultAlpha > 0) AlphaGL.fill(_defaultAlpha);
    }
  }

  /**
   * 将加载完成的状态对象整体交换为当前状态, 并一次性刷新状态栏与画布.
   * Callers: loadIndex(缓存命中与加载完成).
   */
  function _applyCurrent(cur) {
    _cur = cur;
    _calcBrush(); _updateBrushUI();
    AlphaGL.loadRGB(cur.rgbImg);
    fitCanvas(); _scheduleRender(); updateStatusBar();
  }

  async function loadIndex(idx) {
    if (_images.length === 0) return;
    idx = Math.max(0, Math.min(_images.length - 1, idx));
    // 切图终止进行中的绘制: 清理橡皮筋/落笔状态并解绑 document 级跟踪,
    // 防止抬笔事件在新图上执行绘制(跨图污染)或残留红线预览
    if (_isDrawing) {
      _isDrawing = false;
      document.removeEventListener('mousemove', _docMouseMove);
      document.removeEventListener('mouseup', _docMouseUp);
    }
    _rectStart = null; _lineMode = false; _lineStart = null; _mouseOrig = null;
    // 切图终断模板实例/框选状态, 防止未确认模板或选区残留到新图;
    // 拖动进行中须先卸载 doc 级跟踪再清状态, 顺序不可颠倒
    if (_floatDrag) {
      document.removeEventListener('pointermove', _floatMove);
      document.removeEventListener('pointerup', _floatUp);
      _floatDrag = null;
    }
    _floating = null; _captureMode = false; _captureStart = null;
    _templateInst.style.display = 'none'; _captureRectEl.style.display = 'none';
    var img = _images[idx];
    var token = ++_loadToken;
    // 每次加载持有独立状态副本, 完成后经 _applyCurrent 整体交换到 _cur;
    // 加载完成前 _cur 保持上一张图, 状态栏与工作区始终一致(不会"状态栏已变、工作区未变")
    var cur = {
      index: idx, item: img, w: 0, h: 0,
      rgbImg: null, mask: null, alpha: null, modified: false,
    };

    var cached = _preloadCache[img.path];
    if (cached) {
      cur.w = cached.w; cur.h = cached.h;
      cur.rgbImg = cached.img;
      cur.mask = cached.mask;
      _initAlphaCanvas(cur);
      _applyCurrent(cur);
      _preloadNeighbors(idx);
      return;
    }

    var entry = await _loadEntry(img);
    if (token !== _loadToken) return; // 已被更新的加载取代
    cur.w = entry.w; cur.h = entry.h;
    cur.rgbImg = entry.img;
    cur.mask = entry.mask;
    _preloadCache[img.path] = entry;
    _initAlphaCanvas(cur);
    _applyCurrent(cur);
    _preloadNeighbors(idx);
  }

  // ---- 预加载 ----
  var _preloadSeq = 0;

  /**
   * 统计当前索引邻域内已进入缓冲区的数量。
   * @returns {{ready: number, want: number, total: number}}
   */
  function bufferStats() {
    var idx = _cur ? _cur.index : -1;
    var ready = 0;
    var want = 0;
    for (var di = -PRELOAD_RADIUS; di <= PRELOAD_RADIUS; di++) {
      var ni = idx + di;
      if (ni < 0 || ni >= _images.length) continue;
      want++;
      if (_preloadCache[_images[ni].path]) ready++;
    }
    return {
      ready: ready,
      want: want,
      total: Object.keys(_preloadCache).length,
    };
  }

  /**
   * 刷新底部六段状态栏。引用：各切图/加载/保存入口、预加载完成。
   */
  function updateStatusBar() {
    var cur = _cur;
    var img = cur ? _images[cur.index] : null;
    var done = 0;
    for (var i = 0; i < _images.length; i++) {
      if (_images[i].has_mask) done++;
    }
    document.getElementById('seg-pos').textContent =
      (_images.length && cur ? (cur.index + 1) : 0) + '/' + _images.length;
    document.getElementById('seg-done').textContent = '已标注 ' + done;
    document.getElementById('seg-name').textContent = img ? img.stem : '-';
    document.getElementById('seg-obj').textContent = img
      ? (img.has_mask ? '已标注' : '待标注')
      : '-';
    document.getElementById('seg-metric').textContent = (cur && cur.w)
      ? (cur.w + '×' + cur.h)
      : '-';
    var buf = bufferStats();
    document.getElementById('seg-extra').textContent =
      '缓冲 ' + buf.ready + '/' + buf.want + '（缓存 ' + buf.total + '）';
    if (_strip) _strip.sync();
  }

  async function _preloadNeighbors(idx) {
    var seq = ++_preloadSeq;
    for (var di = 1; di <= PRELOAD_RADIUS; di++) {
      if (seq !== _preloadSeq) return;
      var ni = idx + di; if (ni < _images.length) await _preloadOne(_images[ni]);
      ni = idx - di; if (ni >= 0) await _preloadOne(_images[ni]);
    }
  }

  /**
   * 下载接口 blob. Callers: _loadEntry.
   */
  function _fetchBlob(apiPath, imgPath) {
    return fetch(API.apiPath(apiPath) + '?path=' + encodeURIComponent(imgPath))
      .then(function(r) {
        if (!r.ok) throw new Error('加载失败: ' + r.status);
        return r.blob();
      });
  }

  /**
   * 将 blob 解码为 Image. Callers: _loadEntry.
   */
  function _decodeImage(blob) {
    return new Promise(function(resolve, reject) {
      var url = URL.createObjectURL(blob);
      var el = new Image();
      el.onload = function() { URL.revokeObjectURL(url); resolve(el); };
      el.onerror = function() { URL.revokeObjectURL(url); reject(new Error('图片解码失败')); };
      el.src = url;
    });
  }

  /**
   * 加载单张图的视觉与蒙版: 视觉采用 AVIF(有损可接受, 已压缩), 蒙版采用无损 PNG(不可有损),
   * 前端合成. 返回 { img, mask, w, h }. Callers: loadIndex、_preloadOne.
   */
  async function _loadEntry(img) {
    var visualP = _fetchBlob('/paint/image', img.path);
    var maskP = img.has_mask ? _fetchBlob('/paint/mask', img.path) : Promise.resolve(null);
    var results = await Promise.all([visualP, maskP]);
    var visualEl = await _decodeImage(results[0]);
    var maskEl = results[1] ? await _decodeImage(results[1]) : null;
    // 蒙板工作尺寸取自列表(后端已钳制到预览量级); 视觉/蒙版/画布四者同尺寸
    return { img: visualEl, mask: maskEl, w: img.width, h: img.height };
  }

  async function _preloadOne(img) {
    if (_preloadCache[img.path]) return;
    _preloadCache[img.path] = await _loadEntry(img);
    updateStatusBar();
  }
  function _updateBrushUI() {
    var labelEl = document.getElementById('brush-label');
    if (labelEl) labelEl.textContent = _brushPct.toFixed(1) + '% ~' + _brushRadius + 'px';
    // 同步滑动条位置: 滚轮/其它途径改笔刷时滑块跟随, 避免数值与滑块脱节
    var sliderEl = document.getElementById('brush-slider');
    if (sliderEl && parseFloat(sliderEl.value) !== _brushPct) sliderEl.value = _brushPct.toFixed(1);
  }
  function fitCanvas() {
    if (!_cur) return;
    var cw = wrap.clientWidth, ch = wrap.clientHeight;
    _dpr = window.devicePixelRatio || 1;
    _scale = Math.min(cw / _cur.w, ch / _cur.h);
    _viewW = Math.max(1, Math.floor(_cur.w * _scale));
    _viewH = Math.max(1, Math.floor(_cur.h * _scale));
    AlphaGL.resize(_cur.w, _cur.h);
    canvas.style.width = _viewW + 'px';
    canvas.style.height = _viewH + 'px';
    canvas.style.imageRendering = 'auto';
  }
  window.addEventListener('resize', function() { if (_cur && _cur.rgbImg) { fitCanvas(); _scheduleRender(); } });

  // ---- 渲染 ----

  function _scheduleRender() {
    if (!_renderScheduled) {
      _renderScheduled = true;
      requestAnimationFrame(function() {
        _renderScheduled = false;
        _doRender();
      });
    }
  }

  function _doRender() {
    var cur = _cur;
    if (!cur || !cur.rgbImg || !cur.alpha) return;
    // 红色模式: 框选进行中或框选待确认实例存在期间, 整图蒙板以红色半透明渲染
    AlphaGL.render(_captureMode || !!(_floating && _floating.capture));
    _updateLinePreview();
  }

  // SVG 红线预览(叠加在 canvas 上方, 不受 alpha/mask 影响). 引用: _doRender.
  var _lineSvg = null;
  function _updateLinePreview() {
    if (!_lineSvg) {
      _lineSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      _lineSvg.style.position = 'absolute';
      _lineSvg.style.left = '0';
      _lineSvg.style.top = '0';
      _lineSvg.style.width = '100%';
      _lineSvg.style.height = '100%';
      _lineSvg.style.pointerEvents = 'none';
      _lineSvg.style.zIndex = '10';
      wrap.appendChild(_lineSvg);
    }
    if (!_lineMode || !_lineStart || !_mouseOrig || !_cur) {
      _lineSvg.style.display = 'none';
      return;
    }
    _lineSvg.style.display = '';
    // 两端点均从原图坐标映射回视口: 终点 _mouseOrig 由事件上游 screenToOrig 钳位产出,
    // 与笔刷预览同一条数据流, 鼠标移出图片时同步停在边缘
    var wrapRect = wrap.getBoundingClientRect();
    var p1 = _origToScreen(_lineStart[0], _lineStart[1]);
    var p2 = _origToScreen(_mouseOrig[0], _mouseOrig[1]);
    var lw = Math.max(1, _brushRadius * 2 * (canvas.getBoundingClientRect().width / Math.max(1, _cur.w)));
    _lineSvg.setAttribute('viewBox', '0 0 ' + wrapRect.width + ' ' + wrapRect.height);
    _lineSvg.innerHTML =
      '<line x1="' + (p1[0] - wrapRect.left) + '" y1="' + (p1[1] - wrapRect.top) +
      '" x2="' + (p2[0] - wrapRect.left) + '" y2="' + (p2[1] - wrapRect.top) +
      '" stroke="rgba(255,0,0,0.3)" stroke-width="' + lw + '" stroke-linecap="round"/>';
  }

  // 原图坐标 → 画布视口坐标(中心 +0.5 对齐). 引用: _updateLinePreview, _updateBrushPreview.
  function _origToScreen(x, y) {
    var rect = canvas.getBoundingClientRect();
    return [rect.left + (x + 0.5) / _cur.w * rect.width,
            rect.top + (y + 0.5) / _cur.h * rect.height];
  }
  function _applyBackdrop() {
    if (_bgStyle === 'checker') {
      var value = parseInt(document.getElementById('default-alpha').value);
      var dk = (Number.isNaN(value) ? 255 : value) / 255;
      var v0 = Math.round(204 * dk), v1 = Math.round(153 * dk);
      canvas.style.background =
        'repeating-conic-gradient(rgb(' + v0 + ',' + v0 + ',' + v0 + ') 0 25%, rgb(' + v1 + ',' + v1 + ',' + v1 + ') 0 50%) 0 0 / 24px 24px';
    } else {
      var color = _bgStyle === 'white' ? '#ffffff' : (_bgStyle === 'black' ? '#000000' : _bgColor);
      canvas.style.background = color;
    }
  }

  // ---- 笔刷预览 ----
  function _updateBrushPreview() {
    if (!_mouseOrig || !_cur) {
      _brushPreview.style.display = 'none'; return;
    }
    // _mouseOrig 由事件上游 screenToOrig 钳位产出, 与实际绘制位置一致
    var wrapRect = wrap.getBoundingClientRect();
    var p = _origToScreen(_mouseOrig[0], _mouseOrig[1]);
    var mx = p[0] - wrapRect.left, my = p[1] - wrapRect.top;
    var r = _brushRadius * (canvas.getBoundingClientRect().width / Math.max(1, _cur.w));
    if (r < 2) { _brushPreview.style.display = 'none'; return; }
    _brushPreview.style.display = 'block';
    _brushPreview.style.width = (r * 2) + 'px';
    _brushPreview.style.height = (r * 2) + 'px';
    _brushPreview.style.left = (mx - r) + 'px';
    _brushPreview.style.top = (my - r) + 'px';
  }

  function screenToOrig(sx, sy) {
    var cur = _cur;
    if (!cur) return [0, 0];
    var rect = canvas.getBoundingClientRect();
    var x = (sx - rect.left) / Math.max(1e-6, rect.width) * cur.w;
    var y = (sy - rect.top) / Math.max(1e-6, rect.height) * cur.h;
    return [Math.max(0, Math.min(cur.w - 1, Math.floor(x))),
            Math.max(0, Math.min(cur.h - 1, Math.floor(y)))];
  }

  function stampDisc(cx, cy, r, val) {
    if (!_cur || !_cur.alpha) return;
    AlphaGL.stampDisc(cx, cy, r, val);
  }

  function strokeLine(x0, y0, x1, y1, r, val) {
    var dist = Math.max(Math.abs(x1-x0), Math.abs(y1-y0), 1);
    for (var i = 0; i <= dist; i++) {
      var t = i/dist;
      stampDisc(Math.round(x0+(x1-x0)*t), Math.round(y0+(y1-y0)*t), r, val);
    }
  }

  /**
   * 读取指定鼠标按键对应的 Alpha 值; 左键 0 默认 0, 右键默认 255, 越界钳位到 0-255.
   * @param {number} button 鼠标按键(0=左, 其它=右)
   * @returns {number}
   */
  function _alphaFor(button) {
    var id = button === 0 ? 'left-alpha' : 'right-alpha';
    var fallback = button === 0 ? 0 : 255;
    var v = parseInt(document.getElementById(id).value, 10);
    return Number.isNaN(v) ? fallback : Math.max(0, Math.min(255, v));
  }

  // ---- 蒙板模板 ----

  /** 槽位索引 → 键盘键位: 0-8 → '1'-'9', 9 → '0'. 引用: 提示/卡片/快捷键. */
  function _slotKey(i) { return i === 9 ? '0' : String(i + 1); }

  /**
   * 从模板管理对话框进入框选模式: 隐藏对话框开启 _captureMode, 随后在画布拖框选区域,
   * 抬笔生成待确认模板实例(确认后存槽位).
   * Callers: 模板管理对话框「框选当前图区域」点击.
   */
  function beginTemplateCapture() {
    if (!_cur || !_cur.alpha) { toast('当前没有可框选的画布'); return; }
    _templateDlg.hidden = true;
    _captureMode = true;
    _templateInst.style.display = 'none';
    _scheduleRender();
    toast('在图上拖拽框选区域 (Esc 取消)');
  }

  /** 取消框选模式并隐藏选区矩形. Callers: keydown Esc. */
  function cancelTemplateCapture() {
    _captureMode = false; _captureStart = null;
    if (_captureRectEl) _captureRectEl.style.display = 'none';
    _scheduleRender();
  }

  /**
   * 更新框选选区矩形显示(红色虚线, 指针穿过). 引用: _docMouseMove.
   */
  function _updateCaptureRect() {
    if (!_captureMode || !_captureStart || !_mouseOrig || !_cur) { _captureRectEl.style.display = 'none'; return; }
    var wrapRect = wrap.getBoundingClientRect();
    var rect = canvas.getBoundingClientRect();
    var x0 = Math.min(_captureStart[0], _mouseOrig[0]), y0 = Math.min(_captureStart[1], _mouseOrig[1]);
    var x1 = Math.max(_captureStart[0], _mouseOrig[0]), y1 = Math.max(_captureStart[1], _mouseOrig[1]);
    _captureRectEl.style.display = 'block';
    _captureRectEl.style.left = (rect.left + x0 / _cur.w * rect.width - wrapRect.left) + 'px';
    _captureRectEl.style.top = (rect.top + y0 / _cur.h * rect.height - wrapRect.top) + 'px';
    _captureRectEl.style.width = ((x1 - x0 + 1) / _cur.w * rect.width) + 'px';
    _captureRectEl.style.height = ((y1 - y0 + 1) / _cur.h * rect.height) + 'px';
  }

  /**
   * 框选结束: 生成待确认模板实例(红框), 可继续拖动/缩放调整捕获范围, 单击加锚点(≤2),
   * Enter/空格确认时按最终几何抠图存入预定槽位(首个空闲), Esc 取消; 区域过小或槽位已满则丢弃.
   * Callers: _finishTemplateCapture.
   * @param {number} x0 框选左上 orig x
   * @param {number} y0 框选左上 orig y
   * @param {number} x1 框选右下 orig x
   * @param {number} y1 框选右下 orig y
   */
  function _beginCaptureConfirm(x0, y0, x1, y1) {
    if (!_cur || !_cur.alpha) return;
    var px0 = Math.max(0, Math.floor(Math.min(x0, x1)));
    var py0 = Math.max(0, Math.floor(Math.min(y0, y1)));
    var px1 = Math.min(_cur.w, Math.ceil(Math.max(x0, x1)));
    var py1 = Math.min(_cur.h, Math.ceil(Math.max(y0, y1)));
    var pw = px1 - px0, ph = py1 - py0;
    if (pw < 2 || ph < 2) { toast('框选区域过小'); return; }
    var slot = -1;
    for (var i = 0; i < 10; i++) { if (!_templates[i]) { slot = i; break; } }
    if (slot === -1) { toast('模板已满 (最多 10 个)'); return; }
    _floating = { capture: true, slot: slot, rx: px0, ry: py0, rw: pw, rh: ph,
      url: null, data: null, w: 0, h: 0, anchors: [] };
    _updateFloatingHTML();
    _renderFloatingAnchors();
    toast('单击加锚点(≤2), 拖动/缩放调整区域, Enter 确认保存到槽位 ' + (slot + 1));
  }

  /**
   * 确认待确认模板: 按实例最终几何(拖出画布部分裁剪)抠取蒙板图案, 连同锚点存入预定槽位
   * 并持久化. Callers: keydown Enter/空格.
   */
  function _confirmCapture() {
    var f = _floating;
    var px0 = Math.max(0, Math.floor(f.rx)), py0 = Math.max(0, Math.floor(f.ry));
    var px1 = Math.min(_cur.w, Math.ceil(f.rx + f.rw)), py1 = Math.min(_cur.h, Math.ceil(f.ry + f.rh));
    var pw = px1 - px0, ph = py1 - py0;
    if (pw < 2 || ph < 2) { toast('模板区域过小, 请调整后重试'); return; }
    // 仅读取最终区域蒙板, 避免整幅画布 readPixels 的带宽与打包开销
    var local = AlphaGL.readAlphaRegion(px0, py0, pw, ph);
    var data = new Uint8ClampedArray(pw * ph * 4);
    for (var y = 0; y < ph; y++) {
      for (var x = 0; x < pw; x++) {
        var j = (y * pw + x) * 4;
        data[j] = data[j + 1] = data[j + 2] = 255;
        data[j + 3] = local[y * pw + x];
      }
    }
    var tpl = { data: data, w: pw, h: ph, anchors: f.anchors };
    // url 为图案 dataURL, 呼出实例的 CSS 蒙版渲染直接复用, 避免逐像素重绘
    tpl.url = 'data:image/png;base64,' + _templateToB64(tpl);
    _templates[f.slot] = tpl;
    _renderTemplateCards();
    toast('已保存模板到槽位 ' + (f.slot + 1) + ' (按 ' + _slotKey(f.slot) + ' 放置)');
    _saveTemplates().catch(function (e) { toast('模板保存失败: ' + String(e.message || e)); });
    _discardFloating();
  }

  /**
   * 重建模板管理对话框的 10 个槽位卡片(键位 + 图案预览 + 删除). 引用: 捕获/删除后刷新.
   */
  function _renderTemplateCards() {
    var box = document.getElementById('tmpl-cards');
    if (!box) return;
    box.innerHTML = '';
    for (var i = 0; i < 10; i++) {
      var cell = document.createElement('div');
      cell.className = 'tmpl-card' + (_templates[i] ? '' : ' empty');
      var head = document.createElement('div');
      head.className = 'key'; head.textContent = '键 ' + _slotKey(i);
      cell.appendChild(head);
      if (_templates[i]) {
        var t = _templates[i];
        var cv = document.createElement('canvas');
        cv.width = t.w; cv.height = t.h;
        var id = cv.getContext('2d').createImageData(t.w, t.h);
        id.data.set(t.data);
        cv.getContext('2d').putImageData(id, 0, 0);
        cell.appendChild(cv);
        var del = document.createElement('button');
        del.className = 'del'; del.textContent = '×'; del.title = '删除模板';
        (function (slot) { del.addEventListener('click', function () { deleteTemplate(slot); }); })(i);
        cell.appendChild(del);
      }
      box.appendChild(cell);
    }
  }

  /** 删除指定槽位模板并刷新卡片. Callers: 卡片删除按钮. */
  function deleteTemplate(slot) {
    _templates[slot] = null;
    _renderTemplateCards();
    _saveTemplates().catch(function (e) { toast('模板保存失败: ' + String(e.message || e)); });
  }

  /**
   * 丢弃未确认模板实例并隐藏容器: 应用 / 取消 / 换放置共用的唯一清理入口.
   * Callers: placeTemplate/_applyTemplate/keydown Esc.
   */
  function _discardFloating() {
    _floating = null; _floatDrag = null;
    _templateInst.style.display = 'none';
    _scheduleRender();
  }

  /**
   * 将指定槽位模板作为未确认实例放到画布中心(红色矩形, 可拖动/缩放), 锚点从模板槽位载入.
   * 引用: keydown 1-9/0.
   * @param {number} slot 模板槽位索引 0-9.
   */
  function placeTemplate(slot) {
    var t = _templates[slot];
    if (!t || !_cur || !_cur.alpha) return;
    _discardFloating();
    var rw = Math.min(t.w, _cur.w), rh = Math.min(t.h, _cur.h);
    _floating = { slot: slot, rx: Math.round((_cur.w - rw) / 2), ry: Math.round((_cur.h - rh) / 2), rw: rw, rh: rh,
      url: t.url, data: t.data, w: t.w, h: t.h, anchors: t.anchors.slice() };
    _updateFloatingHTML();
    _renderFloatingAnchors();
  }

  /**
   * 依据未确认实例(orig 坐标)渲染未确认模板: 定位容器并更新几何. 图案的红色渲染由
   * .tmpl-red 的 CSS 蒙版合成完成(恒定半透明红底 + 白层 XOR 图案 alpha 的反转蒙版:
   * 不透明处透明红, 透明处半透明红, 与框选期间画布的红色渲染同式见 FS_SHOW), 图案
   * dataURL 在模板载入/捕获确认时生成一次, 拖动缩放零 JS 逐像素开销.
   * Callers: placeTemplate/_beginCaptureConfirm/_floatMove/_resizeFloating/
   *          default-alpha change(视图缩放变化重定位).
   */
  function _updateFloatingHTML() {
    if (!_floating || !_cur) { _templateInst.style.display = 'none'; return; }
    var wrapRect = wrap.getBoundingClientRect();
    var rect = canvas.getBoundingClientRect();
    var sx = rect.width / _cur.w, sy = rect.height / _cur.h;
    _templateInst.style.display = 'block';
    _templateInst.style.left = (rect.left + _floating.rx * sx - wrapRect.left) + 'px';
    _templateInst.style.top = (rect.top + _floating.ry * sy - wrapRect.top) + 'px';
    _templateInst.style.width = Math.max(1, Math.round(_floating.rw * sx)) + 'px';
    _templateInst.style.height = Math.max(1, Math.round(_floating.rh * sy)) + 'px';
    var red = _templateInst.querySelector('.tmpl-red');
    if (_floating.url) {
      red.style.display = 'block';
      red.style.setProperty('--img', 'url("' + _floating.url + '")');
    } else {
      // 框选待确认实例: 图案尚未抠取(确认时按最终几何读取), 仅显示边框
      red.style.display = 'none';
    }
  }

  /**
   * 依据 _floating.anchors 重建锚点标记(≤2 个). 锚点为容器内百分比定位的圆点,
   * 实例平移/缩放时随容器自动保持图案上的相对位置, 仅在增删后调用.
   * Callers: placeTemplate/_beginCaptureConfirm/_addAnchor/_removeAnchor.
   */
  function _renderFloatingAnchors() {
    var olds = _templateInst.querySelectorAll('.tmpl-anchor');
    for (var i = 0; i < olds.length; i++) olds[i].remove();
    if (!_floating) return;
    for (var j = 0; j < _floating.anchors.length; j++) {
      var a = _floating.anchors[j];
      var el = document.createElement('span');
      el.className = 'tmpl-anchor';
      el.setAttribute('data-anchor', String(j));
      el.title = '锚点 ' + (j + 1) + ': 拖动对齐(实例随之平移缩放), 单击移除';
      el.style.left = 'calc(' + (a.x * 100) + '% - 6px)';
      el.style.top = 'calc(' + (a.y * 100) + '% - 6px)';
      _templateInst.appendChild(el);
    }
  }

  /**
   * 把实例锚点同步回所属模板槽位并持久化; 框选待确认实例跳过(确认保存时随模板一并写入).
   * Callers: _addAnchor/_removeAnchor.
   */
  function _syncAnchors() {
    if (_floating.capture) return;
    _templates[_floating.slot].anchors = _floating.anchors;
    _saveTemplates().catch(function (e) { toast('锚点保存失败: ' + String(e.message || e)); });
  }

  /**
   * 在实例上 e 处添加锚点(0-1 分数坐标, ≤2): 落点距既有锚点 10px 内则改为移除该锚点;
   * 增删后即时同步所属模板槽位.
   * Callers: _floatUp(实例主体上无位移单击).
   * @param {PointerEvent} e 指针事件(clientX/clientY 为视口坐标).
   */
  function _addAnchor(e) {
    var rect = canvas.getBoundingClientRect();
    var sx = rect.width / _cur.w, sy = rect.height / _cur.h;
    var fx = ((e.clientX - rect.left) / sx - _floating.rx) / _floating.rw;
    var fy = ((e.clientY - rect.top) / sy - _floating.ry) / _floating.rh;
    var list = _floating.anchors;
    for (var i = 0; i < list.length; i++) {
      var a = list[i];
      if (Math.hypot((a.x - fx) * _floating.rw * sx, (a.y - fy) * _floating.rh * sy) <= 10) {
        list.splice(i, 1);
        _renderFloatingAnchors(); _syncAnchors();
        return;
      }
    }
    if (list.length >= 2) { toast('锚点已满 (最多 2 个)'); return; }
    list.push({ x: fx, y: fy });
    _renderFloatingAnchors(); _syncAnchors();
  }

  /**
   * 移除指定索引的锚点并同步所属模板槽位. Callers: _floatUp(锚点上无位移单击).
   * @param {number} idx 锚点索引.
   */
  function _removeAnchor(idx) {
    _floating.anchors.splice(idx, 1);
    _renderFloatingAnchors();
    _syncAnchors();
  }

  /** 缩放手柄映射: 每个手柄对应一个可动的水平边(l=左/r=右/null=不动)与垂直边(t=上/b=下/null=不动). 引用: _floatPointerDown. */
  var _RESIZE_HV = { nw: { h: 'l', v: 't' }, n: { h: null, v: 't' }, ne: { h: 'r', v: 't' },
    w: { h: 'l', v: null }, e: { h: 'r', v: null }, sw: { h: 'l', v: 'b' }, s: { h: null, v: 'b' }, se: { h: 'r', v: 'b' } };

  /** 未确认模板按下: 命中缩放柄(data-resize)则缩放, 命中锚点(data-anchor)则对齐拖动
   *  (见 _floatMove anchor 分支), 实例主体则平移; 抬笔时无位移单击增删锚点(见 _floatUp);
   *  记录起点并挂 doc 级跟踪. Callers: template-inst pointerdown. */
  function _floatPointerDown(e) {
    e.preventDefault(); e.stopPropagation();
    if (!_floating || !_cur) return;
    var t = e.target;
    var ak = t && t.getAttribute ? t.getAttribute('data-anchor') : null;
    var hv = null;
    if (ak === null && t && t.getAttribute) {
      var rk = t.getAttribute('data-resize');
      if (rk && _RESIZE_HV[rk]) hv = _RESIZE_HV[rk];
    }
    _floatDrag = { mode: ak !== null ? 'anchor' : hv ? 'resize' : 'move',
      anchor: ak !== null ? parseInt(ak, 10) : -1,
      sx: e.clientX, sy: e.clientY,
      x0: _floating.rx, y0: _floating.ry, w0: _floating.rw, h0: _floating.rh,
      h: hv ? hv.h : null, v: hv ? hv.v : null };
    document.addEventListener('pointermove', _floatMove);
    document.addEventListener('pointerup', _floatUp);
  }

  /** 未确认模板拖动中: move 平移(可超出画布) / anchor 对齐拖动(实例平移缩放使被拖锚点
   *  跟随指针, 另一锚点保持画布位置不动; 锚点的图案分数坐标不变) / resize 按手柄锚定对边
   *  缩放, 按住 Shift 改以实例中心为锚点缩放(角柄等比例, 边柄沿自身轴对称伸缩),
   *  均在 orig 域运算避免缩放的像素累积误差. Callers: document pointermove. */
  function _floatMove(e) {
    var d = _floatDrag;
    if (!d || !_cur) return;
    var rect = canvas.getBoundingClientRect();
    var sx = rect.width / _cur.w, sy = rect.height / _cur.h;
    var ox = (e.clientX - d.sx) / sx, oy = (e.clientY - d.sy) / sy;
    if (d.mode === 'anchor') {
      // 两点对齐: 以另一锚点为固定点, x/y 独立缩放+平移实例使被拖锚点精确到达指针
      // (模板应用本就允许非等比拉伸); 某轴两锚点重合时该轴退化为纯平移.
      var list = _floating.anchors, fixed = list[d.anchor === 0 ? 1 : 0];
      var px = (e.clientX - rect.left) / sx, py = (e.clientY - rect.top) / sy;
      var ax = _floating.rx + list[d.anchor].x * _floating.rw;
      var ay = _floating.ry + list[d.anchor].y * _floating.rh;
      if (fixed) {
        var fx = _floating.rx + fixed.x * _floating.rw;
        var fy = _floating.ry + fixed.y * _floating.rh;
        var sxx = ax !== fx ? (px - fx) / (ax - fx) : 1;
        var syy = ay !== fy ? (py - fy) / (ay - fy) : 1;
        sxx = Math.max(4 / _floating.rw, sxx);
        syy = Math.max(4 / _floating.rh, syy);
        _floating.rx = fx + (_floating.rx - fx) * sxx;
        _floating.ry = fy + (_floating.ry - fy) * syy;
        _floating.rw *= sxx;
        _floating.rh *= syy;
      } else {
        // 单锚点: 无固定参照, 实例平移使锚点跟随指针
        _floating.rx += px - ax;
        _floating.ry += py - ay;
      }
      _updateFloatingHTML();
      return;
    }
    if (d.mode === 'move') {
      _floating.rx = d.x0 + ox;
      _floating.ry = d.y0 + oy;
    } else if (e.shiftKey) {
      var r = _floatShiftResize(d, ox, oy);
      _floating.rx = r.rx; _floating.ry = r.ry; _floating.rw = r.rw; _floating.rh = r.rh;
    } else {
      var right = d.x0 + d.w0, bottom = d.y0 + d.h0;
      if (d.h === null) { _floating.rx = d.x0; _floating.rw = d.w0; }
      else if (d.h === 'l') { _floating.rx = Math.min(d.x0 + ox, right - 4); _floating.rw = right - _floating.rx; }
      else { _floating.rx = d.x0; _floating.rw = Math.max(4, d.w0 + ox); }
      if (d.v === null) { _floating.ry = d.y0; _floating.rh = d.h0; }
      else if (d.v === 't') { _floating.ry = Math.min(d.y0 + oy, bottom - 4); _floating.rh = bottom - _floating.ry; }
      else { _floating.ry = d.y0; _floating.rh = Math.max(4, d.h0 + oy); }
    }
    _updateFloatingHTML();
  }

  /**
   * Shift 中心锚点缩放: 以拖拽起始几何的中心为锚点, 被拖边跟随指针、对边镜像跟随,
   * 尺寸取指针到中心距离的两倍; 角柄按主导轴取等比例宽高, 边柄仅沿自身轴伸缩.
   * 引用: _floatMove.
   * @param {object} d 拖拽状态(d.h/d.v 为该手柄可动水平/垂直边).
   * @param {number} ox orig 域水平位移.
   * @param {number} oy orig 域垂直位移.
   * @returns {{rx:number,ry:number,rw:number,rh:number}} 新几何(orig 域).
   */
  function _floatShiftResize(d, ox, oy) {
    var ratio = d.w0 / d.h0;
    var cx = d.x0 + d.w0 / 2, cy = d.y0 + d.h0 / 2;
    var mx = null, my = null;
    if (d.h === 'l') mx = d.x0 + ox;
    else if (d.h === 'r') mx = d.x0 + d.w0 + ox;
    if (d.v === 't') my = d.y0 + oy;
    else if (d.v === 'b') my = d.y0 + d.h0 + oy;
    var w, h;
    if (mx !== null && my !== null) {
      var dx = Math.abs(mx - cx), dy = Math.abs(my - cy);
      if (dx * ratio > dy) { w = Math.max(4, 2 * dx); h = w / ratio; }
      else { h = Math.max(4, 2 * dy); w = h * ratio; }
    } else if (my !== null) { h = Math.max(4, 2 * Math.abs(my - cy)); w = d.w0; }
    else { w = Math.max(4, 2 * Math.abs(mx - cx)); h = d.h0; }
    return { rx: cx - w / 2, ry: cy - h / 2, rw: w, rh: h };
  }

  /**
   * 未确认模板抬笔: 卸载 doc 级跟踪; 无位移(<4px)单击判定 — 锚点上单击移除该锚点,
   * 实例主体上单击加锚点(≤2), 拖动/缩放(≥4px)不受影响; 锚点变更即时同步模板槽位.
   * Callers: document pointerup.
   */
  function _floatUp(e) {
    var d = _floatDrag;
    document.removeEventListener('pointermove', _floatMove);
    document.removeEventListener('pointerup', _floatUp);
    _floatDrag = null;
    if (!d || d.mode === 'resize') return;
    if (Math.hypot(e.clientX - d.sx, e.clientY - d.sy) >= 4) return;
    if (d.mode === 'anchor') _removeAnchor(d.anchor);
    else _addAnchor(e);
  }

  /**
   * 应用未确认模板: 读当前蒙板, 将模板图案(最近邻缩放)按 _mergeMode(min/max) 合并到目标矩形,
   * 重建 alpha 纹理并重绘. 引用: keydown Enter/空格.
   */
  function _applyTemplate() {
    if (!_floating || !_cur || !_cur.alpha) return;
    var f = _floating;
    // 模板允许拖出画布: 应用时先求模板与画布的相交区间, 仅把落在画布内的部分合入,
    // 溢出画布的部分自然裁剪忽略. 钳位并非限制模板位置, 而是裁剪到蒙板可写入的像素范围.
    var x0 = Math.max(0, Math.floor(f.rx)), y0 = Math.max(0, Math.floor(f.ry));
    var x1 = Math.min(_cur.w, Math.ceil(f.rx + f.rw)), y1 = Math.min(_cur.h, Math.ceil(f.ry + f.rh));
    // 完全在画布外则与蒙板无交集: 不做任何修改, 仅清理浮动模板
    if (x0 >= x1 || y0 >= y1) {
      _floating = null; _floatDrag = null; _templateInst.style.display = 'none';
      return;
    }
    var tw = x1 - x0, th = y1 - y0;
    // 由 GPU 片元着色器按模板矩形(f.rx..+f.rw)采样合成, 只对交集矩形写回;
    // 模板矩形以 orig 起始坐标为采样基准, 超界部分自然由交集裁剪.
    // 模板完全在画布内时 tx≈rx、tw≈rw, 与画布外的采样保持一致, 无位置偏移.
    AlphaGL.applyTemplate(x0, y0, tw, th, f.rx, f.ry, f.rw, f.rh,
      f.data, f.w, f.h, _mergeMode === 'max');
    _floating = null; _floatDrag = null;
    _templateInst.style.display = 'none';
    _cur.modified = true;
    _scheduleRender();
  }

  /** 把模板图案化为 base64 PNG(带上限 RGB=255/A=alpha, 无损持久化). 引用: _saveTemplates. */
  function _templateToB64(t) {
    var c = document.createElement('canvas');
    c.width = t.w; c.height = t.h;
    var ctx = c.getContext('2d');
    var id = ctx.createImageData(t.w, t.h);
    id.data.set(t.data);
    ctx.putImageData(id, 0, 0);
    return c.toDataURL('image/png').replace(/^data:image\/png;base64,/, '');
  }

  /**
   * 解码服务端返回的 base64 PNG 模板项为本地图案数据. url 为图案 dataURL(零成本复用
   * image_b64), 供呼出实例的 CSS 蒙版渲染; data 供模板应用与卡片预览.
   * 引用: _loadTemplates.
   * @param {object} item 服务端模板项 {slot, w, h, image_b64, anchors}.
   * @returns {Promise<object>} 本地模板 {url, data, w, h, anchors}.
   */
  async function _b64ToTemplate(item) {
    var c = document.createElement('canvas');
    c.width = item.w; c.height = item.h;
    var img = new Image();
    img.src = 'data:image/png;base64,' + item.image_b64;
    await img.decode();
    c.getContext('2d').drawImage(img, 0, 0);
    return { url: img.src,
      data: c.getContext('2d').getImageData(0, 0, item.w, item.h).data,
      w: item.w, h: item.h, anchors: item.anchors };
  }

  /**
   * 把全部非空槽位模板持久化到服务端(按数据集全量覆盖). 引用: _confirmCapture/deleteTemplate/_syncAnchors.
   */
  async function _saveTemplates() {
    var list = [];
    for (var i = 0; i < 10; i++) {
      var t = _templates[i];
      if (!t) continue;
      list.push({ slot: i, w: t.w, h: t.h, image_b64: _templateToB64(t), anchors: t.anchors });
    }
    await API.request(API.apiPath('/paint/templates'), {
      method: 'PUT',
      body: JSON.stringify({ templates: list }),
    });
  }

  /**
   * 从服务端按数据集加载蒙板模板并刷新槽位卡片. 引用: 页面初始化.
   */
  async function _loadTemplates() {
    var res = await API.request(API.apiPath('/paint/templates'));
    _templates = new Array(10);
    for (var i = 0; i < res.templates.length; i++) {
      var item = res.templates[i];
      if (item.slot < 0 || item.slot > 9) continue;
      _templates[item.slot] = await _b64ToTemplate(item);
    }
    _renderTemplateCards();
  }

  /** 框选结束: 生成待确认模板实例并退出框选模式. Callers: _docMouseUp. */
  function _finishTemplateCapture() {
    var s = _captureStart, e = _mouseOrig;
    _captureMode = false; _captureStart = null;
    if (_captureRectEl) _captureRectEl.style.display = 'none';
    if (s && e) _beginCaptureConfirm(s[0], s[1], e[0], e[1]);
  }

  // ---- 触屏 ----
  canvas.addEventListener('touchstart', function(e) {
    e.preventDefault(); if (!_cur || !_cur.alpha || e.touches.length !== 1) return;
    _isDrawing = true; _isTouch = true;
    var t = e.touches[0];
    var pos = screenToOrig(t.clientX, t.clientY);
    _paintValue = _alphaFor(0);
    stampDisc(pos[0], pos[1], _brushRadius, _paintValue);
    _mouseOrig = pos;
    _rectStart = pos; _scheduleRender();
  }, { passive: false });
  canvas.addEventListener('touchmove', function(e) {
    e.preventDefault(); if (!_cur || !_cur.alpha || !_isDrawing) return;
    var t = e.touches[0];
    _mouseOrig = screenToOrig(t.clientX, t.clientY);
    _updateBrushPreview();
    var pos = _mouseOrig;
    if (_rectStart) { strokeLine(_rectStart[0], _rectStart[1], pos[0], pos[1], _brushRadius, _paintValue); _rectStart = pos; }
    _scheduleRender();
  }, { passive: false });
  canvas.addEventListener('touchend', function(e) {
    e.preventDefault(); if (!_isTouch || !_isDrawing || !_cur || !_cur.alpha) return;
    _isDrawing = false; _isTouch = false; _rectStart = null; _mouseOrig = null; _brushPreview.style.display = 'none'; _cur.modified = true; _scheduleRender();
  }, { passive: false });
  canvas.addEventListener('touchcancel', function() { _isDrawing = false; _isTouch = false; _rectStart = null; _mouseOrig = null; _brushPreview.style.display = 'none'; });
  // 防止触屏时触发对应的 mouse 事件
  canvas.addEventListener('mousedown', function(e) { if (_isTouch) return; e.preventDefault(); if (!_cur || !_cur.alpha) return;
    _isDrawing = true;
    _mouseOrig = screenToOrig(e.clientX, e.clientY);
    var pos = _mouseOrig;
    if (_captureMode) {
      // 框选模板捕获: 记录起点并挂 doc 级跟踪, 抬笔时经 _finishTemplateCapture 存入槽位
      _captureStart = pos;
      document.addEventListener('mousemove', _docMouseMove);
      document.addEventListener('mouseup', _docMouseUp);
      return;
    }
    _paintValue = _alphaFor(e.button);
    if (e.shiftKey) {
      // shift 直线: 只记录起点, 橡皮筋预览, 抬笔时一次 strokeLine 画落笔到抬笔的直线
      _lineMode = true; _lineStart = pos;
      _updateLinePreview();
      document.addEventListener('mousemove', _docMouseMove);
      document.addEventListener('mouseup', _docMouseUp);
      return;
    }
    stampDisc(pos[0], pos[1], _brushRadius, _paintValue);
    _rectStart = pos; _scheduleRender();
    // 按住鼠标后用 document 事件跟踪, 离开画布仍可绘制
    document.addEventListener('mousemove', _docMouseMove);
    document.addEventListener('mouseup', _docMouseUp);
  });
  // 画布内 mouse 事件: 仅负责未按下时的 hover 预览; 按下后由 _docMouseMove 统一接管
  canvas.addEventListener('mousemove', function(e) {
    if (_isDrawing) return;
    _mouseOrig = screenToOrig(e.clientX, e.clientY);
    if (!_cur || !_cur.alpha) return;
    _updateBrushPreview();
  });
  // 移出画布: 仅未按下时隐藏预览; 按下中(按住绘制/直线)由 _docMouseMove 以钳位坐标
  // 继续更新预览, 与红线同步停在图片边缘, 不破坏数据流
  canvas.addEventListener('mouseleave', function() { if (!_isDrawing) _brushPreview.style.display = 'none'; });
  // 文档级跟踪: 按住鼠标后离开画布仍可绘制, 坐标统一经 screenToOrig 钳位到图片边缘
  function _docMouseMove(e) {
    _mouseOrig = screenToOrig(e.clientX, e.clientY);
    if (!_isDrawing || !_cur || !_cur.alpha) return;
    if (_captureMode) { _updateCaptureRect(); return; }
    _updateBrushPreview();
    // 直线模式只改预览不落笔画布: 同步刷新红线与笔刷预览, 不经 rAF 避免两者错帧
    if (_lineMode) { _updateLinePreview(); return; }
    var pos = _mouseOrig;
    if (_rectStart) { strokeLine(_rectStart[0], _rectStart[1], pos[0], pos[1], _brushRadius, _paintValue); _rectStart = pos; }
    _scheduleRender();
  }
  function _docMouseUp() {
    if (!_isDrawing || !_cur || !_cur.alpha || !_mouseOrig) return;
    if (_captureMode) {
      _finishTemplateCapture();
      _isDrawing = false; _rectStart = null; _mouseOrig = null;
      document.removeEventListener('mousemove', _docMouseMove);
      document.removeEventListener('mouseup', _docMouseUp);
      return;
    }
    // 终点统一沿用 _mouseOrig(最后 mousemove 的钳位坐标), 与红线/笔刷预览同一条数据流
    if (_lineMode) {
      strokeLine(_lineStart[0], _lineStart[1], _mouseOrig[0], _mouseOrig[1], _brushRadius, _paintValue);
    }
    _isDrawing = false; _rectStart = null; _lineMode = false; _lineStart = null; _mouseOrig = null;
    _brushPreview.style.display = 'none'; _cur.modified = true; _scheduleRender();
    document.removeEventListener('mousemove', _docMouseMove);
    document.removeEventListener('mouseup', _docMouseUp);
  }
  canvas.addEventListener('contextmenu', function(e) { e.preventDefault(); });
  canvas.addEventListener('wheel', function(e) {
    e.preventDefault();
    var d = e.deltaY < 0 ? 1 : -1;
    _brushPct = Math.max(0.5, Math.min(20, _brushPct + d * Math.max(0.1, _brushPct * 0.15)));
    _calcBrush(); _updateBrushUI(); _updateBrushPreview();
    if (_lineMode) _updateLinePreview(); // 直线模式下同步刷新红线线宽
  });

  // ---- 保存 ----
  async function doSave(force) {
    var cur = _cur;
    if (!cur || !cur.alpha) return;
    var img = cur.item;
    // 全新图片即使没有动笔，也要写出当前默认 alpha mask。
    if (!force && !cur.modified && img.has_mask) return;
    // 从 WebGL 纹理读取 alpha(唯一权威), 构造 RGB=0 的 PNG 上传(后端仅取 A)
    var alphaArr = AlphaGL.readAlpha();
    var tmpCanvas = document.createElement('canvas');
    tmpCanvas.width = cur.w; tmpCanvas.height = cur.h;
    var tctx = tmpCanvas.getContext('2d');
    var imgData = tctx.createImageData(cur.w, cur.h);
    for (var i = 0; i < alphaArr.length; i++) {
      var j = i * 4;
      imgData.data[j] = 0; imgData.data[j + 1] = 0; imgData.data[j + 2] = 0;
      imgData.data[j + 3] = alphaArr[i];
    }
    tctx.putImageData(imgData, 0, 0);
    return new Promise(function(resolve) {
      tmpCanvas.toBlob(function(blob) {
        if (!blob) { errshow('toBlob failed'); resolve(); return; }
        fetch(API.apiPath('/paint/upload-mask') + '?path=' + encodeURIComponent(img.path), {
          method: 'POST', headers: { 'Content-Type': 'image/png' }, body: blob
        }).then(function(r) { return r.json(); }).then(function(res) {
          cur.modified = false;
          img.has_mask = true; updateStatusBar();
          toast('已保存: ' + res.saved);
          var cached = _preloadCache[img.path];
          if (cached) {
            // 保存后刷新蒙版缓存: 视觉不变, 蒙版 A 换成最新(RGB 恒白)
            var mCanvas = document.createElement('canvas');
            mCanvas.width = cur.w; mCanvas.height = cur.h;
            var mctx = mCanvas.getContext('2d');
            var mData = mctx.createImageData(cur.w, cur.h);
            var md = mData.data;
            for (var i = 0; i < alphaArr.length; i++) {
              var j = i * 4, v = alphaArr[i];
              md[j] = 255; md[j + 1] = 255; md[j + 2] = 255; md[j + 3] = v;
            }
            mctx.putImageData(mData, 0, 0);
            cached.mask = mCanvas;
          }
          resolve();
        }).catch(function() { errshow('上传失败'); resolve(); });
      }, 'image/png');
    });
  }

  async function goPrev() { if (_cur && _cur.index > 0) { await doSave(false); loadIndex(_cur.index - 1); } }
  async function goNext() { if (_cur && _cur.index < _images.length - 1) { await doSave(false); loadIndex(_cur.index + 1); } }
  function goSkip() { if (_cur && _cur.index < _images.length - 1) loadIndex(_cur.index + 1); }

  // ---- 工具栏 ----
  document.getElementById('btn-prev').addEventListener('click', goPrev);
  document.getElementById('btn-next').addEventListener('click', goNext);
  document.getElementById('btn-skip').addEventListener('click', goSkip);
  // 横排预览条: 点击第一段(位置)展开, 点击缩略图跳转
  _strip = Filmstrip.create({
    container: document.getElementById('filmstrip'),
    getItems: function () { return _images; },
    thumbSrc: function (it) {
      return it.thumbnail_hash ? API.thumbnailUrl(it.thumbnail_hash) : API.imageUrl(it.path);
    },
    getActive: function () { return _cur ? _cur.index : 0; },
    onSelect: function (i) { loadIndex(i); },
  });
  document.getElementById('seg-pos').addEventListener('click', function () { _strip.toggle(); });
  document.getElementById('brush-slider').addEventListener('input', function() { _brushPct = parseFloat(this.value); _calcBrush(); _updateBrushUI(); _updateBrushPreview(); if (_lineMode) _updateLinePreview(); });
  document.getElementById('bg-style').addEventListener('change', function() {
    _bgStyle = this.value;
    _applyBackdrop();
  });
  document.getElementById('bg-color').addEventListener('change', function() { _bgColor = this.value; _applyBackdrop(); });
  document.getElementById('btn-save').addEventListener('click', function() { doSave(true); });
  document.getElementById('btn-clear').addEventListener('click', function() { if (_cur && _cur.alpha) { AlphaGL.clear(); _cur.modified = true; _scheduleRender(); } });
  document.getElementById('btn-fill').addEventListener('click', function() { if (_cur && _cur.alpha) { AlphaGL.fill(255); _cur.modified = true; _scheduleRender(); } });
  document.getElementById('btn-default').addEventListener('click', function() {
    var value = parseInt(document.getElementById('default-alpha').value);
    _defaultAlpha = Number.isNaN(value) ? 255 : value;
    if (_cur && _cur.alpha) { AlphaGL.clear(); if (_defaultAlpha > 0) AlphaGL.fill(_defaultAlpha); _cur.modified = true; _scheduleRender(); }
    saveConfig();
  });
  document.getElementById('default-alpha').addEventListener('change', function() { saveConfig(); _applyBackdrop(); _updateFloatingHTML(); });
  // 快捷交换左右键 Alpha
  document.getElementById('btn-swap-alpha').addEventListener('click', function() {
    var left = document.getElementById('left-alpha');
    var right = document.getElementById('right-alpha');
    var tmp = left.value;
    left.value = right.value;
    right.value = tmp;
  });

  // ---- 蒙板模板绑定 ----
  // 未确认模板实例: 拖动/缩放/无位移单击增删锚点(确定键 Enter/空格 见 keydown)
  _templateInst.addEventListener('pointerdown', _floatPointerDown);
  // 工具栏「模板」按钮打开管理对话框
  document.getElementById('btn-template').addEventListener('click', function() {
    if (_templateDlg.hidden) { _renderTemplateCards(); _templateDlg.hidden = false; }
  });
  document.getElementById('tmpl-close').addEventListener('click', function() { _templateDlg.hidden = true; });
  document.getElementById('tmpl-capture').addEventListener('click', beginTemplateCapture);
  // 默认合并方式: min(默认) / max
  document.querySelectorAll('input[name="merge-mode"]').forEach(function(el) {
    el.addEventListener('change', function() { if (el.checked) _mergeMode = el.value; });
  });

  document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
    // Esc: 先取消模板框选, 再取消未确认模板实例(框选待确认/已放置)
    if (e.key === 'Escape') {
      if (_captureMode) { cancelTemplateCapture(); toast('已取消框选'); }
      else if (_floating) {
        var wasCapture = _floating.capture;
        _discardFloating();
        toast(wasCapture ? '已取消框选' : '已取消模板');
      }
      return;
    }
    // Enter/空格(确定键): 框选待确认 → 确认保存模板; 放置实例 → 应用蒙板
    if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault();
      if (_floating && _floating.capture) _confirmCapture(); else _applyTemplate();
      return;
    }
    // 快捷键 1-9 / 0: 框选待确认时切换目标槽位, 否则放置对应槽位模板到画布
    var slot = null;
    if (e.key.length === 1 && e.key >= '1' && e.key <= '9') slot = parseInt(e.key, 10) - 1;
    else if (e.key === '0') slot = 9;
    if (slot !== null) {
      if (_floating && _floating.capture) { _floating.slot = slot; toast('确认后将保存到槽位 ' + (slot + 1)); }
      else if (!_captureMode && _templates[slot]) placeTemplate(slot);
      e.preventDefault();
      return;
    }
    if (e.key === 'ArrowLeft' || e.key === 'a' || e.key === 'A') { e.preventDefault(); goPrev(); }
    else if (e.key === 'ArrowRight' || e.key === 'd' || e.key === 'D') { e.preventDefault(); goNext(); }
    else if (e.key === 's' || e.key === 'S') { e.preventDefault(); doSave(true); }
    else if (e.key === 'x' || e.key === 'X') { goSkip(); }
  });

  // ── 蒙板表达式编辑辅助 ──────────────────────────────────────────
  /**
   * 词法切分蒙板表达式. 全角引号先归一为 ASCII 引号(与后端 mask_expression._normalize
   * 一致); 字符串("...")为原子 token 且剥离引号, 其余切分为二元/比较运算符、括号、
   * 逗号、分号(含全角 ；)与裸词/数字. 空白丢弃; 未闭合引号时剩余内容并入字符串 token.
   * Callers:
   *   - paint.js:expandSemicolons
   *   - paint.js:formatExpression
   */
  var tokenizeExpression = function (text) {
    var src = text.replace(/\u201c/g, '"').replace(/\u201d/g, '"');
    var toks = [];
    var i = 0;
    while (i < src.length) {
      var ch = src[i];
      if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') { i++; continue; }
      if (ch === '"') {
        var end = src.indexOf('"', i + 1);
        if (end < 0) { toks.push({ k: 'str', v: src.slice(i + 1) }); break; }
        toks.push({ k: 'str', v: src.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
      var two = src.slice(i, i + 2);
      if (two === '>=' || two === '<=' || two === '==' || two === '!=') {
        toks.push({ k: 'op', v: two });
        i += 2;
        continue;
      }
      if ('+-*<>'.indexOf(ch) >= 0) { toks.push({ k: 'op', v: ch }); i++; continue; }
      if (ch === '(' || ch === ')' || ch === ',') { toks.push({ k: 'pun', v: ch }); i++; continue; }
      if (ch === ';' || ch === '\uff1b') { toks.push({ k: 'semi', v: ';' }); i++; continue; }
      var j = i;
      while (j < src.length && ' \t\n\r"+-*<>(),;\uff1b'.indexOf(src[j]) < 0) j++;
      toks.push({ k: 'atom', v: src.slice(i, j) });
      i = j;
    }
    return toks;
  };

  /**
   * 将 token 列表序列化为表达式文本: 字符串加回引号, 逗号后补一个空格, 其余紧连.
   * Callers:
   *   - paint.js:expandSemicolons
   */
  var serializeTokens = function (toks) {
    var out = '';
    for (var i = 0; i < toks.length; i++) {
      var t = toks[i];
      out += t.k === 'str' ? '"' + t.v + '"' : (t.k === 'pun' && t.v === ',' ? ', ' : t.v);
    }
    return out;
  };

  /**
   * 将表达式中的分号列表展开为字符串相加, 三种形态均识别:
   *   1. 引号内分号: "角色主体;耳朵" → ("角色主体"+"耳朵");
   *      紧邻左括号时省略包裹括号(该括号已提供分组, 如 blur("a;b") → blur("a"+"b"));
   *   2. 既有括号内的分号: ("白色背景";"对话气泡") → ("白色背景"+"对话气泡"),
   *      仅替换为 + 不重复包裹; 相邻裸词概念自动加引号(当前蒙板与数值除外);
   *   3. 顶层分号: 白色背景;对话气泡 → ("白色背景"+"对话气泡"), 整体包裹一层括号.
   * 空片段丢弃; 全角分号 ；与 ASCII 分号同样识别; 无分号时返回 null.
   *
   * Args:
   *   text (string): 原始表达式.
   *
   * Returns:
   *   string | null: 展开后的表达式; 无分号时为 null.
   *
   * Callers:
   *   - paint.js:sam3-expand 点击处理
   */
  var expandSemicolons = function (text) {
    var toks = tokenizeExpression(text);
    var hasSemi = toks.some(function (t) {
      return t.k === 'semi' || (t.k === 'str' && /[;；]/.test(t.v));
    });
    if (!hasSemi) return null;
    // 第一遍: 引号内分号 → 相加组
    var pass1 = [];
    for (var i = 0; i < toks.length; i++) {
      var t = toks[i];
      if (t.k !== 'str' || !/[;；]/.test(t.v)) { pass1.push(t); continue; }
      var parts = t.v.split(/[;；]/).map(function (s) { return s.trim(); })
                     .filter(function (s) { return s; });
      if (parts.length === 1) { pass1.push({ k: 'str', v: parts[0] }); continue; }
      if (parts.length >= 2) {
        var prev = pass1[pass1.length - 1];
        var wrapped = !(prev && prev.k === 'pun' && prev.v === '(');
        if (wrapped) pass1.push({ k: 'pun', v: '(' });
        pass1.push({ k: 'str', v: parts[0] });
        for (var p = 1; p < parts.length; p++) {
          pass1.push({ k: 'op', v: '+' });
          pass1.push({ k: 'str', v: parts[p] });
        }
        if (wrapped) pass1.push({ k: 'pun', v: ')' });
        continue;
      }
      pass1.push(t); // 纯分号空串: 原样保留
    }
    if (!pass1.some(function (t) { return t.k === 'atom' || t.k === 'str'; })) return text;
    // 第二遍: 与分号相邻的裸词概念加引号(当前蒙板与数值除外), 使 SAM3 收到概念提示
    for (var s = 0; s < pass1.length; s++) {
      if (pass1[s].k !== 'semi') continue;
      for (var d = s - 1; d <= s + 1; d += 2) {
        var n = pass1[d];
        if (n && n.k === 'atom' && n.v !== '当前蒙板' && !/^-?\d+(\.\d+)?$/.test(n.v)) {
          pass1[d] = { k: 'str', v: n.v };
        }
      }
    }
    // 第三遍: 按分号切分为片段, 序列化后以 + 连接, 空片段丢弃;
    // 存在括号外的顶层分号时整体包裹一层括号
    var depth = 0;
    var topLevelSemi = false;
    var runs = [[]];
    for (var i2 = 0; i2 < pass1.length; i2++) {
      var t2 = pass1[i2];
      if (t2.k === 'semi') {
        if (depth === 0) topLevelSemi = true;
        runs.push([]);
        continue;
      }
      if (t2.k === 'pun' && t2.v === '(') depth++;
      else if (t2.k === 'pun' && t2.v === ')') depth--;
      runs[runs.length - 1].push(t2);
    }
    var items = runs.map(function (r) { return serializeTokens(r).trim(); })
                    .filter(function (seg) { return seg; });
    if (!items.length) return text;
    var out = items.join('+');
    return topLevelSemi ? '(' + out + ')' : out;
  };

  /**
   * 按括号缩进重排表达式:
   *   - 分组括号: 左括号留在当前行尾(如 "a + ("), 内容换行并按嵌套深度缩进
   *     (每层 4 空格), 右括号独立成行, 其后内容紧随其后(如 ") * xx.xx");
   *   - 函数调用括号(紧随裸词, 如 blur(...)/min(...)): 左右括号均不独立成行;
   *   - 二元/比较运算符尾随在左操作数之后(如 "" +), 右操作数在括号内换行起行;
   *     顶层(无括号)不换行, 如 blur("角色主体") > 0.4 保持单行;
   *   - 字符串内容原样保留, 一元符号紧贴操作数, 运算符两侧留一个空格.
   * 重排仅调整空白, 语义不变; 空表达式返回 null.
   *
   * Args:
   *   text (string): 原始表达式(可含任意空白排版).
   *
   * Returns:
   *   string | null: 重排后的多行表达式; 空表达式为 null.
   *
   * Callers:
   *   - paint.js:sam3-format 点击处理
   */
  var formatExpression = function (text) {
    var toks = tokenizeExpression(text);
    if (!toks.length) return null;
    var INDENT = '    ';
    var lines = [];
    var line = '';
    var depth = 0;
    var calls = [];      // 括号栈: true 为函数调用括号(不独立成行), false 为分组括号
    var prev = null;
    var afterOp = false; // 刚输出二元运算符: 括号内右操作数换行起行
    var pushLine = function () {
      var trimmed = line.replace(/\s+$/, '');
      if (trimmed) lines.push(trimmed);
      line = '';
    };
    var newLine = function () { pushLine(); line = INDENT.repeat(depth); };
    for (var i = 0; i < toks.length; i++) {
      var t = toks[i];
      if (t.k === 'pun' && t.v === '(') {
        line += '(';
        depth++;
        calls.push(prev !== null && prev.k === 'atom');
        if (!calls[calls.length - 1]) newLine();
        prev = t;
        afterOp = false;
        continue;
      }
      if (t.k === 'pun' && t.v === ')') {
        depth--;
        var isCall = calls.pop();
        if (!isCall) newLine();
        line += ')';
        prev = t;
        afterOp = false;
        continue;
      }
      if (t.k === 'pun' && t.v === ',') {
        line += ', ';
        prev = t;
        afterOp = false;
        continue;
      }
      var isBinary = t.k === 'op' && prev !== null &&
        !(prev.k === 'op' || (prev.k === 'pun' && (prev.v === '(' || prev.v === ',')));
      if (isBinary) {
        line += ' ' + t.v + ' ';
        prev = t;
        afterOp = true;
        continue;
      }
      // 操作数 / 字符串 / 一元符号
      if (afterOp && depth > 0) newLine();
      line += t.k === 'str' ? '"' + t.v + '"' : t.v;
      prev = t;
      afterOp = false;
    }
    pushLine();
    return lines.join('\n');
  };

  /**
   * 折叠引号外空白(含换行与缩进)为单空格并去首尾空白, 使多行排版可作为单行
   * 表达式提交后端 ast.parse(括号外换行会语法错误); 引号内内容保持原样.
   * 全角引号与 ASCII 引号同样界定字符串, 与后端 _normalize 语义一致.
   *
   * Args:
   *   text (string): 输入框中的表达式(可能多行).
   *
   * Returns:
   *   string: 折叠后的单行表达式(可能为空串).
   *
   * Callers:
   *   - paint.js:birefnet-start 点击处理(SAM3 分支)
   */
  var flattenExpression = function (text) {
    var out = '';
    var inStr = false;
    var spaced = false;
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (inStr) {
        out += ch;
        if (ch === '"' || ch === '\u201c' || ch === '\u201d') inStr = false;
        continue;
      }
      if (ch === '"' || ch === '\u201c' || ch === '\u201d') {
        inStr = true;
        out += ch;
        continue;
      }
      if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') {
        if (out && !spaced) { out += ' '; spaced = true; }
        continue;
      }
      out += ch;
      spaced = false;
    }
    return out.trim();
  };

  // 蒙版生成 (BiRefNet / SAM3)
  var birefnetBtn = document.getElementById('btn-birefnet-mask');
  var birefnetDlg = document.getElementById('birefnet-dlg');
  if (birefnetBtn && birefnetDlg) {
    var maskModeEl = document.getElementById('mask-mode');
    var birefnetParamsEl = document.getElementById('birefnet-params');
    var sam3ParamsEl = document.getElementById('sam3-params');
    var sam3ExprEl = document.getElementById('sam3-expression');
    // 表达式编辑按钮: 分号转相加 / 按缩进格式化, 结果直接回写输入框
    document.getElementById('sam3-expand').addEventListener('click', function () {
      var expanded = expandSemicolons(sam3ExprEl.value);
      if (expanded === null) { toast('表达式中没有分号'); return; }
      sam3ExprEl.value = expanded;
    });
    document.getElementById('sam3-format').addEventListener('click', function () {
      var formatted = formatExpression(sam3ExprEl.value);
      if (formatted === null) { toast('表达式为空'); return; }
      sam3ExprEl.value = formatted;
    });
    // 按生成方式切换参数区: BiRefNet 显示扩展/收缩/值域, SAM3 显示蒙板表达式
    var syncMaskMode = function () {
      var isSam3 = maskModeEl.value === 'sam3';
      birefnetParamsEl.hidden = isSam3;
      sam3ParamsEl.hidden = !isSam3;
    };
    maskModeEl.addEventListener('change', syncMaskMode);
    syncMaskMode();
    birefnetBtn.addEventListener('click', function () { birefnetDlg.hidden = false; });
    document.getElementById('birefnet-cancel').addEventListener('click', function () { birefnetDlg.hidden = true; });
    // 「跳过已有蒙版」仅批量模式可用: 单图模式禁用(灰显由 CSS 派生), disabled 只能 JS 设
    var syncSkipEnabled = function () {
      document.getElementById('birefnet-skip').disabled = !document.getElementById('birefnet-batch').checked;
    };
    document.getElementById('birefnet-batch').addEventListener('change', syncSkipEnabled);
    syncSkipEnabled();
    document.getElementById('birefnet-start').addEventListener('click', function () {
      var mode = maskModeEl.value;
      var batch = document.getElementById('birefnet-batch').checked;
      // 单图模式无「跳过已有蒙版」概念, 始终重新生成; 仅批量模式应用该选项
      var skipExisting = batch ? document.getElementById('birefnet-skip').checked : false;
      var body = { dir: API.getDir(), mode: mode, skip_existing: skipExisting };
      if (mode === 'sam3') {
        var expression = flattenExpression(sam3ExprEl.value);
        if (!expression) { toast('请输入蒙板表达式'); return; }
        body.expression = expression;
      } else {
        // 0 是合法取值(如不扩展), 不可用 `|| 默认值` 吞掉, 仅对非法输入回退默认
        var intOr = function (val, dflt) {
          var n = parseInt(val, 10);
          return Number.isNaN(n) ? dflt : n;
        };
        body.mask_expand = intOr(document.getElementById('birefnet-expand').value, 32);
        body.mask_shrink = intOr(document.getElementById('birefnet-shrink').value, 0);
        body.mask_min = intOr(document.getElementById('birefnet-min').value, 96);
        body.mask_max = intOr(document.getElementById('birefnet-max').value, 255);
      }
      // 默认单图: 仅重新生成当前图片; 勾选「批量」处理整个目录
      if (!batch) {
        if (!_cur || !_cur.item) { toast('当前没有图片'); return; }
        body.path = _cur.item.path;
      }
      birefnetDlg.hidden = true;
      API.request(API.apiPath('/mask-generate'), { method: 'POST', body: JSON.stringify(body) })
        .then(function (resp) {
          if (resp.ok && resp.job_id) {
            watchBirefnetJob(resp.job_id, batch);
          } else {
            toast('启动失败');
          }
        })
        .catch(function (e) { toast(String(e.message || e)); });
    });
  }

  /**
   * 订阅蒙版生成任务进度. batch 为真表示批量模式(整目录), 完成后使全部图片的
   * 蒙板缓存失效; 单图模式仅当前图变化, 由 reloadCurrentMask 就地更新.
   * Callers: birefnet-start 点击处理.
   */
  function watchBirefnetJob(jobId, batch) {
    var bar = document.getElementById('birefnet-bar');
    var fill = document.getElementById('birefnet-fill');
    var pct = document.getElementById('birefnet-pct');
    var text = document.getElementById('birefnet-text');
    bar.hidden = false;
    // 复用 API.watchJob 统一 SSE 订阅(断线重连/终态自动 close), 这里只做 UI 与后续动作
    API.watchJob(jobId, function (snap) {
      if (snap.total) {
        var p = Math.round(snap.done / snap.total * 100);
        fill.style.width = p + '%';
        pct.textContent = p + '%';
        text.textContent = snap.done + '/' + snap.total + (snap.failed ? ' 失败' + snap.failed : '');
      }
      if (snap.status === 'done') {
        fill.style.width = '100%';
        pct.textContent = '100%';
        text.textContent = '完成 ' + snap.done + '/' + snap.total;
        setTimeout(function () { bar.hidden = true; }, 1500);
        if (batch) {
          // 批量: 整目录蒙板可能都已更新, 清空缓存并重拉列表刷新状态;
          // 保持当前图片位置, 切换图片时重新加载新蒙板
          _preloadCache = {};
          refreshAfterMaskDone().catch(function (e) { toast(String(e.message || e)); });
        } else {
          // 单图: 仅当前图蒙板更新, 标记 has_mask 后只重载当前图蒙板, 不重拉列表
          if (_cur && _cur.item) {
            _cur.item.has_mask = true;
            reloadCurrentMask().catch(function (e) { toast(String(e.message || e)); });
          }
        }
        return;
      }
      if (snap.status === 'error') {
        text.textContent = '失败: ' + (snap.message || '未知错误');
        setTimeout(function () { bar.hidden = true; }, 5000);
        return;
      }
      if (snap.status === 'cancelled') {
        setTimeout(function () { bar.hidden = true; }, 3000);
      }
    });
  }

  /**
   * 批量蒙版生成完成后: 重拉图片列表刷新各图蒙版状态, 并只重新加载当前图片的蒙板.
   * 不重载整张图片, 保持当前图片位置. 单图模式不走此函数(仅 reloadCurrentMask).
   * Callers: watchBirefnetJob(批量任务完成).
   */
  async function refreshAfterMaskDone() {
    var curPath = _cur && _cur.item ? _cur.item.path : null;
    var res = await API.request(API.apiPath('/paint/images') + '?dir=' + encodeURIComponent(API.getDir()));
    _images = res;
    if (_strip) _strip.refresh();
    if (!curPath) { updateStatusBar(); return; }
    // 按路径定位当前图片(列表顺序可能变化), 更新 _cur 指向后只重载其蒙板
    for (var i = 0; i < _images.length; i++) {
      if (_images[i].path === curPath) {
        _cur.index = i;
        _cur.item = _images[i];
        break;
      }
    }
    await reloadCurrentMask();
  }

  /**
   * 只重新加载当前图片的蒙板并重绘, 不重载整张图片.
   * Callers: watchBirefnetJob(单图完成)、refreshAfterMaskDone(批量完成).
   */
  async function reloadCurrentMask() {
    if (!_cur || !_cur.item || !_cur.item.has_mask) return;
    var blob = await _fetchBlob('/paint/mask', _cur.item.path);
    var el = await _decodeImage(blob);
    var mCanvas = document.createElement('canvas');
    mCanvas.width = _cur.w; mCanvas.height = _cur.h;
    mCanvas.getContext('2d').drawImage(el, 0, 0);
    _cur.mask = mCanvas;
    // 同步预加载缓存, 切回该图时直接命中新蒙板
    _preloadCache[_cur.item.path] = { img: _cur.rgbImg, mask: mCanvas, w: _cur.w, h: _cur.h };
    _initAlphaCanvas(_cur);
    _scheduleRender();
    updateStatusBar();
  }

  if (typeof DirPicker !== 'undefined') {
    // 蒙版页为单目录工作流: 一次只处理一个目录的蒙版
    if (DirPicker.setSingle) DirPicker.setSingle(true);
    DirPicker.onChange(function () {
      loadImages().catch(function (e) { toast(String(e.message || e)); });
    });
  }

  loadConfig().then(function() { Promise.all([loadImages(), _loadTemplates()]); });
})();
