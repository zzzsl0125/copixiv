"""``core/ids.py`` —— 作者 / 系列 id 的校验规则（三个写入方共用）。

这条规则原本住在 ``features/novels/update_metadata.py``，被同一切片的批量编辑
和**另一个切片**（``features/library/api.py`` 的预设作者）`import _author_id`
去用。规则本身是纯校验、零 I/O，没有理由留在某一个切片的内部 —— 而且两处一旦
漂移，前端按 ``detail`` 提示的行为就分叉。

这里钉住的是**文案契约**（前端按它显示）与四个拒绝分支：非整数、布尔、
0 / 负数、超出 BIGINT。
"""

from __future__ import annotations

import pytest

from copixiv.core.exceptions import ValidationError
from copixiv.core.ids import author_id, positive_pixiv_id, series_id


class TestAccepted:
    @pytest.mark.parametrize("value", [1, 42, 2**63 - 1])
    def test_positive_integers_pass_through(self, value):
        assert author_id(value) == value

    def test_series_uses_the_same_rule(self):
        assert series_id(123) == 123


class TestRejected:
    @pytest.mark.parametrize("value", ["abc", 1.5, None, [], object()])
    def test_non_integers_are_rejected(self, value):
        with pytest.raises(ValidationError) as excinfo:
            author_id(value)
        assert excinfo.value.detail == "作者 ID 必须是整数"

    def test_bool_is_not_an_integer(self):
        """``True`` 是 ``int`` 的子类，但它不该被悄悄当成 1。"""
        with pytest.raises(ValidationError) as excinfo:
            author_id(True)
        assert excinfo.value.detail == "作者 ID 必须是整数"

    @pytest.mark.parametrize("value", [0, -1, -(2**63)])
    def test_zero_and_negatives_are_rejected_with_the_hint(self, value):
        with pytest.raises(ValidationError) as excinfo:
            author_id(value)
        assert excinfo.value.detail == "作者 ID 必须是正整数（pixiv 用户 ID）"

    def test_above_bigint_is_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            author_id(2**63)
        assert excinfo.value.detail == "作者 ID 超出取值范围"

    def test_the_label_and_hint_change_per_kind(self):
        with pytest.raises(ValidationError) as excinfo:
            series_id(0)
        assert excinfo.value.detail == "系列 ID 必须是正整数（pixiv 系列 ID）"


class TestSingleOwner:
    def test_the_per_slice_validators_are_aliases_not_copies(self):
        """两个「私有孪生」必须是同一个函数 —— 规则只有一份。"""
        from copixiv.features.novels.update_metadata import (
            _author_id,
            _series_id,
        )

        assert _author_id is author_id
        assert _series_id is series_id

    def test_positive_pixiv_id_is_the_generic_form(self):
        assert positive_pixiv_id(5, "帖子", "帖子 ID") == 5
        with pytest.raises(ValidationError) as excinfo:
            positive_pixiv_id(0, "帖子", "帖子 ID")
        assert excinfo.value.detail == "帖子 ID 必须是正整数（帖子 ID）"
