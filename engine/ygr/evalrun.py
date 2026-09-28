"""Regression eval: Analyst -> Writer -> Critic over a frozen set of real threads.

Runs against a throwaway store so eval drafts never touch the real ledger.
Compares against eval/baseline.json; a pass-rate drop of more than 10 points,
or half a revision more on average, counts as a regression. Coach runs this
before any approved learned note reaches the Writer. It costs real tokens.
"""
from __future__ import annotations

import dataclasses
import json
import statistics
import tempfile
import time
from pathlib import Path

from .config import ENGINE_DIR
from .reddit_io import Thread
from .store import Store, iso, utcnow
from .agents import analyst, drafting

EVAL_DIR = ENGINE_DIR / "eval"


def load_threads() -> list[tuple[Thread, dict]]:
    out = []
    for f in sorted((EVAL_DIR / "threads").glob("*.json")):
        d = json.loads(f.read_text())
        t = dict(d["thread"])
        if "age_hours" in t:
            t["created_utc"] = time.time() - float(t.pop("age_hours")) * 3600
        out.append((Thread.from_dict(t), d))
    return out


def run_eval(ctx, update_baseline: bool = False) -> dict:
    cases = load_threads()
    if not cases:
        return {"skipped": True, "reason": "no eval threads in engine/eval/threads"}
    tmp = Path(tempfile.mkdtemp(prefix="ygr-eval-"))
    ectx = dataclasses.replace(ctx, store=Store(tmp / "eval.sqlite"))
    per = []
    for t, meta in cases:
        mode = meta.get("posting_mode", "value_only")
        rc = {"phase": meta.get("phase", "warmup"), "topic": meta.get("topic", "eval")}
        try:
            a = analyst.assess(ectx, [(t, {}, mode, "")], rc).get(t.id)
            if not a:
                per.append({"id": t.id, "status": "error", "error": "no assessment"})
                continue
            score = a["expertise"] + a["gap"] + a["audience"] + 2
            brief = {"thread_id": t.id, "sub": t.sub, "title": t.title, "url": t.url, "question": a["question"],
                     "already_covered": a["already_covered"], "gap_angle": a["gap_angle"],
                     "comment_type": "talking_points" if mode == "talking_points" else "value",
                     "yuvgro_link": None, "posting_mode": mode, "sub_policy": meta.get("sub_policy", ""),
                     "neutral_links_allowed": False, "target_words": int(ctx.cfg.tunables.get("target_words", 170)),
                     "fit_score": score, "phase": rc["phase"], "topic": rc["topic"], "source": "eval"}
            res = drafting.write_and_review(ectx, brief, t, None, "comment")
            per.append({"id": t.id, "sub": t.sub, "score": score, "status": res["status"], "revisions": res["revisions"],
                        "gate1": len((res["review"] or {}).get("gate1", [])), "text": res["draft"]["text"],
                        "reference": meta.get("reference_comment", "")})
        except Exception as e:  # noqa: BLE001 — one bad case shouldn't sink the eval
            per.append({"id": t.id, "status": "error", "error": f"{type(e).__name__}: {e}"})

    ok = [p for p in per if p["status"] != "error"]
    metrics = {
        "n": len(per),
        "errors": len(per) - len(ok),
        "pass_rate": round(sum(p["status"] == "pass" for p in ok) / len(ok), 3) if ok else 0.0,
        "avg_revisions": round(statistics.mean(p["revisions"] for p in ok), 2) if ok else 0.0,
        "avg_score": round(statistics.mean(p["score"] for p in ok), 2) if ok else 0.0,
    }
    base_path = EVAL_DIR / "baseline.json"
    baseline = json.loads(base_path.read_text()) if base_path.exists() else None
    regression = bool(baseline) and (metrics["pass_rate"] < baseline["pass_rate"] - 0.10
                                     or metrics["avg_revisions"] > baseline["avg_revisions"] + 0.5
                                     or metrics["errors"] > baseline.get("errors", 0))
    result = {"at": iso(utcnow()), "metrics": metrics, "baseline": baseline, "regression": regression, "cases": per}
    out_dir = ctx.cfg.data_dir / "evals"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{int(time.time())}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    if update_baseline or baseline is None:
        base_path.write_text(json.dumps(metrics, indent=2) + "\n")
    return result
