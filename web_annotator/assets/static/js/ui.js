/**
 * ui.js —— 全局 UI 原语，供所有页面共享。
 *
 * 引用：所有使用 toast 的模板页面（index/scoring/dedupe/crop/paint/tags/
 *       tag_check/ready/home）。
 */
(function () {
  'use strict';

  var _toastTimer = null;

  /**
   * 显示短暂提示。
   * @param {string} msg
   */
  function toast(msg) {
    var el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  /**
   * 显示错误提示(带 ⚠ 前缀)。引用：各页面业务错误、全局 error/unhandledrejection。
   * @param {string} msg
   */
  function errshow(msg) {
    toast('\u26a0 ' + msg);
  }

  /**
   * 转义 HTML 特殊字符, 防止注入。引用：tag_check、scoring 等渲染用户数据的页面。
   * @param {string} s
   * @returns {string}
   */
  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
  }

  // 全局未捕获错误统一走 errshow, 避免各页面各自绑定监听
  window.addEventListener('error', function (e) { errshow(e.message); });
  window.addEventListener('unhandledrejection', function (e) {
    errshow(String(e.reason && e.reason.message ? e.reason.message : e.reason));
  });

  window.toast = toast;
  window.errshow = errshow;
  window.escHtml = escHtml;
})();
