"""超星字体反爬解码。

题目文字在 DOM 里是一堆生僻字（塬塭塮…），页面用 font-cxsecret 把它们的字形
重新映射成正常汉字。所以题库匹配不上、模型也答不对——它看到的就是乱码。

做法：把页面内联的加密字体取下来，逐字形渲染成位图，再和多个系统字体的候选字
比对。字体投票 + 保持长宽比 + 抗锯齿降采样，把准确率从七成提到九成以上。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .logger import log

RENDER = 64          # 先渲染到这个尺寸
FEAT = 32            # 再降采样做特征
CACHE_FILE = "glyph_cache_v2.npz"

SYS_FONTS = [
    (r"C:\Windows\Fonts\simsun.ttc", 0),
    (r"C:\Windows\Fonts\simsun.ttc", 1),   # 新宋体，字形与宋体略有差异，一起投票
    (r"C:\Windows\Fonts\msyh.ttc", 0),
    (r"C:\Windows\Fonts\simhei.ttf", 0),
]

# 超星会把正常汉字换成康熙部首（⻓ vs 长），解完要转回来
KX_FROM = "⼀⼁⼂⼃⼄⼅⼆⼇⼈⼉⼊⼋⼌⼍⼎⼏⼐⼑⼒⼓⼔⼕⼖⼗⼘⼙⼚⼛⼜⼝⼞⼟⼠⼡⼢⼣⼤⼥⼦⼧⼨⼩⼪⼫⼬⼭⼮⼯⼰⼱⼲⼳⼴⼵⼶⼷⼸⼹⼺⼻⼼⼽⼾⼿⽀⽁⽂⽃⽄⽅⽆⽇⽈⽉⽊⽋⽌⽍⽎⽏⽐⽑⽒⽓⽔⽕⽖⽗⽘⽙⽚⽛⽜⽝⽞⽟⽠⽡⽢⽣⽤⽥⽦⽧⽨⽩⽪⽫⽬⽭⽮⽯⽰⽱⽲⽳⽴⽵⽶⽷⽸⽹⽺⽻⽼⽽⽾⽿⾀⾁⾂⾃⾄⾅⾆⾇⾈⾉⾊⾋⾌⾍⾎⾏⾐⾑⾒⾓⾔⾕⾖⾗⾘⾙⾚⾛⾜⾝⾞⾟⾠⾡⾢⾣⾤⾥⾦⾧⾨⾩⾪⾫⾬⾭⾮⾯⾰⾱⾲⾳⾴⾵⾶⾷⾸⾹⾺⾻⾼髙⾽⾾⾿⿀⿁⿂⿃⿄⿅⿆⿇⿈⿉⿊⿋⿌⿍⿎⿏⿐⿑⿒⿓⿔⿕⺠⻬⻩⻢⻜⻅⺟⻓"
KX_TO = "一丨丶丿乙亅二亠人儿入八冂冖冫几凵刀力勹匕匚匸十卜卩厂厶又口囗土士夂夊夕大女子宀寸小尢尸屮山巛工己巾干幺广廴廾弋弓彐彡彳心戈戶手支攴文斗斤方无日曰月木欠止歹殳毋比毛氏气水火爪父爻爿片牙牛犬玄玉瓜瓦甘生用田疋疒癶白皮皿目矛矢石示禸禾穴立竹米糸缶网羊羽老而耒耳聿肉臣自至臼舌舛舟艮色艸虍虫血行衣襾見角言谷豆豕豸貝赤走足身車辛辰辵邑酉采里金長門阜隶隹雨青非面革韋韭音頁風飛食首香馬骨高高髟鬥鬯鬲鬼魚鳥鹵鹿麥麻黃黍黑黹黽鼎鼓鼠鼻齊齒龍龜龠民齐黄马飞见母长"
KX_TAB = str.maketrans(KX_FROM, KX_TO)

HASH_TABLE_FILE = "font_map_table.json"
_hash_table: dict[str, str] | None = None


def _load_hash_table() -> dict[str, str]:
    """字形轮廓 hash -> uniXXXX。表 1.5 MB，第一次读约 0.3 秒，之后常驻。"""
    global _hash_table
    if _hash_table is not None:
        return _hash_table
    out: dict[str, str] = {}
    path = Path(__file__).resolve().parent.parent / "data" / HASH_TABLE_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for name, h in data.items():
                if name.startswith("uni") and h:
                    out.setdefault(h, name)
            log.info("字体哈希表加载: " + str(len(out)) + " 条")
        except Exception as e:
            log.warning("字体哈希表读取失败: " + str(e))
    _hash_table = out
    return out


def _glyph_hash(glyph) -> str:
    """字形轮廓坐标算 md5，和超星映射表用的同一算法。"""
    if glyph.numberOfContours <= 0:
        return ""
    parts: list[str] = []
    last = 0
    for i in range(glyph.numberOfContours):
        end = glyph.endPtsOfContours[i]
        for j in range(last, end + 1):
            x, y = glyph.coordinates[j]
            parts.append(str(x) + str(y) + str(glyph.flags[j] & 0x01))
        last = end + 1
    return hashlib.md5("".join(parts).encode()).hexdigest()


_cache: dict[str, Any] = {}


def _render_vec(font_path: str, ch: str, index: int = 0):
    """渲染一个字形：裁掉空白、保持长宽比、居中、抗锯齿降采样。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np
    except Exception:
        return None
    try:
        f = ImageFont.truetype(font_path, 52, index=index)
    except Exception:
        return None

    img = Image.new("L", (RENDER * 2, RENDER * 2), 255)
    ImageDraw.Draw(img).text((4, 4), ch, font=f, fill=0)
    a = np.asarray(img, dtype=np.float32)
    ys, xs = np.where(a < 210)
    if len(ys) == 0:
        return None
    a = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    h, w = a.shape
    scale = (RENDER - 8) / max(h, w)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    glyph = Image.fromarray(a.astype("uint8")).resize((nw, nh), Image.LANCZOS)

    canvas = Image.new("L", (RENDER, RENDER), 255)
    canvas.paste(glyph, ((RENDER - nw) // 2, (RENDER - nh) // 2))

    ink = 1.0 - np.asarray(canvas, dtype=np.float32) / 255.0
    small = np.asarray(
        Image.fromarray((ink * 255).astype("uint8")).resize((FEAT, FEAT), Image.BOX),
        dtype=np.float32) / 255.0
    return small.ravel()


def _candidates() -> tuple[list[str], Any]:
    """GB2312 常用字 × 多个系统字体，第一次算完存盘，之后直接读。"""
    if "names" in _cache:
        return _cache["names"], _cache["mat"]
    import numpy as np

    root = Path(__file__).resolve().parent.parent
    cache_path = root / "data" / CACHE_FILE
    if cache_path.exists():
        try:
            z = np.load(cache_path)
            _cache["names"] = list(z["names"])
            _cache["mat"] = z["mat"]
            _cache["common"] = z["common"]
            log.info("字形库从缓存加载: " + str(len(_cache["names"])) + " 字")
            return _cache["names"], _cache["mat"]
        except Exception:
            pass

    names: list[str] = []
    rows: list[Any] = []
    common: list[bool] = []
    # GB2312 一级+二级汉字，覆盖题目里的绝大多数用字
    for hi in range(0xB0, 0xF8):
        for lo in range(0xA1, 0xFF):
            try:
                ch = bytes([hi, lo]).decode("gb2312")
            except Exception:
                continue
            vecs = []
            for fp, idx in SYS_FONTS:
                v = _render_vec(fp, ch, idx)
                if v is not None:
                    vecs.append(v)
            if len(vecs) >= 2:
                names.append(ch)
                rows.append(np.mean(vecs, axis=0))
                # GB2312 一级字 = 常用字，题目里九成是这些
                common.append(hi <= 0xD7)

    # float16 省一半内存，字形比对的精度损失可以忽略
    mat = np.stack(rows).astype(np.float16)
    _cache["names"] = names
    _cache["mat"] = mat
    _cache["common"] = np.array(common)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache_path, names=np.array(names), mat=mat)
        log.info("字形库已建立并缓存: " + str(len(names)) + " 字")
    except Exception as e:
        log.debug("字形库缓存失败: " + str(e))
    return names, mat


JS_GET_FONT = """
() => {
  const out = [];
  for (const s of Array.from(document.styleSheets)) {
    let rules = null;
    try { rules = s.cssRules; } catch (e) { continue; }
    if (!rules) continue;
    for (const r of Array.from(rules)) {
      const src = r.style && r.style.src;
      if (src && src.indexOf('base64') >= 0 && /ttf|woff/i.test(src)) out.push(src);
    }
  }
  return out;
}
"""


class FontDecoder:
    def __init__(self) -> None:
        self.map: dict[str, str] = {}
        self.conf: dict[str, float] = {}
        self.ready = False

    def decode(self, text: str) -> str:
        if not text:
            return ""
        if not self.map:
            return text
        return "".join(self.map.get(ch, ch) for ch in text).translate(KX_TAB)

    def _match_by_hash(self, ttf: Path) -> dict[str, str]:
        """精确匹配：字形轮廓 hash 反查原字。命中就是 100% 准。"""
        table = _load_hash_table()
        if not table:
            return {}
        try:
            from fontTools.ttLib import TTFont
            font = TTFont(str(ttf), lazy=False)
            glyf = font["glyf"]
            order = font.getGlyphOrder()
        except Exception as e:
            log.debug("读字形轮廓失败: " + str(e))
            return {}
        out: dict[str, str] = {}
        for name in order:
            if not name.startswith("uni"):
                continue
            try:
                h = _glyph_hash(glyf[name])
                src = table.get(h) if h else None
                if not src:
                    continue
                out[chr(int(name[3:], 16))] = chr(int(src[3:], 16))
            except Exception:
                continue
        return out

    def _match(self, ttf: Path, cmap) -> dict[str, str]:
        import numpy as np
        names, mat = _candidates()
        out: dict[str, str] = {}
        for code in cmap:
            v = _render_vec(str(ttf), chr(code))
            if v is None:
                continue
            d = np.abs(mat.astype(np.float32) - v).sum(axis=1)
            common = _cache.get("common")
            if common is not None and len(common) == len(d):
                # 生僻字给一点惩罚：字形像但题目里几乎不会出现
                d = d * np.where(common, 1.0, 1.10)
            order = np.argsort(d)[:3]
            best = float(d[order[0]])
            second = float(d[order[1]])
            if best > 300:
                continue        # 太不像，宁可保留原字
            self.conf[chr(code)] = (second - best) / max(second, 1e-6)
            out[chr(code)] = names[int(order[0])]
        return out

    async def build_from_page(self, page) -> int:
        srcs: list[str] = []
        # 必须先看传进来的这个 frame：整页乱扫会先撞上播放器
        # (ananas/modules/video) 里的 font-cxsecret，拿错字体整页解码全废。
        frames: list[Any] = []

        def add(f) -> None:
            if f is not None and all(f is not x for x in frames):
                frames.append(f)

        add(page)
        try:
            owner = getattr(page, "page", None)
            if owner is not None and owner is not page:
                for f in owner.frames:
                    add(f)
        except Exception:
            pass
        if not frames:
            frames = [page]

        for f in frames:
            try:
                got = await f.evaluate(JS_GET_FONT) or []
            except Exception:
                continue
            if got:
                srcs = got
                log.info("在 frame 里找到内联字体: " + (f.url or "")[:60])
                break
        if not srcs:
            log.info("字体解码：没找到内联字体")
            return 0

        m = re.search(r"base64,([A-Za-z0-9+/=]+)", srcs[-1])
        if not m:
            return 0
        try:
            data = base64.b64decode(m.group(1))
        except Exception:
            return 0

        tmp = Path(__file__).resolve().parent.parent / "data" / "fonts"
        tmp.mkdir(parents=True, exist_ok=True)
        ttf = tmp / "current.ttf"
        ttf.write_bytes(data)

        try:
            from fontTools.ttLib import TTFont
        except Exception:
            return 0
        try:
            cmap = TTFont(str(ttf)).getBestCmap()
        except Exception as e:
            log.debug("字体解析失败: " + str(e))
            return 0
        if not cmap:
            return 0

        by_hash = self._match_by_hash(ttf)
        if len(by_hash) >= len(cmap) * 0.6:
            self.map = by_hash
            self.ready = True
            log.info("字体哈希精确匹配 " + str(len(by_hash)) + "/" + str(len(cmap)) + " 个字")
            log.info("  样例: " + "".join("%s->%s " % (k, v)
                                          for k, v in list(self.map.items())[:6]))
            return len(self.map)

        if by_hash:
            log.info("哈希只命中 " + str(len(by_hash)) + "/" + str(len(cmap)) + "，其余走字形比对")
        self.map = dict(by_hash)
        self.map.update(self._match(ttf, cmap))
        self.ready = bool(self.map)
        weak = [k for k, v in self.conf.items() if v < 0.15]
        if self.map:
            log.info("字体解码合计 " + str(len(self.map)) + " 个字" +
                     ("（低置信 " + str(len(weak)) + " 个）" if weak else ""))
        if self.map:
            weak = [k for k, v in self.conf.items() if v < 0.15]
            log.info("字体反爬解码 " + str(len(self.map)) + " 个字" +
                     ("（低置信 " + str(len(weak)) + " 个）" if weak else "") + ": " +
                     "".join("%s->%s " % (k, v) for k, v in list(self.map.items())[:6]))
        return len(self.map)
