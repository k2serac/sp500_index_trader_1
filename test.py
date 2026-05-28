
  from libs.trade_lib import _cdp_evaluate, _cdp_ws_for_tab
  ws = _cdp_ws_for_tab("https://unusualwhales.com/periscope/market-exposure")
  result = _cdp_evaluate(ws, r"""
  (function() {
      const svgs = document.querySelectorAll('svg');
      const canvas = document.querySelectorAll('canvas');
      return JSON.stringify({
          svg_count: svgs.length,
          canvas_count: canvas.length,
          svg_sizes: Array.from(svgs).slice(0, 5).map(s =>
  ({
              w: s.getBoundingClientRect().width,
              h: s.getBoundingClientRect().height,
              viewBox: s.getAttribute('viewBox'),
              id: s.id,
          })),
      }, null, 2);
  })()
  """)
  print(result)

