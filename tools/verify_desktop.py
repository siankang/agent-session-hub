"""桌面窗口端到端自检（不依赖打包，源码运行）。

不使用 window.evaluate_js：pywebview 的 WinForms 后端在每次 evaluate_js 时
会遍历 window.native 对象图，在 pythonnet 3.2 下会递归报错且极慢。
改为在页面内注入探针，通过真实的 JS -> Python 桥接回报结果。

只写 exports/ 下一个测试导出文件，不改动任何 Agent 源数据。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8")

import webview  # noqa: E402

from core.bridge import Bridge  # noqa: E402
from core.cleanup import CleanupManager  # noqa: E402
from core.scanner import Scanner  # noqa: E402
from core.server import API  # noqa: E402
from core.store import Store  # noqa: E402
from desktop import build_html, storage_path  # noqa: E402

PROBE_JS = """
<script>
window.addEventListener('pywebviewready', async () => {
  const rep = (k,v) => window.pywebview.api.report(k,v);
  try {
    const t0 = performance.now();
    const b = await window.pywebview.api.call('GET','/api/bootstrap');
    await rep('bootstrap', {ms:Math.round(performance.now()-t0),
      sessions:b.overview.sessions, sources:b.sources.length,
      cleanup_allowed:b.cleanup_allowed});
    const t1 = performance.now();
    const s = await window.pywebview.api.call('GET','/api/sessions?size=5');
    await rep('sessions', {ms:Math.round(performance.now()-t1), total:s.total, rows:s.items.length});
    const q = await window.pywebview.api.call('GET','/api/search?q=%E4%BC%9A%E8%AF%9D');
    await rep('search', {hits:q.items.length});
    const st = await window.pywebview.api.call('GET','/api/storage');
    await rep('storage', {assets:st.assets.length});
    const ex = await window.pywebview.api.call('GET','/api/export?scope=source&format=md&id=workbuddy');
    await rep('export', {saved:!!ex.__saved__, path:(ex.__saved__||'').slice(-60)});
    // 唯一确认关卡：错误确认词被拒；正确确认词不再因「未解锁」被拒
    const cw1=await window.pywebview.api.call('POST','/api/cleanup/execute',
      {plan_id:'__no_such_plan__',confirm:'随便'});
    const cw2=await window.pywebview.api.call('POST','/api/cleanup/execute',
      {plan_id:'__no_such_plan__',confirm:'确认清理'});
    await rep('confirm', {wrong:(cw1&&cw1.error)||'', right:(cw2&&cw2.error)||'',
      noUnlockError:!String((cw2&&cw2.error)||'').includes('未解锁')});
    await new Promise(r => setTimeout(r, 1200));
    await rep('dom', {
      nav:document.querySelectorAll('#nav button').length,
      k2:(document.querySelector('#k2')||{}).textContent,
      recent:document.querySelectorAll('#recent .row').length,
      sessionRows:document.querySelectorAll('#sessionList .row').length,
      desktop:!!window.__DESKTOP__, started:!!window.__appStarted
    });
    await rep('__done__', true);
  } catch(e) { await rep('__error__', String(e)); }
});
</script>
"""


def main():
    store = Store(BASE / "data" / "index.db")
    scanner = Scanner(store)
    cleanup = CleanupManager(BASE)
    api = API(store, scanner, cleanup)
    bridge = Bridge(api, cleanup, BASE)
    html = build_html().replace("</body>", PROBE_JS + "</body>")
    window = webview.create_window(
        "Agent 会话管理台 - 桌面自检", html=html, js_api=bridge,
        width=1280, height=820, background_color="#0D1016")
    # 不设 bridge.window：导出走「自动保存到 exports/」回退，避免自动化卡在另存为对话框
    bridge.window = None

    def close_when_done():
        for _ in range(60):
            if bridge.reports.get("__done__") or bridge.reports.get("__error__"):
                break
            time.sleep(0.5)
        time.sleep(0.6)
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=close_when_done, daemon=True).start()
    webview.start(gui="edgechromium", private_mode=False,
                  storage_path=str(storage_path()))

    r = bridge.reports
    for k in ("bootstrap", "sessions", "search", "storage", "export",
              "confirm", "dom", "__error__"):
        if k in r:
            print(f"  {k:<16} {r[k]}")
    cf = r.get("confirm", {})
    ok = (r.get("__done__") and not r.get("__error__")
          and r.get("bootstrap", {}).get("sessions", 0) > 0
          and r.get("sessions", {}).get("total", 0) > 0
          and r.get("search", {}).get("hits", 0) > 0
          and r.get("export", {}).get("saved")
          and bool(cf.get("wrong"))
          and cf.get("noUnlockError") is True
          and "计划不存在" in cf.get("right", "")
          and r.get("dom", {}).get("nav") == 7
          and r.get("dom", {}).get("desktop") is True)
    print("\nDESKTOP SELF-CHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
