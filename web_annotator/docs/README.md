# ZhaoLora 网页标注工具

面向 LoRA / Pony 训练数据整理的本地 Web 流水线。

## 启动

```bash
python web_annotator/server.py --data-dir datasets/chen_bin/ --default-crop 1536x1536@1.0
```

默认监听 `http://0.0.0.0:8765`。顶栏「首页」进入本文档；其余入口为各功能模块。

## 推荐流程

1. [美学](aesthetic.md) — 打分并生成报告  
2. [筛选](filter.md) — 浏览、排序、删除不合格样本  
3. [去重](dedupe.md) — 相似图分组清理  
4. [裁剪](crop.md) — 部位选区输出  
5. [蒙版](paint.md) — 绘制 Alpha（可选）  
6. [标签](tags.md) — WD14 自动生成 + 频率统计  
7. [清洗](tag-clean.md) — 规范化旁路 `.txt`，可选写入 Pony 美学档位  
8. [就绪](ready.md) — 生成训练用 TOML 片段  

## 文档索引

| 文档 | 说明 |
|------|------|
| [目录约定](layout.md) | 数据集布局、元数据、路径规则、启动参数 |
| [美学](aesthetic.md) | 美学评分与报告 |
| [筛选](filter.md) | 瀑布流筛选与回收站 |
| [去重](dedupe.md) | 指纹去重 |
| [裁剪](crop.md) | 多选区吸附裁剪 |
| [蒙版](paint.md) | Alpha 绘制 |
| [标签](tags.md) | 频率统计与 WD14 打标 |
| [清洗](tag-clean.md) | 旁路标签清洗流水线 |
| [就绪](ready.md) | 训练子集 TOML 导出 |

## 全局约定摘要

- **数据集根**由 `--data-dir` 指定（可指向含 `main/` 的根，或直接指向 `main/`）。
- 筛选 / 去重 / 美学 / 标签 / 清洗：路径形如 `main/foo.png`（相对根）。
- 裁剪 / 蒙版：路径相对 `main/`，形如 `foo.png`；目录选择器隐藏。
- 右上角目录多选状态保存在浏览器 `localStorage`（键 `annotator_work_dirs`）。
