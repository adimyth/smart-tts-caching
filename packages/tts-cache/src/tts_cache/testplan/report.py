"""TEST_REPORT.md and its charts, generated from ``analysis.json`` alone (TEST_PLAN.md section 14).

Every number carries its kind: **live** (measured against a provider),
**measured** (offline, on this machine), **simulated** (offline against the
simulated provider's billing and timing), **composed** (11.3) or **derived**
(13.2). Tables carry their spread: a 95 percent interval over seeds, or
percentiles with n.

``python -m tts_cache.testplan.report <run dir> [--md PATH]`` writes the
report (default ``<run dir>/TEST_REPORT.md``) and its SVG charts into a
``test-report/`` directory beside it.
"""

import argparse
import json
import math
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from tts_cache.testplan.cells import (
    CORE,
    PRICE_DATE,
    PRICE_NOTES,
    PROVIDER,
    REDIS_USD_PER_GB_MONTH,
    REST,
    SPREAD,
    USD_PER_MCHARS,
)

CONFIGS = ("OFF", "A", "A+B", "A+B+C")
ON = ("A", "A+B", "A+B+C")
LABELS = (
    "new",
    "exact",
    "surface",
    "punct_inner",
    "punct_end",
    "filler",
    "paraphrase",
    "slot_change",
    "meaning_change",
    "punct_question",
)
RATES = (0.25, 0.5)
MS_PER_CHAR = 66.0  # the simulated provider's speech rate, for audio seconds saved offline


# ---------------------------------------------------------------------------
# Formatting.
# ---------------------------------------------------------------------------


def num(v: Any, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.{digits}f}"
    return str(v)


def share(v: Any, digits: int = 1) -> str:
    return "–" if v is None else f"{v * 100:.{digits}f}%"


def ci(b: dict[str, Any] | None, *, points: bool = True) -> str:
    """``mean [lo, hi]`` of a bootstrap record, as percentages."""
    if not b or b.get("mean") is None:
        return "–"
    f = 100.0 if points else 1.0
    if b.get("lo") is None or b.get("n", 0) < 2:
        return f"{b['mean'] * f:.1f}"
    return f"{b['mean'] * f:.1f} [{b['lo'] * f:.1f}, {b['hi'] * f:.1f}]"


def pcts(d: dict[str, Any] | None, keys: Sequence[str] = ("p50", "p95", "max")) -> str:
    if not d or not d.get("n"):
        return "–"
    parts = [f"{num(d.get(k))}" for k in keys]
    return " / ".join(parts) + f" (n={d['n']})"


def table(head: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    rows = [[str(c) for c in r] for r in rows]
    if not rows:
        return "_No data._\n"
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# SVG charts.
# ---------------------------------------------------------------------------

PALETTE = ("#4c72b0", "#dd8452", "#55a868", "#c44e52", "#8172b3", "#937860", "#da8bc3", "#8c8c8c")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def line_chart(
    series: dict[str, list[tuple[float, float]]],
    *,
    title: str,
    xlabel: str,
    ylabel: str,
    xlog: bool = False,
    w: int = 640,
    h: int = 360,
) -> str:
    pts = [p for s in series.values() for p in s]
    if not pts:
        return ""
    ml, mr, mt, mb = 60, 160, 30, 45
    xs = [math.log10(x) if xlog else x for x, _ in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = 0.0, max(y for _, y in pts) or 1.0
    x1 = x1 if x1 > x0 else x0 + 1
    y1 *= 1.05

    def px(x: float) -> float:
        x = math.log10(x) if xlog else x
        return ml + (x - x0) / (x1 - x0) * (w - ml - mr)

    def py(y: float) -> float:
        return h - mb - (y - y0) / (y1 - y0) * (h - mt - mb)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        'font-family="sans-serif" font-size="11">',
        f'<text x="{w / 2}" y="18" text-anchor="middle" font-size="13">{_esc(title)}</text>',
        f'<line x1="{ml}" y1="{h - mb}" x2="{w - mr}" y2="{h - mb}" stroke="#333"/>',
        f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{h - mb}" stroke="#333"/>',
        f'<text x="{(ml + w - mr) / 2}" y="{h - 8}" text-anchor="middle">{_esc(xlabel)}</text>',
        f'<text x="14" y="{(mt + h - mb) / 2}" text-anchor="middle" '
        f'transform="rotate(-90 14 {(mt + h - mb) / 2})">{_esc(ylabel)}</text>',
    ]
    for i in range(5):
        yv = y0 + (y1 - y0) * i / 4
        out.append(f'<text x="{ml - 6}" y="{py(yv) + 4}" text-anchor="end">{yv:.3g}</text>')
        out.append(f'<line x1="{ml}" y1="{py(yv)}" x2="{w - mr}" y2="{py(yv)}" stroke="#eee"/>')
    ticks = sorted({x for x, _ in pts})
    if len(ticks) > 8:
        step = max(1, len(ticks) // 6)
        ticks = ticks[::step]
    for xv in ticks:
        out.append(f'<text x="{px(xv)}" y="{h - mb + 15}" text-anchor="middle">{xv:.3g}</text>')
    for k, (name, s) in enumerate(series.items()):
        color = PALETTE[k % len(PALETTE)]
        s = sorted(s)
        path = " ".join(f"{'M' if i == 0 else 'L'}{px(x):.1f},{py(y):.1f}" for i, (x, y) in enumerate(s))
        out.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6"/>')
        if len(s) <= 12:
            out += [f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="2.5" fill="{color}"/>' for x, y in s]
        ly = mt + 14 * k
        out.append(f'<rect x="{w - mr + 10}" y="{ly}" width="10" height="3" fill="{color}"/>')
        out.append(f'<text x="{w - mr + 24}" y="{ly + 5}">{_esc(name)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def cdf(values: list[float], points: int = 60) -> list[tuple[float, float]]:
    s = sorted(values)
    if not s:
        return []
    n = len(s)
    idx = sorted({min(n - 1, int(i * (n - 1) / max(1, points - 1))) for i in range(points)})
    return [(s[i], (i + 1) / n) for i in idx]


# ---------------------------------------------------------------------------
# Sections.
# ---------------------------------------------------------------------------


class Report:
    def __init__(self, a: dict[str, Any], charts_dir: Path, charts_rel: str) -> None:
        self.a = a
        self.charts_dir = charts_dir
        self.charts_rel = charts_rel
        self.off = a.get("offline") or {}
        self.cells = self.off.get("cells") or {}
        self.sz = a.get("sizes") or {}
        self.lv = a.get("live") or {}
        self.us = a.get("usability") or {}
        self.vc = a.get("vchecks") or {}
        self.ver = a.get("verdicts") or {}
        self.langs: list[str] = a.get("langs") or []
        self.charts: list[str] = []

    def cell(
        self, lang: str, config: str, r: float, mix: str = "labelled", profile: str = "serverless"
    ) -> dict[str, Any]:
        return self.cells.get(f"{lang}|{config}|{r}|{mix}|{profile}") or {}

    def chart(self, name: str, svg: str, caption: str) -> str:
        if not svg:
            return f"_Chart {name}: no data._\n"
        self.charts_dir.mkdir(parents=True, exist_ok=True)
        (self.charts_dir / f"{name}.svg").write_text(svg, encoding="utf-8")
        self.charts.append(name)
        return f"![{caption}]({self.charts_rel}/{name}.svg)\n\n_{caption}_\n"

    def live_l1(self, lang: str, service: str, config: str, r: float) -> dict[str, Any] | None:
        return (self.lv.get("l1") or {}).get(f"{lang}|{service}|{config}|{r}")

    def turn_start(self, lang: str, service: str | None, config: str) -> str:
        """Turn-start L1 p50 with the cache off, then with ``config``, at r = 0.25 (live)."""
        if service is None:
            return "–"
        off = self.live_l1(lang, service, "OFF", 0.25)
        on = self.live_l1(lang, service, config, 0.25)
        if not off or not on:
            return f"{service}: –"
        return f"{service}: {num(off.get('p50'), 0)} → {num(on.get('p50'), 0)} ms"

    # T0.

    def t0(self) -> str:
        out = ["## T0. Summary\n"]
        crit_off = (self.vc.get("V2") or {}).get("offline_critical") or []
        crit_live = (self.vc.get("V2") or {}).get("live_critical") or []
        pop_crit = {
            k: v.get("critical", 0) for k, v in (self.sz.get("population") or {}).items() if v.get("critical")
        }
        out.append("### Critical findings\n")
        if not crit_off and not crit_live and not pop_crit:
            out.append("None: no `new`, `slot_change` or `meaning_change` sentence was served anywhere.\n")
        else:
            rows = [
                (
                    c.get("lang"),
                    c.get("config"),
                    c["run"],
                    c["label"],
                    c.get("level"),
                    _short(c.get("spoken_text")),
                    "measured",
                )
                for c in crit_off[:40]
            ]
            rows += [
                (
                    c["run"].split("_")[0],
                    "",
                    c["run"],
                    c["label"],
                    c.get("level"),
                    _short(c.get("spoken_text")),
                    "live",
                )
                for c in crit_live[:20]
            ]
            out.append(
                table(("language", "config", "run", "label", "level", "served audio of", "kind"), rows)
            )
            if len(crit_off) > 40:
                out.append(
                    f"\n{len(crit_off) - 40} more offline critical serves are listed in "
                    "`analysis.json` (V2).\n"
                )
            if pop_crit:
                out.append(
                    "\nPopulation sweep (9.3), critical serves per language and N (measured): "
                    + ", ".join(f"{k} {v}" for k, v in sorted(pop_crit.items()))
                    + ".\n"
                )
        out.append("\n### Verdict per language (13.1)\n")
        rows = []
        for lang in self.langs:
            v = self.ver.get(lang) or {}
            ws = next((s for s in CORE.get(lang, ()) if s not in REST), None)
            rest = next((s for s in CORE.get(lang, ()) if s in REST), None)

            rows.append(
                (
                    lang,
                    v.get("recommended", "–"),
                    num((v.get("A") or {}).get("safe")),
                    num((v.get("B") or {}).get("safe")),
                    num((v.get("C") or {}).get("safe")),
                    ci((v.get("B") or {}).get("delta_r025")),
                    ci((v.get("C") or {}).get("delta_r025")),
                    self.turn_start(lang, ws, v.get("recommended", "A")),
                    self.turn_start(lang, rest, v.get("recommended", "A")),
                    num(v.get("punct_question_served")),
                )
            )
        out.append(
            table(
                (
                    "language",
                    "recommended",
                    "A safe",
                    "B safe",
                    "C safe",
                    "Δ(B) points, r=0.25 (measured)",
                    "Δ(C) points, r=0.25 (measured)",
                    "turn start L1 p50, websocket (live)",
                    "turn start L1 p50, REST (live)",
                    "punct_question served",
                ),
                rows,
            )
        )
        out.append(
            "\nSafe means zero `new`, `slot_change` and `meaning_change` serves across the offline runs, the "
            "population sweep at its default size and the live runs. Δ is the char hit rate added "
            "by the level, "
            "mean over seeds with its 95 percent interval. `punct_question` serves are reported, "
            "not fixed (owner's "
            "decision): enabling Level B's punctuation class carries that risk.\n"
        )
        imp = (self.a.get("impact") or {}).get("money") or {}
        rows = []
        for lang in self.langs:
            rec = (self.ver.get(lang) or {}).get("recommended", "A")
            for prov in sorted(USD_PER_MCHARS):
                x = imp.get(f"{lang}|{rec}|0.25|{prov}|100000")
                if x:
                    rows.append(
                        (
                            lang,
                            rec,
                            prov,
                            num((x["usd_saved"] or {}).get("mean"), 0),
                            num(x["redis_usd"], 0),
                            num(x["net_usd"], 0),
                        )
                    )
        out.append("\n### Impact at 100,000 calls a month, r = 0.25 (derived)\n")
        out.append(
            table(("language", "config", "provider", "saved $/month", "Redis $/month", "net $/month"), rows)
        )
        out.append("\n### Usability in one line each\n")
        out += [f"- {line}" for line in self.usability_lines()]
        out.append("\n### Largest spreads and the worst cell\n")
        out += [f"- {line}" for line in self.spread_lines()]
        return "\n".join(out) + "\n"

    def usability_lines(self) -> list[str]:
        us = self.us
        lines = [
            "U1, integration from the docs alone: not run in this plan's build (a fresh agent or "
            "developer task)."
        ]
        u2 = us.get("u2") or {}
        shares = [
            (lang, (x["configs"].get("0.25") or {}).get("default_share_of_best"))
            for lang, x in u2.items()
            if x.get("configs")
        ]
        shares = [(lang, s) for lang, s in shares if s is not None]
        if shares:
            worst = min(shares, key=lambda t: t[1])
            lines.append(
                f"U2, defaults: the default A+B+C saves {share(min(s for _, s in shares))} to "
                f"{share(max(s for _, s in shares))} of the best configuration at r = 0.25 (worst "
                f"{worst[0]}; measured)."
            )
        u3 = us.get("u3") or {}
        if u3:
            m = u3.get("model") or {}
            lines.append(
                "U3, footprint: `import tts_cache` "
                f"{num((u3.get('import') or {}).get('tts_cache', {}).get('ms_p50'))} ms; "
                f"Level C model {num((m.get('download_bytes') or 0) / 2**20)} MB, loads in "
                f"{num(m.get('load_ms_p50'), 0)} ms (measured)."
            )
        u4 = us.get("u4") or {}
        if u4.get("by_config"):
            a = u4["by_config"].get("A") or {}
            lines.append(
                "U4, replay against live: Level A replay differs from the live hit rate by "
                f"{num(a.get('mean'))} points on average (live)."
            )
        u5 = us.get("u5") or {}
        if u5:
            errs = sum(len(v.get("host_errors", [])) for v in u5.values() if isinstance(v, dict))
            worst = max((v["added_ms"]["max"] or 0) for v in u5.values() if isinstance(v, dict))
            lines.append(
                f"U5, the store failing: {errs} errors reached the host across five faults; worst "
                f"added lookup {num(worst)} ms (measured)."
            )
        u6 = us.get("u6") or {}
        if u6:
            lines.append(
                "U6, observability: the metric log alone gives the oracle's hit rate, saved "
                f"characters and rejects: {num(u6.get('pass'))} (measured)."
            )
        lines.append(
            "U7, listening check: dropped by the owner's decision of 2026-10-18; the ASR check of "
            "7.2 stands in."
        )
        u8 = us.get("u8") or {}
        if u8:
            bad = [
                k
                for k, v in u8.items()
                if isinstance(v, dict) and v.get("wrong_count") and k != "wrong_voice"
            ]
            unnamed = [
                k for k, v in u8.items() if isinstance(v, dict) and k != "correct" and not v.get("named")
            ]
            lines.append(
                f"U8, misconfiguration: wrong serves in {', '.join(bad) or 'no scenario'} besides "
                "the wrong-voice scope; "
                f"nothing names {', '.join(unnamed) or 'no scenario'} (measured against the fake service)."
            )
        return lines

    def spread_lines(self) -> list[str]:
        lines = []
        rates = {
            f"{lang} {c}": (self.cell(lang, c, 0.25).get("char_hit_rate") or {}).get("mean")
            for lang in self.langs
            for c in ON
        }
        rates = {k: v for k, v in rates.items() if v is not None}
        if rates:
            lo = min(rates, key=lambda k: rates[k])
            hi = max(rates, key=lambda k: rates[k])
            lines.append(
                "Char hit rate at r = 0.25 across languages and configurations: "
                f"{share(rates[lo])} ({lo}) to {share(rates[hi])} ({hi}) (measured)."
            )
        ses = self.off.get("sessions") or {}
        z = {
            k: v.get("zero_hit_share")
            for k, v in ses.items()
            if k.endswith("|0.25") and v.get("zero_hit_share") is not None
        }
        if z:
            k = max(z, key=lambda k: z[k])
            lines.append(
                f"Largest share of zero-hit sessions at r = 0.25: {share(z[k])} "
                f"({k.replace('|', ' ')}) (measured)."
            )
        l1 = self.lv.get("l1") or {}
        offs = {k: v.get("p95") for k, v in l1.items() if "|OFF|" in k and v.get("p95") is not None}
        if offs:
            k = max(offs, key=lambda k: offs[k])
            lines.append(
                f"Slowest cell, turn-start L1 p95 with the cache off: {num(offs[k], 0)} ms "
                f"({k.replace('|', ' ')}) (live)."
            )
        rel = self.lv.get("reliability") or {}
        if rel:
            k = max(rel, key=lambda k: rel[k].get("retried", 0) / max(1, rel[k].get("sentences", 1)))
            lines.append(
                f"Least reliable cell: {k.replace('|', ' ')}, {rel[k].get('retried', 0)} retries "
                f"and {rel[k].get('provider_failed', 0)} failed of {rel[k].get('sentences', 0)} "
                "sentences (live)."
            )
        return lines or ["No spread data."]

    # T1 to T3.

    def t1(self) -> str:
        rec = self.off.get("recall") or {}
        rows = []
        for lang in self.langs:
            for c in ON:
                for r in RATES:
                    wrong = {lab: (rec.get(f"{lang}|{c}|{r}|{lab}") or {}).get("wrong", 0) for lab in LABELS}
                    flagged = (self.off.get("flagged") or {}).get(f"{lang}|{c}|{r}", 0)
                    rows.append((lang, c, r, *[num(wrong[lab]) for lab in LABELS], num(flagged)))
        out = (
            "## T1. Correctness (measured, 10 seeds × 200 sentences per row)\n\nWrong serves by "
            "label; `flagged` counts `punct_end` serves (`.` against `!`), which Level B may serve "
            "but flags (6.1).\n\n" + table(("language", "config", "r", *LABELS, "flagged"), rows)
        )
        v2 = self.lv.get("v2") or []
        if v2:
            live_rows = [
                (
                    x["run"],
                    num(x["wrong"]),
                    num(x["critical"]),
                    ", ".join(f"{k} {v}" for k, v in sorted(x["wrong_by_label"].items())) or "none",
                )
                for x in v2
            ]
            out += "\n**Live (live, seed 1, one run each).**\n\n" + table(
                ("run", "wrong serves", "critical", "wrong by label"), live_rows
            )
        return out + "\n" + self.asr_section()

    def asr_section(self) -> str:
        asr = self.a.get("asr") or []
        head = "**ASR on served clips (live, 7.2).** "
        if not asr:
            return head + "Not run: no live data, or ASR has not been run on it.\n"
        g: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for x in asr:
            if x.get("wer_stored") is not None:
                g.setdefault((x["lang"], x["config"]), []).append(x)
        rows = []
        for (lang, c), xs in sorted(g.items()):
            ws = sorted(x["wer_stored"] for x in xs)
            wr = sorted(x["wer_requested"] for x in xs)
            rows.append(
                (
                    lang,
                    c,
                    num(len(xs)),
                    num(ws[len(ws) // 2], 2),
                    num(wr[len(wr) // 2], 2),
                    share(sum(1 for x in xs if x.get("ok")) / len(xs)),
                )
            )
        return (
            head + "Word error rate of each served clip's transcript against the text stored with the "
            "clip and against the text the sentence requested (they differ on B and C hits). "
            "A clip passes at WER 0.2 or less in English and 0.4 in the Indic languages, where "
            "the transcriber writes some words in a neighbouring script.\n\n"
            + table(("language", "config", "hits", "WER stored p50", "WER requested p50", "pass"), rows)
        )

    def t2(self) -> str:
        rows = []
        for lang in self.langs:
            for r in RATES:
                d = (self.off.get("deltas") or {}).get(f"{lang}|{r}") or {}
                for c in ON:
                    x = self.cell(lang, c, r)
                    if not x:
                        continue
                    rate = (x.get("char_hit_rate") or {}).get("mean") or 0.0
                    saving = " / ".join(
                        f"{p} ${rate * USD_PER_MCHARS[p]:.2f}" for p in sorted(USD_PER_MCHARS)
                    )
                    audio_s = x.get("served_chars", 0) * MS_PER_CHAR / 1000 / max(1, len(x.get("seeds", [])))
                    rows.append(
                        (
                            lang,
                            r,
                            c,
                            ci(x.get("char_hit_rate")),
                            ci(d.get("B")) if c == "A+B" else ci(d.get("C")) if c == "A+B+C" else "",
                            ci(x.get("sentence_hit_rate")),
                            ci(x.get("capture_eff_expected")),
                            saving,
                            num(audio_s, 0),
                        )
                    )
        return (
            "## T2. Cost (hit rates measured, savings derived; 10 seeds)\n\n"
            "Char hit rate counts only correct serves. Saving is per million characters sent by "
            "the agent, at the "
            f"prices frozen in TEST_REPORT.md on {PRICE_DATE}. "
            f"Audio seconds saved are per 200-sentence corpus at "
            f"{MS_PER_CHAR:.0f} ms per character.\n\n"
            + table(
                (
                    "language",
                    "r",
                    "config",
                    "char hit rate %",
                    "Δ of this level, points",
                    "sentence hit rate %",
                    "capture efficiency, expected hits %",
                    "saving per 1M chars",
                    "audio s saved",
                ),
                rows,
            )
        )

    def t3(self) -> str:
        rec = self.off.get("recall") or {}
        rows = []
        for lang in self.langs:
            for c in ON:
                vals = []
                for lab in LABELS:
                    x = rec.get(f"{lang}|{c}|0.25|{lab}") or {}
                    vals.append(share(x.get("recall"), 0) if x else "–")
                para = rec.get(f"{lang}|{c}|0.25|paraphrase") or {}
                rows.append((lang, c, *vals, share(para.get("recall_visible"), 0)))
        return (
            "## T3. Recall per label at r = 0.25 (measured)\n\nThe share of each label's "
            "sentences served correctly; the last column is "
            "paraphrase recall among paraphrases whose base was visible in an earlier session, at "
            "`t_lang` = 0.90.\n\n"
            + table(("language", "config", *LABELS, "paraphrase recall when visible"), rows)
        )

    # T4 to T6: latency.

    def t4(self) -> str:
        l1 = self.lv.get("l1") or {}
        hit = self.lv.get("hit_ttfa") or {}
        l3 = self.lv.get("l3") or {}
        rows = []
        for lang in self.langs:
            for service in (*CORE.get(lang, ()), *SPREAD.get(lang, ())):
                for c in CONFIGS:
                    for r in RATES:
                        k = f"{lang}|{service}|{c}|{r}"
                        if k not in l1:
                            continue
                        rows.append(
                            (
                                lang,
                                service,
                                c,
                                r,
                                pcts(l1[k]),
                                pcts(hit.get(f"{lang}|{service}|{c}")) if c != "OFF" else "",
                                pcts(l3.get(k), ("p50", "p95")),
                            )
                        )
        return (
            "## T4. Live latency (live)\n\nL1 is the time from a turn's release to its first "
            "audio, ms, p50 / p95 / max (n). "
            "Hit time to first audio is per served sentence. L3 is the stall between sentences "
            "within a turn.\n\n"
            + table(("language", "service", "config", "r", "L1 turn start", "hit TTFA", "L3 stall"), rows)
        )

    def t5(self) -> str:
        comp = self.a.get("composed") or {}
        rows = []
        for k, v in sorted(comp.items()):
            lang, service, c, r = k.split("|")
            rows.append(
                (
                    lang,
                    service,
                    c,
                    r,
                    pcts(v.get("l1"), ("p50", "p90", "p95")),
                    ci(v.get("l1_p50_seeds"), points=False),
                    pcts(v.get("live_l1"), ("p50", "p95")),
                    num(v.get("agrees")) if "agrees" in v else "",
                )
            )
        return (
            "## T5. Composed latency (composed, 11.3)\n\nEach offline seed's hit sequence with "
            "measured hit paths and live OFF provider "
            "times; the p50 interval is over seeds. `agrees`: the live p50 lies within the OFF r "
            "= 0.25 versus 0.5 spread "
            "of the composed p50.\n\n"
            + table(
                (
                    "language",
                    "service",
                    "config",
                    "r",
                    "L1 p50 / p90 / p95",
                    "L1 p50 over seeds",
                    "live L1 p50 / p95",
                    "agrees",
                ),
                rows,
            )
        )

    def t6(self) -> str:
        l4 = self.off.get("l4") or {}
        l5 = self.off.get("l5") or {}
        rows = []
        for lang in self.langs:
            for c in ON:
                x = l4.get(f"{lang}|{c}|serverless") or l4.get(f"{lang}|{c}|single")
                rows.append(
                    (
                        lang,
                        c,
                        "L4 miss path",
                        pcts(x, ("p50", "p99", "p99.9", "max")),
                        num((x or {}).get("mean"), 3),
                    )
                )
            for tier in ("memory", "shared"):
                x = l5.get(f"{lang}|{tier}")
                if x:
                    rows.append((lang, tier, "L5 hit path", pcts(x, ("p50", "p99", "p99.9", "max")), ""))
        g = (self.vc.get("V6") or {}).get("per_language") or {}
        gate_rows = [
            (
                lang,
                num(v.get("A_mean_ms"), 3),
                num(v.get("A")),
                num(v.get("B_p99_ms"), 3),
                num(v.get("B")),
                num(v.get("C_p99_ms"), 3),
                num(v.get("C")),
            )
            for lang, v in g.items()
        ]
        return (
            "## T6. Overhead (measured, r = 0 control, ms)\n\nL4 is lookup plus capture enqueue "
            "on a miss; L5 is lookup plus the first decoded "
            "chunk on a hit. p99 and p99.9 appear only where n ≥ 1,000.\n\n"
            + table(("language", "config or tier", "path", "p50 / p99 / p99.9 / max", "mean"), rows)
            + "\nV6 gates at 1,000 warm texts: A mean under 1 ms, B p99 under 1 ms, C p99 under 6 ms.\n\n"
            + table(("language", "A mean ms", "A ok", "B p99 ms", "B ok", "C p99 ms", "C ok"), gate_rows)
        )

    # T7.

    def t7(self) -> str:
        from tts_cache.match.semantic import FLOOR_THRESHOLD, ROUTES

        pre = (self.a.get("preflight") or {}).get("items") or {}
        cov = pre.get("1_2_coverage_and_cache_path") or {}
        rel = self.lv.get("reliability") or {}
        rows = []
        for lang in self.langs:
            pf = [f"{k.split('|')[1]}: {v.get('status')}" for k, v in cov.items() if k.startswith(f"{lang}|")]
            rl = [
                f"{k.split('|')[1]}: {v.get('retried', 0)} retried, {v.get('provider_failed', 0)} "
                f"failed / {v.get('sentences', 0)}"
                for k, v in rel.items()
                if k.startswith(f"{lang}|")
            ]
            rows.append(
                (
                    lang,
                    "full" if ROUTES.get(lang) else "A and B only",
                    FLOOR_THRESHOLD,
                    "; ".join(pf) or "–",
                    "; ".join(rl) or "–",
                )
            )
        return "## T7. Languages\n\n" + table(
            ("language", "support", "t_lang", "preflight (live)", "provider reliability (live)"), rows
        )

    # T8 to T10: sizes.

    def t8(self) -> str:
        cap = self.sz.get("capacity") or {}
        rows = []
        for k, pts in sorted((cap.get("curves") or {}).items()):
            arm, lang, s = k.split("|")
            for p in sorted(pts, key=lambda p: p["frac"]):
                rows.append(
                    (
                        arm,
                        lang,
                        s,
                        share(p["frac"], 0),
                        share(p["char_hit_rate"]),
                        num(p["evicted"], 0),
                        num(p["fail_open"]),
                    )
                )
        knees = ", ".join(f"{k.replace('|', ' ')}: {share(v, 0)}" for k, v in (cap.get("knee") or {}).items())
        press = self.sz.get("pressure") or {}
        prows = [
            (
                *k.split("|"),
                ci(v["char_hit_rate"]),
                num(v["evicted_misses"]),
                num(v["wrong"]),
                num(v["critical"]),
            )
            for k, v in sorted(press.items())
        ]
        svg = line_chart(
            {
                k.replace("|", " "): [(p["frac"], p["char_hit_rate"]) for p in pts]
                for k, pts in (cap.get("curves") or {}).items()
            },
            title="Char hit rate against store capacity (measured)",
            xlabel="capacity, share of the full working set",
            ylabel="char hit rate",
            xlog=True,
        )
        return (
            "## T8. Capacity (measured, 9.1)\n\n"
            + table(
                ("store", "language", "Zipf s", "capacity", "char hit rate", "evicted keys", "fail open"),
                rows,
            )
            + "\nKnee (the smallest capacity reaching 90 percent of the full-capacity hit rate): "
            + f"{knees or '–'}.\n\n"
            + "B and C under pressure (SQLite `max_bytes` as a share of each cell's full stored bytes):\n\n"
            + table(
                ("language", "config", "capacity", "char hit rate %", "evicted misses", "wrong", "critical"),
                prows,
            )
            + "\n"
            + self.chart("capacity", svg, "Char hit rate against capacity, per store, language and Zipf s")
        )

    def t9(self) -> str:
        mem = self.sz.get("memory") or {}
        rows = []
        for k, v in sorted(
            mem.items(),
            key=lambda kv: (kv[0].split("|")[0], int(kv[0].split("|")[1]), int(kv[0].split("|")[2])),
        ):
            lang, wsb, wt = k.split("|")
            tiers = ", ".join(f"{t} {n}" for t, n in sorted((v.get("tiers") or {}).items()))
            lv = ", ".join(f"{t} {n}" for t, n in sorted((v.get("levels") or {}).items()))
            rows.append(
                (
                    lang,
                    f"{int(wsb) / 2**20:g} MB",
                    wt,
                    num(v.get("start_ms")),
                    num(v.get("rss_mb")),
                    tiers or "–",
                    pcts(v.get("first_turn_hit_ms"), ("p50", "p95")),
                    lv or "–",
                    share(v.get("char_hit_rate")),
                )
            )
        return "## T9. Process memory (measured, 9.2)\n\n" + table(
            (
                "language",
                "warm_start_bytes",
                "warm_texts",
                "warm start ms",
                "RSS MB",
                "hits by tier",
                "first-turn hit L1 ms",
                "hits by level",
                "char hit rate",
            ),
            rows,
        )

    def t10(self) -> str:
        pop = self.sz.get("population") or {}
        rows = []
        series_r: dict[str, list[tuple[float, float]]] = {}
        series_w: dict[str, list[tuple[float, float]]] = {}
        for k, v in sorted(pop.items(), key=lambda kv: (kv[0].split("|")[0], int(kv[0].split("|")[1]))):
            lang, n = k.split("|")
            rec = v.get("recall") or {}
            wrong = sum((v.get("wrong") or {}).values())
            rows.append(
                (
                    lang,
                    f"{int(n):,}",
                    pcts(v.get("b_probe_ms"), ("p50", "p99")),
                    pcts(v.get("c_probe_ms"), ("p50", "p99")),
                    num(v.get("index_build_ms"), 0),
                    num((v.get("index_peak_rss_growth") or 0) / 2**20),
                    share(rec.get("paraphrase"), 0),
                    share(rec.get("filler"), 0),
                    num(wrong),
                    num(v.get("critical")),
                )
            )
            series_r.setdefault(f"{lang} paraphrase recall", []).append(
                (int(n), rec.get("paraphrase") or 0.0)
            )
            series_w.setdefault(f"{lang} wrong per probe", []).append(
                (int(n), wrong / max(1, v.get("probes") or 1))
            )
        svg = line_chart(
            {**series_r, **series_w},
            title="Level B and C recall and wrong serves against N (measured)",
            xlabel="entries indexed (N)",
            ylabel="share",
            xlog=True,
        )
        return (
            "## T10. Population (measured, 9.3)\n\n"
            + table(
                (
                    "language",
                    "N",
                    "B probe ms p50 / p99",
                    "C probe ms p50 / p99",
                    "index build ms",
                    "index RSS growth MB",
                    "paraphrase recall",
                    "filler recall",
                    "wrong",
                    "critical",
                ),
                rows,
            )
            + "\n"
            + self.chart("population", svg, "B and C recall and wrong serves against N")
        )

    def t11(self) -> str:
        imp = self.a.get("impact") or {}
        money = imp.get("money") or {}
        rows = []
        for k, v in sorted(money.items()):
            lang, c, r, prov, vol = k.split("|")
            us = v.get("usd_saved") or {}
            rows.append(
                (
                    lang,
                    c,
                    r,
                    prov,
                    f"{int(vol):,}",
                    f"{num(us.get('mean'), 0)} [{num(us.get('lo'), 0)}, {num(us.get('hi'), 0)}]",
                    num(v.get("redis_gb"), 2),
                    num(v.get("net_usd"), 0),
                    share(v.get("break_even_rate"), 2),
                    share(v.get("turn_start_share")),
                )
            )
        wait = imp.get("wait") or {}
        wrows = [
            (
                *k.split("|"),
                num(v.get("per_turn_p50_ms"), 0),
                num(v.get("per_turn_p90_ms"), 0),
                num(v.get("per_call_p50_ms"), 0),
            )
            for k, v in sorted(wait.items())
        ]
        return (
            "## T11. Impact (derived, 13.2)\n\n"
            "Inputs: the offline char hit rate with its interval; characters per session as "
            f"characters per call; prices of {PRICE_DATE}; "
            f"managed Redis at ${REDIS_USD_PER_GB_MONTH:.0f} per GB-month (an assumption); Redis "
            "sized for two days of misses "
            "(probation keeps a singleton 48 hours) at the knee capacity of 9.1. Promoted "
            "repeats, which live 30 days, are not added, so the Redis cost is a lower bound and "
            "the break-even hit rate an optimistic one.\n\n"
            + table(
                (
                    "language",
                    "config",
                    "r",
                    "provider",
                    "calls/month",
                    "$ saved [95% interval]",
                    "Redis GB",
                    "net $",
                    "break-even hit rate",
                    "turns starting from cache",
                ),
                rows,
            )
            + "\nWaiting removed, from composed L1 against OFF (composed):\n\n"
            + table(
                (
                    "language",
                    "service",
                    "config",
                    "r",
                    "per hit turn p50 ms",
                    "per hit turn p90 ms",
                    "per call p50 ms",
                ),
                wrows,
            )
            + "\nPrices: "
            + "; ".join(f"{p}: {n}" for p, n in PRICE_NOTES.items())
            + ".\n"
        )

    def t12(self) -> str:
        us = self.us
        out = ["## T12. Usability (section 10)\n"]
        out.append(
            "**U1.** Not run: integration from the docs alone needs a fresh agent or developer "
            "and is outside this plan's build.\n"
        )
        u2 = us.get("u2") or {}
        rows = []
        for lang, x in u2.items():
            for r, y in (x.get("configs") or {}).items():
                rows.append(
                    (
                        lang,
                        r,
                        y.get("best"),
                        share(y.get("default_share_of_best")),
                        "; ".join(
                            f"{k['warm_start_bytes'] >> 20} MB/{k['warm_texts']} +{k['gain_points']}"
                            for k in x.get("better_knobs", [])
                        )
                        or "none",
                    )
                )
        out.append(
            "**U2, defaults against tuned (measured).**\n\n"
            + table(
                ("language", "r", "best config", "default saves, share of best", "knobs worth changing"), rows
            )
        )
        u3 = us.get("u3") or {}
        if u3:
            inst = u3.get("install") or {}
            model = u3.get("model") or {}
            out.append(
                "\n**U3, footprint (measured).** Install size over an empty venv: "
                + ", ".join(f"{k} {num((v.get('bytes') or 0) / 2**20)} MB" for k, v in inst.items())
                + ". Import: "
                + ", ".join(f"`{k}` {num(v.get('ms_p50'))} ms" for k, v in (u3.get("import") or {}).items())
                + f". Model: {num((model.get('download_bytes') or 0) / 2**20)} MB on disk, load "
                f"{num(model.get('load_ms_p50'), 0)} ms, first encode "
                f"{num((u3.get('model') or {}).get('first_encode_ms_p50'), 0)} ms.\n\n"
                + table(
                    ("config", "RSS max MB", "first session start ms", "later session start ms p50 / max"),
                    [
                        (
                            c,
                            num(v.get("rss_max_mb")),
                            num(v.get("first_start_ms")),
                            f"{num(v.get('start_ms_p50'))} / {num(v.get('start_ms_max'))}",
                        )
                        for c, v in (u3.get("configs") or {}).items()
                    ],
                )
            )
        u4 = us.get("u4") or {}
        out.append(
            "\n**U4, prediction before deploying (live).** "
            + (
                table(
                    ("run", "config", "replay %", "measured %", "difference, points"),
                    [
                        (
                            x["run"],
                            x["config"],
                            share(x["replay"]),
                            share(x["measured"]),
                            num(x["diff_points"], 2),
                        )
                        for x in u4.get("runs", [])
                    ],
                )
                if u4.get("runs")
                else "No live runs.\n"
            )
        )
        u5 = us.get("u5") or {}
        rows = [
            (
                k,
                len(v["host_errors"]),
                num(v["window_s"]),
                f"{num(v['added_ms']['p50'])} / {num(v['added_ms']['p99'])} / {num(v['added_ms']['max'])}",
                num(v.get("bound_ms"), 2),
                "yes" if v.get("within_deadline") else "no",
                " → ".join(share(v["hit_rate"][p], 0) for p in ("before", "during", "after")),
                num(v["recovery_s"], 2),
                ", ".join(f"{a} {b}" for a, b in v["degraded"].items()) or "–",
            )
            for k, v in u5.items()
            if isinstance(v, dict)
        ]
        out.append(
            "\n**U5, the store failing (measured, 200 sentences, en, A+B+C).** The bound is section "
            "10's: the 20 ms deadline plus the first decoded chunk. Host errors must be zero.\n\n"
            + table(
                (
                    "fault",
                    "host errors",
                    "window s",
                    "added ms p50 / p99 / max",
                    "bound ms",
                    "max within bound",
                    "hit rate before → during → after",
                    "recovery s",
                    "degraded lookups",
                ),
                rows,
            )
        )
        u6 = us.get("u6") or {}
        out.append(
            "\n**U6, observability (measured).** "
            + (
                table(
                    (
                        "cell",
                        "metrics: hits / saved chars",
                        "truth: hits / saved chars",
                        "rejects (metrics)",
                        "equal",
                    ),
                    [
                        (
                            c["cell"],
                            f"{c['metrics']['hits']} / {c['metrics']['saved_chars']}",
                            f"{c['truth']['hits']} / {c['truth']['saved_chars']}",
                            json.dumps(c["metrics"]["rejects"]),
                            num(c["pass"]),
                        )
                        for c in u6.get("cells", [])
                    ],
                )
                if u6
                else "Not run.\n"
            )
        )
        out.append(
            "\n**U7.** Dropped by the owner's decision of 2026-10-18; the ASR check of 7.2 stands "
            "in for every language.\n"
        )
        u8 = us.get("u8") or {}
        rows = [
            (
                k,
                num(v["hits"]),
                share(v["char_hit_rate"]),
                num(v["wrong_count"]),
                num(v["named"]),
                ", ".join(f"{a} {b}" for a, b in v["signals"].items()) or "–",
            )
            for k, v in u8.items()
            if isinstance(v, dict)
        ]
        out.append(
            "\n**U8, misconfiguration (measured, fake service, en, A+B+C).**\n\n"
            + table(
                (
                    "scenario",
                    "hits",
                    "char hit rate",
                    "wrong serves",
                    "named by a warning or metric",
                    "signals",
                ),
                rows,
            )
        )
        return "\n".join(out) + "\n"

    def t13(self) -> str:
        ses = self.off.get("sessions") or {}
        rows = []
        for k, v in sorted(ses.items()):
            lang, c, r = k.split("|")
            if c == "OFF":
                continue
            rows.append(
                (
                    lang,
                    c,
                    r,
                    v["n"],
                    f"{share(v['rate_p10'], 0)} / {share(v['rate_p50'], 0)} / {share(v['rate_p90'], 0)}",
                    share(v["zero_hit_share"], 0),
                    share(v["all_turns_cached_share"], 0),
                    share(v["turn_start_hit_share"], 0),
                )
            )
        win = self.lv.get("windows") or {}
        wrows = [
            (k.replace("|", " "), ", ".join(f"{i}: {num(x['p50'], 0)} (n={x['n']})" for i, x in v.items()))
            for k, v in sorted(win.items())
        ]
        vals = self.a.get("l1_values") or {}
        out = [
            "## T13. Spread\n",
            "Per-session distributions (measured; sessions stand in for calls):\n",
            table(
                (
                    "language",
                    "config",
                    "r",
                    "sessions",
                    "session char hit rate p10 / p50 / p90",
                    "zero-hit sessions",
                    "every turn cached",
                    "turn starts from cache",
                ),
                rows,
            ),
            "\nDrift of live L1 p50 per 10-minute window (live):\n",
            table(("cell", "window: p50 ms (n)"), wrows),
        ]
        rates = {
            f"{lang} {c}": (self.cell(lang, c, 0.25).get("char_hit_rate") or {}).get("mean")
            for lang in self.langs
            for c in ON
        }
        rates = {k: v for k, v in rates.items() if v is not None}
        if rates:
            s = sorted(rates.values())
            out.append(
                "\nAcross languages and configurations at r = 0.25 (measured): min "
                f"{share(s[0])}, median {share(s[len(s) // 2])}, max {share(s[-1])}.\n"
            )
        # Chart: L1 CDF per provider.
        by_prov: dict[str, dict[str, list[float]]] = {}
        for k, v in vals.items():
            lang, service, c, _r = k.split("|")
            prov = PROVIDER.get(service) or service
            by_prov.setdefault(prov, {}).setdefault(c, []).extend(v)
        for prov, per in sorted(by_prov.items()):
            svg = line_chart(
                {c: cdf(v) for c, v in per.items() if v},
                title=f"Turn-start L1 CDF, {prov} (live)",
                xlabel="L1 ms",
                ylabel="share of turns",
            )
            out.append(
                self.chart(
                    f"l1_cdf_{prov}",
                    svg,
                    f"Turn-start L1 CDF for {prov}, cache off against each configuration",
                )
            )
        # Chart: hit rate against r.
        series = {}
        for lang in self.langs:
            for c in ON:
                pts = [
                    (r, (self.cell(lang, c, r).get("char_hit_rate") or {}).get("mean"))
                    for r in (0.0, 0.25, 0.5)
                ]
                pts = [(r, v) for r, v in pts if v is not None]
                if len(pts) > 1:
                    series[f"{lang} {c}"] = pts
        if len(series) > len(PALETTE):
            series = {k: v for k, v in series.items() if k.endswith("A+B+C")}
        out.append(
            self.chart(
                "hit_rate_vs_r",
                line_chart(
                    series,
                    title="Char hit rate against r (measured)",
                    xlabel="r, repeat share",
                    ylabel="char hit rate",
                ),
                "Char hit rate against r per language (A+B+C when many languages)",
            )
        )
        # Chart: per-session saving histograms.
        hist = {
            k.replace("|", " "): [(i / 10 + 0.05, n) for i, n in enumerate(v["hist"])]
            for k, v in ses.items()
            if k.endswith("|A+B+C|0.25")
        }
        out.append(
            self.chart(
                "session_saving",
                line_chart(
                    hist,
                    title="Sessions by char hit rate, A+B+C, r = 0.25 (measured)",
                    xlabel="session char hit rate",
                    ylabel="sessions",
                ),
                "Per-session saving histogram, A+B+C at r = 0.25",
            )
        )
        return "\n".join(out) + "\n"

    def vchecks(self) -> str:
        vc = self.vc
        rows = []
        for name in ("V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9", "V10"):
            v = vc.get(name) or {}
            p = v.get("pass")
            detail = ""
            if name == "V1":
                detail = f"offline {num(v.get('offline'))}, live {num(v.get('live'))}"
            elif name == "V2":
                n_off, n_live = len(v.get("offline_critical") or []), len(v.get("live_critical") or [])
                detail = (
                    f"{n_off} offline and {n_live} live critical serves; "
                    f"punct_question served {num(v.get('punct_question_served'))}"
                )
            elif name == "V3":
                detail = f"{len(v.get('unexplained') or [])} misses without a reason"
            elif name == "V4":
                detail = ", ".join(
                    f"{x['run']}: {x['mismatches']} differ, {len(x['unexplained'])} unexplained"
                    for x in v.get("runs", [])[:6]
                )
            elif name == "V5":
                detail = f"max {num(v.get('max_diff_points'), 2)} points"
            elif name == "V7":
                detail = f"{len(v.get('violations') or [])} violations"
            elif name == "V8":
                crit = v.get("pressure_critical") or "none"
                detail = f"{v.get('capacity_errors', 0)} errors; critical under pressure: {crit}"
            elif name == "V10":
                detail = f"offline {num(v.get('offline'))}, U6 {num(v.get('u6'))}"
            rows.append((name, "not evaluated" if p is None else ("pass" if p else "FAIL"), detail))
        return (
            "## Validity checks (section 12)\n\nA failing check marks the numbers it affects as "
            "not established.\n\n" + table(("check", "result", "detail"), rows)
        )

    def header(self) -> str:
        meta = self.a.get("meta") or {}
        fz = meta.get("freeze") or {}
        tm = meta.get("timings") or {}
        lines = [
            "# Test report: usefulness, impact, usability and spread of the TTS cache\n",
            "Generated by `python -m tts_cache.testplan.report` from the saved data of one run "
            "directory; nothing in it was typed by hand.\n",
            f"- Run directory: `{self.a.get('run_dir')}`",
            f"- Languages: {', '.join(self.langs)}",
            f"- Freeze commit: {fz.get('sha', '–')}; Pipecat {fz.get('pipecat_sha', '–')}; Sarvam "
            f"model {fz.get('sarvam_model', '–')}",
            f"- Offline runs: {num(self.off.get('runs'))}; live runs: {num(len(self.lv.get('runs') or {}))}",
        ]
        if tm:
            lines.append("- Stage wall times: " + ", ".join(f"{k} {num(v)} s" for k, v in tm.items()))
        lines.append(
            "\nKinds: **live** against a provider; **measured** offline on this machine; "
            "**simulated** offline billing and "
            "timing of the simulated provider; **composed** (11.3); **derived** (13.2). Intervals "
            "are 95 percent bootstrap "
            "intervals over seeds; percentiles carry n.\n"
        )
        return "\n".join(lines) + "\n"

    def render(self) -> str:
        parts = [
            self.header(),
            self.t0(),
            self.vchecks(),
            self.t1(),
            self.t2(),
            self.t3(),
            self.t4(),
            self.t5(),
            self.t6(),
            self.t7(),
            self.t8(),
            self.t9(),
            self.t10(),
            self.t11(),
            self.t12(),
            self.t13(),
        ]
        return "\n".join(parts)


def _short(s: Any, n: int = 70) -> str:
    s = str(s or "").replace("|", "/")
    return s if len(s) <= n else s[: n - 1] + "…"


def write(run_dir: Path, md: Path | None = None) -> Path:
    from tts_cache.testplan.analysis import analyse

    p = run_dir / "analysis.json"
    a = analyse(run_dir)
    p.write_text(json.dumps(a, ensure_ascii=False, default=str), encoding="utf-8")
    md = md or run_dir / "TEST_REPORT.md"
    charts = md.parent / "test-report"
    rep = Report(json.loads(p.read_text(encoding="utf-8")), charts, "test-report")
    md.write_text(rep.render(), encoding="utf-8")
    (charts / "index.json").parent.mkdir(parents=True, exist_ok=True)
    (charts / "index.json").write_text(json.dumps(rep.charts), encoding="utf-8")
    return md


REQUIRED = tuple(f"## T{i}." for i in range(14))
REQUIRED_CHARTS = ("capacity", "population", "hit_rate_vs_r", "session_saving")


def check(md: Path) -> list[str]:
    """What the generated report lacks: a table T0 to T13 or a chart of section 14."""
    text = md.read_text(encoding="utf-8")
    missing = [t for t in REQUIRED if t not in text]
    charts = json.loads((md.parent / "test-report" / "index.json").read_text())
    missing += [f"chart {c}" for c in REQUIRED_CHARTS if c not in charts]
    if not any(c.startswith("l1_cdf_") for c in charts):
        missing.append("chart l1_cdf")
    return missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tts_cache.testplan.report")
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--md", type=Path)
    a = ap.parse_args(argv)
    md = write(a.run_dir, a.md)
    missing = check(md)
    print(json.dumps({"report": str(md), "missing": missing}))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
