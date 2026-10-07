/*
 * text-anti-overlap.js — SVG <text> 防重叠后处理
 *
 * 用法（注入到已生成的 HTML 动画里）：
 *   <script src="text-anti-overlap.js"></script>
 *   或直接内联整个文件内容到 </body> 前。
 *
 * 原理：动画渲染完成后，遍历每个 <svg class="scene"> 内的 <text>，
 * 用 getBBox() 拿真实边界盒；逐组（按 x 中心聚类）排序后，若与上一个
 * 垂直重叠则沿 y 下推；下推导致溢出舞台底部时，把该组整体上移归位。
 * 只改 transform，不改原始属性，可重复运行、可回滚。
 */
(function () {
  'use strict';

  const PADDING = 3;        // 文本之间最小垂直间隙（px）
  const CLUSTER_DX = 6;     // x 中心差小于此值视为同一列（参与纵向避让）
  const SETTLE_MS = 350;    // 字体/布局就绪后等待再跑

  function boxesOverlap(a, b, pad) {
    pad = pad || 0;
    return !(a.x + a.w + pad <= b.x || b.x + b.w + pad <= a.x ||
             a.y + a.h + pad <= b.y || b.y + b.h + pad <= a.y);
  }

  // 取一个 text 元素相对其所在 svg 的边界盒（已含 transform）
  function bboxIn(el, svg) {
    let b;
    try { b = el.getBBox(); } catch (e) { return null; }
    const bw = b.width, bh = b.height; // getBBox 返回 width/height，不是 w/h
    const m = el.getCTM(); // 含 transform 的屏幕矩阵
    if (!m) return { x: b.x, y: b.y, w: bw, h: bh };
    // 把局部 bbox 四角映射到 svg 用户坐标
    const pts = [
      { x: b.x, y: b.y },
      { x: b.x + bw, y: b.y },
      { x: b.x, y: b.y + bh },
      { x: b.x + bw, y: b.y + bh },
    ];
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const p of pts) {
      const x = m.a * p.x + m.c * p.y + m.e;
      const y = m.b * p.x + m.d * p.y + m.f;
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x > maxX) maxX = x;
      if (y > maxY) maxY = y;
    }
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }

  function clampViewBox(texts, vb) {
    // 若有 text 被推到 viewBox 底部之外，把整组朝上压缩
    const bottom = vb.h;
    let pushed = texts.filter(t => t.box && (t.box.y + t.box.h) > bottom - 1);
    if (!pushed.length) return;
    const maxOverflow = Math.max(...pushed.map(t => t.box.y + t.box.h - bottom));
    // 整组上移 maxOverflow，但不超过顶部
    const minY = Math.min(...texts.filter(t => t.box).map(t => t.box.y));
    const shift = -Math.min(maxOverflow + PADDING, Math.max(0, minY - 4));
    if (shift === 0) return;
    for (const t of texts) {
      if (!t.box) continue;
      applyShift(t, 0, shift);
    }
  }

  function applyShift(t, dx, dy) {
    if (dx === 0 && dy === 0) return;
    const cur = t.el.getAttribute('transform') || '';
    // 叠加一个 translate；保留已有 transform
    t.el.setAttribute('transform',
      (cur ? cur + ' ' : '') + `translate(${dx.toFixed(2)} ${dy.toFixed(2)})`);
    t.box.y += dy;
    t.box.x += dx;
  }

  function processScene(svg) {
    const vb = svg.viewBox && svg.viewBox.baseVal;
    const W = (vb && vb.width) || svg.clientWidth || 1000;
    const H = (vb && vb.height) || svg.clientHeight || 600;
    const texts = Array.from(svg.querySelectorAll('text'))
      .map(el => {
        // 编辑器手动定位过的文字（data-fs-edit）不参与避让：用户放哪就是哪
        if (el.hasAttribute && el.hasAttribute('data-fs-edit')) return null;
        // 跳过 display:none / 不可见
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return null;
        const box = bboxIn(el, svg);
        if (!box || box.w <= 0 || box.h <= 0) return null;
        return { el, box, cx: box.x + box.w / 2 };
      })
      .filter(Boolean);

    if (texts.length < 2) return;

    let moved = 0;

    // 纵向避让：按 cx 聚成列，列内按 y 排序，重叠则下推
    const byCol = [];
    const sorted = texts.slice().sort((a, b) => a.cx - b.cx);
    for (const t of sorted) {
      let placed = false;
      for (const col of byCol) {
        if (Math.abs(col.cx - t.cx) < CLUSTER_DX) { col.items.push(t); placed = true; break; }
      }
      if (!placed) byCol.push({ cx: t.cx, items: [t] });
    }
    for (const col of byCol) {
      col.items.sort((a, b) => a.box.y - b.box.y);
      for (let i = 1; i < col.items.length; i++) {
        const prev = col.items[i - 1];
        const cur = col.items[i];
        const gap = cur.box.y - (prev.box.y + prev.box.h);
        if (gap < PADDING) {
          const dy = PADDING - gap;
          applyShift(cur, 0, dy);
          moved++;
        }
      }
    }

    if (moved) clampViewBox(texts, { w: W, h: H });

    // 横向避让：跨列整体水平重叠（x 完全交叠且 y 交叠）则右推
    const all = texts.filter(t => t.box).sort((a, b) => a.box.x - b.box.x);
    for (let i = 1; i < all.length; i++) {
      const prev = all[i - 1];
      const cur = all[i];
      // 只处理 y 上也重叠且 x 严重重叠的
      if (boxesOverlap(prev.box, cur.box, PADDING) &&
          cur.box.x < prev.box.x + prev.box.w) {
        const dx = prev.box.x + prev.box.w + PADDING - cur.box.x;
        // 仅当右推不溢出 viewBox 才推
        if (cur.box.x + cur.box.w + dx <= W) {
          applyShift(cur, dx, 0);
          moved++;
        }
      }
    }

    return moved;
  }

  function run() {
    const scenes = document.querySelectorAll('svg.scene, svg[class*="scene"]');
    let totalMoved = 0, sceneCount = 0;
    if (!scenes.length) {
      // 兜底：所有 svg
      const svgs = document.querySelectorAll('svg');
      svgs.forEach(s => { const m = processScene(s); if (m) totalMoved += m; sceneCount++ });
    } else {
      scenes.forEach(s => { const m = processScene(s); if (m) totalMoved += m; sceneCount++ });
    }
    console.log(`[text-anti-overlap] processed ${sceneCount} scene(s), moved ${totalMoved} text element(s)`);
  }

  function init() {
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(() => setTimeout(run, SETTLE_MS));
    } else {
      setTimeout(run, SETTLE_MS);
    }
    window.addEventListener('load', () => setTimeout(run, SETTLE_MS + 200));
  }

  if (document.readyState === 'complete') init();
  else window.addEventListener('DOMContentLoaded', init);

  // 暴露手动重跑入口
  window.textAntiOverlap = { run, processScene };
})();
