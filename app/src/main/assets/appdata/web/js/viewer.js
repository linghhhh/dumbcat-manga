/* 竖向条漫手势层：双指缩放 / 双击 2× / 放大后拖动平移。
 *
 * 为什么不直接用 WebView 的原生缩放：原生缩放会把整个页面一起放大，
 * 上下工具栏、翻页条全跟着跑，而且和「点击隐藏菜单」抢事件。
 * 这里只放大手指下那一张图，并且只在放大后接管拖动，scale === 1 时完全不拦截事件，
 * 单指上下滑照常滚动页面。
 *
 * 放大方式是把图片的布局宽度按倍数放大（height:auto 跟着变），而不是 transform：
 * 条漫一张图往往比屏幕高很多，transform 放大后滚不到图片边缘；布局放大后
 * .reader-pages 横向可滚，单指拖动 / 鼠标拖拽都能平移到任意位置。
 */

const MIN_SCALE = 1;
const DOUBLE_TAP_MS = 300;
const DOUBLE_TAP_SLOP = 28;
const DOUBLE_TAP_SCALE = 2;

function distance(a, b) {
  return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
}

export function createViewer(stage, options = {}) {
  if (!stage) return null;
  const maxZoom = Math.max(1.5, Number(options.maxZoom) || 4);
  const view = { scale: 1 };
  let activeImg = null;
  let pinch = null;
  let pan = null;
  let tap = null;
  let drag = null;
  let lastTapAt = 0;
  let lastTapX = 0;
  let lastTapY = 0;
  let lastTouchDoubleAt = -1;

  /* 用坐标找图：滚动模式下屏幕上有好几张图，不能像单页那样取第一张 */
  function imgAt(clientX, clientY) {
    const node = document.elementFromPoint(clientX, clientY);
    const img = node && node.closest ? node.closest('img') : null;
    return img && stage.contains(img) ? img : null;
  }

  function pointInView(clientX, clientY) {
    const rect = stage.getBoundingClientRect();
    return { x: clientX - rect.left, y: clientY - rect.top };
  }

  /* 放大时 .reader-pages 需要能横向滚（纵向本来就能滚），恢复到 1× 时还回去 */
  function setZoomed(on) {
    stage.classList.toggle('is-zoomed', on);
    stage.style.overflowX = on ? 'auto' : '';
    stage.style.cursor = on ? 'grab' : '';
  }

  function suppressClick(ms = 420) {
    stage.dataset.suppressUntil = String(Date.now() + ms);
  }

  /* 记录一次缩放的起点：图片左上角在滚动内容里的位置，以及锚点像素相对它的偏移。
     内容坐标不受缩放影响，后面每次设宽度都用它重算滚动位置，手指下的像素才不会跑。 */
  function capture(img, fx, fy) {
    const sr = stage.getBoundingClientRect();
    const ir = img.getBoundingClientRect();
    const scale = view.scale;
    const left = ir.left - sr.left + stage.scrollLeft;
    const top = ir.top - sr.top + stage.scrollTop;
    return {
      img,
      scale,
      width: ir.width / scale,
      left,
      top,
      dx: stage.scrollLeft + fx - left,
      dy: stage.scrollTop + fy - top,
    };
  }

  function layout(base, target, fx, fy) {
    const next = Math.min(maxZoom, Math.max(MIN_SCALE, target));
    const ratio = next / base.scale;
    if (next <= MIN_SCALE + 0.001) {
      base.img.style.width = '';
      base.img.style.maxWidth = '';
      view.scale = MIN_SCALE;
      activeImg = null;
      setZoomed(false);
    } else {
      /* 全局样式有 max-width:100%，不解除的话宽度会被夹回容器宽，放大看不到效果 */
      base.img.style.maxWidth = 'none';
      base.img.style.width = `${Math.round(base.width * next)}px`;
      view.scale = next;
      activeImg = base.img;
      setZoomed(true);
    }
    /* 让锚点像素继续停在 (fx, fy)；越过滚动边界时浏览器会自动夹紧 */
    stage.scrollLeft = Math.max(0, base.left + base.dx * ratio - fx);
    stage.scrollTop = Math.max(0, base.top + base.dy * ratio - fy);
  }

  function zoomBy(img, next, fx, fy) {
    if (!img) return;
    layout(capture(img, fx, fy), next, fx, fy);
  }

  function reset() {
    stage.querySelectorAll('img').forEach((img) => {
      img.style.width = '';
      img.style.maxWidth = '';
    });
    view.scale = MIN_SCALE;
    activeImg = null;
    pinch = null;
    pan = null;
    tap = null;
    drag = null;
    setZoomed(false);
  }

  function toggleZoom(clientX, clientY) {
    const p = pointInView(clientX, clientY);
    if (view.scale > MIN_SCALE + 0.01) {
      const img = (activeImg && stage.contains(activeImg)) ? activeImg : imgAt(clientX, clientY);
      if (!img) return;
      zoomBy(img, MIN_SCALE, p.x, p.y);
    } else {
      const img = imgAt(clientX, clientY);
      if (!img) return;
      zoomBy(img, DOUBLE_TAP_SCALE, p.x, p.y);
    }
    suppressClick();
  }

  function handleTap(clientX, clientY) {
    const now = performance.now();
    if (now - lastTapAt < DOUBLE_TAP_MS
      && Math.hypot(clientX - lastTapX, clientY - lastTapY) < DOUBLE_TAP_SLOP) {
      lastTapAt = 0;
      lastTouchDoubleAt = now;
      toggleZoom(clientX, clientY);
      return;
    }
    lastTapAt = now;
    lastTapX = clientX;
    lastTapY = clientY;
  }

  /* ---------------- 触摸 ---------------- */
  function startPinch(touches) {
    const a = touches[0];
    const b = touches[1];
    const midX = (a.clientX + b.clientX) / 2;
    const midY = (a.clientY + b.clientY) / 2;
    const img = imgAt(midX, midY) || imgAt(a.clientX, a.clientY) || imgAt(b.clientX, b.clientY);
    if (!img) {
      pinch = null;
      return;
    }
    /* 同一时间只放大一张图：从另一张图起手时，先把上一张收回去 */
    if (view.scale > MIN_SCALE + 0.001 && activeImg && activeImg !== img) reset();
    const p = pointInView(midX, midY);
    pinch = { base: capture(img, p.x, p.y), startDist: Math.max(1, distance(a, b)) };
  }

  function onTouchStart(event) {
    const touches = event.touches;
    if (touches.length >= 2) {
      tap = null;
      pan = null;
      startPinch(touches);
      return;
    }
    if (touches.length !== 1) return;
    const t = touches[0];
    tap = { x: t.clientX, y: t.clientY, at: Date.now(), moved: 0 };
    if (view.scale > MIN_SCALE + 0.001) {
      /* 放大后单指拖动 = 在放大图上平移；scale === 1 时什么都不做，把滚动留给页面 */
      pan = {
        x: t.clientX,
        y: t.clientY,
        left: stage.scrollLeft,
        top: stage.scrollTop,
      };
    }
  }

  function onTouchMove(event) {
    if (pinch) {
      if (event.touches.length < 2) return;
      event.preventDefault();
      const a = event.touches[0];
      const b = event.touches[1];
      const midX = (a.clientX + b.clientX) / 2;
      const midY = (a.clientY + b.clientY) / 2;
      const p = pointInView(midX, midY);
      const target = pinch.base.scale * (distance(a, b) / pinch.startDist);
      layout(pinch.base, target, p.x, p.y);
      return;
    }
    if (!event.touches.length) return;
    const t = event.touches[0];
    if (tap) tap.moved = Math.max(tap.moved, Math.hypot(t.clientX - tap.x, t.clientY - tap.y));
    if (!pan || view.scale <= MIN_SCALE + 0.001) return;
    event.preventDefault();
    stage.scrollLeft = pan.left - (t.clientX - pan.x);
    stage.scrollTop = pan.top - (t.clientY - pan.y);
  }

  function onTouchEnd(event) {
    pan = null;
    if (pinch) {
      if (event.touches.length >= 2) return;
      pinch = null;
      if (view.scale <= MIN_SCALE + 0.02) reset();
      return;
    }
    if (!tap) return;
    const t = event.changedTouches && event.changedTouches[0];
    const fresh = t && Date.now() - tap.at < 500 && tap.moved < 12;
    tap = null;
    if (fresh) handleTap(t.clientX, t.clientY);
  }

  /* ---------------- 鼠标（桌面 / 调试） ---------------- */
  function onMouseDown(event) {
    if (event.button !== 0 || view.scale <= MIN_SCALE + 0.001) return;
    drag = { x: event.clientX, y: event.clientY, left: stage.scrollLeft, top: stage.scrollTop };
    event.preventDefault();
    stage.style.cursor = 'grabbing';
  }

  function onMouseMove(event) {
    if (!drag) return;
    stage.scrollLeft = drag.left - (event.clientX - drag.x);
    stage.scrollTop = drag.top - (event.clientY - drag.y);
  }

  function onMouseUp() {
    if (!drag) return;
    drag = null;
    if (view.scale > MIN_SCALE + 0.001) stage.style.cursor = 'grab';
  }

  function onWheel(event) {
    if (!event.ctrlKey) return; /* 普通滚轮交给页面滚动 */
    const img = imgAt(event.clientX, event.clientY);
    if (!img) return;
    event.preventDefault();
    const factor = Math.exp(-event.deltaY * 0.0025);
    if (view.scale > MIN_SCALE + 0.001 && activeImg && activeImg !== img) reset();
    const p = pointInView(event.clientX, event.clientY);
    zoomBy(img, view.scale * factor, p.x, p.y);
  }

  function onDblClick(event) {
    /* 手机上双击已经由 touchend 处理，浏览器随后补发的 dblclick 不要再来一次 */
    if (performance.now() - lastTouchDoubleAt < 700) {
      event.preventDefault();
      return;
    }
    if (!imgAt(event.clientX, event.clientY)) return;
    event.preventDefault();
    toggleZoom(event.clientX, event.clientY);
  }

  stage.addEventListener('touchstart', onTouchStart, { passive: true });
  stage.addEventListener('touchmove', onTouchMove, { passive: false });
  stage.addEventListener('touchend', onTouchEnd, { passive: true });
  stage.addEventListener('touchcancel', onTouchEnd, { passive: true });
  stage.addEventListener('mousedown', onMouseDown);
  window.addEventListener('mousemove', onMouseMove);
  window.addEventListener('mouseup', onMouseUp);
  stage.addEventListener('wheel', onWheel, { passive: false });
  stage.addEventListener('dblclick', onDblClick);

  return {
    reset,
    get scale() { return view.scale; },
    destroy() {
      reset();
      stage.removeEventListener('touchstart', onTouchStart);
      stage.removeEventListener('touchmove', onTouchMove);
      stage.removeEventListener('touchend', onTouchEnd);
      stage.removeEventListener('touchcancel', onTouchEnd);
      stage.removeEventListener('mousedown', onMouseDown);
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
      stage.removeEventListener('wheel', onWheel);
      stage.removeEventListener('dblclick', onDblClick);
    },
  };
}
