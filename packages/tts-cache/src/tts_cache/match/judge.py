"""Level C's judge: a cross-encoder reads both sentences together (opt-in, ``[similarity]`` extra).

The encoder and the vetoes decide from two independent vectors and a token
diff, and the test plan of 2026-10-19 (TEST_REPORT.md) found that this lets a
changed meaning through in 11 of 12 languages: "cancel" served for
"reschedule", "15 percent off" for "15 percent extra", an unrelated sentence
that shared one number. An embedding places a sentence and its opposite close
together because both are about the same thing. A natural-language-inference
model reads the pair in one pass and says whether one entails the other. The
judge runs after the vetoes on the one candidate they let through, and accepts
it only when each sentence entails the other with probability at least
``threshold``; otherwise the sentence goes to the provider.

The default is mDeBERTa-v3-base fine-tuned on multilingual NLI, exported to
ONNX and run with onnxruntime like the encoder. On the 1,228 distinct pairs
Level C served in the test plan's offline matrix (93 with a changed meaning,
1,135 paraphrases, ``tests/fixtures/judge/``) the highest changed pair scores
0.60, so the default threshold keeps a margin of 0.10, and 52 percent of the
paraphrases clear it. Rerun end to end on that matrix, Level C's wrong-meaning
serves fell from 97 to 0 and its hits by half; in Assamese, Marathi and Odia
the model rarely calls a paraphrase entailed, so there Level C adds almost
nothing. It needs the vetoes as well: alone it accepts a transposed amount
(₹1,450 against ₹1,540), which the slot veto refuses. It costs a
1.1 GB download (the int8 export flips 7 percent of decisions, so the
full-precision model is the one used), about 2.5 GB of resident memory per
process (loaded once, shared by every cache in it), and 17 to 40 ms per
candidate on two threads, Odia the slowest at twice the tokens. That is why it
is opt-in and why a cache whose matcher has a judge extends its lookup
deadline by ``JUDGE_DEADLINE_MS``.
"""

import json
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_JUDGE = "onnx-community/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7-ONNX"
JUDGE_THRESHOLD = 0.70
# Added to a cache's lookup deadline when its matcher has a judge; a check
# that runs past it is a deadline miss, so the sentence goes to the provider.
JUDGE_DEADLINE_MS = 100.0


class Judge(Protocol):
    def score(self, stored: str, asked: str) -> float: ...


class NliJudge:
    """An NLI cross-encoder exported to ONNX, run with onnxruntime on the CPU.

    Loads ``onnx/model.onnx``, ``tokenizer.json`` and ``config.json`` from the
    Hugging Face hub (cached locally after the first download). ``score``
    returns the smaller of the two entailment probabilities, stored to asked
    and asked to stored.
    """

    def __init__(self, name: str = DEFAULT_JUDGE, *, threads: int = 2, max_tokens: int = 256) -> None:
        import onnxruntime as rt
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        self.name = name
        self._tok = Tokenizer.from_file(hf_hub_download(name, "tokenizer.json"))
        self._tok.enable_truncation(max_tokens)
        self._tok.enable_padding()
        labels = json.loads(Path(hf_hub_download(name, "config.json")).read_text(encoding="utf-8"))[
            "id2label"
        ]
        self._entail = next(int(i) for i, label in labels.items() if label.lower().startswith("entail"))
        opts = rt.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self._sess = rt.InferenceSession(
            hf_hub_download(name, "onnx/model.onnx"), opts, providers=["CPUExecutionProvider"]
        )
        self._inputs = {i.name for i in self._sess.get_inputs()}

    def _entailment(self, pairs: Sequence[tuple[str, str]]) -> Any:
        import numpy as np

        enc = self._tok.encode_batch(list(pairs))
        feed: dict[str, Any] = {
            "input_ids": np.asarray([e.ids for e in enc], dtype=np.int64),
            "attention_mask": np.asarray([e.attention_mask for e in enc], dtype=np.int64),
        }
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.asarray([e.type_ids for e in enc], dtype=np.int64)
        logits = np.asarray(self._sess.run(None, feed)[0], dtype=np.float32)
        p = np.exp(logits - logits.max(axis=1, keepdims=True))
        return (p / p.sum(axis=1, keepdims=True))[:, self._entail]

    def score(self, stored: str, asked: str) -> float:
        return float(self._entailment([(stored, asked), (asked, stored)]).min())

    def scores(self, pairs: Sequence[tuple[str, str]], batch: int = 16) -> list[float]:
        """``score`` for many pairs, ``batch`` pairs per model call."""
        out: list[float] = []
        for i in range(0, len(pairs), batch):
            chunk = list(pairs[i : i + batch])
            e = self._entailment(chunk + [(b, a) for a, b in chunk])
            out.extend(float(min(x, y)) for x, y in zip(e[: len(chunk)], e[len(chunk) :], strict=True))
        return out


_JUDGES: dict[str, NliJudge] = {}
_JUDGES_LOCK = threading.Lock()


def load_judge(name: str = DEFAULT_JUDGE) -> NliJudge:
    """The judge, loaded once per process."""
    with _JUDGES_LOCK:
        judge = _JUDGES.get(name)
        if judge is None:
            judge = _JUDGES[name] = NliJudge(name)
        return judge


@dataclass(frozen=True)
class Calibration:
    pairs: int
    threshold: float
    paraphrase_share: float
    refused_max: float


def calibrate(pairs: Iterable[Mapping[str, str]], judge: Any) -> Calibration:
    """The lowest threshold that refuses every pair not labelled ``paraphrase``, and the paraphrases it keeps.

    ``pairs`` are mappings with ``a``, ``b`` and ``label``, as in
    ``tests/fixtures/semantic/<lang>.yaml``; ``a`` is the stored sentence.
    """
    rows = list(pairs)
    texts = [(p["a"], p["b"]) for p in rows]
    scores = judge.scores(texts) if hasattr(judge, "scores") else [judge.score(a, b) for a, b in texts]
    para = [s for s, p in zip(scores, rows, strict=True) if p["label"] == "paraphrase"]
    other = [s for s, p in zip(scores, rows, strict=True) if p["label"] != "paraphrase"]
    threshold = (max(other) + 1e-6) if other else 0.0
    return Calibration(
        pairs=len(rows),
        threshold=round(threshold, 6),
        paraphrase_share=sum(s >= threshold for s in para) / len(para) if para else 0.0,
        refused_max=round(max(other), 6) if other else 0.0,
    )
