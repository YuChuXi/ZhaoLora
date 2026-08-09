/**
 * video_extract.js —— 视频抽帧页。
 *
 * 交互参照 YSM/app.py 的桌面工具在浏览器中复刻：
 *   - 主预览区显示当前帧；左键存帧、右键删帧、滚轮调帧
 *   - 底部缩略图条按采样间隔提供快速跳转
 *   - 帧类型柱状图(Canvas)展示每帧编码类型(I/P/B)与包大小，点击柱状跳帧
 *   - 帧类型走后端长任务(ffprobe 分析 + SSE 进度)，完成后回读并重绘柱状图
 *   - 键盘：A/D 切换视频、Q/E 单帧、←/→ 十帧、S/空格 存帧、X/Delete 删帧
 *
 * 引用：templates/video_extract.html。
 */
var VideoExtract = (function () {
  'use strict';

  var THUMB_COUNT = 60; // 缩略图条采样目标数量
  var THUMB_SIZE = 256; // 缩略图条帧最长边
  var MAIN_SIZE = 1920; // 主图帧最长边
  var FRAME_CACHE_MAX = 200; // 主图帧缓存上限: 前/后各 100 个 I/P 帧(隐藏 B 帧时)
  var FRAME_COLORS = { I: '#ff6b6b', P: '#f5c542', B: '#4a9eff' }; // 红I/黄P/蓝B

  var state = {
    path: '',      // 相对数据集根的视频路径
    total: 0,      // 总帧数
    idx: 0,        // 当前帧索引
    width: 0,
    height: 0,
    duration: 0,
    types: {},     // 帧索引 -> {type: I/P/B, size: 包大小}
    saved: [],     // 已保存帧索引(升序)
    videoList: [], // 全部视频列表(用于 A/D 切换)
    vidIdx: -1,    // 当前视频在列表中的位置
    jobWatch: null,
    analyzing: false,
    hideB: true, // 隐藏 B 帧: 柱状图不绘制 B 帧柱(紧凑排列), 所有切帧操作(滚轮/键盘/按钮)跳过 B 帧, 只落在 I/P 帧
  };

  var _stripItems = []; // 缩略图条 DOM 集合
  var _stripIo = null;  // 缩略图懒加载观察器
  var _frameCache = new Map(); // 主图帧缓存: idx -> objectURL(blob), 超出上限时按插入序淘汰并 revoke
  var _reqSeq = 0; // 主图请求序号, 丢弃过期响应
  var _globalCache = null; // 全局图柱状体缓存(逐帧绘制成本高, 仅宽度/帧数变化时重建)

  function $(id) { return document.getElementById(id); }

  /** 构造帧预览 URL, 可选等比缩略最长边尺寸。引用: loadFrame / buildStrip。 */
  function frameUrl(idx, size) {
    var url = API.apiPath('/video/frame')
      + '?path=' + encodeURIComponent(state.path)
      + '&index=' + idx;
    if (size) url += '&size=' + size;
    return url;
  }

  /** 构造封面 URL(用于视频切换时顶部信息)。引用: 无(仅信息)。 */
  function coverUrl() {
    return API.apiPath('/video/cover') + '?path=' + encodeURIComponent(state.path);
  }

  /** 读取 path 查询参数并解码。引用: init。 */
  function readQueryPath() {
    var p = new URLSearchParams(location.search).get('path');
    return p ? decodeURIComponent(p) : '';
  }

  /**
   * 更新状态栏与工具栏信息。引用: goto / loadDetail / refreshSaved。
   */
  function paintStatus() {
    var name = state.path ? state.path.split('/').pop() : '';
    var seg = $('seg-name');
    if (seg) {
      seg.textContent = (state.vidIdx >= 0 ? '[' + (state.vidIdx + 1) + '/' + state.videoList.length + '] ' : '')
        + name
        + ' · ' + state.total + ' 帧'
        + (state.total ? ' · ' + fmtTime((state.idx + 1) / (state.total / (state.duration || 1))) : '');
    }
    var frame = $('seg-pos');
    if (frame) {
      var ft = state.types[state.idx];
      var tInfo = ft ? (' · ' + ft.type + '帧 ' + ft.size + 'KB') : '';
      frame.textContent = (state.idx + 1) + '/' + state.total + tInfo;
    }
    var saved = $('seg-done');
    if (saved) saved.textContent = '已保存: ' + state.saved.length;
    var size = $('seg-metric');
    if (size) size.textContent = state.width + '×' + state.height;
  }

  /** 秒数格式化为 mm:ss。引用: paintStatus。 */
  function fmtTime(sec) {
    if (!(sec > 0) || !isFinite(sec)) return '';
    var s = Math.floor(sec % 60);
    var m = Math.floor(sec / 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  /**
   * 跳转到指定帧: 钳位、加载预览、同步缩略条与曲线光标。
   * @param {number} idx
   * 引用: init / 各切帧入口 / 键盘 / 滚轮。
   */
  function goto(idx) {
    if (!state.total) return;
    idx = Math.max(0, Math.min(state.total - 1, idx));
    if (idx === state.idx) {
      paintStatus();
      return;
    }
    // 若目标帧已缓存为当前帧图像的一部分则直接使用; 否则即时请求
    loadFrame(idx);
  }

  /** 请求并显示指定帧。引用: goto。 */
  function loadFrame(idx) {
    var stage = $('ve-stage');
    var img = $('ve-img');
    if (stage) stage.classList.add('loading');
    var cached = _frameCache.get(idx);
    var url = cached || frameUrl(idx, MAIN_SIZE);
    img.onload = function () {
      img.setAttribute('data-idx', String(idx));
      state.idx = idx;
      if (stage) stage.classList.remove('loading');
      paintStatus();
      updateCursor();
      syncStrip();
    };
    img.onerror = function () {
      if (stage) stage.classList.remove('loading');
      toast('帧 ' + idx + ' 读取失败');
    };
    img.src = url;
    preloadNeighbors();
  }

  /**
   * 收集当前帧前/后各 count 个导航帧索引(隐藏 B 帧时为 I/P 帧, 否则为全部帧)。
   * 引用: preloadNeighbors。
   */
  function neighborFrameIndices(count) {
    var back = [];
    for (var i = state.idx - 1; back.length < count && i >= 0; i--) {
      var b = state.types[i];
      if (b && (b.type !== 'B' || !state.hideB)) back.unshift(i);
    }
    var fwd = [];
    for (var j = state.idx + 1; fwd.length < count && j < state.total; j++) {
      var f = state.types[j];
      if (f && (f.type !== 'B' || !state.hideB)) fwd.push(j);
    }
    return back.concat(fwd);
  }

  /** 预取单帧到内存缓存(blob -> objectURL), 超出上限时按插入序淘汰并 revoke。引用: preloadNeighbors。 */
  function cacheFrame(idx) {
    if (_frameCache.has(idx)) return;
    fetch(frameUrl(idx, MAIN_SIZE))
      .then(function (r) { return r.ok ? r.blob() : null; })
      .then(function (blob) {
        if (!blob) { _frameCache.delete(idx); return; }
        var url = URL.createObjectURL(blob);
        _frameCache.set(idx, url);
        while (_frameCache.size > FRAME_CACHE_MAX) {
          var oldest = _frameCache.keys().next().value;
          URL.revokeObjectURL(_frameCache.get(oldest));
          _frameCache.delete(oldest);
        }
      })
      .catch(function () { _frameCache.delete(idx); });
  }

  /** 预取当前帧前/后各 100 个导航帧, 使切帧时无需等待网络。引用: loadFrame。 */
  function preloadNeighbors() {
    var list = neighborFrameIndices(100);
    for (var i = 0; i < list.length; i++) cacheFrame(list[i]);
  }

  /** 切换视频(上下一个)，重新加载详情并回到第 0 帧。引用: prevVideo/nextVideo。 */
  function switchVideo(offset) {
    if (!state.videoList.length) return;
    var i = state.vidIdx + offset;
    if (i < 0 || i >= state.videoList.length) {
      toast('已是第一个/最后一个视频');
      return;
    }
    var v = state.videoList[i];
    setPath(v.path);
  }

  /**
   * 切换到一个新视频路径: 重置状态并加载详情。
   * @param {string} p
   * 引用: init / switchVideo。
   */
  async function setPath(p) {
    clearFrameCache();
    state.path = p;
    state.idx = 0;
    state.total = 0;
    state.types = {};
    state.saved = [];
    stopAnalysis();
    var img = $('ve-img');
    if (img) img.removeAttribute('src');
    $('ve-strip').innerHTML = '';
    _stripItems = [];
    _chart = { global: null, local: null };
    paintStatus();
    await loadDetail();
    // 直接加载第 0 帧: goto(0) 会因 idx===state.idx 提前返回而跳过首帧
    loadFrame(0);
  }

  /** 清空主图帧缓存并撤销所有 objectURL。引用: setPath。 */
  function clearFrameCache() {
    _frameCache.forEach(function (url) { URL.revokeObjectURL(url); });
    _frameCache.clear();
  }

  /**
   * 把后端紧凑位流(类型串 + 大小数组)还原为 ``{帧号: {type, size}}``。
   * 数组下标即帧序号, 由 detail 接口压缩传输以减小负载。
   * 引用: loadDetail。
   */
  function decodeBitstream(bits) {
    var out = {};
    if (!bits || !bits.t) return out;
    var t = bits.t, s = bits.s || [];
    for (var i = 0; i < t.length; i++) {
      var c = t[i];
      if (c !== '?') out[i] = { type: c, size: s[i] || 0 };
    }
    return out;
  }

  /**
   * 加载视频详情(宽高/时长/总帧数/帧类型/已保存)。
   * 引用: setPath / analysisDone。
   */
  async function loadDetail() {
    var data = await API.request(API.apiPath('/video/detail')
      + '?path=' + encodeURIComponent(state.path));
    state.total = data.total || 0;
    state.width = data.width || 0;
    state.height = data.height || 0;
    state.duration = data.duration || 0;
    state.types = decodeBitstream(data.types);
    state.saved = data.saved || [];
    if (!state.total) {
      toast('视频无法读取或总帧数为 0');
      return;
    }
    buildStrip();
    drawChart();
    // 位流缓存命中时如实报告已分析状态; 未命中则自动分析(进度会覆盖该文本)
    var segExtra = $('seg-extra');
    if (segExtra && Object.keys(state.types).length) {
      segExtra.textContent = '帧类型: 已分析';
    }
    // 位流未缓存时自动分析帧类型, 使 I/P/B 曲线无需手动触发;
    // 分析完成回调会回读 detail(此时 types 非空)故不会重复启动
    if (!Object.keys(state.types).length && !state.analyzing) {
      toggleAnalysis();
    }
  }

  /**
   * 保存当前帧 PNG 到 main 目录。引用: 存帧按钮/左键/S/空格。
   */
  async function saveCurrent() {
    var res = await API.request(API.apiPath('/video/frame/save'), {
      method: 'POST',
      body: JSON.stringify({ path: state.path, index: state.idx }),
    });
    state.saved = res.saved || [];
    paintStatus();
    drawChart();
    syncStrip();
    toast('已存帧 ' + state.idx);
  }

  /**
   * 删除当前帧已保存的 PNG。引用: 删帧按钮/右键/X/Delete。
   */
  async function deleteCurrent() {
    var res = await API.request(API.apiPath('/video/frame/delete'), {
      method: 'POST',
      body: JSON.stringify({ path: state.path, index: state.idx }),
    });
    state.saved = res.saved || [];
    paintStatus();
    drawChart();
    syncStrip();
    toast('已删帧 ' + state.idx);
  }

  // ---- 缩略图条 ----

  /**
   * 按采样间隔构建缩略图条(目标 THUMB_COUNT 个)。
   * 引用: loadDetail。
   */
  function buildStrip() {
    var strip = $('ve-strip');
    strip.innerHTML = '';
    _stripItems = [];
    if (!state.total) return;
    // 按视频真实宽高比设置缩略图占位, 使懒加载前布局与加载后一致(避免抖动/误判)
    strip.style.setProperty(
      '--thumb-ar',
      (state.width || 16) + '/' + (state.height || 9),
    );
    var interval = Math.max(1, Math.round(state.total / THUMB_COUNT));
    var indices = [];
    for (var i = 0; i < state.total; i += interval) indices.push(i);
    if (indices[indices.length - 1] !== state.total - 1) indices.push(state.total - 1);

    for (var k = 0; k < indices.length; k++) {
      (function (idx) {
        var box = document.createElement('div');
        box.className = 've-thumb';
        box.dataset.idx = String(idx);
        var img = document.createElement('img');
        img.loading = 'lazy';
        img.dataset.src = frameUrl(idx, THUMB_SIZE);
        box.appendChild(img);
        var tag = document.createElement('span');
        tag.className = 'tag';
        tag.textContent = String(idx);
        box.appendChild(tag);
        box.addEventListener('click', function () { goto(idx); });
        strip.appendChild(box);
        _stripItems.push({ idx: idx, box: box, img: img });
      })(indices[k]);
    }
    observeStrip(strip);
    syncStrip();
  }

  /** 为缩略图条绑定懒加载观察器。引用: buildStrip。 */
  function observeStrip(container) {
    if (_stripIo) _stripIo.disconnect();
    _stripIo = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (!entries[i].isIntersecting) continue;
        var img = entries[i].target;
        var url = img.dataset.src;
        if (!url) continue;
        img.src = url;
        delete img.dataset.src;
        _stripIo.unobserve(img);
      }
    }, { root: container, rootMargin: '300px 0px', threshold: 0 });
    var imgs = container.querySelectorAll('img[data-src]');
    for (var j = 0; j < imgs.length; j++) _stripIo.observe(imgs[j]);
  }

  /** 同步缩略图条高亮: 当前帧与已保存帧。引用: goto / saveCurrent / deleteCurrent。 */
  function syncStrip() {
    var savedSet = {};
    for (var i = 0; i < state.saved.length; i++) savedSet[state.saved[i]] = true;
    for (var k = 0; k < _stripItems.length; k++) {
      var it = _stripItems[k];
      it.box.classList.toggle('active', it.idx === state.idx);
      it.box.classList.toggle('saved', !!savedSet[it.idx]);
    }
    // 将当前帧缩略图滚入可视区
    var cur = null;
    for (var m = 0; m < _stripItems.length; m++) {
      if (_stripItems[m].idx === state.idx) { cur = _stripItems[m]; break; }
    }
    if (cur && cur.box.scrollIntoView) {
      cur.box.scrollIntoView({ block: 'nearest', inline: 'center' });
    }
  }

  // ---- 帧类型柱状图 (SVG 原生横向滚动) ----

  /**
   * 帧类型双图, 均渲染整条视频时间轴:
   *   全局图: 视口宽度约对应 10 分钟时间窗, 尺度粗;
   *   局部图: 视口宽度约对应 200 帧, 尺度细, 承担逐帧 I/P/B 明细。
   * 每帧一根柱 I红/P黄/B蓝, 高度按最大包归一化; 叠加当前帧白竖线(游标)与
   * 已保存帧绿刻度。SVG 宽度 = 整条时间轴宽度, 超出视口部分由容器
   * ``overflow-x: auto`` 提供浏览器原生横向滚动, 滚动只浏览、不改变当前帧。
   * 数据变化(分析完成/存取帧/换片/缩放)走 drawChart 全量重建; 仅换帧走
   * updateCursor 只移动游标并滚动居中, 开销最小。
   */
  var _chart = { global: null, local: null }; // 各图渲染信息: {svgId, ppf, svgW, viewportW, cursor}

  /** 显示域帧数: 隐藏 B 帧时仅计非 B 帧, 否则即总帧数。引用: globalPxPerFrame / localPxPerFrame / renderChart。 */
  function domainCount() {
    if (!state.hideB) return state.total;
    var n = 0;
    for (var i = 0; i < state.total; i++) {
      var ft = state.types[i];
      if (ft && ft.type !== 'B') n++;
    }
    return n;
  }

  /** 帧索引映射到显示域序号(0 基): 隐藏 B 帧时为其左侧非 B 帧个数, 否则即帧号。引用: cursorX / renderChart。 */
  function barOrdinal(idx) {
    if (!state.hideB) return idx;
    var n = 0;
    for (var i = 0; i < idx; i++) {
      var ft = state.types[i];
      if (ft && ft.type !== 'B') n++;
    }
    return n;
  }

  /** 全局图每帧像素宽: 视口最多对应 10 分钟帧跨度; 视频更短时整条铺满视口(最多 10 分钟而非固定 10 分钟)。引用: renderChart。 */
  function globalPxPerFrame(viewportW) {
    var domain = domainCount() || 1;
    var dur = state.duration || 0;
    var WIN_SEC = 600; // 10 分钟
    var winFrames = dur > 0 ? Math.max(1, Math.round((WIN_SEC * domain) / dur)) : domain;
    var showFrames = Math.min(winFrames, domain);
    return viewportW / showFrames;
  }

  /** 局部图每帧像素宽: 视口最多对应 200 帧; 视频更短时整条铺满视口。引用: renderChart。 */
  function localPxPerFrame(viewportW) {
    var domain = domainCount() || 1;
    var LOCAL_SPAN = 200;
    return viewportW / Math.min(LOCAL_SPAN, domain);
  }

  /**
   * 全量重建单张 SVG 柱状图(柱 + 已存刻度 + 游标)。
   * @param {string} svgId svg 元素 id。
   * @param {function(number):number} pxPerFrame 每帧像素宽计算函数。
   * @returns {{svgId:string, ppf:number, svgW:number, viewportW:number, cursor:SVGElement}|null}
   * 引用: drawChart。
   */
  function renderChart(svgId, pxPerFrame) {
    var svg = $(svgId);
    if (!svg) return null;
    var container = svg.parentElement;
    var viewportW = container.clientWidth, vh = container.clientHeight;
    if (!viewportW || !vh) return null;
    var total = state.total || 1;
    var domain = domainCount() || 1; // 显示域宽度: 隐藏 B 帧时即非 B 帧数
    var ppf = pxPerFrame(viewportW);
    var svgW = Math.max(viewportW, Math.ceil(domain * ppf));
    svg.setAttribute('width', svgW);
    svg.setAttribute('height', vh);
    svg.style.width = svgW + 'px';
    svg.style.height = vh + 'px';
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    var ns = 'http://www.w3.org/2000/svg';
    var plotH = vh - 2, bottom = vh - 1;

    var maxSize = 1;
    for (var k in state.types) {
      var s = state.types[k].size;
      if (s > maxSize) maxSize = s;
    }

    // 隐藏 B 帧时, 柱按显示顺序紧凑排列(不留空槽), 故用 pos 而非 idx 定位。
    var bars = document.createElementNS(ns, 'g');
    var slots = []; // 显示序号 -> 帧索引(点击反查)
    var pos = 0;
    for (var idx = 0; idx < total; idx++) {
      var ft = state.types[idx];
      if (!ft) continue;
      if (state.hideB && ft.type === 'B') continue;
      slots.push(idx);
      var hgt = (ft.size / maxSize) * plotH;
      var r = document.createElementNS(ns, 'rect');
      r.setAttribute('x', pos * ppf);
      r.setAttribute('y', bottom - hgt);
      r.setAttribute('width', Math.max(1, ppf));
      r.setAttribute('height', hgt);
      r.setAttribute('fill', FRAME_COLORS[ft.type] || '#888');
      bars.appendChild(r);
      pos++;
    }
    svg.appendChild(bars);

    if (state.saved.length) {
      for (var s2 = 0; s2 < state.saved.length; s2++) {
        var t = document.createElementNS(ns, 'rect');
        t.setAttribute('x', barOrdinal(state.saved[s2]) * ppf - 1);
        t.setAttribute('y', bottom - 4);
        t.setAttribute('width', 2);
        t.setAttribute('height', 4);
        t.setAttribute('fill', '#3ecf8e');
        svg.appendChild(t);
      }
    }

    var cursor = document.createElementNS(ns, 'line');
    cursor.setAttribute('y1', 0);
    cursor.setAttribute('y2', vh);
    cursor.setAttribute('stroke', '#ffffff');
    cursor.setAttribute('stroke-width', 1);
    svg.appendChild(cursor);

    var info = { svgId: svgId, ppf: ppf, svgW: svgW, viewportW: viewportW, cursor: cursor, slots: slots };
    _chart[svgId === 've-chart-global' ? 'global' : 'local'] = info;
    positionCursor(info);
    return info;
  }

  /** 当前帧游标在柱上的 x 坐标: 柱中心(避免落在两柱交界)。引用: positionCursor / syncScroll。 */
  function cursorX(info) {
    return barOrdinal(state.idx) * info.ppf + info.ppf / 2;
  }

  /** 将游标移动到当前帧柱中心(仅移动 line, 不重建柱)。引用: renderChart / updateCursor。 */
  function positionCursor(info) {
    if (!info || !info.cursor) return;
    var x = cursorX(info);
    info.cursor.setAttribute('x1', x);
    info.cursor.setAttribute('x2', x);
  }

  /** 数据变化时全量重建两图并滚动居中游标。引用: loadDetail / analysisDone / saveCurrent / deleteCurrent / bindResize。 */
  function drawChart() {
    renderChart('ve-chart-global', globalPxPerFrame);
    renderChart('ve-chart-local', localPxPerFrame);
    syncScroll();
  }

  /** 仅移动游标并滚动居中(换帧时调用, 开销最小)。引用: loadFrame。 */
  function updateCursor() {
    positionCursor(_chart.global);
    positionCursor(_chart.local);
    syncScroll();
  }

  /** 滚动各图容器, 使当前帧游标尽量居中(边缘处贴边)。引用: drawChart / updateCursor。 */
  function syncScroll() {
    for (var key in _chart) {
      var c = _chart[key];
      if (!c) continue;
      var container = $(c.svgId).parentElement;
      var target = cursorX(c) - c.viewportW / 2;
      container.scrollLeft = Math.max(0, Math.min(target, c.svgW - c.viewportW));
    }
  }

  /** 将点击坐标换算为帧索引并跳转(按各图每帧像素宽映射)。引用: 两图点击。 */
  function chartClick(e) {
    if (!state.total) return;
    var svg = e.currentTarget;
    var c = _chart[svg.id === 've-chart-global' ? 'global' : 'local'];
    if (!c) return;
    var r = svg.getBoundingClientRect();
    var xCanvas = e.clientX - r.left;
    var slot = Math.floor(xCanvas / c.ppf);
    var idx = c.slots[slot];
    if (idx === undefined) idx = state.total - 1;
    goto(Math.max(0, Math.min(state.total - 1, idx)));
  }

  // ---- 帧类型分析 ----

  /** 停止进行中的分析 SSE 订阅并隐藏进度条。引用: setPath / toggleAnalysis。 */
  function stopAnalysis() {
    if (state.jobWatch) {
      state.jobWatch.close();
      state.jobWatch = null;
    }
    state.analyzing = false;
    var btn = $('ve-analysis');
    if (btn) btn.disabled = false;
    var bar = $('ve-progress');
    if (bar) bar.hidden = true;
  }

  /**
   * 更新分析进度条(轨道+百分比)与状态栏消息。
   * 引用: toggleAnalysis。
   */
  function setProgress(pct, msg) {
    var bar = $('ve-progress');
    if (bar) bar.hidden = false;
    var fill = $('ve-progress-fill');
    if (fill) fill.style.width = Math.min(100, pct) + '%';
    var pctEl = $('ve-progress-pct');
    if (pctEl) pctEl.textContent = Math.round(pct) + '%';
    var seg = $('seg-extra');
    if (seg) seg.textContent = msg || '';
  }

  /** 设置分析结束状态文本并复位按钮。引用: toggleAnalysis。 */
  function setStatus(t) {
    var prog = $('seg-extra');
    if (prog) prog.textContent = t;
    var btn = $('ve-analysis');
    if (btn) btn.textContent = '帧类型';
  }

  /**
   * 启动/停止位流帧类型分析长任务。
   * 引用: 分析按钮 / loadDetail(自动分析)。
   */
  async function toggleAnalysis() {
    if (state.analyzing) {
      stopAnalysis();
      setStatus('分析: 已停止');
      return;
    }
    if (!state.total) return;
    var data = await API.request(API.apiPath('/video/analysis/start')
      + '?path=' + encodeURIComponent(state.path), { method: 'POST' });
    state.analyzing = true;
    var btn = $('ve-analysis');
    if (btn) btn.textContent = '停止';
    setProgress(0, '提交中…');
    state.jobWatch = API.watchJob(data.job_id, function (s) {
      if (s.status === 'running') {
        setProgress(s.pct || 0, (s.done || 0) + '/' + (s.total || 0)
          + (s.message ? ' · ' + s.message : ''));
      } else if (s.status === 'done') {
        stopAnalysis();
        setStatus('分析: 完成');
        analysisDone();
      } else if (s.status === 'error') {
        stopAnalysis();
        setStatus('分析: 失败');
        toast('帧类型分析失败: ' + (s.message || ''));
      } else if (s.status === 'cancelled') {
        stopAnalysis();
        setStatus('分析: 已取消');
      }
    });
  }

  /** 分析完成后回读帧类型并重绘柱状图。引用: toggleAnalysis。 */
  async function analysisDone() {
    await loadDetail();
    drawChart();
    syncStrip();
    paintStatus();
  }

  // ---- 事件绑定 ----

  /**
   * 按步长切帧; 隐藏 B 帧开启时, 步长按非 B 帧(I/P)计数并跳过 B 帧。
   * 位流尚未分析时无类型信息, 退化为按实际帧数步进。
   * 引用: bindToolbar / bindStage / bindKeys。
   */
  function stepFrame(step) {
    if (!state.hideB) { goto(state.idx + step); return; }
    var dir = step > 0 ? 1 : -1;
    var n = Math.abs(step);
    var i = state.idx;
    while (n > 0) {
      i += dir;
      if (i < 0 || i >= state.total) break;
      var ft = state.types[i];
      if (!ft || ft.type !== 'B') n--;
    }
    goto(i);
  }

  /** 跳到下一个/上一个 I 帧(按方向 dir); 无 I 帧时保持当前帧。引用: bindStage(Shift+滚轮)。 */
  function stepI(dir) {
    var i = state.idx + dir;
    while (i >= 0 && i < state.total) {
      var ft = state.types[i];
      if (ft && ft.type === 'I') break;
      i += dir;
    }
    if (i < 0 || i >= state.total) return;
    goto(i);
  }

  /** 绑定工具栏按钮与主预览交互。引用: init。 */
  function bindToolbar() {
    var el = document.getElementById('ve-toolbar');
    if (!el) return;
    el.addEventListener('click', function (e) {
      var id = e.target && e.target.id;
      if (id === 've-prev-vid') switchVideo(-1);
      else if (id === 've-next-vid') switchVideo(1);
      else if (id === 've-prev-10') stepFrame(-10);
      else if (id === 've-prev-1') stepFrame(-1);
      else if (id === 've-next-1') stepFrame(1);
      else if (id === 've-next-10') stepFrame(10);
      else if (id === 've-save') {
        saveCurrent().catch(function (err) { toast('存帧失败: ' + (err.message || err)); });
      } else if (id === 've-del') {
        deleteCurrent().catch(function (err) { toast('删帧失败: ' + (err.message || err)); });
      } else if (id === 've-analysis') {
        toggleAnalysis().catch(function (err) { toast('帧类型分析失败: ' + (err.message || err)); });
      }
    });
  }

  /** 绑定"隐藏B帧"开关: 切换后重绘柱状图并落到非 B 帧。引用: init。 */
  function bindHideB() {
    var box = $('ve-hide-b');
    if (!box) return;
    state.hideB = box.checked;
    box.addEventListener('change', function () {
      state.hideB = box.checked;
      // 开启隐藏 B 帧时, 若当前帧是 B 帧则跳到最近的 I/P 帧
      if (state.hideB) {
        var ft = state.types[state.idx];
        if (ft && ft.type === 'B') stepFrame(1);
      }
      drawChart();
      paintStatus();
    });
  }

  /** 绑定帧类型柱状图交互(全局图 + 局部图): 点击跳帧; 横向滚动由容器 overflow-x:auto 原生处理。引用: init。 */
  function bindChart() {
    var g = $('ve-chart-global');
    var l = $('ve-chart-local');
    if (g) g.addEventListener('click', chartClick);
    if (l) l.addEventListener('click', chartClick);
  }

  /** 绑定主预览交互: 左键存帧、右键删帧、滚轮调帧。引用: init。 */
  function bindStage() {
    var stage = $('ve-stage');
    if (!stage) return;
    stage.addEventListener('mousedown', function (e) {
      if (e.button === 0) {
        e.preventDefault();
        saveCurrent().catch(function (err) { toast('存帧失败: ' + (err.message || err)); });
      } else if (e.button === 2) {
        e.preventDefault();
        deleteCurrent().catch(function (err) { toast('删帧失败: ' + (err.message || err)); });
      }
    });
    stage.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    stage.addEventListener('wheel', function (e) {
      e.preventDefault();
      if (e.shiftKey) { stepI(e.deltaY < 0 ? -1 : 1); return; } // Shift: 按 I 帧跳
      stepFrame(e.deltaY < 0 ? -1 : 1);
    }, { passive: false });
  }

  /**
   * 绑定全局键盘快捷键。
   * 引用: init。
   */
  function bindKeys() {
    document.addEventListener('keydown', function (e) {
      var k = e.key;
      var handled = true;
      if (k === 'a' || k === 'A') switchVideo(-1);
      else if (k === 'd' || k === 'D') switchVideo(1);
      else if (k === 'q' || k === 'Q') stepFrame(-1);
      else if (k === 'e' || k === 'E') stepFrame(1);
      else if (k === 'ArrowLeft') stepFrame(-10);
      else if (k === 'ArrowRight') stepFrame(10);
      else if (k === 'PageUp') stepFrame(-100);
      else if (k === 'PageDown') stepFrame(100);
      else if (k === 's' || k === 'S' || k === ' ') {
        e.preventDefault();
        saveCurrent().catch(function (err) { toast('存帧失败: ' + (err.message || err)); });
      } else if (k === 'x' || k === 'X' || k === 'Delete') {
        e.preventDefault();
        deleteCurrent().catch(function (err) { toast('删帧失败: ' + (err.message || err)); });
      } else {
        handled = false;
      }
      if (handled) e.preventDefault();
    });
  }

  /** 窗口尺寸变化时重绘画布。引用: init。 */
  function bindResize() {
    var raf = null;
    window.addEventListener('resize', function () {
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(drawChart);
    });
  }

  /**
   * 页面初始化入口。
   * 引用: 页面底部自动调用。
   */
  async function init() {
    if (typeof API === 'undefined' || typeof API.dataset !== 'function') return;
    state.path = readQueryPath();
    if (!state.path) {
      var stage = $('ve-stage');
      if (stage) stage.innerHTML = '<p style="padding:40px;color:var(--text-dim);">缺少 path 参数</p>';
      return;
    }
    bindToolbar();
    bindChart();
    bindStage();
    bindKeys();
    bindResize();
    bindHideB();

    // 拉取视频列表以支持 A/D 切换
    try {
      var list = await API.request(API.apiPath('/videos'));
      state.videoList = list || [];
      for (var i = 0; i < state.videoList.length; i++) {
        if (state.videoList[i].path === state.path) { state.vidIdx = i; break; }
      }
    } catch (e) {
      toast(String(e.message || e));
    }

    await setPath(state.path);
  }

  document.addEventListener('DOMContentLoaded', function () {
    init().catch(function (e) {
      toast('初始化失败: ' + (e.message || e));
    });
  });

  return {
    init: init,
    goto: goto,
    drawChart: drawChart,
  };
})();