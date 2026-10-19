"""Generate the hand-styled SVG diagrams for SOLUTION.md."""
import pathlib

OUT = pathlib.Path(__file__).resolve().parent

STYLE = """
  <style>
    .lane  { fill: #f9fafb; stroke: #e5e7eb; stroke-width: 1.5; rx: 14; }
    .lt    { fill: #111827; font-size: 17px; font-weight: 700; }
    .gt    { fill: #6b7280; font-size: 12px; font-weight: 700; letter-spacing: 1px; }
    .node  { fill: #ffffff; stroke: #374151; stroke-width: 2; rx: 10; }
    .plug  { fill: #ffffff; stroke: #6366f1; stroke-width: 2; stroke-dasharray: 6 4; rx: 10; }
    .blue  { fill: #dbeafe; stroke: #2563eb; stroke-width: 2; rx: 10; }
    .green { fill: #dcfce7; stroke: #15803d; stroke-width: 2; rx: 10; }
    .red   { fill: #fee2e2; stroke: #b91c1c; stroke-width: 2; rx: 10; }
    .grey  { fill: #f3f4f6; stroke: #6b7280; stroke-width: 2; rx: 10; }
    .store { fill: #fff7ed; stroke: #c2410c; stroke-width: 2; }
    .t     { fill: #111827; font-size: 16px; text-anchor: middle; }
    .sub   { fill: #4b5563; font-size: 12.5px; text-anchor: middle; }
    .e     { fill: none; stroke: #374151; stroke-width: 2; marker-end: url(#a); }
    .eg    { fill: none; stroke: #15803d; stroke-width: 2; marker-end: url(#ag); }
    .er    { fill: none; stroke: #b91c1c; stroke-width: 2; marker-end: url(#ar); }
    .ed    { fill: none; stroke: #2563eb; stroke-width: 2; stroke-dasharray: 7 5; marker-end: url(#ab); }
    .l     { fill: #374151; font-size: 13px; text-anchor: middle; }
    .lg    { fill: #15803d; font-size: 13px; text-anchor: middle; }
    .lr    { fill: #b91c1c; font-size: 13px; text-anchor: middle; }
    .lb    { fill: #f9fafb; }
    .legend{ fill: #6b7280; font-size: 12.5px; }
    .life  { stroke: #9ca3af; stroke-width: 1.5; stroke-dasharray: 4 4; }
    .act   { fill: #e5e7eb; stroke: #9ca3af; stroke-width: 1; }
    .msg   { fill: none; stroke: #374151; stroke-width: 1.8; marker-end: url(#a); }
    .msgr  { fill: none; stroke: #374151; stroke-width: 1.8; stroke-dasharray: 5 4; marker-end: url(#a); }
    .msgb  { fill: none; stroke: #2563eb; stroke-width: 1.8; marker-end: url(#ab); }
    .ml    { fill: #111827; font-size: 12.5px; text-anchor: middle; }
    .mlb   { fill: #ffffff; }
  </style>
"""

DEFS = """
  <defs>
    <marker id="a" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L12,6 L0,12 z" fill="#374151"/></marker>
    <marker id="ab" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L12,6 L0,12 z" fill="#2563eb"/></marker>
    <marker id="ag" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L12,6 L0,12 z" fill="#15803d"/></marker>
    <marker id="ar" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L12,6 L0,12 z" fill="#b91c1c"/></marker>
  </defs>
"""


def svg(w, h, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'font-family="Helvetica, Arial, sans-serif">{DEFS}{STYLE}\n{body}\n</svg>\n')


def box(x, y, w, h, title, sub=None, cls="node"):
    cx = x + w / 2
    if sub:
        return (f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}"/>'
                f'<text class="t" x="{cx}" y="{y + h / 2 - 3}">{title}</text>'
                f'<text class="sub" x="{cx}" y="{y + h / 2 + 17}">{sub}</text>\n')
    return (f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}"/>'
            f'<text class="t" x="{cx}" y="{y + h / 2 + 6}">{title}</text>\n')


def card(x, y, w, h, title, sub=None):
    s = (f'<rect class="store" x="{x}" y="{y}" width="{w}" height="{h}" rx="10"/>'
         f'<rect x="{x}" y="{y}" width="{w}" height="8" rx="4" fill="#c2410c"/>')
    cx = x + w / 2
    if sub:
        s += (f'<text class="t" x="{cx}" y="{y + h / 2 + 1}">{title}</text>'
              f'<text class="sub" x="{cx}" y="{y + h / 2 + 20}">{sub}</text>\n')
    else:
        s += f'<text class="t" x="{cx}" y="{y + h / 2 + 9}">{title}</text>\n'
    return s


def arrow(d, cls="e"):
    return f'<path class="{cls}" d="{d}"/>\n'


def label(x, y, text, cls="l", w=None):
    s = ""
    if w:
        s += f'<rect class="lb" x="{x - w / 2}" y="{y - 13}" width="{w}" height="18"/>'
    return s + f'<text class="{cls}" x="{x}" y="{y}">{text}</text>\n'


# ---------------------------------------------------------------- write path
def write_path():
    b = ""
    b += f'<rect class="lane" x="20" y="20" width="1240" height="400"/>'
    b += '<text class="lt" x="44" y="50">Ingest: how a captured clip enters the cache</text>'
    y = 100
    b += box(44, y, 170, 64, "Captured clip", "text and PCM chunks", "blue")
    b += box(254, y, 170, 64, "Assemble", "concatenate, resample")
    b += box(464, y, 170, 64, "Sanity checks", "duration, silence, peak")
    b += box(674, y, 170, 64, "Verifier", "no-op or ASR", "plug")
    b += box(884, y, 170, 64, "Policy", "should_store, ttl_for", "plug")
    b += card(1094, y, 150, 64, "L2 put", "encoded as Opus")
    for x in (214, 424, 634, 844, 1054):
        b += arrow(f"M{x},{y + 32} L{x + 38},{y + 32}")
    b += label(549 + 100, y + 24, "pass", "lg", 40)
    b += label(759 + 100, y + 24, "pass", "lg", 40)
    b += label(969 + 100, y + 24, "yes", "lg", 36)
    # rejects
    ry = 260
    b += box(464, ry, 590, 56, "Rejected, reason counted", "nothing truncated, silent or unverified is ever stored", "red")
    for x, t in ((549, "fail"), (759, "below threshold"), (969, "no")):
        b += arrow(f"M{x},{y + 64} L{x},{ry - 2}", "er")
        b += label(x + 48, y + 100, t, "lr", 110 if len(t) > 5 else 40)
    # L1 fill
    b += card(1094, ry, 150, 56, "L1 fill", "if admitted")
    b += arrow(f"M1169,{y + 64} L1169,{ry - 2}")
    b += label(1210, y + 100, "decode", "l", 56)
    b += '<text class="legend" x="44" y="396">capture() enqueues and returns at once. Everything on this page runs in a bounded background task and never blocks audio.</text>'
    return svg(1280, 440, b)


# ---------------------------------------------------------------- lookup order
def lookup():
    b = ""
    b += '<rect class="lane" x="20" y="20" width="1240" height="560"/>'
    b += '<text class="lt" x="44" y="50">Lookup order for one sentence</text>'
    # serve bar
    b += box(320, 86, 640, 48, "Serve stored audio in the sentence's position", None, "green")
    b += box(1000, 86, 240, 48, "Serve and rewrite transcript", None, "green")
    # gates row
    gy = 250
    b += box(44, gy, 150, 64, "Sentence", "canonicalise, hash", "blue")
    gates = [
        (234, "L1 exact", "process memory"),
        (444, "L2 exact", "deadline 20 ms"),
        (654, "Level B", "near-duplicate, if on"),
        (864, "Level C", "semantic, slot + span veto"),
    ]
    for x, t, s in gates:
        b += box(x, gy, 170, 64, t, s)
    for x in (194, 404, 614, 824):
        b += arrow(f"M{x},{gy + 32} L{x + 38},{gy + 32}")
    for x in (404, 614, 824):
        b += label(x + 19, gy + 22, "miss", "l", 40)
    b += box(1074, gy, 170, 64, "Miss", "synthesise, then capture", "red")
    b += arrow(f"M1034,{gy + 32} L1072,{gy + 32}")
    b += label(1053, gy + 22, "miss", "l", 40)
    # hit arrows up to serve bar
    for x, t in ((319, "hit"), (529, "hit"), (739, "hit, class allowed by policy")):
        b += arrow(f"M{x},{gy} L{x},136", "eg")
        b += label(x, 190, t, "lg", 190 if len(t) > 5 else 36)
    b += arrow(f"M949,{gy} L949,136", "eg")
    b += label(949, 190, "hit: above threshold, both vetoes pass", "lg", 220)
    # L2 hit fills L1
    b += arrow(f"M529,{gy + 64} L529,{gy + 120} L319,{gy + 120} L319,{gy + 66}")
    b += label(424, gy + 112, "L2 hit fills L1", "l", 110)
    # notes
    ny = 440
    b += box(234, ny, 170, 56, "under 0.1 ms", "hash lookup", "grey")
    b += box(444, ny, 170, 56, "1 to 4 ms", "hard deadline, then miss", "grey")
    b += box(654, ny, 170, 56, "under 1 ms", "shingles, LSH, edit distance", "grey")
    b += box(864, ny, 170, 56, "about 3 ms", "e5-small via onnxruntime, HNSW", "grey")
    b += '<text class="legend" x="44" y="556">Levels B and C are on by default; C serves only in languages that passed the acceptance check. Only the first sentence of a turn is on the critical path; later lookups overlap the LLM stream.</text>'
    return svg(1280, 600, b)


# ---------------------------------------------------------------- invalidation
def invalidation():
    b = ""
    cols = [
        (20, "Automatic", "nothing to run", [
            ("Voice, model, settings or sample rate change", "Scope hash changes; old entries become unreachable and age out"),
            ("Canonicalisation rule change", "schema_version bump on deploy; whole cache invalidated"),
        ]),
        (440, "Operator command", "one CLI call", [
            ("Bust one voice or model now", "bump-generation: INCR the scope counter; readers refresh every 60 s"),
            ("A sentence must never be replayed", "purge by tag, key or text match"),
        ]),
        (860, "Scheduled job", "runs on a timer", [
            ("Provider drift under a floating model alias", "daily canary re-synthesises reference sentences, compares, bumps generation and alerts"),
            ("Capture or verifier change", "ingest_version per entry; re-verify on next hit or re-ingest in background"),
        ]),
    ]
    for x, title, sub, items in cols:
        b += f'<rect class="lane" x="{x}" y="20" width="400" height="400"/>'
        b += f'<text class="lt" x="{x + 20}" y="50">{title}</text>'
        b += f'<text class="gt" x="{x + 20}" y="72">{sub.upper()}</text>'
        y = 96
        for trig, mech in items:
            b += box(x + 20, y, 360, 52, trig, None, "blue")
            b += arrow(f"M{x + 200},{y + 52} L{x + 200},{y + 72}")
            # mechanism box, two lines
            words = mech.split()
            half = len(words) // 2
            l1, l2 = " ".join(words[:half]), " ".join(words[half:])
            b += (f'<rect class="node" x="{x + 20}" y="{y + 74}" width="360" height="64"/>'
                  f'<text class="sub" x="{x + 200}" y="{y + 74 + 27}">{l1}</text>'
                  f'<text class="sub" x="{x + 200}" y="{y + 74 + 46}">{l2}</text>\n')
            y += 160
    return svg(1280, 440, b)


# ---------------------------------------------------------------- mixed turn
def mixed_turn():
    b = ""
    names = ["LLM", "TTSCacheRead", "TTSCache core", "TTSService (stock)", "TTSCacheWrite", "transport"]
    xs = [100, 330, 550, 770, 990, 1190]
    top, bottom = 40, 920
    for x, n in zip(xs, names):
        b += box(x - 90, top, 180, 44, n, None, "blue" if "Cache" in n else "node")
        b += f'<line class="life" x1="{x}" y1="{top + 44}" x2="{x}" y2="{bottom}"/>'
    y = [120]

    def msg(a, c, text, cls="msg"):
        xa, xc = xs[a], xs[c]
        d = 6 if xc > xa else -6
        b2 = arrow(f"M{xa},{y[0]} L{xc - d},{y[0]}", cls)
        mid = (xa + xc) / 2
        w = len(text) * 7 + 12
        b2 += f'<rect class="mlb" x="{mid - w / 2}" y="{y[0] - 20}" width="{w}" height="16"/>'
        b2 += f'<text class="ml" x="{mid}" y="{y[0] - 8}">{text}</text>\n'
        y[0] += 40
        return b2

    def note(text, cls="gt"):
        b2 = f'<text class="{cls}" x="44" y="{y[0] - 8}">{text}</text>\n'
        y[0] += 20
        return b2

    b += msg(0, 1, 'tokens: "Hi, who\'s there? Let me check your order. Thanks."')
    b += note("SENTENCE 1")
    b += msg(1, 2, 'lookup("Hi, who\'s there?")')
    b += msg(2, 1, "hit, from L1", "msgr")
    b += msg(1, 3, "TTSStarted, TTSAudioRaw chunks, TTSText, TTSStopped (ctx 1)", "msgb")
    b += msg(3, 4, "same frames, passed through in order")
    b += msg(4, 5, "forwarded")
    b += note("SENTENCE 2")
    b += msg(1, 2, 'lookup("Let me check your order.")')
    b += msg(2, 1, "miss", "msgr")
    b += msg(1, 3, "AggregatedTextFrame (ctx 2)")
    b += f'<rect class="act" x="{xs[3] - 6}" y="{y[0] - 30}" width="12" height="56"/>'
    b += f'<text class="ml" x="{xs[3] + 90}" y="{y[0] - 6}">run_tts: provider call</text>\n'
    y[0] += 40
    b += note("SENTENCE 3, LOOKED UP WHILE SENTENCE 2 SYNTHESISES")
    b += msg(1, 2, 'lookup("Thanks.")')
    b += msg(2, 1, "hit, from L1", "msgr")
    b += msg(1, 3, "cached frames (ctx 3), queued behind ctx 2", "msgb")
    b += note("AUDIO LEAVES IN SENTENCE ORDER")
    b += msg(3, 4, "TTSStarted, TTSAudioRaw..., TTSStopped (ctx 2)")
    b += msg(4, 5, "forwarded unchanged")
    b += msg(4, 2, 'capture("Let me check your order.", pcm)', "msgb")
    b += msg(3, 4, "cached frames (ctx 3)")
    b += msg(4, 5, "forwarded")
    b += '<text class="legend" x="44" y="956">Blue arrows are cache actions. The stock TTSService serialises everything it receives, so a hit queued after a miss waits for the miss audio and a hit before a miss plays at once.</text>'
    return svg(1280, 980, b)


for name, fn in (("write-path", write_path), ("lookup", lookup), ("invalidation", invalidation), ("mixed-turn", mixed_turn)):
    (OUT / f"{name}.svg").write_text(fn(), encoding="utf-8")
    print("wrote", name)
