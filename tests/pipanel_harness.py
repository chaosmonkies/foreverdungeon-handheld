"""Headless PiPanel harness (mirrors orangepi/egames/harness.py).

python pipanel_harness.py OUT_DIR "key1,key2,_,_,..." [delay] [base_url]
Keys: names (up, down, enter, esc, back), single chars, 'space', '_' = idle tick.
After the list it sends esc. Every disp.show() frame is saved as PNG.
"""
import os
import sys
import time

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))


class Disp:
    w, h = 250, 122

    def __init__(self, out_dir, name="fdungeon"):
        self.out_dir, self.name, self.n = out_dir, name, 0
        self.fulls = []
        os.makedirs(out_dir, exist_ok=True)

    def new(self, bg=255):
        return Image.new("1", (250, 122), bg)

    def show(self, img, full=False):
        assert img.mode == "1" and img.size == (250, 122)
        self.n += 1
        self.fulls.append(full)
        img.save(os.path.join(self.out_dir, "%s-%02d%s.png" % (self.name, self.n, "-full" if full else "")))


class Btn:
    mode = "text"

    def __init__(self, ks, delay=0.1, hook=None):
        self.q, self.delay, self.hook, self.i = list(ks), delay, hook, 0

    def flush(self):
        pass

    def poll(self, timeout=0):
        return []

    def text(self, timeout=0.1):
        time.sleep(self.delay)
        self.i += 1
        if self.hook:
            self.hook(self.i)
        if not self.q:
            return [("esc", "")]
        k = self.q.pop(0)
        if k == "_":
            return []
        if k == "space":
            return [("char", " ")]
        if len(k) == 1:
            return [("char", k)]
        return [(k, "")]


def main():
    from foreverdungeon_handheld.displays import pipanel
    out, seq = sys.argv[1], (sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else [])
    delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    base_url = sys.argv[4] if len(sys.argv) > 4 else None
    d = Disp(out)
    t0 = time.time()
    app = pipanel.run(d, Btn(seq, delay), base_url=base_url)
    print("frames=%d state=%s %.1fs" % (d.n, app.state.value, time.time() - t0))


if __name__ == "__main__":
    main()
