"""`core/language.py` — the CJK heuristic that replaced ``langid``.

The old implementation asked ``langid.classify(title + caption) == "zh"``,
which is wrong exactly where Pixiv is hardest: a 2–4 character Chinese title
carries no statistical signal (``「道歉」`` → ``ja``), while a Latin title with
a few han characters reads as ``zh``.  A full-corpus comparison over 245,755
rows found 223 flips (0.09 %), 204 of them now correctly accepted.

These tests pin the rule order (tag → kana → han ratio) and the edge cases the
old classifier got wrong, so the heuristic cannot regress silently.
"""

from __future__ import annotations

import pytest

from copixiv.core.language import filter_chinese, is_chinese


class TestTagPriority:
    def test_explicit_chinese_tag_wins_over_kana(self):
        # An author tagging the work 中文 is authoritative even if the sample
        # happens to contain kana (a Japanese character name, an onomatopoeia).
        assert is_chinese(
            title="日本語ぽいタイトル", tags=["中文", "R-18"],
        ) is True

    @pytest.mark.parametrize("tag", ["简体中文", "繁体", "中國語", "chinese", "中文"])
    def test_tag_synonyms(self, tag):
        assert is_chinese(title="x", tags=[tag]) is True

    def test_unrelated_tags_do_not_force_true(self):
        assert is_chinese(title="Europe", caption="english text", tags=["R-18"]) is False


class TestKanaExclusion:
    @pytest.mark.parametrize("sample", [
        "これは日本語の小説です",
        "テスト",
        "カタカナだけのタイトル",
        "中文正文但标题有 ひらがな",
        "第一章 あとがき",
    ])
    def test_any_kana_rejects(self, sample):
        assert is_chinese(title=sample) is False

    def test_kana_in_caption_also_rejects(self):
        assert is_chinese(title="普通标题", caption="ちょっとした説明") is False


class TestHanRatio:
    @pytest.mark.parametrize("title", [
        "道歉",            # 2 han — langid said ja
        "嫉妒",
        "【光与暗 Ⅰ】",
        "你这是要起飞啊",
        "第一章 开始",
    ])
    def test_short_chinese_titles_are_accepted(self, title):
        assert is_chinese(title=title) is True

    def test_mostly_han_with_punctuation_is_accepted(self):
        # Digits and punctuation must not dilute the ratio.
        assert is_chinese(title="216~220 章") is True
        assert is_chinese(title="1、206章兽交") is True

    def test_latin_title_is_rejected(self):
        assert is_chinese(title="Roll over") is False
        assert is_chinese(title="Catch and Caught") is False
        assert is_chinese(title="Merry Merry！") is False

    def test_english_lyrics_caption_do_not_override_a_latin_title(self):
        assert is_chinese(
            title="LO(VE)st Control.Ⅱ",
            caption="I AM THE STORM THAT IS APPROACHING<br />BURY THE LIGHT",
        ) is False

    def test_html_and_urls_are_stripped_before_measuring(self):
        # A Chinese title with a shop caption full of markup/links stays 中文.
        assert is_chinese(
            title="淫魔神 436 章",
            caption=(
                '<a href="https://www.fansky.net/x">buy</a><br />'
                "https://example.com/a/b/c/d/e/f"
            ),
        ) is True

    def test_latin_dominant_sample_is_rejected(self):
        assert is_chinese(
            title="The Ranee Neise（四）",
            caption="this is mostly an english description",
        ) is False

    def test_emoji_only_title_is_rejected(self):
        assert is_chinese(title="💢，🐘🍑，⚡🌪️🍺") is False


class TestEdgeCases:
    def test_empty_title_and_caption(self):
        assert is_chinese() is False
        assert is_chinese(title="", caption="") is False
        assert is_chinese(title="   ", caption=None) is False

    def test_whitespace_only_is_rejected(self):
        assert is_chinese(title="\n\t ") is False

    def test_single_han_character_still_counts(self):
        """One han and no latin is a Chinese title — ``langid`` agreed.

        Measured on the real corpus: keeping the bar at ``han >= 1`` preserves
        exactly the rows ``langid`` already accepted (``海``, ``壹``, ``216~220
        章``); raising it to 2 silently drops them.  Digits/emoji are not
        Latin, so they do not disqualify the sample.
        """
        assert is_chinese(title="海") is True
        assert is_chinese(title="216~220 章") is True

    def test_two_han_characters_is_accepted(self):
        assert is_chinese(title="海风") is True

    def test_single_han_plus_latin_is_rejected(self):
        # Mixed samples need the ratio: one han among a lot of English is not
        # evidence of a Chinese work.
        assert is_chinese(title="Sea 海 and the long english title here") is False

    def test_none_caption_and_tags(self):
        assert is_chinese(title="中文标题", caption=None, tags=None) is True


class TestNoClassifierDependency:
    def test_langid_is_not_imported(self):
        """The dependency is gone; importing the module must not pull it in."""
        import sys

        assert "langid" not in sys.modules
        import copixiv.core.language  # noqa: F401

        assert "langid" not in sys.modules

    def test_detection_is_pure_and_repeatable(self):
        # No I/O, no caching surprises: same input, same answer.
        sample = ("道歉", "", ["R-18"])
        assert [is_chinese(*sample) for _ in range(3)] == [True, True, True]


class TestFilterChinese:
    """列表级封装：形状适配归这里，规则仍归 ``is_chinese``。

    ``features/novels/ingest.py`` 曾经自己抄了一份这个循环，而唯一的调用者是
    ``tasks/novels.py`` 的四条任务路径 —— 一个与抓取管线无关的工具住在管线里。
    """

    def test_dict_and_object_entries_are_both_accepted(self):
        from types import SimpleNamespace

        novels = [
            {"title": "中文作品", "caption": "", "tags": []},
            SimpleNamespace(title="中文作品二", caption="", tags=[]),
            {"title": "Roll over", "caption": "", "tags": []},     # 拉丁 → 拒
        ]

        kept = filter_chinese(novels)

        # 返回原对象（不做拷贝）：调用方后面还要拿它们去下载。
        titles = [n["title"] if isinstance(n, dict) else n.title for n in kept]
        assert titles == ["中文作品", "中文作品二"]

    def test_a_tag_name_may_be_a_dict_or_a_string(self):
        novels = [
            {"title": "x", "caption": "", "tags": [{"name": "中文"}]},
            {"title": "y", "caption": "", "tags": ["中文"]},
        ]

        assert len(filter_chinese(novels)) == 2

    def test_missing_fields_default_to_empty(self):
        """形状不全的条目不该抛异常 —— 缺字段就是"没有这条信息"。"""
        assert filter_chinese([{"title": "中文作品"}]) != []
        assert filter_chinese([{}]) == []

    def test_an_empty_input_is_an_empty_output(self):
        assert filter_chinese([]) == []

    def test_entries_without_a_title_or_caption_are_dropped(self):
        assert filter_chinese([{"tags": ["其他"]}]) == []
