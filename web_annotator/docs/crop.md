# 裁剪

路径：`/crop`

仅处理 `main/` 中的源图。支持多选区、吸附训练分辨率桶，输出到部位目录或 `crops/`。

## 目录范围

- 目录选择器**隐藏**；输入固定为 `main/`。
- API 路径相对 `main`（如 `foo.png`）。
- 输出写在数据集根下：`head/`、`foot/`、`crops/` 等。

## 默认裁切

启动参数 `--default-crop WxH@Z`（例：`1536x1536@1.0`）经 `GET /api/crop/config` 下发。

- 有默认值：每张图新建选区按该目标匹配。
- 无默认值或无法匹配：按整图用 `chooseBest` 拟合候选桶。

## 吸附网格

前后端一致：

- 边长：512–3072，步长 128
- zoom：`1`、`1.5`、`2`、`2.5`、`3`、`4`
- 最大输出像素：`1536 × 1536`

拖拽 / 缩放选区时前端吸附；后端保存时对坐标 clamp，以请求中的 `target_w/h`、`zoom` 为准。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/crop/images` | 列表；`done` 表示任一部位或 `crops` 已有同 stem 输出 |
| GET | `/api/crop/image?path=` | 原尺寸预览（AVIF） |
| GET | `/api/crop/candidates` | 候选网格 |
| GET | `/api/crop/config` | root、main、mask、已有部位、`default_crop` |
| POST | `/api/crop/save` | 见下 |

### 保存 body

**多选区裁切**

```json
{
  "path": "foo.png",
  "crops": [
    {
      "x1": 0, "y1": 0, "x2": 1024, "y2": 1024,
      "target_w": 1536, "target_h": 1536, "zoom": 1.0,
      "name": "head"
    }
  ]
}
```

- 裁切 ROI 后 LANCZOS 缩放到 `target_w × target_h`，存 PNG。
- 有合法 `name` → `<root>/{name}/{stem}.png`
- 无 `name` → `<root>/crops/{stem}.png`；同批多区时用 `stem_001.png` 等
- 目标已存在冲突时追加 `_02` 等后缀

**整图写入部位（不裁切缩放）**

```json
{ "path": "foo.png", "no_crop_resize": true, "part_name": "full" }
```

原尺寸写入 `<root>/{part_name}/{stem}.png`。

## 自动部位

- `localStorage`：`crop_auto_parts:{root}`、否认列表 `crop_auto_parts_deny:{root}`
- 服务器已有部位目录名会自动纳入（否认除外）
- 启用的部位：打开新图时自动生成同名居中默认选区
- 部位名规则：`^[\w\-]{1,64}$`，不可用保留目录名

## 导航

- 列表优先未 `done` 项。
- 保存成功后跳到下一未完成。
- 「下一张」类操作优先跳未完成。

## 快捷键

| 键 | 行为 |
|----|------|
| Ctrl/Cmd+S 或 S | 保存并下一未完成（输入框内 Ctrl+S 亦生效） |
| Ctrl/Cmd+N、Space、→ | 下一未完成 |
| ← | 上一张 |
| A | 添加选区 |
| C | 重置为默认选区 |
| Delete / Backspace / D | 删除当前选区 |
| 滚轮（多选区时） | 切换活动选区 |
| Enter（部位名输入框） | 添加部位 |

## 边界

- 后端不对 zoom 桶做二次吸附，以前端提交为准。
- `done` 判定：部位目录或 `crops` 中存在同 stem 或 `stem_*` 的 PNG。
