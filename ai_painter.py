"""
"Real AI painting" mode: a vision model looks at the reference + its own canvas
and decides every stroke through a tool call. Strokes are applied in a headless
browser and saved to strokes.json, which record.py then replays at a fixed speed
(so the slow model thinking time doesn't end up in the video).

  pip install anthropic playwright && playwright install chromium
  export ANTHROPIC_API_KEY=...
  python ai_painter.py my_photo.jpg --rounds 4 --out strokes.json
  python record.py my_photo.jpg --strokes strokes.json --dur 30 --out ai_painting.mp4

Any vision + tool-calling model works; swap call_model() for OpenAI/Gemini if you like.
"""
import argparse, base64, functools, http.server, json, os, shutil, tempfile, threading
from pathlib import Path
import anthropic
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
MODEL = os.environ.get("PAINT_MODEL", "claude-opus-5-5")

# (pass name, allowed brush sizes, strokes per round)
PASSES = [
    ("Block-in",        [126, 69], 60),
    ("Form and color",  [38, 21], 120),
    ("Refinement",      [12, 7],  200),
    ("Fine details",    [4, 3],   250),
]

TOOL = {
    "name": "paint_strokes",
    "description": "Paint a batch of brush strokes on the 825x875 canvas. (0,0) is top-left.",
    "input_schema": {
        "type": "object",
        "properties": {
            "strokes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "size":   {"type": "integer", "description": "brush diameter in px"},
                        "color":  {"type": "string",  "description": "hex like #a86b52"},
                        "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                                   "description": "polyline [[x,y],...]; 1 point = a dab"},
                    },
                    "required": ["size", "color", "points"],
                },
            }
        },
        "required": ["strokes"],
    },
}

SYSTEM = (
    "You are a portrait painter working with a real brush on a digital canvas, 825x875 px. "
    "Image 1 is the reference photo (already cropped to the canvas). Image 2 is your canvas right now. "
    "Paint like a classical painter: big shapes and values first, then form, then edges and details. "
    "Pick colours by looking at the reference. Follow the form with stroke direction. "
    "Only use the brush sizes you are allowed in this pass. Respond only by calling paint_strokes."
)


def b64img(data_url: str):
    head, data = data_url.split(",", 1)
    mt = head.split(";")[0].split(":")[1]
    return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}}


def call_model(client, ref_url, canvas_url, pass_name, sizes, n):
    msg = client.messages.create(
        model=MODEL, max_tokens=16000, system=SYSTEM, tools=[TOOL],
        tool_choice={"type": "tool", "name": "paint_strokes"},
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "Reference:"}, b64img(ref_url),
            {"type": "text", "text": "Current canvas:"}, b64img(canvas_url),
            {"type": "text", "text": f"Pass: {pass_name}. Allowed brush sizes: {sizes}. "
                                     f"Paint about {n} strokes where the canvas differs most from the reference."},
        ]}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input.get("strokes", [])
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--rounds", type=int, default=4, help="model calls per pass")
    ap.add_argument("--out", default="strokes.json")
    a = ap.parse_args()

    work = Path(tempfile.mkdtemp())
    shutil.copy(HERE / "paint.html", work / "paint.html")
    ext = Path(a.image).suffix or ".jpg"
    shutil.copy(a.image, work / f"ref{ext}")
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *x): pass
    h = functools.partial(Quiet, directory=str(work))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    client = anthropic.Anthropic()
    log = []
    with sync_playwright() as p:
        page = p.chromium.launch().new_page(viewport={"width": 940, "height": 860})
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/paint.html?ref=ref{ext}")
        page.wait_for_function("window.__ready === true")
        ref_url = page.evaluate("paint.reference()")
        for pass_name, sizes, n in PASSES:
            page.evaluate("n => paint.setPass(n)", pass_name)
            for r in range(a.rounds):
                canvas_url = page.evaluate("paint.snapshot(true)")
                strokes = call_model(client, ref_url, canvas_url, pass_name, sizes, n)
                for s in strokes:
                    s["size"] = min(sizes, key=lambda v: abs(v - s.get("size", sizes[0])))
                    s["pass"] = pass_name
                page.evaluate("""list => { for (const s of list) {
                    paint.setBrush(s.size); paint.setColor(s.color); paint.stroke(s.points); } }""", strokes)
                log += strokes
                print(f"{pass_name} round {r+1}/{a.rounds}: {len(strokes)} strokes (total {len(log)})")
                json.dump(log, open(a.out, "w"))      # save as we go
        data = page.evaluate("paint.snapshot()").split(",", 1)[1]
        Path(a.out).with_suffix(".png").write_bytes(base64.b64decode(data))
    print("saved", a.out)


if __name__ == "__main__":
    main()
