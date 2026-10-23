"""``features/library/decode.py`` —— 上传 TXT 的解码与「乱码宁可报错」。

权威：``docs/LIBRARY_DESIGN.md`` §4-1（解码顺序）与 §8.1（已知限制）。

这个文件存在的原因是**模块边界**：解码规则原来住在 ``ingest.py`` 里，而它
与落库、去重、发号没有任何共同理由去改动。契约级的几条断言（评分择一、
UTF-16 的 BOM 门控、可疑占比阈值两侧）在这里单独钉住，``ingest`` 的行为测试
于是只需要关心"解码失败的那条没进库"。
"""

from __future__ import annotations

import pytest

from copixiv.features.library import decode
from copixiv.features.library.decode import (
    DECODE_ERROR,
    MAX_SUSPICIOUS_RATIO,
    TextDecodeError,
    decode_txt,
    is_suspicious_char,
    suspicious_ratio,
)

# 四种候选编码（utf-16 必须带 BOM）全部失败的字节流。
UNDECODABLE = b"\xff\x00\xff\x00\xff"

BIG5_TEXT = "我的書測試：繁體中文正文，第一行。\n第二行，還有標點符號！"
GB18030_TEXT = "我的书测试：简体中文正文，第一行。\n第二行，还有标点符号！"


class TestDecodeTxt:
    def test_utf8_with_bom(self):
        assert decode_txt(b"\xef\xbb\xbf" + "我的书".encode("utf-8")) == "我的书"

    def test_utf8_plain(self):
        assert decode_txt("中文正文".encode("utf-8")) == "中文正文"

    def test_ascii(self):
        assert decode_txt(b"plain ascii text\n") == "plain ascii text\n"

    def test_gb18030(self):
        assert decode_txt(GB18030_TEXT.encode("gb18030")) == GB18030_TEXT

    def test_utf16_with_bom(self):
        data = "中文正文测试".encode("utf-16")   # codec writes the BOM itself
        assert decode_txt(data) == "中文正文测试"

    def test_utf16_is_bom_gated(self):
        """无 BOM 的偶数长字节流**不**走 utf-16（否则二进制会被静默吃成乱码）。"""
        with pytest.raises(TextDecodeError):
            decode_txt(b"\x80\x81" * 8)

    def test_all_encodings_fail(self):
        with pytest.raises(TextDecodeError) as excinfo:
            decode_txt(UNDECODABLE)
        assert str(excinfo.value) == DECODE_ERROR

    def test_big5_traditional_decodes_exactly(self):
        """Big5 正文不能被 GB18030 抢走（评分择一的意义所在）。"""
        assert decode_txt(BIG5_TEXT.encode("big5")) == BIG5_TEXT

    def test_gb18030_simplified_is_not_stolen_by_big5(self):
        assert decode_txt(GB18030_TEXT.encode("gb18030")) == GB18030_TEXT

    def test_implausible_bytes_are_rejected_not_mojibake(self):
        with pytest.raises(TextDecodeError):
            decode_txt(bytes(range(0xF5, 0xFF)) * 8)


class TestSuspiciousRatio:
    def test_empty_text_is_zero(self):
        assert suspicious_ratio("") == 0.0

    def test_suspicious_categories(self):
        assert is_suspicious_char("\ue000")      # Co 私有区
        assert is_suspicious_char("\ufffd")      # 替换符（类别其实是 So）
        assert is_suspicious_char("\x00")        # Cc 控制
        assert is_suspicious_char("\u200b")      # Cf 格式（零宽空格）
        assert not is_suspicious_char("中")
        assert not is_suspicious_char("A")
        for benign in ("\t", "\n", "\r"):
            assert not is_suspicious_char(benign), "换行/制表符不算可疑"

    def test_ratio_math(self):
        assert suspicious_ratio("阿" * 98 + "\ue000" * 2) == pytest.approx(0.02)
        assert suspicious_ratio("阿" * 97 + "\ue000" * 3) == pytest.approx(0.03)
        assert suspicious_ratio("正文\t第一行\n第二行\r") == 0.0


class TestThreshold:
    """阈值两侧的边界（判据是 ``> MAX_SUSPICIOUS_RATIO``，不是 ``>=``）。

    把候选收窄到 ``gb18030`` 一个：否则样本里的私有区字符会先把另一个候选
    淘汰掉，测到的就不是阈值本身了。
    """

    def test_exactly_at_threshold_is_accepted(self, monkeypatch):
        monkeypatch.setattr(decode, "_SCORED_ENCODINGS", ("gb18030",))
        sample = "阿" * 98 + "\ue000" * 2
        assert suspicious_ratio(sample) == pytest.approx(MAX_SUSPICIOUS_RATIO)
        assert decode_txt(sample.encode("gb18030")) == sample

    def test_just_above_threshold_is_rejected(self, monkeypatch):
        monkeypatch.setattr(decode, "_SCORED_ENCODINGS", ("gb18030",))
        sample = "阿" * 97 + "\ue000" * 3
        assert suspicious_ratio(sample) > MAX_SUSPICIOUS_RATIO
        with pytest.raises(TextDecodeError) as excinfo:
            decode_txt(sample.encode("gb18030"))
        assert str(excinfo.value) == DECODE_ERROR

    def test_candidate_above_threshold_is_rejected(self, monkeypatch):
        """最佳候选的可疑占比仍 > 2% → 判失败（钉住阈值本身）。"""
        monkeypatch.setattr(decode, "_SCORED_ENCODINGS", ("gb18030",))
        data = ("阿" * 10 + "\ue000" * 3).encode("gb18030")   # 3/13 ≈ 23%
        with pytest.raises(TextDecodeError):
            decode_txt(data)
