from coursebot.bank import Bank
from coursebot.quiz import Question, QuizEngine


def make_engine(tmp_path):
    bank = Bank(tmp_path / "b.db")
    return QuizEngine(bank, {"min_similarity": 0.82, "submit": False}, {}), bank


def test_map_answer_variants(tmp_path):
    eng, _ = make_engine(tmp_path)
    q = Question(index=0, stem="x", qtype="single",
                 options=["1945年", "1949年", "1950年", "1978年"])
    assert eng._map_answer(["1"], [], q) == [1]
    assert eng._map_answer([1], [], q) == [1]
    assert eng._map_answer(["B"], [], q) == [1]
    assert eng._map_answer(["1949年"], [], q) == [1]
    assert eng._map_answer([], [], q) == []


def test_map_answer_respects_option_reorder(tmp_path):
    eng, _ = make_engine(tmp_path)
    q = Question(index=0, stem="x", qtype="single",
                 options=["1978年", "1960年", "1949年", "1955年"])
    # 题库存的是选项原文，当前题选项顺序变了
    assert eng._map_answer(["1949年"], ["1945年", "1949年"], q) == [2]


def test_solve_hits_bank(tmp_path):
    eng, bank = make_engine(tmp_path)
    bank.add("TCP三次握手第二个报文段的标志位是", ["1"], "single",
             ["SYN", "SYN+ACK", "ACK", "FIN"])
    q = Question(index=0, stem="TCP 三次握手中第二个报文段的标志位是？", qtype="single",
                 options=["SYN", "SYN+ACK", "ACK", "FIN"])
    eng.solve(q)
    assert q.source == "bank"
    assert q.answer == [1]


def test_solve_falls_back_to_guess(tmp_path):
    eng, _ = make_engine(tmp_path)
    q = Question(index=0, stem="题库里绝对没有这道题", qtype="single",
                 options=["A", "B"])
    eng.solve(q)
    assert q.source == "guess"
    assert q.answer == [0]


def test_judge_guess(tmp_path):
    eng, _ = make_engine(tmp_path)
    q = Question(index=0, stem="某判断题", qtype="judge", options=["正确", "错误"])
    eng.solve(q)
    assert q.answer == [0]
