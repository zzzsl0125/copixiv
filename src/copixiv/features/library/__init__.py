"""「我的上传」上传切片 —— 本地 TXT 导入（写路径）。

契约：``docs/LIBRARY_DESIGN.md`` §4（上传管线）与 §7（不变量）。切片只有
一个端点 ``POST /api/library/uploads``；响应形状由前端
``frontend/src/types/uploads.ts`` 钉住，字段名不可改。
"""
