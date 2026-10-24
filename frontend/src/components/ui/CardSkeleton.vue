<script setup lang="ts">
/** 列表加载中的单张卡片骨架屏。
 *
 * 几何尺寸对齐 `NovelCard`（rounded-xl / border / p-5 / gap-6 栅格宽度由接线方决定），
 * 这样首卡出现时不会因为高度变化把列表顶得一跳。
 *
 * 只渲染**一张**卡的占位 —— 挂载与重复由接线方用 `v-for` 决定。
 * 刻意不带 `novel` prop、不渲染任何文字：它只是视觉占位，不承载数据。
 */
</script>

<template>
  <div
    aria-hidden="true"
    data-testid="card-skeleton"
    class="animate-pulse relative bg-white rounded-xl shadow-sm overflow-hidden flex flex-col border border-gray-100"
  >
    <div class="p-5 grow flex flex-col">
      <!-- 标题：1–2 行 -->
      <div class="mb-2 space-y-1.5">
        <div class="h-4 w-11/12 rounded bg-gray-200" data-testid="skeleton-block"></div>
        <div class="h-4 w-2/3 rounded bg-gray-200" data-testid="skeleton-block"></div>
      </div>
      <!-- 作者 -->
      <div class="mb-3 flex items-center gap-2">
        <div class="h-3 w-10 rounded bg-gray-200" data-testid="skeleton-block"></div>
        <div class="h-3 w-24 rounded bg-gray-200" data-testid="skeleton-block"></div>
      </div>
      <!-- 标签：若干小圆角条 -->
      <div class="flex flex-wrap gap-1">
        <div class="h-4 w-12 rounded bg-gray-200" data-testid="skeleton-block"></div>
        <div class="h-4 w-16 rounded bg-gray-200" data-testid="skeleton-block"></div>
        <div class="h-4 w-10 rounded bg-gray-200" data-testid="skeleton-block"></div>
        <div class="h-4 w-14 rounded bg-gray-200" data-testid="skeleton-block"></div>
      </div>
    </div>

    <!-- 页脚：对齐真实卡片的 px-5 py-4 bg-gray-50 border-t 一行 -->
    <div
      data-testid="skeleton-footer"
      class="px-5 py-4 bg-gray-50 border-t border-gray-100 flex items-center justify-between gap-3"
    >
      <div class="flex items-center gap-3">
        <div class="h-3 w-10 rounded bg-gray-200" data-testid="skeleton-block"></div>
        <div class="h-3 w-10 rounded bg-gray-200" data-testid="skeleton-block"></div>
        <div class="h-3 w-14 rounded bg-gray-200" data-testid="skeleton-block"></div>
      </div>
      <div class="h-4 w-8 rounded bg-gray-200" data-testid="skeleton-block"></div>
    </div>
  </div>
</template>
