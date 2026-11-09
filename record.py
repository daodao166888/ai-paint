"""
Record the painter as a crisp MP4, frame by frame (no dropped frames, 2x pixel density).

  pip install playwright && playwright install chromium
  python record.py photo.jpg --dur 30 --out painting.mp4
  python record.py photo.jpg --strokes strokes.json --dur 30   # replay AI strokes

Outputs:
  painting.mp4        X-ready (fits X's 1920x1200 / 1200x1900 limits, high bitrate)
  painting_master.mp4 full-resolution master (only with --master)
Needs ffmpeg on PATH.
"""
import argparse, functools, http.server, shutil, subprocess, tempfile, threading
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
VIEW = {"width": 940, "height": 860}
FPS = 30


def serve(root: Path) -> int:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--dur", type=float, default=30, help="painting length in seconds")
    ap.add_argument("--out", default="painting.mp4")
    ap.add_argument("--dpr", type=float, default=2, help="device pixel ratio (2 = retina-sharp)")
    ap.add_argument("--hold", type=float, default=2.5, help="seconds to hold the finished painting")
    ap.add_argument("--strokes", help="strokes.json from ai_painter.py")
    ap.add_argument("--master", action="store_true", help="also save full-resolution master")
    a = ap.parse_args()

    work = Path(tempfile.mkdtemp())
    shutil.copy(HERE / "paint.html", work / "paint.html")
    ext = Path(a.image).suffix or ".jpg"
    shutil.copy(a.image, work / f"ref{ext}")
    extra = ""
    if a.strokes:
        shutil.copy(a.strokes, work / "strokes.json")
        extra = "&strokes=strokes.json"
    port = serve(work)
    frames = work / "frames"; frames.mkdir()

    n = 0
    def shot(page, copies=1):
        nonlocal n
        p = frames / f"{n:05d}.jpg"
        page.screenshot(path=str(p), type="jpeg", quality=95)
        n += 1
        for _ in range(copies - 1):
            shutil.copy(p, frames / f"{n:05d}.jpg"); n += 1

    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport=VIEW, device_scale_factor=a.dpr)
        page.goto(f"http://127.0.0.1:{port}/paint.html?ref=ref{ext}&autostart=1&rec=1"
                  f"&manual=1&delay=50&dur={a.dur}{extra}")
        page.wait_for_function("window.__ready === true")
        page.wait_for_timeout(300)
        shot(page, int(0.6 * FPS))                 # blank canvas intro
        while not page.evaluate("window.__done === true"):
            page.evaluate("window.__tick()")
            shot(page)
            if n % 150 == 0:
                print(f"  {n / FPS:.0f}s of video captured")
        shot(page, int(a.hold * FPS))              # hold the result
        b.close()

    enc = ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", "-r", str(FPS)]
    src = ["-y", "-v", "error", "-framerate", str(FPS), "-i", str(frames / "%05d.jpg")]
    # X caps at 1920x1200 landscape / 1200x1900 portrait; this page is ~square, so fit height 1200
    subprocess.run(["ffmpeg", *src, "-vf", "scale=-2:1200:flags=lanczos", *enc, a.out], check=True)
    if a.master:
        m = str(Path(a.out).with_name(Path(a.out).stem + "_master.mp4"))
        subprocess.run(["ffmpeg", *src, "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", *enc, m], check=True)
    print(f"saved {a.out}  ({n / FPS:.1f}s, {n} frames)")


if __name__ == "__main__":
    main()
