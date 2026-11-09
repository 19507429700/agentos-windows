// 图片识字、PDF 取文字（macOS 自带的 Vision 和 PDFKit，不装任何东西），供 extract_text.py 调用。
// 用法：osascript -l JavaScript ops/extract_text.js <文件…>
// 每个文件输出一行 JSON：
//   图片：{"path", "ok": true, "kind": "ocr", "text"}
//   PDF ：{"path", "ok": true, "kind": "pdf", "pages": [{"n": 页码, "how": "text"|"ocr", "text"}], "total": 总页数}
//   失败：{"path", "ok": false, "error"}
// PDF 每页先取文字层；文字层几乎是空的（扫描版），就把这一页渲染成图片再识字。
ObjC.import('Foundation');
ObjC.import('Vision');
ObjC.import('PDFKit');
ObjC.import('AppKit');

const MAX_PDF_PAGES = 300;     // 再长的 PDF 只取前 300 页
// 文字层少于这么多字，就再识一遍字，取字多的那个。扫描版 PDF 常常只有页眉、页码是文字层，
// 正文全在图片里；只看「有没有文字层」会把整页正文漏掉。
const MIN_PAGE_TEXT = 200;
const RENDER_LONG_SIDE = 2000; // 扫描页渲染成图片时长边的像素

function recognize(handler) {
  const req = $.VNRecognizeTextRequest.alloc.init;
  req.recognitionLevel = 0;            // 0 = 精确模式
  req.usesLanguageCorrection = true;
  req.recognitionLanguages = $(['zh-Hans', 'zh-Hant', 'en-US']);
  if (!handler.performRequestsError($([req]), null)) throw new Error('识字失败');
  const out = [];
  const rs = req.results;
  for (let i = 0; i < rs.count; i++) {
    const c = rs.objectAtIndex(i).topCandidates(1);
    if (c.count > 0) out.push(c.firstObject.string.js);
  }
  return out.join('\n');
}

function ocrFile(path) {
  const url = $.NSURL.fileURLWithPath(path);
  return recognize($.VNImageRequestHandler.alloc.initWithURLOptions(url, $({})));
}

function pdfPages(path) {
  const doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(path));
  if (doc.isNil()) throw new Error('打不开这个 PDF（可能加密或损坏）');
  const total = Number(doc.pageCount);
  const pages = [];
  for (let i = 0; i < Math.min(total, MAX_PDF_PAGES); i++) {
    const pg = doc.pageAtIndex(i);
    const layer = pg.string.isNil() ? '' : pg.string.js.trim();
    if (layer.replace(/\s/g, '').length >= MIN_PAGE_TEXT) {
      pages.push({n: i + 1, how: 'text', text: layer});
      continue;
    }
    const b = pg.boundsForBox(0); // 0 = MediaBox
    const scale = RENDER_LONG_SIDE / Math.max(b.size.width, b.size.height, 1);
    const img = pg.thumbnailOfSizeForBox($.NSMakeSize(b.size.width * scale, b.size.height * scale), 0);
    const cg = img.CGImageForProposedRectContextHints(null, $(), $());
    const ocr = recognize($.VNImageRequestHandler.alloc.initWithCGImageOptions(cg, $({}))).trim();
    const count = s => s.replace(/\s/g, '').length;
    pages.push(count(ocr) > count(layer) ? {n: i + 1, how: 'ocr', text: ocr} : {n: i + 1, how: 'text', text: layer});
  }
  return {pages, total};
}

function run(argv) {
  const lines = [];
  for (const path of argv) {
    try {
      if (path.toLowerCase().endsWith('.pdf')) {
        const r = pdfPages(path);
        lines.push(JSON.stringify({path, ok: true, kind: 'pdf', pages: r.pages, total: r.total}));
      } else {
        lines.push(JSON.stringify({path, ok: true, kind: 'ocr', text: ocrFile(path)}));
      }
    } catch (e) {
      lines.push(JSON.stringify({path, ok: false, error: String(e)}));
    }
  }
  return lines.join('\n');
}
