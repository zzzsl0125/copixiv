"""「我的上传」上传切片 —— 本地 TXT / EPUB 导入（写路径）。

契约：``docs/LIBRARY_DESIGN.md`` §4（上传管线）/ §4.1（EPUB）与 §7（不变量）。
切片只有一个端点 ``POST /api/library/uploads``；响应形状由前端
``frontend/src/types/uploads.ts`` 钉住，字段名不可改（``epub_attached`` 是
2026-10 新增的**可选**字段，只在补挂时为 true）。
"""
