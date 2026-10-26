"""本地模拟平台：把超星的关键页面结构复刻在 127.0.0.1 上。

用于在没有真实账号的情况下，跑通 runner 的完整链路：
课程列表 -> 章节 -> 视频(iframe) -> 弹题 -> 测验 -> 提交。

页面结构与真实平台一致的选择器：
    .course / .posCatalog_select / iframe#iframe / .ans-videoquiz-opt / .questionLi
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

EVENTS: list[str] = []
_LOCK = threading.Lock()


def record(msg: str) -> None:
    with _LOCK:
        EVENTS.append(msg)


HEAD = '<html lang="zh-CN"><head><meta charset="utf-8"><title>mock</title></head><body>'

COURSE_PAGE = HEAD + """
<div>我的课程</div>
<div class="course"><a href="/mycourse/studentcourse?courseId=1001">高等数学（演示课程）</a></div>
</body></html>
"""

CHAPTER_PAGE = HEAD + """
<div>我的课程</div>
<ul>
  <li><a class="posCatalog_select" href="/knowledge?ch=1">第1章 极限与连续</a></li>
  <li><a class="posCatalog_select" href="/quiz?ch=1">第1章 测验</a></li>
</ul>
</body></html>
"""

KNOWLEDGE_PAGE = HEAD + """
<div>我的课程</div>
<iframe id="iframe" src="/video?ch=1" style="width:800px;height:450px;border:1px solid #444"></iframe>
</body></html>
"""

VIDEO_PAGE = HEAD + """
<div class="ans-videoquiz-opt questionLi" id="quiz" style="display:none;border:1px solid #3a3">
  <div class="Zy_TItle">TCP 三次握手中第二个报文段的标志位是</div>
  <ul style="list-style:none;padding-left:0">
    <li data-correct="0" style="cursor:pointer">SYN</li>
    <li data-correct="1" style="cursor:pointer">SYN+ACK</li>
    <li data-correct="0" style="cursor:pointer">ACK</li>
    <li data-correct="0" style="cursor:pointer">FIN</li>
  </ul>
  <a class="completeBtn" href="javascript:void(0)" onclick="submitQuiz()">提交</a>
</div>
<video id="v" width="320" height="180"></video>
<script>
window.__t = 0; window.__d = 30; window.__paused = false;
var v = document.getElementById('v');
Object.defineProperty(v, 'duration', { get: function () { return window.__d; } });
Object.defineProperty(v, 'currentTime', {
  get: function () { return window.__t; },
  set: function (x) { window.__t = Math.max(0, Math.min(window.__d, x)); }
});
Object.defineProperty(v, 'paused', { get: function () { return window.__paused; } });
Object.defineProperty(v, 'ended', { get: function () { return window.__t >= window.__d; } });
v.play = function () {
  window.__paused = false;
  if (!window.__timer) {
    window.__timer = setInterval(function () {
      if (!window.__paused) { window.__t = Math.min(window.__d, window.__t + 0.25); }
    }, 50);
  }
  return Promise.resolve();
};
v.pause = function () { window.__paused = true; };

document.querySelectorAll('#quiz li').forEach(function (li) {
  li.addEventListener('click', function () {
    document.querySelectorAll('#quiz li').forEach(function (x) { x.classList.remove('sel'); });
    li.classList.add('sel');
    fetch('/api/event?msg=' + encodeURIComponent('popup-pick:' + li.textContent));
  });
});

function submitQuiz() {
  var picked = document.querySelector('#quiz li.sel');
  var ok = picked && picked.getAttribute('data-correct') === '1';
  fetch('/api/event?msg=' + encodeURIComponent('popup-submit:' + (ok ? 'correct' : 'wrong')));
  document.getElementById('quiz').style.display = 'none';
  window.__quizDone = true;
}

setTimeout(function () {
  if (!window.__quizDone) { document.getElementById('quiz').style.display = 'block'; }
}, 900);
</script>
</body></html>
"""

QUIZ_PAGE = HEAD + """
<div>我的课程</div>
<div id="result"></div>
<div class="questionLi">
  <div class="Zy_TItle">中华人民共和国成立于哪一年</div>
  <ul style="list-style:none;padding-left:0">
    <li data-correct="0" style="cursor:pointer">A. 1945年</li>
    <li data-correct="1" style="cursor:pointer">B. 1949年</li>
    <li data-correct="0" style="cursor:pointer">C. 1950年</li>
    <li data-correct="0" style="cursor:pointer">D. 1978年</li>
  </ul>
</div>
<div class="questionLi">
  <div class="Zy_TItle">HTTP 协议是无状态协议</div>
  <ul style="list-style:none;padding-left:0">
    <li data-correct="1" style="cursor:pointer">正确</li>
    <li data-correct="0" style="cursor:pointer">错误</li>
  </ul>
</div>
<div class="questionLi">
  <div class="Zy_TItle">下列哪个不属于关系型数据库</div>
  <ul style="list-style:none;padding-left:0">
    <li data-correct="1" style="cursor:pointer">A. Redis</li>
    <li data-correct="0" style="cursor:pointer">B. MySQL</li>
    <li data-correct="0" style="cursor:pointer">C. PostgreSQL</li>
    <li data-correct="0" style="cursor:pointer">D. Oracle</li>
  </ul>
</div>
<a class="completeBtn" href="javascript:void(0)" onclick="submitAll()">提交</a>
<script>
document.querySelectorAll('.questionLi li').forEach(function (li) {
  li.addEventListener('click', function () {
    var box = li.closest('.questionLi');
    box.querySelectorAll('li').forEach(function (x) { x.classList.remove('sel'); });
    li.classList.add('sel');
  });
});
function submitAll() {
  var ok = 0, total = 0;
  document.querySelectorAll('.questionLi').forEach(function (q) {
    total += 1;
    var sel = q.querySelector('li.sel');
    if (sel && sel.getAttribute('data-correct') === '1') { ok += 1; }
  });
  document.getElementById('result').textContent = '得分：' + ok + '/' + total;
  fetch('/api/event?msg=' + encodeURIComponent('quiz-submit:' + ok + '/' + total));
}
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音访问日志
        return

    def _send(self, body: str, code: int = 200, ctype: str = "text/html; charset=utf-8"):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)

        if path == "/":
            record("GET /")
            return self._send(COURSE_PAGE)
        if path == "/visit/interaction":
            record("GET /visit/interaction")
            return self._send(COURSE_PAGE)
        if path == "/mycourse/studentcourse":
            record("GET /mycourse/studentcourse?" + u.query)
            return self._send(CHAPTER_PAGE)
        if path == "/knowledge":
            record("GET /knowledge?" + u.query)
            return self._send(KNOWLEDGE_PAGE)
        if path == "/video":
            record("GET /video?" + u.query)
            return self._send(VIDEO_PAGE)
        if path == "/quiz":
            record("GET /quiz?" + u.query)
            return self._send(QUIZ_PAGE)
        if path == "/api/event":
            msg = (qs.get("msg") or [""])[0]
            record("EVENT " + msg)
            return self._send(json.dumps({"ok": True}), ctype="application/json")
        if path == "/api/events":
            return self._send(json.dumps(EVENTS, ensure_ascii=False), ctype="application/json")
        return self._send("not found", 404)


def serve(port: int = 8799) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
