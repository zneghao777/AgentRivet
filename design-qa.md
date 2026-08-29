# 左侧导航与历史会话区 Design QA

本报告验证左侧功能轨、历史会话抽屉，以及本轮新增的玻璃分裂和融回动效。
对话区与上下文区不在修改范围内，仅作为布局联动的视觉背景。

## 验证目标

本轮修复用户截图中可见的高亮错位、悬停提示遮挡，以及历史抽屉突然消失问题。

- 问题截图：
  `/var/folders/n4/lqfj9l312d51ytfvxzgndmz80000gn/T/`
  `codex-clipboard-d01c0adf-9ecf-4303-9edf-74ecbb2515ba.png`。
- 视觉方向：
  `frontend/design-references/sidebar-rail-glass-motion-spec.png`。
- 折叠与 tooltip 实现：
  `design-qa-final-collapsed-tooltip.jpg`。
- 展开实现：`design-qa-final-expanded-after-motion.jpg`。
- 分裂中间帧：`design-qa-motion-split.jpg`。
- 融回中间帧：`design-qa-motion-merge.jpg`。

## 视口与图像规格

主验收在 1536 × 1024 CSS 像素、1× 密度下完成。问题截图为桌面页面的
466 × 1068 局部裁切，因此聚焦对照将最终实现裁切至相同的 466px 宽度，
并通过垂直留白统一为 466 × 1068；没有对内容进行拉伸。

响应式补充检查覆盖 900 × 720 和 760 × 720。两个视口均无页面级横向或
纵向溢出。

## 对照证据

全视图和动效状态对照使用 `design-qa-motion-contact-sheet.jpg`。该图从左到右
展示分裂中间帧、融回中间帧，以及最终展开态。

聚焦问题对照使用 `design-qa-sidebar-bugfix-comparison.png`。左侧为用户提供的
问题截图，右侧为修复后的折叠态和 Agent 管理 tooltip。

## Findings

本轮最终对照未发现仍需处理的 P0、P1 或 P2 问题。

- 高亮层与目标按钮的 x、y、宽度和高度偏差均为 0px。
- tooltip 完整显示在对话面板上方，不再只露出左侧边缘。
- 分裂与融回过程中抽屉始终保持可见的玻璃形态，没有中途切为
  `visibility: hidden`。
- 动效结束后，侧栏和工作区不残留 transform；页面级滚动保持为 0。

## 必要视觉面检查

### 字体与排版

字体、字号、字重、行高和截断规则保持上一版实现。新增动效只改变面板的
transform 和 opacity，没有改变文字换行或会话密度。

### 间距与布局节奏

功能轨保持 64px，历史抽屉保持 224px，间距保持 8px。功能轨圆角为 26px，
抽屉圆角为 28px。动效结束前后，工作区与侧栏边界连续，无跳帧位移。

### 色彩与视觉 token

继续使用蓝白半透明毛玻璃、26px blur 和 145% saturation。分裂连接面复用
相同的蓝色透明度和高光边缘，没有引入新的高饱和颜色。

### 图像与图标质量

本轮没有新增位图资产。图标继续使用项目现有的 Material Symbols，尺寸和
光学重量保持一致；没有使用占位图、CSS 绘图或近似图标。

### 文案与内容

导航名称、会话标题、搜索文案和操作文案均未改变。tooltip 显示真实功能名，
没有把调试说明暴露到界面。

## 交互检查

交互检查覆盖鼠标悬停、展开、收起、列表滑动高亮，以及响应式布局。

- 功能轨和历史列表高亮均使用统一坐标系，测得偏差为 0px。
- 约 620ms 的 GSAP Flip 与 Timeline 协调工作区和玻璃面板。
- 融回中间帧的抽屉宽度依次约为 152px、27px 和 0px。
- 分裂中间帧从约 87px 的半透明玻璃面扩展至 224px。
- 文字在玻璃面建立后进入，在融回开始时先退出。
- 900px 视口保留 64px 功能轨；760px 视口切换为底部圆角工具条。
- `prefers-reduced-motion` 分支跳过形变并直接提交布局状态。
- 浏览器控制台没有 error 或 warning。

## 比较历史

1. P1：功能轨悬停高亮向左偏移 8px、向上偏移 76px。原因是嵌套按钮的
   `offsetLeft` 和 `offsetTop` 属于不同 offset parent。修复后改为基于
   `getBoundingClientRect()` 的同一坐标系，最终偏差为 0px。
2. P1：折叠态 tooltip 被对话区覆盖。原因是侧栏的隔离堆叠上下文没有外部
   z-index。修复后侧栏建立明确层级，tooltip 在聚焦对照中完整可见。
3. P1：收起时抽屉突然消失。原因是在约 100ms 时提交折叠状态，CSS 立即将
   宽度和可见性清零。修复后新增正式的 `splitting` 和 `merging` 阶段，
   抽屉在整个形变周期内保持渲染。
4. P1：首版形变草案的临时 DOM class 被 React 重渲染覆盖，暂停的 Flip
   动画还可能残留 transform。修复后阶段由 React state 管理，Flip 与玻璃
   Timeline 并行播放；最终截图和浏览器测量均无残留状态。

## 实现检查清单

- [x] 修复功能轨和历史列表的高亮坐标。
- [x] 修复折叠态 tooltip 的堆叠层级。
- [x] 实现玻璃抽屉的分裂和融回形变。
- [x] 保持圆角、毛玻璃和历史会话密度。
- [x] 验证桌面与窄屏无页面级溢出。
- [x] 通过 TypeScript 和 Vite 生产构建。

## Follow-up polish

没有阻塞交付的 P3 项。若后续需要更强的物理感，可以在用户确认当前节奏后，
微调连接面的透明度或 620ms 时长，不建议增加弹跳或大幅旋转。

final result: passed
