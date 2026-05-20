# Prototypes Assets

本目录统一使用通用资源命名，避免业务语义耦合到文件名。

## 通用入口

- `prototype.css`: 默认原型页面样式入口（可直接在 `docs/prototypes/*.html` 引用）。
- `prototype.js`: 默认原型页面交互入口（状态推进、场景切换、时间线渲染）。
- `prototype-common.css`: 跨页面共享样式变量和基础组件。

## 页面资源命名

- `prototype-page-01.css` / `prototype-page-01.js`
- `prototype-page-02.css` / `prototype-page-02.js`
- `prototype-page-03.css` / `prototype-page-03.js`
- `prototype-page-04.css` / `prototype-page-04.js`
- `prototype-page-05.css` / `prototype-page-05.js`

新增页面时建议按 `prototype-page-xx.*` 延续，保证目录可维护性。
