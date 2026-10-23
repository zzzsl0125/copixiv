"""``storage/assets.py`` —— 小说图片资产的文件命名约定。

这份约定原本抄在四个地方（下载侧拼名字、builder 回找、检查任务 glob、
孤儿回收反解）。这里钉住"名字长什么样"与"怎么从名字认出它"，因为写错一次的
症状不是报错，而是「图片明明下载了，EPUB 里却是缺图框」或者「把真书的插图当
孤儿删掉」。
"""

from __future__ import annotations

import pytest

from copixiv.storage import assets


def _touch(path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class TestNames:
    def test_the_three_shapes(self, tmp_path):
        assert assets.asset_path(tmp_path, 7, assets.COVER).name == "7_c_cover.jpg"
        assert assets.asset_path(
            tmp_path, 7, assets.UPLOADED, 42,
        ).name == "7_u_42.jpg"
        assert assets.asset_path(
            tmp_path, 7, assets.ILLUSTRATION, 90, ".png",
        ).name == "7_p_90.png"

    def test_cover_ignores_the_asset_id(self, tmp_path):
        """封面没有编号 —— 传了也不进名字（否则回找永远查不到）。"""
        assert assets.asset_path(
            tmp_path, 7, assets.COVER, "ignored",
        ).name == "7_c_cover.jpg"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://i.pximg.net/img/1.jpg", ".jpg"),
            ("https://i.pximg.net/img/1.png?x=1", ".png"),
            ("https://i.pximg.net/img/1", ".jpg"),        # 无后缀 → 默认
        ],
    )
    def test_suffix_comes_from_the_url_with_a_default(self, url, expected):
        assert assets.asset_suffix(url) == expected


class TestLookup:
    def test_find_cover_tries_known_suffixes(self, tmp_path):
        _touch(tmp_path / "7_c_cover.png")
        assert assets.find_cover(tmp_path, 7).name == "7_c_cover.png"

    def test_find_cover_is_none_when_absent(self, tmp_path):
        assert assets.find_cover(tmp_path, 7) is None

    def test_iter_assets_picks_only_ids_with_a_file(self, tmp_path):
        _touch(tmp_path / "7_u_1.jpg")
        _touch(tmp_path / "7_p_2.png")

        found = assets.iter_assets(
            tmp_path, 7,
            [(assets.UPLOADED, "1"), (assets.UPLOADED, "2"),
             (assets.ILLUSTRATION, "2"), (assets.ILLUSTRATION, "3")],
        )

        assert set(found) == {"1", "2"}
        assert found["2"].name == "7_p_2.png"     # 先到先得（u 那一份不在盘上）

    def test_scan_assets_falls_back_to_the_directory(self, tmp_path):
        _touch(tmp_path / "7_u_5.jpg")
        _touch(tmp_path / "7_p_6.jpg")
        _touch(tmp_path / "7_c_cover.jpg")
        _touch(tmp_path / "8_u_1.jpg")            # 别的书
        _touch(tmp_path / "7_u_5.txt")            # 不是图片后缀

        found = assets.scan_assets(tmp_path, 7)

        # 封面不是"插图"，扫描只收 u / p。
        assert set(found) == {"5", "6"}


class TestNovelId:
    @pytest.mark.parametrize(
        ("stem", "expected"),
        [
            ("12345_u_678", 12345),
            ("12345_c_cover", 12345),
            ("12345_p_90", 12345),
            ("12345_u_", None),          # 缺资产号
            ("我的书_-42", None),        # 正文文件名，不是资产
            ("NOTES", None),
        ],
    )
    def test_asset_stems(self, stem, expected):
        assert assets.novel_id_of(stem) == expected

    def test_illustrations_are_counted_but_not_the_cover(self, tmp_path):
        """``has_illustration_assets``：只有封面不算"插图曾经落过盘"。"""
        _touch(tmp_path / "7_c_cover.jpg")
        assert assets.has_illustration_assets(tmp_path, 7) is False

        _touch(tmp_path / "7_u_1.jpg")
        assert assets.has_illustration_assets(tmp_path, 7) is True

    def test_another_novels_assets_do_not_count(self, tmp_path):
        _touch(tmp_path / "70_u_1.jpg")
        assert assets.has_illustration_assets(tmp_path, 7) is False
