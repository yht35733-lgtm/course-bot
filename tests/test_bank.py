import json

from coursebot.bank import Bank, normalize, similarity


def test_normalize_strips_punct_and_width():
    assert normalize("ＡＢＣ， 123！") == "abc123"
    assert normalize("  TCP  三次握手？ ") == "tcp三次握手"
    assert normalize(None) == ""


def test_similarity_levels():
    assert similarity("TCP三次握手", "TCP三次握手") == 1.0
    assert similarity("TCP三次握手第二个报文段的标志位是", "三次握手第二个报文段") > 0.5
    assert similarity("完全无关的题干", "TCP") < 0.5


def test_add_search_and_bump(tmp_path):
    bank = Bank(tmp_path / "b.db")
    added = bank.add("TCP 三次握手中第二个报文段的标志位是", ["1"], "single",
                     ["SYN", "SYN+ACK", "ACK", "FIN"])
    assert added is True
    assert bank.add("TCP 三次握手中第二个报文段的标志位是", ["1"], "single") is False

    hit, score = bank.search("TCP三次握手中第二个报文段的标志位是", "single")
    assert hit is not None and score == 1.0
    assert hit["answer"] == ["1"]

    # 排版差异仍应命中
    hit2, score2 = bank.search("tcp 三次握手 中，第二个报文段的标志位是？", "single", 0.8)
    assert hit2 is not None and score2 > 0.8

    bank.bump("TCP 三次握手中第二个报文段的标志位是", "single", True)
    assert bank.stats()["hits"] == 1


def test_import_export_roundtrip(tmp_path):
    src = tmp_path / "seed.jsonl"
    src.write_text(
        json.dumps({"question": "HTTP 是无状态协议", "qtype": "judge",
                    "options": ["正确", "错误"], "answer": ["0"]}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    bank = Bank(tmp_path / "b.db")
    a, u = bank.import_file(src)
    assert (a, u) == (1, 0)

    out = tmp_path / "out.jsonl"
    n = bank.export_file(out)
    assert n == 1
    rec = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert rec["qtype"] == "judge" and rec["answer"] == ["0"]


def test_csv_import(tmp_path):
    src = tmp_path / "s.csv"
    src.write_text("question,answer,qtype\n1+1=?,2,fill\n", encoding="utf-8")
    bank = Bank(tmp_path / "b.db")
    a, _ = bank.import_file(src)
    assert a == 1
    hit, _ = bank.search("1+1=?", "fill")
    assert hit["answer"] == ["2"]
