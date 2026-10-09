"""Build the GovAward and WHALE-OS legend sheets (HTML + PDF) from live screenshots.

Each numbered marker is pinned to the real on-screen position of the panel it explains
(measured with getBoundingClientRect), so the legend matches the dashboard as it is.
Run on the VPS:  /root/pwtool/bin/python legend_build.py
"""
import html
import json
import pathlib
import time

from playwright.sync_api import sync_playwright

from legend_content import GOV, WHALE

CHROME = "/usr/bin/google-chrome"
ARGS = ["--no-sandbox", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]

MEASURE = """(items) => items.map(([n, sel, up]) => {
  let el = document.querySelector(sel); if (!el) return [n, null];
  if (up) el = el.closest(up) || el;
  const r = el.getBoundingClientRect();
  return [n, {x: r.left, y: r.top, w: r.width, h: r.height}];
})"""


def capture(page, sys_, outdir):
    shots = []
    for k, shot in enumerate(sys_["shots"]):
        # scrollIntoView works whether the page scrolls the window (GovAward) or an inner
        # panel (the WHALE-OS HUD scrolls <main class="os">, so window.scrollTo does nothing)
        if shot.get("scroll"):
            page.evaluate("(s) => { const e = document.querySelector(s); if (e) e.scrollIntoView({block: 'start'}); }", shot["scroll"])
        else:
            page.evaluate("() => { window.scrollTo(0, 0); document.querySelectorAll('main, .os').forEach(e => e.scrollTo && e.scrollTo(0, 0)); }")
        time.sleep(shot.get("settle", 2.5))
        boxes = dict(page.evaluate(MEASURE, [[i["n"], i["sel"], i.get("up")] for i in shot["items"]]))
        name = f"{sys_['slug']}_view{k + 1}.jpg"
        page.screenshot(path=str(outdir / name), type="jpeg", quality=84)
        vw, vh = page.viewport_size["width"], page.viewport_size["height"]
        shots.append({"img": name, "vw": vw, "vh": vh, "boxes": boxes, **shot})
    return shots


def marker_html(shot):
    out = []
    for it in shot["items"]:
        b = shot["boxes"].get(it["n"])
        if not b or b["y"] > shot["vh"] - 10 or b["y"] + b["h"] < 0:
            print(f"  ! marker {it['n']} ({it['sel']}) not on screen - listed without a pin")
            continue
        ox, oy = it.get("dx", 10), it.get("dy", 10)
        x = min(max(b["x"] + ox, 4), shot["vw"] - 30) / shot["vw"] * 100
        y = min(max(b["y"] + oy, 4), shot["vh"] - 30) / shot["vh"] * 100
        frame = (f'<i class="box" style="left:{b["x"] / shot["vw"] * 100:.2f}%;top:{max(0, b["y"]) / shot["vh"] * 100:.2f}%;'
                 f'width:{b["w"] / shot["vw"] * 100:.2f}%;height:{min(b["h"], shot["vh"] - max(0, b["y"])) / shot["vh"] * 100:.2f}%"></i>') if it.get("frame", True) else ""
        out.append(f'{frame}<b class="mk" style="left:{x:.2f}%;top:{y:.2f}%">{it["n"]}</b>')
    return "".join(out)


def page_html(sys_, shots):
    T = sys_["theme"]
    css = f"""
@page {{ size: Letter; margin: 0; }}
* {{ box-sizing: border-box; }}
html {{ background: {T['paper']}; }}
body {{ margin: 0; font: 9.2pt/1.38 {T['body']}; color: {T['ink']}; background: {T['paper']};
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
.page {{ width: 8.5in; height: 11in; padding: 0.42in; overflow: hidden; background: {T['paper']};
  page-break-after: always; break-after: page; }}
.ref {{ font-size: 8.5pt; line-height: 1.32; }}
.ref td, .ref th {{ padding: 2.5px 6px; }}
.ref h2 {{ margin: 10px 0 4px; }}
.page:last-child {{ page-break-after: auto; break-after: auto; }}
h1 {{ font: 700 20pt/1.05 {T['head']}; letter-spacing: .06em; margin: 0; color: {T['accent']}; }}
h1 small {{ font: 600 9pt {T['head']}; letter-spacing: .28em; color: {T['muted']}; margin-left: 10px; }}
h2 {{ font: 700 11pt {T['head']}; letter-spacing: .14em; text-transform: uppercase; margin: 14px 0 6px; color: {T['accent']}; }}
.lede {{ color: {T['muted']}; margin: 4px 0 10px; }}
.shot {{ position: relative; border: 1px solid {T['line']}; }}
.shot img {{ display: block; width: 100%; }}
.box {{ position: absolute; border: 1px dashed {T['frame']}; border-radius: 3px; }}
.mk {{ position: absolute; transform: translate(-2px,-2px); min-width: 19px; height: 19px; padding: 0 4px; border-radius: 10px;
  background: {T['pin']}; color: {T['pinInk']}; font: 700 9pt/19px {T['head']}; text-align: center;
  box-shadow: 0 0 0 2px {T['paper']}, 0 1px 4px rgba(0,0,0,.6); }}
ol.key {{ columns: 2; column-gap: 22px; margin: 10px 0 0; padding: 0; list-style: none; }}
ol.key li {{ break-inside: avoid; margin: 0 0 7px; padding-left: 28px; position: relative; }}
ol.key li .n {{ position: absolute; left: 0; top: 0; min-width: 20px; height: 18px; border-radius: 9px; background: {T['pin']};
  color: {T['pinInk']}; font: 700 8.5pt/18px {T['head']}; text-align: center; }}
ol.key li b {{ color: {T['strong']}; }}
table {{ width: 100%; border-collapse: collapse; margin: 2px 0 8px; }}
td, th {{ text-align: left; vertical-align: top; padding: 4px 7px; border-bottom: 1px solid {T['line']}; }}
th {{ font: 700 8pt {T['head']}; letter-spacing: .12em; text-transform: uppercase; color: {T['muted']}; }}
td:first-child {{ width: 30%; font-weight: 600; color: {T['strong']}; }}
.grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
.box2 {{ border: 1px solid {T['line']}; padding: 8px 10px; border-radius: 4px; background: {T['card']}; }}
code {{ font: 8.4pt {T['mono']}; background: {T['card']}; border: 1px solid {T['line']}; padding: 1px 4px; border-radius: 3px; color: {T['strong']}; }}
.cmd {{ display: block; margin: 4px 0 6px; padding: 6px 8px; white-space: pre-wrap; word-break: break-all; }}
.sw {{ display: inline-block; width: 10px; height: 10px; vertical-align: -1px; margin-right: 5px; border-radius: 2px; }}
.foot {{ color: {T['muted']}; font-size: 7.8pt; margin-top: 8px; }}
"""
    pages = []
    for k, shot in enumerate(shots):
        key = "".join(f'<li><span class="n">{i["n"]}</span><b>{i["title"]}</b> — {i["text"]}</li>' for i in shot["items"])
        pages.append(f"""<section class="page">
  <h1>{sys_['title']}<small>{sys_['subtitle']} · {k + 1}/{len(shots) + 1}</small></h1>
  <p class="lede">{shot['lede']}</p>
  <div class="shot"><img src="{shot['img']}" alt="{html.escape(shot['lede'])}">{marker_html(shot)}</div>
  <ol class="key">{key}</ol>
</section>""")
    pages.append(f"""<section class="page ref">
  <h1>{sys_['title']}<small>{sys_['subtitle']} · {len(shots) + 1}/{len(shots) + 1}</small></h1>
  {sys_['reference']}
  <p class="foot">Screens captured from the live system on {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. Regenerate: <code>docs/legend/legend_build.py</code>.</p>
</section>""")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{sys_['title']} legend</title>
<link href="{T['fonts']}" rel="stylesheet"><style>{css}</style></head><body>{''.join(pages)}</body></html>"""


def build(sys_, pw):
    outdir = pathlib.Path(sys_["outdir"]); outdir.mkdir(parents=True, exist_ok=True)
    b = pw.chromium.launch(executable_path=CHROME, headless=True, args=ARGS)
    ctx = b.new_context(viewport=sys_["viewport"], device_scale_factor=sys_.get("scale", 2),
                        reduced_motion=sys_.get("motion", "no-preference"), **sys_.get("ctx", {}))
    ctx.set_default_timeout(150000)
    page = ctx.new_page()
    page.goto(sys_["url"], wait_until="networkidle")
    time.sleep(sys_.get("warm", 8))
    shots = capture(page, sys_, outdir)
    ctx.close()
    doc = outdir / f"{sys_['slug']}_LEGEND.html"
    doc.write_text(page_html(sys_, shots), encoding="utf-8")
    p2 = b.new_page()
    p2.goto(doc.as_uri(), wait_until="networkidle")
    time.sleep(1.5)
    p2.pdf(path=str(outdir / f"{sys_['slug']}_LEGEND.pdf"), format="Letter", print_background=True,
           margin={"top": "0", "bottom": "0", "left": "0", "right": "0"}, prefer_css_page_size=True)
    b.close()
    print("built", sys_["slug"], sorted(x.name for x in outdir.iterdir()))


if __name__ == "__main__":
    env = pathlib.Path("/root/govaward-alpha/.env").read_text()
    pw_ = next((l.split("=", 1)[1].strip() for l in env.splitlines() if l.startswith("GOVAWARD_DASH_PASSWORD=")), None)
    GOV["ctx"] = {"http_credentials": {"username": "legend", "password": pw_}}
    import sys
    only = set(sys.argv[1:])            # e.g. "WHALE_OS" to rebuild one sheet
    with sync_playwright() as pw:
        for s in (GOV, WHALE):
            if not only or s["slug"] in only:
                build(s, pw)
