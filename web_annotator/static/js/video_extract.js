/**
 * video_extract.js —— 视频抽帧页。
 *
 * 交互参照 YSM/app.py 的桌面工具在浏览器中复刻：
 *   - 主预览区显示当前帧；左键存帧、右键删帧、滚轮调帧
 *   - 底部缩略图条按采样间隔提供快速跳转
 *   - 帧类型柱状图(Canvas)展示每帧编码类型(I/P/B)与包大小，点击柱状跳帧
 *   - 帧类型经由后端长任务(逐帧解码分析 + SSE 进度)，完成后回读并重绘柱状图
 *   - 键盘：A/D 切换视频、Q/E 单帧、←/→ 十帧、S/空格 存帧、X/Delete 删帧
 *
 * 引用：templates/video_extract.html。
 */
var VideoExtract = (function () {
  'use strict';

  var THUMB_COUNT = 60; // 缩略图条采样目标数量
  var THUMB_SIZE = 256; // 缩略图条帧最长边
  var FRAME_COLORS = { I: '#ff6b6b', P: '#f5c542', B: '#4a9eff' }; // 红I/黄P/蓝B
  var SEG_MAX_SEC = 10; // 帧窗口最大时长(秒), 前后各半
  var FRAME_CACHE = 60; // 已解码帧缓存上限(段内位置), LRU 淘汰
  var FEED_MARGIN = 16; // 回退重建解码器时, 目标帧之后追加喂入的包数(覆盖 B 帧后向参考)

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
    hideB: true, // 隐藏 B 帧: 仅显示层——柱状图不绘制 B 帧柱(紧凑排列), 所有切帧操作(滚轮/键盘/按钮)跳过 B 帧, 只落在 I/P 帧; 段始终含 B 帧, 不影响请求
  };

  var _stripItems = []; // 缩略图条 DOM 集合

  // ---- WebCodecs 帧窗口段: 后端从 start 前最近关键帧流复制源压缩包(不重编码),
  //      段首 GOP 对齐的前导帧与段尾 B 帧参考帧由解码器自然消化。解码为流式:
  //      按目标位置增量喂包(喂到目标 + FEED_MARGIN 供 B 帧后向参考, 不 flush),
  //      output 按显示顺序持续回调, 喂包时以段内位置作为 chunk timestamp 直通
  //      输出帧; 已解码帧按 LRU 缓存, 段内向前切帧零重复解码, 回退超出缓存才
  //      从目标前最近关键帧重建解码器。VideoFrame 必须显式 close 回还解码器
  //      buffer, 未 close 的帧累积会耗尽硬件解码 surface 池导致输出停摆 ----
  var _canvas = null;   // 主预览 canvas
  var _cctx = null;     // canvas 2D 上下文
  var _decoder = null;  // VideoDecoder 实例
  var _seg = null;      // 当前窗口: { start, end, chunks, sps, pps }
  var _segSeq = 0;      // 窗口段版本号, 换段递增以丢弃过期响应
  var _frames = {};     // 段内位置 -> VideoFrame(LRU 缓存)
  var _frameOrder = []; // 缓存帧的输出顺序(段内位置, 供 LRU 淘汰)
  var _maxOut = -1;     // 已输出的最大段内位置(显示序单调)
  var _fedUntil = -1;   // 已喂入解码器的最大段内位置(增量喂包游标)
  var _flushedEnd = -1; // 已执行过段尾 flush 的段内位置(避免重复 flush)
  var _pendingTarget = -1; // 请求中的全局帧索引(过期目标丢弃)
  var _pendingPos = -1;    // 等待解码输出的段内位置
  var _pendingSeq = -1;    // _pendingPos 所属的段版本(防跨段回调误绘)
  var _inflight = null;    // 在途请求覆盖的窗口 { start, end }, 目标已被覆盖时不重复发请求

  /** ``document.getElementById`` 快捷别名, 全文件通用。 */
  function $(id) { return document.getElementById(id); }

  /** 构造抽帧缩略图 URL, 可选等比缩略最长边尺寸。Callers: buildStrip。 */
  function frameUrl(idx, size) {
    var url = API.apiPath('/video/frame')
      + '?path=' + encodeURIComponent(state.path)
      + '&index=' + idx;
    if (size) url += '&size=' + size;
    return url;
  }

  /** 构造帧窗口段 URL(后端流复制返回 H.264 Annex-B + X-Segment-Start/X-Frame-Count 头)。Callers: ensureSegment。 */
  function segmentUrl(start, end) {
    return API.apiPath('/video/segment')
      + '?path=' + encodeURIComponent(state.path)
      + '&start=' + start + '&end=' + end;
  }

  /** 读取 path 查询参数并解码。Callers: init。 */
  function readQueryPath() {
    var p = new URLSearchParams(location.search).get('path');
    return p ? decodeURIComponent(p) : '';
  }

  /**
   * 更新状态栏与工具栏信息。
   *
   * Callers:
   *   - `goto`
   *   - `setPath`
   *   - `loadDetail`
   *   - `saveCurrent`
   *   - `deleteCurrent`
   *   - `analysisDone`
   *   - `bindHideB`
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

  /** 秒数格式化为 mm:ss。Callers: paintStatus。 */
  function fmtTime(sec) {
    if (!(sec > 0) || !isFinite(sec)) return '';
    var s = Math.floor(sec % 60);
    var m = Math.floor(sec / 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  /**
   * 跳转到指定帧: 钳位、解码预览、同步缩略条与曲线光标。
   *
   * @param {number} idx 目标帧索引.
   *
   * Callers:
   *   - `buildStrip` 缩略图点击
   *   - `chartClick` 柱状图点击
   *   - `stepFrame` 切帧步进
   *   - `stepI` I 帧跳转
   */
  function goto(idx) {
    if (!state.total) return;
    idx = Math.max(0, Math.min(state.total - 1, idx));
    if (idx === state.idx) {
      paintStatus();
      return;
    }
    showFrame(idx);
  }

  /**
   * 显示指定帧: 命中缓存即绘; 尚未输出则等待流式解码推进到该帧; 移出窗口
   * 则先请求新段。
   *
   * @param {number} idx 目标帧索引.
   *
   * Callers:
   *   - `goto`
   *   - `setPath`
   *   - `bindHideB`
   */
  function showFrame(idx) {
    _pendingTarget = idx;
    ensureSegment(idx).then(function () {
      if (_pendingTarget !== idx) return;
      displayAt(idx - _seg.start, idx);
    }).catch(function (e) {
      toast('帧 ' + idx + ' 加载失败: ' + ((e && e.message) || e));
      var stage = $('ve-stage');
      if (stage) stage.classList.remove('loading');
    });
  }

  /** 帧窗口半宽(帧数): 按 fps 和 SEG_MAX_SEC 计算, 前后各最多 SEG_MAX_SEC/2 秒。Callers: ensureSegment。 */
  function segHalf() {
    if (state.duration > 0 && state.total > 0) {
      var fps = state.total / state.duration;
      return Math.max(30, Math.round(fps * SEG_MAX_SEC / 2));
    }
    return 100; // 无时长信息时的回退值
  }

  /**
   * 拉取覆盖目标帧的窗口段并重建解码器。段首为 start 前最近关键帧, 其帧号经
   * ``X-Segment-Start`` 响应头告知; 实际帧数由 ``X-Frame-Count`` 告知(含段尾
   * B 帧参考帧), 前端不猜测。编码包由 displayAt 按目标位置增量喂入。
   * Callers: showFrame。
   */
  function ensureSegment(idx) {
    if (_seg && idx >= _seg.start && idx <= _seg.end) return Promise.resolve();
    if (_inflight && idx >= _inflight.start && idx <= _inflight.end) return Promise.resolve();
    var half = segHalf();
    var start = Math.max(0, idx - half);
    var end = Math.min(state.total - 1, idx + half);
    _inflight = { start: start, end: end };
    var seq = ++_segSeq;
    var stage = $('ve-stage');
    if (stage) stage.classList.add('loading');
    return fetch(segmentUrl(start, end))
      .then(function (r) {
        if (!r.ok) throw new Error('段获取失败 ' + r.status);
        var count = parseInt(r.headers.get('X-Frame-Count'), 10) || 0;
        var segStart = parseInt(r.headers.get('X-Segment-Start'), 10);
        return r.arrayBuffer().then(function (buf) { return { buf: buf, count: count, segStart: segStart }; });
      })
      .then(function (result) {
        if (seq !== _segSeq) return; // 期间已切换目标, 丢弃该段
        var parsed = demuxAnnexB(new Uint8Array(result.buf));
        if (!parsed.sps || !parsed.pps || !parsed.chunks.length) {
          throw new Error('段内无有效编码帧');
        }
        if (_decoder) { try { _decoder.close(); } catch (ignore) {} }
        _decoder = makeDecoder();
        _decoder.configure({
          codec: buildCodecString(parsed.sps),
          description: buildAvcConfig(parsed.sps, parsed.pps),
        });
        _seg = {
          start: result.segStart,
          end: result.segStart + result.count - 1,
          chunks: parsed.chunks,
          sps: parsed.sps,
          pps: parsed.pps,
        };
        clearFrames();
        _pendingPos = -1;
        _fedUntil = -1;
        _flushedEnd = -1;
      })
      .catch(function (e) {
        if (seq === _segSeq) _inflight = null; // 最新请求失败, 释放窗口登记允许重试
        throw e;
      });
  }

  /** 向解码器喂入段内 [from, to] 位置的编码包(不 flush, 输出按显示顺序流式回调)。Callers: ensureSegment / redecode。 */
  function feedChunks(from, to) {
    for (var p = from; p <= to && p < _seg.chunks.length; p++) {
      var chunk = _seg.chunks[p];
      _decoder.decode(new EncodedVideoChunk({
        type: chunk.key ? 'key' : 'delta',
        timestamp: p,
        data: chunk.bytes,
      }));
    }
  }

  /**
   * 显示段内位置 pos 的帧: 缓存命中即绘; 尚未输出则增量喂包到 pos +
   * FEED_MARGIN 后等待回调(帧到即绘); 已输出但被 LRU 淘汰(回退过远)则重建
   * 解码器, 从目标前最近关键帧喂到目标 + FEED_MARGIN。喂包到段尾时按解码器
   * 协议执行一次 flush, 排空缓冲以输出无后向参考的末尾帧。
   * @param {number} pos 窗口内目标位置(显示顺序)。
   * @param {number} idx 全局帧索引。
   * Callers: showFrame。
   */
  function displayAt(pos, idx) {
    var f = _frames[pos];
    if (f) {
      paintFrame(f, idx);
      return;
    }
    if (pos > _maxOut) {
      var last = _seg.chunks.length - 1;
      var to = Math.min(pos + FEED_MARGIN, last);
      if (to > _fedUntil) {
        feedChunks(_fedUntil + 1, to);
        _fedUntil = to;
      }
      if (to === last && _flushedEnd !== last) {
        _flushedEnd = last;
        _decoder.flush();
      }
      _pendingPos = pos;
      _pendingSeq = _segSeq;
      return;
    }
    redecode(pos);
  }

  /** 关闭并清空全部缓存帧(换段/回退重建时调用, 未 close 的帧会耗尽解码器 buffer 池导致输出停摆)。Callers: ensureSegment / redecode / resetDecoder。 */
  function clearFrames() {
    for (var pos in _frames) _frames[pos].close();
    _frames = {};
    _frameOrder = [];
    _maxOut = -1;
  }

  /** 窗口内最近一个关键帧位置(≤ pos); 无则 0。Callers: redecode。 */
  function lastKeyAtOrBefore(pos) {
    for (var i = pos; i >= 0; i--) {
      if (_seg.chunks[i] && _seg.chunks[i].key) return i;
    }
    return 0;
  }

  /** 构造 VideoDecoder, 解码结果经 onDecoded 收集。Callers: ensureSegment / redecode。 */
  function makeDecoder() {
    return new VideoDecoder({
      output: onDecoded,
      error: function (e) {
        console.error('VideoDecoder 解码错误', e);
      },
    });
  }

  /** 回退到已被淘汰的帧: 重建解码器并从目标前最近关键帧重新喂包。Callers: displayAt。 */
  function redecode(pos) {
    if (_decoder) { try { _decoder.close(); } catch (ignore) {} }
    _decoder = makeDecoder();
    _decoder.configure({
      codec: buildCodecString(_seg.sps),
      description: buildAvcConfig(_seg.sps, _seg.pps),
    });
    clearFrames();
    _flushedEnd = -1;
    var last = _seg.chunks.length - 1;
    var to = Math.min(pos + FEED_MARGIN, last);
    feedChunks(lastKeyAtOrBefore(pos), to);
    _fedUntil = to;
    _pendingPos = pos;
    _pendingSeq = _segSeq;
  }

  /**
   * 解码器输出回调: 帧 timestamp 即喂包时的段内位置, 帧按显示顺序流式到达。
   * 输出帧进入 LRU 缓存(超出 FRAME_CACHE 淘汰最早输出的), 目标位置到达时
   * 立即绘制。Callers: makeDecoder。
   */
  function onDecoded(frame) {
    var pos = frame.timestamp;
    if (_frames[pos]) _frames[pos].close();
    _frames[pos] = frame;
    _frameOrder.push(pos);
    if (pos > _maxOut) _maxOut = pos;
    while (_frameOrder.length > FRAME_CACHE) {
      var oldPos = _frameOrder.shift();
      var old = _frames[oldPos];
      delete _frames[oldPos];
      if (old) old.close();
    }
    if (_pendingPos === pos && _pendingSeq === _segSeq) {
      _pendingPos = -1;
      var idx = _seg ? _seg.start + pos : -1;
      if (idx >= 0 && idx === _pendingTarget) paintFrame(frame, idx);
    }
  }

  /** 把解码帧绘制到主预览 canvas 并同步状态(帧生命周期由 LRU 缓存统一管理, 此处不 close)。Callers: displayAt / onDecoded。 */
  function paintFrame(frame, idx) {
    var w = frame.displayWidth, h = frame.displayHeight;
    if (_canvas.width !== w || _canvas.height !== h) {
      _canvas.width = w;
      _canvas.height = h;
    }
    _cctx.drawImage(frame, 0, 0, w, h);
    state.idx = idx;
    paintStatus();
    updateCursor();
    syncStrip();
    var stage = $('ve-stage');
    if (stage) stage.classList.remove('loading');
  }

  /**
   * 解析 H.264 Annex-B 裸流: 提取 SPS/PPS 并按片 NAL 分组成帧。
   * SPS/PPS 只提取用于 configure 的 description, 不包含在 chunk 中(AVCC 格式
   * 要求参数集通过 description 传入, chunk 只含 slice 数据)。
   * 关键帧判定: 片 NAL 为 IDR(类型 5) 即关键帧。
   * Callers: ensureSegment。
   */
  function demuxAnnexB(bin) {
    var nals = [];
    var i = 0, n = bin.length;
    while (i < n) {
      // 找起始码(3 字节 00 00 01 或 4 字节 00 00 00 01)
      var scLen = 0;
      if (i + 3 <= n && bin[i] === 0 && bin[i + 1] === 0 && bin[i + 2] === 1) {
        scLen = 3;
      } else if (i + 4 <= n && bin[i] === 0 && bin[i + 1] === 0 && bin[i + 2] === 0 && bin[i + 3] === 1) {
        scLen = 4;
      }
      if (!scLen) { i++; continue; }
      i += scLen;
      var start = i;
      // 扫描到下一个起始码或末尾
      while (i < n) {
        if (i + 3 <= n && bin[i] === 0 && bin[i + 1] === 0 && bin[i + 2] === 1) break;
        if (i + 4 <= n && bin[i] === 0 && bin[i + 1] === 0 && bin[i + 2] === 0 && bin[i + 3] === 1) break;
        i++;
      }
      nals.push(bin.subarray(start, i));
    }
    var sps = null, pps = null;
    for (var k = 0; k < nals.length; k++) {
      var t = nals[k][0] & 0x1F;
      if (t === 7 && !sps) sps = nals[k];
      else if (t === 8 && !pps) pps = nals[k];
    }
    // 按片 NAL 分组为帧: 每个 slice NAL(类型 1 或 5)为一个独立 chunk,
    // SPS/PPS 已通过 configure 的 description 传入, 不包含在 chunk 中。
    var chunks = [];
    for (var m = 0; m < nals.length; m++) {
      var ty = nals[m][0] & 0x1F;
      if (ty === 1 || ty === 5) {
        chunks.push({ bytes: toLengthPrefixed([nals[m]]), key: ty === 5 });
      }
    }
    return { sps: sps, pps: pps, chunks: chunks };
  }

  /** 把一组 NAL 封装为 4 字节长度前缀的 AVCC 数据(WebCodecs 期望的 chunk 格式)。Callers: demuxAnnexB。 */
  function toLengthPrefixed(nals) {
    var total = 0;
    for (var i = 0; i < nals.length; i++) total += 4 + nals[i].length;
    var out = new Uint8Array(total);
    var dv = new DataView(out.buffer), off = 0;
    for (var j = 0; j < nals.length; j++) {
      dv.setUint32(off, nals[j].length);
      out.set(nals[j], off + 4);
      off += 4 + nals[j].length;
    }
    return out;
  }

  /** 由 SPS 构造 avc1 codec 字符串(profile.compat.level)。Callers: ensureSegment。 */
  function buildCodecString(sps) {
    function hex2(b) { return ('0' + b.toString(16)).slice(-2); }
    return 'avc1.' + hex2(sps[1]) + hex2(sps[2]) + hex2(sps[3]);
  }

  /** 由 SPS/PPS 构造 AVCDecoderConfigurationRecord 作为解码器 description。Callers: ensureSegment。 */
  function buildAvcConfig(sps, pps) {
    var cfg = new Uint8Array(11 + sps.length + pps.length);
    var dv = new DataView(cfg.buffer);
    dv.setUint8(0, 1);       // configurationVersion
    dv.setUint8(1, sps[1]);  // AVCProfileIndication
    dv.setUint8(2, sps[2]);  // profile_compatibility
    dv.setUint8(3, sps[3]);  // AVCLevelIndication
    dv.setUint8(4, 0xFF);    // lengthSizeMinusOne = 3(4 字节长度)
    dv.setUint8(5, 0xE1);    // numOfSequenceParameterSets = 1
    dv.setUint16(6, sps.length);
    cfg.set(sps, 8);
    var off = 8 + sps.length;
    dv.setUint8(off, 1);     // numOfPictureParameterSets
    dv.setUint16(off + 1, pps.length);
    cfg.set(pps, off + 3);
    return cfg;
  }

  /** 关闭解码器并清空窗口段与缓存状态(切换视频时调用).
   *
   *  Callers:
   *    - `setPath`
   */
  function resetDecoder() {
    if (_decoder) { try { _decoder.close(); } catch (ignore) {} }
    _decoder = null;
    _seg = null;
    _inflight = null; // 旧视频的窗口帧号对新视频无意义, 必须释放
    _segSeq++;
    clearFrames();
    _fedUntil = -1;
    _flushedEnd = -1;
    _pendingTarget = -1;
    _pendingPos = -1;
    _pendingSeq = -1;
  }

  /** 切换视频(上下一个), 重新加载详情并回到第 0 帧.
   *
   *  @param {number} offset 偏移量(-1 上一个, 1 下一个).
   *
   *  Callers:
   *    - `bindToolbar`
   *    - `bindKeys`
   */
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
   *
   * @param {string} p 视频相对路径.
   *
   * Callers:
   *   - `init`
   *   - `switchVideo`
   */
  async function setPath(p) {
    state.path = p;
    // 视频序号随当前路径同步定位, 是连续切换的游标
    state.vidIdx = -1;
    for (var i = 0; i < state.videoList.length; i++) {
      if (state.videoList[i].path === p) { state.vidIdx = i; break; }
    }
    state.idx = 0;
    state.total = 0;
    state.types = {};
    state.saved = [];
    stopAnalysis();
    resetDecoder();
    var strip = $('ve-strip');
    if (strip) strip.innerHTML = '';
    _stripItems = [];
    _chart = { global: null, local: null };
    paintStatus();
    await loadDetail();
    // 加载首帧; goto(0) 会因 idx===state.idx 提前返回而跳过首帧
    showFrame(0);
  }

  /**
   * 把后端紧凑位流(类型串 + 大小数组)还原为 ``{帧号: {type, size}}``。
   * 数组下标即帧序号, 由 detail 接口压缩传输以减小负载。
   * Callers: loadDetail。
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
   * Callers: setPath / analysisDone。
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
   * 保存当前帧 PNG 到 main 目录。
   *
   * Callers:
   *   - `bindToolbar` 存帧按钮
   *   - `bindStage` 左键
   *   - `bindKeys` S/空格
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
   * 删除当前帧已保存的 PNG。
   *
   * Callers:
   *   - `bindToolbar` 删帧按钮
   *   - `bindStage` 右键
   *   - `bindKeys` X/Delete
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
   * 绑定缩略图条悬停大图预览(事件委托): 悬停显示对应帧大图并跟随鼠标,
   * 防抖后加载大图避免快速划过时连续请求抽帧接口。
   * Callers: init。
   */
  function bindStripPreview() {
    var strip = $('ve-strip');
    var layer = $('ve-preview');
    if (!strip || !layer) return;
    var img = layer.querySelector('img');
    var tag = layer.querySelector('.tag');
    var timer = 0;
    strip.addEventListener('mouseover', function (e) {
      var box = e.target.closest ? e.target.closest('.ve-thumb') : null;
      if (!box) return;
      var idx = parseInt(box.dataset.idx, 10);
      if (isNaN(idx)) return;
      tag.textContent = String(idx);
      clearTimeout(timer);
      timer = setTimeout(function () {
        img.src = frameUrl(idx, 480);
      }, 100);
      layer.style.display = 'block';
    });
    strip.addEventListener('mousemove', function (e) {
      var w = layer.offsetWidth, h = layer.offsetHeight;
      var x = Math.min(Math.max(e.clientX - w / 2, 8), window.innerWidth - w - 8);
      var y = e.clientY - h - 14;
      if (y < 8) y = e.clientY + 18;
      layer.style.left = x + 'px';
      layer.style.top = y + 'px';
    });
    strip.addEventListener('mouseout', function (e) {
      if (e.target.closest && e.target.closest('.ve-thumb')) {
        clearTimeout(timer);
        layer.style.display = 'none';
      }
    });
  }

  /**
   * 按采样间隔构建缩略图条(目标 THUMB_COUNT 个)。
   * Callers: loadDetail。
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
        img.src = frameUrl(idx, THUMB_SIZE);
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
    syncStrip();
  }

  /** 同步缩略图条高亮: 当前帧与已保存帧。
   *
   *  Callers:
   *    - `goto`
   *    - `saveCurrent`
   *    - `deleteCurrent`
   *    - `analysisDone`
   */
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
   * 数据变化(分析完成/存取帧/换片/缩放)调用 drawChart 全量重建; 仅换帧调用
   * updateCursor 只移动游标并滚动居中, 开销最小。
   */
  var _chart = { global: null, local: null }; // 各图渲染信息: {svgId, ppf, svgW, viewportW, cursor}

  /** 显示域帧数: 隐藏 B 帧时仅计非 B 帧, 否则即总帧数。Callers: globalPxPerFrame / localPxPerFrame / renderChart。 */
  function domainCount() {
    if (!state.hideB) return state.total;
    var n = 0;
    for (var i = 0; i < state.total; i++) {
      var ft = state.types[i];
      if (ft && ft.type !== 'B') n++;
    }
    return n;
  }

  /** 帧索引映射到显示域序号(0 基): 隐藏 B 帧时为其左侧非 B 帧个数, 否则即帧号。Callers: cursorX / renderChart。 */
  function barOrdinal(idx) {
    if (!state.hideB) return idx;
    var n = 0;
    for (var i = 0; i < idx; i++) {
      var ft = state.types[i];
      if (ft && ft.type !== 'B') n++;
    }
    return n;
  }

  /**
   * 已保存帧在显示域中的可见位置: 非隐藏 B 帧时即自身; 隐藏 B 帧时若该帧为 B
   * 则向外搜索最近的非 B 帧作为落点, 使标记始终可见。Callers: renderChart。
   * @param {number} idx 已保存帧的全局索引。
   * @returns {number} 可见帧的全局索引。
   */
  function nearestVisibleIdx(idx) {
    if (!state.hideB) return idx;
    var ft = state.types[idx];
    if (ft && ft.type !== 'B') return idx;
    for (var d = 1; d < state.total; d++) {
      if (idx - d >= 0 && state.types[idx - d] && state.types[idx - d].type !== 'B') return idx - d;
      if (idx + d < state.total && state.types[idx + d] && state.types[idx + d].type !== 'B') return idx + d;
    }
    return idx;
  }

  /** 全局图每帧像素宽: 视口最多对应 10 分钟帧跨度; 视频更短时整条铺满视口(最多 10 分钟而非固定 10 分钟)。Callers: renderChart。 */
  function globalPxPerFrame(viewportW) {
    var domain = domainCount() || 1;
    var dur = state.duration || 0;
    var WIN_SEC = 600; // 10 分钟
    var winFrames = dur > 0 ? Math.max(1, Math.round((WIN_SEC * domain) / dur)) : domain;
    var showFrames = Math.min(winFrames, domain);
    return viewportW / showFrames;
  }

  /** 局部图每帧像素宽: 视口最多对应 200 帧; 视频更短时整条铺满视口。Callers: renderChart。 */
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
   * Callers: drawChart。
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

    // 已保存帧标记: 顶部彩色条, 颜色取帧类型(I 红/P 黄/B 蓝);
    // 隐藏 B 帧时已保存的 B 帧映射到最近的可见帧位置
    if (state.saved.length) {
      for (var s2 = 0; s2 < state.saved.length; s2++) {
        var saveIdx = state.saved[s2];
        var visIdx = nearestVisibleIdx(saveIdx);
        var saveFt = state.types[saveIdx];
        var markerColor = saveFt ? (FRAME_COLORS[saveFt.type] || '#888') : '#3ecf8e';
        var t = document.createElementNS(ns, 'rect');
        t.setAttribute('x', barOrdinal(visIdx) * ppf);
        t.setAttribute('y', 0);
        t.setAttribute('width', Math.max(2, ppf));
        t.setAttribute('height', 8);
        t.setAttribute('fill', markerColor);
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

  /** 当前帧游标在柱上的 x 坐标: 柱中心(避免落在两柱交界)。Callers: positionCursor / syncScroll。 */
  function cursorX(info) {
    return barOrdinal(state.idx) * info.ppf + info.ppf / 2;
  }

  /** 将游标移动到当前帧柱中心(仅移动 line, 不重建柱)。Callers: renderChart / updateCursor。 */
  function positionCursor(info) {
    if (!info || !info.cursor) return;
    var x = cursorX(info);
    info.cursor.setAttribute('x1', x);
    info.cursor.setAttribute('x2', x);
  }

  /** 数据变化时全量重建两图并滚动居中游标。
   *
   *  Callers:
   *    - `loadDetail`
   *    - `analysisDone`
   *    - `saveCurrent`
   *    - `deleteCurrent`
   *    - `bindResize`
   *    - `bindHideB`
   */
  function drawChart() {
    renderChart('ve-chart-global', globalPxPerFrame);
    renderChart('ve-chart-local', localPxPerFrame);
    syncScroll();
  }

  /** 仅移动游标并滚动居中(换帧时调用, 开销最小)。Callers: showFrame。 */
  function updateCursor() {
    positionCursor(_chart.global);
    positionCursor(_chart.local);
    syncScroll();
  }

  /** 滚动各图容器, 使当前帧游标尽量居中(边缘处贴边)。Callers: drawChart / updateCursor。 */
  function syncScroll() {
    for (var key in _chart) {
      var c = _chart[key];
      if (!c) continue;
      var container = $(c.svgId).parentElement;
      var target = cursorX(c) - c.viewportW / 2;
      container.scrollLeft = Math.max(0, Math.min(target, c.svgW - c.viewportW));
    }
  }

  /** 将点击坐标换算为帧索引并跳转(按各图每帧像素宽映射)。
   *
   *  Callers:
   *    - `bindChart`
   */
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

  /** 停止进行中的分析 SSE 订阅并隐藏进度条。Callers: setPath / toggleAnalysis。 */
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
   * Callers: toggleAnalysis。
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

  /** 设置分析结束状态文本并复位按钮。Callers: toggleAnalysis。 */
  function setStatus(t) {
    var prog = $('seg-extra');
    if (prog) prog.textContent = t;
    var btn = $('ve-analysis');
    if (btn) btn.textContent = '帧类型';
  }

  /**
   * 启动/停止位流帧类型分析长任务。
   *
   * Callers:
   *   - `bindToolbar` 分析按钮
   *   - `loadDetail` 自动分析
   */
  /**
   * 帧类型分析 SSE 回调(提交与重连共用).
   * @param {object} s JobSnapshot
   * 引用: ``toggleAnalysis`` / ``resumeAnalysis``.
   */
  function onAnalysisStatus(s) {
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
  }

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
    state.jobWatch = API.watchJob(data.job_id, onAnalysisStatus);
  }

  /**
   * 页面刷新后恢复未结束的帧类型分析任务.
   * 引用: ``init``.
   */
  async function resumeAnalysis() {
    var snap = await API.findActiveJob('video_analysis', API.dataset());
    if (!snap) return;
    state.analyzing = true;
    var btn = $('ve-analysis');
    if (btn) btn.textContent = '停止';
    setProgress(snap.pct || 0, snap.message || '恢复中…');
    state.jobWatch = API.watchJob(snap.job_id, onAnalysisStatus);
  }

  /** 分析完成后回读帧类型并重绘柱状图。Callers: toggleAnalysis。 */
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
   *
   * @param {number} step 步长(正向后, 负向前).
   *
   * Callers:
   *   - `bindToolbar`
   *   - `bindStage`
   *   - `bindKeys`
   *   - `bindHideB`
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

  /** 跳到下一个/上一个 I 帧(按方向 dir); 无 I 帧时保持当前帧。Callers: bindStage(Shift+滚轮)。 */
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

  /** 绑定工具栏按钮与主预览交互。Callers: init。 */
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

  /** 绑定"隐藏B帧"开关: 纯显示层切换, 重绘柱状图并落到非 B 帧(段与 B 帧无关, 无需重新请求)。Callers: init。 */
  function bindHideB() {
    var box = $('ve-hide-b');
    if (!box) return;
    state.hideB = box.checked;
    box.addEventListener('change', function () {
      state.hideB = box.checked;
      drawChart();
      paintStatus();
      // 开启隐藏 B 帧时, 若当前帧是 B 帧则跳到最近的 I/P 帧(stepFrame 内部调 showFrame)
      if (state.hideB) {
        var ft = state.types[state.idx];
        if (ft && ft.type === 'B') stepFrame(1);
      }
    });
  }

  /** 绑定帧类型柱状图交互(全局图 + 局部图): 点击跳帧; 垂直滚轮转水平滚动。Callers: init。 */
  function bindChart() {
    var g = $('ve-chart-global');
    var l = $('ve-chart-local');
    if (g) g.addEventListener('click', chartClick);
    if (l) l.addEventListener('click', chartClick);
  }

  /** 绑定主预览交互: 左键存帧、右键删帧、滚轮调帧。Callers: init。 */
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
   * Callers: init。
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

  /** 窗口尺寸变化时重绘画布。Callers: init。 */
  function bindResize() {
    var raf = null;
    window.addEventListener('resize', function () {
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(drawChart);
    });
  }

  /**
   * 页面初始化入口: 绑定 DOM 事件、加载视频列表、加载首帧。
   *
   * Callers:
   *   - `DOMContentLoaded` 事件监听器
   */
  async function init() {
    if (typeof API === 'undefined' || typeof API.dataset !== 'function') return;
    // 先拉取视频列表(A/D 切换游标与无 path 时自动选中第一个视频)
    try {
      var list = await API.request(API.apiPath('/videos'));
      state.videoList = list || [];
    } catch (e) {
      toast(String(e.message || e));
    }
    state.path = readQueryPath();
    if (!state.path && state.videoList.length) state.path = state.videoList[0].path;
    if (!state.path) {
      var stageDom = $('ve-stage');
      if (stageDom) stageDom.innerHTML = '<p style="padding:40px;color:var(--text-dim);">数据集 video 目录为空</p>';
      return;
    }
    if (typeof VideoDecoder === 'undefined' || typeof EncodedVideoChunk === 'undefined') {
      var uns = $('ve-stage');
      if (uns) uns.innerHTML = '<p style="padding:40px;color:var(--text-dim);">当前浏览器不支持 WebCodecs, 无法逐帧解码</p>';
      return;
    }
    _canvas = $('ve-canvas');
    if (_canvas) _cctx = _canvas.getContext('2d');
    bindToolbar();
    bindChart();
    bindStage();
    bindKeys();
    bindResize();
    bindHideB();
    bindStripPreview();

    // 横向滚动容器(图表×2 + 缩略图条): 垂直滚轮转水平滚动
    document.querySelectorAll('.ve-chart-scroll, .ve-strip').forEach(function (el) {
      el.addEventListener('wheel', function (e) {
        if (!e.deltaY) return;
        e.preventDefault();
        el.scrollLeft += e.deltaY;
      }, { passive: false });
    });

    await setPath(state.path);
    resumeAnalysis();
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