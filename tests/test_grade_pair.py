"""grade_pair: answer-only packets, readability preference, styles."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "answer_quality"))

import grade_pair  # noqa: E402


def _runs(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    rec = lambda i, style: {"id": i, "answer": f"answer {i} {style}", "answer_style": style,
                            "claims": [{"text": "c", "quote": "q q q q", "passage": 1},
                                       {"text": "d", "quote": "q q q q", "passage": 2}]}
    (runs / "r.a.jsonl").write_text("\n".join(json.dumps(rec(i, "claims")) for i in ("Q1", "Q2")) + "\n")
    (runs / "r.b.jsonl").write_text("\n".join(json.dumps(rec(i, s)) for i, s in (("Q1", "prose"), ("Q2", "claims"))) + "\n")
    qs = tmp_path / "q.json"
    qs.write_text(json.dumps([{"id": "Q1", "question": "one?"}, {"id": "Q2", "question": "two?"}]))
    monkeypatch.setattr(grade_pair, "HERE", tmp_path)
    monkeypatch.setitem(grade_pair.SETS, "dev", qs)
    return tmp_path


def test_answer_only_packets_carry_just_the_answer(tmp_path, monkeypatch):
    _runs(tmp_path, monkeypatch)
    grade_pair.packet("r", ["a", "b"], "dev", tmp_path / "out", seed=1, answer_only=True)
    item = json.loads((tmp_path / "out" / "packet.json").read_text())[0]
    assert set(item["A"]) == {"answer"} and set(item["B"]) == {"answer"}


def test_styles_reports_the_fallback_rate(tmp_path, monkeypatch, capsys):
    _runs(tmp_path, monkeypatch)
    grade_pair.styles("r", ["a", "b"])
    out = capsys.readouterr().out
    assert "b: prose 1, claims 1" in out and "fallback 50%" in out


def test_score_counts_the_readability_preference(tmp_path, capsys):
    out = tmp_path / "g"
    (out / "sealed").mkdir(parents=True)
    (out / "sealed" / "key.json").write_text(json.dumps({"Q1": {"A": "a", "B": "b"}, "Q2": {"A": "b", "B": "a"}}))
    g = lambda f: {"facts_covered": f, "facts_total": 3, "false_claims": 0, "duplicates": 0, "verdict": "partly"}
    (out / "grades.json").write_text(json.dumps([
        {"id": "Q1", "A": g(1), "B": g(2), "preferred": "B"},
        {"id": "Q2", "A": g(2), "B": g(1), "preferred": "A"}]))
    grade_pair.score(out)
    text = capsys.readouterr().out
    assert "readability: b preferred 2, a preferred 0, tie 0" in text
