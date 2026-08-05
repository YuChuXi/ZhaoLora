/**
 * filmstrip.js —— 横排图像预览条（共享模块）。
 *
 * 供蒙板页 / 裁剪页 / 标签检查页复用：点击状态栏第一段(seg-pos)展开当前图片
 * 列表的横排缩略图，点击任意缩略图跳转到对应图片。逻辑与样式三页完全一致。
 *
 * 引用：crop.js / paint.js / tag_check.js 在各自 init 中 `Filmstrip.create`。
 */
var Filmstrip = (function () {
  'use strict';

  /**
   * 创建一个横排预览条控制器。
   *
   * Args:
   *   opts (object): 配置项
   *     container (HTMLElement): 预览条容器元素(id=filmstrip)。
   *     getItems (function): 返回当前图片列表。
   *     thumbSrc (function(item)): 计算单张缩略图 URL。
   *     getActive (function): 返回当前索引, 用于高亮。
   *     onSelect (function(index)): 点击缩略图跳转回调。
   *
   * Returns:
   *   object: { toggle, close, refresh, sync } 控制器方法。
   *
   * Callers:
   *   - `crop.js:init`
   *   - `paint.js:init`
   *   - `tag_check.js:init`
   */
  function create(opts) {
    var el = opts.container;
    var open = false;
    var built = false;

    /** 依据当前索引更新高亮。 Callers: build / toggle / sync。 */
    function renderActive() {
      var imgs = el.querySelectorAll('img');
      var active = opts.getActive();
      for (var i = 0; i < imgs.length; i++) {
        imgs[i].classList.toggle('active', i === active);
      }
    }

    /** 以当前列表重建缩略图。 Callers: toggle(首次展开) / refresh。 */
    function build() {
      el.textContent = '';
      var items = opts.getItems();
      for (var i = 0; i < items.length; i++) {
        (function (idx, item) {
          var img = document.createElement('img');
          img.src = opts.thumbSrc(item);
          img.title = item.stem || '';
          img.addEventListener('click', function () { opts.onSelect(idx); });
          el.appendChild(img);
        })(i, items[i]);
      }
      renderActive();
    }

    /**
     * 切换预览条显隐。首次展开时构建缩略图。
     *
     * Callers:
     *   - `crop.js` / `paint.js` / `tag_check.js`: 点击 seg-pos 时调用。
     */
    function toggle() {
      open = !open;
      el.hidden = !open;
      if (open && !built) {
        build();
        built = true;
      } else if (open) {
        renderActive();
      }
    }

    /** 关闭预览条。 Callers: `tag_check.js:hideSingle`。 */
    function close() {
      open = false;
      el.hidden = true;
    }

    /**
     * 列表数据变化后重建缩略图(展开状态下即时更新)。
     *
     * Callers:
     *   - `crop.js:loadImages`
     *   - `paint.js:loadImages`
     *   - `tag_check.js:runCheck`
     */
    function refresh() {
      built = false;
      el.textContent = '';
      if (open) build();
    }

    /**
     * 当前索引变化后同步高亮。
     *
     * Callers:
     *   - `crop.js:updateNav`
     *   - `paint.js:_applyCurrent`
     *   - `tag_check.js:renderSingle`
     */
    function sync() {
      if (open) renderActive();
    }

    return { toggle: toggle, close: close, refresh: refresh, sync: sync };
  }

  return { create: create };
})();